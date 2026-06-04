# -*- coding: utf-8 -*-
#
# Thin wrapper around the `wikiget` binary for reading wiki text.
#
# wikiget is self-contained (no Python deps; only needs standard tools like
# wget/curl) and handles OAuth, maxlag, retry/pause and the Toolforge private
# proxy internally. We only use it for READS here ( -w ); edits stay on the
# per-user mwoauth flow in app.py so they are attributed to the logged-in user.
#
# Path is configurable via BUP_WIKIGET; defaults to the Toolforge location.

import os
import subprocess

DEFAULT_WIKIGET = "/data/project/bup/BotWikiAwk/bin/wikiget"


def wikiget_path():
    return os.environ.get("BUP_WIKIGET", DEFAULT_WIKIGET)


def fetch_wikitext(page, timeout=120):
    """Return the current wiki text of `page`, or "" on any failure.

    Mirrors the old `wikiget -w <page>` call. Returns "" (not None) so callers
    can treat a missing/failed fetch the same as an empty article.
    """
    try:
        proc = subprocess.run(
            [wikiget_path(), "-w", page],
            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout
