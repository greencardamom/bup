# -*- coding: utf-8 -*-
#
# Reconcile the DB `done` flags from the authoritative edit logs.
#
# Background: in the old system, done-marking ran through awk setjson(), which
# bailed out whenever the db/json.temp lock file existed. A stale 0-byte
# json.temp left setjson a silent no-op, so out.json ended up with every record
# done:"0" even though ~120+ articles had actually been edited. The real record
# of what was done therefore lives in:
#   - db/log.txt   : "<page> ---- <user> ---- <count> ---- <date> ---- Success"
#   - db/clearit   : jq commands of the form  select (.page == "<page>") .done
#
# This marks done=1 in the pages table for every page named in either source.
# Idempotent and safe to re-run; it only sets done=1, never clears it.
#
# Usage:
#   python reconcile_done.py [--db PATH] [--log db/log.txt] [--clearit db/clearit]
#                            [--dry-run]

import os
import re
import argparse

import db as dbmod

__dir__ = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_DIR = os.path.normpath(os.path.join(__dir__, "..", "..", "db"))

_CLEARIT_PAGE = re.compile(r'select \(\.page == "(.*?)"\) \.done')


def success_pages(log_path):
    """Page titles with a Success entry in log.txt."""
    pages = set()
    if not os.path.exists(log_path):
        return pages
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            if "---- Success" in line:
                pages.add(line.split(" ---- ")[0].strip())
    return pages


def clearit_pages(clearit_path):
    """Page titles that were marked done in the clearit command log."""
    pages = set()
    if not os.path.exists(clearit_path):
        return pages
    with open(clearit_path, encoding="utf-8") as f:
        for line in f:
            m = _CLEARIT_PAGE.search(line)
            if m:
                pages.add(m.group(1))
    return pages


def main():
    ap = argparse.ArgumentParser(description="Mark done from edit logs")
    ap.add_argument("--db", default=None)
    ap.add_argument("--log", default=os.path.join(DEFAULT_DB_DIR, "log.txt"))
    ap.add_argument("--clearit", default=os.path.join(DEFAULT_DB_DIR, "clearit"))
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change without writing")
    args = ap.parse_args()

    done_pages = success_pages(args.log) | clearit_pages(args.clearit)
    print("done pages from logs: %d (log.txt Success + clearit)" % len(done_pages))

    conn = dbmod.connect(args.db)
    cur = conn.cursor()

    present = 0
    to_set = []
    for title in done_pages:
        row = cur.execute(
            "SELECT done FROM pages WHERE page = ? LIMIT 1", (title,)).fetchone()
        if row is None:
            continue
        present += 1
        if row["done"] == 0:
            to_set.append(title)

    print("present in pages table: %d" % present)
    print("currently done=0 (will mark done): %d" % len(to_set))

    if args.dry_run:
        for t in sorted(to_set):
            print("   would set done=1:", t)
        conn.close()
        return

    cur.executemany("UPDATE pages SET done = 1 WHERE page = ?",
                    [(t,) for t in to_set])
    conn.commit()

    total_done = cur.execute("SELECT COUNT(*) FROM pages WHERE done=1").fetchone()[0]
    print("marked %d pages done; pages table now has %d done=1"
          % (len(to_set), total_done))
    conn.close()


if __name__ == "__main__":
    main()
