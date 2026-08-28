"""SQLite storage. Nine tables, and three columns hold anything you typed.

`updates.body` is the raw text, stored exactly as entered and never rewritten.
`projects.about` says who a page is to you and every brief for it is written
against that; `projects.guidance` says what you want out of such a brief. No
model ever writes any of the three.
Everything else — summaries, answers, panes, preps — is derived, cached, and
safe to throw away: `DELETE FROM summaries` costs one Claude call to rebuild.

A project has topics; an update may sit in one of them or in none. Both halves
of that matter. Filing is optional so capture stays as fast as it was — being
made to choose a topic before you can type is exactly the friction this app
exists to avoid — and one topic rather than many keeps "what does this topic
say" a question with one answer.
"""

import datetime
import json
import os
import pathlib
import re
import sqlite3

DB_PATH = pathlib.Path(os.environ.get("BRAIN_DB") or
                       pathlib.Path(__file__).parent / "brain.db")

# projects and topics are created before anything else runs: with
# foreign_keys=ON, adding a column that REFERENCES topics — or renaming a table
# while such a column exists — fails outright if the target table is missing.
PRELUDE = """
-- A person is the same thing a project is: a bucket you drop dated raw text
-- into and ask Claude about. Notes from a 1-1 want topics, a summary, staleness
-- and Ask exactly as a project's updates do, so people live here under a `kind`
-- rather than in a parallel set of four tables that would need all of it again.
-- The table name is then half a lie, and that is the price.
-- `about` is who this page is to you, in your words: your manager, your
-- report, the vendor you renew in March. It is the second thing in the store
-- that cannot be regenerated, and the only one besides updates.body — every
-- brief for this page is written against it, so a model must never write it.
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL DEFAULT 'project',
    about       TEXT,
    guidance    TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS topics (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE(project_id, name)
);
"""

SCHEMA = """
-- topic_id is NULL-able and ON DELETE SET NULL, not CASCADE. Deleting a topic
-- is a filing decision; it must never take raw updates with it. They fall back
-- to the project and show up under Unfiled.
CREATE TABLE IF NOT EXISTS updates (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    topic_id    INTEGER          REFERENCES topics(id)   ON DELETE SET NULL,
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS updates_by_project ON updates(project_id, id DESC);
CREATE INDEX IF NOT EXISTS updates_by_topic ON updates(topic_id, id DESC);

-- One cached summary per SCOPE: topic_id NULL means the whole project, and a
-- topic id means just that topic. `through_update_id` is the newest update it
-- was built from, which is how the page knows it has gone stale without
-- re-running Claude to find out.
CREATE TABLE IF NOT EXISTS summaries (
    project_id        INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    topic_id          INTEGER          REFERENCES topics(id)   ON DELETE CASCADE,
    body              TEXT NOT NULL,
    through_update_id INTEGER NOT NULL,
    -- How many updates were in scope when this was written. A watermark alone
    -- only notices the set growing at the top; linking an OLDER update into a
    -- project, or deleting one, changes what the brief should say without
    -- moving the newest id at all.
    read_updates      INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL
);
-- NOT a PRIMARY KEY: SQLite permits NULLs in primary key columns, so
-- PRIMARY KEY (project_id, topic_id) would happily store two whole-project
-- summaries. IFNULL folds the NULL scope into one slot and makes it a real
-- constraint.
CREATE UNIQUE INDEX IF NOT EXISTS summaries_scope
    ON summaries(project_id, IFNULL(topic_id, 0));

-- An update has one home — the project or person you wrote it in — and any
-- number of links to others. A note from a 1-1 that is really about the GoBMP
-- migration is linked to GoBMP; it is not copied there. Copying raw text would
-- make two of the one thing that cannot be regenerated, and they would drift.
-- Deleting either end removes the link and nothing else.
CREATE TABLE IF NOT EXISTS update_links (
    update_id  INTEGER NOT NULL REFERENCES updates(id)   ON DELETE CASCADE,
    project_id INTEGER NOT NULL REFERENCES projects(id)  ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (update_id, project_id)
);
CREATE INDEX IF NOT EXISTS links_by_project ON update_links(project_id);

-- Questions are kept because the answer to "why did we pick Postgres" is worth
-- as much the second time, and re-asking costs another Claude call.
CREATE TABLE IF NOT EXISTS answers (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    topic_id    INTEGER          REFERENCES topics(id)   ON DELETE SET NULL,
    question    TEXT NOT NULL,
    answer      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS answers_by_project ON answers(project_id, id DESC);

-- The left pane, and the only thing in here that reads every project at once.
-- One row: it has no scope to key on, which is the whole point of it.
--
-- It goes stale two ways. A new update anywhere is the familiar one. The other
-- is the calendar: an agenda that says "today" was true on the morning it was
-- built and is a lie the morning after, so `for_date` is checked exactly as
-- strictly as `through_update_id`.
CREATE TABLE IF NOT EXISTS agenda (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    body              TEXT NOT NULL,
    for_date          TEXT NOT NULL,
    through_update_id INTEGER NOT NULL,
    read_updates      INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL
);

-- The same thing for one page. `agenda` is one row because reading every
-- project at once is the point of it; this one is keyed by page because
-- reading one page deeply is the point of this. The cross-project pane caps
-- each project at 40 updates and then keeps only the 12 most urgent items
-- across all of them, so a page's own third-most-urgent deadline can fall off
-- a cliff it never sees. This pane has one page to spend its budget on.
-- The brief for a meeting you are about to walk into. One per page, like the
-- pane: you prepare for the next conversation, not for a list of them.
-- `since` is the day the last one happened, kept because the brief is written
-- against it and a brief that no longer says which gap it covers is a brief you
-- cannot trust.
CREATE TABLE IF NOT EXISTS preps (
    project_id        INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    body              TEXT NOT NULL,
    since             TEXT,
    through_update_id INTEGER NOT NULL,
    read_updates      INTEGER NOT NULL,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS page_agenda (
    project_id        INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    body              TEXT NOT NULL,
    for_date          TEXT NOT NULL,
    through_update_id INTEGER NOT NULL,
    read_updates      INTEGER NOT NULL,
    created_at        TEXT NOT NULL
);
"""


