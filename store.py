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

-- A priority you set by hand on one item of the pane.
--
-- Every item in this app is derived: the pane is rewritten wholesale on each
-- rebuild, and items are addressed by position, which is why acting on one
-- sends back the pane's timestamp. A priority pinned to a position would
-- therefore land on a different item after the next rebuild — silently, and
-- wrongly. So it is keyed by the page and the item's own words instead.
--
-- `key` is that text lowercased with its whitespace collapsed, and no more:
-- between rebuilds a model varies capitalisation and spacing far more often
-- than it rewords the sentence, and normalising harder would start folding two
-- genuinely different items on one page into one row. When the wording does
-- change, the priority is *lost* rather than misapplied — the failure that
-- shows itself, rather than the one that quietly reorders your day.
--
-- Orphans are kept. An item can drop out of one rebuild and come back in the
-- next, and a decision you made should come back with it. A row is a few bytes
-- and it is only ever read while an item it matches is on screen.
CREATE TABLE IF NOT EXISTS priorities (
    id         INTEGER PRIMARY KEY,
    -- NULL for an item Claude could not tie to a page. Nullable, so the
    -- uniqueness below folds it the way summaries folds a NULL topic.
    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    key        TEXT NOT NULL,
    rank       INTEGER NOT NULL,      -- 1 high, -1 low. 0 is not stored.
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS priorities_item
    ON priorities(IFNULL(project_id, 0), key);

-- A label you put on one item of the pane, in your own words.
--
-- Identical identity discipline to `priorities` above, and for the same
-- reason: an item is derived, rewritten wholesale on every rebuild, and
-- addressed by position. So a tag is keyed by the page and the item's own
-- words, and when the wording changes the tags are *lost* rather than landing
-- on whatever took that position. One row per tag rather than a list in a
-- column, so a filter is an index lookup and removing one tag is a DELETE.
--
-- The vocabulary is not fixed and is never suggested by a model. It is
-- whatever you have typed before, offered back to you — a tag a model invented
-- would vary between rebuilds, and a filter built on it would quietly mean
-- something different each morning.
CREATE TABLE IF NOT EXISTS tags (
    id         INTEGER PRIMARY KEY,
    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    key        TEXT NOT NULL,
    tag        TEXT NOT NULL,         -- lowercased, whitespace collapsed
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS tags_item
    ON tags(IFNULL(project_id, 0), key, tag);
CREATE INDEX IF NOT EXISTS tags_tag ON tags(tag);

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

-- Who the reader is. One row, like `agenda`, because there is one of you.
-- The fourth column in this database that a model may never write.
CREATE TABLE IF NOT EXISTS me (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    about      TEXT NOT NULL DEFAULT '',
    updated_at TEXT
);

-- A question asked of the whole notebook rather than of one page. It has no
-- project_id for the same reason `agenda` has one row: the scope is
-- everything, and that is the point of it. Kept, like the per-page answers,
-- because re-asking costs another Claude call.
CREATE TABLE IF NOT EXISTS store_answers (
    id                INTEGER PRIMARY KEY,
    question          TEXT NOT NULL,
    answer            TEXT NOT NULL,
    through_update_id INTEGER NOT NULL DEFAULT 0,
    read_updates      INTEGER NOT NULL DEFAULT 0,
    dropped           INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL
);

-- A question you keep, to check the store against.
--
-- Everything else derived in here is built when you ask for it and read once.
-- These are the opposite: a fixed set, asked again months later, so that the
-- answers can be compared. The number worth knowing is not any one answer but
-- how many of them the store could not answer at all — that list is what to go
-- and write down.
--
-- Seventh thing in this file that no model writes, after `updates.body`,
-- `me.about`, `projects.about`, `projects.guidance`, `priorities.rank` and
-- `tags.tag`. A
-- question Claude wrote would be one the store can already answer: a model
-- reading these notes and asked what to ask of them proposes what it has just
-- read. The whole value here is in the questions that do *not* come from the
-- notes, and only you know those.
CREATE TABLE IF NOT EXISTS checks (
    id         INTEGER PRIMARY KEY,
    question   TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- One pass over the set.
CREATE TABLE IF NOT EXISTS check_runs (
    id         INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at   TEXT
);

-- What one question came back with in one run, and what you made of it.
--
-- `mark` is the eighth thing no model writes, and it is the point of the
-- whole table. A model asked whether its own answer was any good grades its
-- own homework, and the one judgement that matters here -- *the store does not
-- know this* -- is exactly the one it is worst placed to make, because an
-- answer that hedges gracefully reads like an answer.
--
-- `asked` records whether this run spent a Claude call or reused an answer
-- that was still fresh. A review of a store that has not moved should be
-- nearly free, and the page says how much of it will be.
--
-- Both ends CASCADE. Deleting a question takes its marks out of every run it
-- was ever in, including finished ones -- which does rewrite history, and is
-- deliberate: a coverage number is only worth reading against a fixed set of
-- questions, and two runs scored against different sets cannot be compared at
-- all. The set is always the current set.
CREATE TABLE IF NOT EXISTS check_marks (
    run_id     INTEGER NOT NULL REFERENCES check_runs(id) ON DELETE CASCADE,
    check_id   INTEGER NOT NULL REFERENCES checks(id)     ON DELETE CASCADE,
    -- The answer this run read. ON DELETE SET NULL, because deleting an answer
    -- from the home view must not take the judgement you made about it.
    answer_id  INTEGER          REFERENCES store_answers(id) ON DELETE SET NULL,
    mark       TEXT,                  -- 'yes' | 'thin' | 'no'. NULL = unread.
    asked      INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, check_id)
);
"""


def today() -> str:
    """The local calendar date. The agenda is built for one specific day, and
    the pane compares this against the day it was built for."""
    return datetime.date.today().isoformat()


def now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def stamp(on=None) -> str:
    """The moment an update goes on the timeline. Almost always now().

    `on` is the day the thing actually happened, for the Wednesday you write up
    Monday's 1-1. It overwrites the date rather than sitting beside it in a
    second column, because there is one timeline here and a second timestamp
    would fork the meaning of "when" across every query and every prompt in the
    app — for the sake of a distinction, when you typed it as against when it
    happened, that no brief has ever needed.

    The clock time is kept rather than zeroed, so several notes backdated to
    the same day still order among themselves, and so a date never renders as a
    suspiciously round midnight.

    `id` is deliberately untouched. Every staleness watermark in the store asks
    "has anything been entered since this brief was written", which is a
    question about entry order — and entry order is exactly what an
    autoincrementing id is. Backdating must not make a brief look current.
    """
    if on in (None, ""):
        return now()
    try:
        d = datetime.date.fromisoformat(str(on).strip())
    except (ValueError, TypeError):
        raise ValueError("a date looks like 2026-09-04")
    t = datetime.datetime.now().astimezone()
    if d > t.date():
        # An update is a record of something that happened. A future-dated one
        # would sort above everything, tell `last_meeting` you have already had
        # the next conversation, and read as "today" in the list.
        raise ValueError("an update records what happened — today or earlier")
    if d == t.date():
        return now()
    return t.replace(year=d.year, month=d.month, day=d.day).isoformat(timespec="seconds")


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
    if _has(conn, "store_answers") and "dropped" not in _cols(conn, "store_answers"):
        conn.execute("ALTER TABLE store_answers ADD COLUMN dropped INTEGER NOT NULL"
                     " DEFAULT 0")
        done.append("store_answers.dropped")
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


def backup() -> dict:
    """A copy of the store, beside it, named for the minute it was taken.

    `VACUUM INTO` rather than copying the file: it runs inside a read
    transaction, so the copy is consistent even if the server is mid-write and
    the WAL has uncommitted pages in it. `cp brain.db elsewhere` does not
    promise that, and the one thing in here that cannot be rebuilt is the one
    thing a torn copy would lose.

    It refuses to overwrite — SQLite will not write into a file that exists —
    which also means a second click in the same minute is a no-op that says so
    rather than a silent replacement of the copy you meant to keep.
    """
    when = datetime.datetime.now().strftime("%Y-%m-%d-%H%M")
    dst = DB_PATH.with_name(f"{DB_PATH.stem}.{when}.bak")
    if dst.exists():
        return {"path": str(dst), "bytes": dst.stat().st_size, "made": False}
    conn = connect()
    try:
        # A parameter, not an f-string: the filename is data.
        conn.execute("VACUUM INTO ?", (str(dst),))
    finally:
        conn.close()
    return {"path": str(dst), "bytes": dst.stat().st_size, "made": True}


# A real personal-context file is a document, not a paragraph — the thing this
# emulates is an 841-line CLAUDE.md re-read every session, and the first person
# to paste one in lost three of its four parts to a 4,000-character cap that
# said nothing. Generous, because it competes with 200,000 characters of notes
# and loses; bounded, because it rides on top of every one of the seven calls.
ME_MAX = 20000


def me() -> dict:
    """Who the reader is, in their own words.

    `projects.about` says who a *page* is to you. Nothing said who *you* are,
    so every brief in the app was addressed to nobody in particular — which is
    exactly the thing that makes a brief for your manager and a brief for
    someone who reports to you come out identical.

    One row, like `agenda`: there is one reader. It reaches every prompt in the
    app, which is what makes it the highest-leverage text in the store and also
    what makes the rule about it non-negotiable — you write it, it is stored as
    typed, and no model ever writes it. A profile a model wrote and nobody read
    would quietly curate everything afterwards on the strength of a guess.
    """
    conn = connect()
    try:
        r = conn.execute("SELECT about, updated_at FROM me WHERE id = 1").fetchone()
        return {"about": (r["about"] if r else "") or "",
                "updated_at": r["updated_at"] if r else None}
    finally:
        conn.close()


def set_me(about: str) -> dict:
    """Save it, and bin everything that was written for whoever you used to be.

    Every interpretation in the store — the briefs, the preps, both kinds of
    pane — was composed against the old profile, and this is the invisible kind
    of staleness: a brief written for the wrong reader reads perfectly well. So
    it goes rather than being flagged, the same call `set_page_setup` makes for
    one page, made here for all of them.

    Answers stay, for the reason they always do: an answer quotes the raw text
    back at you rather than interpreting it, and it answered the question that
    was actually asked. Nothing touches the updates.
    """
    about = (about or "").strip()
    # Refused, never trimmed. Silently keeping the first 4,000 characters of
    # something you wrote is a partial write to text that cannot be
    # regenerated, and it looks exactly like a successful save.
    if len(about) > ME_MAX:
        raise ValueError(f"that is {len(about):,} characters and the limit is"
                         f" {ME_MAX:,} — nothing was saved")
    conn = connect()
    try:
        with conn:
            cur = conn.execute("SELECT about FROM me WHERE id = 1").fetchone()
            if (cur["about"] if cur else "") == about:
                return {"about": about, "changed": False, "cleared": {}}
            conn.execute(
                "INSERT INTO me (id, about, updated_at) VALUES (1, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET about = excluded.about,"
                " updated_at = excluded.updated_at", (about, now()))
            cleared = {}
            for t in ("summaries", "preps", "page_agenda", "agenda"):
                n = conn.execute(f"DELETE FROM {t}").rowcount
                if n:
                    cleared[t] = n
        return {"about": about, "changed": True, "cleared": cleared}
    finally:
        conn.close()


# How much of the newest update a card may show. Enough for a two-line clamp
# at the widest column, and nothing like enough to matter on the wire.
BLURB = 240


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
        # `about` and the opening of the newest update ride along because the
        # list is where you decide which page you meant, and a name with a
        # count beside it says nothing about the page — you had to open it to
        # find out. Both are raw user text, so neither can go stale and neither
        # is a model's opinion. Truncated here rather than in the browser: a
        # year of pasted transcripts would otherwise cross the wire to be
        # thrown away by a two-line clamp.
        return _rows(conn.execute("""
            SELECT p.id, p.name, p.kind, p.created_at, p.about,
                   (SELECT count(*) FROM updates u WHERE u.project_id = p.id)    AS updates,
                   (SELECT count(*) FROM topics  t WHERE t.project_id = p.id)    AS topics,
                   (SELECT max(created_at) FROM updates u WHERE u.project_id = p.id)
                       AS last_update_at,
                   (SELECT substr(u.body, 1, ?) FROM updates u
                     WHERE u.project_id = p.id
                     ORDER BY u.created_at DESC, u.id DESC LIMIT 1)              AS last_body
            FROM projects p
            ORDER BY last_update_at IS NULL, last_update_at DESC, p.name COLLATE NOCASE
        """, (BLURB,)))
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


def page_head(pid: int) -> dict:
    """Just a page's name and kind. `project()` reads its whole history, which
    is far more than a write needs when all it has to do is say where the note
    landed."""
    conn = connect()
    try:
        r = conn.execute("SELECT id, name, kind FROM projects WHERE id = ?",
                         (pid,)).fetchone()
        if not r:
            raise ValueError("no such project or person")
        return dict(r)
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
            " WHERE project_id = ? ORDER BY created_at DESC, id DESC", (pid,)))
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
            ORDER BY u.created_at DESC, u.id DESC
        """, (pid, pid)))
        for g in guests:
            g["via"] = {"id": g.pop("via_id"), "name": g.pop("via_name"),
                        "kind": g.pop("via_kind")}
            g["links"] = []
        # By date, then by entry order within a day. Backdating an update is
        # a claim about where it sits in the story, and the list is the story.
        out["updates"] = sorted(own + guests,
                                key=lambda u: (u["created_at"], u["id"]), reverse=True)
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

