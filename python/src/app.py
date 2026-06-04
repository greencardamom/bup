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
import json
import random
import string
from datetime import date
from email.utils import formatdate
from functools import wraps, update_wrapper

import flask
from flask import Flask, request, make_response
import yaml
import requests
import mwoauth
from requests_oauthlib import OAuth1
from requests_toolbelt.utils import dump

from common import cache  # see common.py
import db as dbmod
import bookbot
import wiki

app = flask.Flask(__name__)

cache.init_app(app=app, config={"CACHE_TYPE": "filesystem",
                                 'CACHE_DIR': '/data/project/bup/www/cache'})

# Load configuration from YAML file
__dir__ = os.path.dirname(__file__)
app.config.update(
    yaml.safe_load(open(os.path.join(__dir__, 'config.yaml'))))

# Users allowed to run the bot on an arbitrary (on-demand) article.
ONDEMAND_USERS = {"GreenC", "Markjgraham hmb", "Brewsterkahle"}

# Number of articles shown in the main table (was G["tablelength"]).
TABLE_LENGTH = 75


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


def read_wikitext(page):
    """Fetch current wiki text of `page`, signed with the logged-in user's
    OAuth token (authenticated read rate limit) and a policy-compliant
    User-Agent. Falls back to an anonymous read if no token is present."""
    username = flask.session.get('username')
    contact = app.config.get('UA_CONTACT', 'https://bup.toolforge.org')
    user_agent = wiki.build_user_agent(contact, username)

    token = flask.session.get('access_token')
    auth = None
    if token:
        auth = OAuth1(
            app.config['CONSUMER_KEY'],
            app.config['CONSUMER_SECRET'],
            token['key'],
            token['secret'])

    return wiki.fetch_wikitext(page, auth=auth, user_agent=user_agent)


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

    rows = dbmod.worklist(get_db(), limit=TABLE_LENGTH)
    return flask.render_template(
        'main.html', username=username, rows=rows,
        can_ondemand=(username in ONDEMAND_USERS))


# --- Preview / analysis ---------------------------------------------------

@app.route('/preview/<int:id>')
@nocache
def preview(id):
    greeting = app.config['GREETING']
    username = flask.session.get('username', None)
    if not username:
        return flask.render_template(
            'index.html', username=username, greeting=greeting)

    record = dbmod.get_page(get_db(), id)
    if record is None:
        log_line('errorlog.txt',
                 "%s ---- %s ---- Error finding record in database (1)"
                 % (id, username))
        return flask.render_template(
            'message.html', message="Error finding record in database (1).")

    wikitext = read_wikitext(record['page'])
    rows, numofcites, available = bookbot.preview_rows(record, wikitext)

    if available == 0:
        dbmod.mark_done(get_db(), id)
        log_line('errorlog.txt', "%s ---- %d ---- No active cites found (1)"
                 % (record['page'], numofcites))
        return flask.render_template(
            'message.html',
            message="No active cites found (1). Article &lt;%s&gt; removed "
                    "from list." % record['page'])

    return flask.render_template(
        'preview.html', page=record['page'], rows=rows,
        numofcites=numofcites, available=available, showexpired=False)


# --- Run bot (from the worklist) ------------------------------------------

@app.route('/runbot/<int:id>')
@nocache
def runbot(id):
    greeting = app.config['GREETING']
    username = flask.session.get('username', None)
    if not username:
        return flask.render_template(
            'index.html', username=username, greeting=greeting)

    record = dbmod.get_page(get_db(), id)
    if record is None:
        return flask.redirect(flask.url_for('main'))

    return _run_bot_on_record(record, username, id_for_done=id)


# --- Run bot on an arbitrary article (on-demand) --------------------------

@app.route('/ondemand', methods=['POST'])
def ondemand():
    greeting = app.config['GREETING']
    username = flask.session.get('username', None)
    if not username:
        return flask.render_template(
            'index.html', username=username, greeting=greeting)

    pagename = request.form['text'].replace("_", " ").strip()
    if not pagename:
        return flask.redirect(flask.url_for('main'))

    # On-demand searches the full archive first (was the out.json.all grep),
    # falling back to the worklist.
    record = dbmod.get_archive_page(get_db(), pagename)
    if record is None:
        record = _record_by_title(get_db(), pagename)
    if record is None:
        return flask.redirect(flask.url_for('static', filename='nolinks.html'))

    return _run_bot_on_record(record, username, title_for_done=pagename)


# --- Shared run-bot implementation ----------------------------------------

def _record_by_title(conn, title):
    cur = conn.execute(
        "SELECT id, page, count, ref_count, sim_count, book_count, done, "
        "citations FROM pages WHERE page = ?", (title,))
    row = cur.fetchone()
    if row is None:
        return None
    d = dict(row)
    d["citations"] = json.loads(d["citations"])
    return d


def _run_bot_on_record(record, username, id_for_done=None, title_for_done=None):
    """Fetch the live article, apply edits, post, mark done. Shared by
    runbot (worklist) and ondemand. Returns a rendered response."""
    page = record['page']
    wikitext = read_wikitext(page)
    new_content, count = bookbot.apply_edits(record, wikitext)

    if count == 0:
        # Nothing to do; drop from the worklist (was "No active cites (2)").
        _mark_done(id_for_done, title_for_done, page)
        log_line('errorlog.txt',
                 "%s ---- %d ---- No active cites found (2)" % (page, count))
        return flask.render_template(
            'message.html',
            message="No active cites found (2). Article &lt;%s&gt; removed "
                    "from list." % page)

    access_token = flask.session.get('access_token', None)
    summary = "Added book" if count == 1 else "Added books"

    if edit_wiki_page(page, new_content, access_token, summary):
        _mark_done(id_for_done, title_for_done, page)
        log_line('log.txt', "%s ---- %s ---- %d ---- %s ---- Success"
                 % (page, username, count, date.today()))
        return flask.render_template('done.html', page=page, count=count)
    else:
        log_line('errorlog.txt', "%s ---- %s ---- %d ---- %s ---- Error posting"
                 % (page, username, count, date.today()))
        return flask.redirect(flask.url_for('main'))


def _mark_done(id_for_done, title_for_done, page):
    conn = get_db()
    if id_for_done is not None:
        dbmod.mark_done(conn, id_for_done)
    else:
        # On-demand: mark the worklist row done if the title is present there.
        dbmod.mark_done_by_title(conn, title_for_done or page)
