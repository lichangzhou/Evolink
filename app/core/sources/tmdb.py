"""TMDB 数据源（走代理，因为 api.themoviedb.org 在国内直连不通）。

实测 2026-10-18：直连 000，经 NAS mihomo 7890 → 401（无 key），通道可用。
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import config

IMG = "https://image.tmdb.org/t/p/w500"
BACKDROP = "https://image.tmdb.org/t/p/w780"

# 分类 → (tmdb 媒体类型, 附加参数)
CATEGORY_SPEC = {
    "电影":      ("movie", {}),
    "剧集":      ("tv", {}),
    "动画":      ("both", {"with_genres": "16"}),
    "纪录片":    ("both", {"with_genres": "99"}),
    "综艺":      ("tv", {"with_genres": "10764|10767|10763"}),
    "演唱会":    ("movie", {"with_genres": "10402"}),
}

# ---- 类型（TMDB genre_id → 中文）----
# movie 与 tv 的 id 有重叠但不完全一致，这里合并成一套展示用名称。
GENRE_CN = {
    # movie
    28: "动作", 12: "冒险", 16: "动画", 35: "喜剧", 80: "犯罪", 99: "纪录",
    18: "剧情", 10751: "家庭", 14: "奇幻", 36: "历史", 27: "恐怖",
    10402: "音乐", 9648: "悬疑", 10749: "爱情", 878: "科幻", 10770: "电视电影",
    53: "惊悚", 10752: "战争", 37: "西部",
    # tv 独有
    10759: "动作", 10762: "儿童", 10763: "新闻", 10764: "真人秀",
    10765: "科幻", 10766: "肥皂剧", 10767: "脱口秀", 10768: "战争",
}
# 一个 id 对应多个展示类型的，单独展开
GENRE_EXPAND = {
    10759: ["动作", "冒险"],   # tv 动作冒险
    10765: ["科幻", "奇幻"],   # tv 科幻奇幻
}
# 展示顺序（用户指定）；不在表里的按名称排在后面
GENRE_ORDER = ["动作", "冒险", "喜剧", "犯罪", "纪录", "剧情", "家庭", "奇幻",
               "历史", "恐怖", "音乐", "悬疑", "爱情", "科幻", "惊悚", "战争"]


def genre_names(ids):
    """TMDB genre_ids → 中文类型列表（去重、按固定顺序）。"""
    out = []
    for gid in ids or []:
        try:
            gid = int(gid)
        except (TypeError, ValueError):
            continue
        names = GENRE_EXPAND.get(gid) or ([GENRE_CN[gid]] if gid in GENRE_CN else [])
        for n in names:
            if n not in out:
                out.append(n)
    return sorted(out, key=lambda n: (GENRE_ORDER.index(n) if n in GENRE_ORDER else 90, n))


# ---- 地区 ----
# TMDB movie discover 的结果不带 origin_country，所以地区只能靠「按国家请求」确定：
# 用 with_origin_country 分组拉取，一次请求的国家集合就是该条目的地区归属。
# 多国用 | 连接（TMDB 的 OR 语法），一次请求即可覆盖整档。
#
# 2026-10-18 二版：原来 5 档（亚洲/欧美 各塞十几个国家）粒度太粗，
# 且每档页数相同导致「每档都是 60 条」。现在拆成 10 档国家/区域，并支持按档配额。
REGIONS = [
    ("大陆",     "CN"),
    ("香港",     "HK"),
    ("台湾",     "TW"),
    ("日本",     "JP"),
    ("韩国",     "KR"),
    ("印度",     "IN"),
    ("东南亚",   "TH|SG|MY|ID|PH|VN"),
    ("美国",     "US|CA"),
    ("欧洲",     "GB|FR|DE|IT|ES|NL|BE|SE|NO|DK|FI|IE|AT|CH|PL|PT|CZ|GR|HU|RO|UA|RU"),
    ("其他",     "AU|NZ|BR|MX|AR|CL|CO|ZA|TR|IL|AE|SA|PK|BD|LK|MM|KH|MN|NP"),
]
REGION_MAP = dict(REGIONS)
REGION_NAMES = [r[0] for r in REGIONS]

# 默认配额权重：反映各产区的实际产出体量与国内观看习惯，差异是刻意的
# （旧版每档 60 条就是因为权重全等）。
DEFAULT_WEIGHTS = {
    "大陆": 100, "香港": 45, "台湾": 45, "日本": 90, "韩国": 60,
    "印度": 35, "东南亚": 25, "美国": 130, "欧洲": 90, "其他": 30,
}



class TMDBError(RuntimeError):
    pass


class TMDB:
    def __init__(self, key=None, proxy=None):
        cfg = config.load()
        # 有人从网页复制会带上换行/空格，末端空白会让 api_key 变成 %0A → TMDB 直接 401
        self.key = (key or cfg.get("tmdb_api_key") or "").strip()
        # 有人连 "Bearer " 一起抄进来，去掉，否则会被当成 v3 Key
        if self.key[:7].lower() == "bearer ":
            self.key = self.key[7:].strip()
        self.proxy = proxy if proxy is not None else config.proxy_url(cfg)
        self.lang = cfg.get("tmdb_lang", "zh-CN")
        if not self.key:
            raise TMDBError("未配置 TMDB API Key")

    def _opener(self):
        if self.proxy:
            return urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy}))
        return urllib.request.build_opener()

    def _is_v4(self):
        """v4 「API Read Access Token」是 JWT，eyJ 开头；只能放 Authorization 头。
        放错位置（当成 api_key 查询参数）TMDB 一律回 401 —— 这是「Key 明明是对的却显示无效」的头号原因。"""
        return self.key.startswith("eyJ")

    def get(self, path, **params):
        params.setdefault("language", self.lang)
        headers = {"Accept": "application/json"}
        if self._is_v4():
            headers["Authorization"] = "Bearer " + self.key
        else:
            params["api_key"] = self.key
        url = f"https://api.themoviedb.org/3{path}?{urllib.parse.urlencode(params)}"
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers=headers)
                with self._opener().open(req, timeout=25) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(2 * (attempt + 1))
                    continue
                if e.code == 401:
                    kind = "v4 读取令牌（eyJ 开头）" if self._is_v4() else "v3 API Key"
                    raise TMDBError(
                        f"TMDB 拒绝了这个 {kind}（401）。依次检查："
                        "① 用的是同一账号「API 设置」页里的凭据，不是别的网站抄来的；"
                        "② v3 Key 是 32 位字母数字、v4 令牌是 eyJ 开头的长串，两者都能用但必须整段复制；"
                        "③ 复制时没带空格/换行；④ 该账号的 API 申请状态是已批准（刚申请可能要等几分钟）。")
                raise TMDBError(f"TMDB {path} HTTP {e.code}: {e.read()[:200]}")

            except Exception as e:
                if attempt == 2:
                    raise TMDBError(f"TMDB {path} 请求失败: {e}")
                time.sleep(1.5)
        raise TMDBError(f"TMDB {path} 重试耗尽")

    def discover(self, media, pages=5, sort="popularity.desc", extra=None, on_page=None, **kw):
        """分页拉一个列表，返回归一化 dict 列表。"""
        out = []
        extra = dict(extra or {})
        extra.update({k: v for k, v in kw.items() if v})
        if sort.startswith("vote_average"):
            extra.setdefault("vote_count.gte", 200)
        for page in range(1, pages + 1):
            data = self.get(f"/discover/{media}", page=page, sort_by=sort, **extra)
            rows = data.get("results", [])
            if not rows:
                break
            for r in rows:
                out.append(_norm(r, media))
            if on_page:
                on_page(page, int(data.get("total_pages") or 1), len(out))
            if page >= data.get("total_pages", 1):
                break
        return out

    def trending(self, media="movie", window="week"):
        data = self.get(f"/trending/{media}/{window}")
        return [_norm(r, media) for r in data.get("results", [])]

    def chart(self, media, kind, pages=1, region="US"):
        """官方榜单：top_rated / now_playing / upcoming / popular。"""
        if kind == "popular":
            return self.discover(media, pages=pages, sort="popularity.desc")
        out = []
        for page in range(1, pages + 1):
            params = {"page": page}
            if kind in ("now_playing", "upcoming"):
                params["region"] = region
            data = self.get(f"/{media}/{kind}", **params)
            rows = data.get("results", [])
            if not rows:
                break
            out += [_norm(r, media) for r in rows]
            if page >= data.get("total_pages", 1):
                break
        return out

    def by_provider(self, media, provider_id, pages=1, region="US"):
        """流媒体平台在架榜（Netflix / Disney+ / HBO Max …）。"""
        return self.discover(media, pages=pages, sort="popularity.desc",
                             with_watch_providers=str(provider_id),
                             watch_region=region)

    def provider_list(self, media="movie", region="US"):
        data = self.get(f"/watch/providers/{media}", watch_region=region)
        return {p["provider_id"]: p.get("provider_name") or "" for p in data.get("results", [])}

    def search(self, media, query, year=None):
        p = {"query": query}
        if year:
            p["year" if media == "movie" else "first_air_date_year"] = year
        data = self.get(f"/search/{media}", **p)
        return [_norm(r, media) for r in data.get("results", [])[:8]]



def _norm(r, media):
    if media == "movie":
        title, orig, date = r.get("title"), r.get("original_title"), r.get("release_date")
    else:
        title, orig, date = r.get("name"), r.get("original_name"), r.get("first_air_date")
    poster = r.get("poster_path")
    year = None
    if date and len(date) >= 4:
        try:
            year = int(date[:4])
        except ValueError:
            pass
    gids = r.get("genre_ids") or []
    return {
        "cid": f"tmdb:{media}:{r['id']}",
        "source": "tmdb",
        "media": media,
        "tmdb_id": str(r["id"]),
        "title": title or "",
        "original_title": orig or "",
        "year": year,
        "release_date": date or "",
        "poster": (IMG + poster) if poster else "",
        "backdrop": (BACKDROP + r["backdrop_path"]) if r.get("backdrop_path") else "",
        "rating": round(r.get("vote_average") or 0, 1),
        "popularity": round(r.get("popularity") or 0, 1),
        "original_language": r.get("original_language") or "",
        "overview": (r.get("overview") or "")[:400],
        "genres": gids,
        "genre_names": genre_names(gids),
    }


def plan_quota(total, weights=None):
    """把「每分类总条数」按地区权重分配到 10 个档位。

    旧版每档页数相同 → 每档都是 pages×20 条，看起来「五个地区全部 60」。
    现在权重不等，且总量固定，各档自然拉开差距。
    """
    w = dict(DEFAULT_WEIGHTS)
    for k, v in (weights or {}).items():
        if k in w and v is not None:
            try:
                w[k] = float(v)
            except (TypeError, ValueError):
                pass
    s = sum(w.get(n, 0) for n, _ in REGIONS) or 1
    out = {}
    for name, _ in REGIONS:
        out[name] = max(1, int(round(int(total) * w.get(name, 0) / s)))
    return out


def region_totals(media="movie", extra=None, since="2018-01-01", min_votes=30):
    """按地区查 TMDB 的实际条目总量，用于校准配额权重。

    注意：不限时间时 TMDB 的 total_results 会被截在 20001，所有大区都一样，
    校准就失效了。所以默认只看 2018 年之后 + 有一定投票数的片子，
    这个口径下各区数值有区分度，也贴近「近几年实际在产出的量」。
    """
    tm = TMDB()
    base = {"vote_count.gte": min_votes}
    if media == "movie":
        base["primary_release_date.gte"] = since
        base["primary_release_date.lte"] = "2026-12-31"
    else:
        base["first_air_date.gte"] = since
        base["first_air_date.lte"] = "2026-12-31"
    base.update(extra or {})
    out = {}
    for name, cc in REGIONS:
        try:
            d = tm.get(f"/discover/{media}", page=1, sort_by="popularity.desc",
                       with_origin_country=cc, **base)
            out[name] = int(d.get("total_results") or 0)
        except TMDBError:
            out[name] = 0
    return out


def plan_jobs(specs, weights=None, incremental=False):
    """把「分类 × 地区 × media」展开成任务队列。

    队列同时也是「缺口账本」的计量单位：一档跑完就是一处缺口补上。
    """
    jobs = []
    for s in specs:
        cat = s["category"]
        media = s.get("media") or CATEGORY_SPEC.get(cat, ("movie", {}))[0]
        extra = dict(CATEGORY_SPEC.get(cat, ("", {}))[1])
        if s.get("with_genres"):
            extra["with_genres"] = str(s["with_genres"])
        if s.get("year_from"):
            gte_key = "primary_release_date.gte" if media == "movie" else "first_air_date.gte"
            lte_key = "primary_release_date.lte" if media == "movie" else "first_air_date.lte"
            extra[gte_key] = f"{s['year_from']}-01-01"
            extra[lte_key] = f"{s.get('year_to') or 2100}-12-31"
        medias = ["movie", "tv"] if media == "both" else [media]
        regions = s.get("regions") or REGION_NAMES

        quota = s.get("quota")
        if not quota:
            total = int(s.get("total") or 0)
            if total:
                quota = plan_quota(total, s.get("weights") or weights)
            elif s.get("pages"):
                quota = {n: int(s["pages"]) * 20 for n in REGION_NAMES}
            else:
                quota = plan_quota(400, weights)

        for region in regions:
            country = REGION_MAP.get(region)
            if not country:
                continue
            takes = int(quota.get(region, 0) or 0)
            if takes <= 0:
                continue
            per_media = max(1, takes // len(medias)) if len(medias) > 1 else takes
            # 单档上限 400 页（TMDB 自己 500 页封顶）：全量模式下一档可能要取上万条
            pages = 1 if incremental else max(1, min(400, -(-per_media // 20)))
            for m in medias:
                jobs.append({"category": cat, "media": m, "region": region,
                             "country": country, "extra": extra, "pages": pages,
                             "per_media": per_media,
                             "sort": s.get("sort", "popularity.desc")})
    return jobs


def build_catalog(specs, progress=None, weights=None, incremental=False, on_step=None,
                  cursors=None, known=None, resume=False, stop_after_known=3):
    """按清单定义批量拉取。

    specs: [{category, media, total, sort, with_genres, year_from, regions, quota, pages}]
    - total：该分类的目标总条数，按 weights 分到各地区（推荐用法）
    - quota：直接指定 {地区: 条数}，优先级高于 total
    - pages：老配置兼容，表示「每地区页数」，会被换算成 quota
    - incremental：只拉每个地区第 1 页，用于定时增量更新
    - cursors：断点游标 {「分类|media|地区」: {page, done}}，resume=True 时从这里接着翻
    - known：已有 cid 集合；一页里全是老面孔就提前收工（连续 stop_after_known 页）
    """
    tm = TMDB()
    out = []
    known = known if known is not None else set()
    cursors = cursors if cursors is not None else {}
    jobs = plan_jobs(specs, weights, incremental)
    total_jobs = len(jobs)
    fetched = 0

    for i, j in enumerate(jobs):
        cat, m, region = j["category"], j["media"], j["region"]
        key = f"{cat}|{m}|{region}"
        pages = j["pages"]
        st = cursors.get(key) or {}
        if resume and st.get("done"):
            if on_step:
                on_step(i + 1, total_jobs, f"{cat} / {region}（{m}）已完成，跳过")
            continue
        start = max(1, int(st.get("page") or 1)) if resume else 1
        if start > pages:
            cursors[key] = {"page": pages, "done": True, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
            continue
        if on_step:
            on_step(i, total_jobs, f"{cat} / {region}（{m}）")
        if progress:
            tail = f"从第 {start} 页续抓 · " if resume and start > 1 else ""
            progress(f"{cat} / {region} ({m}) {tail}{j['per_media']} 条 · 到第 {pages} 页…")

        got = [0]
        streak = 0
        last = start - 1
        exhausted = False
        for page in range(start, pages + 1):
            try:
                data = tm.get(f"/discover/{m}", page=page, sort_by=j["sort"],
                              with_origin_country=j["country"], **j["extra"])
            except TMDBError as e:
                if progress:
                    progress(f"  ! {cat}/{region}/{m} 第 {page} 页跳过：{e}")
                # Key 无效就没必要每个地区都试一遍，直接整体失败，让上层提示用户去填 Key
                if "API Key" in str(e):
                    raise
                break
            rows = data.get("results") or []
            if not rows:
                exhausted = True
                break
            last = page
            new = 0
            for r in rows:
                nr = _norm(r, m)
                if nr["cid"] not in known:
                    new += 1
                    known.add(nr["cid"])
                fetched += 1
                nr["category"] = cat
                nr["region"] = region
                nr["regions"] = region
                nr["genre_str"] = ",".join(nr.get("genre_names") or [])
                nr["extra"] = {
                    "region": region,
                    "media": nr.get("media"),
                    "genres": nr.get("genres") or [],
                    "genre_names": nr.get("genre_names") or [],
                    "overview": nr.get("overview", ""),
                    "backdrop": nr.get("backdrop", ""),
                }
                out.append(nr)
            got[0] = len(out)
            if progress and (page % 20 == 0 or page >= pages or new == 0):
                progress(f"  {cat}/{region}({m}) 第 {page}/{pages} 页 · 新增 {new} · 累计 {fetched} 条")
            if page >= int(data.get("total_pages") or 1):
                exhausted = True
                break
            streak = streak + 1 if new == 0 else 0
            if stop_after_known and streak >= stop_after_known:
                if progress:
                    progress(f"  · {cat}/{region}({m}) 连续 {streak} 页没有新条目，这一档已补齐")
                exhausted = True
                break

        if resume:
            done = exhausted or last >= pages
            cursors[key] = {"page": min(last + 1, pages), "done": bool(done),
                            "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if on_step:
        on_step(total_jobs, total_jobs, "完成")
    return out


# ---------------- 年份分桶：发行量统计 + 热门榜 ----------------
# 两件事分开做，互不依赖：
#   1) 发行量：只读 discover 的 total_results，一次请求拿到「这一年这一类一共多少部」，
#      不落条目，所以再多年份也只是几分钟的事；
#   2) 热门榜：只把每年每子类的热度前 500 条拉回来，作为「可补的片」清单。
# 这样本地库不用再存几十万条 —— 数字来自统计表，条目只留真正会看的那些。
#
# 一个 job = 分类 × 子类(bucket) × 媒体 × 年份。年份倒序放最外层，
# 最近年份先算完、先能看；每个 job 一组多国 OR 的 with_origin_country，
# 已经按国家去重（US|GB|FR 的 total_results 小于三国相加），正好等于子类口径。

MIN_VOTES = 1
HOT_PER_YEAR = 500        # 每年每子类保留的热门条数
HOT_PAGES = HOT_PER_YEAR // 20   # TMDB 每页 20 条
HOT_YEAR_FLOOR = 2000     # 只有这年之后的年份才留条目，更早的只统计发行量
                          # （1980 年的缺失清单没人看，但「那年发行了多少」依然有价值）

# 国家码 → 地区档名。条目的地区归属以 TMDB 返回的 origin_country 为准，
# 不再按「这次请求的是哪个地区」一刀切（多国合查时那样会全标成同一个地区）。
CC2REGION = {}
for _name, _cc in REGIONS:
    for _code in (_cc or "").split("|"):
        if _code:
            CC2REGION.setdefault(_code, _name)


def bucket_countries(regions):
    """子类的地区档 → with_origin_country 用的「A|B|C」串（去重保序）。"""
    out = []
    for r in regions or []:
        for code in (REGION_MAP.get(r) or "").split("|"):
            if code and code not in out:
                out.append(code)
    return "|".join(out)


def year_jobs(specs, years, min_votes=MIN_VOTES):
    """展开成「年 × 子类 × 媒体」任务队列，年份倒序在最外层。

    spec: {category, bucket, regions, years?, min_votes?, media?}
    """
    jobs = []
    all_years = sorted({int(y) for s in specs for y in (s.get("years") or years)},
                       reverse=True)
    for y in all_years:
        for s in specs:
            ys = [int(x) for x in (s.get("years") or years)]
            if y not in ys:
                continue
            cat = s["category"]
            bucket = s.get("bucket") or ""
            media = s.get("media") or CATEGORY_SPEC.get(cat, ("movie", {}))[0]
            extra = dict(CATEGORY_SPEC.get(cat, ("", {}))[1])
            if s.get("with_genres"):
                extra["with_genres"] = str(s["with_genres"])
            mv = s.get("min_votes", min_votes)
            if mv:
                # 0 票的条目绝大多数是未上映占位/微电影，会把「发行量」撑成噪声；
                # 而且美国一年 0 票有 1.9 万条，超过 TMDB 的 500 页上限。
                extra["vote_count.gte"] = str(int(mv))
            cc = bucket_countries(s.get("regions"))
            if not cc:
                continue
            for m in (["movie", "tv"] if media == "both" else [media]):
                p = dict(extra)
                if m == "movie":
                    p["primary_release_date.gte"] = "%d-01-01" % y
                    p["primary_release_date.lte"] = "%d-12-31" % y
                else:
                    p["first_air_date.gte"] = "%d-01-01" % y
                    p["first_air_date.lte"] = "%d-12-31" % y
                jobs.append({"category": cat, "bucket": bucket, "media": m,
                             "regions": s.get("regions") or [],
                             "country": cc, "year": y, "params": p,
                             "sort": s.get("sort") or "popularity.desc"})
    return jobs


def fetch_counts(specs, years, min_votes=MIN_VOTES, progress=None, on_count=None,
                 on_step=None, cursors=None, resume=False):
    """只读发行量：每个（子类 × 媒体 × 年）发一次请求，取 total_results。

    on_count(category, bucket, year, total) 由调用方写库并 commit。
    1,300 档大约 10 分钟，跑完所有年份的「全量发行量」就直接可看。
    """
    tm = TMDB()
    cur = cursors if cursors is not None else {}
    jobs = year_jobs(specs, years, min_votes)
    total = len(jobs)
    got = 0
    for i, j in enumerate(jobs):
        key = "%s|%s|%s|%s" % (j["category"], j["bucket"], j["media"], j["year"])
        if resume and (cur.get(key) or {}).get("done"):
            got += 1
            continue
        label = "%d · %s · %s" % (j["year"], j["bucket"] or j["category"], j["media"])
        if on_step:
            on_step(i, total, label)
        try:
            d = tm.get("/discover/" + j["media"], page=1, sort_by="popularity.desc",
                       with_origin_country=j["country"], **j["params"])
        except TMDBError as e:
            if "API Key" in str(e):
                raise
            if progress:
                progress("  ! %s 取发行量失败：%s" % (label, e))
            continue
        n = int(d.get("total_results") or 0)
        if on_count:
            on_count(j["category"], j["bucket"], j["year"], n)
        cur[key] = {"done": True, "n": n, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        got += 1
        if progress and (i % 40 == 0 or i == total - 1):
            progress("  %s：%d 部（%d/%d 档）" % (label, n, i + 1, total))
    if on_step:
        on_step(total, total, "完成")
    return {"jobs": got, "total": total}


def build_year_catalog(specs, years, min_votes=MIN_VOTES, progress=None, on_step=None,
                       on_batch=None, cursors=None, resume=False, max_pages=HOT_PAGES,
                       page_limit=None, year_floor=HOT_YEAR_FLOOR):
    """按年份倒序抓「热门榜」：每年每子类只取热度前 HOT_PER_YEAR 条。

    为什么只取前 500：发行量已经由 fetch_counts 用 total_results 拿到了，
    条目只用来做「缺哪几部」的清单。500 名以后基本没人看，全量拉回几十万条
    只是把本地库撑大，没有实际用途。

    - on_batch(rows)：每抓几页就回调一次，调用方负责写库 + commit。
    - cursors：{「分类|子类|媒体|年份」: {page, pages, done}}，resume=True 时续抓。
    - max_pages：硬上限，默认 25 页 = 500 条。
    - page_limit：只抓前 N 页（增量更新用，不标 done、不读旧游标）。
    """
    tm = TMDB()
    cur = cursors if cursors is not None else {}
    jobs = year_jobs(specs, years, min_votes)
    total = len(jobs)
    buf = []
    done_jobs = 0
    fetched = 0

    def flush(key=None, page=0, pages=0):
        nonlocal buf
        if buf and on_batch:
            on_batch(buf)
        buf = []
        if key and pages:
            cur[key] = {"page": page, "pages": pages, "done": page >= pages,
                        "at": time.strftime("%Y-%m-%dT%H:%M:%S")}

    if year_floor:
        jobs = [j for j in jobs if j["year"] >= year_floor]
        total = len(jobs)
    for i, j in enumerate(jobs):
        key = "%s|%s|%s|%s" % (j["category"], j["bucket"], j["media"], j["year"])
        label = "%d · %s · %s" % (j["year"], j["bucket"] or j["category"], j["media"])
        st = cur.get(key) or {}
        if resume and st.get("done"):
            done_jobs += 1
            if on_step:
                on_step(i + 1, total, label + "（已完成，跳过）")
            continue
        if on_step:
            on_step(i, total, label)
        if progress:
            progress("  %s …" % label)
        start = max(1, int(st.get("page") or 1)) if resume else 1
        pages = int(st.get("pages") or 0)
        page = start
        while True:
            if pages and page > pages:
                break
            try:
                d = tm.get("/discover/" + j["media"], page=page, sort_by=j["sort"],
                           with_origin_country=j["country"], **j["params"])
            except TMDBError as e:
                if progress:
                    progress("  ! %s 第 %d 页失败：%s" % (label, page, e))
                if "API Key" in str(e):
                    raise
                break
            rows = d.get("results") or []
            if not rows:
                if not pages:
                    pages = page - 1
                break
            if not pages:
                real = int(d.get("total_pages") or 1)
                pages = min(real, max_pages)
                if page_limit:
                    pages = min(pages, int(page_limit))
                if real > max_pages and progress:
                    progress("  · %s 共 %d 页，TMDB 只给前 %d 页，已按上限截断"
                             % (label, real, max_pages))
            for r in rows:
                nr = _norm(r, j["media"])
                # 地区归属按条目自己的 origin_country 算：多国合查时
                # 「这次请求的是美国」并不代表这条就是美国片
                names = []
                for code in (r.get("origin_country") or []):
                    nm = CC2REGION.get(code)
                    if nm and nm not in names:
                        names.append(nm)
                if not names:
                    names = list(j.get("regions") or [])[:1] or ["其他"]
                nr["category"] = j["category"]
                nr["region"] = names[0]
                nr["regions"] = ",".join(names)
                nr["bucket"] = j["bucket"]
                nr["genre_str"] = ",".join(nr.get("genre_names") or [])
                nr["extra"] = {
                    "region": names[0],
                    "regions": names,
                    "bucket": j["bucket"],
                    "media": nr.get("media"),
                    "genres": nr.get("genres") or [],
                    "genre_names": nr.get("genre_names") or [],
                    "overview": nr.get("overview", ""),
                    "backdrop": nr.get("backdrop", ""),
                }
                buf.append(nr)
            fetched += len(rows)
            if page % 5 == 0 or page >= pages:
                flush(key, page, pages)
                if progress:
                    progress("  %s 第 %d/%d 页 · 累计 %d 条" % (label, page, pages, fetched))
            page += 1
        flush(None)
        cur[key] = {"page": pages or 0, "pages": pages or 0, "done": True,
                    "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        done_jobs += 1
        if on_step:
            on_step(i + 1, total, "%s 完成" % label)
    if progress:
        progress("完成：%d 档，共 %d 条" % (done_jobs, fetched))
    return {"jobs": done_jobs, "rows": fetched}

