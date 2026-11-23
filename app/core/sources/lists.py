"""榜单源：TMDB 官方榜 / 豆瓣榜 / 主流流媒体平台在架榜。

统一输出成 catalog 行（和 build_catalog 同构），额外带 `lists` 字段记录所属榜单。
一个条目可同时出现在多个榜单里（lists 用逗号分隔累积）。
"""
from .. import config
from . import douban as douban_src
from . import tmdb as tmdb_src

# ---------------- 榜单定义 ----------------
# src: trending / chart / provider / douban
LIST_DEFS = [
    # --- TMDB 官方 ---
    {"key": "tmdb_trend_movie", "name": "TMDB 趋势 · 电影", "group": "TMDB",
     "src": "trending", "media": "movie", "window": "week", "category": "电影"},
    {"key": "tmdb_trend_tv", "name": "TMDB 趋势 · 剧集", "group": "TMDB",
     "src": "trending", "media": "tv", "window": "week", "category": "剧集"},
    # top=250：这两档是全时段评分榜，固定取当前快照的前 250 名，不受「拉多少条」影响。
    # min_votes：TMDB 官方 top_rated 是纯分数排序，几张票的新片就能冲 9.1；带票数门槛
    # 才是公认高分榜（前几名会是肖申克 / 教父这种）。电影 1 万票、剧集 1 千票都还有
    # 500+ 条可选，够凑 250。
    {"key": "tmdb_top_movie", "name": "TMDB TOP250 · 电影", "group": "TMDB",
     "src": "chart", "media": "movie", "kind": "top_rated", "category": "电影",
     "top": 250, "min_votes": 10000},
    {"key": "tmdb_top_tv", "name": "TMDB TOP250 · 剧集", "group": "TMDB",
     "src": "chart", "media": "tv", "kind": "top_rated", "category": "剧集",
     "top": 250, "min_votes": 1000},
    {"key": "tmdb_now", "name": "正在上映", "group": "TMDB",
     "src": "chart", "media": "movie", "kind": "now_playing", "category": "电影"},
    {"key": "tmdb_soon", "name": "即将上映", "group": "TMDB",
     "src": "chart", "media": "movie", "kind": "upcoming", "category": "电影"},

    # --- 豆瓣 ---
    # top=250：全时段排名，不按年份切开。
    # 豆瓣的 /j/new_search_subjects 最多只给 80 条，凑不出 250，所以两档改走：
    #   电影 → 官方 /top250 页面（10 页 × 25 条）
    #   剧集 → 没有官方 TOP250，用 6 个语种标签按评分排序拼（去重后取前 250）
    {"key": "douban_top_movie", "name": "豆瓣 TOP250 · 电影", "group": "豆瓣",
     "src": "douban", "mode": "top250", "media": "movie", "category": "电影", "top": 250},
    {"key": "douban_top_tv", "name": "豆瓣 TOP250 · 剧集", "group": "豆瓣",
     "src": "douban", "mode": "tv_top", "media": "tv", "category": "剧集", "top": 250,
     "need_tmdb": True,
     "drop_genres": ["纪录", "动画", "真人秀", "脱口秀", "新闻", "儿童"]},
    {"key": "douban_hot", "name": "豆瓣热门 · 电影", "group": "豆瓣",
     "src": "douban", "tags": "电影", "sort": "U", "rating": "0,10",
     "media": "movie", "category": "电影"},

    # --- 流媒体平台（watch_region=US，TMDB 只在美国区有平台在架数据）---
    {"key": "nf_movie", "name": "Netflix · 电影", "group": "平台",
     "src": "provider", "media": "movie", "provider": 8, "category": "电影"},
    {"key": "nf_tv", "name": "Netflix · 剧集", "group": "平台",
     "src": "provider", "media": "tv", "provider": 8, "category": "剧集"},
    {"key": "disney_movie", "name": "Disney+ · 电影", "group": "平台",
     "src": "provider", "media": "movie", "provider": 337, "category": "电影"},
    {"key": "disney_tv", "name": "Disney+ · 剧集", "group": "平台",
     "src": "provider", "media": "tv", "provider": 337, "category": "剧集"},
    {"key": "hbo_movie", "name": "HBO Max · 电影", "group": "平台",
     "src": "provider", "media": "movie", "provider": 1899, "category": "电影"},
    {"key": "hbo_tv", "name": "HBO Max · 剧集", "group": "平台",
     "src": "provider", "media": "tv", "provider": 1899, "category": "剧集"},
    {"key": "prime_movie", "name": "Prime Video · 电影", "group": "平台",
     "src": "provider", "media": "movie", "provider": 9, "category": "电影"},
    {"key": "appletv_movie", "name": "Apple TV+ · 电影", "group": "平台",
     "src": "provider", "media": "movie", "provider": 350, "category": "电影"},
    {"key": "hulu_movie", "name": "Hulu · 电影", "group": "平台",
     "src": "provider", "media": "movie", "provider": 15, "category": "电影"},
]
BY_KEY = {d["key"]: d for d in LIST_DEFS}