def today() -> str:
    """The local calendar date. The agenda is built for one specific day, and
    the pane compares this against the day it was built for."""
    return datetime.date.today().isoformat()


def now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _cols(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _has(conn, table: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _migrate(conn) -> list[str]:
    """Bring an existing store up to the current schema, in place.

    CREATE TABLE IF NOT EXISTS does nothing to a table that already exists, so
    a store made before topics needs the new columns added explicitly. Every
    step is guarded by what is actually in the file, so running it twice is a
    no-op.

    Deliberately no executescript(): it COMMITs any open transaction before it
    runs, which would silently break init()'s atomicity and leave a store
    half-migrated if a later statement failed. Every statement here goes
    through execute() so the whole thing is one transaction that rolls back.
    """
    done = []
    if _has(conn, "updates") and "topic_id" not in _cols(conn, "updates"):
        conn.execute("ALTER TABLE updates ADD COLUMN topic_id INTEGER"
                     " REFERENCES topics(id) ON DELETE SET NULL")
        done.append("updates.topic_id")
    if _has(conn, "answers") and "topic_id" not in _cols(conn, "answers"):
        conn.execute("ALTER TABLE answers ADD COLUMN topic_id INTEGER"
                     " REFERENCES topics(id) ON DELETE SET NULL")
        done.append("answers.topic_id")
    if _has(conn, "summaries") and "topic_id" not in _cols(conn, "summaries"):
        # summaries was keyed `project_id INTEGER PRIMARY KEY`, which ALTER
        # cannot widen. Rebuilt rather than dropped: the existing rows are
        # whole-project summaries and stay valid, and binning them would cost a
        # Claude call each to get back.
        conn.execute("ALTER TABLE summaries RENAME TO summaries_old")
        conn.execute("""
            CREATE TABLE summaries (
                project_id        INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                topic_id          INTEGER          REFERENCES topics(id)   ON DELETE CASCADE,
                body              TEXT NOT NULL,
                through_update_id INTEGER NOT NULL,
                created_at        TEXT NOT NULL
            )""")
        conn.execute(
            "INSERT INTO summaries (project_id, topic_id, body, through_update_id,"
            " created_at) SELECT project_id, NULL, body, through_update_id, created_at"
            " FROM summaries_old")
        conn.execute("DROP TABLE summaries_old")
        done.append("summaries rekeyed by scope")
    if _has(conn, "projects") and "kind" not in _cols(conn, "projects"):
        conn.execute("ALTER TABLE projects ADD COLUMN kind TEXT NOT NULL"
                     " DEFAULT 'project'")
        done.append("projects.kind")
    if _has(conn, "projects") and "about" not in _cols(conn, "projects"):
        conn.execute("ALTER TABLE projects ADD COLUMN about TEXT")
        done.append("projects.about")
    if _has(conn, "projects") and "guidance" not in _cols(conn, "projects"):
        conn.execute("ALTER TABLE projects ADD COLUMN guidance TEXT")
        done.append("projects.guidance")
    if _has(conn, "summaries") and "read_updates" not in _cols(conn, "summaries"):
        conn.execute("ALTER TABLE summaries ADD COLUMN read_updates INTEGER NOT NULL"
                     " DEFAULT 0")
        done.append("summaries.read_updates")
    if _has(conn, "agenda") and "read_updates" not in _cols(conn, "agenda"):
        conn.execute("ALTER TABLE agenda ADD COLUMN read_updates INTEGER NOT NULL DEFAULT 0")
        done.append("agenda.read_updates")
    return done


def init() -> None:
    conn = connect()
    try:
        # Three steps, in this order. PRELUDE first because topics must exist
        # before any column can reference it. Then the migration, alone in its
        # own transaction so a failure rolls the whole thing back. Then the
        # rest. The DDL either side is idempotent, so a crash between steps is
        # fixed by running init() again.
        conn.executescript(PRELUDE)
        with conn:
            moves = _migrate(conn)
        conn.executescript(SCHEMA)
        if moves:
            import sys
            print("  migrated: " + ", ".join(moves), file=sys.stderr)
    finally:
        conn.close()


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


# ------------------------------------------------------------------ projects

KINDS = ("project", "person")


def _kind(k) -> str:
    k = (k or "project").strip().lower()
    if k not in KINDS:
        raise ValueError("a thing is either a project or a person")
    return k


def projects() -> list[dict]:
    """Projects and people together, each carrying its `kind`.

    Both at once on purpose: the home view tabs between them and needs the
    count for the side you are not on, and the agenda wants everything anyway —
    a promise made in a 1-1 is as due as one made in a project update.
    """
    conn = connect()
    try:
        # Counted with subqueries, not two LEFT JOINs: joining both would
        # multiply the rows and report updates × topics for each.
        return _rows(conn.execute("""
            SELECT p.id, p.name, p.kind, p.created_at,
                   (SELECT count(*) FROM updates u WHERE u.project_id = p.id)    AS updates,
                   (SELECT count(*) FROM topics  t WHERE t.project_id = p.id)    AS topics,
                   (SELECT max(created_at) FROM updates u WHERE u.project_id = p.id)
                       AS last_update_at
            FROM projects p
            ORDER BY last_update_at IS NULL, last_update_at DESC, p.name COLLATE NOCASE
        """))
    finally:
        conn.close()


def create_project(name: str, kind=None) -> dict:
    kind = _kind(kind)
    name = name.strip()
    if not name:
        raise ValueError("a person needs a name" if kind == "person"
                         else "a project needs a name")
    if len(name) > 120:
        raise ValueError("that name is too long")
    conn = connect()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO projects (name, kind, created_at) VALUES (?, ?, ?)",
                (name, kind, now()))
        return {"id": cur.lastrowid, "name": name, "kind": kind}
    except sqlite3.IntegrityError:
        # Names are unique across both kinds. Two things called the same thing
        # would be two pages you cannot tell apart in the Now pane.
        raise ValueError(f"there is already a project or person called {name!r}")
    finally:
        conn.close()


def rename_project(pid: int, name: str) -> dict:
    """Rename a page, and drop the one brief that was built on its old name.

    Prep is the only thing here that reaches across pages, and it finds what it
    reaches by a whole-word match on this page's name (see `prep_context`).
    Rename the page and that match moves underneath it: an update elsewhere
    saying the old name stops being found, one saying the new name starts. The
    cached brief was written from the old reading of the store.

    That is the same invisible staleness a changed profile causes — a brief
    written against a name nobody uses any more still reads perfectly well — so
    it gets the same treatment: dropped, not flagged, and the page says so.
    Summaries and the pane stay: neither has ever matched on a name, and both
    are about updates that did not move.
    """
    name = name.strip()
    if not name:
        raise ValueError("a project needs a name")
    conn = connect()
    try:
        row = conn.execute("SELECT name FROM projects WHERE id = ?", (pid,)).fetchone()
        if not row:
            raise ValueError("no such project")
        if row["name"] == name:
            return {"id": pid, "name": name, "was": name, "cleared": []}
        with conn:
            conn.execute("UPDATE projects SET name = ? WHERE id = ?", (name, pid))
            conn.execute("DELETE FROM preps WHERE project_id = ?", (pid,))
        return {"id": pid, "name": name, "was": row["name"], "cleared": ["preps"]}
    except sqlite3.IntegrityError:
        raise ValueError(f"there is already a project called {name!r}")
    finally:
        conn.close()


ABOUT_MAX = 2000
GUIDANCE_MAX = 1000


def set_page_setup(pid: int, about=None, guidance=None) -> dict:
    """The two things you write about a page rather than into its log.

    `about` is context — who this page is to you. It rides on stdin with the
    updates and changes what a brief selects.

    `guidance` is instruction — what you want out of a brief for this page. It
    rides in argv with the prompt, below the rules, because it is addressed to
    the writer rather than describing the subject. That is a real difference in
    power and the UI says which is which.

    Both are yours: stored as typed, never rewritten, never written by a model.

    Either may be left as None to mean "leave it alone". What actually changed
    decides which caches go, because the two reach different things — guidance
    never touches the pane, so editing it must not cost you the pane.
    """
    conn = connect()
    try:
        with conn:
            cur = conn.execute("SELECT about, guidance FROM projects WHERE id = ?",
                               (pid,)).fetchone()
            if not cur:
                raise ValueError("no such project or person")
            new = {"about": cur["about"], "guidance": cur["guidance"]}
            if about is not None:
                new["about"] = about.strip()[:ABOUT_MAX] or None
            if guidance is not None:
                new["guidance"] = guidance.strip()[:GUIDANCE_MAX] or None

            moved = {k for k in new if new[k] != cur[k]}
            if not moved:
                return {"id": pid, **new, "cleared": []}
            conn.execute("UPDATE projects SET about = ?, guidance = ? WHERE id = ?",
                         (new["about"], new["guidance"], pid))

            # A brief written before either changed was written for a different
            # reader, or to a different brief. Dropped rather than flagged:
            # unlike the other three kinds of staleness this one is invisible
            # on the page — a brief for the wrong reader reads perfectly well.
            gone = ["summaries", "preps"]
            if "about" in moved:
                gone.append("page_agenda")   # guidance never reaches the pane
            for t in gone:
                conn.execute(f"DELETE FROM {t} WHERE project_id = ?", (pid,))
        # Answers stay. An answer quotes the raw text rather than interpreting
        # it, and it answered the question that was actually asked.
        return {"id": pid, **new, "cleared": gone}
    finally:
        conn.close()


def set_about(pid: int, about: str) -> dict:
    return set_page_setup(pid, about=about)


def set_guidance(pid: int, guidance: str) -> dict:
    return set_page_setup(pid, guidance=guidance)


def delete_project(pid: int) -> dict:
    """Deletes the updates too, via ON DELETE CASCADE. The caller is expected
    to have asked first — this is the one irreversible thing in the app."""
    conn = connect()
    try:
        with conn:
            n = conn.execute("DELETE FROM projects WHERE id = ?", (pid,)).rowcount
        if not n:
            raise ValueError("no such project")
        return {"deleted": pid}
    finally:
        conn.close()


def project(pid: int) -> dict:
    """Everything the project page needs, in one read.

    Updates come back whole and the page filters them by topic, so switching
    topics is instant and costs no round trip. Summaries come back keyed by
    scope — "" for the whole project, a topic id otherwise — because each scope
    goes stale on its own schedule.
    """
    conn = connect()
    try:
        p = conn.execute("SELECT id, name, kind, about, guidance, created_at"
                         " FROM projects WHERE id = ?", (pid,)).fetchone()
        if not p:
            raise ValueError("no such project")
        out = dict(p)
        out["topics"] = _rows(conn.execute("""
            SELECT t.id, t.name, t.created_at,
                   count(u.id)       AS updates,
                   max(u.created_at) AS last_update_at
            FROM topics t
            LEFT JOIN updates u ON u.topic_id = t.id
            WHERE t.project_id = ?
            GROUP BY t.id ORDER BY t.name COLLATE NOCASE
        """, (pid,)))
        own = _rows(conn.execute(
            "SELECT id, topic_id, body, created_at FROM updates"
            " WHERE project_id = ? ORDER BY id DESC", (pid,)))
        for u in own:
            u["via"] = None
            u["links"] = []
        by_id = {u["id"]: u for u in own}
        for r in conn.execute("""
                SELECT l.update_id, p.id, p.name, p.kind
                FROM update_links l JOIN projects p ON p.id = l.project_id
                WHERE l.update_id IN (SELECT id FROM updates WHERE project_id = ?)
                ORDER BY p.name COLLATE NOCASE
        """, (pid,)):
            by_id[r["update_id"]]["links"].append(
                {"id": r["id"], "name": r["name"], "kind": r["kind"]})

        # Updates that live somewhere else and were linked here. Guests: they
        # carry where they came from, and their topic belongs to their home, so
        # they are not filed under anything on this page.
        guests = _rows(conn.execute("""
            SELECT u.id, NULL AS topic_id, u.body, u.created_at,
                   h.id AS via_id, h.name AS via_name, h.kind AS via_kind
            FROM update_links l
            JOIN updates  u ON u.id = l.update_id
            JOIN projects h ON h.id = u.project_id
            WHERE l.project_id = ? AND u.project_id != ?
            ORDER BY u.id DESC
        """, (pid, pid)))
        for g in guests:
            g["via"] = {"id": g.pop("via_id"), "name": g.pop("via_name"),
                        "kind": g.pop("via_kind")}
            g["links"] = []
        out["updates"] = sorted(own + guests, key=lambda u: u["id"], reverse=True)
        out["answers"] = _rows(conn.execute(
            "SELECT id, topic_id, question, answer, created_at FROM answers"
            " WHERE project_id = ? ORDER BY id DESC LIMIT 40", (pid,)))
        # Unfiled means "written here and not yet filed". A guest is not
        # unfiled — filing it is its home's business, not this page's.
        out["unfiled"] = sum(1 for u in out["updates"]
                             if u["topic_id"] is None and not u["via"])

        # A scope's summary is stale against the newest update IN THAT SCOPE.
        # Filing something under Rollout must not mark the Vendor brief stale.
        newest = {"": 0}
        count = {"": 0}
        for t in out["topics"]:
            newest[str(t["id"])] = 0
            count[str(t["id"])] = 0
        for u in out["updates"]:
            newest[""] = max(newest[""], u["id"])
            count[""] += 1
            k = str(u["topic_id"])
            # A guest has no topic here, so it counts towards the whole project
            # and towards no topic — which is exactly where it is read from.
            if k in newest and not u["via"]:
                newest[k] = max(newest[k], u["id"])
                count[k] += 1
        summaries = {}
        for s in conn.execute(
                "SELECT topic_id, body, through_update_id, read_updates, created_at"
                " FROM summaries WHERE project_id = ?", (pid,)):
            k = "" if s["topic_id"] is None else str(s["topic_id"])
            d = dict(s)
            d["behind"] = sum(1 for u in out["updates"]
                              if u["id"] > s["through_update_id"]
                              and (k == "" or (str(u["topic_id"]) == k and not u["via"])))
            # Two ways to be out of date, as the agenda has three: something
            # newer, or the set itself no longer matching. Linking an older
            # update in is the second, and a watermark cannot see it.
            d["stale"] = (newest.get(k, 0) > s["through_update_id"]
                          or count.get(k, 0) != s["read_updates"])
            summaries[k] = d
        out["summaries"] = summaries
        return out
    finally:
        conn.close()


# -------------------------------------------------------------------- topics

def _own_topic(conn, pid: int, tid) -> int | None:
    """A topic id is only meaningful inside its own project. Without this you
    could file an update under another project's topic and it would vanish from
    both pages."""
    if tid in (None, "", 0):
        return None
    row = conn.execute("SELECT project_id FROM topics WHERE id = ?", (tid,)).fetchone()
    if not row:
        raise ValueError("no such topic")
    if row["project_id"] != pid:
        raise ValueError("that topic belongs to another project")
    return int(tid)


def create_topic(pid: int, name: str) -> dict:
    name = name.strip()
    if not name:
        raise ValueError("a topic needs a name")
    if len(name) > 120:
        raise ValueError("that name is too long")
    conn = connect()
    try:
        if not conn.execute("SELECT 1 FROM projects WHERE id = ?", (pid,)).fetchone():
            raise ValueError("no such project")
        with conn:
            cur = conn.execute(
                "INSERT INTO topics (project_id, name, created_at) VALUES (?,?,?)",
                (pid, name, now()))
        return {"id": cur.lastrowid, "project_id": pid, "name": name}
    except sqlite3.IntegrityError:
        raise ValueError(f"this project already has a topic called {name!r}")
    finally:
        conn.close()


def rename_topic(tid: int, name: str) -> dict:
    name = name.strip()
    if not name:
        raise ValueError("a topic needs a name")
    conn = connect()
    try:
        with conn:
            n = conn.execute("UPDATE topics SET name = ? WHERE id = ?",
                             (name, tid)).rowcount
        if not n:
            raise ValueError("no such topic")
        return {"id": tid, "name": name}
    except sqlite3.IntegrityError:
        raise ValueError(f"this project already has a topic called {name!r}")
    finally:
        conn.close()


def delete_topic(tid: int) -> dict:
    """Unfiles its updates; never deletes them.

    ON DELETE SET NULL on updates.topic_id does this. Deleting a topic is a
    filing decision, and a filing decision must not be able to destroy the one
    thing in this database that cannot be regenerated. The updates reappear
    under Unfiled. The topic's cached summary DOES go, because it describes a
    scope that no longer exists.
    """
    conn = connect()
    try:
        row = conn.execute("SELECT name FROM topics WHERE id = ?", (tid,)).fetchone()
        if not row:
            raise ValueError("no such topic")
        n = conn.execute("SELECT count(*) FROM updates WHERE topic_id = ?",
                         (tid,)).fetchone()[0]
        with conn:
            conn.execute("DELETE FROM topics WHERE id = ?", (tid,))
        return {"deleted": tid, "name": row["name"], "unfiled": n}
    finally:
        conn.close()


# ------------------------------------------------------------------- updates

def add_update(pid: int, body: str, topic_id=None, links=None) -> dict:
    body = body.strip()
    if not body:
        raise ValueError("nothing to add")
    conn = connect()
    try:
        if not conn.execute("SELECT 1 FROM projects WHERE id = ?", (pid,)).fetchone():
            raise ValueError("no such project")
        tid = _own_topic(conn, pid, topic_id)
        ts = now()
        # One transaction: an update that saved without the links you chose
        # would look filed correctly and quietly not be.
        with conn:
            cur = conn.execute(
                "INSERT INTO updates (project_id, topic_id, body, created_at)"
                " VALUES (?,?,?,?)", (pid, tid, body, ts))
            for other in (links or []):
                _link(conn, cur.lastrowid, int(other))
        return {"id": cur.lastrowid, "topic_id": tid, "body": body, "created_at": ts}
    finally:
        conn.close()


def move_update(uid: int, topic_id) -> dict:
    """File an update, or unfile it. Its body is not touched."""
    conn = connect()
    try:
        row = conn.execute("SELECT project_id FROM updates WHERE id = ?",
                           (uid,)).fetchone()
        if not row:
            raise ValueError("no such update")
        tid = _own_topic(conn, row["project_id"], topic_id)
        with conn:
            conn.execute("UPDATE updates SET topic_id = ? WHERE id = ?", (tid, uid))
        return {"id": uid, "topic_id": tid}
    finally:
        conn.close()


def delete_update(uid: int) -> dict:
    conn = connect()
    try:
        with conn:
            n = conn.execute("DELETE FROM updates WHERE id = ?", (uid,)).rowcount
        if not n:
            raise ValueError("no such update")
        return {"deleted": uid}
    finally:
        conn.close()


# ------------------------------------------------------- derived, cached, cheap

def _link(conn, uid: int, pid: int) -> None:
    home = conn.execute("SELECT project_id FROM updates WHERE id = ?", (uid,)).fetchone()
    if not home:
        raise ValueError("no such update")
    if home["project_id"] == pid:
        # Not an error worth shouting about, but not a link either: an update
        # is already in its own home, and a row saying otherwise would render
        # the thing twice on its own page.
        return
    if not conn.execute("SELECT 1 FROM projects WHERE id = ?", (pid,)).fetchone():
        raise ValueError("no such project or person")
    conn.execute("INSERT OR IGNORE INTO update_links (update_id, project_id,"
                 " created_at) VALUES (?, ?, ?)", (uid, pid, now()))


def link_update(uid: int, pid: int) -> dict:
    """Put an existing update in front of another project or person too.

    The text is not touched and not copied — this is one row saying the update
    is relevant in two places. Unlinking removes only that row.
    """
    conn = connect()
    try:
        with conn:
            _link(conn, uid, pid)
        return {"id": uid, "project_id": pid, "linked": True}
    finally:
        conn.close()


def unlink_update(uid: int, pid: int) -> dict:
    conn = connect()
    try:
        with conn:
            conn.execute("DELETE FROM update_links WHERE update_id = ?"
                         " AND project_id = ?", (uid, pid))
        return {"id": uid, "project_id": pid, "linked": False}
    finally:
        conn.close()


def save_summary(pid: int, body: str, through: int, topic_id=None, read=0) -> dict:
    ts = now()
    conn = connect()
    try:
        with conn:
            # The conflict target is the expression index, not the columns —
            # see summaries_scope for why the constraint is written that way.
            conn.execute(
                "INSERT INTO summaries (project_id, topic_id, body, through_update_id,"
                " read_updates, created_at) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(project_id, IFNULL(topic_id, 0)) DO UPDATE SET"
                " body=excluded.body, through_update_id=excluded.through_update_id,"
                " read_updates=excluded.read_updates,"
                " created_at=excluded.created_at",
                (pid, topic_id, body, through, read, ts))
        return {"topic_id": topic_id, "body": body, "through_update_id": through,
                "read_updates": read, "created_at": ts, "stale": False, "behind": 0}
    finally:
        conn.close()


def save_answer(pid: int, question: str, answer: str, topic_id=None) -> dict:
    ts = now()
    conn = connect()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO answers (project_id, topic_id, question, answer, created_at)"
                " VALUES (?,?,?,?,?)", (pid, topic_id, question, answer, ts))
        return {"id": cur.lastrowid, "topic_id": topic_id, "question": question,
                "answer": answer, "created_at": ts}
    finally:
        conn.close()


