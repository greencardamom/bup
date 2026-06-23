# -*- coding: utf-8 -*-
#
# Data layer for bup's `pages` worklist.
#
# Two interchangeable backends, selected at connect() time by the
# BUP_DB_BACKEND env var:
#
#   sqlite  (default) - the original SQLite file on Toolforge NFS (bup.db).
#   toolsdb           - the shared, WMF-backed-up MariaDB (ToolsDB), where the
#                       `pages` table lives alongside userdb's prefs/edits in
#                       the same `<user>__bup` database.
#
# The toolsdb backend is the durable fix for the SQLite-on-NFS corruption class
# (one DB file written from two hosts -- the webservice and the verify job).
# Both backends are kept so cutover is a single env-var flip and rollback is the
# reverse flip + restart (see docs/toolsdb-migration.md). The default stays
# `sqlite` until the one-time data copy + cutover happens.
#
# Callers never touch SQL or cursors: every public helper takes a connection
# from connect() and returns plain dicts (citations pre-parsed where relevant),
# so app.py / api.py / verify.py / reconcile.py / migrate.py are backend-blind.
#
# One table:
#   pages - the working list (imported from out.json). Its `id` is assigned in
#           file order and is STABLE: it is what the /preview/<id>, /apply/<id>,
#           /runbot/<id> routes and the on-wiki gadget reference. The on-demand
#           and API routes look pages up by title.
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
import random
import sqlite3

__dir__ = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_PATH = os.path.normpath(
    os.path.join(__dir__, "..", "..", "db", "bup.db"))


def _backend():
    """Active backend: 'sqlite' (default) or 'toolsdb'. The default stays
    sqlite until the data has been copied into ToolsDB and cutover flips this."""
    return os.environ.get("BUP_DB_BACKEND", "sqlite").strip().lower()


def db_path():
    """Path to the SQLite file. Still the canonical locator for the on-NFS
    `db/` directory: auditlog.py / stats.py / app.py take os.path.dirname() of
    this to find their log/stats files, which stay on NFS regardless of which
    backend serves the worklist. data_dir() is the preferred spelling now."""
    return os.environ.get("BUP_DB_PATH", DEFAULT_DB_PATH)


def data_dir():
    """The on-NFS `db/` directory (logs, stats files, out.json). Unaffected by
    the worklist backend -- these artifacts always live on NFS."""
    return os.path.dirname(db_path())


# --- Schema ---------------------------------------------------------------

SQLITE_SCHEMA = """
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

# MariaDB / ToolsDB. Differences from SQLite that matter:
#   - citations is LONGTEXT, not TEXT: MySQL TEXT caps at 64KB and some pages'
#     citation blobs exceed that. This is the single most important gotcha.
#   - id is plain BIGINT (NOT AUTO_INCREMENT): the one-time data copy inserts
#     explicit ids to preserve the client-facing values. migrate.py manages ids
#     itself on a full rebuild.
#   - idx_count is new: top / paginated / random views sort by count, which is
#     a full scan + sort of ~183k rows on SQLite today.
TOOLSDB_SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
  id          BIGINT       NOT NULL,
  page        VARCHAR(255) NOT NULL,
  count       INT          NOT NULL DEFAULT 0,
  ref_count   INT          NOT NULL DEFAULT 0,
  sim_count   INT          NOT NULL DEFAULT 0,
  book_count  INT          NOT NULL DEFAULT 0,
  revid       BIGINT       NOT NULL DEFAULT 0,
  citations   LONGTEXT     NOT NULL,
  PRIMARY KEY (id),
  KEY idx_page  (page),
  KEY idx_count (count)
) DEFAULT CHARSET=utf8mb4
"""
# `revid` is the article's lastrevid as of the last verify; 0 = never verified.
# verify.py uses it to skip pages that haven't been edited since last checked.


# --- Connection & lifecycle -----------------------------------------------