# 榜单条目的地区：TMDB 不返回 origin_country，用 original_language 做保守推断
LANG_REGION = {
    "zh": "大陆", "cn": "大陆", "ja": "日本", "ko": "韩国", "hi": "印度",
    "th": "东南亚", "vi": "东南亚", "id": "东南亚", "tl": "东南亚",
    "en": "美国", "fr": "欧洲", "de": "欧洲", "it": "欧洲", "es": "欧洲",
    "ru": "欧洲", "sv": "欧洲", "no": "欧洲", "da": "欧洲", "nl": "欧洲",
    "pl": "欧洲", "pt": "欧洲", "tr": "其他", "ar": "其他",
}


def region_of_lang(lang):
    return LANG_REGION.get((lang or "").lower(), "")


def _pack(r, d, list_key, page_size=None):
    r = dict(r)
    r["category"] = d["category"]
    r["list_key"] = list_key
    lang = r.pop("original_language", "") or ""
    reg = region_of_lang(lang)
    if reg:
        r["region"] = reg
        r["regions"] = reg
    r["genre_str"] = ",".join(r.get("genre_names") or [])
    extra = r.get("extra") or {}
    extra.update({"media": r.get("media"), "list": list_key,
                  "genre_names": r.get("genre_names") or [],
                  "genres": r.get("genres") or []})
    r["extra"] = extra
    return r


def fetch(list_key, limit=40, pages=2, progress=None):
    """拉一个榜单，返回 catalog 行列表。

    `d["top"]` 的榜（TMDB / 豆瓣 TOP250）固定按 250 条抓，与抽屉里的「拉多少条」无关；
    其余榜仍按调用方给的 limit。
    """
    d = BY_KEY.get(list_key)
    if not d:
        raise ValueError(f"未知榜单 {list_key}")
    src = d["src"]
    rows = []
    top = int(d.get("top") or 0)
    want = top or limit

    if src == "trending":
        tm = tmdb_src.TMDB()
        if progress:
            progress(f"TMDB 趋势 {d['media']}…")
        for r in tm.trending(d["media"], d.get("window", "week")):
            rows.append(_pack(r, d, list_key))
        rows = rows[:want]

    elif src == "chart":
        tm = tmdb_src.TMDB()
        # TMDB 每页 20 条；TOP250 → 13 页（260 条，按 want 截断）
        pg = (want + 19) // 20 if top else pages
        kind = d.get("kind", "top_rated")
        mv = int(d.get("min_votes") or 0)
        if progress:
            progress(f"TMDB {kind} {d['media']}（{pg} 页）…")
        if mv:
            rs = tm.discover(d["media"], pages=pg, sort="vote_average.desc",
                             extra={"vote_count.gte": mv})
        else:
            rs = tm.chart(d["media"], kind, pages=pg)
        for r in rs:
            rows.append(_pack(r, d, list_key))
        rows = rows[:want]

    elif src == "provider":
        tm = tmdb_src.TMDB()
        if progress:
            progress(f"TMDB 平台榜 {d.get('provider')} {d['media']}…")
        for r in tm.by_provider(d["media"], d["provider"], pages=pages):
            rows.append(_pack(r, d, list_key))
        rows = rows[:want]

    elif src == "douban":
        mode = d.get("mode") or "search"
        if progress:
            progress(f"豆瓣 {d['name']}（{want} 条）…")
        if mode == "top250":
            raw = douban_src.fetch_top250(limit=want, progress=progress)
        elif mode == "tv_top":
            raw = douban_src.fetch_tv_top(limit=want, progress=progress)
        else:
            raw = douban_src.fetch_list(tags=d["tags"], sort=d["sort"],
                                        rating=d.get("rating", "0,10"), limit=want,
                                        progress=progress)
        if progress:
            progress(f"  抓到 {len(raw)} 条，开始补 TMDB 信息…")
        raw = douban_src.enrich_with_tmdb(raw, media=d["media"], progress=progress)
        # 豆瓣的剧集标签混着纪录片/动画/综艺，靠 TMDB 类型把它们滤掉。
        # 没补上 TMDB 的条目也丢掉：只能按标题比对，还会把类型污染带进来，
        # 宁可少几条也不让剧集榜混进纪录片。
        drop = set(d.get("drop_genres") or [])
        if drop or d.get("need_tmdb"):
            before = len(raw)
            if d.get("need_tmdb"):
                raw = [r for r in raw if r.get("tmdb_id")]
            if drop:
                raw = [r for r in raw if not (set(r.get("genre_names") or []) & drop)]
            if progress:
                progress(f"  过滤后剩 {len(raw)} 条（原 {before} 条）")
        raw = raw[:want]
        for r in raw:
            cid = (f"tmdb:{d['media']}:{r['tmdb_id']}" if r.get("tmdb_id")
                   else f"douban:{r.get('douban_id')}")
            rows.append({
                "cid": cid,
                "source": "douban",
                "media": d["media"],
                "category": d["category"],
                "title": r.get("title", ""),
                "original_title": r.get("original_title", ""),
                "year": r.get("year"),
                "release_date": "",
                "poster": r.get("poster") or r.get("cover") or "",
                "tmdb_id": str(r.get("tmdb_id") or ""),
                "rating": float(r["rate"]) if str(r.get("rate", "")).replace(".", "").isdigit() else None,
                "popularity": 0,
                "overview": r.get("overview", ""),
                "genre_str": "",
                "regions": "",
                "list_key": list_key,
                "extra": {"media": d["media"], "list": list_key, "douban_id": r.get("douban_id"),
                          "douban_url": r.get("url"), "casts": r.get("casts") or [],
                          "overview": r.get("overview", "")},
            })
    return rows