def add_update(pid: int, body: str, topic_id=None, links=None, on=None) -> dict:
    """`on` is the day it happened, when that is not today. See `stamp`."""
    body = body.strip()
    if not body:
        raise ValueError("nothing to add")
    ts = stamp(on)          # before the write, so a bad date costs nothing
    conn = connect()
    try:
        if not conn.execute("SELECT 1 FROM projects WHERE id = ?", (pid,)).fetchone():
            raise ValueError("no such project")
        tid = _own_topic(conn, pid, topic_id)
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


def rehome_update(uid: int, pid: int) -> dict:
    """Move an update to a different page. Its text is not touched.

    Filing something on the wrong page is a filing mistake, and the app already
    holds that filing mistakes must never cost raw text — which until now left
    delete-and-retype as the only answer, and that costs exactly the thing the
    rule protects. This is `move_update` one level up.

    Its topic goes, for the same reason `_own_topic` refuses one from another
    project: a topic id is only meaningful inside the project that owns it. It
    lands unfiled on its new page, which is where a thing you have just moved
    honestly belongs.

    Any link to the page it is moving to goes as well — an update cannot visit
    its own home, and a row saying otherwise would render it twice. Links to
    everywhere else are kept: they were about the update, not about where it
    lived.

    Nothing is cleared. Both pages' briefs recorded how many updates they read,
    and both counts have just moved, so both go visibly stale on their own —
    which is better than dropping them, because here there is something on the
    page to see.
    """
    conn = connect()
    try:
        row = conn.execute("SELECT project_id FROM updates WHERE id = ?",
                           (uid,)).fetchone()
        if not row:
            raise ValueError("no such update")
        was = conn.execute("SELECT name FROM projects WHERE id = ?",
                           (row["project_id"],)).fetchone()
        to = conn.execute("SELECT name, kind FROM projects WHERE id = ?", (pid,)).fetchone()
        if not to:
            raise ValueError("no such project or person")
        if row["project_id"] == pid:
            return {"id": uid, "project_id": pid, "moved": False,
                    "from": was["name"], "to": to["name"]}
        with conn:
            conn.execute("UPDATE updates SET project_id = ?, topic_id = NULL"
                         " WHERE id = ?", (pid, uid))
            conn.execute("DELETE FROM update_links WHERE update_id = ? AND project_id = ?",
                         (uid, pid))
        return {"id": uid, "project_id": pid, "moved": True,
                "from": was["name"], "to": to["name"], "kind": to["kind"]}
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
                WHERE u.project_id = ? ORDER BY u.created_at DESC, u.id DESC LIMIT ?
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


