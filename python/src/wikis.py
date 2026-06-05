# -*- coding: utf-8 -*-
#
# Registry of wikis bup can operate on.
#
# bup is currently English-Wikipedia-only: the offline matcher produces data
# for enwiki and the `pages` table has no `wiki` dimension yet. But the UI is
# being built multi-wiki-ready (a wiki selector in the header), so the wiki is
# threaded through the read/edit paths as an explicit parameter from the start.
# Adding a second wiki later means flipping `has_data` here (plus giving the
# database a wiki column) -- not rewiring call sites.
#
#   id        : stable internal key (matches the Wiki Replicas db name sans _p)
#   label     : what the selector shows
#   api_url   : action=API endpoint used for reads/edits/intersections
#   has_data  : True if the worklist has entries for this wiki (only enwiki now)

WIKIS = {
    "enwiki": {
        "label": "en.wikipedia.org",
        "api_url": "https://en.wikipedia.org/w/api.php",
        "has_data": True,
    },
    "dewiki": {
        "label": "de.wikipedia.org",
        "api_url": "https://de.wikipedia.org/w/api.php",
        "has_data": False,
    },
    "frwiki": {
        "label": "fr.wikipedia.org",
        "api_url": "https://fr.wikipedia.org/w/api.php",
        "has_data": False,
    },
}

DEFAULT_WIKI = "enwiki"


def get(wiki_id):
    """The registry entry for `wiki_id`, or the default wiki's entry if the id
    is unknown/missing. Never raises -- callers can trust the result."""
    return WIKIS.get(wiki_id) or WIKIS[DEFAULT_WIKI]


def resolve(wiki_id):
    """Normalize an incoming (possibly bogus) wiki id to a known one."""
    return wiki_id if wiki_id in WIKIS else DEFAULT_WIKI


def api_url(wiki_id):
    """The action-API endpoint for `wiki_id` (default wiki's if unknown)."""
    return get(wiki_id)["api_url"]


def has_data(wiki_id):
    return get(wiki_id)["has_data"]


def selector_list():
    """[(id, label, has_data)] in registry order, for the header dropdown."""
    return [(k, v["label"], v["has_data"]) for k, v in WIKIS.items()]