def delete_answer(aid: int) -> dict:
    conn = connect()
    try:
        with conn:
            n = conn.execute("DELETE FROM answers WHERE id = ?", (aid,)).rowcount
        if not n:
            raise ValueError("no such answer")
        return {"deleted": aid}
    finally:
        conn.close()


SEARCH_MAX = 200


def search(q: str) -> dict:
    """Every update and page that says this, across the whole store.

    The one read that spans everything by design. It is plain substring match —
    SQL, no model, no ranking — because search is the correlation primitive
    where a false positive costs nothing: you typed the word, you can see the
    line it matched, and nothing is written or filed anywhere. The clever
    matching (whole words, confirmation) is reserved for the places that
    create edges; a lookup gets to be dumb and instant.

    Newest first, because "where did this last come up" is the usual question.
    Guests are not duplicated: an update matches once, on its home page.
    """
    q = (q or "").strip()
    if len(q) < 2:
        return {"q": q, "pages": [], "updates": [], "total": 0, "dropped": 0}
    like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    conn = connect()
    try:
        pages = _rows(conn.execute(
            "SELECT id, name, kind FROM projects WHERE name LIKE ? ESCAPE '\\'"
            " ORDER BY name COLLATE NOCASE", (like,)))
        total = conn.execute(
            "SELECT count(*) FROM updates WHERE body LIKE ? ESCAPE '\\'",
            (like,)).fetchone()[0]
        rows = _rows(conn.execute("""
            SELECT u.id, u.body, u.created_at, t.name AS topic,
                   p.id AS page_id, p.name AS page_name, p.kind AS page_kind
            FROM updates u
            JOIN projects p ON p.id = u.project_id
            LEFT JOIN topics t ON t.id = u.topic_id
            WHERE u.body LIKE ? ESCAPE '\\'
            ORDER BY u.created_at DESC, u.id DESC LIMIT ?
        """, (like, SEARCH_MAX)))
        for r in rows:
            r["page"] = {"id": r.pop("page_id"), "name": r.pop("page_name"),
                         "kind": r.pop("page_kind")}
        return {"q": q, "pages": pages, "updates": rows,
                "total": total, "dropped": max(0, total - SEARCH_MAX)}
    finally:
        conn.close()


