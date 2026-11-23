# EvoLink

A **strm media library + archive scraping + weighted dedup** toolkit for
Emby / Jellyfin / Plex.

It stores no media files — only titles, covers, release dates, and `.strm` pointers to
resources on your netdisk.

Implemented with the **Python standard library only** (zero third-party dependencies) and
runs in a single container.

---

## Features

| Module | What it does |
|---|---|
| **115 → 302 link swapping** | A `.strm` holds a netdisk path; at play time EvoLink signs a direct link and answers with a `302` redirect. Separate reverse proxies for Emby / Jellyfin / Plex |
| **Library sync** | Scans the local strm directory, compares it against what the media servers actually hold, and produces an "have / missing / broken" list with batch cleanup and a recycle bin |
| **Archive & scrape** | Generates correctly named `.strm` files from TMDB metadata, with the directory tree derived from your classification rules — dry run first, write to disk only after you approve |
| **Weighted dedup** | When a title exists in several versions (4K REMUX / 1080p WEB-DL…), every version is scored across 13 dimensions and only the best one is kept. Plans can be created, edited, imported and exported as JSON |
| **Category settings** | Define the archive directory hierarchy: media type → second-level category → third level and below. Ships with templates, a field dictionary and per-condition explanations |
| **Delete linkage** | Deleting an item in Emby automatically cleans up the matching `.strm` (webhook) |
| **Gap analysis** | Pulls TMDB / chart / catalogue sources and compares them against your inventory to show what is missing |