def connect(path=None):
    """Open a worklist connection for the active backend.

    sqlite: NFS-safe settings (see _sqlite_connect). `path` overrides db_path().
    toolsdb: a ToolsDB connection to the `<user>__bup` database (`path` ignored;
             reuses userdb's connection pattern -- same replica.my.cnf, host,
             utf8mb4, DictCursor, autocommit, short fail-fast timeouts).
    """
    if _backend() == "toolsdb":
        return _toolsdb_connect()
    return _sqlite_connect(path)


def _sqlite_connect(path=None):
    """Open a SQLite connection with NFS-safe settings.

    journal_mode is deliberately NOT WAL. bup.db lives on Toolforge NFS and is
    opened from two different hosts -- the webservice pod and the daily verify
    job. WAL keeps its index in a memory-mapped -shm file that is NOT coherent
    across hosts (SQLite does not support WAL on a network filesystem), so two
    hosts writing in WAL mode corrupt the file -- which is exactly what happened
    once. Rollback journal (DELETE) uses whole-file POSIX locks that NFS lockd
    serializes; busy_timeout makes a blocked reader/writer wait for the lock
    instead of erroring. Every write here is a tiny, single-statement,
    immediately-committed transaction (see replace_citations / set_revid), so
    the lock is held only momentarily and contention stays cheap.
    """
    conn = sqlite3.connect(path or db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")   # NOT WAL -- unsafe on NFS/multi-host
    conn.execute("PRAGMA synchronous=FULL")      # durable across a crash mid-write
    conn.execute("PRAGMA busy_timeout=30000")    # wait out the other host's lock
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _toolsdb_connect():
    # Lazy import so the sqlite backend never requires pymysql. pages lives in
    # the same <user>__bup database as userdb's prefs/edits, so we reuse its
    # connection verbatim (DictCursor rows are dict-shaped like sqlite3.Row).
    import userdb
    return userdb.connect()


def _is_sqlite(conn):
    return isinstance(conn, sqlite3.Connection)


def _xlate(sql):
    """Adapt SQL written in the SQLite dialect for pymysql/MariaDB:
    `?` placeholders -> `%s`, and RANDOM() -> RAND(). Operates on SQL text only
    (arguments are bound separately), and the worklist SQL has no literal `?`,
    `%`, or "RANDOM()" outside placeholders/that function, so this is safe."""
    return sql.replace("?", "%s").replace("RANDOM()", "RAND()")


def _select(conn, sql, args=()):
    """Run a SELECT; return a list of plain dicts."""
    if _is_sqlite(conn):
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    with conn.cursor() as cur:
        cur.execute(_xlate(sql), args)
        return list(cur.fetchall())


def _select_one(conn, sql, args=()):
    """Run a SELECT expected to match one row; return a dict or None."""
    if _is_sqlite(conn):
        r = conn.execute(sql, args).fetchone()
        return dict(r) if r is not None else None
    with conn.cursor() as cur:
        cur.execute(_xlate(sql), args)
        return cur.fetchone()


def _write(conn, sql, args=()):
    """Run a single-statement write. sqlite commits immediately (its NFS-safe
    contract: tiny transactions, lock held momentarily); the toolsdb connection
    is autocommit, so the statement is durable on return either way."""
    if _is_sqlite(conn):
        conn.execute(sql, args)
        conn.commit()
        return
    with conn.cursor() as cur:
        cur.execute(_xlate(sql), args)


def init_schema(conn):
    """Create the `pages` table (+ indexes) for whichever backend `conn` is."""
    if _is_sqlite(conn):
        conn.executescript(SQLITE_SCHEMA)
        conn.commit()
    else:
        with conn.cursor() as cur:
            cur.execute(TOOLSDB_SCHEMA)


def setup():
    """ToolsDB one-time setup: create the `<user>__bup` database (idempotent)
    and the `pages` table in it. Mirrors userdb.setup(); the two share the DB.
    Run once before the data copy:  python db.py --setup
    """
    import userdb
    name = userdb.db_name()
    conn = userdb.connect(with_db=False)
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE DATABASE IF NOT EXISTS `%s` "
                        "DEFAULT CHARSET utf8mb4" % name)
            cur.execute("USE `%s`" % name)
            cur.execute(TOOLSDB_SCHEMA)
    finally:
        conn.close()
    return name