# -------------------------------------------------------------------- agenda

# Per project, so one noisy project cannot crowd out a quiet one that has the
# deadline you actually needed to see. Anything beyond this is reported in the
# pane rather than dropped silently — a cap you cannot see reads as "there was
# nothing else", which is the one thing it must not do.
AGENDA_PER_PROJECT = 40


def agenda_context() -> dict:
    """Every project's recent updates, for the one cross-project read.

    Newest last, like every other context this app builds, so the model reads
    each project forwards. Projects with nothing in them are included by name
    anyway: "no updates" about a live project is itself worth the model seeing.
    """
    conn = connect()
    try:
        out, dropped = [], 0
        for p in conn.execute(
                "SELECT id, name, kind FROM projects ORDER BY name COLLATE NOCASE"):
            n = conn.execute("SELECT count(*) FROM updates WHERE project_id = ?",
                             (p["id"],)).fetchone()[0]
            rows = _rows(conn.execute("""
                SELECT u.id, u.body, u.created_at, t.name AS topic
                FROM updates u LEFT JOIN topics t ON t.id = u.topic_id
                WHERE u.project_id = ? ORDER BY u.id DESC LIMIT ?
            """, (p["id"], AGENDA_PER_PROJECT)))
            dropped += max(0, n - AGENDA_PER_PROJECT)
            out.append({"id": p["id"], "name": p["name"], "kind": p["kind"],
                        "updates": list(reversed(rows))})
        return {
            "projects": out,
            "dropped": dropped,
            "newest": conn.execute("SELECT ifnull(max(id), 0) FROM updates").fetchone()[0],
            "total": conn.execute("SELECT count(*) FROM updates").fetchone()[0],
        }
    finally:
        conn.close()


