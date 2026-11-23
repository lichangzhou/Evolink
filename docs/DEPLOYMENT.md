# Deployment guide

This guide covers the things that are easy to get wrong: which mounts are mandatory, how
the ports interact with the media server containers, and what to check when a feature does
not behave as expected.

---

## 1. Requirements

| Item | Requirement |
|---|---|
| Docker | Docker Compose v2 (`docker compose`, not `docker-compose`) |
| Network | Host networking — EvoLink binds host ports directly |
| Python | 3.12+ if you run from source instead of the image |
| Disk | ~2 GB for the image, plus room for the `.strm` library |
| Media servers | At least one of Emby / Plex / Jellyfin / Playable (optional — you can start without any) |

There is **no third-party Python dependency**. The image contains everything.

---

## 2. Deploying

### Option A — Compose (recommended)

```bash
git clone https://github.com/lichangzhou/Evolink.git
cd Evolink
mkdir -p data
```

Edit `compose.yml`. Two mounts are mandatory:

```yaml
volumes:
  - ./data:/app/data           # EvoLink's own config, catalog and recycle bin
  - /vol1/1000/Link:/app/Link  # your .strm library
```

Then:

```bash
docker compose up -d
docker compose logs -f --tail 100
```

### Option B — Plain `docker run`

```bash
docker run -d --name evolink \
  --network host \
  -e TZ=Asia/Shanghai \
  -e PUID=1000 -e PGID=1001 \
  -v /path/to/evolink/data:/app/data \
  -v /path/to/your/strm:/app/Link \
  -v /path/to/clouddrive:/CloudNAS:shared \
  lichangzhou/evolink:latest
```

### Option C — From source

```bash
cd app
python3 server.py
```

`PORT` selects the listening port (default `9191`).

---

## 3. Mount reference

| Host path | Container path | Mode | Required | Purpose |
|---|---|---|---|---|
| `./data` | `/app/data` | rw | **yes** | `config.json`, `vl.db`, `tasks.json`, `strm_trash/` |
| your strm library | `/app/Link` | rw | **yes** | Link sync, archive output, dedup |
| your strm library | `/Link` | ro | for `302` | The reverse proxy reads the first line of each `.strm` |
| CloudDrive2 mount | `/CloudNAS` | shared | for path-style links | Local mount of the netdisk |
| Emby `data` | `/emby_data` | ro | for delete linkage | Resolve a deleted item back to its `.strm` path |
| Jellyfin `data` | `/jellyfin_data` | ro | for `302` | Map an item to its `.strm` path |
| Plex `config` | `/plex_data` | ro | for `302` | Same, for Plex |

> **Mount the whole `data` directory of Emby and Jellyfin, not just the main database file.**
> Both use WAL mode; mounting only the main file gives you a stale snapshot.

> The same strm directory is mounted **twice** on purpose: read-only at `/Link` for the
> proxy, read-write at `/app/Link` for the writers.

---

## 4. Ports

| Port | Who owns it | Change it where |
|---|---|---|
| `9191` | EvoLink panel and API | `PORT` env var |
| `8093` | EvoLink → Emby `302` proxy | Panel → 302 Assistant |
| `8099` | EvoLink → Jellyfin `302` proxy | Panel → 302 Assistant |
| `32411` | EvoLink → Plex `302` proxy | Panel → 302 Assistant |
| `8092` | Playable server | — |

> EvoLink uses `network_mode: host`, so a `ports:` section has **no effect**. The service
> binds the host port directly and the port has to be changed from the panel.

> The native Emby / Jellyfin / Plex containers already own their usual ports. EvoLink must
> not occupy or modify them — only the reverse-proxy ports above belong to EvoLink.

---

## 5. First run

1. Open `http://<your-nas-ip>:9191`.
2. **Settings** — set the timezone, then add your media servers:
   - Emby / Jellyfin: URL + API key (`/System/Info` in Emby, `/Users` in Jellyfin)
   - Plex: use the *Log in to Plex* button — do not paste a token by hand
   - TMDB: API key
3. **115 login** — scan the QR code with the 115 app. The cookie is stored in `data/config.json`.
4. **Index Libraries** — pick a server and run the index. The first run takes a while on
   a large library; it is incremental afterwards.
5. **Category Settings** — load a template and adjust.
6. **Archive & Scrape** — generate a plan, review it, then write.

---

## 6. Verification checklist

```bash
# health
curl -s http://127.0.0.1:9191/api/version
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:9191/

# the three proxies answer with a redirect to their upstream
curl -s -o /dev/null -w 'emby %{http_code} -> %{redirect_url}\n' http://127.0.0.1:8093/
curl -s -o /dev/null -w 'jellyfin %{http_code}\n' http://127.0.0.1:8099/
curl -s -o /dev/null -w 'plex %{http_code}\n' http://127.0.0.1:32411/

# a .strm link resolves to a 302
curl -s -o /dev/null -w '%{http_code} -> %{redirect_url}\n' http://127.0.0.1:9191/api/swap115/<path>
```

---

## 7. Upgrades

```bash
cd /path/to/Evolink
git pull
docker compose up -d --build     # or: docker compose pull && docker compose up -d
```

Your data lives in `data/` and is not touched by an upgrade. Back it up anyway:

```bash
docker run --rm -v /path/to/evolink/data:/data -v $PWD:/backup alpine \
  tar czf /backup/evolink-data-$(date +%Y%m%d).tar.gz -C /data .
```

> After a restart the panel returns `000`/`502` for 30–60 seconds while the health check
> comes back. That is the container starting, not a failure.

---

## 8. Troubleshooting

### The panel is unreachable after a restart

The container binds the port only once the process is up. Check `docker compose ps` and
`docker compose logs -f`. If the container is `healthy` and the port is still refused,
another process on the host owns `9191`.

### Playback fails but the library works

The reverse proxy cannot read the strm library. Verify the mount:

```bash
docker exec evolink ls -l /Link | head
docker exec evolink head -1 /Link/<some-file>.strm
```

The first line must be an `http://` URL pointing back at EvoLink's own `swap115` endpoint.

### `plex.direct` certificate does not match

The self-signed certificate is generated for the LAN IP of the machine running EvoLink.
If you moved the stack, delete the old certificate so it is regenerated:

```bash
docker exec evolink rm -f /app/data/plex_proxy_tls/cert.pem /app/data/plex_proxy_tls/key.pem
docker restart evolink
```

To pin the address explicitly, set `EVOLINK_HOST_IP`.

### Metadata sources time out

Most catalogue APIs need an outbound proxy. Fill in **Settings → HTTP proxy** with the
host's LAN address and port. With host networking, `127.0.0.1` points at the container
itself and never works.

### Scanning the netdisk is slow

Prefer **local mode**, which walks the CloudDrive2 mount and issues no netdisk requests at
all. Use the API mode only when you need the complete remote tree.

### Disk usage grows

`data/strm_trash/` keeps deleted `.strm` files for recovery. Empty it from the panel
(**Link cleanup → empty recycle bin**) or set up a periodic purge.
