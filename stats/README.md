# bup usage statistics

bup records a set of daily usage counts and publishes them in a file accessible over HTTP.

## The data file

- **Path (on Toolforge):** `www/static/booksup-stats-<year>.jsonl`
- **URL:** `https://tools-static.wmflabs.org/bup/booksup-stats-<year>.jsonl`
- **Format:** [JSON Lines](https://jsonlines.org/) — one JSON object per line, **one line per day**.
- **One file per calendar year** (the year is in the filename, e.g. `booksup-stats-2026.jsonl`); a year's file has ~365 lines (366 in a leap year).
- Every day gets a record, **including days with all zeros**.

### Record schema

```json
{
  "date": "2026-06-04",
  "urls_added": 85,
  "webtool": { "edits": 3, "urls": 85 },
  "gadget":  { "edits": 0, "urls": 0 },
  "api":     { "page": 0, "random": 0, "worklist": 0, "pages": 0 }
}
```

| field | meaning |
|---|---|
| `date` | the UTC day the record covers (`YYYY-MM-DD`) |
| `urls_added` | URLs added to Wikipedia that day via the tool = `webtool.urls + gadget.urls` |
| `webtool.edits` / `webtool.urls` | edits made / links added via the bup web interface |
| `gadget.edits` / `gadget.urls` | edits made / links added via the BooksUp on-wiki gadget |
| `api.page` / `random` / `worklist` / `pages` | calls to each read-only API endpoint (invocations / engagement) |

> A single edit can add many links, so **`urls_added` counts links, not edits.**

## What is collected, and how

A daily job computes the **previous UTC day's** counts from three independent
sources:

| stat | source | method |
|---|---|---|
| **webtool** | bup's `db/log.txt` | every successful web-tool edit logs `page ---- user ---- COUNT ---- date ---- Success`; for the day, sum `COUNT` → links, count lines → edits |
| **gadget** | English Wikipedia database replica (`enwiki_p`) | count saved revisions that day whose edit summary contains `BooksUp`; the link count `N` is parsed from the gadget's "Adding N book link(s)" summary → links, rows → edits |
| **api** | bup's `db/api_hits.log` | the API appends one line per call (`<utc-iso> <endpoint>`); counted per endpoint for the day |

Notes:
- The replica query uses `PyMySQL` and the tool's `~/replica.my.cnf`.
- API invocation counts begin the day request logging was enabled; earlier days show `api` as all zeros.
- Web-tool and gadget link counts can be reconstructed for past days — the source log and the replica both retain history (see *Regenerating a day*).

## How it runs

A Toolforge scheduled job runs once per day:

```
toolforge jobs run booksup-stats --image python3.11 --schedule "@daily" \
  --command "$HOME/www/python/venv/bin/python $HOME/www/python/src/stats.py" \
  --mount all
```

`stats.py` (in `python/src/`) computes the prior UTC day, appends the record to
that year's file in `www/static/`, and prints it.

## Retrieving the data

**Over HTTP** (public, read-only):

```
curl https://tools-static.wmflabs.org/bup/booksup-stats-2026.jsonl
```

Parse it line by line — each line is one day. 

**On Toolforge:** read `www/static/booksup-stats-<year>.jsonl` directly.

### Regenerating a day

Recompute and append a specific day (e.g. to backfill web-tool / gadget counts):

```
python3 stats.py --date 2026-06-04
```

## Files

| file | role |
|---|---|
| `python/src/stats.py` | the daily stats job |
| `python/src/api.py` | logs each API call to `api_hits.log` (`_log_hit`) |
| `db/log.txt` | web-tool edit log (source for web-tool counts) |
| `db/api_hits.log` | API invocation log (source for `api` counts) |
| `www/static/booksup-stats-<year>.jsonl` | the published daily statistics |
