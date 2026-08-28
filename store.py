"""SQLite storage. Five tables, and only one of them holds anything you typed.

`updates.body` is the raw text, stored exactly as entered and never rewritten.
Everything else — summaries, answers — is derived, cached, and safe to throw
away: `DELETE FROM summaries` costs one Claude call to rebuild.

A project has topics; an update may sit in one of them or in none. Both halves
of that matter. Filing is optional so capture stays as fast as it was — being
made to choose a topic before you can type is exactly the friction this app
exists to avoid — and one topic rather than many keeps "what does this topic
say" a question with one answer.
"""

import datetime
import os
import pathlib
import sqlite3

DB_PATH = pathlib.Path(os.environ.get("BRAIN_DB") or
                       pathlib.Path(__file__).parent / "brain.db")

# projects and topics are created before anything else runs: with
# foreign_keys=ON, adding a column that REFERENCES topics — or renaming a table
# while such a column exists — fails outright if the target table is missing.
PRELUDE = """
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
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
    created_at        TEXT NOT NULL
);
-- NOT a PRIMARY KEY: SQLite permits NULLs in primary key columns, so
-- PRIMARY KEY (project_id, topic_id) would happily store two whole-project
-- summaries. IFNULL folds the NULL scope into one slot and makes it a real
-- constraint.
CREATE UNIQUE INDEX IF NOT EXISTS summaries_scope
    ON summaries(project_id, IFNULL(topic_id, 0));

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
"""


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

def projects() -> list[dict]:
    conn = connect()
    try:
        # Counted with subqueries, not two LEFT JOINs: joining both would
        # multiply the rows and report updates × topics for each.
        return _rows(conn.execute("""
            SELECT p.id, p.name, p.created_at,
                   (SELECT count(*) FROM updates u WHERE u.project_id = p.id)    AS updates,
                   (SELECT count(*) FROM topics  t WHERE t.project_id = p.id)    AS topics,
                   (SELECT max(created_at) FROM updates u WHERE u.project_id = p.id)
                       AS last_update_at
            FROM projects p
            ORDER BY last_update_at IS NULL, last_update_at DESC, p.name COLLATE NOCASE
        """))
    finally:
        conn.close()


def create_project(name: str) -> dict:
    name = name.strip()
    if not name:
        raise ValueError("a project needs a name")
    if len(name) > 120:
        raise ValueError("that name is too long")
    conn = connect()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO projects (name, created_at) VALUES (?, ?)",
                (name, now()))
        return {"id": cur.lastrowid, "name": name}
    except sqlite3.IntegrityError:
        raise ValueError(f"there is already a project called {name!r}")
    finally:
        conn.close()


def rename_project(pid: int, name: str) -> dict:
    name = name.strip()
    if not name:
        raise ValueError("a project needs a name")
    conn = connect()
    try:
        with conn:
            n = conn.execute("UPDATE projects SET name = ? WHERE id = ?",
                             (name, pid)).rowcount
        if not n:
            raise ValueError("no such project")
        return {"id": pid, "name": name}
    except sqlite3.IntegrityError:
        raise ValueError(f"there is already a project called {name!r}")
    finally:
        conn.close()


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
        p = conn.execute("SELECT id, name, created_at FROM projects WHERE id = ?",
                         (pid,)).fetchone()
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
        out["updates"] = _rows(conn.execute(
            "SELECT id, topic_id, body, created_at FROM updates"
            " WHERE project_id = ? ORDER BY id DESC", (pid,)))
        out["answers"] = _rows(conn.execute(
            "SELECT id, topic_id, question, answer, created_at FROM answers"
            " WHERE project_id = ? ORDER BY id DESC LIMIT 40", (pid,)))
        out["unfiled"] = sum(1 for u in out["updates"] if u["topic_id"] is None)

        # A scope's summary is stale against the newest update IN THAT SCOPE.
        # Filing something under Rollout must not mark the Vendor brief stale.
        newest = {"": 0}
        for t in out["topics"]:
            newest[str(t["id"])] = 0
        for u in out["updates"]:
            newest[""] = max(newest[""], u["id"])
            k = str(u["topic_id"])
            if k in newest:
                newest[k] = max(newest[k], u["id"])
        summaries = {}
        for s in conn.execute(
                "SELECT topic_id, body, through_update_id, created_at"
                " FROM summaries WHERE project_id = ?", (pid,)):
            k = "" if s["topic_id"] is None else str(s["topic_id"])
            d = dict(s)
            d["behind"] = sum(1 for u in out["updates"]
                              if u["id"] > s["through_update_id"]
                              and (k == "" or str(u["topic_id"]) == k))
            d["stale"] = newest.get(k, 0) > s["through_update_id"]
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

def add_update(pid: int, body: str, topic_id=None) -> dict:
    body = body.strip()
    if not body:
        raise ValueError("nothing to add")
    conn = connect()
    try:
        if not conn.execute("SELECT 1 FROM projects WHERE id = ?", (pid,)).fetchone():
            raise ValueError("no such project")
        tid = _own_topic(conn, pid, topic_id)
        ts = now()
        with conn:
            cur = conn.execute(
                "INSERT INTO updates (project_id, topic_id, body, created_at)"
                " VALUES (?,?,?,?)", (pid, tid, body, ts))
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

def save_summary(pid: int, body: str, through: int, topic_id=None) -> dict:
    ts = now()
    conn = connect()
    try:
        with conn:
            # The conflict target is the expression index, not the columns —
            # see summaries_scope for why the constraint is written that way.
            conn.execute(
                "INSERT INTO summaries (project_id, topic_id, body, through_update_id,"
                " created_at) VALUES (?,?,?,?,?)"
                " ON CONFLICT(project_id, IFNULL(topic_id, 0)) DO UPDATE SET"
                " body=excluded.body, through_update_id=excluded.through_update_id,"
                " created_at=excluded.created_at", (pid, topic_id, body, through, ts))
        return {"topic_id": topic_id, "body": body, "through_update_id": through,
                "created_at": ts, "stale": False, "behind": 0}
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