EVERYTHING_PER_PAGE = 150

# A second cap, on size rather than on count, because this read is the only one
# that sends every page at once and an update here is whatever you pasted — a
# line, or a whole transcript. Measured on a real store: 27 updates came to
# 160,000 characters, so a count alone is no guide to what will fit. Whatever
# does not fit is counted and printed, like every other cap in this app.
EVERYTHING_CHARS = 400_000


def everything_context(per_page: int = EVERYTHING_PER_PAGE,
                       chars: int = EVERYTHING_CHARS) -> dict:
    """Every page at once, in full, for the one read that crosses all of them.

    Its own function rather than the agenda's, because the two reads want
    different things. The agenda wants enough of each project to spot a date
    and has to stay cheap enough to rebuild on a whim. This one is handed a
    question and should answer it out of everything there is, so the budget per
    page is larger and each page's `about` comes with it — who a page is to you
    is most of what decides whether an answer about it is any use.

    Own updates only. A guest is the same row read from its other home, and
    sending it twice would let one sentence look like two people saying it.
    The link is named on the update instead: a connection you made by hand is
    worth more than one the model has to infer, and naming it costs a word.
    """
    conn = connect()
    try:
        pages, dropped = [], 0
        for p in conn.execute("SELECT id, name, kind, about FROM projects"
                              " ORDER BY name COLLATE NOCASE"):
            n = conn.execute("SELECT count(*) FROM updates WHERE project_id = ?",
                             (p["id"],)).fetchone()[0]
            rows = _rows(conn.execute("""
                SELECT u.id, u.body, u.created_at, t.name AS topic
                FROM updates u LEFT JOIN topics t ON t.id = u.topic_id
                WHERE u.project_id = ? ORDER BY u.created_at DESC, u.id DESC LIMIT ?
            """, (p["id"], per_page)))
            dropped += max(0, n - per_page)
            by_id = {u["id"]: u for u in rows}
            for u in rows:
                u["links"] = []
                u["size"] = len(u["body"])
            for r in conn.execute("""
                    SELECT l.update_id, q.name FROM update_links l
                    JOIN projects q ON q.id = l.project_id
                    WHERE l.update_id IN (SELECT id FROM updates WHERE project_id = ?)
                    ORDER BY q.name COLLATE NOCASE
            """, (p["id"],)):
                if r["update_id"] in by_id:
                    by_id[r["update_id"]]["links"].append(r["name"])
            pages.append({"id": p["id"], "name": p["name"], "kind": p["kind"],
                          "about": p["about"], "updates": rows})

        # Spent newest-first, one update per page per pass, so a page that
        # pastes transcripts cannot eat the budget before a quiet page has
        # given up its one line. When a page's next update will not fit, that
        # page stops there rather than skipping it for a shorter one further
        # back: a hole in the middle of a page's chronology is worse than a
        # shorter chronology, because the prompt reads each page as a story.
        spent, stopped, kept = 0, set(), {p["id"]: [] for p in pages}
        for i in range(max((len(p["updates"]) for p in pages), default=0)):
            for p in pages:
                if p["id"] in stopped or i >= len(p["updates"]):
                    continue
                u = p["updates"][i]
                if spent + u["size"] > chars:
                    stopped.add(p["id"])
                    continue
                spent += u["size"]
                kept[p["id"]].append(u)
        for p in pages:
            dropped += len(p["updates"]) - len(kept[p["id"]])
            # Oldest first, so the model reads each page in the order it
            # happened and "recently" is the end of the text.
            p["updates"] = list(reversed(kept[p["id"]]))
        return {
            "pages": pages,
            "dropped": dropped,
            "newest": conn.execute("SELECT ifnull(max(id), 0) FROM updates").fetchone()[0],
            "total": conn.execute("SELECT count(*) FROM updates").fetchone()[0],
        }
    finally:
        conn.close()


