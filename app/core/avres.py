"""AV 番号磁力解析：sukebei + bt4g（纯标准库，走配置里的 HTTP 代理）。

背景（2026-10-18 实测）：
- 兜底检索服务没有 AV 资源，AV 卡片原来只能跳 `t.me/s/{频道}?q=番号`，
  而资源类频道普遍没开网页预览（实测 tgsearchers2 / avdb_chat 返回 0 条消息），
  点开等于什么都没发生。
- 公开 BT 索引站可以直接按番号检索，命中率很高（12 个缺失番号实测 11 个有结果）：
    sukebei.nyaa.si  → RSS，自带 infoHash / size / seeders
    bt4gprx.com      → RSS，<link> 里直接就是 magnet
- 这两个站大陆直连不通，必须走代理；代理地址统一取 config.proxy_url()。

返回统一结构，前端按 kind 渲染：
{"kind":"magnet","items":[{title,url,type,size,size_b,seeds,src}],"counts":{"magnet":n},"code":"MIDA-829"}
"""
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from . import config

TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
]

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_CACHE = {}          # code -> (ts, payload)
_TTL = 600
_MAX = 400


def _opener():
    proxy = ""
    try:
        proxy = config.proxy_url() or ""
    except Exception:
        proxy = ""
    if proxy:
        h = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    else:
        h = urllib.request.ProxyHandler({})
    return urllib.request.build_opener(h)


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with _opener().open(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


# ---------- 番号规范化 ----------

def norm(s):
    """去掉所有非字母数字，转小写。'MIDA-829' / 'MIDA829' 都变 'mida829'。"""
    return re.sub(r"[^0-9a-z]+", "", (s or "").lower())


def dash_code(code):
    """'MIDA829' -> 'MIDA-829'；已经是 MIDA-829 的原样返回。
    拆在最后一个「字母→数字」的边界上（FTHTD213 -> FTHTD-213，NINE126 -> NINE-126）。"""
    c = (code or "").strip().upper()
    if not c:
        return ""
    if "-" in c:
        return c
    m = re.match(r"^([A-Z]+)(\d+)$", c)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    m = re.match(r"^([A-Z]+\d*[A-Z]+)(\d+)$", c)   # 例如 ABP1-123 之类的怪号
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return c


def _size_bytes(s):
    m = re.match(r"([\d.]+)\s*([KMGT])i?B", (s or "").strip(), re.I)
    if not m:
        return 0
    v = float(m.group(1))
    unit = m.group(2).upper()
    mul = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}[unit]
    return int(v * mul)


def _magnet(infohash, name=""):
    base = "magnet:?xt=urn:btih:" + infohash
    if name:
        base += "&dn=" + urllib.parse.quote(name)
    for t in TRACKERS:
        base += "&tr=" + urllib.parse.quote(t, safe="")
    return base


# ---------- 数据源 ----------

def _parse_rss(xml):
    out = []
    for raw in re.findall(r"<item>(.*?)</item>", xml, re.S):
        def tag(name):
            m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", raw, re.S)
            return (m.group(1) if m else "").strip()
        title = tag("title")
        if not title:
            continue
        out.append({
            "title": re.sub(r"<!\[CDATA\[|\]\]>", "", title),
            "link": re.sub(r"<!\[CDATA\[|\]\]>", "", tag("link")).replace("&amp;", "&"),
            "hash": tag("nyaa:infoHash"),
            "size": tag("nyaa:size"),
            "seeds": tag("nyaa:seeders"),
        })
    return out


def sukebei(code):
    q = urllib.parse.quote(code)
    xml = _get(f"https://sukebei.nyaa.si/?page=rss&q={q}&c=0_0&f=0")
    need = norm(code)
    items = []
    for it in _parse_rss(xml):
        if need not in norm(it["title"]):
            continue
        if not it["hash"]:
            continue
        items.append({
            "title": it["title"],
            "url": _magnet(it["hash"], it["title"]),
            "type": "magnet",
            "size": it["size"],
            "size_b": _size_bytes(it["size"]),
            "seeds": int(it["seeds"] or 0),
            "src": "sukebei",
        })
    return items


