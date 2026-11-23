# Ecosystem

EvoLink is not a standalone product. It is one of three self-hosted projects that are
designed to sit on the same NAS and cover the whole "media library" lifecycle — from
operating the container, through keeping the library correct, to watching it on the couch.

| Project | Role | Repository |
|---|---|---|
| **Docker-Link** | Operate the stack | [lichangzhou/Docker-link](https://github.com/lichangzhou/Docker-link) |
| **EvoLink** | Keep the library correct | [lichangzhou/Evolink](https://github.com/lichangzhou/Evolink) · [EvoLink-CN](https://github.com/lichangzhou/EvoLink-CN) |
| **Playable** | Watch it | [lichangzhou/Playable](https://github.com/lichangzhou/Playable) |

All three are **single-container**, **dependency-light** and **self-hosted**. None of them
requires a cloud account, a subscription or a phone home — with the single exception of the
optional license verification, which you can point at your own server.

---

## How they fit together

```
┌──────────────────────────────────────────────────────────────┐
│  Docker-Link                                                 │
│  container · image · volume · network · Compose             │
│  web terminal · file manager · remote agent · monitoring    │
│  → runs and supervises everything below                      │
└───────────────────────────┬──────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────┐
│  EvoLink                                                     │
│  index Emby / Plex / Jellyfin / Playable                    │
│  compare against release catalogues → find the gaps         │
│  archive · deduplicate · classify · sign 302 links           │
│  → maintains /app/Link, the .strm library                    │
└───────────────────────────┬──────────────────────────────────┘
                            │  .strm files
                            │  http://…/api/swap115/<path>
                            ▼
┌──────────────────────────────────────────────────────────────┐
│  Playable                                                    │
│  Android · Windows · iOS                                    │
│  plays the .strm library, follows the 302 redirects          │
│  → the client on the TV, the phone and the projector          │
└──────────────────────────────────────────────────────────────┘
```

---

## Pairing notes

### Docker-Link → EvoLink

Docker-Link manages the EvoLink container the same way it manages everything else:

- **One-click Compose deploy** — drop the EvoLink `compose.yml` into Docker-Link's Compose
  section and it is up, with the right volumes, without touching a terminal.
- **Live container view** — EvoLink's `9191` port, its health check and its logs are visible
  next to the rest of the stack.
- **File manager + web terminal** — inspect `data/config.json`, read the SQLite catalog, run
  a one-off `python3 -m compileall app` against a mounted checkout.
- **Monitoring and auto-recovery** — if EvoLink's container exits, Docker-Link restarts it;
  if the health check fails, it alerts you through the notification channel you configured.

### EvoLink → Playable

EvoLink produces the library, Playable consumes it:

- **The `.strm` format** is the contract. A `.strm` file holds a single URL; Playable
  resolves it, follows the `302`, and plays the direct netdisk link.
- **`302` link swapping** means the netdisk path never leaves the server side — the player
  only ever sees a signed, expiring link.
- **Cross-source indexing** means a title added on Plex but missing on Emby is visible to
  EvoLink, so the gap analysis covers what you actually watch, not just what one server
  happens to hold.

### Playable → EvoLink

Playable is also an **index source**. EvoLink can index a Playable library alongside Emby,
Plex and Jellyfin, so titles that exist only in Playable still count as "owned" and do not
show up as missing.

---

## Using them together

A complete setup, in order:

1. **Docker-Link** — deploy the stack: EvoLink, your media servers, Playable.
2. **EvoLink** — log in, add the media servers, let it index, then run a comparison to see
   what is missing. Set up the category rules, generate an archive plan, and write the
   `.strm` files.
3. **Playable** — point it at the same `.strm` library. It picks up the catalogue and plays
   it, with the `302` handled transparently.

### Recommended ports

| Service | Port | Note |
|---|---|---|
| Docker-Link | `9080` | Panel; agent channel on `9081` |
| EvoLink | `9191` | Panel and API |
| EvoLink → Emby | `8093` | `302` reverse proxy |
| EvoLink → Jellyfin | `8099` | `302` reverse proxy |
| EvoLink → Plex | `32411` | `302` reverse proxy |
| Playable | `8092` | Server (optional) |

Docker-Link's host networking means it does not need `ports:` mappings either — it binds
the host ports directly.

---

## Why three separate projects?

Because they solve different problems and have different lifetimes:

- **You replace Docker-Link** rarely — it is infrastructure.
- **You replace EvoLink** rarely — it is data-adjacent, and your catalog and rules live in
  `data/`, which is a plain directory you can back up like any other file.
- **You replace Playable** most often — it is the client, and it tracks new device formats.

Keeping them apart means an upgrade in one does not drag the other two along, and each one
can be recommended on its own merits.

---

## Cross-promotion

Each project README links to the other two, and each project ships this file. If you write
about one of them, mentioning the other two costs you nothing and gives the reader a
complete setup instead of a half-solution.

- **EvoLink** → [Docker-Link](https://github.com/lichangzhou/Docker-link),
  [Playable](https://github.com/lichangzhou/Playable)
- **Docker-Link** → [EvoLink](https://github.com/lichangzhou/Evolink),
  [Playable](https://github.com/lichangzhou/Playable)
- **Playable** → [Docker-Link](https://github.com/lichangzhou/Docker-link),
  [EvoLink](https://github.com/lichangzhou/Evolink)

If you write a post, a video or a talk covering more than one of them, open a
[Promotion submission](https://github.com/lichangzhou/Evolink/issues/new?template=promotion_submission.yml)
so it can be listed in the repository description.
