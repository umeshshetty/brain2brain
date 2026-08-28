#!/usr/bin/env python3
"""Brain — a page per project, raw updates in, Claude Code on top.

    python3 app.py            # -> http://127.0.0.1:8765/

Stdlib only. Storage is store.py; every AI feature is a `claude -p` subprocess
in ai.py. Bound to loopback, with a per-launch token on the API so a random
page you have open in another tab cannot post into your store.
"""

import argparse
import contextlib
import json
import os
import re
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
# The Now pane rebuilds itself once when the calendar has moved past it, since
# half of what it says is "today". That is one unasked Claude call a day, and
# an unasked call is the kind of thing a person should be able to refuse.
CATCHUP = "0" if os.environ.get("BRAIN_NO_CATCHUP") else "1"

# `claude -p` takes seconds and costs tokens. One at a time keeps a stray
# double-click from launching two identical summaries of the same project.
_AI_LOCK = threading.Lock()
_AI_NOW = None        # what is holding it, in words
_AI_WAITING = 0       # how many are queued behind it


@contextlib.contextmanager
def _ai(what: str):
    """One `claude -p` at a time, and a name for whoever ends up behind it.

    The lock is right; what it could not do was explain itself. A call that
    arrives while it is held simply takes twice as long, and a spinner meaning
    "Claude is thinking" is indistinguishable from a spinner meaning "queued
    behind the pane another tab is rebuilding" — only one of which is anything
    you did. So the holder writes down what it is and how many are waiting, and
    /api/busy answers. Cheap: two module variables and a read.

    The names here are shown to the reader, and the page compares its own
    against them so it never reports itself as the thing it is waiting for —
    keep the two in step (see AI_LABEL in index.html).
    """
    global _AI_NOW, _AI_WAITING
    if not _AI_LOCK.acquire(blocking=False):
        _AI_WAITING += 1
        try:
            _AI_LOCK.acquire()
        finally:
            _AI_WAITING -= 1
    _AI_NOW = what
    try:
        yield
    finally:
        _AI_NOW = None
        _AI_LOCK.release()


def api_busy(q):
    """What Claude is doing right now, for a button that would otherwise just
    spin. A read with no store behind it at all."""
    return {"busy": _AI_NOW, "queued": _AI_WAITING}


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
    pid = _int(q.get("id", [None])[0])
    out = store.project(pid)
    out["pane"] = store.page_agenda(pid)
    out["prep"] = store.prep(pid)
    return out


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
    links = b.get("links") or []
    if not isinstance(links, list) or len(links) > 20:
        raise ValueError("bad links")
    # `on` is the day it happened, when that is not today. The store checks
    # the shape and refuses a future one — see store.stamp.
    return store.add_update(_int(b.get("project_id"), "project"),
                            b.get("body") or "", b.get("topic_id"),
                            [_int(x, "link") for x in links],
                            on=(b.get("on") or "").strip() or None)


def post_link(b):
    """Link or unlink one update to one other project or person."""
    uid = _int(b.get("id"), "update")
    pid = _int(b.get("project_id"), "project")
    return (store.link_update(uid, pid) if b.get("on")
            else store.unlink_update(uid, pid))


def post_move_update(b):
    return store.move_update(_int(b.get("id")), b.get("topic_id"))


def post_rehome_update(b):
    """Move an update to another page — a filing fix, not an edit."""
    return store.rehome_update(_int(b.get("id"), "update"),
                               _int(b.get("project_id"), "project"))


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
        # A guest has no topic here, so a topic brief never sees one. Cross-
        # pollination happens at the project's own scope, where the whole
        # picture belongs.
        updates = [u for u in updates if u["topic_id"] == tid and not u["via"]]
    return p, tid, label, list(reversed(updates)), kind


def post_summarize(b):
    """Rebuild the brief for one scope.

    The Claude call happens with no transaction open — it takes seconds, and
    holding a write lock across it would block every other tab.
    """
    p, tid, label, updates, kind = _scope(b)
    if not updates:
        raise ValueError("nothing to summarise here yet — add an update first")
    with _ai(f"a brief on {label}"):
        body = ai.summarize(label, updates, kind, p.get("about"),
                            p.get("guidance"))
    return store.save_summary(p["id"], body, max(u["id"] for u in updates),
                              tid, len(updates))


