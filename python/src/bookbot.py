# -*- coding: utf-8 -*-
#
# Core book-linking logic, ported from awk/cli.awk. Deliberately free of Flask
# and config dependencies so it can be unit-tested in isolation.
#
# A "page record" here is the dict returned by db.get_page / db.get_archive_page:
#   {id, page, count, ref_count, sim_count, book_count, done, citations: [...]}
# where each citation is {oldcite, newcite, iaid, meta}.

import re

# Matches the archive.org "/details/<id>/page/<n>" URL inside a newcite.
# Ported from the awk regex: https://archive.org/details/[^/]*/page/[^ ]*[^ ]
_IA_PAGE_URL = re.compile(r"https://archive\.org/details/[^/]*/page/[^ ]*[^ ]")


def archive_url(citation):
    """The archive.org URL to highlight for a citation (ported from makepreview).

    Prefer the explicit /details/<id>/page/<n> URL found in the newcite; fall
    back to a bare /details/<iaid> URL built from the iaid field.
    """
    m = _IA_PAGE_URL.search(citation.get("newcite", ""))
    if m:
        return m.group(0)
    return "https://archive.org/details/" + citation.get("iaid", "")


def colorcite(cite, iaurl):
    """Wrap the archive.org URL(s) in the new cite with red <mark> highlight.

    Faithful port of awk colorcite(). gsubs in awk is a literal (non-regex)
    string replace, so we use str.replace here. When the highlighted URL is a
    /page/ URL, both the bare /details/<id> URL (e.g. in |url=) and the
    /page/<n> URL get highlighted; the __HIDE__ sentinel keeps the bare-URL
    replacement from corrupting the /page/ URL's prefix.
    """
    mark = '<mark class="red"><a href="%s">%s</a></mark>'
    if "/page/" in iaurl:
        cite = cite.replace(iaurl, "__HIDE__")
        plain = re.sub(r"/page/.*$", "", iaurl)
        cite = cite.replace(plain, mark % (plain, plain))
        cite = cite.replace("__HIDE__", mark % (iaurl, iaurl))
    else:
        cite = cite.replace(iaurl, "__HIDE__")
        cite = cite.replace("__HIDE__", mark % (iaurl, iaurl))
    return cite


def preview_rows(record, wikitext, showexpired=False):
    """Build the rows for the preview/analysis table (ported from makepreview).

    Returns (rows, numofcites, available) where rows is a list of
    {"oldcite": str, "newcite_html": str}. Citations whose oldcite is no longer
    present in the article are "expired": skipped (default) or shown with a red
    note (showexpired=True), matching the awk G["showexpired"] toggle.
    """
    citations = record.get("citations", [])
    numofcites = len(citations)
    rows = []
    expired = 0
    for c in citations:
        oldcite = c.get("oldcite", "")
        newcite = c.get("newcite", "")
        iaurl = archive_url(c)

        present = oldcite in wikitext
        if showexpired:
            if not present:
                rows.append({
                    "oldcite": oldcite,
                    "newcite_html": '<mark class="red">Old cite no longer '
                                    'visible in article. Deleted? Modified?'
                                    '</mark>',
                })
                expired += 1
                continue
        else:
            if not present:
                expired += 1
                continue

        rows.append({
            "oldcite": oldcite,
            "newcite_html": colorcite(newcite, iaurl),
        })

    available = numofcites - expired
    return rows, numofcites, available


def count_present(record, wikitext):
    """How many citations' oldcite still appears in the article.

    Ported from getpagecount: counts citations present (not occurrences).
    """
    return sum(1 for c in record.get("citations", [])
               if c.get("oldcite", "") and c.get("oldcite", "") in wikitext)


def apply_edits(record, wikitext):
    """Replace each present oldcite with its newcite in the article wiki text.

    Ported from makepagehtml. Returns (new_wikitext, count) where count is the
    number of citations that were present and replaced. Replaces all
    occurrences of each oldcite (awk gsubs replaces all).
    """
    count = 0
    for c in record.get("citations", []):
        oldcite = c.get("oldcite", "")
        newcite = c.get("newcite", "")
        if oldcite and oldcite in wikitext:
            wikitext = wikitext.replace(oldcite, newcite)
            count += 1
    return wikitext, count
