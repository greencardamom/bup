# bup API (v1)

Read-only JSON API for **bup** — the tool that proposes archive.org book/journal
links for English Wikipedia citations. The API exposes bup's precomputed
worklist so on-wiki gadgets and external bots can fetch candidates for
an article (or browse the whole worklist) programmatically.

- **Base URL:** `https://bup.toolforge.org/api/v1`
- **Methods:** `GET`, plus one `POST` (`/pages`) for batch lookup. Read-only —
  the API never edits anything.
- **Auth:** none (open)
- **CORS:** `Access-Control-Allow-Origin: *` — callable from browser JS on
  `*.wikipedia.org` (gadgets/userscripts)
- **Encoding:** `application/json`, UTF-8, with real characters (e.g. `–`, `é`)
- **Versioning:** the path is versioned (`/api/v1`); breaking changes get a new
  version.

---

## ⚠️ The data is *candidates*, not *commands*

Every citation bup returns is a **proposal** that assumes the live article still
contains the `oldcite` string **verbatim** (bup matches citation strings literally — exact
whitespace, newlines, punctuation).

Before applying a candidate, a consumer searches for the `oldcite` in the current article text and replaces *that exact 
string* with `newcite`. 

---

## Endpoints

### `GET /health`
Liveness/version check.

```json
{ "status": "ok", "service": "bup", "api": "v1" }
```

### `GET /stats`
Totals for the **open** worklist (resolved citations are pruned, so these are
"work remaining", not "work ever done").

```json
{
  "pages": 193185,
  "citations": 322535,
  "by_type": { "book": 229343, "sim": 93192, "ref": 0 },
  "note": "Counts of OPEN work only; applied/resolved citations are pruned."
}
```

### `GET /page/<title>`
The link candidates bup has for one article. The title may use spaces or
underscores and may contain slashes (e.g. `MOS:FOO/Bar`); it is matched exactly
against the worklist.

`200` when found:

```json
{
  "title": "Miriam Cooper",
  "found": true,
  "counts": { "book": 1, "sim": 0, "ref": 0, "total": 1 },
  "citations": [
    {
      "oldcite": "{{cite book |title=Dark Lady of the Silents |last=Cooper ...}}",
      "newcite": "{{cite book |title=Dark Lady of the Silents |last=Cooper ... |pages=[https://archive.org/details/darkladyofsilent0000coop/page/204 205]–207 }}",
      "iaid": "darkladyofsilent0000coop",
      "meta": "110110",
      "url": "https://archive.org/details/darkladyofsilent0000coop/page/204",
      "type": "book"
    }
  ]
}
```

`404` when the article is not in the worklist:

```json
{ "title": "No Such Article", "found": false, "citations": [] }
```

```bash
curl 'https://bup.toolforge.org/api/v1/page/Miriam%20Cooper'
```

### `GET /worklist`
Browse the worklist, ordered by citation `count` descending (biggest impact
first). For bots iterating the corpus.

| param       | type | default | notes                                            |
|-------------|------|---------|--------------------------------------------------|
| `limit`     | int  | `50`    | 1–500                                            |
| `offset`    | int  | `0`     | for paging                                       |
| `type`      | enum | (all)   | `book`, `sim`, or `ref` — only pages having that |
| `min_count` | int  | `0`     | only pages with at least this many citations     |

```json
{
  "limit": 2, "offset": 0, "type": null, "min_count": 0, "count": 2,
  "pages": [
    { "title": "List of footballers in England by number of league appearances",
      "counts": { "book": 175, "sim": 0, "ref": 0, "total": 175 } },
    { "title": "Bibliography of the history of Poland",
      "counts": { "book": 0, "sim": 165, "ref": 0, "total": 165 } }
  ]
}
```

```bash
curl 'https://bup.toolforge.org/api/v1/worklist?type=book&min_count=10&limit=100'
```

### `GET /random`
Random worklist articles — for a "give me something to work on" feature. Same
filters as `/worklist`.

| param       | type | default | notes                                            |
|-------------|------|---------|--------------------------------------------------|
| `limit`     | int  | `1`     | 1–50                                             |
| `type`      | enum | (all)   | `book`, `sim`, or `ref` — only pages having that |
| `min_count` | int  | `0`     | only pages with at least this many citations     |

```json
{ "count": 1,
  "pages": [ { "title": "HMS Otter (1896)",
               "counts": { "book": 2, "sim": 0, "ref": 0, "total": 2 } } ] }
```

```bash
curl 'https://bup.toolforge.org/api/v1/random?limit=3'
```

### `POST /pages`
Batch membership check: given a list of article titles, return the subset that
is in the worklist (with counts). Use it to intersect the worklist with a set
you already have — a watchlist, a category's members, a WikiProject's articles —
so you can find pages worth working on instead of guessing.

Send the titles either as a **newline-separated `text/plain` body** (a
CORS-safelisted content type, so no preflight) or as JSON `{"titles": [...]}`.
Titles may use spaces or underscores. Up to **2000** titles per request — send
larger sets in chunks.

```bash
# text/plain (one title per line)
printf 'Miriam Cooper\nNot A Real Article\nElectronic music\n' \
  | curl -s -X POST -H 'Content-Type: text/plain' --data-binary @- \
    https://bup.toolforge.org/api/v1/pages
```

```json
{ "requested": 3, "found": 2,
  "pages": [
    { "title": "Electronic music", "counts": { "book": 1, "sim": 0, "ref": 0, "total": 1 } },
    { "title": "Miriam Cooper",    "counts": { "book": 1, "sim": 0, "ref": 0, "total": 1 } }
  ] }
```

`requested` is how many distinct titles were parsed; `found` how many are in the
worklist. Titles not in the worklist are simply omitted.

---

## The citation object

| field     | meaning                                                                 |
|-----------|-------------------------------------------------------------------------|
| `oldcite` | the **literal** citation wikitext bup expects to find (match key) |
| `newcite` | the proposed replacement, with the archive.org link added               |
| `iaid`    | the archive.org item identifier (`archive.org/details/<iaid>`)          |
| `url`     | convenience archive.org URL for display/linking (derived from newcite)  |
| `meta`    | internal flags string (opaque to consumers)                             |
| `type`    | `book` (book scan), `sim` (journal/serial), or `ref` (refactor)         |

> The same archive.org `url`/`iaid` may appear in several *different* citations,
> so `url` is **not** an identity key. The `oldcite` string is the key.

---

## Data freshness

bup's worklist is a precomputed corpus rebuilt occasionally (expensive). A **daily reconciler** checks all articles and 
prunes any citation whose `oldcite` no longer appears in the live article, so the worklist stays roughly current and shrinks 
as work gets done — but the candidate text itself reflects the last rebuild. Always re-check `oldcite` against the live 
article before acting.

---

## Errors

| status | meaning                                              |
|--------|------------------------------------------------------|
| `200`  | OK                                                   |
| `404`  | `/page/<title>` — article not in the worklist        |

Responses are always JSON.

---

## Example: gadget usage

A gadget running on an article page fetches bup's candidates, then checks each
against the page's current wikitext (which it already has) before offering to
apply:

```js
const title = mw.config.get('wgPageName');           // e.g. "Miriam_Cooper"
const res = await fetch(
  `https://bup.toolforge.org/api/v1/page/${encodeURIComponent(title)}`);
if (res.ok) {
  const data = await res.json();
  for (const c of data.citations) {
    if (wikitext.includes(c.oldcite)) {              // literal match required
      // offer to replace c.oldcite -> c.newcite (the user makes the edit)
    }
  }
}
```

---

*Read-only v1. Write/apply actions stay in the human-authenticated bup web UI.*
