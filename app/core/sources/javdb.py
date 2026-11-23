"""JavDB 番号汇总源（仅日系）。

实测 2026-10-18（经 NAS mihomo）：
- /censored?page=N                        最新有码发行，40 条/页，可连续翻页
- /rankings/movies?p=monthly&t=censored   排行榜，60 条/页，无分页参数
- /uncensored                            需登录（302 → /login），暂不使用

只要日本：censored（有码）本身就是日系；t=western 是欧美，不抓。
"""
import html
import re
import time
import urllib.error
import urllib.request

from .. import config

DEFAULT_BASE = "https://javdb.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

ITEM_RE = re.compile(r'<a href="(/v/[^"]+)" class="box" title="([^"]*)">(.*?)</a>', re.S)
CODE_RE = re.compile(r"<strong>([^<]+)</strong>")
IMG_RE = re.compile(r'<img[^>]+src="([^"]+)"')
DATE_RE = re.compile(r'class="meta">\s*(\d{4}-\d{2}-\d{2})')
SCORE_RE = re.compile(r"([\d.]+)分")
TAG_RE = re.compile(r'<span class="tag[^"]*">([^<]+)</span>')

# 抓取模式 → (路径, 说明)
MODES = {
    "new":         ("/censored", "最新发行"),
    "sub":         ("/censored?f=sub", "最新字幕"),
    "rank_daily":  ("/rankings/movies?p=daily&t=censored", "日榜"),
    "rank_weekly": ("/rankings/movies?p=weekly&t=censored", "周榜"),
    "rank_monthly": ("/rankings/movies?p=monthly&t=censored", "月榜"),
}
# 排行榜只有一页，翻页无意义
NO_PAGING = {"rank_daily", "rank_weekly", "rank_monthly"}


class JavDBError(RuntimeError):
    pass


def _base(cfg=None):
    return ((cfg or config.load()).get("javdb_base") or DEFAULT_BASE).rstrip("/")


def _opener(proxy):
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()


