# -*- coding: utf-8 -*-
#
# Daily usage stats for bup + the BooksUp gadget. Appends one JSON line per day
# to www/static/booksup-stats-<year>.jsonl — served (read-only, public) at
#   https://tools-static.wmflabs.org/bup/booksup-stats-<year>.jsonl
# so other tools (e.g. iabotwatch on acre) can fetch it over HTTP. E.g.:
#
#   {"date":"2026-06-05","urls_added":49,
#    "webtool":{"edits":1,"urls":44},
#    "gadget":{"edits":2,"urls":5},
#    "api":{"page":40,"random":6,"worklist":3,"pages":8}}
#
# Headline metric = urls_added (archive.org links added to Wikipedia via the
# tool, web UI + gadget combined). Run once a day for the PREVIOUS UTC day:
#
#   toolforge jobs run booksup-stats --image python3.11 --schedule "@daily" \
#     --command "$HOME/www/python/venv/bin/python $HOME/www/python/src/stats.py" \
#     --mount all
#
# Sources, per day:
#   webtool : bup's own db/log.txt   (page ---- user ---- COUNT ---- date ---- Success)
#   gadget  : Wiki Replicas          (saved edits whose summary contains "BooksUp";
#                                      the count N is parsed from "Adding N book link(s)")
#   api     : db/api_hits.log         (one line per API call, written by api.py)
# Every run writes a record (all-zero days included). No backfill — starts now.

import os
import re
import sys
import json
import argparse
from datetime import date, timedelta, datetime

import db as dbmod

DB_DIR = dbmod.data_dir()
# www/static is served at https://tools-static.wmflabs.org/bup/ (sibling of db/)
STATIC_DIR = os.path.join(os.path.dirname(DB_DIR), "static")
LOG_TXT = os.path.join(DB_DIR, "log.txt")
API_HITS = os.path.join(DB_DIR, "api_hits.log")
REPLICA_CNF = os.path.expanduser("~/replica.my.cnf")
REPLICA_HOST = "enwiki.analytics.db.svc.wikimedia.cloud"
REPLICA_DB = "enwiki_p"

_COUNT_RE = re.compile(r"Adding\s+(\d+)\s+book link")


def webtool_counts(day):
    """(edits, urls) from bup's log.txt Success lines dated `day` (YYYY-MM-DD)."""
    edits = urls = 0
    try:
        with open(LOG_TXT, encoding="utf-8") as f:
            for line in f:
                p = [x.strip() for x in line.split("----")]
                if len(p) >= 5 and p[4].startswith("Success") and p[3] == day:
                    edits += 1
                    try:
                        urls += int(p[2])
                    except ValueError:
                        pass
    except OSError:
        pass
    return edits, urls


def api_counts(day):
    """Per-endpoint API call counts for `day` from api_hits.log."""
    c = {"page": 0, "random": 0, "worklist": 0, "pages": 0}
    try:
        with open(API_HITS, encoding="utf-8") as f:
            for line in f:
                ts, _, ep = line.partition(" ")
                ep = ep.strip()
                if ts[:10] == day and ep in c:
                    c[ep] += 1
    except OSError:
        pass
    return c


def gadget_counts(day):
    """(edits, urls) of saved gadget edits on `day`, from the Wiki Replicas.
    Gadget edit summaries contain 'BooksUp' and 'Adding N book link(s)'."""
    import pymysql
    start = day.replace("-", "") + "000000"
    end = day.replace("-", "") + "235959"
    conn = pymysql.connect(read_default_file=REPLICA_CNF, host=REPLICA_HOST,
                           db=REPLICA_DB, charset="utf8mb4")
    edits = urls = 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT comment_text FROM revision "
                "JOIN comment ON rev_comment_id = comment_id "
                "WHERE rev_timestamp BETWEEN %s AND %s "
                "AND comment_text LIKE %s",
                (start, end, "%BooksUp%"))
            for (ct,) in cur.fetchall():
                if isinstance(ct, (bytes, bytearray)):
                    ct = ct.decode("utf-8", "replace")
                edits += 1
                m = _COUNT_RE.search(ct or "")
                urls += int(m.group(1)) if m else 1
    finally:
        conn.close()
    return edits, urls


def main():
    ap = argparse.ArgumentParser(description="Daily bup/BooksUp usage stats")
    ap.add_argument("--date", help="YYYY-MM-DD (default: yesterday, UTC)")
    args = ap.parse_args()
    d = (datetime.strptime(args.date, "%Y-%m-%d").date() if args.date
         else date.today() - timedelta(days=1))
    ds = d.isoformat()

    wt_e, wt_u = webtool_counts(ds)
    try:
        g_e, g_u = gadget_counts(ds)
    except Exception as e:
        sys.stderr.write("gadget/replica error: %s\n" % e)
        g_e = g_u = 0
    api = api_counts(ds)

    rec = {
        "date": ds,
        "urls_added": wt_u + g_u,
        "webtool": {"edits": wt_e, "urls": wt_u},
        "gadget": {"edits": g_e, "urls": g_u},
        "api": api,
    }
    os.makedirs(STATIC_DIR, exist_ok=True)
    out = os.path.join(STATIC_DIR, "booksup-stats-%d.jsonl" % d.year)
    write_record(out, rec)
    print(json.dumps(rec))


def write_record(out, rec):
    """Write `rec` as the line for its date, keeping the file to ONE line per
    date, date-sorted. Idempotent: re-running a day replaces that day's line
    instead of appending a duplicate (and a single run also de-dups/sorts any
    pre-existing duplicate lines). Atomic via temp-file rename."""
    by_date = {}
    if os.path.exists(out):
        with open(out, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                d = r.get("date")
                if d:
                    by_date[d] = r
    by_date[rec["date"]] = rec        # new record wins for its date

    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for d in sorted(by_date):
            f.write(json.dumps(by_date[d]) + "\n")
    os.replace(tmp, out)              # atomic
    try:
        os.chmod(out, 0o644)          # readable by the tools-static web server
    except OSError:
        pass


if __name__ == "__main__":
    main()
