# -*- coding: utf-8 -*-
#
# MediaWiki API access for bup: article-text reads, batch reads for the
# verifier, and the list queries (watchlist/category/backlinks) behind the
# intersection views.
#
# Reads are signed with the logged-in user's OAuth token (passed in from
# app.py), so they use that user's *authenticated* read rate limit -- which
# avoids the 429s the WMF edge hands out for anonymous reads. This reuses the
# per-user token the app already holds for edits; no separate/fixed bot OAuth
# identity is needed.
#
# The text content cannot come from the SQL Wiki Replicas: the `text` table
# (revision content) is not replicated. The API is the only live source.
#
# Backoff: 429s/503s are assumed LIKELY (the WMF edge sheds load aggressively).
# Every query goes through _api_call, which on each rejection pauses longer AND
# raises its maxlag tolerance upward, so a request eventually lands during a lag
# spike while still being a good citizen (strict maxlag) on the first attempt.

import re
import time

import requests

API_URL = "https://en.wikipedia.org/w/api.php"

_REDIRECT_RE = re.compile(r"^\s*#redirect", re.IGNORECASE)


def looks_like_article(content):
    """Safety gate for the verifier: only treat content as a real article (and
    thus trust an absent oldcite) if the fetch clearly succeeded. Rejects empty
    / tiny / missing pages and redirects, so a bad read never prunes."""
    if not content or len(content) < 200:
        return False
    if _REDIRECT_RE.match(content):
        return False
    return True

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


# --- Centralized request + escalating backoff -----------------------------
#
# Shared by every API query below. Adapted from a long-tested awk retry loop:
#   - maxlag starts at 10 and escalates LINEARLY (+5 per attempt), so the first
#     try is a strict good citizen and later tries grow more lag-tolerant.
#   - waits are linear and patient (tens of seconds), keyed to the failure: the
#     WMF edge sheds load by handing out 429s, busy 503s, Varnish HTML error
#     pages, and truncated/empty payloads -- all of which we detect and retry.
#   - a real API error (any other "error.code") is NOT retried -- it is returned
#     so the caller degrades instead of hammering.
# Two retry budgets: interactive (foreground web requests, kept short enough not
# to hang a worker) and batch (the background verifier, which can afford to wait
# like the bot the algorithm came from).

INITIAL_MAXLAG = 10        # replication-lag tolerance on the first try
MAXLAG_STEP = 5            # ... grows by this each retry (linear, uncapped)
INTERACTIVE_RETRIES = 8    # foreground reads (preview/apply, intersection views)
BATCH_RETRIES = 20         # background verifier (matches the awk bot's wiki tries)


def _retry_after(resp, default):
    if resp is None:
        return default
    try:
        return float(resp.headers.get("Retry-After", default))
    except (TypeError, ValueError):
        return default


def _evaluate(resp, n):
    """Classify one response. Returns (kind, data, wait, reason):
      kind 'ok'    -> data is the parsed JSON (success)
      kind 'fatal' -> data is the parsed JSON or None; do not retry
      kind 'retry' -> back off `wait` seconds and try again (`reason` is a short
                      human label for the failure, surfaced to the UI)
    `n` is the 1-based number of the attempt that just finished (wait scales
    with it). Detects, beyond HTTP status: ratelimited/maxlag error codes,
    transient mwoauth errors, Varnish HTML gateway pages, and truncated/empty
    JSON payloads -- the failure modes the awk loop learned to ride out."""
    if resp is None:
        return ("retry", None, 15 + n * 10, "no response")     # network timeout
    if resp.status_code == 429:
        return ("retry", None, 15 + n * 10, "rate limited")
    if resp.status_code == 503:
        return ("retry", None, 15 + n * 5, "server busy")      # maxlag-as-503
    if resp.status_code != 200:
        return ("fatal", None, 0, "")                          # other HTTP error

    body = resp.text or ""
    if not body.strip():
        return ("retry", None, 15 + n * 10, "empty response")  # gateway drop
    head = body.lstrip()[:14].lower()
    if head.startswith("<!doctype html") or head.startswith("<html"):
        return ("retry", None, 15 + n * 5, "gateway error")    # Varnish HTML page
    try:
        data = resp.json()
    except ValueError:
        return ("retry", None, 15 + n * 10, "incomplete response")  # truncated

    err = data.get("error") if isinstance(data, dict) else None
    if err:
        code = err.get("code", "")
        if code == "maxlag":
            return ("retry", None, 15 + n * 10, "replication lag")
        if code == "ratelimited":
            return ("retry", None, 15 + n * 10, "rate limited")
        if code.startswith("mwoauth-"):
            return ("retry", None, 10 + n * 5, "auth retry")   # transient OAuth drop
        return ("fatal", data, 0, "")                          # genuine API error
    return ("ok", data, 0, "")


