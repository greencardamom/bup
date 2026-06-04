# -*- coding: utf-8 -*-
#
# Read current article wikitext via the MediaWiki API.
#
# Replaces the old `wikiget -w` subprocess. Reads are signed with the
# logged-in user's OAuth token (passed in from app.py), so they use that
# user's *authenticated* read rate limit -- which avoids the 429s that the
# WMF edge hands out for anonymous reads. This reuses the per-user token the
# app already holds for edits; no separate/fixed bot OAuth identity is needed.
#
# The text content cannot come from the SQL Wiki Replicas: the `text` table
# (revision content) is not replicated. The API is the only live source.

import time

import requests

API_URL = "https://en.wikipedia.org/w/api.php"

# WMF User-Agent policy:
#   https://foundation.wikimedia.org/wiki/Policy:Wikimedia_Foundation_User-Agent_Policy
# Format: <client>/<version> (<contact>) <library>/<version>
TOOL = "bup"
VERSION = "1.1"


def build_user_agent(contact, username=None):
    """Build a policy-compliant User-Agent string.

    contact  : a contact URI and/or email (e.g. "https://bup.toolforge.org;
               you@example.org"). The policy requires a way to reach the
               operator; a tool URL suffices, an email is optional.
    username : the logged-in editor, appended for accountability (optional).
    """
    ua = "%s/%s (%s) python-requests/%s" % (
        TOOL, VERSION, contact, requests.__version__)
    if username:
        ua += " user:%s" % username
    return ua


def fetch_wikitext(page, auth=None, user_agent=None, timeout=60,
                   max_retries=4):
    """Return the current wiki text of `page`, or "" on any failure.

    auth        : a requests OAuth1 object for an authenticated read, or None
                  to read anonymously (lower rate limit).
    user_agent  : a policy-compliant UA string (strongly recommended).

    Follows redirects (matching the old `wikiget -w`). Honors HTTP 429/503
    Retry-After and the API `maxlag` error, retrying with exponential backoff
    up to `max_retries` times. Returns "" (not None) so callers treat a
    missing/failed fetch the same as an empty article.
    """
    params = {
        "action": "query",
        "prop": "revisions",
        "titles": page,
        "rvslot": "main",
        "rvprop": "content",
        "rvlimit": 1,
        "redirects": 1,
        "format": "json",
        "formatversion": 2,
        "maxlag": 5,
    }
    headers = {"User-Agent": user_agent} if user_agent else {}

    delay = 1.0
    for attempt in range(max_retries + 1):
        try:
            r = requests.get(API_URL, params=params, auth=auth,
                             headers=headers, timeout=timeout)
        except requests.RequestException:
            return ""

        # Rate limited (429) or server busy / maxlag-as-503: back off.
        if r.status_code in (429, 503):
            if attempt >= max_retries:
                return ""
            time.sleep(_retry_after(r, delay))
            delay *= 2
            continue

        if r.status_code != 200:
            return ""

        try:
            data = r.json()
        except ValueError:
            return ""

        # API-level maxlag error can arrive as HTTP 200 with an error body.
        err = data.get("error")
        if err and err.get("code") == "maxlag":
            if attempt >= max_retries:
                return ""
            time.sleep(_retry_after(r, delay))
            delay *= 2
            continue

        return _extract_content(data)

    return ""


def _retry_after(resp, default):
    try:
        return float(resp.headers.get("Retry-After", default))
    except (TypeError, ValueError):
        return default


def _extract_content(data):
    """Pull main-slot content out of a formatversion=2 query response."""
    try:
        pages = data["query"]["pages"]
    except (KeyError, TypeError):
        return ""
    if not pages:
        return ""
    page = pages[0]
    if page.get("missing"):
        return ""
    try:
        return page["revisions"][0]["slots"]["main"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""
