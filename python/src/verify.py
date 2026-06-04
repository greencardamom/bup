# -*- coding: utf-8 -*-
#
# Daily reconciler. Walks the worklist, batch-fetches each page's current wiki
# text from the MediaWiki API (~BATCH titles per call), and prunes any citation
# whose literal `oldcite` is no longer present (logging removals + inferred
# applications via reconcile.py). Keeps bup.db fresh between the rare, expensive
# full rebuilds, capturing edits by anyone (gadget users, bots, manual editors).
#
# Run as a Toolforge scheduled job:
#   toolforge jobs run verify --image python3.11 --schedule "@daily" \
#       --command "$HOME/www/python/venv/bin/python $HOME/www/python/src/verify.py" \
#       --mount all
#
# Reads are anonymous (no user OAuth token), so they are batched + paced +
# maxlag'd with a descriptive User-Agent to stay well under rate limits.
# Safety: a page is only pruned on a clearly-valid read (looks_like_article);
# a failed/empty/redirect read is skipped and retried next run -- a bad read
# never deletes data.

import sys
import time
import argparse

import db as dbmod
import wiki
import reconcile

BATCH = 40          # titles per API call (<=50 for anonymous)
PAUSE = 1.0         # seconds between API calls
UA_CONTACT = "https://bup.toolforge.org"


def main():
    ap = argparse.ArgumentParser(description="Reconcile bup.db against live wiki")
    ap.add_argument("--db", default=None)
    ap.add_argument("--batch", type=int, default=BATCH)
    ap.add_argument("--pause", type=float, default=PAUSE)
    ap.add_argument("--max-pages", type=int, default=0,
                    help="stop after N pages (0 = whole worklist; for testing)")
    args = ap.parse_args()

    conn = dbmod.connect(args.db)
    ua = wiki.build_user_agent(UA_CONTACT)

    after_id = 0
    seen = pruned = pages_emptied = skipped = 0
    while True:
        if args.max_pages and seen >= args.max_pages:
            break
        rows = dbmod.fetch_page_batch(conn, after_id, args.batch)
        if not rows:
            break
        after_id = rows[-1]["id"]
        contents = wiki.fetch_wikitext_batch(
            [r["page"] for r in rows], user_agent=ua)
        for rec in rows:
            seen += 1
            content = contents.get(rec["page"])
            if not wiki.looks_like_article(content):
                skipped += 1
                continue
            open_cites, gone = reconcile.reconcile_page(conn, rec, content)
            pruned += len(gone)
            if gone and not open_cites:
                pages_emptied += 1
        time.sleep(args.pause)

    conn.close()
    print("verify: seen=%d citations_pruned=%d pages_emptied=%d skipped=%d"
          % (seen, pruned, pages_emptied, skipped))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