def _api_call(api_url, params, auth=None, user_agent=None, method="GET",
              timeout=120, max_retries=INTERACTIVE_RETRIES, on_retry=None):
    """Issue ONE logical API query and return the parsed JSON dict, or None on
    failure/exhaustion. `params` is copied; `maxlag` is set per attempt (10, 15,
    20, ...). Honors Retry-After (uses the larger of it and the linear wait).
    `on_retry(attempt, wait, reason)` is called (if given) just before each
    backoff sleep, so callers can surface progress (e.g. stream it to the UI);
    `attempt` is the 1-based number of the try that just failed."""
    headers = {"User-Agent": user_agent} if user_agent else {}
    p = dict(params)

    for attempt in range(max_retries + 1):
        p["maxlag"] = INITIAL_MAXLAG + attempt * MAXLAG_STEP
        try:
            if method == "POST":
                resp = requests.post(api_url, data=p, auth=auth, headers=headers,
                                     timeout=timeout)
            else:
                resp = requests.get(api_url, params=p, auth=auth, headers=headers,
                                    timeout=timeout)
        except requests.RequestException:
            resp = None

        kind, data, wait, reason = _evaluate(resp, attempt + 1)
        if kind in ("ok", "fatal"):
            return data
        if attempt >= max_retries:
            return None
        wait = max(wait, _retry_after(resp, 0))
        if on_retry:
            try:
                on_retry(attempt + 1, wait, reason)
            except Exception:
                pass  # progress reporting must never break the fetch
        time.sleep(wait)

    return None


# --- Article-text reads ----------------------------------------------------

def fetch_wikitext(page, auth=None, user_agent=None, timeout=60,
                   max_retries=INTERACTIVE_RETRIES, api_url=API_URL,
                   on_retry=None):
    """Return the current wiki text of `page`, or "" on any failure.

    auth        : a requests OAuth1 object for an authenticated read, or None
                  to read anonymously (lower rate limit).
    user_agent  : a policy-compliant UA string (strongly recommended).
    api_url     : action-API endpoint (defaults to enwiki; pass another wiki's
                  endpoint for a multi-wiki read).
    on_retry    : optional callback(attempt, wait, reason) for backoff progress.

    Follows redirects (matching the old `wikiget -w`). Returns "" (not None) so
    callers treat a missing/failed fetch the same as an empty article.
    """
    params = {
        "action": "query",
        "prop": "revisions",
        "titles": page,
        "rvslots": "main",
        "rvprop": "content",
        "rvlimit": 1,
        "redirects": 1,
        "format": "json",
        "formatversion": 2,
    }
    data = _api_call(api_url, params, auth=auth, user_agent=user_agent,
                     method="GET", timeout=timeout, max_retries=max_retries,
                     on_retry=on_retry)
    if data is None:
        return ""
    return _extract_content(data)


def fetch_wikitext_batch(titles, user_agent=None, timeout=90,
                         max_retries=BATCH_RETRIES, api_url=API_URL):
    """Fetch current wiki text for many titles (<=50) in ONE API call.

    Returns {requested_title: content-or-None}. Titles are looked up exactly;
    redirects are NOT followed (a redirect page returns its own '#redirect ...'
    text, which looks_like_article() then rejects). On any failure returns the
    dict with None values (caller skips those, retries next run).

    Uses POST: 40 long, URL-encoded titles can exceed the ~8 KB request-line
    limit on a GET, so the params go in the request body (the MediaWiki API
    recommends POST for many titles; reads work fine over POST).
    """
    result = {t: None for t in titles}
    if not titles:
        return result
    params = {
        "action": "query",
        "prop": "revisions",
        "titles": "|".join(titles),
        "rvslots": "main",
        "rvprop": "content",
        "format": "json",
        "formatversion": 2,
    }
    data = _api_call(api_url, params, user_agent=user_agent, method="POST",
                     timeout=timeout, max_retries=max_retries)
    if data is None:
        return result

    query = data.get("query", {})
    # The API normalizes some titles (underscores, capitalization); map the
    # returned (normalized) title back to what we asked for.
    norm = {n.get("from"): n.get("to") for n in query.get("normalized", [])}
    by_title = {}
    for pg in query.get("pages", []):
        if pg.get("missing"):
            continue
        try:
            by_title[pg["title"]] = \
                pg["revisions"][0]["slots"]["main"]["content"]
        except (KeyError, IndexError, TypeError):
            continue
    for t in titles:
        result[t] = by_title.get(norm.get(t, t))
    return result


