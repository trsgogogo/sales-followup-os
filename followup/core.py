"""Transactional workflow engine. All timestamps are UTC epoch seconds."""
import json
import math
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager


class Invalid(ValueError):
    pass


def text(value, name, maximum=10000, required=False):
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise Invalid("Invalid " + name)
    return value.strip()


class Engine:
    def __init__(self, path="data/followup.db", owners=None, clock=time.time,
                 first_response_minutes=10, quote_days=(3, 7, 14), meeting_hours=24):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.clock = clock
        self.owners = owners or ["sales"]
        self.first = first_response_minutes * 60
        self.quote_days = quote_days
        self.meeting = meeting_hours * 3600
        if not quote_days or any(not math.isfinite(v) or v <= 0 for v in (self.first, self.meeting, *quote_days)):
            raise Invalid("Timing settings must be positive")
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS leads(
          id TEXT PRIMARY KEY, external_id TEXT UNIQUE, name TEXT NOT NULL,
          company TEXT NOT NULL, email TEXT NOT NULL, source TEXT NOT NULL,
          owner TEXT NOT NULL, notes TEXT NOT NULL, stage TEXT NOT NULL DEFAULT 'new',
          created REAL NOT NULL, updated REAL NOT NULL, last_contact REAL,
          quote_at REAL, meeting_at REAL, next_due REAL, snooze_until REAL,
          opted_out INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS events(
          id TEXT PRIMARY KEY, lead_id TEXT NOT NULL REFERENCES leads(id),
          kind TEXT NOT NULL, body TEXT NOT NULL, created REAL NOT NULL,
          idempotency_key TEXT UNIQUE);
        CREATE TABLE IF NOT EXISTS tasks(
          id TEXT PRIMARY KEY, lead_id TEXT NOT NULL REFERENCES leads(id),
          rule TEXT NOT NULL, anchor TEXT NOT NULL, due REAL NOT NULL,
          status TEXT NOT NULL DEFAULT 'open', summary TEXT NOT NULL,
          draft TEXT NOT NULL, approved_version INTEGER,
          created REAL NOT NULL, UNIQUE(lead_id, rule, anchor));
        CREATE TABLE IF NOT EXISTS outbox(
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
          status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
          next_attempt REAL NOT NULL, last_error TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        # An interrupted SMTP send is ambiguous: never automatically resend it.
        self.db.execute("UPDATE tasks SET status='delivery_unknown' WHERE status='sending'")

    @contextmanager
    def tx(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def lead(self, lid):
        row = self.db.execute("SELECT * FROM leads WHERE id=?", (lid,)).fetchone()
        if not row:
            raise KeyError("Lead not found")
        return dict(row)

    def _cancel(self, lid):
        self.db.execute("UPDATE tasks SET status='cancelled',approved_version=NULL "
                        "WHERE lead_id=? AND status IN ('open','approved')", (lid,))
        self.db.execute("UPDATE outbox SET status='cancelled' WHERE status='pending' "
                        "AND task_id IN (SELECT id FROM tasks WHERE lead_id=? AND status='cancelled')", (lid,))

    def create(self, data):
        name = text(data.get("name", ""), "name", 200, True)
        email = text(data.get("email", ""), "email", 254)
        if email and ("@" not in email or any(c.isspace() for c in email)):
            raise Invalid("Invalid email")
        fields = {k: text(data.get(k, ""), k, 10000 if k == "notes" else 200)
                  for k in ("company", "source", "notes", "owner", "external_id")}
        with self.tx():
            if fields["external_id"]:
                found = self.db.execute("SELECT id FROM leads WHERE external_id=?", (fields["external_id"],)).fetchone()
                if found:
                    return self.lead(found["id"])
            count = self.db.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
            lid, now = uuid.uuid4().hex, self.clock()
            owner = fields["owner"] or self.owners[count % len(self.owners)]
            self.db.execute("INSERT INTO leads(id,external_id,name,company,email,source,owner,notes,created,updated) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?)", (lid, fields["external_id"] or None, name,
                            fields["company"], email, fields["source"], owner, fields["notes"], now, now))
            self._log(lid, "created", {"source": fields["source"]})
            return self.lead(lid)

    def _log(self, lid, kind, body, key=None):
        self.db.execute("INSERT INTO events VALUES(?,?,?,?,?,?)",
                        (uuid.uuid4().hex, lid, kind, json.dumps(body, ensure_ascii=False), self.clock(), key))

    def event(self, lid, data):
        kind = data.get("kind")
        allowed = {"contacted", "reply", "quote", "meeting", "next_step", "won", "lost", "optout", "snooze", "resume", "assign", "note"}
        if not isinstance(kind, str) or kind not in allowed:
            raise Invalid("Unknown event kind")
        body = {"text": text(data.get("text", ""), "text")}
        key = text(data.get("idempotency_key", ""), "idempotency_key", 200) or None
        if kind in ("next_step", "snooze"):
            due = data.get("due")
            if isinstance(due, bool) or not isinstance(due, (float, int)) or not self.clock() < due < self.clock() + 10 * 365 * 86400:
                raise Invalid("due must be a future UTC epoch timestamp (within 10 years)")
            body["due"] = due
        if kind == "assign":
            body["owner"] = text(data.get("owner", ""), "owner", 200, True)
        with self.tx():
            lead = self.lead(lid)
            if key:
                previous = self.db.execute("SELECT * FROM events WHERE idempotency_key=?", (key,)).fetchone()
                if previous:
                    if previous["lead_id"] != lid or previous["kind"] != kind or json.loads(previous["body"]) != body:
                        raise Invalid("Idempotency key was used with a different event")
                    return lead
            now = self.clock()
            updates = {"updated": now, "version": lead["version"] + 1}
            closed = lead["stage"] in ("won", "lost", "optout")
            if closed and kind not in ("note", "assign", "resume"):
                raise Invalid("Lead is closed; explicitly resume before changing its workflow")
            if kind == "resume" and lead["opted_out"]:
                raise Invalid("Opted-out leads cannot be resumed in this application")
            if kind == "contacted":
                updates.update(last_contact=now, stage="contacted" if lead["stage"] == "new" else lead["stage"])
            elif kind == "reply":
                updates.update(stage="replied", quote_at=None, meeting_at=None, next_due=None, snooze_until=None)
            elif kind == "quote":
                updates.update(stage="quoted", quote_at=now, last_contact=now, next_due=None, meeting_at=None, snooze_until=None)
            elif kind == "meeting":
                updates.update(stage="meeting", meeting_at=now, last_contact=now, quote_at=None, next_due=None, snooze_until=None)
            elif kind == "next_step":
                updates.update(next_due=body["due"], meeting_at=None, quote_at=None, snooze_until=None)
            elif kind in ("won", "lost", "optout"):
                updates.update(stage=kind, next_due=None, quote_at=None, meeting_at=None)
                if kind == "optout":
                    updates["opted_out"] = 1
            elif kind == "snooze":
                updates["snooze_until"] = body["due"]
            elif kind == "resume":
                updates.update(stage="contacted", snooze_until=None, quote_at=None, meeting_at=None, next_due=None)
            elif kind == "assign":
                updates["owner"] = body["owner"]
            self.db.execute("UPDATE leads SET " + ",".join(k + "=?" for k in updates) + " WHERE id=?", (*updates.values(), lid))
            if kind not in ("note", "assign"):
                self._cancel(lid)
            else:
                self.db.execute("UPDATE tasks SET status='open',approved_version=NULL WHERE lead_id=? AND status='approved'", (lid,))
            self._log(lid, kind, body, key)
            return self.lead(lid)

    def context(self, lid):
        lead = self.lead(lid)
        events = [dict(x) for x in self.db.execute("SELECT kind,body,created FROM events WHERE lead_id=? ORDER BY rowid DESC LIMIT 12", (lid,))]
        for e in events:
            e["body"] = json.loads(e["body"])
        return {"lead": lead, "events": list(reversed(events))}

    def draft(self, lid):
        context = self.context(lid)
        lead = context["lead"]
        recent = [e["body"].get("text") for e in context["events"] if e["body"].get("text")]
        facts = "；".join(recent[-3:]) or lead["notes"] or "尚无需求记录，请先确认客户需求。"
        summary = f"{lead['name']} / {lead['company'] or '公司待确认'}；负责人：{lead['owner']}；阶段：{lead['stage']}。已记录：{facts}"
        if lead["stage"] == "quoted":
            message = f"你好 {lead['name']}，想跟进一下之前发给你的报价。你对方案范围、商务条件或下一步安排有什么需要确认的？我可以按你的重点补充说明。"
        elif lead["stage"] == "meeting":
            message = f"你好 {lead['name']}，谢谢之前的交流。我想确认我们接下来优先推进的事项，以及适合的时间安排。你这边有需要补充的内容吗？"
        else:
            message = f"你好 {lead['name']}，感谢联系。方便说一下你当前最想解决的问题，以及期望的时间安排吗？我会根据你的需求整理合适的下一步。"
        return {"summary": summary, "draft": message, "mode": "template", "version": lead["version"]}

    def tick(self):
        with self.tx():
            now, created = self.clock(), 0
            for row in self.db.execute("SELECT * FROM leads WHERE stage NOT IN ('won','lost','optout') AND opted_out=0").fetchall():
                l = dict(row)
                if l["snooze_until"] and l["snooze_until"] > now:
                    continue
                rules = []
                if l["stage"] == "new" and l["last_contact"] is None:
                    rules.append(("first_response", l["created"] + self.first, str(l["created"])))
                if l["stage"] == "quoted" and l["quote_at"]:
                    # Catch up with only the latest tier, never a burst of 3 reminders.
                    passed = [d for d in self.quote_days if l["quote_at"] + d * 86400 <= now]
                    if passed:
                        d = max(passed)
                        rules.append((f"quote_{d:g}d", l["quote_at"] + d * 86400, str(l["quote_at"])))
                if l["meeting_at"] and not l["next_due"]:
                    rules.append(("meeting_next_step", l["meeting_at"] + self.meeting, str(l["meeting_at"])))
                if l["next_due"]:
                    rules.append(("next_step_overdue", l["next_due"], str(l["next_due"])))
                for rule, due, anchor in rules:
                    if due > now:
                        continue
                    anchor += ":" + str(l["snooze_until"] or "")
                    if self.db.execute("SELECT 1 FROM tasks WHERE lead_id=? AND rule=? AND anchor=?", (l["id"], rule, anchor)).fetchone():
                        continue
                    self._cancel(l["id"])
                    draft = self.draft(l["id"])
                    tid = uuid.uuid4().hex
                    self.db.execute("INSERT INTO tasks(id,lead_id,rule,anchor,due,summary,draft,created) VALUES(?,?,?,?,?,?,?,?)",
                                    (tid, l["id"], rule, anchor, due, draft["summary"], draft["draft"], now))
                    self.db.execute("INSERT INTO outbox(id,task_id,next_attempt) VALUES(?,?,?)", (uuid.uuid4().hex, tid, now))
                    created += 1
            self.db.execute("INSERT OR REPLACE INTO meta VALUES('last_tick',?)", (str(now),))
            return {"created": created, "at": now}

    def task(self, tid):
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if not row:
            raise KeyError("Task not found")
        return dict(row)

    def task_action(self, tid, data):
        action = data.get("action")
        if not isinstance(action, str) or action not in ("approve", "complete", "retry_notification"):
            raise Invalid("Unknown task action")
        with self.tx():
            task = self.task(tid)
            lead = self.lead(task["lead_id"])
            if action == "retry_notification":
                if task["status"] not in ("open", "approved"):
                    raise Invalid("Task is no longer actionable")
                self.db.execute("UPDATE outbox SET status='pending',attempts=0,next_attempt=?,last_error='' WHERE task_id=? AND status='failed'", (self.clock(), tid))
            elif action == "approve":
                if task["status"] not in ("open", "approved") or lead["opted_out"] or lead["stage"] in ("won", "lost"):
                    raise Invalid("Task is no longer actionable")
                draft = text(data.get("draft", task["draft"]), "draft", 10000, True)
                self.db.execute("UPDATE tasks SET draft=?,status='approved',approved_version=? WHERE id=?", (draft, lead["version"], tid))
            else:
                if task["status"] not in ("open", "approved", "delivery_unknown"):
                    raise Invalid("Task cannot be completed in its current state")
                self.db.execute("UPDATE tasks SET status='done',approved_version=NULL WHERE id=?", (tid,))
                self.db.execute("UPDATE outbox SET status='cancelled' WHERE task_id=? AND status='pending'", (tid,))
            self._log(lead["id"], "task_" + action, {"task_id": tid})
            return self.task(tid)

    def state(self):
        with self.lock:
            return {"leads": [dict(r) for r in self.db.execute("SELECT * FROM leads ORDER BY created DESC")],
                    "tasks": [dict(r) for r in self.db.execute("SELECT * FROM tasks ORDER BY created DESC LIMIT 500")],
                    "notifications": [dict(r) for r in self.db.execute("SELECT * FROM outbox ORDER BY rowid DESC LIMIT 500")],
                    "last_tick": (self.db.execute("SELECT value FROM meta WHERE key='last_tick'").fetchone() or [None])[0]}