def save_store_answer(question: str, answer: str, through: int, read: int,
                      dropped: int = 0) -> dict:
    conn = connect()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO store_answers (question, answer, through_update_id,"
                " read_updates, dropped, created_at) VALUES (?,?,?,?,?,?)",
                (question.strip(), answer, through, read, dropped, now()))
        return {"id": cur.lastrowid}
    finally:
        conn.close()


def store_answers(limit: int = 20) -> list[dict]:
    """Newest first, each saying whether the store has moved under it.

    Same two axes as every other cached read here: the watermark notices the
    set growing, the count notices it changing. An answer that quoted an update
    you have since deleted should say so rather than reading as though it still
    holds.
    """
    conn = connect()
    try:
        newest = conn.execute("SELECT ifnull(max(id), 0) FROM updates").fetchone()[0]
        total = conn.execute("SELECT count(*) FROM updates").fetchone()[0]
        out = []
        for r in _rows(conn.execute(
                "SELECT id, question, answer, through_update_id, read_updates,"
                " dropped, created_at FROM store_answers ORDER BY id DESC LIMIT ?",
                (limit,))):
            behind = conn.execute(
                "SELECT count(*) FROM updates WHERE id > ?",
                (r["through_update_id"],)).fetchone()[0]
            r["behind"] = behind
            r["stale"] = bool(behind) or r["read_updates"] != total
            out.append(r)
        return out
    finally:
        conn.close()


def delete_store_answer(aid: int) -> dict:
    conn = connect()
    try:
        with conn:
            n = conn.execute("DELETE FROM store_answers WHERE id = ?", (aid,)).rowcount
        if not n:
            raise ValueError("no such answer")
        return {"deleted": aid}
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
                    "dropped": dropped, "per_project": AGENDA_PER_PROJECT,
                    "priorities": []}
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
            # Read here rather than from a route of their own so the pane and
            # the to-do list can never be looking at different marks.
            "priorities": _rows(conn.execute(
                "SELECT project_id, key, rank FROM priorities ORDER BY id")),
        }
    finally:
        conn.close()


def _pkey(text: str) -> str:
    """An item's identity: its own words, lowercased, whitespace collapsed.

    Deliberately no cleverer than that — see the note above `priorities` in
    SCHEMA for why a looser key would be worse than a lost priority.
    """
    return " ".join((text or "").split()).lower()