def agenda() -> dict:
    """The cached agenda, with both kinds of staleness already worked out."""
    conn = connect()
    try:
        total = conn.execute("SELECT count(*) FROM updates").fetchone()[0]
        # What the cap left out, so the pane can say so. A cap you cannot see
        # reads as "there was nothing else".
        dropped = conn.execute("""
            SELECT ifnull(sum(max(0, n - ?)), 0) FROM
                (SELECT count(*) AS n FROM updates GROUP BY project_id)
        """, (AGENDA_PER_PROJECT,)).fetchone()[0]
        r = conn.execute("SELECT body, for_date, through_update_id, read_updates,"
                         " created_at FROM agenda WHERE id = 1").fetchone()
        if not r:
            return {"built": False, "items": [], "updates": total,
                    "dropped": dropped, "per_project": AGENDA_PER_PROJECT}
        behind = conn.execute("SELECT count(*) FROM updates WHERE id > ?",
                              (r["through_update_id"],)).fetchone()[0]
        outdated = r["for_date"] != today()
        # A newest-id watermark only notices the set growing. Delete a project
        # and its updates go with it, leaving items about work that no longer
        # exists — so the count it read is checked as well.
        changed = total != r["read_updates"] and not behind
        return {
            "built": True,
            # Stored as JSON because it was validated on the way in; a row that
            # will not parse means the store was edited by hand, and an empty
            # pane is a better answer to that than a broken page.
            "items": json.loads(r["body"]),
            "for_date": r["for_date"],
            "created_at": r["created_at"],
            "through_update_id": r["through_update_id"],
            "behind": behind,
            "outdated": outdated,
            "changed": changed,
            "stale": bool(behind or outdated or changed),
            "updates": total,
            "dropped": dropped,
            "per_project": AGENDA_PER_PROJECT,
        }
    finally:
        conn.close()