# --- Bulk load (migrate.py) -----------------------------------------------

def rebuild_pages(conn, rows, batch=5000, max_bytes=8_000_000):
    """Drop the pages table, recreate it for this backend, and bulk-load `rows`
    in a single transaction. Each row is a full tuple:
        (id, page, count, ref_count, sim_count, book_count, revid, citations)
    where `citations` is the JSON text blob. `rows` may be any iterable -- it is
    streamed in chunks, so a 154k-row generator stays cheap. Returns the number
    of rows inserted.

    On toolsdb a chunk is flushed when it reaches `batch` rows OR ~`max_bytes`
    of citation text, whichever comes first: pymysql packs each chunk into one
    INSERT, and a cluster of large citation blobs (the biggest are ~120 KB) must
    not build a statement over MariaDB's max_allowed_packet (32 MB on ToolsDB).
    8 MB leaves generous headroom for multibyte + per-row SQL overhead.

    ids come from `rows`, so renumbering is the caller's choice: the out.json
    rebuild assigns sequential ids (renumbering, expected on a corpus refresh);
    the one-time SQLite->ToolsDB copy passes existing ids through to preserve the
    client-facing /apply/<id> references (see docs/toolsdb-migration.md §7)."""
    sql = ("INSERT INTO pages "
           "(id, page, count, ref_count, sim_count, book_count, revid, citations) "
           "VALUES (?,?,?,?,?,?,?,?)")

    if _is_sqlite(conn):
        conn.execute("DROP TABLE IF EXISTS pages")
        init_schema(conn)
        total, buf = 0, []
        for r in rows:
            buf.append(r)
            if len(buf) >= batch:
                conn.executemany(sql, buf)
                total += len(buf)
                buf = []
        if buf:
            conn.executemany(sql, buf)
            total += len(buf)
        conn.commit()
        return total

    # toolsdb: DROP/CREATE implicitly commit in MariaDB; load the data in ONE
    # explicit transaction (the connection is autocommit, so without begin()
    # each chunk's inserts would commit per round-trip).
    isql = _xlate(sql)
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS pages")
        cur.execute(TOOLSDB_SCHEMA)
    conn.begin()
    try:
        total, buf, buf_bytes = 0, [], 0
        with conn.cursor() as cur:
            for r in rows:
                buf.append(r)
                buf_bytes += len(r[7])          # citations blob dominates row size
                if len(buf) >= batch or buf_bytes >= max_bytes:
                    cur.executemany(isql, buf)
                    total += len(buf)
                    buf, buf_bytes = [], 0
            if buf:
                cur.executemany(isql, buf)
                total += len(buf)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return total


def max_lengths(conn):
    """Max byte-length of `page` and `citations` across the table -- for the
    cutover dry-run checks (page must fit VARCHAR(255); citations must fit
    LONGTEXT). LENGTH() is bytes on MariaDB but characters on SQLite, so the
    title-fits-255 check is only authoritative against the ToolsDB target."""
    row = _select_one(
        conn,
        "SELECT COALESCE(MAX(LENGTH(page)),0) AS page, "
        "COALESCE(MAX(LENGTH(citations)),0) AS citations FROM pages")
    return {k: int(v) for k, v in row.items()}


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
    return _select(
        conn,
        "SELECT id, page, count, ref_count, sim_count, book_count "
        "FROM pages ORDER BY id LIMIT ?", (limit,))


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
    return _select(conn, sql, args)


