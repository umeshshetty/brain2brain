#!/usr/bin/env python3
"""Brain — a page per project, raw updates in, Claude Code on top.

    python3 app.py            # -> http://127.0.0.1:8765/

Stdlib only. Storage is store.py; every AI feature is a `claude -p` subprocess
in ai.py. Bound to loopback, with a per-launch token on the API so a random
page you have open in another tab cannot post into your store.
"""

import argparse
import json
import os
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import ai
import store

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "index.html")
TOKEN = secrets.token_urlsafe(24)
MAX_BODY = 1 << 20

# `claude -p` takes seconds and costs tokens. One at a time keeps a stray
# double-click from launching two identical summaries of the same project.
_AI_LOCK = threading.Lock()


def _int(v, what="id") -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ValueError(f"bad {what}")


# ----------------------------------------------------------------- handlers

def api_projects(q):
    # Both kinds in one read: the home view needs a count for the tab you are
    # not looking at, and filtering server-side would cost a second request to
    # get it back.
    return {"projects": store.projects()}


def api_project(q):
    return store.project(_int(q.get("id", [None])[0]))


def post_project(b):
    return store.create_project(b.get("name") or "", b.get("kind"))


def post_rename(b):
    return store.rename_project(_int(b.get("id")), b.get("name") or "")


def post_delete_project(b):
    return store.delete_project(_int(b.get("id")))


def post_topic(b):
    return store.create_topic(_int(b.get("project_id"), "project"), b.get("name") or "")


def post_rename_topic(b):
    return store.rename_topic(_int(b.get("id")), b.get("name") or "")


def post_delete_topic(b):
    return store.delete_topic(_int(b.get("id")))


def post_update(b):
    return store.add_update(_int(b.get("project_id"), "project"),
                            b.get("body") or "", b.get("topic_id"))


def post_move_update(b):
    return store.move_update(_int(b.get("id")), b.get("topic_id"))


def post_delete_update(b):
    return store.delete_update(_int(b.get("id")))


def post_delete_answer(b):
    return store.delete_answer(_int(b.get("id")))


def _scope(b):
    """Resolve a request to (project, topic_id, label, updates-oldest-first, kind).

    A topic_id narrows the AI to that topic's updates and gives the summary its
    own slot. Without one the scope is the whole project. Either way the model
    only ever sees updates that are in scope — a topic brief that quietly drew
    on the rest of the project would be worse than no topic at all.
    """
    pid = _int(b.get("project_id"), "project")
    p = store.project(pid)
    raw = b.get("topic_id")
    tid = None if raw in (None, "", 0, "0") else _int(raw, "topic")
    label = p["name"]
    updates = p["updates"]
    kind = p.get("kind", "project")
    if tid is not None:
        topic = next((t for t in p["topics"] if t["id"] == tid), None)
        if not topic:
            raise ValueError("no such topic")
        label = f"{p['name']} — {topic['name']}"
        updates = [u for u in updates if u["topic_id"] == tid]
    return p, tid, label, list(reversed(updates)), kind


def post_summarize(b):
    """Rebuild the brief for one scope.

    The Claude call happens with no transaction open — it takes seconds, and
    holding a write lock across it would block every other tab.
    """
    p, tid, label, updates, kind = _scope(b)
    if not updates:
        raise ValueError("nothing to summarise here yet — add an update first")
    with _AI_LOCK:
        body = ai.summarize(label, updates, kind)
    return store.save_summary(p["id"], body, updates[-1]["id"], tid)


def post_ask(b):
    question = (b.get("question") or "").strip()
    if not question:
        raise ValueError("ask something")
    p, tid, label, updates, kind = _scope(b)
    if not updates:
        raise ValueError("no updates in scope yet")
    with _AI_LOCK:
        answer = ai.ask(label, updates, question, kind)
    return store.save_answer(p["id"], question, answer, tid)


def api_agenda(q):
    return store.agenda()


def post_agenda(b):
    """Rebuild the cross-project agenda: one Claude call over every project.

    It is built for one specific day and stamped with it, because half of what
    the pane says is "today" and that stops being true at midnight.
    """
    ctx = store.agenda_context()
    if not ctx["total"]:
        raise ValueError("nothing to read yet — add an update to a project first")
    day = store.today()
    with _AI_LOCK:
        items = ai.agenda(day, ctx["projects"])
    return store.save_agenda(items, ctx["newest"], day, ctx["total"])


READS = {"/api/projects": api_projects,
         "/api/project": api_project,
         "/api/agenda": api_agenda}
WRITES = {
    "/api/project/new": post_project,
    "/api/project/rename": post_rename,
    "/api/project/delete": post_delete_project,
    "/api/topic/new": post_topic,
    "/api/topic/rename": post_rename_topic,
    "/api/topic/delete": post_delete_topic,
    "/api/update": post_update,
    "/api/update/move": post_move_update,
    "/api/update/delete": post_delete_update,
    "/api/answer/delete": post_delete_answer,
    "/api/agenda/refresh": post_agenda,
    "/api/summarize": post_summarize,
    "/api/ask": post_ask,
}


class Handler(BaseHTTPRequestHandler):
    server_version = "brain"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        sys.stderr.write("  %s\n" % (fmt % a))

    # -- guards ------------------------------------------------------------
    def _host_ok(self) -> bool:
        """Blocks DNS rebinding: a hostile name resolving to 127.0.0.1 still
        sends its own Host header, and this refuses it."""
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost", "[::1]", "::1")

    def _auth_ok(self) -> bool:
        """A custom header cannot be set cross-origin without a preflight, and
        no preflight is answered — so only our own page can call the API."""
        return secrets.compare_digest(self.headers.get("X-Brain-Token") or "", TOKEN)

    def _send(self, code, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; "
                         "script-src 'unsafe-inline'; connect-src 'self'; "
                         "base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj).encode(), "application/json")

    # -- routes ------------------------------------------------------------
    def do_GET(self):
        if not self._host_ok():
            return self._json(421, {"error": "bad host"})
        path = urlparse(self.path).path
        if path == "/":
            with open(PAGE, encoding="utf-8") as fh:
                html = fh.read().replace("__TOKEN__", TOKEN)
            return self._send(200, html.encode(), "text/html; charset=utf-8")
        if path not in READS:
            return self._json(404, {"error": "not found"})
        if not self._auth_ok():
            return self._json(403, {"error": "bad token"})
        try:
            q = parse_qs(urlparse(self.path).query)
            return self._json(200, READS[path](q))
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        except Exception as e:
            return self._json(500, {"error": repr(e)})

    def do_POST(self):
        if not self._host_ok():
            return self._json(421, {"error": "bad host"})
        path = urlparse(self.path).path
        if path not in WRITES:
            return self._json(404, {"error": "not found"})
        if not self._auth_ok():
            return self._json(403, {"error": "bad token"})
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_BODY:
            return self._json(413, {"error": "bad body length"})
        try:
            return self._json(200, WRITES[path](json.loads(self.rfile.read(n))))
        except ai.AIError as e:
            # Claude failing is normal operation, not a server fault: the CLI
            # may be missing, logged out, rate limited, or just slow.
            return self._json(503, {"error": str(e)})
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        except Exception as e:
            return self._json(500, {"error": repr(e)})


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    store.init()
    url = f"http://127.0.0.1:{args.port}/"
    print(f"  brain → {url}\n  store: {store.DB_PATH}\n  ctrl-c to stop\n",
          file=sys.stderr)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    if not args.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  bye", file=sys.stderr)


if __name__ == "__main__":
    main()
