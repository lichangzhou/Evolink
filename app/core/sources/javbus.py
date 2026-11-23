"""JavBus 番号目录源（日系，按片商维度可回溯到 2007 年）。

2026-10-19 实测结论（NAS + mihomo 代理）：
- 非日本 IP 会被 302 到 `/doc/driver-verify`，那是**年龄确认页**（不是机器人挑战）。
  POST `Submit=確認` 后拿到 `age=verified`（30 天），此后带浏览器头即可正常访问。
  cookie 过期不用人工管：请求只要又被打回年龄页，本模块会自动重验一次。
- 主站 `/page/N` 只能翻到约 400 页（500 就 404），**不能用**；
  但 `/studio/<id>/<page>` **没有翻页上限**（S1：300 页 2014 年、500 页 2007 年），
  所以目录按「片商」维度抓，才能覆盖完整历史。
- 列表每条 `class="movie-box"` 自带 `<date>番号</date> / <date>发行日</date>`，
  番号 + 发行日 + 标题 + 封面一次拿全，天然可做逐年对比。

口径说明：JavDB 负责「新片增量」，本模块负责「历史全量回灌」。
两者按 av_code 合并成同一行（见 server._upsert 的去重），不会重复计数。
"""
import html
import http.cookiejar
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import config

DEFAULT_BASE = "https://www.javbus.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 实测：只带 cookie 或只带这些头，都会继续 302；两者齐了才返回内容
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

ITEM_RE = re.compile(r'<a class="movie-box" href="([^"]+)"[^>]*>(.*?)</a>', re.S)
DATE_TAG = re.compile(r"<date>([^<]*)</date>")
IMG_RE = re.compile(r'<img[^>]+src="([^"]+)"')
IMG_TITLE = re.compile(r'<img[^>]+title="([^"]*)"')
STUDIO_RE = re.compile(r'/studio/([^"/]+)"[^>]*>([^<]*)<')
STAR_RE = re.compile(r'/star/([^"/]+)"[^>]*>([^<]*)<')

# 一页满 30 条；不足 30 说明到底了
PAGE_SIZE = 30

_jar_lock = threading.Lock()
_jar_obj = None


class JavBusError(RuntimeError):
    pass


def _base(cfg=None):
    return ((cfg or config.load()).get("javbus_base") or DEFAULT_BASE).rstrip("/")


def _jar():
    """进程内共用的 cookie jar，落盘持久化，重启后不用重新过年龄页。"""
    global _jar_obj
    with _jar_lock:
        if _jar_obj is None:
            path = config.DATA / "javbus_cookies.txt"
            j = http.cookiejar.LWPCookieJar(str(path))
            try:
                j.load(ignore_discard=True)      # 过期的不会载入，触发自动重验
            except Exception:
                pass
            _jar_obj = j
        return _jar_obj


def _save_jar():
    try:
        _jar().save(ignore_discard=True, ignore_expires=True)
    except Exception:
        pass


def _opener(cfg):
    proxy = config.proxy_url(cfg) or ""
    handlers = [urllib.request.HTTPCookieProcessor(_jar())]
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)


def _is_gate(text):
    return "Age Verification" in text or 'id="ageVerify"' in text


def _host(cfg=None):
    return urllib.parse.urlsplit(_base(cfg)).hostname or "www.javbus.com"


def _ensure_prefs(cfg=None):
    """强制 existmag=all（全部影片）。

    站点首次 302 会种 `existmag=mag`，那是「只显示有磁力的」——用它抓出来的是
    「可得性索引」，会系统性漏掉发行过但没人发种的片子（实测同一页差两年）。
    我们要的是发行目录，必须切到 all。
    """
    want = "all"
    host = _host(cfg)
    j = _jar()
    for c in list(j):
        if c.name == "existmag" and c.domain.lstrip(".") == host:
            if c.value == want:
                return True
            j.clear(c.domain, c.path, c.name)
    j.set_cookie(http.cookiejar.Cookie(
        0, "existmag", want, None, False, host, False, False, "/", True,
        False, None, False, None, None, {}))
    _save_jar()
    return True


def _raw(path, cfg, data=None, referer=None, timeout=30):
    """发一次请求，返回 (最终 URL, 正文)。404 返回 ("", "")。"""
    url = path if path.startswith("http") else _base(cfg) + path
    headers = dict(BROWSER_HEADERS)
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with _opener(cfg).open(req, timeout=timeout) as r:
            return r.geturl(), r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return "", ""
        raise JavBusError(f"JavBus {path} HTTP {e.code}")


def verify(cfg=None):
    """过年龄确认页，拿到 age=verified。返回是否成功。"""
    cfg = cfg or config.load()
    base = _base(cfg)
    data = urllib.parse.urlencode({"Submit": "確認"}).encode()
    try:
        _raw(base + "/doc/driver-verify", cfg, data=data, referer=base + "/")
    except Exception:
        pass
    _ensure_prefs(cfg)
    _save_jar()
    for c in _jar():
        if c.name == "age" and c.value:
            return True
    return False