def save_agenda(items: list, through: int, for_date: str, read: int) -> dict:
    conn = connect()
    try:
        with conn:
            conn.execute("""
                INSERT INTO agenda (id, body, for_date, through_update_id,
                                    read_updates, created_at)
                VALUES (1, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    body = excluded.body, for_date = excluded.for_date,
                    through_update_id = excluded.through_update_id,
                    read_updates = excluded.read_updates,
                    created_at = excluded.created_at
            """, (json.dumps(items), for_date, through, read, now()))
    finally:
        conn.close()
    return agenda()


def agenda_items(items: list) -> dict:
    """Rewrite the cached items and nothing else.

    Deliberately leaves `through_update_id`, `read_updates` and `for_date`
    alone. Acting on an item writes an update, so the pane should go visibly
    behind the moment you act — that is the honest state. It is showing you a
    list built before the thing you just did.
    """
    conn = connect()
    try:
        with conn:
            n = conn.execute("UPDATE agenda SET body = ? WHERE id = 1",
                             (json.dumps(items),)).rowcount
        if not n:
            raise ValueError("no agenda to act on")
    finally:
        conn.close()
    return agenda()


# --------------------------------------------------------------- page pane

# A page reads its own updates deeply rather than broadly: the cross-project
# pane spends its budget across every project, this one has a single page to
# spend it on. Still a cap, and still printed in the pane when it bites.
PAGE_AGENDA_MAX = 120


