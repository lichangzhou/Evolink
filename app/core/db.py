"""SQLite 存储：owned（Emby 现有库存）+ catalog（目标发行片）。"""
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS owned (
  emby_id        TEXT PRIMARY KEY,
  name           TEXT,
  original_title TEXT,
  year           INTEGER,
  premiere       TEXT,
  type           TEXT,
  lib_id         TEXT,
  library        TEXT,
  tmdb_id        TEXT,
  imdb_id        TEXT,
  tvdb_id        TEXT,
  has_image      INTEGER DEFAULT 0,
  image_tag      TEXT,
  path           TEXT,
  norm           TEXT,
  av_code        TEXT,
  medias         TEXT DEFAULT '',
  updated_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_owned_av   ON owned(av_code);
CREATE INDEX IF NOT EXISTS idx_owned_tmdb ON owned(tmdb_id);
CREATE INDEX IF NOT EXISTS idx_owned_imdb ON owned(imdb_id);
CREATE INDEX IF NOT EXISTS idx_owned_tvdb ON owned(tvdb_id);
CREATE INDEX IF NOT EXISTS idx_owned_norm ON owned(norm);
CREATE INDEX IF NOT EXISTS idx_owned_lib  ON owned(lib_id);

CREATE TABLE IF NOT EXISTS catalog (
  cid           TEXT PRIMARY KEY,
  source        TEXT,
  category      TEXT,
  title         TEXT,
  original_title TEXT,
  year          INTEGER,
  release_date  TEXT,
  poster        TEXT,
  tmdb_id       TEXT,
  imdb_id       TEXT,
  tvdb_id       TEXT,
  rating        REAL,
  popularity    REAL,
  extra         TEXT,
  norm          TEXT,
  av_code       TEXT,
  regions       TEXT DEFAULT '',
  genres        TEXT DEFAULT '',
  media         TEXT DEFAULT '',
  lists         TEXT DEFAULT '',
  match_status  TEXT DEFAULT 'missing',
  match_emby_id TEXT,
  match_score   REAL,
  matched_at    TEXT,
  -- 排序键（生成列）：对 popularity 取对数压平，避免头部几部把后面全挤掉。
  -- 早期版本曾把它当普通列，SCHEMA 漏写后全新库首访 /api/catalog 会崩
  -- "no such column: sort_key"。这里用 GENERATED 生成列补齐，老库 ALTER 也兜底。
  sort_key      REAL GENERATED ALWAYS AS (
                  CASE WHEN COALESCE(popularity,0) > 0 THEN log(popularity+1) ELSE 0 END
                ) VIRTUAL
);
CREATE INDEX IF NOT EXISTS idx_cat_status ON catalog(match_status);
CREATE INDEX IF NOT EXISTS idx_cat_tmdb   ON catalog(tmdb_id);
CREATE INDEX IF NOT EXISTS idx_cat_cat    ON catalog(category);
CREATE INDEX IF NOT EXISTS idx_cat_norm   ON catalog(norm);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);

-- 每年发行量（只存数字，不存条目）：TMDB discover 返回的 total_results。
-- 一次请求就能拿到「这一年这一类一共发行了多少部」，不用把条目全拉回来。
CREATE TABLE IF NOT EXISTS cat_count (
  category TEXT NOT NULL,
  bucket   TEXT NOT NULL,
  year     INTEGER NOT NULL,
  total    INTEGER DEFAULT 0,
  at       TEXT,
  PRIMARY KEY (category, bucket, year)
);
CREATE INDEX IF NOT EXISTS idx_cnt_bucket ON cat_count(bucket, year);