def get(path, cfg=None, retry=2):
    """取一页 HTML；被年龄页拦下就自动重验再试。"""
    cfg = cfg or config.load()
    _ensure_prefs(cfg)
    text = ""
    for i in range(max(1, retry + 1)):
        try:
            _url, text = _raw(path, cfg)
        except JavBusError:
            if i >= retry:
                raise
            time.sleep(1.2)
            continue
        if text and _is_gate(text):
            if not verify(cfg):
                raise JavBusError("JavBus 年龄确认失败（代理不可用或站点改版）")
            continue
        _save_jar()
        return text
    return text


def _code(raw):
    return re.sub(r"[^A-Z0-9]", "", html.unescape(raw or "").upper())


def parse_list(text, base=None):
    """解析一页片商列表 → [{code,date,title,poster,url}]。"""
    base = base or DEFAULT_BASE
    out, seen = [], set()
    for href, body in ITEM_RE.findall(text or ""):
        dates = DATE_TAG.findall(body)
        if len(dates) < 2:
            continue
        code = _code(dates[0])
        if not code or len(code) < 4 or code in seen:
            continue
        seen.add(code)
        date = dates[1].strip()
        img = IMG_RE.search(body)
        poster = img.group(1) if img else ""
        if poster.startswith("/"):
            poster = base + poster
        t = IMG_TITLE.search(body)
        title = html.unescape(t.group(1)).strip() if t else code
        out.append({
            "code": code,
            "raw_code": html.unescape(dates[0]).strip(),
            "date": date,
            "title": title or code,
            "poster": poster,
            "url": href if href.startswith("http") else base + href,
        })
    return out


def parse_movie(text):
    """从番号详情页取片商与女优：{studio:{id,name}, stars:[{id,name}]}。"""
    studio = None
    m = STUDIO_RE.search(text or "")
    if m:
        studio = {"id": m.group(1), "name": html.unescape(m.group(2)).strip()}
    stars, seen = [], set()
    for sid, name in STAR_RE.findall(text or ""):
        if sid in seen:
            continue
        seen.add(sid)
        stars.append({"id": sid, "name": html.unescape(name).strip()})
    return {"studio": studio, "stars": stars}


_SLUG_RE = re.compile(r"^(\d*)([A-Z]+)(\d+)([A-Z]*)$")


def slug(code):
    """'SSIS001' / 'SSIS-001' / '08SSIS378' → 'SSIS-001' / 'SSIS-378'。

    本地文件名常带日期前缀（08SSIS378），前导数字要丢掉，否则详情页 404。
    """
    c = _code(code)
    m = _SLUG_RE.match(c)
    if not m:
        return c
    return f"{m.group(2)}-{m.group(3)}{('-' + m.group(4)) if m.group(4) else ''}"


def studio_of(code, cfg=None):
    """用任一番号查出它属于哪个片商 → (studio_id, studio_name)。"""
    if not code:
        return "", ""
    text = get("/" + slug(code), cfg)
    info = parse_movie(text)
    st = info.get("studio") or {}
    return st.get("id") or "", st.get("name") or ""


def crawl_studio(sid, cfg=None, start_page=1, year_floor=2000, max_pages=900,
                 delay=0.35, on_page=None, stop=None):
    """按页抓一个片商的全部作品，直到 404 / 不满一页 / 早于 year_floor。

    返回 (rows, state)；state = {page, exhausted, oldest}
    """
    cfg = cfg or config.load()
    rows, seen = [], set()
    last = max(1, int(start_page)) - 1
    exhausted = False
    oldest = 9999
    for page in range(max(1, int(start_page)), max_pages + 1):
        if stop is not None and stop.is_set():
            break
        path = f"/studio/{sid}" if page == 1 else f"/studio/{sid}/{page}"
        try:
            text = get(path, cfg)
        except JavBusError:
            if page == 1:
                raise
            exhausted = True
            break
        items = parse_list(text, _base(cfg))
        if not items:
            exhausted = True
            break
        last = page
        for it in items:
            if it["code"] in seen:
                continue
            seen.add(it["code"])
            rows.append(it)
        years = [int(it["date"][:4]) for it in items if it["date"][:4].isdigit()]
        if years:
            oldest = min(oldest, min(years))
        if on_page:
            on_page(page, len(rows), oldest)
        if years and min(years) < int(year_floor):
            exhausted = True
            break
        if len(items) < PAGE_SIZE:
            exhausted = True
            break
        if delay:
            time.sleep(delay)
    return rows, {"page": last, "exhausted": exhausted, "oldest": oldest}