def _page_scope(conn, pid: int):
    """The ids in a page's pane scope: its own updates and its guests.

    A guest is in scope because that is the entire point of linking. A
    commitment Priya made in a 1-1, linked to GoBMP, is a GoBMP deadline; a
    pane that could not see it would make linking decorative.
    """
    return [r[0] for r in conn.execute("""
        SELECT id FROM updates WHERE project_id = ?
        UNION
        SELECT update_id FROM update_links WHERE project_id = ?
    """, (pid, pid))]


def page_agenda_context(pid: int) -> dict:
    """One page's updates, oldest last, guests stamped with where they came from."""
    conn = connect()
    try:
        p = conn.execute("SELECT id, name, kind, about FROM projects WHERE id = ?",
                         (pid,)).fetchone()
        if not p:
            raise ValueError("no such project or person")
        rows = _rows(conn.execute("""
            SELECT u.id, u.body, u.created_at, t.name AS topic,
                   h.id AS via_id, h.name AS via_name, h.kind AS via_kind
            FROM updates u
            LEFT JOIN topics   t ON t.id = u.topic_id
            JOIN      projects h ON h.id = u.project_id
            WHERE u.id IN (
                SELECT id FROM updates WHERE project_id = :p
                UNION SELECT update_id FROM update_links WHERE project_id = :p)
            ORDER BY u.id DESC LIMIT :n
        """, {"p": pid, "n": PAGE_AGENDA_MAX}))
        for r in rows:
            home = r.pop("via_id"), r.pop("via_name"), r.pop("via_kind")
            r["via"] = None if home[0] == pid else {
                "id": home[0], "name": home[1], "kind": home[2]}
        total = len(_page_scope(conn, pid))
        return {
            "id": p["id"], "name": p["name"], "kind": p["kind"],
            "about": p["about"],
            "updates": list(reversed(rows)),
            "newest": max((r["id"] for r in rows), default=0),
            "total": total,
            "dropped": max(0, total - PAGE_AGENDA_MAX),
        }
    finally:
        conn.close()


def page_agenda(pid: int) -> dict:
    """The cached pane for one page, with all three kinds of staleness worked out.

    The same three axes as the cross-project pane, measured over this page's
    scope rather than the whole store: a new update on a project you are not
    looking at must not make this one say it is behind.
    """
    conn = connect()
    try:
        ids = _page_scope(conn, pid)
        total = len(ids)
        dropped = max(0, total - PAGE_AGENDA_MAX)
        r = conn.execute("SELECT body, for_date, through_update_id, read_updates,"
                         " created_at FROM page_agenda WHERE project_id = ?",
                         (pid,)).fetchone()
        if not r:
            return {"built": False, "items": [], "updates": total,
                    "dropped": dropped, "cap": PAGE_AGENDA_MAX}
        behind = sum(1 for i in ids if i > r["through_update_id"])
        changed = total != r["read_updates"] and not behind
        return {
            "built": True,
            "items": json.loads(r["body"]),
            "for_date": r["for_date"],
            "created_at": r["created_at"],
            "through_update_id": r["through_update_id"],
            "behind": behind,
            "outdated": r["for_date"] != today(),
            "changed": changed,
            "stale": bool(behind or r["for_date"] != today() or changed),
            "updates": total,
            "dropped": dropped,
            "cap": PAGE_AGENDA_MAX,
        }
    finally:
        conn.close()


def save_page_agenda(pid: int, items: list, through: int, for_date: str,
                     read: int) -> dict:
    conn = connect()
    try:
        with conn:
            conn.execute("""
                INSERT INTO page_agenda (project_id, body, for_date,
                                         through_update_id, read_updates, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    body = excluded.body, for_date = excluded.for_date,
                    through_update_id = excluded.through_update_id,
                    read_updates = excluded.read_updates,
                    created_at = excluded.created_at
            """, (pid, json.dumps(items), for_date, through, read, now()))
    finally:
        conn.close()
    return page_agenda(pid)


def page_agenda_items(pid: int, items: list) -> dict:
    """Rewrite one page's cached items and nothing else — see `agenda_items`."""
    conn = connect()
    try:
        with conn:
            n = conn.execute("UPDATE page_agenda SET body = ? WHERE project_id = ?",
                             (json.dumps(items), pid)).rowcount
        if not n:
            raise ValueError("no pane to act on")
    finally:
        conn.close()
    return page_agenda(pid)


# ------------------------------------------------------------- meeting prep

PREP_MAX = 120


