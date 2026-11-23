# Architecture

EvoLink is a single Python process with a SQLite database, four HTML pages and a handful of
background workers. There is no message broker, no cache server and no frontend build step.

```
app/
├── server.py              HTTP server, routing, background scheduler
├── core/
│   ├── config.py          the single source of truth for every tunable
│   ├── db.py              SQLite schema and access layer
│   ├── matcher.py         normalisation, identifier extraction, 3-tier matching
│   ├── sources/           metadata providers (one module each)
│   ├── emby.py            Emby read-only client
│   ├── jellyfin.py        Jellyfin / Playable read-only client
│   ├── plex_proxy.py      Plex reverse proxy + plex.direct certificate
│   ├── reverse_proxy.py   Emby / Jellyfin reverse proxy
│   ├── pan115.py          115 QR login and directory tree
│   ├── pan115_swap.py     path-style 302 link signing
│   ├── pan115_downurl.py  pickcode-style direct-link resolution
│   ├── strm_sync.py       strm library synchronisation engine
│   ├── strm_classify.py   classification rule engine
│   ├── strm_archive.py    archive plan generation and write-out
│   ├── strm_weight.py     13-dimension scoring and dedup
│   ├── strm_lib.py        strm library persistence model
│   ├── strm_webhook.py    Emby delete linkage
│   ├── avres.py           adult-resource index (international build)
│   ├── re0.py             RE0 direct channel
│   ├── re0browser.py      CloakBrowser manager
│   ├── hdhub.py           fallback resource search channel
│   ├── downurl_panel.py   115 link panel backend
│   └── license_ops.py     free / Pro entitlement
├── tools/
│   ├── match.py           match a target directory against the inventory
│   └── sync_emby.py       full Emby index into SQLite
└── web/
    ├── index.html         panel (Simplified Chinese)
    ├── index_en.html      panel (English)
    ├── docs.html          user guide (Simplified Chinese)
    └── docs_en.html      user guide (English)
```

---

## Data model

Two tables carry the product:

### `owned` — what you already have

One row per item, merged across all four media servers. `src` records which server it came
from and `emby_id` / `tmdb_id` / `imdb_id` / `tvdb_id` hold the external identifiers.

```
emby_id · name · original_title · year · type · library · tmdb_id · imdb_id
tvdb_id · has_image · path · norm · av_code · updated_at · medias · src
```

### `catalog` — what exists in the world

One row per release candidate pulled from a provider, with the match result written back
into the same row.

```
cid · source · category · title · original_title · year · release_date · poster
tmdb_id · imdb_id · tvdb_id · rating · popularity · norm · av_code
match_status · match_emby_id · match_score · matched_at · regions · genres · lists
```

### The join

```
catalog ──match──▶ owned
   │                  │
   │  source          │  library
   ▼                  ▼
  "what exists"    "what you have"
        └──── gap ────┘
```

`match_status` is the whole gap-analysis mechanism: every catalog row is either matched to
an owned item or not, and the unmatched ones are the gap.

### Other tables

| Table | Purpose |
|---|---|
| `cat_count` | Per-category, per-bucket, per-year counts, used to prioritise the gap list |
| `pan115` | Cached netdisk directory index |
| `meta` | Timestamps and counters (`owned_synced_at`, `owned_count`, …) |
| `strm_lib`, `strm_lib_run` | The strm library model and its run history |
| `strm_webhook_log` | Delete-linkage audit trail |

---

## The matching engine

`core/matcher.py` normalises titles and extracts identifiers; `tools/match.py` applies
them. Matching runs in three tiers, highest determinism first:

1. **External ID** — a TMDB / IMDb / TVDB id present on both sides is an exact match.
2. **Normalised title** — the title is lowercased, punctuation stripped, release
   suffixes removed, and the year is compared with a tolerance window.
3. **Catalogue identifier** — for catalogues that carry a product code (Japanese release
   codes), the code is normalised to `prefix + zero-stripped number` and compared
   exactly. This is what makes differently-localised titles collapse into one work.

Every match writes `match_status`, `match_emby_id`, `match_score` and `match_how`, so the
reason for a match is auditable rather than a black box.

---

## Reverse proxies

The `302` mechanism is the part that touches the player's network path.

```
client ──▶ EvoLink proxy :8093 ──▶ Emby API
             │
             ├─▶ resolve the playback item → its .strm path
             ├─▶ read the first line of that .strm → the 115 path
             ├─▶ sign a direct link with the configured cookie
             └─▶ 302 Location: <signed direct link>
```

Two details make this work in practice:

- **The signing request and the playback request must use the same User-Agent.** EvoLink
  pins both.
- **The Plex variant needs a self-signed `plex.direct` certificate.** Plex clients do not
  validate the CA for `plex.direct` hosts, so a self-signed certificate is enough — but it
  must carry the right SAN, which is why `plex_proxy.py` derives the host IP from
  `EVOLINK_HOST_IP` or auto-detects it.

`core/reverse_proxy.py` handles Emby and Jellyfin, `core/plex_proxy.py` handles Plex,
which needs its own because Plex publishes `plex.direct` https candidates separately.

---

## Background workers

`server.py` owns a small scheduler. Long jobs — indexing, catalogue pulls, comparisons —
are resumable: progress is written to `data/tasks.json`, so a container restart continues
where it stopped instead of starting over.

> Do not enable periodic full-library scans against a netdisk account. Repeated bulk reads
> are the fastest way to get a netdisk provider to rate-limit you. EvoLink's local scan
> mode exists for exactly this reason.

---

## Configuration

`core/config.py` is the only place that knows the shape of `data/config.json`. Writes are
atomic: the new content goes to a `.tmp` file and is then moved into place with
`os.replace`, so a container that is killed mid-write never leaves a half-written
configuration behind.

Environment variables override a few keys at first start (see the deployment guide).

---

## Pages

Four single-file pages, no build step, no framework:

| File | Role |
|---|---|
| `index.html` / `index_en.html` | The panel — one single-page application |
| `docs.html` / `docs_en.html` | The user guide, opened in a new tab |

The English pages are **mirrors** of the Chinese ones: identical DOM and ids, different
text. That is a hard rule of the project — see [CONTRIBUTING.md](../CONTRIBUTING.md).

---

## Extending it

### A new metadata provider

1. Add `app/core/sources/<name>.py` exposing a function that returns rows shaped like
   `catalog` rows.
2. Register it in `app/core/sources/lists.py`.
3. Add a settings block for its key, proxy handling and pagination.

No change to `db.py` or `server.py` is required.

### A new media server

1. Add a read-only client in `app/core/` returning libraries and items.
2. Add an index route next to the existing `/api/sync-emby`.
3. Register the source in the inventory merge.

### A new classification rule

Rules live in `data/config.json` under the classification section and are evaluated
top-down — the first match wins. Templates are importable and exportable as JSON.
