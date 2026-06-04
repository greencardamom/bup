# -*- coding: utf-8 -*-
#
# One-time importer: out.json (JSONL) -> SQLite `pages` table.
#
# Idempotent: re-running drops and rebuilds the pages table so the import
# always reflects the current out.json. Safe to run repeatedly.
#
# Usage:
#   python migrate.py [--db PATH] [--worklist out.json]
#
# out.json is left untouched.

import os
import sys
import json
import argparse

import db as dbmod

__dir__ = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_DIR = os.path.normpath(os.path.join(__dir__, "..", "..", "db"))

# Numeric fields copied straight across from each JSON record.
NUM_FIELDS = ("count", "ref_count", "sim_count", "book_count")

BATCH = 5000


def _num(rec, key):
    try:
        return int(rec.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _done(rec):
    # done is stored as a string "0"/"1" in the JSONL; normalize to int.
    try:
        return 1 if int(rec.get("done", 0) or 0) else 0
    except (TypeError, ValueError):
        return 0


def _iter_records(path):
    """Yield (lineno, record) for each non-blank JSONL line; skip bad lines."""
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield lineno, json.loads(line)
            except ValueError as e:
                sys.stderr.write("skip %s:%d bad json: %s\n"
                                 % (os.path.basename(path), lineno, e))


def load_pages(conn, path):
    """Import out.json into pages, preserving file order via autoincrement id."""
    conn.execute("DROP TABLE IF EXISTS pages")
    dbmod.init_schema(conn)
    sql = ("INSERT INTO pages "
           "(page, count, ref_count, sim_count, book_count, done, citations) "
           "VALUES (?,?,?,?,?,?,?)")
    batch, total = [], 0
    for _lineno, rec in _iter_records(path):
        batch.append((
            rec.get("page", ""),
            _num(rec, "count"), _num(rec, "ref_count"),
            _num(rec, "sim_count"), _num(rec, "book_count"),
            _done(rec),
            json.dumps(rec.get("citations", []), ensure_ascii=False),
        ))
        if len(batch) >= BATCH:
            conn.executemany(sql, batch)
            total += len(batch)
            batch = []
    if batch:
        conn.executemany(sql, batch)
        total += len(batch)
    conn.commit()
    return total


def main():
    ap = argparse.ArgumentParser(description="Import out.json into SQLite")
    ap.add_argument("--db", default=None, help="SQLite path (default db.db_path())")
    ap.add_argument("--worklist", default=os.path.join(DEFAULT_DB_DIR, "out.json"))
    args = ap.parse_args()

    conn = dbmod.connect(args.db)
    dbmod.init_schema(conn)

    if os.path.exists(args.worklist):
        n = load_pages(conn, args.worklist)
        print("pages:   imported %d rows from %s" % (n, args.worklist))
    else:
        print("pages:   SKIP (not found: %s)" % args.worklist)

    # Quick integrity summary.
    p_total = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    p_done = conn.execute("SELECT COUNT(*) FROM pages WHERE done=1").fetchone()[0]
    print("summary: pages=%d (done=%d), db=%s"
          % (p_total, p_done, args.db or dbmod.db_path()))
    conn.close()


if __name__ == "__main__":
    main()