def last_meeting(pid: int) -> str | None:
    """The day of the newest update the page itself holds.

    A 1-1 note is the record of the 1-1, so the last one you wrote is the last
    time you spoke. Guests are excluded on purpose: an update linked in from a
    project is work, not a conversation, and letting it move this date would
    make the brief skip the very thing it is meant to catch you up on.
    """
    conn = connect()
    try:
        # By date rather than by id. They agree for anything this app wrote —
        # created_at is always now() — but the question asked here is "when did
        # we last speak", and that is a date, so ask it of the dates.
        r = conn.execute("SELECT created_at FROM updates WHERE project_id = ?"
                         " ORDER BY created_at DESC, id DESC LIMIT 1",
                         (pid,)).fetchone()
        return r["created_at"][:10] if r else None
    finally:
        conn.close()


def prep_context(pid: int, since: str | None) -> dict:
    """Everything worth reading before the next meeting about this page.

    Three sources, and the brief is told which is which:

      own        this page's updates. The history.
      linked     updates you explicitly linked here. Also history, with a
                 mouth attached to it.
      elsewhere  updates on OTHER pages, written since the last meeting, that
                 name this page. This is "the work done in between", and it is
                 the only place in the app that reaches across pages without
                 being asked to.

    That last one is a whole-word match on the page's name — the same match the
    mention nudge uses, and no more clever than that. It is safe here in a way
    it would not be as a filing decision: nothing is written, every line is
    attributed to the page it came from, and if it picks up the wrong Priya you
    will see it said so and ignore it.
    """
    conn = connect()
    try:
        p = conn.execute("SELECT id, name, kind, about, guidance FROM projects"
                         " WHERE id = ?", (pid,)).fetchone()
        if not p:
            raise ValueError("no such project or person")

        own = _rows(conn.execute("""
            SELECT u.id, u.body, u.created_at, t.name AS topic
            FROM updates u LEFT JOIN topics t ON t.id = u.topic_id
            WHERE u.project_id = ? ORDER BY u.id DESC LIMIT ?
        """, (pid, PREP_MAX)))
        for r in own:
            r["source"] = "own"

        linked = _rows(conn.execute("""
            SELECT u.id, u.body, u.created_at, NULL AS topic,
                   h.name AS from_name, h.kind AS from_kind
            FROM update_links l
            JOIN updates  u ON u.id = l.update_id
            JOIN projects h ON h.id = u.project_id
            WHERE l.project_id = ? AND u.project_id != ?
            ORDER BY u.id DESC LIMIT ?
        """, (pid, pid, PREP_MAX)))
        for r in linked:
            r["source"] = "linked"

        # Whole words only, and never the page itself. LIKE is case-insensitive
        # for ASCII in SQLite; the boundary check is done in Python because a
        # portable one in SQL would be a wall of replace().
        seen = {r["id"] for r in own} | {r["id"] for r in linked}
        elsewhere = []
        if len(p["name"]) >= 3:
            pat = re.compile(rf"(^|[^0-9A-Za-z]){re.escape(p['name'])}([^0-9A-Za-z]|$)",
                             re.I)
            rows = _rows(conn.execute("""
                SELECT u.id, u.body, u.created_at, NULL AS topic,
                       h.name AS from_name, h.kind AS from_kind
                FROM updates u JOIN projects h ON h.id = u.project_id
                WHERE u.project_id != ? AND u.body LIKE ?
                  AND (? IS NULL OR u.created_at >= ?)
                ORDER BY u.id DESC LIMIT ?
            """, (pid, f"%{p['name']}%", since, since, PREP_MAX)))
            for r in rows:
                if r["id"] not in seen and pat.search(r["body"]):
                    r["source"] = "elsewhere"
                    elsewhere.append(r)

        updates = sorted(own + linked + elsewhere,
                         key=lambda r: (r["created_at"], r["id"]))
        return {
            "id": p["id"], "name": p["name"], "kind": p["kind"],
            "about": p["about"], "guidance": p["guidance"],
            "since": since,
            "updates": updates,
            "newest": max((r["id"] for r in updates), default=0),
            "total": len(updates),
            "counts": {"own": len(own), "linked": len(linked),
                       "elsewhere": len(elsewhere)},
        }
    finally:
        conn.close()


def prep(pid: int) -> dict:
    """The cached brief, and whether the store has moved under it."""
    conn = connect()
    try:
        r = conn.execute("SELECT body, since, through_update_id, read_updates,"
                         " created_at FROM preps WHERE project_id = ?",
                         (pid,)).fetchone()
        if not r:
            return {"built": False, "last_meeting": last_meeting(pid)}
        # Measured over the same three sources it read, so a brief does not go
        # stale because something unrelated was written somewhere else.
        now_ctx = prep_context(pid, r["since"])
        behind = sum(1 for u in now_ctx["updates"] if u["id"] > r["through_update_id"])
        return {
            "built": True,
            "body": r["body"],
            "since": r["since"],
            "created_at": r["created_at"],
            "behind": behind,
            "changed": now_ctx["total"] != r["read_updates"] and not behind,
            "stale": bool(behind or now_ctx["total"] != r["read_updates"]),
            "read": r["read_updates"],
            "last_meeting": last_meeting(pid),
        }
    finally:
        conn.close()


def save_prep(pid: int, body: str, since: str | None, through: int,
              read: int) -> dict:
    conn = connect()
    try:
        with conn:
            conn.execute("""
                INSERT INTO preps (project_id, body, since, through_update_id,
                                   read_updates, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    body = excluded.body, since = excluded.since,
                    through_update_id = excluded.through_update_id,
                    read_updates = excluded.read_updates,
                    created_at = excluded.created_at
            """, (pid, body, since, through, read, now()))
    finally:
        conn.close()
    return prep(pid)
