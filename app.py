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

def api_projects():
    return {"projects": store.projects()}


def api_project(q):
    return store.project(_int(q.get("id", [None])[0]))


def post_project(b):
    return store.create_project(b.get("name") or "")


def post_rename(b):
    return store.rename_project(_int(b.get("id")), b.get("name") or "")


def post_delete_project(b):
    return store.delete_project(_int(b.get("id")))


def post_update(b):
    return store.add_update(_int(b.get("project_id"), "project"), b.get("body") or "")


def post_delete_update(b):
    return store.delete_update(_int(b.get("id")))


def post_delete_answer(b):
    return store.delete_answer(_int(b.get("id")))


def post_summarize(b):
    """Rebuild the brief from every update on the project.

    The Claude call happens with no transaction open — it takes seconds, and
    holding a write lock across it would block every other tab.
    """
    pid = _int(b.get("project_id"), "project")
    p = store.project(pid)
    if not p["updates"]:
        raise ValueError("nothing to summarise yet — add an update first")
    with _AI_LOCK:
        body = ai.summarize(p["name"], list(reversed(p["updates"])))
    return store.save_summary(pid, body, p["updates"][0]["id"])


def post_ask(b):
    pid = _int(b.get("project_id"), "project")
    question = (b.get("question") or "").strip()
    if not question:
        raise ValueError("ask something")
    p = store.project(pid)
    if not p["updates"]:
        raise ValueError("no updates on this project yet")
    with _AI_LOCK:
        answer = ai.ask(p["name"], list(reversed(p["updates"])), question)
    return store.save_answer(pid, question, answer)


READS = {"/api/projects": api_projects, "/api/project": api_project}
WRITES = {
    "/api/project/new": post_project,
    "/api/project/rename": post_rename,
    "/api/project/delete": post_delete_project,
    "/api/update": post_update,
    "/api/update/delete": post_delete_update,
    "/api/answer/delete": post_delete_answer,
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
            fn = READS[path]
            q = parse_qs(urlparse(self.path).query)
            return self._json(200, fn(q) if fn is api_project else fn())
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