def post_ask(b):
    question = (b.get("question") or "").strip()
    if not question:
        raise ValueError("ask something")
    p, tid, label, updates, kind = _scope(b)
    if not updates:
        raise ValueError("no updates in scope yet")
    with _ai(f"a question about {label}"):
        answer = ai.ask(label, updates, question, kind, p.get("about"))
    return store.save_answer(p["id"], question, answer, tid)


def api_search(q):
    """Substring search across every page. A read like any other — no model,
    nothing cached, nothing written — so it lives in READS with no ceremony."""
    return store.search((q.get("q", [""])[0] or "")[:200])


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
    with _ai("the Now pane"):
        items = ai.agenda(day, ctx["projects"])
    return store.save_agenda(items, ctx["newest"], day, ctx["total"])


# What an action writes into the log. Composed rather than free-typed so a
# month later the log still says which item it was about — a bare "sent it"
# under a project tells you nothing. The item's own words are quoted back
# because they came from your updates in the first place.
def _logged(action: str, item: dict, note: str, date: str) -> str:
    head = {"done": f"Done — {item['text']}",
            "date": f"Due date moved to {date} — {item['text']}",
            "note": f"Re: {item['text']}"}[action]
    return head + ("\n" + note if note else "")


def _act_request(pane, b):
    """The half of acting that both panes do identically.

    Returns the pane's items, the one being acted on, and the checked action.
    Kept in one place because the two act routes differ in exactly one thing —
    whether the target page has to be picked — and everything else drifting
    apart would mean the cross-project pane and a page's own pane could come to
    disagree about what a valid action is.

    Items are addressed by position, so the client sends back the stamp of the
    pane it rendered. A rebuild in another tab reshuffles the array, and acting
    on index 2 of a list you cannot see is how you file a note against the
    wrong project.
    """
    if not pane.get("built"):
        raise ValueError("nothing to act on yet")
    if b.get("built") != pane["created_at"]:
        raise ValueError("this pane was rebuilt — reload and try again")

    items = pane["items"]
    i = _int(b.get("index"), "item")
    if not 0 <= i < len(items):
        raise ValueError("no such item")

    action = (b.get("action") or "").strip()
    if action not in ("done", "note", "date"):
        raise ValueError("bad action")
    note = (b.get("note") or "").strip()[:4000]
    date = (b.get("date") or "").strip()
    if action == "date" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ValueError("a date looks like 2026-09-04")
    if action == "note" and not note:
        raise ValueError("write something")
    return items, items[i], action, note, date


def _act_mark(item, action, date):
    """Mark the cached item so the pane does not lie between acting and
    rebuilding. Derived state on derived state: it evaporates the next time
    the pane reads the raw log, which is where the truth actually is."""
    if action == "done":
        item["done"] = True
    elif action == "date":
        item["date"] = date
    item["logged"] = int(item.get("logged") or 0) + 1


def post_agenda_act(b):
    """Act on one item in the pane: log a note, mark it done, move its date.

    Every one of these writes an update into the project or person it belongs
    to, and that update is the only record. Nothing here sets a status: the
    item stops appearing because the next agenda reads a log that says it is
    done, not because a flag was flipped. That keeps the raw text the single
    source of truth and means Ask and the briefs see what you did for free.
    """
    items, item, action, note, date = _act_request(store.agenda(), b)

    # The one thing this pane does that a page's own pane does not: an item
    # Claude could not tie to anything has nowhere to be logged, so the page
    # offers a picker. Either way the target is checked before we write.
    pid = b.get("project_id") or item.get("project_id")
    if not pid:
        raise ValueError("pick which project or person this belongs to")
    pid = _int(pid, "project")
    target = store.project(pid)

    up = store.add_update(pid, _logged(action, item, note, date))
    _act_mark(item, action, date)
    out = store.agenda_items(items)
    out["logged_to"] = {"id": pid, "name": target["name"], "update_id": up["id"]}
    return out


def post_page_agenda(b):
    """Rebuild one page's pane: one Claude call over that page's updates.

    Separate from the cross-project pane rather than a filter over it. That one
    caps each project at 40 updates and then keeps the 12 most urgent items
    across the whole store, so a page's own deadlines can be crowded out by a
    noisier page entirely — which is exactly what you notice when you open a
    page and it tells you nothing.
    """
    ctx = store.page_agenda_context(_int(b.get("project_id"), "project"))
    if not ctx["updates"]:
        raise ValueError("nothing to read here yet — add an update first")
    day = store.today()
    with _ai(f"{ctx['name']}'s dates"):
        items = ai.page_agenda(day, ctx["name"], ctx["kind"], ctx["updates"],
                               ctx["about"])
    return store.save_page_agenda(ctx["id"], items, ctx["newest"], day, ctx["total"])


