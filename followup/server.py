"""Small-team HTTP application. Deploy behind a TLS reverse proxy."""
import argparse
import hmac
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .core import Engine, Invalid
from .integrations import ai_draft, deliver_notifications, send_approved

LOG = logging.getLogger("followup")
STATIC = Path(__file__).parent / "static"


def make_handler(engine, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            LOG.info("request method=%s status=%s", self.command, args[1] if len(args) > 1 else "-")

        def output(self, status, data, content_type="application/json; charset=utf-8"):
            body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            supplied = self.headers.get("Authorization", "")
            return hmac.compare_digest(supplied.encode(), ("Bearer " + token).encode())

        def body(self):
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise Invalid("Invalid content length")
            if size < 0 or size > 65536:
                raise Invalid("JSON body limit is 64 KiB")
            if self.headers.get_content_type() != "application/json":
                raise Invalid("Use Content-Type: application/json")
            try:
                data = json.loads(self.rfile.read(size) or b"{}")
            except (ValueError, UnicodeDecodeError):
                raise Invalid("Invalid JSON")
            if not isinstance(data, dict):
                raise Invalid("JSON body must be an object")
            return data

        def route(self):
            path = urlsplit(self.path).path
            if self.command == "GET" and path == "/healthz":
                with engine.lock:
                    row = engine.db.execute("SELECT value FROM meta WHERE key='last_tick'").fetchone()
                fresh = bool(row and time.time() - float(row[0]) < 180)
                return self.output(200 if fresh else 503, {"ok": fresh, "scheduler": "fresh" if fresh else "stale_or_starting"})
            files = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css")}
            if self.command == "GET" and path in files:
                name, mime = files[path]
                return self.output(200, (STATIC / name).read_bytes(), mime + "; charset=utf-8")
            if not self.authorized():
                return self.output(401, {"error": "Bearer token required"})
            if self.command == "GET" and path == "/api/state":
                return self.output(200, engine.state())
            parts = path.strip("/").split("/")
            if self.command == "GET" and len(parts) == 3 and parts[:2] == ["api", "leads"]:
                with engine.lock:
                    return self.output(200, engine.context(parts[2]))
            if self.command != "POST":
                return self.output(404, {"error": "Not found"})
            data = self.body()
            if path == "/api/leads":
                return self.output(201, engine.create(data))
            if path == "/api/tick":
                result = engine.tick()
                result["notifications"] = deliver_notifications(engine)
                return self.output(200, result)
            if len(parts) == 4 and parts[:2] == ["api", "leads"]:
                if parts[3] == "events":
                    return self.output(200, engine.event(parts[2], data))
                if parts[3] == "draft":
                    return self.output(200, ai_draft(engine, parts[2]))
            if len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] == "actions":
                result = send_approved(engine, parts[2]) if data.get("action") == "send" else engine.task_action(parts[2], data)
                return self.output(200, result)
            return self.output(404, {"error": "Not found"})

        def handle_request(self):
            self.connection.settimeout(30)
            try:
                self.route()
            except Invalid as exc:
                self.output(400, {"error": str(exc)})
            except KeyError:
                self.output(404, {"error": "Record not found"})
            except (BrokenPipeError, TimeoutError):
                pass
            except Exception:
                LOG.exception("Request failed")
                self.output(500, {"error": "Internal error"})

        do_GET = handle_request
        do_POST = handle_request

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default=os.environ.get("FOLLOWUP_DB", "data/followup.db"))
    args = parser.parse_args()
    token = os.environ.get("FOLLOWUP_API_TOKEN", "")
    if len(token) < 24 or token.startswith("replace-with-"):
        parser.error("Set FOLLOWUP_API_TOKEN to a random value of at least 24 characters")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    owners = [s.strip() for s in os.environ.get("SALES_OWNERS", "sales").split(",") if s.strip()]
    engine = Engine(args.db, owners=owners, first_response_minutes=float(os.environ.get("FIRST_RESPONSE_MINUTES", "10")),
                    quote_days=tuple(float(s) for s in os.environ.get("QUOTE_FOLLOWUP_DAYS", "3,7,14").split(",")),
                    meeting_hours=float(os.environ.get("MEETING_NEXT_STEP_HOURS", "24")))
    stop = threading.Event()

    def worker():
        while not stop.is_set():
            try:
                engine.tick()
                deliver_notifications(engine)
            except Exception:
                LOG.exception("Scheduler tick failed")
            stop.wait(30)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(engine, token))
    server.daemon_threads = True
    LOG.info("Sales Follow-up OS listening on %s:%s", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        thread.join(timeout=20)


if __name__ == "__main__":
    main()