def priorities() -> list[dict]:
    """Every priority you have set, for the pane and the list to join against.

    Sent whole rather than looked up per item: there are as many of these as
    you have bothered to set, and a page that draws two hundred items should
    not make two hundred queries to draw them.
    """
    conn = connect()
    try:
        return _rows(conn.execute(
            "SELECT project_id, key, rank FROM priorities ORDER BY id"))
    finally:
        conn.close()


def set_priority(project_id, text: str, rank: int) -> dict:
    """Raise, lower or unset one item. Writes nothing to the log.

    Unlike `done`, `note` and `date` — which record something you did and
    belong in the raw text like any other update — this records only how you
    want the list ordered. It is about the reading, not about the work, so it
    stays out of the one thing that cannot be regenerated.
    """
    key = _pkey(text)
    if not key:
        raise ValueError("an item needs text to carry a priority")
    rank = int(rank or 0)
    if rank not in (-1, 0, 1):
        raise ValueError("a priority is high, low, or neither")
    pid = int(project_id) if project_id else None
    conn = connect()
    try:
        with conn:
            if pid is not None and not conn.execute(
                    "SELECT 1 FROM projects WHERE id = ?", (pid,)).fetchone():
                raise ValueError("no such page")
            # Unsetting deletes rather than storing a zero: absent and
            # "explicitly normal" are the same thing, and one of them would
            # otherwise accumulate a row per item you ever touched.
            if rank == 0:
                conn.execute("DELETE FROM priorities WHERE"
                             " IFNULL(project_id, 0) = IFNULL(?, 0) AND key = ?",
                             (pid, key))
            else:
                conn.execute(
                    "INSERT INTO priorities (project_id, key, rank, created_at)"
                    " VALUES (?, ?, ?, ?)"
                    " ON CONFLICT(IFNULL(project_id, 0), key) DO UPDATE SET"
                    " rank = excluded.rank, created_at = excluded.created_at",
                    (pid, key, rank, now()))
        return {"project_id": pid, "key": key, "rank": rank}
    finally:
        conn.close()


TAG_MAX = 24          # a label, not a sentence
TAGS_PER_ITEM = 8


def _tkey(tag: str) -> str:
    """A tag's identity: lowercased, whitespace collapsed, no leading #.

    So `Waiting`, `waiting` and `#waiting` are one tag rather than three
    filters that each show a third of what you meant.
    """
    return " ".join((tag or "").replace("#", " ").split()).lower()


def tags() -> list[dict]:
    """Every tag you have put on an item, for the list to join against.

    Sent whole for the reason `priorities` is: there are as many as you have
    bothered to set, and drawing two hundred items should not cost two hundred
    queries.
    """
    conn = connect()
    try:
        return _rows(conn.execute(
            "SELECT project_id, key, tag FROM tags ORDER BY tag, id"))
    finally:
        conn.close()


def set_tag(project_id, text: str, tag: str, on: bool = True) -> dict:
    """Put one label on an item, or take it off. Writes nothing to the log.

    Like `set_priority`, this records how you want the list read rather than
    work you did, so it stays out of the one thing that cannot be regenerated.
    """
    key = _pkey(text)
    if not key:
        raise ValueError("an item needs text to carry a tag")
    t = _tkey(tag)
    if not t:
        raise ValueError("a tag needs a word")
    if len(t) > TAG_MAX:
        raise ValueError(f"a tag is a label, not a sentence — {TAG_MAX}"
                         " characters at most")
    pid = int(project_id) if project_id else None
    conn = connect()
    try:
        with conn:
            if pid is not None and not conn.execute(
                    "SELECT 1 FROM projects WHERE id = ?", (pid,)).fetchone():
                raise ValueError("no such page")
            if not on:
                conn.execute("DELETE FROM tags WHERE"
                             " IFNULL(project_id, 0) = IFNULL(?, 0)"
                             " AND key = ? AND tag = ?", (pid, key, t))
                return {"project_id": pid, "key": key, "tag": t, "on": False}
            # A cap per item, because a row wearing a dozen labels has stopped
            # being filterable and the list is what this exists to narrow.
            n = conn.execute("SELECT COUNT(*) FROM tags WHERE"
                             " IFNULL(project_id, 0) = IFNULL(?, 0) AND key = ?"
                             " AND tag <> ?", (pid, key, t)).fetchone()[0]
            if n >= TAGS_PER_ITEM:
                raise ValueError(f"that item already has {TAGS_PER_ITEM} tags")
            conn.execute(
                "INSERT INTO tags (project_id, key, tag, created_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(IFNULL(project_id, 0), key, tag) DO NOTHING",
                (pid, key, t, now()))
        return {"project_id": pid, "key": key, "tag": t, "on": True}
    finally:
        conn.close()


