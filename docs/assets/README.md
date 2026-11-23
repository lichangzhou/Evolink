# Screenshot assets

All images in this folder were captured from a **local, sanitised instance** of EvoLink
(`tools/shoot_clean.py`), not from a production deployment.

| | International build | China-facing build |
|---|---|---|
| Source | local instance + a copy of the index database | local instance, empty `data/` |
| Real library data | yes (titles, categories, counters) | no |
| Credentials / cookies | none | none |
| Device fingerprint / activation key | none | none |
| License verification address | none | none |

## What is deliberately NOT in these images

- No 115 cookie, no Emby / Plex / TMDB API key
- No device id (`data/.device_id`)
- No activation key and no license verification server address
- No production-only screens (the license panel is removed on capture)

> Screenshots taken against the live NAS deployment **do** contain a device fingerprint and
> the license verification address. Those are kept out of the repository on purpose.

## Files

| File | View |
|---|---|
| `01-dashboard.png` | Dashboard — release status counters, category filters, gap totals |
| `02-media-library.png` | Media library browser — merged across all four servers |
| `03-index-libraries.png` | Index management — add and index Emby / Plex / Jellyfin / Playable |
| `04-index-results.png` | Index results — every indexed item, searchable and filterable |
| `05-start-compare.png` | Start a comparison against the release catalogues |
| `06-cloud-drive-login.png` | Netdisk login and directory access |
| `07-302-assistant.png` | 302 assistant — reverse proxy ports and link settings |
| `08-archive-scrape.png` | Archive & scrape — source, output and naming template |
| `09-weighted-dedup.png` | Weighted dedup — the 13 scoring dimensions |
| `10-category-settings.png` | Category settings — the directory rule engine |
| `11-delete-linkage.png` | Delete linkage — the Emby webhook |
| `12-settings.png` | Settings |

## Licensing of the images

The screenshots show a personal media library. They are published under the same MIT terms
as the rest of this repository. If you fork the project, replace them with your own — a
screenshot of your own library is far more convincing anyway.
