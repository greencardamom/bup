# -*- coding: utf-8 -*-
#
# SQLite data layer for bup. Replaces the old out.json JSONL file and the
# awk+jq+grep+sed access pattern.
#
# One table:
#   pages - the working list (imported from out.json). Its INTEGER PRIMARY KEY
#           `id` is assigned in file order, so it is stable and is what the
#           /preview/<id> and /runbot/<id> routes reference. The on-demand and
#           API routes look pages up by title.
#
# There is no `done` column: a citation that has been applied (or whose oldcite
# no longer matches the live article) is PRUNED from the row, and the page is
# deleted when its last citation goes. So every row present is open work. See
# reconcile.py (who prunes) and verify.py (the daily reconciler).
#
# Citations are stored as a JSON text blob per row since they are always
# consumed together for a single page.

import os
import json
import sqlite3

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
    revid      INTEGER NOT NULL DEFAULT 0,
    citations  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pages_page ON pages(page);
"""
# `revid` is the article's lastrevid as of the last verify; 0 = never verified.
# verify.py uses it to skip pages that haven't been edited since last checked.


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


# --- Citation typing (mirrors makejson.awk) -------------------------------

def citation_type(c):
    """'ref' (refactor), 'sim' (journal), or 'book', from the iaid field."""
    iaid = c.get("iaid", "")
    if "Type" in iaid:
        return "ref"
    if iaid.startswith("sim_"):
        return "sim"
    return "book"


def _counts(citations):
    book = sim = ref = 0
    for c in citations:
        t = citation_type(c)
        if t == "ref":
            ref += 1
        elif t == "sim":
            sim += 1
        else:
            book += 1
    return book, sim, ref


# --- Read helpers ---------------------------------------------------------

def worklist(conn, limit=75):
    """The first `limit` pages in stable id order (every row is open work)."""
    cur = conn.execute(
        "SELECT id, page, count, ref_count, sim_count, book_count "
        "FROM pages ORDER BY id LIMIT ?", (limit,))
    return [dict(r) for r in cur.fetchall()]


def worklist_page(conn, limit=50, offset=0, min_count=0, ctype=None):
    """Paginated worklist for the API, ordered by count desc (most first)."""
    where, args = ["count >= ?"], [int(min_count)]
    if ctype == "book":
        where.append("book_count > 0")
    elif ctype == "sim":
        where.append("sim_count > 0")
    elif ctype == "ref":
        where.append("ref_count > 0")
    sql = ("SELECT id, page, count, book_count, sim_count, ref_count "
           "FROM pages WHERE %s ORDER BY count DESC, id LIMIT ? OFFSET ?"
           % " AND ".join(where))
    args += [int(limit), int(offset)]
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def get_page(conn, page_id):
    """Fetch one worklist row by stable id, with citations parsed."""
    cur = conn.execute(
        "SELECT id, page, count, ref_count, sim_count, book_count, citations "
        "FROM pages WHERE id = ?", (page_id,))
    return _row_with_citations(cur.fetchone())


def get_page_by_title(conn, title):
    """Fetch one worklist row by exact title, with citations parsed."""
    cur = conn.execute(
        "SELECT id, page, count, ref_count, sim_count, book_count, citations "
        "FROM pages WHERE page = ?", (title,))
    return _row_with_citations(cur.fetchone())


def fetch_page_batch(conn, after_id, limit):
    """Pages with id > after_id ascending (for the verifier's batched pass).
    Includes `revid` so the verifier can skip unedited pages."""
    cur = conn.execute(
        "SELECT id, page, count, ref_count, sim_count, book_count, revid, "
        "citations FROM pages WHERE id > ? ORDER BY id LIMIT ?", (after_id, limit))
    return [_row_with_citations(r) for r in cur.fetchall()]


def random_pages(conn, limit=1, min_count=0, ctype=None):
    """`limit` random worklist pages (for the gadget's 'random article')."""
    where, args = ["count >= ?"], [int(min_count)]
    if ctype == "book":
        where.append("book_count > 0")
    elif ctype == "sim":
        where.append("sim_count > 0")
    elif ctype == "ref":
        where.append("ref_count > 0")
    sql = ("SELECT id, page, count, book_count, sim_count, ref_count FROM pages "
           "WHERE %s ORDER BY RANDOM() LIMIT ?" % " AND ".join(where))
    args.append(int(limit))
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def pages_present(conn, titles):
    """Given a list of titles, return the worklist rows for those present
    (for the gadget's watchlist/category intersection). Chunked to stay under
    SQLite's bound-variable limit."""
    titles = [t for t in titles if t]
    out, seen, CHUNK = [], set(), 900
    for i in range(0, len(titles), CHUNK):
        chunk = titles[i:i + CHUNK]
        ph = ",".join("?" * len(chunk))
        cur = conn.execute(
            "SELECT page, count, book_count, sim_count, ref_count FROM pages "
            "WHERE page IN (%s)" % ph, chunk)
        for r in cur.fetchall():
            if r["page"] not in seen:
                seen.add(r["page"])
                out.append(dict(r))
    return out


def stats(conn):
    row = conn.execute(
        "SELECT COUNT(*) AS pages, "
        "COALESCE(SUM(count),0) AS citations, "
        "COALESCE(SUM(book_count),0) AS book, "
        "COALESCE(SUM(sim_count),0) AS sim, "
        "COALESCE(SUM(ref_count),0) AS ref FROM pages").fetchone()
    return dict(row)


def _row_with_citations(row):
    if row is None:
        return None
    d = dict(row)
    d["citations"] = json.loads(d["citations"])
    return d


# --- Write helpers --------------------------------------------------------

def set_revid(conn, page_id, revid):
    """Record the lastrevid we just verified this page against."""
    conn.execute("UPDATE pages SET revid = ? WHERE id = ?", (int(revid), page_id))
    conn.commit()


def replace_citations(conn, page_id, citations):
    """Set a page's citations to `citations`, recomputing counts. If the list
    is empty, delete the page row. Used when pruning resolved citations."""
    if citations:
        book, sim, ref = _counts(citations)
        conn.execute(
            "UPDATE pages SET citations=?, count=?, book_count=?, sim_count=?, "
            "ref_count=? WHERE id=?",
            (json.dumps(citations, ensure_ascii=False), len(citations),
             book, sim, ref, page_id))
    else:
        conn.execute("DELETE FROM pages WHERE id=?", (page_id,))
    conn.commit()