def rename_tag(old: str, new: str) -> dict:
    """Rename one tag everywhere, or delete it everywhere when `new` is empty.

    A vocabulary you grow by typing acquires near-duplicates — `waiting` and
    `waiting on`— and without this the only cure is finding every item that
    wears one. Merging into a tag an item already has is not a conflict: the
    row is simply dropped, because the item ends up wearing the tag either way.
    """
    a = _tkey(old)
    if not a:
        raise ValueError("which tag?")
    b = _tkey(new)
    if len(b) > TAG_MAX:
        raise ValueError(f"a tag is a label, not a sentence — {TAG_MAX}"
                         " characters at most")
    conn = connect()
    try:
        with conn:
            n = conn.execute("SELECT COUNT(*) FROM tags WHERE tag = ?",
                             (a,)).fetchone()[0]
            if not b:
                conn.execute("DELETE FROM tags WHERE tag = ?", (a,))
                return {"tag": a, "to": None, "items": n}
            if b != a:
                # Anything that would collide has the destination already.
                conn.execute(
                    "DELETE FROM tags WHERE tag = ? AND EXISTS ("
                    "  SELECT 1 FROM tags o WHERE o.tag = ?"
                    "  AND IFNULL(o.project_id, 0) = IFNULL(tags.project_id, 0)"
                    "  AND o.key = tags.key)", (a, b))
                conn.execute("UPDATE tags SET tag = ? WHERE tag = ?", (b, a))
        return {"tag": a, "to": b, "items": n}
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
            ORDER BY u.created_at DESC, u.id DESC LIMIT :n
        """, {"p": pid, "n": PAGE_AGENDA_MAX}))
        for r in rows:
            home = r.pop("via_id"), r.pop("via_name"), r.pop("via_kind")
            r["via"] = None if home[0] == pid else {
                "id": home[0], "name": home[1], "kind": home[2]}
        ids = _page_scope(conn, pid)
        total = len(ids)
        return {
            "id": p["id"], "name": p["name"], "kind": p["kind"],
            "about": p["about"],
            "updates": list(reversed(rows)),
            # The newest id in SCOPE, not the newest one read. Rows come back
            # by date now, so a backdated update can carry the highest id and
            # still fall outside the cap — and a pane whose watermark could
            # never reach the store's would report itself behind forever.
            "newest": max(ids, default=0),
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


def all_page_agendas() -> list[dict]:
    """Every page's own pane, in one read, for the to-do list.

    The to-do list is built out of these rather than out of the cross-project
    pane, and the difference is not small: measured on a real store, eleven
    page panes held **172 items where the Now pane held 20**. That pane caps
    each project at 40 updates and then keeps the most urgent items across the
    whole store — it is a digest of what is soon, which is the right answer to
    "what is happening" and the wrong one to "what do I owe". A page's pane is
    asked to read one page exhaustively, and does.

    Only pages that have something on them. A page with no updates owes
    nothing, and listing it as "not read yet" would send you to build a pane
    over an empty page.
    """
    conn = connect()
    try:
        pages = _rows(conn.execute(
            "SELECT p.id, p.name, p.kind FROM projects p"
            " WHERE EXISTS (SELECT 1 FROM updates u WHERE u.project_id = p.id)"
            "    OR EXISTS (SELECT 1 FROM update_links l WHERE l.project_id = p.id)"
            " ORDER BY p.name COLLATE NOCASE"))
        built = {r["project_id"]: r for r in _rows(conn.execute(
            "SELECT project_id, body, for_date, through_update_id, read_updates,"
            " created_at FROM page_agenda"))}
        day = today()
        out = []
        for pg in pages:
            ids = _page_scope(conn, pg["id"])
            total = len(ids)
            r = built.get(pg["id"])
            if not r:
                out.append({**pg, "built": False, "items": [], "updates": total,
                            "dropped": max(0, total - PAGE_AGENDA_MAX)})
                continue
            behind = sum(1 for i in ids if i > r["through_update_id"])
            changed = total != r["read_updates"] and not behind
            out.append({
                **pg, "built": True, "items": json.loads(r["body"]),
                "created_at": r["created_at"], "for_date": r["for_date"],
                "behind": behind, "outdated": r["for_date"] != day,
                "changed": changed,
                "stale": bool(behind or r["for_date"] != day or changed),
                "updates": total, "dropped": max(0, total - PAGE_AGENDA_MAX),
            })
        return out
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
            WHERE u.project_id = ? ORDER BY u.created_at DESC, u.id DESC LIMIT ?
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
            ORDER BY u.created_at DESC, u.id DESC LIMIT ?
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
                ORDER BY u.created_at DESC, u.id DESC LIMIT ?
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


# -------------------------------------------------------------------- checks
# A fixed set of questions, asked of everything, and scored by hand.
#
# Every other read in this app is one you wanted the answer to. These are asked
# because the *answers* are not the point: what you learn from running thirty
# of them is which ones came back empty, and that list is a list of things to
# go and write down. So the interesting number is the one nothing else here
# reports — how much of what you need to know is not in the store at all.

MARKS = ("yes", "thin", "no")


def checks() -> list[dict]:
    conn = connect()
    try:
        return _rows(conn.execute(
            "SELECT id, question, created_at FROM checks ORDER BY id"))
    finally:
        conn.close()


def add_check(question: str) -> dict:
    """One question. Stored as typed, like every other thing you write here."""
    q = (question or "").strip()
    if not q:
        raise ValueError("write a question")
    if len(q) > 400:
        raise ValueError("that is longer than a question")
    conn = connect()
    try:
        dup = conn.execute("SELECT id FROM checks WHERE lower(question) = ?",
                           (q.lower(),)).fetchone()
        if dup:
            raise ValueError("you already ask that one")
        with conn:
            cur = conn.execute(
                "INSERT INTO checks (question, created_at) VALUES (?,?)",
                (q, now()))
        return {"id": cur.lastrowid, "question": q}
    finally:
        conn.close()


def delete_check(cid: int) -> dict:
    """And its marks, in every run — see the note on `check_marks`."""
    conn = connect()
    try:
        n = conn.execute("SELECT count(*) FROM check_marks WHERE check_id = ?",
                         (cid,)).fetchone()[0]
        with conn:
            gone = conn.execute("DELETE FROM checks WHERE id = ?", (cid,)).rowcount
        if not gone:
            raise ValueError("no such question")
        return {"deleted": cid, "marks": n}
    finally:
        conn.close()


def _fresh(conn, question: str, newest: int, total: int):
    """The newest whole-store answer to exactly this question, if the store has
    not moved under it.

    Same two axes as `store_answers`: the watermark notices the set growing,
    the count notices it changing. An answer that is still fresh is one this
    run does not have to spend a Claude call on — which is what makes the
    second run of a week cheap and the first run after a busy month expensive,
    in proportion to how much there is that is new to read.
    """
    r = conn.execute(
        "SELECT id, answer, through_update_id, read_updates, created_at"
        " FROM store_answers WHERE question = ? ORDER BY id DESC LIMIT 1",
        (question.strip(),)).fetchone()
    if not r:
        return None
    if r["through_update_id"] != newest or r["read_updates"] != total:
        return None
    return dict(r)


def fresh_answer(question: str):
    """A still-current whole-store answer to this exact question, or None.

    The one thing a run needs to know before it decides to spend a call.
    """
    conn = connect()
    try:
        newest = conn.execute("SELECT ifnull(max(id), 0) FROM updates").fetchone()[0]
        total = conn.execute("SELECT count(*) FROM updates").fetchone()[0]
        return _fresh(conn, question, newest, total)
    finally:
        conn.close()


def _run_row(conn, r: dict) -> dict:
    """One run, scored. The denominator is what it actually asked, not the size
    of today's set — a question added since simply was not in it."""
    counts = {m: 0 for m in MARKS}
    unmarked = 0
    for m in conn.execute(
            "SELECT mark FROM check_marks WHERE run_id = ?", (r["id"],)):
        if m["mark"] in counts:
            counts[m["mark"]] += 1
        else:
            unmarked += 1
    r = dict(r)
    r.update(counts)
    r["unmarked"] = unmarked
    r["n"] = sum(counts.values()) + unmarked
    return r


