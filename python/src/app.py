# -*- coding: utf-8 -*-
#
# bup - adds archive.org/details book links to English Wikipedia citations.
#
# Originally a Flask front-end that shelled out to awk/cli.awk (+ jq/grep/sed)
# over a 223MB out.json JSONL cache. Rewritten to read/write a SQLite database
# (see db.py / migrate.py) and render Jinja templates. The per-user mwoauth
# edit flow is unchanged: edits are attributed to the logged-in Wikipedia user.
# Live wiki text is read via the MediaWiki API, signed with the logged-in
# user's OAuth token for an authenticated (higher) read rate limit (see wiki.py).
#
# 'webservice restart' after modifying .py

import os
import glob
import json
import queue
import random
import string
import threading
from datetime import date
from email.utils import formatdate
from functools import wraps, update_wrapper

import flask
from flask import Flask, request, make_response, Response, stream_with_context
import yaml
import requests
import mwoauth
from requests_oauthlib import OAuth1
from requests_toolbelt.utils import dump

from common import cache  # see common.py
import db as dbmod
import bookbot
import wiki
import wikis
import reconcile
from api import api as api_blueprint

app = flask.Flask(__name__)
# Emit real UTF-8 characters in JSON (e.g. en dash "–") instead of \uXXXX
# escapes. application/json is UTF-8 by spec, so consumers parse both identically.
app.json.ensure_ascii = False
app.register_blueprint(api_blueprint, url_prefix="/api/v1")

cache.init_app(app=app, config={"CACHE_TYPE": "filesystem",
                                 'CACHE_DIR': '/data/project/bup/www/cache'})

# Load configuration from YAML file
__dir__ = os.path.dirname(__file__)
app.config.update(
    yaml.safe_load(open(os.path.join(__dir__, 'config.yaml'))))

# Usernames that may view the dashboard's "Links added" section. The allowed
# set is this hardcoded seed UNION the entries in stats_users.txt (which ships
# empty — add more names there, no need to touch the code).
STATS_USERS_SEED = {"GreenC"}

# Articles per page in the paginated worklist views.
PAGE_SIZE = 50
# How many articles the Random view shows; cap on Search results.
RANDOM_COUNT = 25
SEARCH_LIMIT = 50

# Sidebar view modes. Titles drive the content-header heading and <title>.
VIEW_TITLES = {
    "top":       "Top articles",
    "random":    "Random",
    "watchlist": "Watchlist",
    "category":  "Category",
    "backlinks": "Backlinks",
    "search":    "Search",
}
# View shown when none is requested (the landing view).
DEFAULT_VIEW = "random"


def current_wiki():
    """The wiki id selected via ?wiki=, normalized to a known one (default
    enwiki). Threaded into templates and the read/edit paths so the codebase
    is wiki-agnostic even though only enwiki has data today."""
    return wikis.resolve(request.args.get("wiki"))


# --- Database connection (one per request, closed on teardown) ------------

def get_db():
    if "db" not in flask.g:
        flask.g.db = dbmod.connect()
    return flask.g.db


@app.teardown_appcontext
def close_db(exc):
    conn = flask.g.pop("db", None)
    if conn is not None:
        conn.close()


