# -*- coding: utf-8 -*-
#
# Reconcile a page record against the live article text: prune citations whose
# oldcite is no longer present (bup matches literally, so an absent oldcite can
# never match again -> it is dead weight), logging each removal.
#
# Attribution for the edits.log:
#   - oldcite in `applied_oldcites`  -> "bupUI"      (bup's web tool just did it)
#   - else if newcite is in `content` -> "inferredAPI" (the bluelink landed by
#                                                        other means, e.g. a gadget)
#   - else                            -> not an application (citation removed or
#                                         reworded); removed.log only.
#
# Flask-free so verify.py can use it directly.

import db as dbmod
import auditlog
import bookbot


def reconcile_page(conn, record, content, applied_oldcites=()):
    """Prune gone citations from `record` against `content`. Returns
    (open_citations, gone_citations). Caller must have validated `content`
    (non-empty, not a redirect) — this function trusts it."""
    applied = set(applied_oldcites)
    open_cites, gone = [], []
    for c in record.get("citations", []):
        oldcite = c.get("oldcite", "")
        if oldcite and oldcite in content:
            open_cites.append(c)
        else:
            gone.append(c)

    for c in gone:
        oldcite = c.get("oldcite", "")
        newcite = c.get("newcite", "")
        auditlog.log_removed(record["page"], oldcite, newcite)
        if oldcite in applied:
            auditlog.log_edit(record["page"], oldcite, newcite, "bupUI")
        elif newcite and newcite in content:
            auditlog.log_edit(record["page"], oldcite, newcite, "inferredAPI")
        # else: removed/reworded, not an application -> removed.log only

    if gone:
        dbmod.replace_citations(conn, record["id"], open_cites)
    return open_cites, gone


def prune_unviable(conn, record):
    """Drop candidates that can never produce an edit: their `newcite` adds no
    archive.org link (newcite == oldcite, or no real /details/<id> URL). Unlike
    reconcile_page this is independent of the live article -- a dead candidate is
    dead regardless of what the page says -- so the verifier can run it over
    every page, including ones that never change. Logs each removal and rewrites
    the row's counts (deleting the page if nothing viable remains). Returns
    (kept, dropped)."""
    kept, dropped = [], []
    for c in record.get("citations", []):
        (kept if bookbot.is_viable(c) else dropped).append(c)

    if dropped:
        for c in dropped:
            auditlog.log_removed(record["page"], c.get("oldcite", ""),
                                 c.get("newcite", ""))
        dbmod.replace_citations(conn, record["id"], kept)
    return kept, dropped