def _get(path, cfg=None, timeout=25):
    cfg = cfg or config.load()
    proxy = config.proxy_url(cfg) or None
    url = _base(cfg) + path
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8",
            })
            with _opener(proxy).open(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                time.sleep(2 * (attempt + 1))
                last = e
                continue
            raise JavDBError(f"JavDB {path} HTTP {e.code}")
        except Exception as e:
            last = e
            time.sleep(1.5)
    raise JavDBError(f"JavDB {path} 请求失败：{last}")


def _norm_code(raw):
    """'DLDSS-538' → 'DLDSS538'（与 Emby 库存侧 av_code 同一格式）。"""
    return re.sub(r"[^A-Z0-9]", "", html.unescape(raw or "").upper())


def parse(html_text, base=None):
    """解析一页列表，返回 [{code,title,date,poster,score,tags,url}]。"""
    base = base or DEFAULT_BASE
    out, seen = [], set()
    for href, title, body in ITEM_RE.findall(html_text):
        m = CODE_RE.search(body)
        code = _norm_code(m.group(1)) if m else ""
        if not code or code in seen or len(code) < 4:
            continue
        seen.add(code)
        img = IMG_RE.search(body)
        date = DATE_RE.search(body)
        score = SCORE_RE.search(body)
        tags = TAG_RE.findall(body)
        # title 属性是「番号 + 空格 + 标题」，掐掉番号留标题
        name = html.unescape(title)
        name = re.sub(r"^%s\s*" % re.escape(html.unescape(m.group(1)).strip()), "", name).strip()
        out.append({
            "code": code,
            "title": name or code,
            "raw_code": html.unescape(m.group(1)).strip(),
            "date": date.group(1) if date else "",
            "poster": img.group(1) if img else "",
            "score": float(score.group(1)) if score else 0.0,
            "tags": tags,
            "url": base + href,
        })
    return out


def fetch(mode="new", pages=1, progress=None, cfg=None, on_page=None,
          start_page=1, known=None, stop_after_known=0, state=None):
    """按模式抓取，返回去重后的条目列表。

    start_page：断点续抓的起始页；known：已有番号集合，一页全是老番号就提前收工。
    state：可选 dict，回填 {last_page, exhausted} 给上层记游标。
    """
    cfg = cfg or config.load()
    if mode not in MODES:
        raise JavDBError(f"未知模式 {mode}")
    path, label = MODES[mode]
    base = _base(cfg)
    out, seen = [], set()
    last, exhausted, streak = start_page - 1, False, 0
    for page in range(max(1, start_page), max(1, pages) + 1):
        if page > 1 and mode in NO_PAGING:
            break
        q = path
        if page > 1:
            q = path + ("&" if "?" in path else "?") + f"page={page}"
        if progress:
            progress(f"JavDB {label} 第 {page} 页…")
        rows = parse(_get(q, cfg), base)
        if not rows:
            exhausted = True
            break
        last = page
        new = 0
        for r in rows:
            if known is not None and r["code"] not in known:
                new += 1
                known.add(r["code"])
            if r["code"] not in seen:
                seen.add(r["code"])
                out.append(r)
        if on_page:
            on_page(page, max(1, pages), len(out))
        streak = streak + 1 if (known is not None and new == 0) else 0
        if stop_after_known and streak >= stop_after_known:
            if progress:
                progress(f"  · JavDB {label} 连续 {streak} 页没有新番号，这一档已补齐")
            exhausted = True
            break
        time.sleep(0.6)
    if state is not None:
        state["last_page"] = last
        state["exhausted"] = exhausted
    return out


def build_av_list(pages=10, modes=("new", "rank_monthly"), progress=None, on_step=None,
                  cursors=None, known=None, resume=False, stop_after_known=3):
    """拉一批日系番号：最新发行 N 页 + 月榜 TOP。返回统一 catalog 行。

    resume=True 时按 cursors（模式 → {page, done}）接着上次的页码翻，
    已经抓完的档直接跳过 —— 所以「全量」点第二次不会从头再扫一遍。
    """
    cfg = config.load()
    rows, seen = [], set()
    cursors = cursors if cursors is not None else {}
    known = known if known is not None else set()
    plan = [(m, 1 if m in NO_PAGING else pages) for m in modes]
    total_pages = sum(p for _m, p in plan) or 1
    done = 0
    for mode, p in plan:
        st = cursors.get(mode) or {}
        if resume and st.get("done"):
            done += p
            if progress:
                progress(f"JavDB {MODES[mode][1]} 已完成，跳过")
            continue
        start = max(1, int(st.get("page") or 1)) if resume else 1
        if start > p:
            cursors[mode] = {"page": p, "done": True, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
            done += p
            continue
        step = [done]
        state = {}

        # 全量模式一档 500 页，必须逐页回报，否则进度条要等整档跑完才跳
        def _pg(page, tp, n, _m=mode, _p=p, _s=step):
            _s[0] += 1
            if on_step:
                on_step(min(_s[0], total_pages), total_pages,
                        f"{_m} 第 {page}/{tp} 页 · 本档 {n} 个番号 · 累计 {len(rows) + n}")

        for r in fetch(mode, p, progress=progress, cfg=cfg, on_page=_pg,
                       start_page=start, known=known, stop_after_known=stop_after_known,
                       state=state):
            if r["code"] in seen:
                continue
            seen.add(r["code"])
            year = int(r["date"][:4]) if r["date"][:4].isdigit() else None
            rows.append({
                "cid": f"javdb:{r['code']}",
                "source": "javdb",
                "category": "AV",
                "title": r["raw_code"],
                "original_title": r["title"],
                "year": year,
                "release_date": r["date"],
                "poster": r["poster"],
                "tmdb_id": "",
                "rating": r["score"] or None,
                "popularity": 0,
                "av_code": r["code"],
                "extra": {"media": "movie", "site": "javdb", "url": r["url"],
                          "tags": r["tags"], "mode": mode},
            })
        done += p
        if resume:
            last = int(state.get("last_page") or start - 1)
            cursors[mode] = {"page": min(last + 1, p),
                             "done": bool(state.get("exhausted") or last >= p),
                             "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        if on_step:
            on_step(min(done, total_pages), total_pages, f"{mode} · {len(rows)} 个番号")
    if progress:
        progress(f"JavDB 共取到 {len(rows)} 个番号")
    return rows


def exists(code, cfg=None):
    """番号在 JavDB 上是否存在（用于校验手工导入的番号）。"""
    return bool(parse(_get(f"/search?f=all&q={code}", cfg or config.load()), _base(cfg)))