def worklist_total(conn, min_count=0, ctype=None):
    """Total worklist rows matching the same filters as worklist_page, for the
    UI pager ('Page X of Y'). Mirrors worklist_page's WHERE clause."""
    where, args = ["count >= ?"], [int(min_count)]
    if ctype == "book":
        where.append("book_count > 0")
    elif ctype == "sim":
        where.append("sim_count > 0")
    elif ctype == "ref":
        where.append("ref_count > 0")
    sql = "SELECT COUNT(*) AS n FROM pages WHERE %s" % " AND ".join(where)
    return int(_select_one(conn, sql, args)["n"])


def search_titles(conn, q, ctype=None, limit=50):
    """Case-insensitive title substring search + optional citation-type filter,
    ordered by count desc (most-impactful first). For the Search view. `%`/`_`
    in the query are escaped so they match literally rather than as wildcards.
    Substring LIKE is a full scan, fine for an occasional interactive search.

    On toolsdb the match folds case by the utf8mb4_general_ci collation (and so
    also folds non-ASCII case) -- a superset of SQLite's ASCII-only LIKE folding;
    acceptable / arguably better."""
    where, args = ["page LIKE ? ESCAPE '!'"], [_like_pattern(q)]
    if ctype == "book":
        where.append("book_count > 0")
    elif ctype == "sim":
        where.append("sim_count > 0")
    elif ctype == "ref":
        where.append("ref_count > 0")
    sql = ("SELECT id, page, count, book_count, sim_count, ref_count FROM pages "
           "WHERE %s ORDER BY count DESC, id LIMIT ?" % " AND ".join(where))
    args.append(int(limit))
    return _select(conn, sql, args)


def _like_pattern(q):
    """Wrap `q` as a `%substring%` LIKE pattern with its wildcards neutralised.
    Uses '!' as the ESCAPE char (not the SQL-standard backslash): a backslash in
    a string literal is reprocessed by MariaDB but not SQLite, so '!' keeps the
    ESCAPE clause identical and safe across both backends."""
    for ch in ("!", "%", "_"):          # escape the escape char itself first
        q = q.replace(ch, "!" + ch)
    return "%" + q + "%"


def get_page(conn, page_id):
    """Fetch one worklist row by stable id, with citations parsed."""
    return _row_with_citations(_select_one(
        conn,
        "SELECT id, page, count, ref_count, sim_count, book_count, citations "
        "FROM pages WHERE id = ?", (page_id,)))


def get_page_by_title(conn, title):
    """Fetch one worklist row by exact title, with citations parsed."""
    return _row_with_citations(_select_one(
        conn,
        "SELECT id, page, count, ref_count, sim_count, book_count, citations "
        "FROM pages WHERE page = ?", (title,)))


def fetch_page_batch(conn, after_id, limit):
    """Pages with id > after_id ascending (for the verifier's batched pass).
    Includes `revid` so the verifier can skip unedited pages. The `id > ?
    ORDER BY id` keyway rides the primary key on both backends."""
    rows = _select(
        conn,
        "SELECT id, page, count, ref_count, sim_count, book_count, revid, "
        "citations FROM pages WHERE id > ? ORDER BY id LIMIT ?",
        (after_id, limit))
    return [_row_with_citations(r) for r in rows]


