# Scope: move the `pages` worklist from SQLite-on-NFS to ToolsDB

Status: **proposed** (follow-up). Author note: written after the 2026-06-17
corruption incident (WAL on NFS, two writer hosts — see `python/src/db.py`).
The WAL→rollback-journal change + daily integrity-checked backups already landed
and make the current setup *safe enough*; this document scopes the **durable**
fix that removes the SQLite-on-NFS failure class entirely.

## 1. Why

`bup.db` lives on Toolforge NFS and is written from **two hosts** — the
webservice pod and the daily `verify` job pod. SQLite is a single-host database;
concurrent multi-host writes over NFS are unsupported and corrupted the file
once. The mitigations in place reduce the odds but don't change the
architecture. ToolsDB (the shared MariaDB at `tools.db.svc.wikimedia.cloud`) is
built for exactly this: multiple clients, real concurrency, **centrally backed
up by WMF**. The tool already uses it for user prefs (`python/src/userdb.py`),
so the connection pattern, credentials (`~/replica.my.cnf`), and DB-naming
(`<user>__bup`) are all established.

## 2. What moves / what stays

**Moves to ToolsDB:** the single `pages` table and everything in `db.py` that
reads/writes it (~15 functions, all called as `dbmod.fn(conn, …)`).

**Stays on NFS / unchanged:**
- `out.json` (the large offline-built corpus seed) and the `db/` directory.
- `auditlog.py` / `stats.py` — they import `db` **only** for `db_path()` to
  locate the `db/` directory for logs and stats files; they never touch the
  `pages` table. Keep a `data_dir()` helper so they don't break (see §5).
- `userdb.py` (prefs/edits) — already in ToolsDB; this migration makes the
  `pages` table its neighbor in the same `<user>__bup` database.

**Goes away after cutover:** `backup.py` + the `backup` cron job (ToolsDB is
WMF-backed-up), and the NFS-specific PRAGMAs in `db.connect()`. *Keep* a thin
logical-dump job if we want point-in-time worklist snapshots, but it's optional.

## 3. Target schema (ToolsDB / MariaDB)

```sql
CREATE TABLE IF NOT EXISTS pages (
  id          BIGINT       NOT NULL,          -- preserved from SQLite (NOT auto)
  page        VARCHAR(255) NOT NULL,
  count       INT          NOT NULL DEFAULT 0,
  ref_count   INT          NOT NULL DEFAULT 0,
  sim_count   INT          NOT NULL DEFAULT 0,
  book_count  INT          NOT NULL DEFAULT 0,
  revid       BIGINT       NOT NULL DEFAULT 0,
  citations   LONGTEXT     NOT NULL,          -- JSON blob; TEXT (64KB) is too small
  PRIMARY KEY (id),
  KEY idx_page  (page),
  KEY idx_count (count)                       -- NEW: top/paginated views sort by count
) DEFAULT CHARSET=utf8mb4;
```

Notes:
- **`citations` must be `LONGTEXT`** (or `MEDIUMTEXT`). MySQL `TEXT` caps at
  64 KB; some pages have enough citations to exceed that. This is the single
  most important schema gotcha. (SQLite `TEXT` is unbounded, so this never bit.)
- **`id` is not `AUTO_INCREMENT` for the data move** — we insert explicit ids to
  preserve them (see §6). For the ongoing `migrate.py` rebuild path we *can* use
  `AUTO_INCREMENT` since a fresh rebuild reassigns ids in file order anyway; the
  cleanest is to keep `id` plain and let `migrate.py` assign sequential ids
  itself, or set `AUTO_INCREMENT` and rely on insert order. Decide in §6.
- `idx_count` is **new** and a genuine win: `worklist_page` / `stats` /
  `random_pages` currently full-scan + sort 183k rows on SQLite every call.
- `page` is `VARCHAR(255)` — matches `userdb` and the MediaWiki title limit
  (255 chars). Confirm no worklist title exceeds 255 bytes before cutover (a
  one-line `MAX(LENGTH(page))` check; titles are ≤255 by MediaWiki rule).

## 4. Connection & lifecycle

Reuse `userdb.connect()` almost verbatim — same `replica.my.cnf`, host,
`utf8mb4`, `DictCursor`, short timeouts. Differences for `pages`:
- The webservice already opens one connection per request and closes on teardown
  (`app.get_db` / `api.get_db`); keep that. `userdb` proves the pattern works.
- **autocommit:** `userdb` uses `autocommit=True`. For `pages`, single-statement
  writes (`set_revid`, `replace_citations`) are fine with autocommit. **But
  `migrate.py` bulk-loads ~183k rows** — wrap that in one explicit transaction
  (autocommit off for the load) or it will be unbearably slow.

## 5. Per-file changes

| File | Change | Size |
|------|--------|------|
| `db.py` | Rewrite the data layer against pymysql. Keep the **public function signatures identical** (`worklist_page`, `get_page`, `pages_present`, `replace_citations`, `set_revid`, `stats`, …) so callers don't change. Replace `conn.execute(...).fetchall()` (a SQLite Connection convenience) with `with conn.cursor() as cur: cur.execute(...)`. Add `data_dir()` (returns the NFS `db/` dir) and keep `db_path()` as a deprecated alias for `auditlog`/`stats`. | **Large** — the core of the work |
| `migrate.py` | Target ToolsDB: `DROP TABLE`/recreate, bulk `executemany` in one transaction. `?`→`%s`. | Medium |
| `app.py` | `get_db()` → open a ToolsDB connection (or import from a shared helper). Teardown already closes it. No route/logic changes. | Small |
| `api.py` | Same `get_db()` swap. | Small |
| `verify.py` | None (uses `dbmod.connect` + `dbmod.fetch_page_batch`/`set_revid`/`replace_citations`, all preserved). Confirm `fetch_page_batch`'s `id > ? ORDER BY id` keyway still works (it does; PK). | None–tiny |
| `reconcile.py` | None (uses `dbmod.replace_citations`). | None |
| `auditlog.py`, `stats.py` | Repoint `db_path()` usage at `data_dir()`. | Tiny |
| `backup.py` + cron | Retire after cutover (optional logical dump instead). | Removal |

