# -*- coding: utf-8 -*-
#
# Online, integrity-checked backup of bup.db.
#
# The database had NO backups when an NFS/WAL-induced corruption hit it once
# (see db.py for why WAL was removed). A recent good copy turns a future
# corruption from a data-loss incident into a quick restore -- which is the
# main threat here, since bup.db is rebuilt only rarely from the (large,
# offline) out.json and the daily verify job mutates it in between.
#
# Uses SQLite's online backup API: it produces a transactionally-consistent
# snapshot even while the webservice is reading/writing, so the service need
# not be stopped. The snapshot is integrity-checked BEFORE it is allowed to
# join the backup set, so a corrupt source can never overwrite a known-good
# backup. The newest --keep copies are retained; older ones are pruned.
#
# Run as a Toolforge scheduled job (daily, ahead of verify):
#   toolforge jobs run backup --image python3.11 --schedule "@daily" \
#       --command "$HOME/www/python/venv/bin/python $HOME/www/python/src/backup.py" \
#       --mount all
#
# Backups land in $HOME/backups (sibling of db/); override with BUP_BACKUP_DIR.
# They share the same NFS volume, so this guards against logical corruption
# (the failure we actually saw), not against a loss of the volume itself.

import os
import sys
import glob
import sqlite3
import argparse
from datetime import datetime, timezone

import db as dbmod

KEEP = 7   # most-recent backups to retain


def backup_dir():
    return os.environ.get(
        "BUP_BACKUP_DIR",
        os.path.join(os.path.dirname(os.path.dirname(dbmod.db_path())), "backups"))


def _integrity_ok(path):
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        return conn.execute("PRAGMA integrity_check(1)").fetchone()[0] == "ok"
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(
        description="Online, integrity-checked backup of bup.db")
    ap.add_argument("--keep", type=int, default=KEEP,
                    help="number of most-recent backups to retain (default %d)" % KEEP)
    ap.add_argument("--stamp", default=None,
                    help="filename timestamp (default: current UTC YYYYmmdd-HHMM)")
    args = ap.parse_args()

    src_path = dbmod.db_path()
    bdir = backup_dir()
    os.makedirs(bdir, exist_ok=True)

    stamp = args.stamp or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    tmp = os.path.join(bdir, ".bup-%s.db.tmp" % stamp)
    final = os.path.join(bdir, "bup-%s.db" % stamp)
    if os.path.exists(tmp):
        os.remove(tmp)

    # Read-only source connection: the backup reads, it never takes a write
    # lock on the live db (so it can't block the webservice or verify).
    src = sqlite3.connect("file:%s?mode=ro" % src_path, uri=True)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)          # consistent online snapshot, retried by SQLite
    finally:
        dst.close()
        src.close()

    if not _integrity_ok(tmp):
        os.remove(tmp)
        print("backup: ABORT -- snapshot failed integrity_check; "
              "source may be corrupt. Existing backups left untouched.")
        sys.exit(1)

    os.replace(tmp, final)       # atomic publish
    print("backup: wrote %s (%d bytes)" % (final, os.path.getsize(final)))

    # Rotate: names sort chronologically (YYYYmmdd-HHMM), keep the newest --keep.
    existing = sorted(glob.glob(os.path.join(bdir, "bup-*.db")))
    for old in existing[:-args.keep] if args.keep > 0 else []:
        os.remove(old)
        print("backup: pruned %s" % old)

    sys.stdout.flush()


if __name__ == "__main__":
    main()