def post_page_act(b):
    """Act on one item in a page's pane. Identical in spirit to the cross-
    project one: done, a note and a new date all write an update, and that
    update is the whole record.

    The target needs no picking here. Every item in this pane came out of this
    page's updates, so this page is where what you did belongs.
    """
    pid = _int(b.get("project_id"), "project")
    items, item, action, note, date = _act_request(store.page_agenda(pid), b)

    up = store.add_update(pid, _logged(action, item, note, date))
    _act_mark(item, action, date)
    out = store.page_agenda_items(pid, items)
    out["logged_to"] = {"id": pid, "update_id": up["id"]}
    return out


def post_about(b):
    """Save what you wrote about this page. The only write that drops caches.

    Either field may be absent, which means leave it alone — the store works
    out what actually changed and drops only what that reaches.
    """
    pid = _int(b.get("project_id"), "project")
    vals = {}
    for key, cap in (("about", store.ABOUT_MAX), ("guidance", store.GUIDANCE_MAX)):
        if key not in b:
            continue
        v = b[key]
        if not isinstance(v, str):
            raise ValueError(f"{key} must be text")
        if len(v) > cap:
            raise ValueError(f"keep that under {cap} characters")
        vals[key] = v
    if not vals:
        raise ValueError("nothing to save")
    return store.set_page_setup(pid, **vals)


def post_about_draft(b):
    """Propose a profile from the notes. Returns it; saves nothing.

    The reader edits and presses Save, or does not. `about` steers every brief
    for this page, so a model may write toward it but never into it.
    """
    ctx = store.page_agenda_context(_int(b.get("project_id"), "project"))
    if not ctx["updates"]:
        raise ValueError("nothing to read here yet — add an update first")
    with _ai(f"a profile for {ctx['name']}"):
        return {"about": ai.draft_about(ctx["name"], ctx["kind"], ctx["updates"])}


def post_prep(b):
    """Build the brief for the next meeting about this page.

    `since` is the day of the last meeting. It defaults to the day of the newest
    update the page itself holds — a 1-1 note is the record of the 1-1 — and you
    can override it, because sometimes you spoke and did not write it down.
    """
    pid = _int(b.get("project_id"), "project")
    since = (b.get("since") or "").strip() or store.last_meeting(pid)
    if since and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
        raise ValueError("a date looks like 2026-09-04")
    ctx = store.prep_context(pid, since)
    if not ctx["total"]:
        raise ValueError("nothing to read yet — add an update first")
    with _ai(f"the {ctx['name']} meeting brief"):
        body = ai.prep(ctx["name"], ctx["kind"], ctx["updates"], since,
                       ctx["about"], ctx["guidance"])
    out = store.save_prep(pid, body, since, ctx["newest"], ctx["total"])
    out["counts"] = ctx["counts"]
    return out


READS = {"/api/projects": api_projects,
         "/api/project": api_project,
         "/api/agenda": api_agenda,
         "/api/search": api_search,
         "/api/busy": api_busy}
WRITES = {
    "/api/project/new": post_project,
    "/api/project/rename": post_rename,
    "/api/project/delete": post_delete_project,
    "/api/topic/new": post_topic,
    "/api/topic/rename": post_rename_topic,
    "/api/topic/delete": post_delete_topic,
    "/api/update": post_update,
    "/api/update/move": post_move_update,
    "/api/update/rehome": post_rehome_update,
    "/api/update/link": post_link,
    "/api/update/delete": post_delete_update,
    "/api/answer/delete": post_delete_answer,
    "/api/agenda/refresh": post_agenda,
    "/api/agenda/act": post_agenda_act,
    "/api/page/refresh": post_page_agenda,
    "/api/page/act": post_page_act,
    "/api/about": post_about,
    "/api/about/draft": post_about_draft,
    "/api/prep": post_prep,
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
                html = (fh.read().replace("__TOKEN__", TOKEN)
                                 .replace("__CATCHUP__", CATCHUP))
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