## 6. SQLite → MariaDB translation gotchas

- **Placeholders:** `?` → `%s` (pymysql) throughout `db.py`/`migrate.py`.
- **`conn.execute(...)`:** SQLite allows it on the Connection; pymysql needs a
  cursor. Every helper body changes; **return shapes stay dict-like** (DictCursor
  ≈ `sqlite3.Row`), so `r["page"]` keeps working and callers are unaffected.
- **`ORDER BY RANDOM()`** → `ORDER BY RAND()`. On 183k rows this materializes +
  sorts the whole set (same cost as today on SQLite). Optional speedup: pick a
  random `id` window instead. Low priority.
- **`LIKE … ESCAPE '\\'`:** valid in MariaDB. Note `search_titles` becomes
  case-insensitive **by collation** (`utf8mb4_general_ci`) — that's the desired
  behavior and matches SQLite's ASCII-case-insensitive LIKE, but it now also
  folds non-ASCII case. Acceptable/arguably better; call it out in review.
- **`pages_present` chunking (900):** that limit exists for SQLite's bound-
  variable cap. MariaDB's constraint is `max_allowed_packet`, not a 999-var
  limit; chunking can grow (e.g. 5k) or stay — keep it, it's harmless.
- **`AUTOINCREMENT`:** → `AUTO_INCREMENT` *if* used; but see id preservation.
- **Booleans / NULLs:** `revid` default 0 stays; `citations` NOT NULL stays.

## 7. ID preservation (critical)

`id` is **client-facing**: `/apply/<int:id>`, `/preview-fragment/<int:id>`,
`/runbot/<id>`, `/preview/<id>`, and the on-wiki gadget all reference it. The
live ids have gaps (7 months of `verify` deletes), so a fresh `migrate.py`
rebuild from `out.json` would **renumber** rows and break any in-flight links.

→ The **one-time data move copies current rows *with their ids*** (explicit
`INSERT … (id, …)`), not a rebuild. After cutover, `migrate.py` rebuilds (which
*does* renumber) remain as rare as they are today, and only happen alongside a
full corpus refresh where renumbering is already expected.

## 8. One-time migration + cutover plan

1. `userdb`-style `setup()` adds the `pages` table to `<user>__bup` (idempotent).
2. **Dry run:** copy current `bup.db` rows → ToolsDB preserving ids; verify
   `COUNT(*)` matches (183,389), spot-check a few ids/titles, check
   `MAX(LENGTH(page)) ≤ 255` and that the largest `citations` blob fits LONGTEXT.
3. **Cutover (low-traffic window):**
   - `webservice stop`; ensure no `verify` job running.
   - Final delta copy (re-run the copy; ids are stable so it's an upsert/replace).
   - Flip `app.get_db`/`api.get_db`/`dbmod.connect` to ToolsDB (config flag, §9).
   - `webservice start`; smoke-test `top`, `search`, `backlinks`, `apply`.
4. Repoint the `verify` job (no command change if `dbmod.connect` is the switch).
5. Decommission `backup` job; keep `bup.db` on NFS read-only for a grace period.

## 9. Rollback

Gate the backend behind one switch — e.g. `BUP_DB_BACKEND=sqlite|toolsdb` read
in `dbmod.connect()` / `get_db()`. If ToolsDB misbehaves post-cutover, flip the
env var and restart to fall back to the (retained, read-only-ish) `bup.db`. Keep
the NFS file for at least one full `verify` cycle + a week before deleting.

## 10. Risks & tradeoffs

- **Availability coupling (the big one):** today the core worklist is served
  from a local-ish NFS file that's almost always readable. Moving it to ToolsDB
  means **a ToolsDB outage or maintenance window = the tool's core views are
  down.** `userdb` tolerates this (prefs fall back to defaults); the `pages`
  table cannot fall back. Mitigations: ToolsDB is generally reliable; optionally
  keep a periodically-refreshed **read-only SQLite cache** on NFS for degraded
  read-only mode (adds complexity — probably not worth it initially).
- **Per-request connect latency:** a TCP+auth round-trip per request vs. a local
  file open. Small on-cluster, but the read-heavy views do it every load.
  Acceptable to start; add a small connection pool later if needed.
- **ToolsDB storage quota:** ~227 MB (mostly `citations` JSON). Within typical
  tool quotas; confirm headroom, request more if needed.
- **`ORDER BY RAND()` / `LIKE '%q%'`** remain full scans — no worse than today,
  and `idx_count` makes the common views *faster*.

## 11. Effort estimate

- `db.py` rewrite + unit pass: ~0.5–1 day (mechanical but touches every helper).
- `migrate.py` + one-time copy script + dry run: ~0.5 day.
- Wiring (`app`/`api`/`auditlog`/`stats`), config flag, smoke tests: ~0.5 day.
- Cutover + verification + grace-period cleanup: ~0.5 day.
- **Total: ~2–3 focused days**, low-to-moderate risk given the gated rollback
  and that the function-level interface to callers doesn't change.

## 12. Explicitly out of scope

- Connection pooling (add only if per-request connect latency proves real).
- A read-only NFS fallback cache for ToolsDB-outage degraded mode.
- Schema changes beyond the `idx_count` addition and the `LONGTEXT` fix.
- Touching `out.json` or the offline corpus pipeline.