def bt4g(code):
    q = urllib.parse.quote(code)
    xml = _get(f"https://bt4gprx.com/search?q={q}&page=rss")
    need = norm(code)
    items = []
    for it in _parse_rss(xml):
        if need not in norm(it["title"]):
            continue
        link = it["link"]
        if not link.startswith("magnet:"):
            continue
        hash_m = re.search(r"btih:([0-9a-f]{40}|[a-z2-7]{32})", link, re.I)
        items.append({
            "title": it["title"],
            "url": link,
            "type": "magnet",
            "size": "",
            "size_b": 0,
            "seeds": 0,
            "src": "bt4g",
            "_hash": (hash_m.group(1).lower() if hash_m else link[-40:]),
        })
    return items


# ---------- 对外入口 ----------

def _dedupe(items):
    seen, out = set(), []
    for it in items:
        key = it.get("_hash") or re.search(r"btih:([^&]+)", it["url"])
        key = key if isinstance(key, str) else (key.group(1).lower() if key else it["url"])
        if key in seen:
            continue
        seen.add(key)
        it.pop("_hash", None)
        out.append(it)
    return out


_TITLE_SPLIT = re.compile(r"\s*[～〜〜：:・]|\s*（|\s*\(|\s+第[一二三四五六七八九十\d]+[话話幕章夜]")
_CORE_JUNK = re.compile(r"(?i)^(ova|oad|the|movie|第一部|第2部)[\s．.]*")


def title_core(title):
    """里番没有番号，拿标题主干去搜：砍掉副标题（～…～）和「第N幕」这类尾巴。"""
    if not title:
        return ""
    t = _TITLE_SPLIT.split(str(title))[0].strip()
    t = _CORE_JUNK.sub("", t).strip(" -_.·～〜")
    return t if len(t) >= 3 else ""


def search(av_code, title=""):
    """按番号查磁力；里番没番号时退回「标题主干」查。返回 {kind, code, items, ...}。"""
    code = dash_code(av_code)
    if not code:
        q = title_core(title)
        if not q:
            return {"kind": "magnet", "code": "", "items": [], "counts": {},
                    "message": "这条没有番号，也无法从标题提取关键词"}
        return _search_terms([q], q, "标题主干")

    hit = _CACHE.get(code)
    if hit and time.time() - hit[0] < _TTL:
        return dict(hit[1], cached=True)

    forms = [code]
    plain = code.replace("-", "")
    if plain != code:
        forms.append(plain)
    return _search_terms(forms, code, "番号")


def _search_terms(forms, cache_key, what):
    items, errs = [], []
    jobs = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        for f in forms:
            jobs.append(ex.submit(sukebei, f))
            jobs.append(ex.submit(bt4g, f))
        for j in jobs:
            try:
                items.extend(j.result(timeout=25) or [])
            except Exception as e:
                errs.append(f"{type(e).__name__}: {e}")

    items = _dedupe(items)
    # 磁力站没有 seeders 的（bt4g）排后面，但有 1080p/中字 的往前放
    def rank(it):
        bonus = 0
        t = it["title"].lower()
        if "1080" in t or "fhd" in t:
            bonus += 2
        if "2160" in t or "4k" in t or "uhd" in t:
            bonus += 1
        if "中字" in t or "中文" in t or "字幕" in t:
            bonus += 1
        return (-it["seeds"], -bonus, -it["size_b"])
    items.sort(key=rank)
    items = items[:40]

    counts = {}
    for it in items:
        counts[it["type"]] = counts.get(it["type"], 0) + 1
    res = {
        "kind": "magnet",
        "code": cache_key,
        "items": items,
        "counts": counts,
        "sources": sorted({i["src"] for i in items}),
        "message": "" if items else f"两个磁力站都没搜到（{what}「{cache_key}」，新片/冷门常见）",
    }
    if errs and not items:
        res["message"] = "检索失败：" + "；".join(errs[:2])

    _CACHE[cache_key] = (time.time(), res)
    if len(_CACHE) > _MAX:
        _CACHE.clear()
    return res
