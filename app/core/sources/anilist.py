"""AniList 里番（成人动画）清单源 —— 免注册、免 API Key。

为什么不用 Bangumi 当主源：它的「里番」标签实测只有 351 条（R18 98 / 18禁 54），
覆盖不到实际发行量；而 AniList 的 isAdult 过滤能翻到 5000 条（平台上限），
每条带日文原名 / 罗马字 / 年份 / 集数 / 封面，正好拿来当里番发行片。

接口：POST https://graphql.anilist.co （GraphQL，公开读）
限流：实测连续 40 页后会被掐一段（后续排序全 429），所以每页间隔 2s、
单页重试 3 次；某排序连续 2 页失败就放弃该排序，等下次手动再拉补全。
"""
import json
import time
import urllib.error
import urllib.request

from .. import config

API = "https://graphql.anilist.co"
UA = "EvoLink/1.0"
PER_PAGE = 50

QUERY = """
query($page:Int,$perPage:Int,$sort:[MediaSort]){
  Page(page:$page,perPage:$perPage){
    pageInfo{ total currentPage lastPage hasNextPage }
    media(type:ANIME, isAdult:true, sort:$sort){
      id
      title{ romaji english native }
      startDate{ year month day }
      episodes
      format
      averageScore
      popularity
      coverImage{ large }
      siteUrl
      genres
    }
  }
}
"""

# 三种排序各取一段，覆盖「新番为主 + 老经典」；重复的按 id 去重
SORTS = [["START_DATE_DESC"], ["POPULARITY_DESC"], ["SCORE_DESC"]]


def _post(payload, proxy, timeout=30):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(API, data=data, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": UA,
    })
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
    opener = urllib.request.build_opener(*handlers)
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _row(m):
    t = m.get("title") or {}
    native, romaji, english = t.get("native") or "", t.get("romaji") or "", t.get("english") or ""
    d = m.get("startDate") or {}
    y = d.get("year")
    date = ""
    if y:
        date = "%04d-%02d-%02d" % (y, d.get("month") or 1, d.get("day") or 1)
    return {
        "cid": "al:%s" % m.get("id"),
        "source": "anilist",
        "category": "里番",
        "title": native or romaji or english,
        "original_title": romaji or native,
        "year": y,
        "release_date": date,
        "poster": ((m.get("coverImage") or {}).get("large") or ""),
        "rating": (m.get("averageScore") or 0) / 10.0 or None,
        "popularity": m.get("popularity") or 0,
        "extra": {
            "url": m.get("siteUrl") or "",
            "episodes": m.get("episodes"),
            "format": m.get("format") or "",
            "genres": m.get("genres") or [],
        },
    }


def fetch(pages=40, progress=None, on_step=None, cursors=None, known=None,
          resume=False, stop_after_known=3):
    """翻 pages 页 × 50 条（三种排序轮转），返回可直接喂给 _upsert 的行。

    容错策略：单页重试 3 次（429 退避 10s）；某排序连续 2 页失败就放弃该排序，
    不拖死整个任务；三个排序全失败才抛错。

    resume=True 时按 cursors（排序名 → {page, done}）接着翻；known 用来提前收工。
    """
    log = progress or (lambda *a: None)
    cfg = config.load()
    proxy = config.proxy_url(cfg) or None
    out, seen = [], set()
    known = known if known is not None else set()
    cursors = cursors if cursors is not None else {}
    pages = max(1, min(int(pages or 40), 100))
    total_pages = pages * len(SORTS)
    done_pages = 0
    for sort in SORTS:
        name = sort[0]
        st = cursors.get(name) or {}
        if resume and st.get("done"):
            done_pages += pages
            log(f"  · {name} 已抓完，跳过")
            continue
        start = max(1, int(st.get("page") or 1)) if resume else 1
        if start > pages:
            cursors[name] = {"page": pages, "done": True, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
            done_pages += pages
            continue
        got, fails = 0, 0
        last, exhausted, streak = start - 1, False, 0
        for p in range(start, pages + 1):
            if on_step:
                on_step(done_pages, total_pages, f"{name} 第 {p}/{pages} 页")
            done_pages += 1
            payload = {"query": QUERY,
                       "variables": {"page": p, "perPage": PER_PAGE, "sort": sort}}
            data = None
            for attempt in range(3):
                try:
                    data = _post(payload, proxy, timeout=25)
                    break
                except urllib.error.HTTPError as e:
                    if e.code == 429:
                        log(f"  · AniList 限流，等 10s 重试")
                        time.sleep(10)
                        continue
                    log(f"  ! AniList HTTP {e.code}")
                    time.sleep(2)
                except Exception as e:
                    log(f"  ! AniList {type(e).__name__}: {e}")
                    time.sleep(2)
            if not data:
                fails += 1
                log(f"  ! {name} 第 {p} 页失败（连续 {fails}）")
                if fails >= 2:
                    log(f"  ! {name} 连续失败，放弃该排序")
                    break
                continue
            fails = 0
            if data.get("errors"):
                log(f"  ! AniList 返回错误：{str(data['errors'])[:100]}")
                break
            page = ((data.get("data") or {}).get("Page") or {})
            media = page.get("media") or []
            if not media:
                exhausted = True
                break
            last = p
            new = 0
            for m in media:
                r = _row(m)
                if r["cid"] not in known:
                    new += 1
                    known.add(r["cid"])
                if r["cid"] in seen:
                    continue
                seen.add(r["cid"])
                out.append(r)
                got += 1
            info = page.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                exhausted = True
                break
            streak = streak + 1 if new == 0 else 0
            if stop_after_known and streak >= stop_after_known:
                log(f"  · {name} 连续 {streak} 页没有新条目，这一档已补齐")
                exhausted = True
                break
            time.sleep(2.0)
        # 游标始终记录（不只在 resume 时）：仪表盘/设置里的「拉取进度」靠它判断档位是否补齐。
        # resume 只决定「从第几页开始翻」，不该决定「要不要记账」——
        # 否则点按钮拉完了，进度还是显示「未开始」。
        cursors[name] = {"page": min(last + 1, pages),
                         "done": bool(exhausted or last >= pages),
                         "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        if got:
            log(f"  ✓ {name} 取到 {got} 条（累计 {len(out)}）")
    if not out:
        raise RuntimeError("AniList 没有取到任何数据（网络或限流），稍后再试")
    return out