def log_line(filename, line):
    # Logs live alongside the database (the db/ directory).
    path = os.path.join(os.path.dirname(dbmod.db_path()), filename)
    try:
        with open(path, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def nocache(view):
    @wraps(view)
    def no_cache(*args, **kwargs):
        response = make_response(view(*args, **kwargs))
        # HTTP-date string (Werkzeug 3 wants str header values, not a datetime).
        response.headers['Last-Modified'] = formatdate(usegmt=True)
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, post-check=0, pre-check=0, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '-1'
        return response

    return update_wrapper(no_cache, view)


# from: https://github.com/dissemin/oabot/blob/master/src/app.py
def edit_wiki_page(page_name, content, access_token, summary=None, bot=False):
    auth = OAuth1(
        app.config['CONSUMER_KEY'],
        app.config['CONSUMER_SECRET'],
        access_token['key'],
        access_token['secret'])

    # Get token
    r = requests.get('https://en.wikipedia.org/w/api.php', params={
        'action': 'query',
        'meta': 'tokens',
        'format': 'json',
    }, auth=auth)
    r.raise_for_status()
    token = r.json()['query']['tokens']['csrftoken']

    data = {
        'action': 'edit',
        'title': page_name,
        'text': content,
        'summary': summary,
        'format': 'json',
        'token': token,
        'watchlist': 'nochange',
    }
    if bot:
        data['bot'] = '1'

    r = requests.post('https://en.wikipedia.org/w/api.php', data=data, auth=auth)

    if r.status_code != 200:
        ddata = dump.dump_all(r)
        with open('/data/project/bup/debug-post.txt', 'w') as f:
            print(ddata.decode('utf-8'))
        return False
    return True


def _user_agent():
    """Policy-compliant User-Agent for the logged-in user's API requests."""
    contact = app.config.get('UA_CONTACT', 'https://bup.toolforge.org')
    return wiki.build_user_agent(contact, flask.session.get('username'))


def _oauth_auth():
    """OAuth1 signer from the logged-in user's token, or None if not present
    (anonymous read at a lower rate limit)."""
    token = flask.session.get('access_token')
    if not token:
        return None
    return OAuth1(
        app.config['CONSUMER_KEY'], app.config['CONSUMER_SECRET'],
        token['key'], token['secret'])


def read_wikitext(page, wiki_id=wikis.DEFAULT_WIKI):
    """Fetch current wiki text of `page`, signed with the logged-in user's
    OAuth token (authenticated read rate limit) and a policy-compliant
    User-Agent. Falls back to an anonymous read if no token is present.
    `wiki_id` selects which wiki's API to read (defaults to enwiki)."""
    return wiki.fetch_wikitext(page, auth=_oauth_auth(),
                               user_agent=_user_agent(),
                               api_url=wikis.api_url(wiki_id))


# --- Server-Sent-Events streaming for the inline preview/apply ------------
#
# The retry-prone bit is the live wiki read (wiki._api_call backs off, possibly
# for tens of seconds). To show the user it's working — not hung — we stream:
# the read runs in a worker thread reporting each retry via a queue, while a
# request-thread generator emits SSE 'retry' events and, once the text arrives,
# does the DB/render/edit work and emits the final 'result' (or 'error') event.
# Keeping the DB/render in the request thread avoids cross-thread SQLite / Flask
# context issues (the worker only touches the network, with no Flask access).

def _sse_event(name, data):
    return "event: %s\ndata: %s\n\n" % (name, json.dumps(data))


def _stream_fetch_then(page, wiki_id, finish):
    """Stream SSE while fetching `page`'s wikitext, then call finish(wikitext)
    in the request thread to produce the final (event_name, data) to emit.
    finish() may use the DB, render templates, and post edits."""
    auth = _oauth_auth()
    user_agent = _user_agent()
    api_url = wikis.api_url(wiki_id)
    q = queue.Queue()
    box = {}

    def on_retry(attempt, wait, reason):
        q.put(("retry", {"attempt": attempt, "wait": int(round(wait)),
                         "reason": reason}))

    def worker():
        try:
            box["text"] = wiki.fetch_wikitext(
                page, auth=auth, user_agent=user_agent, api_url=api_url,
                on_retry=on_retry)
        except Exception as e:        # pragma: no cover - defensive
            box["err"] = str(e)
        q.put(("__done__", None))

    threading.Thread(target=worker, daemon=True).start()

    @stream_with_context
    def gen():
        while True:
            name, data = q.get()
            if name == "__done__":
                break
            yield _sse_event(name, data)
        if "err" in box:
            yield _sse_event("error", {"message": box["err"]})
            return
        try:
            event, data = finish(box.get("text", ""))
        except Exception as e:        # pragma: no cover - defensive
            yield _sse_event("error", {"message": str(e)})
            return
        yield _sse_event(event, data)

    resp = Response(gen(), mimetype="text/event-stream")
    resp.headers["X-Accel-Buffering"] = "no"   # don't let a proxy buffer SSE
    resp.headers["Cache-Control"] = "no-cache"
    return resp


# --- Public / auth routes (unchanged behavior) ----------------------------

@app.route('/')
def index():
    greeting = app.config['GREETING']
    username = flask.session.get('username', None)
    if not username:
        return flask.render_template(
            'index.html', username=username, greeting=greeting)
    return flask.redirect(flask.url_for('main'))


@app.route('/about')
@nocache
def about():
    greeting = app.config['GREETING']
    username = flask.session.get('username', None)
    if not username:
        return flask.render_template(
            'index.html', username=username, greeting=greeting)
    return flask.redirect(flask.url_for('static', filename='about.html'))


@app.route('/login')
def login():
    """Initiate an OAuth login."""
    consumer_token = mwoauth.ConsumerToken(
        app.config['CONSUMER_KEY'], app.config['CONSUMER_SECRET'])
    try:
        redirect, request_token = mwoauth.initiate(
            app.config['OAUTH_MWURI'], consumer_token)
    except Exception:
        app.logger.exception('mwoauth.initiate failed')
        return flask.redirect(flask.url_for('index'))
    else:
        flask.session['request_token'] = dict(zip(
            request_token._fields, request_token))
        return flask.redirect(redirect)


@app.route('/oauth-callback')
def oauth_callback():
    """OAuth handshake callback."""
    if 'request_token' not in flask.session:
        flask.flash(u'OAuth callback failed. Are cookies disabled?')
        return flask.redirect(flask.url_for('index'))

    consumer_token = mwoauth.ConsumerToken(
        app.config['CONSUMER_KEY'], app.config['CONSUMER_SECRET'])

    try:
        access_token = mwoauth.complete(
            app.config['OAUTH_MWURI'],
            consumer_token,
            mwoauth.RequestToken(**flask.session['request_token']),
            flask.request.query_string)

        identity = mwoauth.identify(
            app.config['OAUTH_MWURI'], consumer_token, access_token)
    except Exception:
        app.logger.exception('OAuth authentication failed')
    else:
        flask.session['access_token'] = dict(zip(
            access_token._fields, access_token))
        flask.session['username'] = identity['username']

    return flask.redirect(flask.url_for('index'))


@app.route('/logout')
def logout():
    """Log the user out by clearing their session."""
    flask.session.clear()
    return flask.redirect(flask.url_for('index'))


@app.route('/edit', methods=['GET'])
def edit():
    access_token = flask.session.get('access_token', None)
    edit_wiki_page(u"User:GreenC/sandbox", random.choice(string.ascii_letters),
                   access_token, u"Test edit summary")
    return flask.redirect(flask.url_for('main'))


# --- Main table -----------------------------------------------------------

@app.route('/main')
@nocache
def main():
    greeting = app.config['GREETING']
    username = flask.session.get('username', None)
    if not username:
        return flask.render_template(
            'index.html', username=username, greeting=greeting)

    wiki_id = current_wiki()
    view = request.args.get('view', DEFAULT_VIEW)
    if view not in VIEW_TITLES:
        view = DEFAULT_VIEW

    ctx = dict(
        username=username,
        view=view,
        view_title=VIEW_TITLES[view],
        wiki=wiki_id,
        wiki_label=wikis.get(wiki_id)['label'],
        wikis=wikis.selector_list(),
        has_data=wikis.has_data(wiki_id),
    )

    # Data-backed views (only the enwiki worklist is populated). watchlist /
    # category / backlinks are still placeholders until step 4.
    if ctx['has_data']:
        conn = get_db()
        if view == 'top':
            total = dbmod.worklist_total(conn)
            total_pages = max(1, -(-total // PAGE_SIZE))  # ceil division
            try:
                page = int(request.args.get('page', 1))
            except (TypeError, ValueError):
                page = 1
            page = max(1, min(page, total_pages))
            ctx['rows'] = dbmod.worklist_page(
                conn, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
            ctx['stats'] = dbmod.stats(conn)
            ctx['page'] = page
            ctx['total_pages'] = total_pages
        elif view == 'random':
            ctx['rows'] = dbmod.random_pages(conn, limit=RANDOM_COUNT)
        elif view == 'search':
            q = request.args.get('q', '').strip()
            ctype = request.args.get('type')
            if ctype not in ('book', 'sim', 'ref'):
                ctype = None
            ctx['q'] = q
            ctx['ctype'] = ctype
            ctx['search_limit'] = SEARCH_LIMIT
            if q:
                ctx['rows'] = dbmod.search_titles(
                    conn, q, ctype=ctype, limit=SEARCH_LIMIT)
        elif view in ('watchlist', 'category', 'backlinks'):
            _intersection_view(conn, ctx, view, wiki_id)

    return flask.render_template('worklist.html', **ctx)


def _intersection_view(conn, ctx, view, wiki_id):
    """Fetch a set of titles from the live wiki (the user's watchlist, a
    category's members, or an article's backlinks) and intersect it with the
    worklist via db.pages_present. Fills ctx['rows'] (impact-sorted) and
    ctx['scanned'] (how many titles were checked). category/backlinks read
    their target from ?name=; watchlist needs none."""
    ua = _user_agent()
    api_url = wikis.api_url(wiki_id)
    titles, ran = [], False

    if view == 'watchlist':
        ran = True
        titles = wiki.fetch_watchlist(
            auth=_oauth_auth(), user_agent=ua, api_url=api_url)
    else:
        name = request.args.get('name', '').strip()
        ctx['name'] = name
        if name:
            ran = True
            if view == 'category':
                cat = (name if name.lower().startswith('category:')
                       else 'Category:' + name)
                titles = wiki.fetch_category_members(
                    cat, user_agent=ua, api_url=api_url)
            else:  # backlinks
                titles = wiki.fetch_backlinks(
                    name, user_agent=ua, api_url=api_url)

    if ran:
        rows = dbmod.pages_present(conn, titles)
        rows.sort(key=lambda r: r['count'], reverse=True)
        ctx['rows'] = rows
        ctx['scanned'] = len(titles)


# --- Dashboard (TOOLS) ----------------------------------------------------

def _stats_dir():
    """Where the daily job writes booksup-stats-<year>.jsonl. Mirrors stats.py:
    www/static, the sibling of db/ (served at tools-static.wmflabs.org/bup)."""
    return os.path.join(os.path.dirname(os.path.dirname(dbmod.db_path())),
                        "static")


def _read_stats_records():
    """All daily usage records across every year file, oldest first. Missing
    files -> []. Each record: {date, urls_added, webtool:{edits,urls},
    gadget:{edits,urls}, api:{page,random,worklist,pages}}."""
    recs = []
    for path in sorted(glob.glob(os.path.join(_stats_dir(),
                                              "booksup-stats-*.jsonl"))):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        recs.append(json.loads(line))
                    except ValueError:
                        pass
        except OSError:
            pass
    recs.sort(key=lambda r: r.get("date", ""))
    return recs


def _stats_users_path():
    return os.path.join(os.path.dirname(dbmod.db_path()), "stats_users.txt")


def _normalize_user(name):
    """Drop a 'User:' prefix and surrounding whitespace, so 'User:GreenC' and
    'GreenC' compare equal."""
    name = (name or "").strip()
    if name.lower().startswith("user:"):
        name = name[5:].strip()
    return name


def stats_users():
    """Usernames allowed to see the dashboard's 'Links added' section: the
    hardcoded seed (GreenC) UNION the entries in stats_users.txt (newline-
    separated; 'User:Name' or 'Name'; blank lines and #comments ignored). The
    file is created empty (comment-only) if absent, so names can be added on
    disk without code changes and without committing them to the repo."""
    users = set(STATS_USERS_SEED)
    path = _stats_users_path()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                u = _normalize_user(line)
                if u:
                    users.add(u)
    except OSError:
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("# Usernames allowed to view the dashboard's 'Links "
                        "added' section.\n# One per line; 'User:Name' or "
                        "'Name'. GreenC is always allowed.\n")
        except OSError:
            pass
    return users


@app.route('/dashboard')
@nocache
def dashboard():
    """Corpus 'work remaining' (from the live DB), shown to everyone. The
    'Links added' usage section (from the published daily JSONL) is shown only
    to users in stats_users(): year-scoped totals + API counts, plus a one-month
    daily bar chart with prev/next month navigation. CSS bars, no chart lib."""
    username = flask.session.get('username', None)
    if not username:
        return flask.render_template(
            'index.html', username=username, greeting=app.config['GREETING'])

    wiki_id = current_wiki()
    corpus = dbmod.stats(get_db()) if wikis.has_data(wiki_id) else None
    can_view_stats = _normalize_user(username) in stats_users()

    ctx = dict(
        username=username, view='dashboard', wiki=wiki_id,
        wiki_label=wikis.get(wiki_id)['label'], wikis=wikis.selector_list(),
        has_data=wikis.has_data(wiki_id), corpus=corpus,
        can_view_stats=can_view_stats)

    if can_view_stats:
        recs = _read_stats_records()
        years = sorted({r["date"][:4] for r in recs if r.get("date")})
        months = sorted({r["date"][:7] for r in recs if r.get("date")})

        # Year filter -> totals + API counts. 'all' (default) or a specific year.
        year = request.args.get('year', 'all')
        if year != 'all' and year not in years:
            year = 'all'
        trecs = (recs if year == 'all'
                 else [r for r in recs if r.get("date", "").startswith(year)])

        # Month -> the daily bar chart (one month, switchable). Default: latest.
        month = request.args.get('month')
        if month not in months:
            month = months[-1] if months else None
        mrecs = [r for r in recs
                 if month and r.get("date", "").startswith(month)]
        idx = months.index(month) if month in months else -1
        prev_month = months[idx - 1] if idx > 0 else None
        next_month = months[idx + 1] if 0 <= idx < len(months) - 1 else None

        api_totals = {}
        for r in trecs:
            for k, v in (r.get("api") or {}).items():
                api_totals[k] = api_totals.get(k, 0) + int(v or 0)

        ctx.update(
            years=years, year=year, months=months, month=month,
            prev_month=prev_month, next_month=next_month,
            recent=mrecs,
            max_added=max([int(r.get("urls_added", 0) or 0) for r in mrecs],
                          default=0),
            total_added=sum(int(r.get("urls_added", 0) or 0) for r in trecs),
            total_webtool=sum(int((r.get("webtool") or {}).get("urls", 0) or 0)
                              for r in trecs),
            total_gadget=sum(int((r.get("gadget") or {}).get("urls", 0) or 0)
                             for r in trecs),
            api_totals=api_totals, days=len(trecs))

    return flask.render_template('dashboard.html', **ctx)


# --- Inline preview fragment (for the in-row expand in the worklist) ------

@app.route('/preview-fragment/<int:id>')
@nocache
def preview_fragment(id):
    """Stream the proposed-change diff for one worklist row as an HTML fragment
    (no layout), for the inline expand. SSE: 'retry' events while the live read
    backs off, then a 'result' event whose data is {"html": ...}. Non-streaming
    early returns (not-logged-in / missing row) are handled directly."""
    username = flask.session.get('username', None)
    if not username:
        return ("Not logged in.", 403)

    record = dbmod.get_page(get_db(), id)
    if record is None:
        return flask.render_template(
            'preview_fragment.html', available=0, page="", rows=[], page_id=id)

    wiki_id = current_wiki()

    def finish(wikitext):
        rows, numofcites, available = bookbot.preview_rows(record, wikitext)
        if available == 0:
            # No oldcites remain -> prune (drops the page), same as /preview.
            reconcile.reconcile_page(get_db(), record, wikitext)
        html = flask.render_template(
            'preview_fragment.html', available=available, page=record['page'],
            rows=rows, page_id=id, wiki=wiki_id)
        return ("result", {"html": html})

    return _stream_fetch_then(record['page'], wiki_id, finish)


# --- Apply (JSON, for the inline Confirm & save) --------------------------

@app.route('/apply/<int:id>', methods=['POST'])
@nocache
def apply(id):
    """Apply a worklist row's edits, streaming SSE 'retry' events during the
    live read and a final 'result' event with the JSON status dict (for
    bup-ui.js). Shares _apply_with_wikitext with the HTML run-bot route."""
    username = flask.session.get('username', None)
    if not username:
        return flask.jsonify({"status": "error",
                              "message": "Not logged in."}), 403

    record = dbmod.get_page(get_db(), id)
    if record is None:
        return flask.jsonify({"status": "none", "count": 0,
                              "message": "Already resolved."}), 404

    # Optional {"indices": [...]} body selects which citations to apply
    # (the inline Add/Skip choice). Missing/invalid -> apply all.
    indices = None
    data = request.get_json(silent=True)
    if isinstance(data, dict) and isinstance(data.get("indices"), list):
        indices = [i for i in data["indices"] if isinstance(i, int)]

    def finish(wikitext):
        res = _apply_with_wikitext(record, username, wikitext, indices)
        return ("result", res)

    return _stream_fetch_then(record['page'], current_wiki(), finish)


# --- Apply implementation -------------------------------------------------

def _apply_with_wikitext(record, username, wikitext, indices=None):
    """Apply edits against ALREADY-FETCHED `wikitext`, post, then prune the
    resolved citations (logging them). Returns the status dict the streaming
    /apply route emits to the inline UI:

        {"status": "ok"|"none"|"error", "count": int, "page": str}

    'none'  = no selected candidate's oldcite still matched (page may be pruned).
    'error' = the edit POST failed.

    `indices` selects which citations to apply, by position in
    record["citations"] (the inline UI's Add/Skip choice); None = apply all
    (fallback if no selection was sent). Skipped citations keep their oldcite in
    the posted text, so reconcile leaves them as open work; only applied ones
    are pruned.

    Split from the fetch so the streaming route can run the (retry-prone) read
    in a worker thread and this DB/edit work in the request thread.
    """
    page = record['page']
    citations = record['citations']
    if indices is None:
        selected = citations
    else:
        selected = [citations[i] for i in indices if 0 <= i < len(citations)]

    present_before = set(c['oldcite'] for c in selected
                         if c.get('oldcite') and c['oldcite'] in wikitext)
    # Replace only the selected citations; reconcile (below) still runs against
    # the FULL record so skipped-but-present citations are correctly retained.
    sel_record = dict(record, citations=selected)
    new_content, count = bookbot.apply_edits(sel_record, wikitext)

    if count == 0:
        # Nothing matched. Reconcile against the text we just read: prune any
        # citations already gone (drops the page if it empties).
        reconcile.reconcile_page(get_db(), record, wikitext)
        log_line('errorlog.txt',
                 "%s ---- %d ---- No active cites found (2)" % (page, count))
        return {"status": "none", "count": 0, "page": page}

    access_token = flask.session.get('access_token', None)
    summary = "Added book" if count == 1 else "Added books"

    if edit_wiki_page(page, new_content, access_token, summary):
        # Citations bup just applied (present before, now replaced) -> bupUI;
        # reconcile against the content we posted (prunes them, drops the page).
        reconcile.reconcile_page(get_db(), record, new_content,
                                 applied_oldcites=present_before)
        log_line('log.txt', "%s ---- %s ---- %d ---- %s ---- Success"
                 % (page, username, count, date.today()))
        return {"status": "ok", "count": count, "page": page}

    log_line('errorlog.txt', "%s ---- %s ---- %d ---- %s ---- Error posting"
             % (page, username, count, date.today()))
    return {"status": "error", "count": count, "page": page}
