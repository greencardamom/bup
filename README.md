# bup — Books Up!

**bup** adds [archive.org](https://archive.org) book and journal links to
citations on the English Wikipedia, so readers can jump straight to the cited
page of a scanned source. It runs on Wikimedia Toolforge as a human-reviewed
editing tool and as a read-only data API.

- **Live tool:** https://bup.toolforge.org
- **API:** https://bup.toolforge.org/api/v1 — see [`api/README.md`](api/README.md)
- **Gadget:** [User:GreenC/BooksUp](https://en.wikipedia.org/wiki/User:GreenC/BooksUp) — on-wiki **BooksUp** user script
- **Maintainer:** [User:GreenC](https://en.wikipedia.org/wiki/User:GreenC)

---

## What it does

Many Wikipedia citations name a book or journal that Internet Archive has
digitized, but link nowhere. bup proposes a replacement citation that adds the
exact `archive.org/details/<id>/page/<n>` link — turning a dead reference into
one a reader can open and verify.

The actual matching (citation → archive.org item + page number) is done offline
by a separate scanning tool (see [Data source](#data-source)). **This** project
is the front end: it stores those precomputed proposals, lets a logged-in editor
review and apply them, and serves them over an API.

## How it works

1. **Precompute** — an offline tool scans enwiki citations, finds archive.org
   matches, and emits `out.json`: for each article, a list of
   `{oldcite, newcite, iaid, meta}` candidates. `oldcite` is the *exact* current
   citation wikitext; `newcite` is the same citation with the archive.org link
   added.
2. **Import** — `migrate.py` loads `out.json` into a SQLite database
   (`db/bup.db`, a single `pages` table = the worklist).
3. **Review & apply** — a Wikipedia editor logs in via OAuth, previews the
   proposed change for an article, and runs the bot. The edit is made through
   the **logged-in user's** OAuth credentials, so it is attributed to them.
4. **Match literally** — bup only applies a candidate if `oldcite` is still
   present in the live article *verbatim* (exact whitespace/punctuation). If the
   citation has changed, the candidate no longer matches and is skipped.
5. **Stay current** — a daily job (`verify.py`) re-checks the worklist against
   live articles and **prunes** any citation whose `oldcite` is gone (applied by
   anyone, or edited away). The database therefore holds only open work.

> **Candidates, not commands.** Everything bup stores is a *proposal* tied to an
> exact `oldcite` string. Consumers (including the API) must re-check the live
> article before applying.

## Components

| File | Role |
|------|------|
| `python/src/app.py` | Flask app: routes, OAuth login, preview/run-bot, edits |
| `python/src/api.py` | Read-only JSON API blueprint (`/api/v1`) |
| `python/src/db.py` | SQLite data layer (the `pages` worklist) |
| `python/src/bookbot.py` | Core citation logic (literal match, preview, apply) |
| `python/src/wiki.py` | MediaWiki API reads (single signed read + batch reader) |
| `python/src/reconcile.py` | Prune resolved citations + write audit logs |
| `python/src/verify.py` | Daily reconciler job (batched live-article re-check) |
| `python/src/auditlog.py` | Append-only flat logs (`removed.log`, `edits.log`) |
| `python/src/migrate.py` | Import `out.json` → `bup.db` |
| `python/src/templates/`, `static/` | Jinja2 UI templates and assets |
| `python/src/stats.py` | Daily usage-stats job (see `stats/README.md`) |
| `gadget/BooksUp.js` | On-wiki BooksUp user script (a client of the API) |

**Stack:** Python 3.11, Flask 3.x, SQLite, Jinja2, mwoauth — on a Toolforge
Kubernetes webservice.

## API

A read-only JSON API for on-wiki gadgets and external bots (CORS-enabled):

```
GET /api/v1/page/<title>     candidates for one article
GET /api/v1/worklist         browse the worklist (paginated)
GET /api/v1/stats            corpus totals
GET /api/v1/health           liveness
```

Full reference: [`api/README.md`](api/README.md).

## Gadget (BooksUp)

**BooksUp** is an on-wiki user script — a client of the API above — that shows
bup's suggestions while you read or edit an article and applies the ones you
accept in the normal edit window (you review and save). It also helps you *find*
articles that have suggestions: a random one, ones on your watchlist, or the
full worklist.

- Source: [`gadget/BooksUp.js`](gadget/BooksUp.js)
- Install & usage: [User:GreenC/BooksUp](https://en.wikipedia.org/wiki/User:GreenC/BooksUp)

## Usage statistics

Daily usage counts — chiefly the number of archive.org links added to Wikipedia
via the tool — are published as JSON Lines at
`https://tools-static.wmflabs.org/bup/booksup-stats-<year>.jsonl`. See
[`stats/README.md`](stats/README.md).

## Repository layout

```
python/src/        application code, templates, static assets
  config.yaml.example   copy to config.yaml and fill in (gitignored)
  requirements.txt      Python dependencies
api/README.md      API documentation
stats/README.md    usage-statistics documentation
gadget/            BooksUp on-wiki user script (BooksUp.js) + its doc (BooksUp.wiki)
db/                SQLite db + data (gitignored; built on deploy)
cache/             Flask filesystem cache (gitignored)
LICENSE.md         GPL-3.0 (code) / CC-BY-SA-4.0 (docs)
```

## Deployment (Toolforge)

bup runs as a `python3.11` webservice. In outline:

```bash
# 1. code is pulled from this repo into the tool's www/ directory
# 2. build the virtualenv (inside the python3.11 image) from requirements.txt
# 3. build the database from out.json
python migrate.py
# 4. start / restart the web service
webservice python3.11 restart
```

The daily reconciler is a scheduled Toolforge job:

```bash
toolforge jobs run verify --image python3.11 --schedule "@daily" \
  --command ".../venv/bin/python .../src/verify.py" --mount all
```

## Data source

The citation→archive.org matching is produced by a separate offline pipeline
(not in this repo) that scans English Wikipedia and queries Internet Archive's
full-text index, emitting `out.json`. Rebuilding the full corpus is expensive, so `bup.db` is refreshed only occasionally; the daily
`verify.py` job keeps the worklist accurate in between.

## License

- **Code** — [GPL-3.0](LICENSE.md)
- **Documentation & content** — [CC-BY-SA-4.0](LICENSE.md)

© 2026 Greencardamom
