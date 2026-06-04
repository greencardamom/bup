# -*- coding: utf-8 -*-
#
# SQLite data layer for bup. Replaces the old out.json / out.json.all JSONL
# files and the awk+jq+grep+sed access pattern.
#
# Two tables:
#   pages    - the working list (imported from out.json). Its INTEGER PRIMARY
#              KEY `id` is assigned in file order, so it is stable and is what
#              the /preview/<id> and /runbot/<id> routes reference.
#   archive  - the older full archive (imported from out.json.all), keyed by
#              page title, used by the on-demand route to run the bot against
#              any precomputed page.
#
# Citations are stored as a JSON text blob per row since they are always
# consumed together for a single page.

import os
import json
import sqlite3

# DB path is configurable so the same code runs in the local checkout and in
# production (/data/project/bup/www/db). Order of precedence:
#   1. BUP_DB_PATH environment variable
#   2. <this file's dir>/../../db/bup.db
__dir__ = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_PATH = os.path.normpath(
    os.path.join(__dir__, "..", "..", "db", "bup.db"))


def db_path():
    return os.environ.get("BUP_DB_PATH", DEFAULT_DB_PATH)


SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    page       TEXT    NOT NULL,
    count      INTEGER NOT NULL DEFAULT 0,
    ref_count  INTEGER NOT NULL DEFAULT 0,
    sim_count  INTEGER NOT NULL DEFAULT 0,
    book_count INTEGER NOT NULL DEFAULT 0,
    done       INTEGER NOT NULL DEFAULT 0,
    citations  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pages_done ON pages(done, id);
CREATE INDEX IF NOT EXISTS idx_pages_page ON pages(page);

CREATE TABLE IF NOT EXISTS archive (
    page       TEXT    PRIMARY KEY,
    count      INTEGER NOT NULL DEFAULT 0,
    ref_count  INTEGER NOT NULL DEFAULT 0,
    sim_count  INTEGER NOT NULL DEFAULT 0,
    book_count INTEGER NOT NULL DEFAULT 0,
    done       INTEGER NOT NULL DEFAULT 0,
    citations  TEXT    NOT NULL
);
"""


def connect(path=None):
    """Open a connection with sane defaults (WAL, row factory, FK on)."""
    conn = sqlite3.connect(path or db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()


# --- Read helpers ---------------------------------------------------------

def worklist(conn, limit=75):
    """The first `limit` not-done pages, in stable id order (replaces loadT)."""
    cur = conn.execute(
        "SELECT id, page, count, ref_count, sim_count, book_count "
        "FROM pages WHERE done = 0 ORDER BY id LIMIT ?", (limit,))
    return [dict(r) for r in cur.fetchall()]


def get_page(conn, page_id):
    """Fetch one worklist row by stable id, with citations parsed."""
    cur = conn.execute(
        "SELECT id, page, count, ref_count, sim_count, book_count, done, "
        "citations FROM pages WHERE id = ?", (page_id,))
    row = cur.fetchone()
    return _row_with_citations(row)


def get_archive_page(conn, page_title):
    """Fetch one archive row by title (replaces the out.json.all grep)."""
    cur = conn.execute(
        "SELECT page, count, ref_count, sim_count, book_count, done, "
        "citations FROM archive WHERE page = ?", (page_title,))
    row = cur.fetchone()
    return _row_with_citations(row)


def _row_with_citations(row):
    if row is None:
        return None
    d = dict(row)
    d["citations"] = json.loads(d["citations"])
    return d


# --- Write helpers --------------------------------------------------------

def mark_done(conn, page_id):
    """Flip the done flag for one worklist row (replaces the jq full rewrite)."""
    conn.execute("UPDATE pages SET done = 1 WHERE id = ?", (page_id,))
    conn.commit()


def mark_done_by_title(conn, page_title):
    conn.execute("UPDATE pages SET done = 1 WHERE page = ?", (page_title,))
    conn.commit()