def random_pages(conn, limit=1, min_count=0, ctype=None):
    """`limit` random worklist pages (for the gadget's 'random article' and
    /main's default 'random' view).

    SQLite uses ORDER BY RANDOM() -- cheap on the local file. On ToolsDB
    (MariaDB) the equivalent ORDER BY RAND() filesorts the entire ~150k-row
    `pages` table to pick a handful of rows, which blows past the query timeout
    and 500s /main (whose default view is 'random'). There we sample by primary
    key instead: pick random ids and take the next row at or above each -- an
    index range scan that touches only a few rows per pick. The result is
    slightly biased toward rows after id gaps, which is fine for a "random
    article" feature."""
    where, args = ["count >= ?"], [int(min_count)]
    if ctype == "book":
        where.append("book_count > 0")
    elif ctype == "sim":
        where.append("sim_count > 0")
    elif ctype == "ref":
        where.append("ref_count > 0")
    cols = "id, page, count, book_count, sim_count, ref_count"
    where_sql = " AND ".join(where)
    limit = int(limit)

    if _is_sqlite(conn):
        sql = ("SELECT %s FROM pages WHERE %s ORDER BY RANDOM() LIMIT ?"
               % (cols, where_sql))
        return _select(conn, sql, args + [limit])

    # MariaDB: id-range sampling -- no full-table filesort.
    row = _select_one(conn, "SELECT MAX(id) AS m FROM pages")
    max_id = (row or {}).get("m") or 0
    if not max_id:
        return []
    pick_sql = ("SELECT %s FROM pages WHERE id >= ? AND %s ORDER BY id LIMIT 1"
                % (cols, where_sql))
    # First matching row from the table start -- the wrap target when a pick
    # lands past the last qualifying row.
    first_sql = ("SELECT %s FROM pages WHERE %s ORDER BY id LIMIT 1"
                 % (cols, where_sql))
    out, seen = [], set()
    # Bounded attempts so a sparse filter (a ctype with few matches) cannot
    # loop forever; it just returns fewer than `limit` rows.
    for _ in range(limit * 10):
        if len(out) >= limit:
            break
        rid = random.randint(1, max_id)
        r = _select_one(conn, pick_sql, [rid] + args)
        if r is None:
            r = _select_one(conn, first_sql, args)
        if r and r["id"] not in seen:
            seen.add(r["id"])
            out.append(r)
    return out


def pages_present(conn, titles):
    """Given a list of titles, return the worklist rows for those present
    (for the gadget's watchlist/category intersection). Chunked to stay under
    SQLite's bound-variable limit; the same chunk size is harmless on MariaDB
    (whose constraint is max_allowed_packet, not a variable count)."""
    titles = [t for t in titles if t]
    out, seen, CHUNK = [], set(), 900
    for i in range(0, len(titles), CHUNK):
        chunk = titles[i:i + CHUNK]
        ph = ",".join("?" * len(chunk))
        rows = _select(
            conn,
            "SELECT id, page, count, book_count, sim_count, ref_count FROM pages "
            "WHERE page IN (%s)" % ph, chunk)
        for r in rows:
            if r["page"] not in seen:
                seen.add(r["page"])
                out.append(r)
    return out


def stats(conn):
    """Corpus totals. SUM() is DECIMAL on MariaDB and int on SQLite, so coerce
    to int for a stable, JSON-serialisable shape across backends."""
    row = _select_one(
        conn,
        "SELECT COUNT(*) AS pages, "
        "COALESCE(SUM(count),0) AS citations, "
        "COALESCE(SUM(book_count),0) AS book, "
        "COALESCE(SUM(sim_count),0) AS sim, "
        "COALESCE(SUM(ref_count),0) AS ref FROM pages")
    return {k: int(v) for k, v in row.items()}


def _row_with_citations(row):
    if row is None:
        return None
    d = dict(row)
    d["citations"] = json.loads(d["citations"])
    return d


# --- Write helpers --------------------------------------------------------

def set_revid(conn, page_id, revid):
    """Record the lastrevid we just verified this page against."""
    _write(conn, "UPDATE pages SET revid = ? WHERE id = ?",
           (int(revid), page_id))


def replace_citations(conn, page_id, citations):
    """Set a page's citations to `citations`, recomputing counts. If the list
    is empty, delete the page row. Used when pruning resolved citations."""
    if citations:
        book, sim, ref = _counts(citations)
        _write(
            conn,
            "UPDATE pages SET citations=?, count=?, book_count=?, sim_count=?, "
            "ref_count=? WHERE id=?",
            (json.dumps(citations, ensure_ascii=False), len(citations),
             book, sim, ref, page_id))
    else:
        _write(conn, "DELETE FROM pages WHERE id=?", (page_id,))


def main():
    import sys
    if "--setup" in sys.argv:
        print("db: created/verified ToolsDB database + pages table:", setup())
    else:
        print("usage: python db.py --setup   (ToolsDB backend only)")


if __name__ == "__main__":
    main()