def check_page(runs: int = 8) -> dict:
    """Everything the page needs, in one read and no Claude call.

    The latest run is the one you are looking at: each question carries its
    mark *in that run* and the answer that run read. Earlier runs come back as
    scores only — the trend is a row of numbers, and the answers behind an old
    one are still in `store_answers` where every other answer lives.
    """
    conn = connect()
    try:
        newest = conn.execute("SELECT ifnull(max(id), 0) FROM updates").fetchone()[0]
        total = conn.execute("SELECT count(*) FROM updates").fetchone()[0]
        past = _rows(conn.execute(
            "SELECT id, started_at, ended_at FROM check_runs ORDER BY id DESC"
            " LIMIT ?", (runs,)))
        scored = [_run_row(conn, r) for r in past]
        run = scored[0] if scored else None
        marks = {}
        if run:
            for m in _rows(conn.execute(
                    "SELECT check_id, answer_id, mark, asked FROM check_marks"
                    " WHERE run_id = ?", (run["id"],))):
                marks[m["check_id"]] = m
        out, needs = [], 0
        for c in _rows(conn.execute(
                "SELECT id, question, created_at FROM checks ORDER BY id")):
            fresh = _fresh(conn, c["question"], newest, total)
            c["fresh"] = bool(fresh)
            if not fresh:
                needs += 1
            m = marks.get(c["id"])
            c["mark"] = m["mark"] if m else None
            c["asked"] = bool(m["asked"]) if m else False
            c["done"] = bool(m)
            # The answer this run read, or — before a run has reached it — the
            # cached one it would reuse. Both are the same row from the same
            # table; which of them you are looking at is `done`.
            a = None
            if m and m["answer_id"]:
                a = conn.execute(
                    "SELECT id, answer, created_at FROM store_answers WHERE id = ?",
                    (m["answer_id"],)).fetchone()
                a = dict(a) if a else None
            c["answer"] = a or (fresh if not m else None)
            out.append(c)
        return {"checks": out, "runs": scored, "run": run,
                "needs": needs, "fresh": len(out) - needs,
                "pending": [c["id"] for c in out if not c["done"]]}
    finally:
        conn.close()


def start_run() -> dict:
    """A new pass. Nothing is asked here — the client walks the questions one
    at a time so it can name the one it is on and stop when you leave."""
    conn = connect()
    try:
        n = conn.execute("SELECT count(*) FROM checks").fetchone()[0]
        if not n:
            raise ValueError("no questions to ask yet")
        with conn:
            cur = conn.execute(
                "INSERT INTO check_runs (started_at) VALUES (?)", (now(),))
        return {"id": cur.lastrowid, "n": n}
    finally:
        conn.close()


def pending(run_id: int) -> list[dict]:
    """The questions this run has not reached. Read fresh each time, so a run
    you left half-finished this morning resumes rather than starting over."""
    conn = connect()
    try:
        if not conn.execute("SELECT 1 FROM check_runs WHERE id = ?",
                            (run_id,)).fetchone():
            raise ValueError("no such run")
        return _rows(conn.execute(
            "SELECT id, question FROM checks WHERE id NOT IN"
            " (SELECT check_id FROM check_marks WHERE run_id = ?) ORDER BY id",
            (run_id,)))
    finally:
        conn.close()


def record(run_id: int, check_id: int, answer_id, asked: bool) -> dict:
    """This run has now read this question. The mark itself stays NULL: that is
    yours, and it is made after you have read the answer."""
    conn = connect()
    try:
        with conn:
            conn.execute(
                "INSERT INTO check_marks (run_id, check_id, answer_id, mark,"
                " asked, created_at) VALUES (?,?,?,NULL,?,?)"
                " ON CONFLICT(run_id, check_id) DO UPDATE SET"
                " answer_id = excluded.answer_id, asked = excluded.asked",
                (run_id, check_id, answer_id, 1 if asked else 0, now()))
        return {"run_id": run_id, "check_id": check_id, "answer_id": answer_id,
                "asked": bool(asked)}
    finally:
        conn.close()


