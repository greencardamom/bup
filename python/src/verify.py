# -*- coding: utf-8 -*-
#
# Daily reconciler. Keeps bup.db fresh between the rare, expensive full rebuilds
# by pruning any citation whose literal `oldcite` no longer appears in the live
# article (logging removals + inferred applications via reconcile.py). Captures
# edits by anyone (gadget users, bots, manual editors).
#
# Two phases per batch, so we don't re-download every article every day:
#   1. Cheap metadata: fetch each page's current lastrevid (prop=info, tiny).
#      If it matches the stored `revid`, the page hasn't been edited since we
#      last checked -> skip entirely (no content fetch).
#   2. Content: only for pages whose lastrevid changed (or were never checked),
#      fetch the full wiki text and reconcile, then record the new revid.
# After a fresh `migrate.py` every revid is 0, so the first run does a full
# content pass (re-seeding revids); subsequent runs touch only edited pages.
#
# Run as a Toolforge scheduled job:
#   toolforge jobs run verify --image python3.11 --schedule "@daily" \
#       --command "$HOME/www/python/venv/bin/python $HOME/www/python/src/verify.py" \
#       --mount all
#
# Reads are anonymous (no user OAuth token): batched + paced + maxlag'd with a
# descriptive User-Agent. Safety: a page is pruned only on a clearly-valid read
# (looks_like_article); a failed/empty/redirect read is skipped and retried next
# run -- a bad read never deletes data.

import sys
import time
import argparse

import db as dbmod
import wiki
import reconcile

BATCH = 50          # titles per API call (<=50 for anonymous)
PAUSE = 0.5         # seconds between API calls (info calls are cheap)
UA_CONTACT = "https://bup.toolforge.org"


def main():
    ap = argparse.ArgumentParser(description="Reconcile bup.db against live wiki")
    ap.add_argument("--db", default=None)
    ap.add_argument("--batch", type=int, default=BATCH)
    ap.add_argument("--pause", type=float, default=PAUSE)
    ap.add_argument("--max-pages", type=int, default=0,
                    help="stop after N pages (0 = whole worklist; for testing)")
    ap.add_argument("--rebuild", action="store_true",
                    help="check content for every page, ignoring stored revid")
    args = ap.parse_args()

    conn = dbmod.connect(args.db)
    ua = wiki.build_user_agent(UA_CONTACT)

    after_id = 0
    seen = unchanged = checked = pruned = pages_emptied = skipped = 0
    while True:
        if args.max_pages and seen >= args.max_pages:
            break
        rows = dbmod.fetch_page_batch(conn, after_id, args.batch)
        if not rows:
            break
        after_id = rows[-1]["id"]
        seen += len(rows)

        # --- Phase 1: which pages changed since we last verified them? ---
        revids = wiki.fetch_revids_batch([r["page"] for r in rows], user_agent=ua)
        changed = {}   # title -> current lastrevid
        for r in rows:
            cur = revids.get(r["page"])
            if cur is None:
                skipped += 1            # missing / fetch failure -> retry next run
                continue
            if not args.rebuild and cur == r["revid"] and r["revid"] != 0:
                unchanged += 1          # not edited since last check -> skip
                continue
            changed[r["page"]] = cur
        time.sleep(args.pause)

        if not changed:
            continue

        # --- Phase 2: content only for the changed pages, then reconcile ---
        contents = wiki.fetch_wikitext_batch(list(changed.keys()), user_agent=ua)
        by_title = {r["page"]: r for r in rows}
        for title, cur_revid in changed.items():
            content = contents.get(title)
            if not wiki.looks_like_article(content):
                skipped += 1            # bad/empty read -> don't prune, retry
                continue
            checked += 1
            rec = by_title[title]
            open_cites, gone = reconcile.reconcile_page(conn, rec, content)
            pruned += len(gone)
            if open_cites:              # page still exists -> mark verified
                dbmod.set_revid(conn, rec["id"], cur_revid)
            else:
                pages_emptied += 1
        time.sleep(args.pause)

    conn.close()
    print("verify: seen=%d unchanged=%d checked=%d citations_pruned=%d "
          "pages_emptied=%d skipped=%d"
          % (seen, unchanged, checked, pruned, pages_emptied, skipped))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