def fetch_revids_batch(titles, user_agent=None, timeout=60,
                       max_retries=BATCH_RETRIES, api_url=API_URL):
    """Fetch the current lastrevid for many titles (<=50) in ONE API call via
    prop=info (tiny payloads — no article body). Returns
    {requested_title: lastrevid-or-None}; None = missing page or fetch failure
    (caller skips). POST (same reasons as fetch_wikitext_batch); redirects not
    followed.

    Phase 1 of the verifier: compare these against the stored `revid` to find
    which pages were edited, so only those need a content fetch.
    """
    result = {t: None for t in titles}
    if not titles:
        return result
    params = {
        "action": "query",
        "prop": "info",
        "titles": "|".join(titles),
        "format": "json",
        "formatversion": 2,
    }
    data = _api_call(api_url, params, user_agent=user_agent, method="POST",
                     timeout=timeout, max_retries=max_retries)
    if data is None:
        return result

    query = data.get("query", {})
    norm = {n.get("from"): n.get("to") for n in query.get("normalized", [])}
    by_title = {}
    for pg in query.get("pages", []):
        if pg.get("missing"):
            continue
        rid = pg.get("lastrevid")
        if rid is not None:
            by_title[pg["title"]] = int(rid)
    for t in titles:
        result[t] = by_title.get(norm.get(t, t))
    return result


# --- List queries for the intersection views (watchlist/category/backlinks) --
#
# Each returns a list of main-namespace article titles, capped at `max_titles`,
# following the API `continue` protocol (each page via _api_call, so the same
# escalating backoff applies). The titles are then intersected with the worklist
# via db.pages_present (the heavy lifting stays in SQLite). `api_url` keeps these
# wiki-agnostic.

def _fetch_titles(base_params, extract, auth=None, user_agent=None,
                  api_url=API_URL, max_titles=5000, timeout=60,
                  max_retries=INTERACTIVE_RETRIES):
    """Run a paginated list query, accumulating titles from `extract(data)`
    until exhausted or `max_titles`. Returns whatever was gathered when a page
    fails (callers treat a short/empty list as "no intersection found")."""
    params = dict(base_params)
    titles = []

    for _ in range(200):  # hard stop against pathological continue loops
        data = _api_call(api_url, params, auth=auth, user_agent=user_agent,
                         method="GET", timeout=timeout, max_retries=max_retries)
        if data is None:
            return titles

        for item in extract(data):
            t = item.get("title")
            if t:
                titles.append(t)
        if len(titles) >= max_titles:
            return titles[:max_titles]

        cont = data.get("continue")
        if not cont:
            return titles
        params.update(cont)

    return titles


def fetch_watchlist(auth=None, user_agent=None, api_url=API_URL, max_titles=5000):
    """Main-namespace titles on the logged-in user's watchlist. Requires an
    authenticated `auth` (the watchlist is per-user); returns [] without it."""
    if auth is None:
        return []
    params = {"action": "query", "list": "watchlistraw", "wrnamespace": 0,
              "wrlimit": "max", "format": "json", "formatversion": 2}

    def extract(data):
        return (data.get("watchlistraw")
                or data.get("query", {}).get("watchlistraw", []) or [])

    return _fetch_titles(params, extract, auth=auth, user_agent=user_agent,
                         api_url=api_url, max_titles=max_titles)


def fetch_category_members(category, user_agent=None, api_url=API_URL,
                           max_titles=5000):
    """Main-namespace page titles in `category` (full title, e.g.
    'Category:1980s films'). Subcategories/files are excluded (cmtype=page)."""
    params = {"action": "query", "list": "categorymembers", "cmtitle": category,
              "cmnamespace": 0, "cmtype": "page", "cmlimit": "max",
              "format": "json", "formatversion": 2}

    def extract(data):
        return data.get("query", {}).get("categorymembers", [])

    return _fetch_titles(params, extract, user_agent=user_agent,
                         api_url=api_url, max_titles=max_titles)


def fetch_backlinks(title, user_agent=None, api_url=API_URL, max_titles=5000):
    """Main-namespace pages that link to `title` (the "What links here" set)."""
    params = {"action": "query", "list": "backlinks", "bltitle": title,
              "blnamespace": 0, "bllimit": "max",
              "format": "json", "formatversion": 2}

    def extract(data):
        return data.get("query", {}).get("backlinks", [])

    return _fetch_titles(params, extract, user_agent=user_agent,
                         api_url=api_url, max_titles=max_titles)


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