def end_run(run_id: int) -> dict:
    conn = connect()
    try:
        with conn:
            conn.execute("UPDATE check_runs SET ended_at = ? WHERE id = ?"
                         " AND ended_at IS NULL", (now(), run_id))
        return {"id": run_id}
    finally:
        conn.close()


def set_mark(run_id: int, check_id: int, mark) -> dict:
    """Your judgement of one answer. Three values and nothing else — the
    question is whether the store *held* this, not how well it was written."""
    if mark is not None and mark not in MARKS:
        raise ValueError("a mark is yes, thin or no")
    conn = connect()
    try:
        with conn:
            n = conn.execute(
                "UPDATE check_marks SET mark = ? WHERE run_id = ? AND check_id = ?",
                (mark, run_id, check_id)).rowcount
        if not n:
            raise ValueError("that run has not read that question yet")
        return {"run_id": run_id, "check_id": check_id, "mark": mark}
    finally:
        conn.close()


def delete_run(run_id: int) -> dict:
    conn = connect()
    try:
        with conn:
            n = conn.execute("DELETE FROM check_runs WHERE id = ?",
                             (run_id,)).rowcount
        if not n:
            raise ValueError("no such run")
        return {"deleted": run_id}
    finally:
        conn.close()


# ------------------------------------------------------------------ catch-up
# What has already been built, and what the store has moved under since.
#
# Everything derived here is a click away from being rebuilt, and *Now* has
# always made exactly one exception: a pane that says "today" is a lie the next
# morning, so the day turning over rebuilds it once, unasked. This is that
# exception widened from the calendar to the updates, and the argument for it
# is a measurement rather than a preference — a store holding 48 updates and
# 207,430 characters across 37 pages held **five** derived objects: no summary
# at all, no meeting brief at all, four page panes and the Now pane. Every one
# of them cost a click at the moment you were busy writing something down, so
# capture ran and interpretation simply never did.
#
# What keeps it honest is that it can only **refresh**, never **build**. A
# brief you never asked for is not out of date, it is absent, and asking for
# one is a deliberate first act — the same rule the calendar catch-up already
# holds, for the same reason. So this can never widen the set of things the app
# spends a call on; it can only bring the set you already chose back up to what
# your updates now say. Building the first one stays a button.

def _summary_scope(conn, pid: int, tid) -> tuple[int, int]:
    """The newest id and the count in a summary's scope — the same two numbers
    the page reader compares a cached brief against, and they must stay the
    same two or a brief would be called stale in one place and fresh in the
    other. A topic sees only its own page's updates; a guest belongs to no
    topic, which is why it counts towards the project scope and no other."""
    if tid is None:
        ids = _page_scope(conn, pid)
    else:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM updates WHERE project_id = ? AND topic_id = ?",
            (pid, tid))]
    return (max(ids) if ids else 0), len(ids)


def behind() -> dict:
    """Every built-and-stale cache, oldest first, with the Now pane ahead of
    all of them because it is the one thing that claims to be about today.

    `newest` is the newest update id in the store. The caller watches it to
    decide when the store has gone quiet: catching up in the middle of you
    entering a run of notes would spend a call on a picture you are still
    halfway through changing.
    """
    conn = connect()
    try:
        newest = conn.execute(
            "SELECT ifnull(max(id), 0) FROM updates").fetchone()[0]
        total = conn.execute("SELECT count(*) FROM updates").fetchone()[0]
        names = {r["id"]: r["name"] for r in _rows(conn.execute(
            "SELECT id, name FROM projects"))}
        tnames = {r["id"]: r["name"] for r in _rows(conn.execute(
            "SELECT id, name FROM topics"))}
        summaries = _rows(conn.execute(
            "SELECT project_id, topic_id, through_update_id, read_updates,"
            " created_at FROM summaries"))
        preps = [r[0] for r in conn.execute("SELECT project_id FROM preps")]
    finally:
        conn.close()

    jobs = []
    ag = agenda()
    if ag["built"] and ag["stale"]:
        jobs.append({"kind": "now", "project_id": None, "topic_id": None,
                     "name": "the Now pane", "behind": ag["behind"],
                     "created_at": ag["created_at"]})
    for pg in all_page_agendas():
        if pg["built"] and pg["stale"]:
            jobs.append({"kind": "page", "project_id": pg["id"],
                         "topic_id": None, "name": f"{pg['name']} — dates",
                         "behind": pg["behind"], "created_at": pg["created_at"]})

    conn = connect()
    try:
        for s in summaries:
            top, count = _summary_scope(conn, s["project_id"], s["topic_id"])
            if top <= s["through_update_id"] and count == s["read_updates"]:
                continue
            where = names.get(s["project_id"], "?")
            if s["topic_id"] is not None:
                where += f" — {tnames.get(s['topic_id'], '?')}"
            jobs.append({"kind": "summary", "project_id": s["project_id"],
                         "topic_id": s["topic_id"], "name": f"a brief on {where}",
                         "behind": max(0, count - s["read_updates"]),
                         "created_at": s["created_at"]})
    finally:
        conn.close()

    for pid in preps:
        pr = prep(pid)
        if pr["built"] and pr["stale"]:
            jobs.append({"kind": "prep", "project_id": pid,
                         "topic_id": None, "since": pr["since"],
                         "name": f"the {names.get(pid, '?')} meeting brief",
                         "behind": pr["behind"], "created_at": pr["created_at"]})

    # Oldest cache first, so the thing that has been wrong longest is put right
    # first — but the Now pane ahead of everything, because it is the only one
    # of these that is read before you have decided what you are looking for.
    jobs.sort(key=lambda j: (j["kind"] != "now", j["created_at"]))
    return {"newest": newest, "total": total, "jobs": jobs}
