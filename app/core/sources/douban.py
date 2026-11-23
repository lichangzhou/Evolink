"""豆瓣榜单源（走代理，社区接口，无官方开放 API）。

实测 2026-10-18：`/j/new_search_subjects` 经 NAS mihomo 可用，
返回 title / rate / cover / url / casts / directors，但**不含年份和 TMDB id**。
所以这里分两步：先抓榜，再用 TMDB search 按片名补 tmdb_id 与年份
（补不上就保留豆瓣条目，只用标题参与比对）。
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from .. import config

BASE = "https://movie.douban.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

# 剧集没有官方 TOP250，用这几个标签的「评分排序」拼出来
TV_TAGS = ["国产剧", "美剧", "英剧", "日剧", "韩剧", "港剧"]


class DoubanError(RuntimeError):
    pass


def _opener():
    cfg = config.load()
    proxy = config.proxy_url(cfg)
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()


def fetch_list(tags="电影", sort="S", rating="8.5,10", limit=40, progress=None):
    """抓一页豆瓣列表。sort: S=评分 U=综合 T=最新。"""
    out, start = [], 0
    while len(out) < limit:
        q = urllib.parse.urlencode({"tags": tags, "sort": sort, "range": rating, "start": start})
        url = f"{BASE}/j/new_search_subjects?{q}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                       "Referer": BASE + "/"})
            with _opener().open(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as e:
            raise DoubanError(f"豆瓣 HTTP {e.code}")
        except Exception as e:
            raise DoubanError(f"豆瓣请求失败: {e}")
        rows = data.get("data") or []
        if not rows:
            break
        for r in rows:
            out.append({
                "douban_id": str(r.get("id") or ""),
                "title": r.get("title") or "",
                "rate": r.get("rate") or "",
                "cover": r.get("cover") or "",
                "url": r.get("url") or "",
                "casts": r.get("casts") or [],
                "directors": r.get("directors") or [],
            })
            if len(out) >= limit:
                break
        start += len(rows)
        if start >= 500:
            break
        time.sleep(0.35)
    return out


def _get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": BASE + "/"})
    try:
        with _opener().open(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        raise DoubanError(f"豆瓣 HTTP {e.code}")
    except Exception as e:
        raise DoubanError(f"豆瓣请求失败: {e}")


def _unescape(s):
    return (s.replace("&nbsp;", " ").replace("&amp;", "&")
             .replace("&quot;", '"').replace("&#39;", "'").replace("&middot;", "·"))


def _rate(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def _parse_top250(html):
    """解析 /top250 页面的 <div class="item"> 块，一页 25 条。"""
    out = []
    for ch in html.split('<div class="item">')[1:]:
        t = re.search(r'<span class="title">([^<]+)</span>', ch)
        if not t:
            continue
        did = re.search(r'/subject/(\d+)/', ch)
        rate = re.search(r'<span class="rating_num"[^>]*>([\d.]+)</span>', ch)
        img = re.search(r'<img[^>]+src="([^"]+)"', ch)
        # 年份在 <p class=""> 的 <br> 之后：1994&nbsp;/&nbsp;美国&nbsp;/&nbsp;犯罪 剧情
        year = None
        p = re.search(r'<p class="">(.*?)</p>', ch, re.S)
        if p:
            ym = re.search(r'(\d{4})', p.group(1).split("<br>")[-1])
            if ym:
                year = int(ym.group(1))
        out.append({
            "douban_id": did.group(1) if did else "",
            "title": _unescape(t.group(1)).strip(),
            "rate": rate.group(1) if rate else "",
            "cover": (img.group(1) if img else "").replace("&amp;", "&"),
            "url": f"{BASE}/subject/{did.group(1)}/" if did else "",
            "year": year,
            "casts": [],
            "directors": [],
        })
    return out


def fetch_top250(limit=250, progress=None):
    """豆瓣官方电影 TOP250：10 页 × 25 条，全时段排名，与年份无关。"""
    out, page_no = [], 0
    for start in range(0, min(int(limit or 250), 250), 25):
        page_no += 1
        rows = _parse_top250(_get(f"{BASE}/top250?start={start}&filter="))
        if progress:
            progress(f"  豆瓣 TOP250 第 {page_no}/10 页：{len(rows)} 条")
        if not rows:
            break
        out += rows
        time.sleep(0.5)
    return out[:limit]


def _search_subjects(tag, sort="rank", start=0, page_limit=20, type_="tv"):
    q = urllib.parse.urlencode({"type": type_, "tag": tag, "sort": sort,
                                "page_limit": page_limit, "page_start": start})
    data = json.loads(_get(f"{BASE}/j/search_subjects?{q}"))
    return data.get("subjects") or []


def fetch_tv_top(limit=250, progress=None):
    """豆瓣剧集「高分 250」：剧集没有官方 TOP250，用各语种标签按评分排序拼。

    豆瓣的「美剧 / 英剧」标签里混着纪录片和动画（蓝色星球、猫和老鼠都在里面），
    所以这里多抓一倍候选，交给调用方按 TMDB 类型过滤后再截断。
    """
    out, seen = [], set()
    per_tag = 100
    for tag in TV_TAGS:
        got = 0
        for start in range(0, per_tag, 20):
            try:
                rows = _search_subjects(tag, sort="rank", start=start)
            except DoubanError:
                rows = []
            if not rows:
                break
            for r in rows:
                did = str(r.get("id") or "")
                if not did or did in seen:
                    continue
                seen.add(did)
                out.append({
                    "douban_id": did,
                    "title": r.get("title") or "",
                    "rate": str(r.get("rate") or ""),
                    "cover": r.get("cover") or "",
                    "url": r.get("url") or "",
                    "year": None,
                    "casts": r.get("casts") or [],
                    "directors": r.get("directors") or [],
                })
                got += 1
            time.sleep(0.35)
        if progress:
            progress(f"  豆瓣剧集 {tag}：{got} 条")
    out.sort(key=lambda x: _rate(x.get("rate")), reverse=True)
    return out


def _enrich_one(tm, media, r):
    title = r["title"]
    # 去掉季/集后缀噪声，"老友记 第二季" → "老友记"
    clean = title.split(" 第")[0].strip()
    hit = None
    for q in (clean, title):
        try:
            res = tm.search(media, q)
        except Exception:
            res = []
        if res:
            hit = res[0]
            break
    if not hit:
        return False
    r["tmdb_id"] = hit["tmdb_id"]
    r["year"] = hit["year"] or r.get("year")
    r["poster"] = r.get("cover") or hit["poster"]
    r["original_title"] = hit.get("original_title") or ""
    r["rating_tmdb"] = hit.get("rating")
    r["genre_names"] = hit.get("genre_names") or []
    r["overview"] = hit.get("overview") or ""
    return True


def enrich_with_tmdb(rows, media="movie", progress=None, workers=8):
    """给豆瓣条目补 tmdb_id / 年份 / 中文简介。补不上就原样保留。

    250 条串行要十几分钟，这里 8 并发；进度回调只在主线程里发。
    """
    from . import tmdb as tmdb_src
    tm = tmdb_src.TMDB()
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_enrich_one, tm, media, r) for r in rows]
        for f in as_completed(futs):
            done += 1
            try:
                f.result()
            except Exception:
                pass
            if progress and done % 20 == 0:
                progress(f"  豆瓣补 TMDB：{done}/{len(rows)}")
    return rows
