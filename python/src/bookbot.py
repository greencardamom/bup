# -*- coding: utf-8 -*-
#
# Core book-linking logic, ported from awk/cli.awk. Deliberately free of Flask
# and config dependencies so it can be unit-tested in isolation.
#
# A "page record" here is the dict returned by db.get_page:
#   {id, page, count, ref_count, sim_count, book_count, citations: [...]}
# where each citation is {oldcite, newcite, iaid, meta}.

import re

# Matches the archive.org "/details/<id>/page/<n>" URL inside a newcite.
# The URL terminates at a citation-template/wikitext boundary -- a pipe, brace,
# bracket, angle bracket, quote, or whitespace -- NOT just a space (the awk
# original used [^ ], which bled past `|journal=...|date=August` up to the first
# real space). The page segment may contain slashes (e.g. /page/584/mode/2up).
_IA_PAGE_URL = re.compile(
    r'https://archive\.org/details/[^/\s|]+/page/[^\s|}\]<>"]+')


def archive_url(citation):
    """The archive.org URL to highlight for a citation (ported from makepreview).

    Prefer the explicit /details/<id>/page/<n> URL found in the newcite; fall
    back to a bare /details/<iaid> URL built from the iaid field.
    """
    m = _IA_PAGE_URL.search(citation.get("newcite", ""))
    if m:
        return m.group(0)
    return "https://archive.org/details/" + citation.get("iaid", "")


# A real archive.org item URL: /details/<id> with a non-empty id (not the bare
# prefix). Used to tell a genuine match from a stale/dead candidate.
_IA_DETAILS = re.compile(r"archive\.org/details/[^\s/|}\]]")


def is_viable(citation):
    """True if the candidate actually adds an archive.org link: the new cite
    must differ from the old AND contain a real /details/<id> URL.

    Guards against stale candidates whose `newcite` equals `oldcite` (the
    matched item no longer resolves, for whatever reason) -- those would
    otherwise render a preview whose 'before' and 'after' are identical with no
    link, instead of being recognized as 'no match available'."""
    old = citation.get("oldcite", "")
    new = citation.get("newcite", "")
    return bool(new) and new != old and bool(_IA_DETAILS.search(new))


def colorcite(cite, iaurl):
    """Wrap the archive.org URL(s) in the new cite with red <mark> highlight.

    Faithful port of awk colorcite(). gsubs in awk is a literal (non-regex)
    string replace, so we use str.replace here. When the highlighted URL is a
    /page/ URL, both the bare /details/<id> URL (e.g. in |url=) and the
    /page/<n> URL get highlighted; the __HIDE__ sentinel keeps the bare-URL
    replacement from corrupting the /page/ URL's prefix.
    """
    mark = ('<mark class="red"><a href="%s" target="_blank" '
            'rel="noopener noreferrer">%s</a></mark>')
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
    {"i": int, "oldcite": str, "newcite_html": str}. `i` is the citation's
    position in record["citations"] (stable id used by the inline UI's
    Add/Skip selection, so /apply can act on just the chosen citations).
    Citations whose oldcite is no longer present in the article are "expired":
    skipped (default) or shown with a red note (showexpired=True), matching the
    awk G["showexpired"] toggle.
    """
    citations = record.get("citations", [])
    numofcites = len(citations)
    rows = []
    unavailable = 0     # expired (oldcite gone) OR non-viable (adds no link)
    for idx, c in enumerate(citations):
        oldcite = c.get("oldcite", "")
        newcite = c.get("newcite", "")
        iaurl = archive_url(c)

        present = oldcite in wikitext
        if showexpired:
            if not present:
                rows.append({
                    "i": idx,
                    "oldcite": oldcite,
                    "newcite_html": '<mark class="red">Old cite no longer '
                                    'visible in article. Deleted? Modified?'
                                    '</mark>',
                })
                unavailable += 1
                continue
        else:
            if not present:
                unavailable += 1
                continue

        # Present, but the candidate no longer adds an archive.org link (its
        # 'after' == 'before'): a dead match -> drop it so the preview shows
        # only working citations.
        if not is_viable(c):
            unavailable += 1
            continue

        rows.append({
            "i": idx,
            "oldcite": oldcite,
            "newcite_html": colorcite(newcite, iaurl),
        })

    available = numofcites - unavailable
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