The international build additionally ships the adult-content and anime-oriented catalogue
adapters — see the main [README](../README.md#gap-analysis-and-metadata-providers).

---

## Quick start

```bash
docker compose up -d
```

Then open `http://<your-nas-ip>:9191`.

First run, in this order:

1. **Settings** — Emby address and API key, TMDB key, 115 cookie
2. **Archive & Scrape** — source directory, output directory, naming template → "generate
   plan" (dry run, writes nothing) → review → "write strm files"
3. **Category Settings** — pick a template, adjust the conditions and the order, save
4. **Weighted Dedup** — create a plan, tune the dimension weights, save, then pick it as
   the movie / series dedup plan on the archive page

---

## Archive directory structure

```
Movies / Domestic / Avatar Collection / Avatar: The Way of Water (2009).strm
```

- **Level 1** — media type (Movies / Series); can be switched off in Category Settings
- **Level 2** — the first matching classification rule. Rules are evaluated **top to
  bottom and the first hit wins**, so put the specific ones first and the catch-all last
- **Level 3 and below** — sub-directories, either generated from actor / director / series
  keywords or added by hand

Conditions available on a level-2 rule:

| Condition | Meaning |
|---|---|
| TMDB genre id | Comma separated, negative = exclude. `99,-10402` means documentary but not music documentary |
| TMDB genre name | Localised names, e.g. Action, Comedy, Sci-Fi |
| Country / region code | e.g. `CN,US,JP` |
| Localised country name | e.g. Mainland China, Japan, United States |
| Original language code | e.g. `zh,en,ja` |
| Year range | e.g. `2020-2099`, also `2020-` / `-2019` |
| Title keyword | Matches when the title contains any of them |
| Series keyword | **Only used to build the "… Collection" sub-directory — does not affect matching** |
| Actor / Director | **Only used to build the actor / director sub-directories — does not affect matching** |

---

## Weighted dedup

Every version of a title is scored per dimension, the scores are summed, and the highest
total wins.

The 13 dimensions: resolution, audio codec, video codec, bit depth, frame rate, Dolby
Vision, dynamic range, source, subtitles, file extension, release flags, release group and
custom attributes.

Each dimension also supports its own blacklist and whitelist. With a dedup plan selected,
only the best-scoring version of a title is written; raise "versions to keep" to keep the
best two instead.

Plans can be exported to and imported from JSON, which makes a tuned setup portable between
machines.

---

## 302 link swapping

A `.strm` file contains a single line:

```
http://<EvoLinkAddress>:9191/api/swap115/115open%2FVideo%2FMovie%2Fxxx.mkv
```

EvoLink reads the path → signs a direct link with the configured cookie → `302` redirect.
The signing request and the link request must use the same User-Agent; the program handles
that.

Reverse proxy ports are configured on the "302 Assistant" page:

| Port | Purpose |
|---|---|
| 9191 | EvoLink management panel |
| 8093 | Emby 302 proxy (upstream is the native Emby) |
| 8099 | Jellyfin 302 proxy (upstream is the native Jellyfin) |
| 32411 | Plex 302 proxy (upstream is the native Plex) |

> ⚠️ The host's native Emby / Jellyfin / Plex ports are already owned by their own
> containers. **EvoLink must not occupy or modify them.**

---

## Directory structure

```
compose.yml                container orchestration
app/
  server.py                HTTP service + background tasks (index / pull / compare / archive)
  core/
    config.py              reads and writes data/config.json
    emby.py                read-only Emby client
    jellyfin.py            read-only Jellyfin / Playable client
    matcher.py             normalisation + identifier extraction + scoring
    db.py                  SQLite schema (owned / catalog)
    sources/tmdb.py        TMDB discover / trending / search (through the proxy)
    sources/douban.py      Douban metadata and charts
    sources/lists.py       chart and list sources
    strm_sync.py           library sync engine (incremental, exclusions, symlinks)
    strm_classify.py       classification rule matching + directory generation
    strm_archive.py        archive plan generation and write-out
    strm_weight.py         weighted scoring and dedup
    strm_webhook.py        delete-linkage observer
    strm_lib.py            strm library persistence model
    pan115.py              115 account and directory tree
    pan115_swap.py         115 link signing
    pan115_downurl.py      115 direct-link resolution
    reverse_proxy.py       Emby / Jellyfin reverse proxy
    plex_proxy.py          Plex reverse proxy + plex.direct certificate
  tools/
    match.py               match a directory against the Emby inventory
    sync_emby.py           full Emby index into the local database
  web/index.html           single-page interface
data/
  config.json              all configuration
  vl.db                    media index database
  tasks.json               background tasks / resumable progress
  strm_trash/              strm recycle bin
```

---

## Mounts

| Mount | Purpose | Mode |
|---|---|---|
| `data/` | Configuration and database | rw |
| `app/web/index.html`, `app/server.py`, `app/core/*.py` | Source code (restart to apply changes) | ro |
| `/vol1/1000/Link:/Link` | strm library — the proxy reads the first line | ro |
| the same → `/app/Link` | Link sync and strm generation write here | rw |
| Jellyfin `config/data` | Proxy resolves an item to its strm path | ro |
| Plex `config` | same, for Plex | ro |
| Emby `data` | Delete linkage resolves an item id by Path | ro |
| `/vol1/1000/CloudNAS:/CloudNAS` | local netdisk mount (CD2) | ro |

> The container uses `network_mode: host`, so `ports:` mappings have no effect — the service
> binds the host ports directly. Change the ports from the web panel.

---

## FAQ

**Link swapping returns 401 / 403**
The 115 cookie expired, or the UA differs between signing and playback. Re-scan the QR code
in the settings page.

**The proxy starts but the player shows nothing**
Usually the strm directory is not mounted where EvoLink expects it, or the container cannot
read `/Link`. Check the mounts in `compose.yml`.

**The generated file names are wrong**
Adjust the conditions under Category Settings first, then generate a plan (dry run) on the
archive page and only then write to disk.

**Deleting an item in Emby leaves the strm behind**
Add the Emby webhook manually (pointing at EvoLink's `/api/emby/webhook`) and save the
token on the Delete Linkage page.

**Scanning the whole netdisk is too slow**
Two scan modes exist. Prefer **local mode**, which walks the CD2 mount and issues zero
netdisk requests. Use the API mode only when you really need the full remote tree.
