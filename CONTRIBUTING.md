# Contributing to EvoLink

Thanks for helping. This document explains the ground rules that keep the project buildable,
reviewable and safe to run.

## Before you open a pull request

1. **Search existing issues** — bugs and feature requests are often already filed.
2. **Keep the change focused.** One topic per PR makes review fast.
3. **Never commit runtime data.** `data/` holds `config.json`, the SQLite catalog and the
   strm recycle bin. It is git-ignored and must stay that way.
4. **Never paste credentials into an issue, a log or a commit.** Cookies, API keys and
   Plex tokens are secrets even when they belong to you.

## Language pairs must stay in sync

The UI ships as four files:

| File | Language |
|---|---|
| `app/web/index.html` | Simplified Chinese |
| `app/web/index_en.html` | English |
| `app/web/docs.html` | Simplified Chinese |
| `app/web/docs_en.html` | English |

The English pages are **mirrors** of the Chinese pages: same DOM, same ids, same
structure — only the visible text differs. When you add a control, a field or a
section, update both files in the same commit. Never restructure one page and leave
the other behind.

## Code conventions

- **Python standard library only.** EvoLink ships as a single container with no
  third-party runtime dependency. A new `pip install` needs a very good reason.
- `server.py` is the HTTP layer and the scheduler; reusable logic belongs in
  `app/core/`.
- Provider integrations live in `app/core/sources/` and are loaded through
  `app/core/sources/lists.py`. A new provider should be one module plus one entry in
  that list.
- Match the surrounding style: 4-space indent, `snake_case` functions,
  `PascalCase` classes, short English docstrings on public functions.
- Comments explain *why*, not *what*. Existing code carries Chinese comments in the
  China-facing build; keep the same language as the surrounding block.

## Verify before you push

```bash
# syntax
python3 -m compileall -q app

# lint the docs
npm install -g markdownlint-cli
markdownlint "**/*.md" --ignore node_modules
```

Then run the container and click through the pages you touched:

```bash
docker compose up -d
# open http://<your-nas-ip>:9191
```

A change is not done until it has been opened in a browser.

## Reporting bugs

Use the **Bug report** issue template and include:

- EvoLink version and build number (`/api/version`)
- deployment method (Compose / `docker run` / from source)
- what you expected vs. what happened
- the relevant `docker logs evolink --tail 100` output, with credentials redacted

## Suggesting features

Open a **Feature request** and describe the problem you are trying to solve rather than
the implementation you have in mind. Features that can be verified end to end get
merged much faster than features that would need a mock-up.

## Security

Do not report vulnerabilities in a public issue. Contact the maintainer privately
first, and give us a reasonable window to ship a fix.

## License

Contributions are accepted under the [MIT License](LICENSE). By submitting a pull
request you confirm that you have the right to license the code you contribute.
