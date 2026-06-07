# -*- coding: utf-8 -*-
#
# Per-user data, stored in ToolsDB (the shared, backed-up MariaDB on Toolforge),
# NOT in bup.db -- because migrate.py drops/rebuilds bup.db on every corpus
# refresh, and ToolsDB is the canonical home for writable, durable tool data.
#
# Database: `<user>__bup` on tools.db.svc.wikimedia.cloud, where <user> is the
# MySQL account in ~/replica.my.cnf (the same credentials file the stats job
# uses for the replica). Two tables:
#   prefs  - one row per user: custom edit summary, default view, minor flag
#   edits  - one row per applied edit: for the user's personal stats
#
# Connections are opened per request and closed on teardown (ToolsDB reaps idle
# connections). Callers treat ToolsDB as best-effort: a failure here must never
# block a Wikipedia edit -- prefs fall back to defaults, recording is skipped.
#
# One-time setup (creates the database + tables; idempotent):
#   python userdb.py --setup

import os
import sys
import configparser

import pymysql
import pymysql.cursors

TOOLSDB_HOST = "tools.db.svc.wikimedia.cloud"
REPLICA_CNF = os.path.expanduser("~/replica.my.cnf")


def _db_user():
    """The MySQL account name from replica.my.cnf — the prefix ToolsDB requires
    on databases this credential may create/use (`<user>__<name>`)."""
    cp = configparser.ConfigParser()
    cp.read(REPLICA_CNF)
    return cp.get("client", "user", fallback="").strip().strip("'\"")


def db_name():
    return "%s__bup" % _db_user()


def connect(with_db=True):
    """Open a ToolsDB connection (autocommit; short timeouts so a stalled
    ToolsDB fails fast rather than hanging a web request)."""
    kw = dict(read_default_file=REPLICA_CNF, host=TOOLSDB_HOST,
              charset="utf8mb4", autocommit=True,
              connect_timeout=5, read_timeout=10, write_timeout=10,
              cursorclass=pymysql.cursors.DictCursor)
    if with_db:
        kw["database"] = db_name()
    return pymysql.connect(**kw)


_PREFS_DDL = """
CREATE TABLE IF NOT EXISTS prefs (
  username      VARCHAR(255) PRIMARY KEY,
  edit_summary  VARCHAR(500) NULL,
  default_view  VARCHAR(16)  NULL,
  minor         TINYINT      NOT NULL DEFAULT 0,
  updated       DATETIME     NULL
) DEFAULT CHARSET=utf8mb4
"""

_EDITS_DDL = """
CREATE TABLE IF NOT EXISTS edits (
  id        BIGINT       AUTO_INCREMENT PRIMARY KEY,
  username  VARCHAR(255) NOT NULL,
  page      VARCHAR(255) NOT NULL,
  count     INT          NOT NULL DEFAULT 0,
  oldrevid  BIGINT       NULL,
  newrevid  BIGINT       NULL,
  ts        DATETIME     NOT NULL,
  KEY idx_user_ts (username, ts)
) DEFAULT CHARSET=utf8mb4
"""


def setup():
    """Create the database and tables if absent (idempotent). Tools may create
    databases prefixed with their own MySQL user name."""
    name = db_name()
    conn = connect(with_db=False)
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE DATABASE IF NOT EXISTS `%s` "
                        "DEFAULT CHARSET utf8mb4" % name)
            cur.execute("USE `%s`" % name)
            cur.execute(_PREFS_DDL)
            cur.execute(_EDITS_DDL)
    finally:
        conn.close()
    return name


# --- Preferences ----------------------------------------------------------

def get_prefs(conn, username):
    """Row dict {edit_summary, default_view, minor} or None if unset."""
    with conn.cursor() as cur:
        cur.execute("SELECT edit_summary, default_view, minor "
                    "FROM prefs WHERE username=%s", (username,))
        return cur.fetchone()


def save_prefs(conn, username, edit_summary, default_view, minor):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO prefs (username, edit_summary, default_view, minor, "
            "updated) VALUES (%s,%s,%s,%s,NOW()) "
            "ON DUPLICATE KEY UPDATE edit_summary=VALUES(edit_summary), "
            "default_view=VALUES(default_view), minor=VALUES(minor), "
            "updated=NOW()",
            (username, edit_summary or None, default_view or None,
             1 if minor else 0))


# --- Per-edit log (personal stats) ----------------------------------------

def record_edit(conn, username, page, count, oldrevid, newrevid):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO edits (username, page, count, oldrevid, newrevid, ts) "
            "VALUES (%s,%s,%s,%s,%s,NOW())",
            (username, page, int(count or 0), oldrevid or None, newrevid or None))


def user_stats(conn, username):
    """{edits, links, last_active} totals for one user."""
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS edits, COALESCE(SUM(count),0) AS links, "
                    "MAX(ts) AS last_active FROM edits WHERE username=%s",
                    (username,))
        return cur.fetchone()


def recent_edits(conn, username, limit=20):
    with conn.cursor() as cur:
        cur.execute("SELECT page, count, oldrevid, newrevid, ts FROM edits "
                    "WHERE username=%s ORDER BY ts DESC, id DESC LIMIT %s",
                    (username, int(limit)))
        return list(cur.fetchall())


def main():
    if "--setup" in sys.argv:
        print("userdb: created/verified database + tables:", setup())
    else:
        print("usage: python userdb.py --setup")


if __name__ == "__main__":
    main()
