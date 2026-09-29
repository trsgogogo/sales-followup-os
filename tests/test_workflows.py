import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from followup.core import Engine, Invalid
from followup.integrations import ai_draft, deliver_notifications, send_approved
from followup.server import make_handler


class Workflows(unittest.TestCase):
    def setUp(self):
        self.now = 1700000000.0
        self.e = Engine(":memory:", owners=["A", "B"], clock=lambda: self.now)
        self.l = self.e.create({"name": "Example Buyer", "email": "buyer@example.com", "external_id": "form:1"})

    def tearDown(self):
        self.e.db.close()

    def advance(self, seconds):
        self.now += seconds
        return self.e.tick()

    def active(self):
        return [t for t in self.e.state()["tasks"] if t["status"] in ("open", "approved")]

    def event(self, kind, **kw):
        return self.e.event(self.l["id"], {"kind": kind, **kw})

    def test_round_robin_and_idempotent_intake(self):
        self.assertEqual("A", self.l["owner"])
        self.assertEqual(self.l["id"], self.e.create({"name": "Duplicate", "external_id": "form:1"})["id"])
        self.assertEqual("B", self.e.create({"name": "Next buyer"})["owner"])

    def test_ten_minute_boundary_and_scan_deduplication(self):
        self.assertEqual(0, self.advance(599)["created"])
        self.assertEqual(1, self.advance(1)["created"])
        self.assertEqual(0, self.advance(60)["created"])
        self.assertEqual("first_response", self.active()[0]["rule"])

    def test_contacted_lead_stops_first_response(self):
        self.event("contacted")
        self.assertEqual(0, self.advance(3600)["created"])

    def test_reply_cancels_quote_and_pending_notification(self):
        self.event("quote")
        self.advance(3 * 86400)
        self.assertEqual(1, len(self.active()))
        self.event("reply", text="Please clarify the service scope")
        self.advance(20 * 86400)
        self.assertEqual([], self.active())
        self.assertEqual("cancelled", self.e.state()["notifications"][0]["status"])

    def test_quote_catchup_uses_latest_tier_only(self):
        self.event("quote")
        self.advance(15 * 86400)
        self.assertEqual(1, len(self.active()))
        self.assertEqual("quote_14d", self.active()[0]["rule"])

    def test_equivalent_timing_config_does_not_duplicate_tasks(self):
        self.event("quote")
        self.advance(3 * 86400)
        self.e.quote_days = (3.0, 7.0, 14.0)
        self.assertEqual(0, self.e.tick()["created"])
        self.assertEqual(1, len(self.e.state()["tasks"]))

    def test_quote_three_seven_fourteen_day_sequence(self):
        self.event("quote")
        self.advance(3 * 86400)
        self.assertEqual("quote_3d", self.active()[0]["rule"])
        self.advance(4 * 86400)
        self.assertEqual(["quote_7d"], [x["rule"] for x in self.active()])
        self.advance(7 * 86400)
        self.assertEqual(["quote_14d"], [x["rule"] for x in self.active()])

    def test_new_quote_resets_cadence(self):
        self.event("quote")
        self.advance(4 * 86400)
        self.event("quote", text="Revised scope")
        self.assertEqual([], self.active())
        self.advance(3 * 86400)
        self.assertEqual(1, len(self.active()))

    def test_snooze_realerts_after_deadline(self):
        self.advance(600)
        self.event("snooze", due=self.now + 3600)
        self.assertEqual(0, self.advance(3599)["created"])
        self.assertEqual(1, self.advance(1)["created"])

    def test_meeting_and_scheduled_next_step(self):
        self.event("meeting", text="Needs a proposal")
        self.advance(24 * 3600)
        self.assertEqual("meeting_next_step", self.active()[0]["rule"])
        self.event("next_step", due=self.now + 3600, text="Send proposal")
        self.assertEqual([], self.active())
        self.advance(3600)
        self.assertEqual("next_step_overdue", self.active()[0]["rule"])

    def test_close_and_optout_stop_all_tasks(self):
        for kind in ("won", "lost", "optout"):
            lead = self.e.create({"name": kind})
            self.e.event(lead["id"], {"kind": kind})
        self.event("optout")
        self.advance(30 * 86400)
        self.assertEqual([], self.active())
        with self.assertRaises(Invalid):
            self.event("resume")

    def test_event_replay_and_conflicting_idempotency(self):
        self.event("quote", idempotency_key="quote:1")
        self.now += 1000
        self.event("quote", idempotency_key="quote:1")
        self.assertEqual(self.now - 1000, self.e.lead(self.l["id"])["quote_at"])
        with self.assertRaises(Invalid):
            self.event("reply", idempotency_key="quote:1")

    def test_notification_retries_then_requires_manual_retry(self):
        self.advance(600)
        def fail(*args, **kwargs):
            raise RuntimeError("secret URL must never appear in the database")
        config = {"NOTIFY_WEBHOOK_URL": "https://example.com/webhook"}
        for _ in range(5):
            deliver_notifications(self.e, config, fail)
            self.now += 3600
        row = self.e.state()["notifications"][0]
        self.assertEqual("failed", row["status"])
        self.assertEqual("RuntimeError", row["last_error"])
        self.e.task_action(row["task_id"], {"action": "retry_notification"})
        deliver_notifications(self.e, config, lambda *a, **k: {})
        self.assertEqual("sent", self.e.state()["notifications"][0]["status"])

    def test_notification_provider_error_and_reply_stop(self):
        self.advance(600)
        env = {"NOTIFY_WEBHOOK_URL": "https://example.com", "NOTIFY_FORMAT": "wecom"}
        deliver_notifications(self.e, env, lambda *a, **k: {"errcode": 93000})
        self.assertEqual("pending", self.e.state()["notifications"][0]["status"])
        self.event("reply")
        self.now += 3600
        calls = []
        deliver_notifications(self.e, env, lambda *a, **k: calls.append(a))
        self.assertEqual([], calls)

    def test_ai_optional_and_malformed_output_fallback(self):
        self.assertEqual("template", ai_draft(self.e, self.l["id"], {})["mode"])
        env = {"LLM_URL": "https://example.com", "LLM_API_KEY": "test", "LLM_MODEL": "test"}
        response = ai_draft(self.e, self.l["id"], env, lambda *a, **k: {"choices": [{"message": {"content": "not json"}}]})
        self.assertEqual("template", response["mode"])
        self.assertIn("warning", response)

    def test_approval_invalidated_by_new_customer_context(self):
        self.advance(600)
        tid = self.active()[0]["id"]
        self.e.task_action(tid, {"action": "approve", "draft": "Reviewed message"})
        self.event("note", text="New requirement")
        self.assertEqual("open", self.e.task(tid)["status"])

    def test_smtp_is_opt_in_and_sends_approved_body_once(self):
        self.advance(600)
        tid = self.active()[0]["id"]
        with self.assertRaises(Invalid):
            send_approved(self.e, tid, {})
        self.e.task_action(tid, {"action": "approve", "draft": "Reviewed message"})
        sent = []
        class SMTP:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def starttls(self, **k): pass
            def login(self, *a): pass
            def send_message(self, msg): sent.append(msg); return {}
        env = {"ENABLE_EMAIL_SEND": "true", "SMTP_HOST": "test", "SMTP_USER": "test", "SMTP_PASSWORD": "test", "SMTP_FROM": "sender@example.com"}
        self.assertEqual("sent", send_approved(self.e, tid, env, SMTP)["status"])
        self.assertEqual("Reviewed message\n", sent[0].get_content())
        with self.assertRaises(Invalid):
            send_approved(self.e, tid, env, SMTP)
        self.assertEqual(1, len(sent))

    def test_smtp_failure_is_not_automatically_retried(self):
        self.advance(600)
        tid = self.active()[0]["id"]
        self.e.task_action(tid, {"action": "approve"})
        def broken(*a, **k): raise TimeoutError()
        env = {"ENABLE_EMAIL_SEND": "true", "SMTP_HOST": "test", "SMTP_USER": "test", "SMTP_PASSWORD": "test", "SMTP_FROM": "sender@example.com"}
        with self.assertRaises(Invalid): send_approved(self.e, tid, env, broken)
        self.assertEqual("delivery_unknown", self.e.task(tid)["status"])
        self.assertEqual(0, self.e.tick()["created"])

    def test_concurrent_scans_create_one_task(self):
        self.now += 600
        threads = [threading.Thread(target=self.e.tick) for _ in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(1, len(self.e.state()["tasks"]))

    def test_persistence_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            e = Engine(folder + "/db.sqlite", clock=lambda: self.now)
            e.create({"name": "Persistent"})
            self.now += 600
            e.tick()
            e.db.close()
            e = Engine(folder + "/db.sqlite", clock=lambda: self.now)
            self.assertEqual(0, e.tick()["created"])
            self.assertEqual(1, len(e.state()["tasks"]))
            e.db.close()

    def test_invalid_input_does_not_create_record(self):
        for data in ({"name": ""}, {"name": "A", "email": "bad\nemail@example.com"}):
            with self.assertRaises(Invalid): self.e.create(data)
        with self.assertRaises(Invalid): self.event("snooze", due=self.now - 1)
        self.assertEqual(1, len(self.e.state()["leads"]))


class HTTP(unittest.TestCase):
    def setUp(self):
        self.e = Engine(":memory:")
        self.token = "test-token-with-at-least-24-characters"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.e, self.token))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.e.db.close()

    def request(self, path, data=None, auth=True):
        req = urllib.request.Request(self.url + path, data=json.dumps(data).encode() if data is not None else None,
              headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + self.token} if auth else {})})
        return urllib.request.urlopen(req)

    def test_auth_and_api_lifecycle(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/state", auth=False)
        self.assertEqual(401, error.exception.code)
        with self.request("/api/leads", {"name": "API Buyer"}) as r:
            lead = json.load(r)
        with self.request("/api/leads/" + lead["id"] + "/events", {"kind": "quote"}) as r:
            self.assertEqual("quoted", json.load(r)["stage"])
        with self.request("/api/state") as r:
            self.assertEqual(1, len(json.load(r)["leads"]))

    def test_static_csp_and_non_object_json(self):
        with self.request("/", auth=False) as r:
            self.assertIn("frame-ancestors 'none'", r.headers["Content-Security-Policy"])
        with self.assertRaises(urllib.error.HTTPError) as err:
            self.request("/api/leads", [])
        self.assertEqual(400, err.exception.code)


if __name__ == "__main__":
    unittest.main()
