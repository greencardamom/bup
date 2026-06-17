# -*- coding: utf-8 -*-
#
# Loader for the `pages` worklist. Two modes, both writing to whichever backend
# db.connect() selects (BUP_DB_BACKEND=sqlite|toolsdb):
#
#   rebuild (default)  out.json (JSONL) -> pages, RENUMBERING ids in file order.
#                      Idempotent: drops and rebuilds the table so the result
#                      always reflects the current out.json. This is the normal
#                      corpus-refresh path; renumbering is expected here.
#
#   --copy-from FILE   one-time SQLite bup.db -> active backend, PRESERVING ids.
#                      This is the cutover move onto ToolsDB: ids are
#                      client-facing (/apply/<id>, the on-wiki gadget), so they
#                      must survive the move -- a rebuild would break in-flight
#                      links. Re-runnable (it drops + reloads), so the dry-run
#                      and the final delta copy use the same command.
#                      See docs/toolsdb-migration.md §7-§8.
#
# Usage:
#   python migrate.py [--db PATH] [--worklist out.json]      # rebuild
#   BUP_DB_BACKEND=toolsdb python migrate.py --copy-from /path/to/bup.db
#
# out.json and the source SQLite file are left untouched.

import os
import sys
import json
import sqlite3
import argparse

import db as dbmod

__dir__ = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_DIR = os.path.normpath(os.path.join(__dir__, "..", "..", "db"))


def _num(rec, key):
    try:
        return int(rec.get(key, 0) or 0)
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


def out_json_rows(path):
    """Stream rows for a rebuild: sequential ids in file order (matching the old
    AUTOINCREMENT behaviour), revid reset to 0 (fresh corpus, never verified)."""
    next_id = 1
    for _lineno, rec in _iter_records(path):
        yield (next_id, rec.get("page", ""),
               _num(rec, "count"), _num(rec, "ref_count"),
               _num(rec, "sim_count"), _num(rec, "book_count"), 0,
               json.dumps(rec.get("citations", []), ensure_ascii=False))
        next_id += 1


def sqlite_rows(src_path):
    """Stream rows from a source SQLite bup.db, PRESERVING id and revid (for the
    cutover copy). The citations blob is passed through verbatim as stored."""
    src = sqlite3.connect(src_path)
    src.row_factory = sqlite3.Row
    try:
        cur = src.execute(
            "SELECT id, page, count, ref_count, sim_count, book_count, revid, "
            "citations FROM pages ORDER BY id")
        for r in cur:
            yield (r["id"], r["page"], r["count"], r["ref_count"],
                   r["sim_count"], r["book_count"], r["revid"], r["citations"])
    finally:
        src.close()


def main():
    ap = argparse.ArgumentParser(
        description="Load the pages worklist (rebuild from out.json, or copy "
                    "an existing SQLite bup.db preserving ids).")
    ap.add_argument("--db", default=None,
                    help="target SQLite path (sqlite backend only; "
                         "default db.db_path()). Ignored for toolsdb.")
    ap.add_argument("--worklist",
                    default=os.path.join(DEFAULT_DB_DIR, "out.json"),
                    help="rebuild source (JSONL)")
    ap.add_argument("--copy-from", metavar="SQLITE_DB", default=None,
                    help="copy this SQLite bup.db into the active backend, "
                         "PRESERVING ids (one-time cutover move)")
    args = ap.parse_args()

    backend = dbmod._backend()
    conn = dbmod.connect(args.db)
    try:
        if args.copy_from:
            if not os.path.exists(args.copy_from):
                sys.exit("error: --copy-from file not found: %s" % args.copy_from)
            n = dbmod.rebuild_pages(conn, sqlite_rows(args.copy_from))
            print("pages:   copied %d rows from %s (ids PRESERVED)"
                  % (n, args.copy_from))
        elif os.path.exists(args.worklist):
            n = dbmod.rebuild_pages(conn, out_json_rows(args.worklist))
            print("pages:   imported %d rows from %s (ids renumbered, file order)"
                  % (n, args.worklist))
        else:
            print("pages:   SKIP (not found: %s)" % args.worklist)
            return

        # Integrity / cutover dry-run summary.
        st = dbmod.stats(conn)
        ml = dbmod.max_lengths(conn)
        print("summary: backend=%s pages=%d citations=%d"
              % (backend, st["pages"], st["citations"]))
        print("         max(page)=%d B (VARCHAR(255) cap), "
              "max(citations)=%d B (LONGTEXT cap 4 GB)"
              % (ml["page"], ml["citations"]))
        if ml["page"] > 255:
            sys.stderr.write("WARNING: a page title exceeds 255 bytes -- it will "
                             "not fit ToolsDB's VARCHAR(255)\n")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