-- 115 网盘库存（扫码登录后扫描目录得到）
CREATE TABLE IF NOT EXISTS pan115 (
  fid        TEXT PRIMARY KEY,
  cid        TEXT,
  parent     TEXT,
  name       TEXT,
  path       TEXT,
  size       INTEGER DEFAULT 0,
  is_dir     INTEGER DEFAULT 0,
  ext        TEXT,
  norm       TEXT,
  av_code    TEXT,
  acct       TEXT DEFAULT '',      -- 115 账号 UID：多账号时互不串内容
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_115_norm ON pan115(norm);
CREATE INDEX IF NOT EXISTS idx_115_av   ON pan115(av_code);
CREATE INDEX IF NOT EXISTS idx_115_cid  ON pan115(cid);
"""

# 老库补列（CREATE TABLE IF NOT EXISTS 不会改已存在的表）
MIGRATIONS = [
    ("catalog", "regions", "TEXT DEFAULT ''"),
    ("catalog", "genres", "TEXT DEFAULT ''"),
    ("catalog", "media", "TEXT DEFAULT ''"),
    ("catalog", "lists", "TEXT DEFAULT ''"),
    ("owned", "medias", "TEXT DEFAULT ''"),
    # 匹配依据：番号 / 标题 / TMDB / 里番标题 / 同厂牌同标题（v1.7）
    ("catalog", "match_how", "TEXT DEFAULT ''"),
    # 115 扫描结果按账号分区：换账号后不显示上一个账号的网盘内容
    ("pan115", "acct", "TEXT DEFAULT ''"),
    # 排序键生成列兜底（全新库由 SCHEMA 建好；老库若缺则用 ALTER 补回）
    ("catalog", "sort_key",
     "REAL GENERATED ALWAYS AS (CASE WHEN COALESCE(popularity,0)>0 THEN log(popularity+1) ELSE 0 END) VIRTUAL"),
]


def conn():
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    # 并发写：WAL 同一时刻只允许一个写者，默认 busy_timeout=0 会立刻抛
    # "database is locked"。给 30s 等待窗口，绝大多数短事务能等到写锁。
    c.execute("PRAGMA busy_timeout=30000")
    c.execute("PRAGMA synchronous=NORMAL")
    return c


# 全局写锁：串行化「重活」写事务（后台扫描/同步/比对），避免两个长事务
# 同时抢写锁导致其中一个 database is locked 静默失败。短事务（设置保存等）
# 不抢这把锁，靠上面的 busy_timeout 兜底。
_WRITE_LOCK = threading.Lock()


@contextmanager
def write_lock():
    with _WRITE_LOCK:
        yield


def init():
    c = conn()
    c.executescript(SCHEMA)
    _migrate(c)
    c.commit()
    return c


def _migrate(c):
    """给老库补新列（ALTER TABLE 幂等靠 PRAGMA 判断）。"""
    for table, col, ddl in MIGRATIONS:
        try:
            cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        except Exception:
            continue
        if cols and col not in cols:
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
            except sqlite3.OperationalError as e:
                # 老库「幻影列」：PRAGMA table_info 查不到该列，但物理列早已存在
                # （sort_key 曾经是生成列、状态损坏），ALTER 会报 duplicate column name。
                # 列已存在就达到目的，忽略即可，不要因此让整个服务起不来。
                if "duplicate column name" not in str(e).lower():
                    raise
    try:
        # pan115 的账号索引必须在补列（acct）之后建，否则老库会报 no such column
        c.execute("CREATE INDEX IF NOT EXISTS idx_115_acct ON pan115(acct)")
    except Exception:
        pass
    try:
        c.execute("CREATE INDEX IF NOT EXISTS idx_cat_regions ON catalog(regions)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_cat_genres  ON catalog(genres)")
        # 按番号合并多源（JavDB 新片 + JavBus 历史）时要按 av_code 查
        c.execute("CREATE INDEX IF NOT EXISTS idx_cat_av     ON catalog(av_code)")
        # 本地条目回查匹配到的发行片（补年份用）
        c.execute("CREATE INDEX IF NOT EXISTS idx_cat_emby   ON catalog(match_emby_id)")
        # 覆盖索引：compare() 的 _EFF_JOIN 要按 match_emby_id 分组取 MAX(year)、
        # 按 av_code 分组取 release_date。catalog 涨到 27 万行后，单列索引每行都要
        # 回表取第二个列 → 整个查询从几十毫秒劣化到 60~90 秒（stats/仪表盘全卡住）。
        # 带上第二列后分组+聚合全在索引内完成，不回表。
        c.execute("CREATE INDEX IF NOT EXISTS idx_cat_emby_y ON catalog(match_emby_id, year)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_cat_av_rel ON catalog(av_code, release_date)")
        # 排序键用「表达式索引」，不在 sort_key 上建普通索引：sort_key 是 VIRTUAL 生成列
        # （table_info 查不到、SELECT 却能取），VIRTUAL 列不能建普通索引——2026-10-23 曾在
        # 它上面建 idx_cat_sortkey，与表结构不符，一写 catalog 就 malformed，被
        # fix_broken_indexes DROP 掉，此后 ORDER BY c.sort_key 变成 27 万行全表扫描+临时
        # B-Tree 排序（catalog 劣化到 8~10 秒、健康探针 4s 超时→容器一直 unhealthy）。
        # 对 popularity 表达式建索引，server.py 的 ORDER BY 用同一表达式即可走索引。
        c.execute("CREATE INDEX IF NOT EXISTS idx_cat_sortkey ON catalog("
                  "(CASE WHEN COALESCE(popularity,0)>0 THEN log(popularity+1) ELSE 0 END) DESC, "
                  "year DESC, rating DESC)")
    except Exception:
        pass


def set_meta(k, v):
    c = conn()
    c.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
    c.commit()


def get_meta(k, default=None):
    c = conn()
    r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def stats(category=None):
    """全局统计。category 非空时 by_status / catalog 只算该分类的发行片口径。

    本地侧（owned）只算用户勾选的库 —— 未勾选的库对系统来说不存在。
    """
    c = conn()
    aw, aa = active_sql("library")
    cat = (category or "").strip()
    o = c.execute("SELECT COUNT(*) n FROM owned WHERE " + aw, aa).fetchone()["n"]
    t_all = c.execute("SELECT COUNT(*) n FROM catalog").fetchone()["n"]
    cw, ca = cat_scope(category)
    where = " WHERE " + cw
    args = list(ca)
    t = c.execute("SELECT COUNT(*) n FROM catalog" + where, args).fetchone()["n"]
    rows = c.execute("SELECT match_status s, COUNT(*) n FROM catalog" + where +
                     " GROUP BY match_status", args).fetchall()
    by_status = {r["s"]: r["n"] for r in rows}
    by_cat = {r["category"] or "": r["n"] for r in
              c.execute("SELECT category, COUNT(*) n FROM catalog GROUP BY category")}
    libs = c.execute("SELECT library, COUNT(*) n FROM owned WHERE " + aw +
                     " GROUP BY library ORDER BY n DESC", aa).fetchall()
    return {
        "owned": o,
        "catalog": t,
        "catalog_total": t_all,
        "category": cat or "all",
        "by_status": by_status,
        "by_category": by_cat,
        "libraries": [{"name": r["library"], "count": r["n"]} for r in libs],
        "last_sync": get_meta("owned_synced_at"),
        "last_fetch": get_meta("catalog_fetched_at"),
        "last_av": get_meta("av_fetched_at"),
        "last_match": get_meta("matched_at"),
        "last_auto": get_meta("auto_run_at"),
    }


# ---------------- Emby 库 → 分类桶 ----------------
# owned 表没有 category 字段，一个分类对应哪些 Emby 库只能靠这份配置。
# 用户在「设置 → EMBY 设置 → EMBY 媒体库」里按桶勾库，存在 config["emby_sel"]，
# 只记改过的桶；没改过的照 DEFAULT_SEL 走。一个库可以被勾进多个桶，但统计按主力桶走。
BUCKETS = [
    ("movie_us",    "电影",   "欧美电影"),
    ("movie_cn",    "电影",   "国语电影"),
    ("movie_jpkr",  "电影",   "日韩印电影"),
    ("movie_other", "电影",   "其他语电影"),
    ("tv_cn",       "剧集",   "国语剧"),
    ("tv_us",       "剧集",   "欧美剧"),
    ("tv_jpkr",     "剧集",   "日韩剧"),
    ("tv_other",    "剧集",   "其他剧"),
    ("anime_movie", "动画",   "动画电影"),
    ("anime_tv",    "动画",   "动画剧集"),
    ("doc_movie",   "纪录片", "纪录片"),
    ("variety_cn",    "综艺",   "国语综艺"),
    ("variety_other", "综艺",   "外语综艺"),
    ("concert_cn",    "演唱会", "国语演唱会"),
    ("concert_other", "演唱会", "外语演唱会"),
    ("av",          "AV",     "日本AV"),
    ("hentai",      "里番",   "里番"),
]
# catalog（发行片）视角下「全部」的边界：AV / 里番 是侧栏独立栏目
# （自己的页面、自己的筛选），混进媒体库首页会把 17 万番号淹掉常规影视。
# 用户报「首页选全部显示的是里番内容」= 全站口径没排除这两类。
# 规则：「全部」= 六大常规分类之和，AV/里番 只能显式选（chip/侧栏）才出现。
ADULT_CATS = ("AV", "里番")


def cat_scope(cat):
    """分类筛选 SQL：(片段, 参数)。cat 有值且非 all → 该分类；空/all → 全部常规分类。"""
    cat = (cat or "").strip()
    if cat and cat != "all":
        return "category=?", [cat]
    return "category NOT IN (%s)" % ",".join("?" * len(ADULT_CATS)), list(ADULT_CATS)


BUCKET_LABEL = {k: l for k, _c, l in BUCKETS}
BUCKET_CAT = {k: c for k, c, _l in BUCKETS}
CAT_BUCKETS = {}
for _k, _c, _l in BUCKETS:
    CAT_BUCKETS.setdefault(_c, []).append({"key": _k, "label": _l})

# 地区 → 本地库。电影/剧集/综艺/演唱会的 Emby 库本来就按地区拆开，
# 所以「地区」这个筛选在本地侧能精确落库，不用靠猜。
_REGION_BUCKETS = {
    "电影":   {"cn": "movie_cn", "asia": "movie_jpkr", "us": "movie_us", "other": "movie_other"},
    "剧集":   {"cn": "tv_cn", "asia": "tv_jpkr", "us": "tv_us", "other": "tv_other"},
    "综艺":   {"cn": "variety_cn", "us": "variety_other"},
    "演唱会": {"cn": "concert_cn", "us": "concert_other"},
}


def libs_for_region(category, region):
    """按地区筛出该分类对应的本地库；分类没有地区拆分时返回 None。"""
    m = _REGION_BUCKETS.get((category or "").strip())
    if not m or not (region or "").strip():
        return None
    r = region.strip()
    if r in CN_REGIONS:
        k = m.get("cn")
    elif r in ASIA_REGIONS:
        k = m.get("asia") or m.get("us")
    elif r == "其他":
        k = m.get("other") or m.get("us")
    else:
        k = m.get("us")
    return list(sel_map().get(k) or []) if k else None

# 桶 → 发行片（catalog）的过滤口径。
# 电影/剧集按地区分三类且互斥：国语（大陆+港澳台）→ 亚洲（日韩印等）→ 其余算欧美；
# regions 为空的归「其他语」。动画/纪录片用 media 精确区分。
CN_REGIONS = ["大陆", "香港", "台湾", "国语", "粤语", "华语", "中国", "港台"]
ASIA_REGIONS = ["日本", "韩国", "印度", "东南亚", "亚洲", "泰国", "新加坡",
                "马来西亚", "印尼", "菲律宾", "越南", "蒙古"]

BUCKET_FILTER = {
    "movie_cn":    {"category": "电影", "regions_any": CN_REGIONS},
    "movie_jpkr":  {"category": "电影", "regions_any": ASIA_REGIONS, "regions_none": CN_REGIONS},
    "movie_us":    {"category": "电影", "regions_none": CN_REGIONS + ASIA_REGIONS + ["其他"],
                    "regions_not_empty": True},
    "movie_other": {"category": "电影", "regions_any": ["其他"], "or_empty": True},
    "tv_cn":       {"category": "剧集", "regions_any": CN_REGIONS},
    "tv_jpkr":     {"category": "剧集", "regions_any": ASIA_REGIONS, "regions_none": CN_REGIONS},
    "tv_us":       {"category": "剧集", "regions_none": CN_REGIONS + ASIA_REGIONS + ["其他"],
                    "regions_not_empty": True},
    "tv_other":    {"category": "剧集", "regions_any": ["其他"], "or_empty": True},
    "anime_movie": {"category": "动画", "media": "movie"},
    "anime_tv":    {"category": "动画", "media": "tv"},
    "doc_movie":   {"category": "纪录片"},
    "variety_cn":    {"category": "综艺", "regions_any": CN_REGIONS},
    "variety_other": {"category": "综艺", "regions_none": CN_REGIONS},
    "concert_cn":    {"category": "演唱会", "regions_any": CN_REGIONS},
    "concert_other": {"category": "演唱会", "regions_none": CN_REGIONS},
    "av":          {"category": "AV"},
    "hentai":      {"category": "里番"},
}

_REG = "(','||COALESCE(c.regions,'')||',')"


def _region_hit(names):
    return "(" + " OR ".join(f"{_REG} LIKE ?" for _ in names) + ")"


def bucket_where(bucket):
    """桶 → (where 片段列表, 参数列表)，用于按子类过滤发行片。"""
    f = BUCKET_FILTER.get((bucket or "").strip())
    if not f:
        return [], []
    w, a = [], []
    if f.get("category"):
        w.append("c.category=?")
        a.append(f["category"])
    if f.get("media"):
        w.append("c.media=?")
        a.append(f["media"])
    any_r = f.get("regions_any") or []
    none_r = f.get("regions_none") or []
    if any_r:
        hit = _region_hit(any_r)
        if f.get("or_empty"):
            w.append(f"({hit} OR COALESCE(c.regions,'')='')")
        else:
            w.append(hit)
        a += [f"%,{r},%" for r in any_r]
    if none_r:
        w.append("NOT " + _region_hit(none_r))
        a += [f"%,{r},%" for r in none_r]
    if f.get("regions_not_empty"):
        w.append("COALESCE(c.regions,'')<>''")
    return w, a


# 缺省选择：按本机 Emby 的实际库名预填，用户可在设置里随便改。
DEFAULT_SEL = {
    "movie_us":    ["欧美电影"],
    "movie_cn":    ["国产电影"],
    "movie_jpkr":  ["日韩印电影"],
    "movie_other": [],
    "tv_cn":       ["国产剧"],
    "tv_us":       ["欧美剧"],
    "tv_jpkr":     ["日韩剧"],
    "tv_other":    [],
    "anime_movie": ["国语动漫", "原声动漫"],     # 这两个库在 Emby 里是 movies 类型
    "anime_tv":    ["动漫剧集", "国漫剧集"],
    "doc_movie":   ["纪录片"],
    "variety_cn":    ["综艺"],
    "variety_other": ["综艺"],
    "concert_cn":    ["演唱会"],
    "concert_other": ["演唱会"],
    "av":          ["Adult-亚洲", "Adult-亚洲无码"],
    "hentai":      ["Adult-动画"],
}


def sel_map():
    """桶 → 库名列表。config 里只存用户改过的桶，没改的用 DEFAULT_SEL。"""
    m = {k: list(v) for k, v in DEFAULT_SEL.items()}
    override = config.load().get("emby_sel")
    if isinstance(override, dict):
        # 老配置里的 variety / concert 已拆成 cn / other 两个桶，旧值同时铺到两边
        for old, news in (("variety", ("variety_cn", "variety_other")),
                          ("concert", ("concert_cn", "concert_other"))):
            if old in override and not any(n in override for n in news):
                override = dict(override)
                override[news[0]] = override[news[1]] = override[old]
        for k, v in override.items():
            if k in m and isinstance(v, list):
                m[k] = [str(x).strip() for x in v if str(x).strip()]
    return m


def lib_map():
    """库名 → 桶（由 sel_map 反解）。库被勾进多个桶时按 BUCKETS 顺序取第一个。"""
    out = {}
    m = sel_map()
    for k, _c, _l in BUCKETS:
        for lib in m.get(k) or []:
            out.setdefault(lib, k)
    return out


def libs_for(*keys):
    """某几个桶名下勾选的 Emby 库（空库名过滤掉）。"""
    m = sel_map()
    out = []
    for k in keys:
        for lib in (m.get(k) or []):
            if lib and lib not in out:
                out.append(lib)
    return tuple(sorted(out))


def libs_for_cat(category):
    return libs_for(*[k for k, c, _l in BUCKETS if c == (category or "").strip()])


def active_libs():
    """用户勾选的库的并集 = 系统承认的全部媒体库。

    未勾选的库在这里根本不存在：不参与比对、不进任何统计、页面上也不出现。
    唯一能看到它们的地方是「设置 → EMBY 媒体库」的勾选清单本身（否则没法勾）。
    """
    out = []
    for k, _c, _l in BUCKETS:
        for lib in (sel_map().get(k) or []):
            if lib and lib not in out:
                out.append(lib)
    return tuple(sorted(out))


def active_sql(alias="library"):
    """只承认用户勾选的库。返回 (SQL 片段, 参数)。没勾任何库时返回 '0'。"""
    libs = list(active_libs())
    if not libs:
        return "0", []
    return "%s IN (%s)" % (alias, ",".join("?" * len(libs))), libs


def excluded_libs():
    """本地有、但用户没勾进任何桶的库：对系统来说不存在。"""
    used = set(lib_map())
    return tuple(sorted(r["library"] for r in
                        conn().execute("SELECT DISTINCT library FROM owned")
                        if r["library"] and r["library"] not in used))


def _lib_sql(libs):
    libs = [x for x in libs if x]
    if not libs:
        return "0", []
    return "library IN (%s)" % ",".join("?" * len(libs)), list(libs)


def sel_rows(extra_libs=()):
    """「EMBY 媒体库」页用：16 个桶逐个列出，带已选库、条目数、以及全库清单。"""
    c = conn()
    cnt = {r["library"]: r["n"] for r in
           c.execute("SELECT library, COUNT(*) n FROM owned GROUP BY library")}
    m = sel_map()
    buckets = []
    for k, cat, label in BUCKETS:
        libs = m.get(k) or []
        buckets.append({
            "key": k, "category": cat, "label": label,
            "libs": libs,
            "items": sum(cnt.get(l, 0) for l in libs),
            "detail": [{"name": l, "items": cnt.get(l, 0)} for l in libs],
            "missing": [l for l in libs if cnt.get(l, 0) == 0],
        })
    known = dict(cnt)
    for l in extra_libs:
        known.setdefault(l, 0)
    return {"buckets": buckets,
            "libraries": [{"name": l, "items": n} for l, n in
                          sorted(known.items(), key=lambda kv: -kv[1])],
            "unmapped": sorted(l for l in known if l not in set(lib_map()) and l)}



# ---------------- 年代对比（发行片 / 本地库 / 缺口） ----------------
# 只回答一个问题：某个分类（或子类）在某一年，发行片上有多少、本地库有多少、还缺多少。
# 前端把这张表直接铺开，不再让人自己拿两套口径的数字相减。
_DEC_CATALOG = """CASE
    WHEN {y} IS NULL OR {y}=0 THEN 'none'
    WHEN {y}>=2026 THEN 'y2026'
    WHEN {y}>=2010 THEN 'y'||{y}
    WHEN {y}>=2000 THEN '2000'
    WHEN {y}>=1990 THEN '1990'
    WHEN {y}>=1980 THEN '1980'
    ELSE 'old' END"""
# owned 里存过 2087 这类脏年份，统一归到「无年份」，否则会和列表筛选对不上
_DEC_OWNED = """CASE
    WHEN {y} IS NULL OR {y}<1900 OR {y}>2027 THEN 'none'
    WHEN {y}>=2026 THEN 'y2026'
    WHEN {y}>=2010 THEN 'y'||{y}
    WHEN {y}>=2000 THEN '2000'
    WHEN {y}>=1990 THEN '1990'
    WHEN {y}>=1980 THEN '1980'
    ELSE 'old' END"""


def _all_mapped_libs():
    return list(libs_for(*[k for k, _c, _l in BUCKETS]))


def _all_regular_libs():
    """「全部」口径的本地库：六大常规分类勾选的库（AV/里番 库不进来）。"""
    return list(libs_for(*[k for k, c, _l in BUCKETS if c not in ADULT_CATS]))


def count_put(c, category, bucket, year, total):
    """写一条年度发行量（只存数字）。重复抓只更新，不重复计数。"""
    c.execute("INSERT INTO cat_count(category,bucket,year,total,at) VALUES(?,?,?,?,?) "
              "ON CONFLICT(category,bucket,year) DO UPDATE SET "
              "total=excluded.total, at=excluded.at",
              (category, bucket, int(year), int(total), now()))


def counts_by_year(bucket="", category=""):
    """{年: 该年发行总量}。给「全量发行量」这一组卡片用。

    bucket 优先；只给分类时把该分类下所有子类相加 —— 各子类的地区/媒体
    互不重叠（电影三桶按地区切、动画按 movie/tv 切），相加不会重复计数。
    """
    c = conn()
    if bucket:
        rows = c.execute("SELECT year, total FROM cat_count WHERE bucket=?", (bucket,))
    elif category and category != "all":
        rows = c.execute("SELECT year, SUM(total) t FROM cat_count "
                         "WHERE category=? GROUP BY year", (category,))
    else:
        rows = c.execute("SELECT year, SUM(total) t FROM cat_count GROUP BY year")
    return {int(r[0]): int(r[1] or 0) for r in rows}


def count_span(bucket="", category=""):
    """发行量已覆盖的年份区间，前端用来提示「哪些年份还没统计」。"""
    c = conn()
    if bucket:
        r = c.execute("SELECT MIN(year) a, MAX(year) b, COUNT(*) n FROM cat_count WHERE bucket=?",
                      (bucket,)).fetchone()
    elif category and category != "all":
        r = c.execute("SELECT MIN(year) a, MAX(year) b, COUNT(*) n FROM cat_count WHERE category=?",
                      (category,)).fetchone()
    else:
        r = c.execute("SELECT MIN(year) a, MAX(year) b, COUNT(*) n FROM cat_count").fetchone()
    return {"from": r["a"], "to": r["b"], "years": r["n"] or 0}


def catalog_dec_where(dec):
    """发行片侧年代条件，与列表筛选同一套口径：y2026 兜住 2026 之后。"""
    dec = (dec or "").strip()
    if dec == "old":
        return "(c.year IS NOT NULL AND c.year > 0 AND c.year < 1980)", []
    if dec == "none":
        return "(c.year IS NULL OR c.year = 0)", []
    if dec.startswith("y") and dec[1:].isdigit():
        y = int(dec[1:])
        return ("(c.year >= ?)", [2026]) if y >= 2026 else ("c.year = ?", [y])
    if dec.isdigit():
        y = int(dec)
        return ("(c.year >= ? AND c.year <= ?)", [y, y + 9])
    return "", []


# 本地条目的「有效年份」。Emby 里 3 千条 AV、2.9 千条里番的 year 是空的，
# 只按 year 分年会让它们全落到「无年份」，卡片上的本地库数字因此失真。
# 三级兜底：自身年份 → 番号对应的发行片发行日 → 匹配到的那条发行片的年份。
# 依赖 catalog(av_code) 与 catalog(match_emby_id) 两个索引，25k 行约 40ms。
_EFF_JOIN = """
  LEFT JOIN (SELECT av_code AS _ak, MAX(CAST(substr(release_date,1,4) AS INTEGER)) AS _ry
               FROM catalog WHERE av_code<>'' AND release_date<>''
              GROUP BY av_code) _m1 ON _m1._ak = o.av_code
  LEFT JOIN (SELECT match_emby_id AS _mk, MAX(year) AS _my
               FROM catalog WHERE match_emby_id<>'' AND year IS NOT NULL
              GROUP BY match_emby_id) _m2 ON _m2._mk = o.emby_id"""

_EFF_YEAR = """COALESCE(
    CASE WHEN o.year BETWEEN 1900 AND 2027 THEN o.year END,
    _m1._ry, _m2._my)"""


def eff_year_sql():
    return _EFF_YEAR


def dec_case_sql(ye):
    """有效年份 → 年代档的 SQL 版（与 _dec_bucket() 完全同口径）。"""
    return ("CASE WHEN {y} IS NULL THEN 'none' WHEN {y} >= 2026 THEN 'y2026' "
            "WHEN {y} >= 2010 THEN 'y'||{y} WHEN {y} >= 2000 THEN '2000' "
            "WHEN {y} >= 1990 THEN '1990' WHEN {y} >= 1980 THEN '1980' "
            "ELSE 'old' END").format(y=ye)


def owned_dec_where(dec, ye="year"):
    """本地库侧年代条件。ye 传 _EFF_YEAR 时按兜底年份算。"""
    dec = (dec or "").strip()
    if dec == "old":
        return ("(%s IS NOT NULL AND %s < 1980)" % (ye, ye)), []
    if dec == "none":
        return ("(%s IS NULL)" % ye), []
    if dec.startswith("y") and dec[1:].isdigit():
        y = int(dec[1:])
        return ("(%s >= ?)" % ye, [2026]) if y >= 2026 else ("%s = ?" % ye, [y])
    if dec.isdigit():
        y = int(dec)
        return ("(%s >= ? AND %s <= ?)" % (ye, ye), [y, y + 9])
    return "", []


def compare(category="", bucket=""):
    """年代 → {发行片, 缺口, 本地库}。

    口径：
    - 发行片 = catalog 里该分类/子类该年的条数（拉来的目标清单）
    - 缺口 = 发行片里 match_status='missing' 的条数（发行片上有、本地没有）
    - 本地库 = owned 里该分类/子类该年的实际条目（可能包含发行片外的片子，所以不保证等于发行片-缺口）
    """
    cat = (category or "").strip()
    c = conn()
    if bucket:
        bw, ba = bucket_where(bucket)
        libs = list(sel_map().get(bucket) or [])
    else:
        cw, ba = cat_scope(cat)
        bw = [cw] if cw else []
        libs = list(libs_for_cat(cat)) if cat and cat != "all" else _all_regular_libs()
    cond = (" WHERE " + " AND ".join(bw)) if bw else ""
    dec = {}
    for r in c.execute(f"SELECT {_DEC_CATALOG.format(y='c.year')} d, COUNT(*) n, "
                       f"SUM(CASE WHEN c.match_status='missing' THEN 1 ELSE 0 END) m "
                       f"FROM catalog c{cond} GROUP BY d", ba):
        x = dec.setdefault(str(r["d"]), {"catalog": 0, "missing": 0, "local": 0})
        x["catalog"] = r["n"]
        x["missing"] = r["m"] or 0
    lw, la = _lib_sql(libs)
    for r in c.execute(f"SELECT {dec_case_sql(_EFF_YEAR)} d, COUNT(*) n "
                       f"FROM owned o{_EFF_JOIN} WHERE {lw} GROUP BY d", la):
        x = dec.setdefault(str(r["d"]), {"catalog": 0, "missing": 0, "local": 0})
        x["local"] = r["n"]
    total = {k: sum(x[k] for x in dec.values()) for k in ("catalog", "missing", "local")}
    return {"decades": dec, "total": total, "libs": libs}


# ---------------- 本地库统计 ----------------
def av_series(code):
    """番号去尾部数字即系列：IPZZ001 → IPZZ、259LUXU1678 → 259LUXU。"""
    s = re.sub(r"\d+$", "", (code or "").upper()).strip("-_ ")
    return s


def _av_scope(where, args=None, top=24):
    c = conn()
    args = list(args or [])
    total = c.execute(f"SELECT COUNT(*) n FROM owned WHERE {where}", args).fetchone()["n"]
    with_code = c.execute(f"SELECT COUNT(*) n FROM owned WHERE {where} AND av_code!=''", args).fetchone()["n"]
    codes = c.execute(f"SELECT COUNT(DISTINCT av_code) n FROM owned WHERE {where} "
                      f"AND av_code!='' AND av_code IS NOT NULL", args).fetchone()["n"]
    dups = c.execute(f"SELECT COUNT(*) n FROM (SELECT av_code FROM owned WHERE {where} "
                     f"AND av_code!='' GROUP BY av_code HAVING COUNT(*)>1)", args).fetchone()["n"]
    extra = c.execute(f"SELECT COALESCE(SUM(n-1),0) e FROM (SELECT COUNT(*) n FROM owned "
                      f"WHERE {where} AND av_code!='' GROUP BY av_code HAVING n>1)", args).fetchone()["e"]
    libs = []
    for r in c.execute(f"SELECT library, COUNT(*) n, "
                       f"SUM(CASE WHEN av_code!='' THEN 1 ELSE 0 END) c FROM owned "
                       f"WHERE {where} GROUP BY library ORDER BY n DESC", args):
        libs.append({"name": r["library"], "items": r["n"], "codes": r["c"]})
    cnt = {}
    for r in c.execute(f"SELECT av_code FROM owned WHERE {where} AND av_code!=''", args):
        s = av_series(r["av_code"])
        if len(s) >= 2:
            cnt[s] = cnt.get(s, 0) + 1
    series = sorted(cnt.items(), key=lambda kv: -kv[1])
    return {
        "items": total,                  # 该库条目数（一个文件一条）
        "with_code": with_code,
        "no_code": total - with_code,
        "codes": codes,                  # 唯一番号数
        "dups": dups,                    # 同番号多版本组数
        "extra_files": extra,            # 冗余文件数
        "series_count": len(series),
        "series_top": [{"name": k, "n": v} for k, v in series[:top]],
        "libraries": libs,
    }


def av_library(top=24):
    """日本AV库（桶 av）与里番库（桶 hentai）分开统计。"""
    out = {}
    for key, name, bucket in (("jp", "日本AV库", "av"), ("anime", "里番库", "hentai")):
        w, a = _lib_sql(libs_for(bucket))
        d = _av_scope(w, a, top)
        d["key"] = key
        d["name"] = name
        d["libs"] = list(libs_for(bucket))
        out[key] = d
    # 未勾选的库不再回传：对系统来说它们不存在，页面也没有它们的入口。
    out["excluded"] = []
    return out


YEAR_MIN, YEAR_MAX = 1900, 2027     # 库里存在 2087 这类脏年份，超范围当无年份


def _dec_bucket(year):
    """跟发行片同一套年代分档：y2026…y2010 逐年，2000/1990/1980 段，old 更早，none 无年份。"""
    if not year:
        return "none"
    y = int(year)
    if not (YEAR_MIN <= y <= YEAR_MAX):
        return "none"
    if y >= 2026:
        return "y2026"
    if y >= 2010:
        return "y%d" % y
    if y >= 2000:
        return "2000"
    if y >= 1990:
        return "1990"
    if y >= 1980:
        return "1980"
    return "old"


def cat_library(category):
    """某个分类在本地的真实库存 + 各子桶条数（统计条与「国语剧/欧美剧」这类子分类用）。"""
    cat = (category or "").strip()
    keys = [k for k, c, _l in BUCKETS if c == cat]
    if not keys:
        return None
    m = lib_map()
    lib_to_key = {l: b for l, b in m.items() if b in keys}
    w, a = _lib_sql(list(lib_to_key))
    c = conn()
    items = titles = 0
    per, dec = {}, {}
    years = set()
    norms = set()
    for r in c.execute(f"SELECT library, norm, year FROM owned WHERE {w}", a):
        lib = r["library"] or ""
        items += 1
        per[lib] = per.get(lib, 0) + 1
        if r["norm"]:
            norms.add(r["norm"])
        d = _dec_bucket(r["year"])
        dec[d] = dec.get(d, 0) + 1
        y = r["year"]
        if y and YEAR_MIN <= int(y) <= YEAR_MAX:
            years.add(int(y))
    # 子桶本地条数按「该桶自己勾的库」累加：电影/剧集的库各自分开，
    # 综艺/演唱会这类只有一个库的，两个子类都会看到整库条数（shared_local 标记出去）。
    sm = sel_map()
    bcount = {k: sum(per.get(l, 0) for l in (sm.get(k) or [])) for k in keys}
    shared_local = {k: any((sm.get(k) or []) and set(sm.get(k) or []) == set(sm.get(j) or [])
                           for j in keys if j != k) for k in keys}
    # 每个桶的发行片数 / 缺口数（子类行点击后显示的就是这份数据）
    bucket_stats = {}
    for k in keys:
        bw, ba = bucket_where(k)
        if not bw:
            bucket_stats[k] = {"catalog": 0, "missing": 0}
            continue
        cond = " AND ".join(bw)
        cat_n = c.execute(f"SELECT COUNT(*) n FROM catalog c WHERE {cond}", ba).fetchone()["n"]
        miss = c.execute(f"SELECT COUNT(*) n FROM catalog c WHERE {cond} AND c.match_status='missing'",
                         ba).fetchone()["n"]
        bucket_stats[k] = {"catalog": cat_n, "missing": miss}
    return {"name": cat, "items": items, "titles": len(norms),
            "libraries": [{"name": k, "items": v} for k, v in
                          sorted(per.items(), key=lambda kv: -kv[1])],
            "buckets": [{"key": k, "label": BUCKET_LABEL[k], "items": bcount.get(k, 0),
                         "shared_local": shared_local.get(k, False),
                         "catalog": bucket_stats[k]["catalog"], "missing": bucket_stats[k]["missing"]}
                        for k in keys],
            "decade": dec,
            "year_min": min(years) if years else None,
            "year_max": max(years) if years else None}


def local_items(category="", bucket="", lib="", series="", decade="", q="", page=1, size=100):
    """浏览某个分类的本地库：搜索 / 子桶 / 单库 / 系列 / 年代。

    facet 全部基于 owned（真实条目），所以「SNIS 有多少部」「2022 年有多少部」
    这类数字跟库里对得上（catalog 那套是发行片口径，只有几百个番号）。
    buckets facet 用来渲染「国语剧 / 欧美剧 / 日韩剧」这排子分类及其条数。
    """
    cat = (category or "").strip()
    keys = [k for k, c, _l in BUCKETS if c == cat]
    m = lib_map()
    lib_to_key = {l: b for l, b in m.items() if b in keys}
    size = max(1, min(int(size or 100), 500))
    page = max(1, int(page or 1))
    empty = {"total": 0, "page": page, "size": size, "category": cat, "bucket": bucket, "items": [],
             "facets": {"decade": {}, "series": {}, "libs": {}, "buckets": {}, "base_total": 0}}
    if not lib_to_key:
        return empty
    # 基础集：分类 + 搜索（不含子桶/单库/系列/年代，这样切维度时 facet 不塌）
    w_base, a_base = _lib_sql(list(lib_to_key))
    if q:
        w_base += " AND (av_code LIKE ? OR name LIKE ? OR path LIKE ?)"
        a_base += [f"%{q.upper()}%", f"%{q}%", f"%{q}%"]
    w_full, a_full = w_base, list(a_base)
    if bucket and bucket in keys:
        # 用该桶自己勾的库（综艺/演唱会两子类共用一个库时，两边都能看到整库）
        sub = [l for l in (sel_map().get(bucket) or []) if l in lib_to_key]
        sw, sa = _lib_sql(sub)
        w_full += " AND " + sw
        a_full += sa
    if lib:
        w_full += " AND library=?"
        a_full.append(lib)
    if series:
        w_full += " AND UPPER(av_code) LIKE ?"
        a_full.append(series.upper() + "%")

    dec, ser, libs = {}, {}, {}
    c = conn()
    eff = _EFF_YEAR
    for r in c.execute(f"SELECT {eff} y, COALESCE(o.av_code,'') code, o.library lib "
                       f"FROM owned o{_EFF_JOIN} WHERE {w_base}", a_base):
        d = _dec_bucket(r["y"])
        dec[d] = dec.get(d, 0) + 1
        s = av_series(r["code"])
        if len(s) >= 2:
            ser[s] = ser.get(s, 0) + 1
        libname = r["lib"] or ""
        libs[libname] = libs.get(libname, 0) + 1
    # 子桶条数按各桶自己勾的库累加（同库被两个子桶共用时两边都给数）
    _sm = sel_map()
    bcount = {k: sum(libs.get(l, 0) for l in (_sm.get(k) or [])) for k in keys}

    if decade:
        dw, da = owned_dec_where(decade, eff)
        if dw:
            w_full += " AND " + dw
            a_full += da

    total = c.execute(f"SELECT COUNT(*) n FROM owned o{_EFF_JOIN} WHERE {w_full}", a_full).fetchone()["n"]
    rows = c.execute(f"""SELECT o.emby_id, o.name, {eff} AS year, o.library,
                                o.av_code, o.path, o.has_image, o.image_tag
                         FROM owned o{_EFF_JOIN} WHERE {w_full}
                         ORDER BY CASE WHEN {eff} BETWEEN 1900 AND 2027
                                       THEN {eff} ELSE 0 END DESC,
                                  o.name LIMIT ? OFFSET ?""",
                     a_full + [size, (page - 1) * size]).fetchall()
    return {
        "total": total, "page": page, "size": size, "category": cat, "bucket": bucket,
        "items": [dict(r) for r in rows],
        "facets": {
            "decade": dec, "base_total": sum(dec.values()),
            "series": ser, "series_total": sum(ser.values()),
            "libs": libs,
            "buckets": bcount,
        },
    }


def av_items(q="", lib="", series="", decade="", page=1, size=100, scope="jp"):
    """兼容老接口：/api/av/lib?scope=jp|anime。"""
    return local_items(category="AV" if (scope or "jp") == "jp" else "里番",
                       lib=lib, series=series, decade=decade, q=q, page=page, size=size)


# ---------------- 排重 ----------------
# 同片判定：有 tmdb_id 用 tmdb_id；否则 norm+year 兜底。
# AV / 里番 的命名很乱（"[Studio] 标题"），标题归一化会把不相干的片子判成同片，
# 所以这两类只认 av_code。
_DUP_KEY = ("CASE WHEN COALESCE(tmdb_id,'')!='' THEN 't:'||tmdb_id "
            "ELSE 'n:'||norm||':'||COALESCE(year,0) END")

# ---- 文件身份：分集 / 多版本细分 ----
# 只按 tmdb / 标题+年份 分组有两类实测误报（2026-10-24 用户报的）：
#
# 1) 分集被当成重复。Emby 把剧集的每一集当成独立条目（纪录片 / 综艺库常见）：
#    name 是剧名，集数信息只在 path 的文件名里。「国家地理 我家就是军火铺」
#    8 集共用同一 norm+year，旧口径算 8 个副本 —— 其实只是不同集。
#    反过来，同一集的高低码两份（第3集 1080p / 第3集 2160p）才是真重复。
#    ⇒ 分集文件按集数标记并入归组键。
# 2) 误刮同 tmdb。不同片子被 Emby 刮成同一 tmdb（多部纪录片全刮成「狩猎季」），
#    多库同扫时捆成一组。⇒ 跨库合并时要求归一化片名一致（去掉尾部年份）；
#    库内不校验 ——「龙珠 / 七龙珠」「月光骑士 / Moon Knight」这种同片不同
#    译名正是要抓的重复，不能误杀。
_EP_MARK = re.compile(r"第\s*(\d+)\s*[集期話话]|\bS(\d{1,2})E(\d{1,3})\b|\bEP?(\d{2,3})\b", re.I)
_SEASON_DIR = re.compile(r"(?:^|/)Season(?:/|$)", re.I)
_QUALITY = re.compile(r"""(?xi)\b(?:
      2160p|1080p|720p|480p|576p|4k|8k|uhd|hdr10\+?|hdr|dv|dolby ?vision|
      remux|blu-?ray|web-?dl|webrip|hdtv|bdrip|dvdrip|
      h\.?265|hevc|x265|h\.?264|x264|avc|vc-?1|10-?bit|8-?bit|
      dts-?hd(?:\s*ma)?|dts|truehd|atmos|eac3|ac3|ddp?\d(?:\.\d)?|aac|flac|lpcm|
      sdr|imax|3d|60fps|50fps|30fps|24fps|120hz|60hz)\b""")
_VIDEO_EXT = re.compile(r"\.(?:strm|mkv|mp4|avi|ts|iso|m2ts|mov|wmv|rmvb|flv)$", re.I)
_YEAR_TAIL = re.compile(r"\d{4}$")


def _ep_marker(path):
    """路径里的集数标记 → 规范化键：'S01E03'→'s01e03'，'第3集'/'EP03'→'e03'。"""
    m = _EP_MARK.search(path or "")
    if not m:
        return ""
    if m.group(2):                       # SxxEyy（带季号）
        return "s%02de%02d" % (int(m.group(2)), int(m.group(3)))
    return "e%02d" % int(m.group(1) or m.group(4))


def _file_core(path):
    """文件名去掉画质/音质/帧率标签后的核心（Season 目录下的文件靠它区分）。"""
    base = (path or "").rsplit("/", 1)[-1]
    base = _VIDEO_EXT.sub("", base)
    if " - " in base:                    # Release 名把画质写在横线后
        base = base.split(" - ", 1)[0]
    base = _QUALITY.sub(" ", base)
    base = re.sub(r"[()\[\]{}【】_\-.]+", " ", base)
    return re.sub(r"\s+", " ", base).strip().lower()


def _file_identity(path):
    """分集文件的身份键；普通影片文件返回 ''（按 tmdb/标题归组，不细分）。

    第3集 1080p 与 第3集 2160p → 同键（真重复）；
    第3集 与 第4集 → 不同键（不同集，不算重复）；
    Season 目录下没集号的文件（误刮高发）→ 按文件名核心区分。
    """
    mk = _ep_marker(path)
    if mk:
        return mk
    if _SEASON_DIR.search(path or ""):
        return "f:" + _file_core(path)
    return ""


def _name_core(norm):
    """归一化标题去掉尾部年份，用于跨库同名校验。"""
    return _YEAR_TAIL.sub("", norm or "")


def _split_by_file(items, by_code=False, min_n=2, multi_lib=False):
    """把同一归组键的条目细分成子组，只留 ≥min_n 的（详见顶部注释）。

    items 需带 k（归组键）/ library / path / norm 字段。
    by_code（AV/里番按番号）不细分——番号即文件，没有分集问题。
    multi_lib=归组键的条目跨了多个库 → tmdb 组加同名校验，防误刮捆包。
    """
    if by_code:
        return [items] if len(items) >= min_n else []
    plain, eps = {}, {}
    for it in items:
        ident = _file_identity(it.get("path") or "")
        (eps if ident else plain).setdefault(ident, []).append(it)
    key = str((items[0] if items else {}).get("k") or "")
    out = []
    for g in list(plain.values()) + list(eps.values()):
        if len(g) < min_n:
            continue
        if multi_lib and key.startswith("t:"):
            byname = {}
            for it in g:
                byname.setdefault(_name_core(it.get("norm") or ""), []).append(it)
            out.extend(s for s in byname.values() if len(s) >= min_n)
        else:
            out.append(g)
    return out


def dup_libs(category="", bucket=""):
    """排重范围 → Emby 库列表。

    bucket（三级分类）优先；只给了二级分类就用它下面所有子类的库；
    两者都空 = 全库（侧栏旧入口 / 深链兜底）。
    """
    if bucket:
        return libs_for(bucket)
    cat = (category or "").strip()
    if cat and cat != "all":
        return libs_for_cat(cat)
    return ()


def is_av_scope(category="", bucket=""):
    return (bucket or "") in ("av", "hentai") or (category or "").strip() in ("AV", "里番")


def _dup_where(libs=None, by_code=False):
    """排重的 WHERE。libs 非空 = 只在这些库里找；空 = 全库（标题口径排除 Adult/三级）。"""
    libs = [l for l in (libs or []) if l]
    where = "av_code!=''" if by_code else "norm!=''"
    args = []
    if libs:
        where += " AND library IN (%s)" % ",".join("?" * len(libs))
        args = list(libs)
    elif not by_code:
        where += " AND library NOT LIKE 'Adult%' AND library NOT LIKE '%-三级'"
    return where, args


def duplicates(libs=None, limit=300, min_n=2, by_code=False):
    """同片多文件分组：[{key, code, n, title, year, items:[{emby_id,name,library,path,medias,year,type}]}]。

    libs 为空 = 全库；非空 = 只在这些 Emby 库里找（分类页「一键排重」走这条）。
    by_code=True 按 av_code 判定（AV / 里番），否则 tmdb_id → 标题+年份。
    分集文件按集数细分、跨库合并要求同名（_split_by_file），
    避免同部不同集 / 误刮同 tmdb 被算成重复。
    """
    c = conn()
    key = "av_code" if by_code else _DUP_KEY
    where, args = _dup_where(libs, by_code)
    rows = c.execute(f"""
        SELECT {key} k, COUNT(*) n FROM owned WHERE {where}
        GROUP BY k HAVING n>=? ORDER BY n DESC""", args + [min_n]).fetchall()
    keys = [r["k"] for r in rows]
    if not keys:
        return []
    detail = {}
    for it in c.execute(f"""
            WITH cand AS (SELECT {key} k FROM owned WHERE {where}
                          GROUP BY k HAVING COUNT(*)>=?)
            SELECT {key} k, emby_id, name, year, type, library, path, medias, av_code, norm
            FROM owned WHERE {where} AND {key} IN (SELECT k FROM cand)
            ORDER BY library, name""", args + [min_n] + args):
        detail.setdefault(it["k"], []).append(dict(it))
    out = []
    for k in keys:
        items = detail.get(k) or []
        multi = len({i["library"] for i in items}) > 1
        for sub in _split_by_file(items, by_code, min_n, multi_lib=multi):
            out.append({"key": k, "code": (k if by_code else ""), "n": len(sub),
                        "title": sub[0]["name"], "year": sub[0]["year"],
                        "items": sub})
    out.sort(key=lambda g: -g["n"])
    return out[:limit]


def duplicates_av(libs=None, limit=300, min_n=2):
    return duplicates(libs=libs, limit=limit, min_n=min_n, by_code=True)


def dup_summary(libs=None, by_code=False):
    """当前范围的多版本组数 / 冗余文件数 / 按库分布（只统计范围内）。

    口径与 duplicates() 完全一致（同样细分分集 / 跨库同名校验），
    摘要数字和详情列表不会对不上。
    """
    c = conn()
    key = "av_code" if by_code else _DUP_KEY
    where, args = _dup_where(libs, by_code)
    rows = c.execute(f"""
        WITH cand AS (SELECT {key} k FROM owned WHERE {where}
                      GROUP BY k HAVING COUNT(*)>1)
        SELECT {key} k, library, path, norm FROM owned
        WHERE {where} AND {key} IN (SELECT k FROM cand)""", args + args).fetchall()
    by_key = {}
    for r in rows:
        by_key.setdefault(r["k"], []).append(dict(r))
    groups = extra = 0
    by_lib = {}
    for k, members in by_key.items():
        multi = len({m["library"] for m in members}) > 1
        for sub in _split_by_file(members, by_code, multi_lib=multi):
            groups += 1
            extra += len(sub) - 1
        # 按库口径：每个库内部再按同一把钥匙细分（跨库重复两边都计，和历史一致）
        perlib = {}
        for m in members:
            perlib.setdefault(m["library"], []).append(m)
        for lib, lm in perlib.items():
            d = by_lib.setdefault(lib, {"groups": 0, "extra": 0})
            for s in _split_by_file(lm, by_code):
                d["groups"] += 1
                d["extra"] += len(s) - 1
    items = c.execute(f"SELECT COUNT(*) n FROM owned WHERE {where}", args).fetchone()["n"]
    av_groups = groups if by_code else c.execute(
        "SELECT COUNT(*) n FROM (SELECT av_code FROM owned WHERE av_code!='' "
        "GROUP BY av_code HAVING COUNT(*)>1)").fetchone()["n"]
    return {
        "groups": groups,
        "extra_files": extra,
        "items": items,
        "av_groups": av_groups,
        "by_library": [{"library": l, "groups": d["groups"], "extra": d["extra"]}
                       for l, d in sorted(by_lib.items(), key=lambda kv: -kv[1]["extra"])],
    }


now = lambda: datetime.now().isoformat(timespec="seconds")



# ---------------- 仪表盘：各分类缺口分布 ----------------
def gap_by_category():
    """各分类的缺口分布：一次 GROUP BY 出「分类 × 状态」全表。

    仪表盘要画「缺口分布 TOP」。以前只能前端并发调 8 次 stats(分类)，
    实测要 4 秒（前端干等），这里一次查询搞定。
    """
    c = conn()
    agg = {}
    for r in c.execute("SELECT category, match_status s, COUNT(*) n "
                       "FROM catalog GROUP BY category, match_status"):
        agg.setdefault(r["category"] or "", {})[r["s"]] = r["n"]
    rows = []
    for cat, d in agg.items():
        rows.append({
            "category": cat or "未分类",
            "total": sum(d.values()),
            "missing": d.get("missing", 0),
            "owned": d.get("owned", 0),
            "likely": d.get("likely", 0),
            "unmatchable": d.get("unmatchable", 0),
        })
    rows.sort(key=lambda x: (-x["missing"], -x["total"]))
    return rows


# ---------------- 数据库自愈：坏索引 ----------------
def _integrity_rows(c):
    """跑 integrity_check，返回 [(消息,)]。失败抛异常。"""
    return c.execute("PRAGMA integrity_check").fetchall()


def _bad_index_names(rows):
    """从 integrity_check 的报错里挑出涉及的索引名（保持出现顺序）。"""
    names = []
    for (msg,) in rows:
        m = re.search(r"index\s+([A-Za-z0-9_]+)", str(msg) or "")
        if m and m.group(1) not in names:
            names.append(m.group(1))
    return names


def fix_broken_indexes():
    """找出 integrity_check 报错的索引并修好它，返回修复报告。

    背景（2026-10-23 实战）：老库里残留 `idx_cat_sortkey`，建在
    `catalog(sort_key DESC, year DESC, rating DESC)` 上，而 `sort_key`
    早就从普通列改成了**生成列**（table_info 查不到、SELECT 却能取到）。
    索引定义与表结构对不上 ⇒ 每次写 catalog 都得维护这个坏索引 ⇒
    直接抛 `database disk image is malformed`，抓到的条目一条都进不去。

    ⚠️ `REINDEX` 只是止血：修好后跑一次写入又坏回去。所以流程是
    **先 REINDEX（保住索引）→ 复检 → 还坏就 DROP INDEX**（索引可以重建，
    数据不受影响）。

    删掉 sort_key 那条索引不影响 `ORDER BY c.sort_key`（用的是列本身，
    不是索引），只是排序走全表扫描。
    """
    rep = {"ok": True, "reindexed": [], "dropped": [], "errors": [], "detail": ""}
    c = conn()
    try:
        try:
            rows = _integrity_rows(c)
        except Exception as e:
            rep.update(ok=False, detail="integrity_check 失败：%s" % e)
            return rep
        if not rows:
            return rep
        if str(rows[0][0] or "").strip().lower() == "ok":
            return rep

        rep["ok"] = False
        rep["detail"] = str(rows[0][0])[:200]
        for ix in _bad_index_names(rows):
            # 先尝试 REINDEX —— 能修好就保住索引
            try:
                c.execute("REINDEX %s" % ix)
                c.commit()
                again = _integrity_rows(c)
                if not [r for r in again if ix in str(r[0])]:
                    rep["reindexed"].append(ix)
                    continue
            except Exception as e:
                rep["errors"].append("REINDEX %s: %s" % (ix, e))
            # 还坏 → 直接删索引
            try:
                c.execute("DROP INDEX IF EXISTS %s" % ix)
                c.commit()
                rep["dropped"].append(ix)
            except Exception as e:
                rep["errors"].append("DROP %s: %s" % (ix, e))

        # 收尾整体复检
        try:
            rows2 = _integrity_rows(c)
            rep["ok"] = bool(rows2) and str(rows2[0][0]).strip().lower() == "ok"
            rep["detail"] = str(rows2[0][0])[:200] if rows2 else ""
        except Exception as e:
            rep["errors"].append("复检失败：%s" % e)
        return rep
    finally:
        try:
            c.close()
        except Exception:
            pass
