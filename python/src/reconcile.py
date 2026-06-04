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