def build_av_catalog(prefixes, samples, cursors, known=None, resume=True, cfg=None,
                     year_floor=2000, max_pages=900, delay=0.35,
                     progress=None, on_step=None, stop=None, on_batch=None):
    """按「番号前缀 → 片商」逐个抓全史，返回统一 catalog 行。

    prefixes：要抓的前缀（按本地库出现频次降序）
    samples：{prefix: 任一番号}，用来反查 studio id
    cursors：断点游标 {prefix: {sid, page, done}}，调用方负责持久化
    known：已有 av_code 集合；用来判断「这一页有没有新东西」
    on_batch(prefix, rows)：每抓完一个片商回调一次。给了它就不再往内存里囤
        （全量几十万条，囤起来会吃光容器内存），调用方负责入库 + 存游标。
    """
    cfg = cfg or config.load()
    known = known if known is not None else set()
    rows = []
    done_n = 0
    total = len(prefixes) or 1
    # 多个番号前缀经常属于同一个片商（SSIS/SSNI/SONE 都是 S1）→ 同一片商只抓一次
    seen_sids = {}
    for p, v in cursors.items():
        if isinstance(v, dict) and v.get("sid"):
            seen_sids.setdefault(v["sid"], p)
    for idx, prefix in enumerate(prefixes, 1):
        st = cursors.setdefault(prefix, {})
        if st.get("done"):
            if on_step:
                on_step(idx, total, f"{prefix} 已完成，跳过")
            continue
        if stop is not None and stop.is_set():
            break
        sid = st.get("sid") or ""
        sname = st.get("studio") or ""
        if not sid:
            code = samples.get(prefix) or ""
            if progress:
                progress(f"查片商：{prefix}（用 {code or '无样本'}）…")
            try:
                sid, sname = studio_of(code, cfg)
            except Exception as e:
                progress(f"  ! {prefix} 反查片商失败：{e}")
                sid, sname = "", ""
            if not sid:
                if progress:
                    progress(f"  · {prefix} 未找到片商（样本 {code or '空'}），跳过")
                st.update({"done": True, "note": "未找到片商",
                           "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
                if on_batch:
                    on_batch(prefix, [])
                if on_step:
                    on_step(idx, total, f"{prefix} 未找到片商，跳过")
                continue
            st.update({"sid": sid, "studio": sname})
            if progress:
                progress(f"  {prefix} → 片商 {sname or sid}（{sid}）")
        owner = seen_sids.get(sid)
        if owner and owner != prefix:
            st.update({"done": True, "note": f"同片商 {sid}，随 {owner} 抓过",
                       "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
            if progress:
                progress(f"  · {prefix} 与 {owner} 同片商（{sname or sid}），跳过")
            if on_batch:
                on_batch(prefix, [])
            if on_step:
                on_step(idx, total, f"{prefix} 同片商跳过")
            continue
        seen_sids[sid] = prefix
        start = 1
        if resume:
            start = max(1, int(st.get("page") or 1))
        if start > max_pages:
            st.update({"done": True, "note": f"已到页数上限 {max_pages}",
                       "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
            if on_batch:
                on_batch(prefix, [])
            if on_step:
                on_step(idx, total, f"{prefix} 到页数上限，跳过")
            continue

        def _pg(page, n, oldest, _p=prefix, _s=sname or sid):
            if on_step:
                on_step(idx - 1, total, f"{_p}（{_s}）第 {page} 页 · 累计 {n} 部 · 最早 {oldest}")

        got, state = crawl_studio(sid, cfg, start_page=start, year_floor=year_floor,
                                  max_pages=max_pages, delay=delay, on_page=_pg, stop=stop)
        added, batch = 0, []
        for it in got:
            y = int(it["date"][:4]) if it["date"][:4].isdigit() else None
            if y and y < int(year_floor):
                continue
            added += 1
            batch.append({
                "cid": f"javbus:{it['code']}",
                "source": "javbus",
                "category": "AV",
                "title": it["raw_code"],
                "original_title": it["title"],
                "year": y,
                "release_date": it["date"],
                "poster": it["poster"],
                "rating": None,
                "popularity": 0,
                "av_code": it["code"],
                "extra": {"media": "movie", "site": "javbus", "url": it["url"],
                          "studio": sname or sid, "studio_id": sid, "prefix": prefix},
            })
        known.update(it["code"] for it in got)
        st.update({"page": state["page"] + (0 if state["exhausted"] else 1),
                   "done": bool(state["exhausted"]),
                   "oldest": state["oldest"],
                   "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        if on_batch:
            on_batch(prefix, batch)          # 立即入库 + 存游标，不囤内存
        else:
            rows.extend(batch)
        done_n = done_n + len(batch)
        if progress:
            tag = "已抓完" if state["exhausted"] else f"下次从第 {st['page']} 页继续"
            progress(f"  ✓ {prefix}（{sname or sid}）{len(got)} 部，最早 {state['oldest']} · {tag}")
        if on_step:
            on_step(idx, total, f"{prefix} 完成 · 累计 {done_n} 部")
    return rows
