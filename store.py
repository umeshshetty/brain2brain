"""SQLite storage. Four tables, and only one of them holds anything you typed.

`updates.body` is the raw text, stored exactly as entered and never rewritten.
Everything else — summaries, answers — is derived, cached, and safe to throw
away: `DELETE FROM summaries` costs one Claude call to rebuild.
"""

import datetime
import os
import pathlib
import sqlite3

DB_PATH = pathlib.Path(os.environ.get("BRAIN_DB") or
                       pathlib.Path(__file__).parent / "brain.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS updates (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS updates_by_project ON updates(project_id, id DESC);

-- One cached summary per project. `through_update_id` is the newest update it
-- was built from, which is how the page knows it has gone stale without
-- re-running Claude to find out.
CREATE TABLE IF NOT EXISTS summaries (
    project_id        INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    body              TEXT NOT NULL,
    through_update_id INTEGER NOT NULL,
    created_at        TEXT NOT NULL
);

-- Questions are kept because the answer to "why did we pick Postgres" is worth
-- as much the second time, and re-asking costs another Claude call.
CREATE TABLE IF NOT EXISTS answers (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
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


def init() -> None:
    conn = connect()
    try:
        with conn:
            conn.executescript(SCHEMA)
    finally:
        conn.close()


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


# ------------------------------------------------------------------ projects

def projects() -> list[dict]:
    conn = connect()
    try:
        return _rows(conn.execute("""
            SELECT p.id, p.name, p.created_at,
                   count(u.id)      AS updates,
                   max(u.created_at) AS last_update_at
            FROM projects p
            LEFT JOIN updates u ON u.project_id = p.id
            GROUP BY p.id
            ORDER BY last_update_at IS NULL, last_update_at DESC, p.name
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
    conn = connect()
    try:
        p = conn.execute("SELECT id, name, created_at FROM projects WHERE id = ?",
                         (pid,)).fetchone()
        if not p:
            raise ValueError("no such project")
        out = dict(p)
        out["updates"] = _rows(conn.execute(
            "SELECT id, body, created_at FROM updates"
            " WHERE project_id = ? ORDER BY id DESC", (pid,)))
        out["answers"] = _rows(conn.execute(
            "SELECT id, question, answer, created_at FROM answers"
            " WHERE project_id = ? ORDER BY id DESC LIMIT 20", (pid,)))
        s = conn.execute(
            "SELECT body, through_update_id, created_at FROM summaries"
            " WHERE project_id = ?", (pid,)).fetchone()
        if s:
            newest = out["updates"][0]["id"] if out["updates"] else 0
            out["summary"] = dict(s)
            # How many updates landed after the summary was written. The page
            # shows this instead of silently serving a stale brief.
            out["summary"]["behind"] = sum(
                1 for u in out["updates"] if u["id"] > s["through_update_id"])
            out["summary"]["stale"] = newest > s["through_update_id"]
        else:
            out["summary"] = None
        return out
    finally:
        conn.close()


# ------------------------------------------------------------------- updates

def add_update(pid: int, body: str) -> dict:
    body = body.strip()
    if not body:
        raise ValueError("nothing to add")
    conn = connect()
    try:
        if not conn.execute("SELECT 1 FROM projects WHERE id = ?", (pid,)).fetchone():
            raise ValueError("no such project")
        ts = now()
        with conn:
            cur = conn.execute(
                "INSERT INTO updates (project_id, body, created_at) VALUES (?,?,?)",
                (pid, body, ts))
        return {"id": cur.lastrowid, "body": body, "created_at": ts}
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

def save_summary(pid: int, body: str, through: int) -> dict:
    ts = now()
    conn = connect()
    try:
        with conn:
            conn.execute(
                "INSERT INTO summaries (project_id, body, through_update_id, created_at)"
                " VALUES (?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET"
                " body=excluded.body, through_update_id=excluded.through_update_id,"
                " created_at=excluded.created_at", (pid, body, through, ts))
        return {"body": body, "through_update_id": through, "created_at": ts,
                "stale": False, "behind": 0}
    finally:
        conn.close()


def save_answer(pid: int, question: str, answer: str) -> dict:
    ts = now()
    conn = connect()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO answers (project_id, question, answer, created_at)"
                " VALUES (?,?,?,?)", (pid, question, answer, ts))
        return {"id": cur.lastrowid, "question": question, "answer": answer,
                "created_at": ts}
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
