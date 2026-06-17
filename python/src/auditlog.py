# -*- coding: utf-8 -*-
#
# Append-only flat audit logs (write-once, never read by the app). They live in
# the db/ directory alongside log.txt:
#
#   removed.log : every citation pruned from the DB
#       <title> ---- oldcite ---- newcite ---- date
#   edits.log   : every detected APPLICATION of a bup citation, with source
#       <title> ---- oldcite ---- newcite ---- date ---- <bupUI|inferredAPI>
#
# oldcite/newcite can contain literal newlines, which would break the one-line
# format, so newlines are encoded as the __hidenewline__ marker (same marker
# makejson.awk uses) to keep every entry a single greppable line.

import os
from datetime import date

import db as dbmod

MARKER = "__hidenewline__"
SEP = " ---- "


def _enc(s):
    return (s or "").replace("\r\n", "\n").replace("\n", MARKER)


def _logdir():
    return dbmod.data_dir()


def _append(filename, fields):
    # These logs expose which articles/citations were touched, so keep them
    # owner-only (0600); set on creation so a freshly-created log is private.
    path = os.path.join(_logdir(), filename)
    try:
        existed = os.path.exists(path)
        with open(path, "a", encoding="utf-8") as f:
            f.write(SEP.join(fields) + "\n")
        if not existed:
            os.chmod(path, 0o600)
    except OSError:
        pass


def log_removed(title, oldcite, newcite, when=None):
    _append("removed.log",
            [title, _enc(oldcite), _enc(newcite), str(when or date.today())])


def log_edit(title, oldcite, newcite, kind, when=None):
    """kind is 'bupUI' (bup's web tool edited) or 'inferredAPI' (verifier saw
    the bluelink land by other means)."""
    _append("edits.log",
            [title, _enc(oldcite), _enc(newcite), str(when or date.today()),
             kind])
