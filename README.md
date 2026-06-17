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
digitized, but links nowhere. bup proposes a replacement citation that adds the
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
2. **Import** — `migrate.py` loads `out.json` into the worklist: a single
   `pages` table in **ToolsDB** (the shared MariaDB on Toolforge). A SQLite
   file backend is also selectable — see *Storage backend* below.
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
| `python/src/app.py` | Flask app: routes, OAuth login, views, inline preview/apply (SSE), dashboard, edits |
| `python/src/api.py` | Read-only JSON API blueprint (`/api/v1`) |
| `python/src/db.py` | Data layer for the `pages` worklist — ToolsDB (MariaDB) or SQLite, selected by `BUP_DB_BACKEND` |
| `python/src/wikis.py` | Wiki registry (multi-wiki-ready; only enwiki populated) |
| `python/src/bookbot.py` | Core citation logic (literal match, preview, apply) |
| `python/src/wiki.py` | MediaWiki API: signed reads, batch readers, list queries (watchlist/category/backlinks), escalating backoff |
| `python/src/reconcile.py` | Prune resolved citations + write audit logs |
| `python/src/verify.py` | Daily reconciler job (batched live-article re-check) |
| `python/src/auditlog.py` | Append-only flat logs (`removed.log`, `edits.log`) |
| `python/src/migrate.py` | Load the worklist: rebuild from `out.json`, or copy SQLite→ToolsDB preserving ids |
| `python/src/templates/`, `static/` | Jinja2 templates + hand-rolled Vector-style CSS/JS (`bup-ui.css`, `bup-ui.js`) |
| `python/src/stats.py` | Daily usage-stats job (see `stats/README.md`) |
| `gadget/BooksUp.js` | On-wiki BooksUp user script (a client of the API) |

**Stack:** Python 3.11, Flask 3.x, ToolsDB (MariaDB) / SQLite, Jinja2, mwoauth —
on a Toolforge Kubernetes webservice.

## Web interface

The tool at [bup.toolforge.org](https://bup.toolforge.org) is a
Wikipedia/Vector-styled Flask UI (hand-rolled CSS, no frontend build). After an
OAuth login, a left sidebar offers:

- **Views** — ways to find articles with suggestions:
  - **Top** — the whole worklist, most-available-links first, paginated.
  - **Random** — a random set.
  - **Watchlist / Category / Backlinks** — the worklist intersected with your
    watchlist, a category's members, or an article's "what links here".
  - **Search** — title search + citation-type filter (books / journals / refactors).
- **Tools → Dashboard** — "work remaining" corpus totals (everyone), plus, for
  users listed in `db/stats_users.txt` (newline-separated, `User:Name` or
  `Name`; `GreenC` is always allowed), a "links added via the tool" panel with
  year-scoped totals, API call counts, and a switchable per-month bar chart.

Each row's single **Run** action expands the proposed change inline (Add/Skip
per citation) and applies the accepted ones in place, **streaming live retry
progress** if the wiki API is busy. Edits are attributed to the logged-in user.
A header **wiki selector** is threaded through the read/edit paths for future
multi-language support; only the English Wikipedia worklist is populated today.

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
db/                worklist data — out.json, logs, SQLite fallback db (gitignored)
cache/             Flask filesystem cache (gitignored)
LICENSE.md         GPL-3.0 (code) / CC-BY-SA-4.0 (docs)
```

## Deployment (Toolforge)

bup runs as a `python3.11` webservice. In outline:

**One-time setup:**

```bash
# 1. code: ./bupsave.sh  (commit → push → git pull on Toolforge → restart)
# 2. build the virtualenv (inside the python3.11 image) from requirements.txt
# 3. select the worklist backend (ToolsDB in production; default is sqlite):
toolforge envvars create BUP_DB_BACKEND toolsdb
# 4. create the ToolsDB pages table:
python db.py --setup
# 5. load the worklist (see "Rebuilding the worklist" below), then start:
webservice python3.11 restart
```

### Rebuilding the worklist (recurring)

The corpus refresh **does not go away**: whenever the offline pipeline produces a
new `out.json` (see *Data source*), reload the worklist with `migrate.py`. This
is the normal, repeated path and targets whichever backend `BUP_DB_BACKEND`
selects. On Toolforge it runs in the `python3.11` image (the venv), as the tool:

```bash
# drop the new out.json into db/, then:
toolforge jobs run pages-rebuild --image python3.11 --mount all --wait 3600 \
  --command "$HOME/www/python/venv/bin/python $HOME/www/python/src/migrate.py"
```

`migrate.py` drops and recreates the `pages` table and reloads it in file order,
so ids are **renumbered**. That is expected for a corpus refresh — a fresh corpus
supersedes the old worklist, and in-flight `/apply/<id>` links were tied to the
old corpus anyway. (The id-preserving `migrate.py --copy-from` mode is only for
moving an *existing* worklist between backends, e.g. the one-time SQLite→ToolsDB
cutover — not for corpus refreshes.)

The daily reconciler is a scheduled Toolforge job:

```bash
toolforge jobs run verify --image python3.11 --schedule "@daily" \
  --command ".../venv/bin/python .../src/verify.py" --mount all
```

**Storage backend.** The worklist lives in **ToolsDB** — the shared, WMF-backed-up
MariaDB on Toolforge — as of the 2026-06-17 cutover, alongside the user prefs the
tool already keeps there (`userdb.py`). The backend is chosen at runtime by
`BUP_DB_BACKEND` (`toolsdb` | `sqlite`); the original SQLite-on-NFS file
(`db/bup.db`) remains a selectable fallback and the rollback target. Design and
runbook: `docs/toolsdb-migration.md`, `docs/toolsdb-cutover.md`.

SQLite on NFS was the original store but is single-host: `bup.db` was written
from two hosts — the webservice pod and the `verify` job pod — which is the
hazard ToolsDB removes. In SQLite mode `db.connect()` uses rollback-journal mode
(**not** WAL — WAL's shared-memory index is not coherent across hosts and
corrupted the file once); a daily `backup.py` job kept integrity-checked
snapshots in `$HOME/backups`. On ToolsDB that backup job is unnecessary (WMF
backs ToolsDB up centrally) and is retired after the post-cutover grace period.

## Data source

The citation→archive.org matching is produced by a separate offline pipeline
(not in this repo) that scans English Wikipedia and queries Internet Archive's
full-text index, emitting `out.json`. Rebuilding the full corpus is expensive, so
the worklist is refreshed only occasionally (see *Rebuilding the worklist*); the
daily `verify.py` job keeps the worklist accurate in between.

## License

- **Code** — [GPL-3.0](LICENSE.md)
- **Documentation & content** — [CC-BY-SA-4.0](LICENSE.md)

© 2026 Greencardamom
