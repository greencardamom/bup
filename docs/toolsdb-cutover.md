# Runbook: cut the `pages` worklist over to ToolsDB

Operational companion to `docs/toolsdb-migration.md` (the *why* and the design).
This is the *how* — the concrete, copy-pasteable steps to flip production from
the SQLite-on-NFS `bup.db` to ToolsDB and to roll back if needed.

The switch is one env var, **`BUP_DB_BACKEND`** (`sqlite` | `toolsdb`), read by
`db.connect()`. Default is `sqlite`, so the deployed code is inert until flipped.

## Facts (validated 2026-06-17)

| | |
|---|---|
| Tool account | `tools.bup` (`become bup`) |
| ToolsDB database | `s55000__bup` (shared with `userdb`'s `prefs`/`edits`) |
| Source SQLite | `/data/project/bup/www/db/bup.db` |
| venv python | `/data/project/bup/www/python/venv/bin/python` |
| src dir | `/data/project/bup/www/python/src` |
| Webservice | type `python3.11`, kubernetes backend |
| Worklist size | **154,288 rows**, ~202 MB in InnoDB (dry-run measured) |
| Jobs touching `pages` | `verify` (@daily) — reads/writes the worklist |
| Jobs NOT touching `pages` | `booksup-stats` (NFS logs only), `backup` (backs up the SQLite file; retire after cutover) |

`BUP_DB_BACKEND` is set via the Toolforge **envvars** service, which injects it
into the webservice **and** all jobs (so `verify` picks it up on its next run; no
job edit needed).

## 0. Prerequisites (done)

- [x] Migration code deployed to `www/` (commit `1832b5c`); default backend `sqlite`, so production is unchanged.
- [x] `pages` table created + dry-run copy validated in ToolsDB (count, id/revid preservation, JSON integrity, indexes). Re-running the copy is idempotent (drop + reload, ids preserved).

## 1. Pre-cutover checks (any time, no downtime)

```bash
become bup
# code is current?
cd /data/project/bup/www && git log --oneline -1     # expect 1832b5c or later
# ToolsDB reachable + neighbours intact?
mysql --defaults-file=$HOME/replica.my.cnf -h tools.db.svc.wikimedia.cloud \
  s55000__bup -e "SHOW TABLES; SELECT COUNT(*) FROM pages;"
# envvars quota has room for one more?
toolforge envvars quota
```

## 2. Cutover (low-traffic window — tool is briefly down)

Pick a quiet window and confirm `verify` is not mid-run (`toolforge jobs list`;
it runs ~04:42 UTC). Then, as `become bup`:

```bash
# (a) stop the webservice so the SQLite worklist stops changing under us
webservice stop

# (b) final id-preserving copy SQLite -> ToolsDB (inline env; ~30s for 154k rows)
#     run as a python3.11 job so it uses the same runtime as the webservice.
#     One long line on purpose: backslashes inside the quoted --command break it.
toolforge jobs run bup-pages-cutover --image python3.11 --mount all --wait 3600 --command "/bin/bash -c 'BUP_DB_BACKEND=toolsdb /data/project/bup/www/python/venv/bin/python /data/project/bup/www/python/src/migrate.py --copy-from /data/project/bup/www/db/bup.db'"
cat $HOME/bup-pages-cutover.out      # expect: copied 154288 rows ... (ids PRESERVED)

# (c) flip the backend for the webservice + all jobs
toolforge envvars create BUP_DB_BACKEND toolsdb

# (d) bring the webservice back up on ToolsDB
webservice start
```

Verify the row count landed before opening up traffic:

```bash
mysql --defaults-file=$HOME/replica.my.cnf -h tools.db.svc.wikimedia.cloud \
  s55000__bup -e "SELECT COUNT(*) AS n, MIN(id), MAX(id) FROM pages;"   # n = 154288
```

## 3. Smoke test (in a browser / curl)

- `/` (TOP) renders the worklist; counts look sane.
- Search returns results (substring + type filter).
- `backlinks` / `category` views load (the previously-fixed authed reads).
- Open one `/preview/<id>` for a known id — confirms id preservation end-to-end.
- An `apply` round-trip prunes the citation (writes `replace_citations` to ToolsDB).
- `tail` the webservice logs for tracebacks; check `db/api_hits.log` still appends
  (NFS path via `data_dir()`, unaffected by the backend).

## 4. Repoint the `verify` job

No command change — `verify` reads `BUP_DB_BACKEND` from the envvars service. Let
its next scheduled run go, or trigger one and confirm it reconciles against
ToolsDB (and that `set_revid`/`replace_citations` write there, not to `bup.db`):

```bash
toolforge jobs run verify-once --image python3.11 --mount all --wait 3600 \
  --command "$HOME/www/python/venv/bin/python $HOME/www/python/src/verify.py"
```

## 5. Rollback (if ToolsDB misbehaves post-cutover)

The SQLite file is untouched by the cutover, so rollback is a flip + restart:

```bash
become bup
toolforge envvars create BUP_DB_BACKEND sqlite   # or: toolforge envvars delete BUP_DB_BACKEND
webservice restart
```

Note: edits applied **after** cutover landed in ToolsDB, not `bup.db`, so on
rollback those pages reappear as open work until the next `verify` reconciles
them. Acceptable (verify is idempotent — it re-prunes already-applied citations).
Roll back only for a real ToolsDB problem, not cosmetic issues.

## 6. Decommission (after a grace period)

Keep the SQLite path retained and rollback-ready for **at least one full `verify`
cycle plus one week**. Once confident:

- Disable the `backup` job (ToolsDB is WMF-backed-up):
  `toolforge jobs delete backup` and drop its entry from the jobs config.
- Optionally keep `bup.db` on NFS read-only a while longer, then delete it and
  the `backup.py` cron remnants.
- Leave `db/` in place — `out.json`, `log.txt`, `api_hits.log`, and the stats
  files still live there (located via `db.data_dir()`).

## Deploy mechanics

Code changes go acre → GitHub → Toolforge via `./bupsave.sh` (from the repo
root). For a code change that must land *with* a backend flip, deploy first
(`./bupsave.sh "msg"`), then run the envvars step above. `--no-restart` is handy
when you want the new code on disk without bouncing the running webservice.
