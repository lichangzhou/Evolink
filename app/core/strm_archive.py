"""
EvoLink 归档刮削模块（方案 A：不搬动网盘文件，只做扫描 → 识别 → 匹配 TMDB → 分类 → 规划 strm 输出路径）
"""

import os
import re
import sqlite3
from typing import Optional, Dict, List, Any

from core import strm_classify

CATALOG_DB = "/app/data/vl.db"

_MEDIA_EXTS = {
    "mkv", "mp4", "avi", "ts", "m2ts", "iso",
    "wmv", "mov", "flv", "webm", "m4v"
}

# 归一化用：Python 标准 re 不支持 \p{P} 这类 Unicode 属性转义，
# 所以不写正则，直接按字符的 isalnum() 过滤（中文与字母数字都保留）。
_PUNCT_RE = re.compile(r"[\s\.,;:!?\-_/\\'\"()\[\]{}<>|~`@#$%^&*+=_，。；：！？、（）【】《》「」『』“”‘’…—·]+")


def norm_key(text: str) -> str:
    """标题归一化：转小写、去掉所有空白与标点符号（中英文）。用于精确匹配。"""
    if not text:
        return ""
    t = str(text).lower()
    # 先去空白（isalnum 已能覆盖），再兜底过滤标点
    t = "".join(ch for ch in t if ch.isalnum())
    return t


def load_catalog_index(db_path: str = CATALOG_DB, media: str = None) -> dict:
    """
    加载 catalog 到内存索引。
    返回 {norm_key: [row, ...]}，row 是 dict，至少含：
      title, original_title, year, genres, regions, media, tmdb_id, category, rating
    media: None=全部；'movie'；'tv'。永远排除 media='' 的行。
    内存占用要可控：用 dict 存，不要把整行全塞进去。
    """
    index: Dict[str, List[Dict[str, Any]]] = {}
    if not db_path or not os.path.exists(db_path):
        return index

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cols = ["title", "original_title", "year", "genres", "regions",
                "media", "tmdb_id", "category", "rating"]
        col_list = ", ".join(cols)

        if media in ("movie", "tv"):
            sql = f"SELECT {col_list} FROM catalog WHERE media = ?"
            params = (media,)
        else:
            # 只收 movie / tv，AV 类（media 为空或空格）一律排除
            sql = f"SELECT {col_list} FROM catalog WHERE media IN ('movie','tv')"
            params = ()

        cur.execute(sql, params)
        for row in cur.fetchall():
            d = {c: row[c] for c in cols}
            # 确保 media 非空
            if not d.get("media"):
                continue
            # 用 title 和 original_title 做 key
            keys = set()
            for field in ("title", "original_title"):
                v = d.get(field)
                if v:
                    k = norm_key(v)
                    if k:
                        keys.add(k)
            for k in keys:
                index.setdefault(k, []).append(d)
        conn.close()
    except Exception:
        pass

    return index


def infer_media(info: dict) -> str:
    """从解析结果推断媒体类型：有 season 或 episode → 'tv'，否则 'movie'。"""
    if not info:
        return "movie"
    if info.get("season") is not None or info.get("episode") is not None:
        return "tv"
    return "movie"


def match_catalog(info: dict, index: dict, threshold: float = 0.72) -> dict:
    """
    用解析结果匹配 catalog。
    先用 norm_key(info['title']) 精确命中 index（可能多个候选，全部参与打分）；
    精确未命中时，退化为在 index 的 key 里做相似度扫描（用 strm_classify.similarity），
    为控制耗时只取相似度前 50 的候选参与打分。

    打分规则（满分 100）：
      标题分 60：sim = max(similarity(info.title, cand.title), similarity(info.title, cand.original_title))
                 sim * 60
      年份分 25：info.year 与 cand.year 相等 → 25；差 1 年 → 15；差 2~3 年 → 7；差更多 → 0；
                 info.year 为 None → 12（不给满分但保留机会）
      类型分 15：infer_media(info) == cand.media → 15；否则 → 4
    返回得分最高的候选：
      {"title","original_title","year","genres","regions","media","tmdb_id","category","rating","score"}
    最高分 < threshold 时返回 None。
    """
    if not info or not index:
        return None

    title = info.get("title") or ""
    if not title:
        return None

    info_year = info.get("year")
    info_media = infer_media(info)

    # 收集候选
    candidates: List[Dict[str, Any]] = []
    seen_ids = set()

    # 1. 精确命中
    nk = norm_key(title)
    if nk and nk in index:
        for cand in index[nk]:
            cid = id(cand)
            if cid not in seen_ids:
                seen_ids.add(cid)
                candidates.append(cand)

    # 2. 相似度扫描（仅当精确未命中或候选太少时补充）
    if len(candidates) < 5:
        # 取 index 的 key 做相似度
        scored_keys = []
        for k in index.keys():
            if not k:
                continue
            sim = strm_classify.similarity(title, k)
            if sim > 0.3:
                scored_keys.append((sim, k))
        scored_keys.sort(key=lambda x: x[0], reverse=True)
        for _, k in scored_keys[:50]:
            for cand in index[k]:
                cid = id(cand)
                if cid not in seen_ids:
                    seen_ids.add(cid)
                    candidates.append(cand)

    if not candidates:
        return None

    # 打分
    best = None
    best_score = -1.0

    for cand in candidates:
        cand_title = cand.get("title") or ""
        cand_orig = cand.get("original_title") or ""
        cand_year = cand.get("year")
        cand_media = cand.get("media") or ""

        # 年份硬否决：片名完全相同但年份相差超过 3 年 —— 是不同的一部电影
        # （同名翻拍/重制很常见），直接淘汰这个候选，不靠分数救。
        if info_year is not None and cand_year is not None:
            try:
                if abs(int(info_year) - int(cand_year)) > 3:
                    continue
            except (TypeError, ValueError):
                pass

        # 标题分
        sim1 = strm_classify.similarity(title, cand_title) if cand_title else 0.0
        sim2 = strm_classify.similarity(title, cand_orig) if cand_orig else 0.0
        sim = max(sim1, sim2)
        title_score = sim * 60

        # 年份分
        if info_year is None:
            year_score = 12.0
        else:
            try:
                iy = int(info_year)
                cy = int(cand_year) if cand_year is not None else None
                if cy is None:
                    year_score = 0.0
                else:
                    diff = abs(iy - cy)
                    if diff == 0:
                        year_score = 25.0
                    elif diff == 1:
                        year_score = 15.0
                    elif diff <= 3:
                        year_score = 7.0
                    else:
                        year_score = 0.0
            except (ValueError, TypeError):
                year_score = 0.0

        # 类型分
        if info_media == cand_media:
            type_score = 15.0
        else:
            type_score = 4.0

        total = title_score + year_score + type_score

        if total > best_score:
            best_score = total
            best = cand

    if best is None or best_score < threshold:
        return None

    return {
        "title": best.get("title"),
        "original_title": best.get("original_title"),
        "year": best.get("year"),
        "genres": best.get("genres"),
        "regions": best.get("regions"),
        "media": best.get("media"),
        "tmdb_id": best.get("tmdb_id"),
        "category": best.get("category"),
        "rating": best.get("rating"),
        "score": round(best_score, 2)
    }


def build_archive_info(info: dict, match: dict) -> dict:
    """
    把解析结果和 catalog 匹配结果合成一个用于分类/渲染的 info 字典。
    规则：
      - title：match 存在且 tmdb_name 启用时用 match['title']（中文名），否则用原 info['title']
      - year：match 存在时优先 match['year']
      - genres / regions / media_type：来自 match（match 为 None 时留空）
      - 其余字段（resolution/source/video_codec/...）沿用原 info
    """

    if not info:
        info = {}

    ainfo = dict(info)

    if match:
        # title：匹配到 catalog 时优先用 TMDB 中文名（更权威的来源）
        if match.get("title"):
            ainfo["title"] = match["title"]
        if match.get("year") is not None:
            ainfo["year"] = match["year"]
        # genres / regions 为中文逗号分隔串（如 "动作,科幻"），与 catalog 一致
        ainfo["genres"] = match.get("genres") or ""
        ainfo["regions"] = match.get("regions") or ""
        ainfo["media_type"] = match.get("media") or ""
        ainfo["tmdb_id"] = str(match.get("tmdb_id") or "")
    else:
        ainfo["genres"] = ""
        ainfo["regions"] = ""
        ainfo["media_type"] = ""
        ainfo["tmdb_id"] = ""

    return ainfo

def plan_archive(items: list, index: dict, categories: dict,
                 name_template: str, folder_template: str,
                 out_dir: str = "", category_first: bool = False,
                 tmdb_name: bool = False, skip_existing: bool = True,
                 weight_rule: dict = None, keep_count: int = 1) -> list:
    """
    生成归档计划（**只规划不写盘**）。

    items: [{"path": "/CloudNAS/.../xxx.mkv", "filename": "xxx.mkv", "size": 12345}, ...]
    index: load_catalog_index 的返回
    categories: {"movie": {...}, "tv": {...}}
    name_template / folder_template: 双括号模板字符串
    out_dir: strm 输出根目录
    category_first: True 时输出路径形如 <out_dir>/<分类>/<子目录>/<目录名>/<文件名>
    tmdb_name: 用 catalog 中文名做命名
    skip_existing: 目标已存在时标 skip

    每个 item 的处理：
      1. info = strm_classify.parse_release_name(item['filename'])
      2. 过滤：info['title'] 为空 → skip（skip_reason='no_title'）
         非媒体扩展名（mkv/mp4/avi/ts/m2ts/iso/wmv/mov/flv/webm/m4v 之外）→ skip（'not_media'）
      3. match = match_catalog(info, index)
      4. ainfo = build_archive_info(info, match)
      5. cls = strm_classify.classify_media(ainfo, categories) → category / subdir / matched
      6. 文件名 = strm_classify.build_filename(ainfo, name_template)
         目录名 = strm_classify.build_folder_name(ainfo, folder_template)
      7. 目标路径拼装（Windows/Posix 都能跑，用 os.path.join）
      8. 返回一条：
         {"src": 源路径, "dst": 目标 strm 路径, "filename": 文件名, "folder": 目录名,
          "category": cls['category'], "subdir": cls['subdir'],
          "matched": bool(cls['matched']), "tmdb_matched": bool(match),
          "score": match['score'] if match else 0,
          "skip": bool, "skip_reason": str}
    返回列表，顺序与 items 一致。
    """
    if not items:
        return []

    if not categories:
        categories = {}
    if not index:
        index = {}

    results = []
    for item in items:
        if not item:
            continue

        src = item.get("path") or ""
        filename = item.get("filename") or ""
        size = item.get("size") or 0

        # 1. 解析
        info = strm_classify.parse_release_name(filename) if filename else {}
        if not info:
            info = {}

        # 2. 过滤
        title = info.get("title") or ""
        if not title:
            results.append({
                "src": src,
                "dst": "",
                "filename": filename,
                "folder": "",
                "category": "",
                "subdir": "",
                "matched": False,
                "tmdb_matched": False,
                "score": 0,
                "skip": True,
                "skip_reason": "no_title"
            })
            continue

        # 检查扩展名
        ext = os.path.splitext(filename)[1].lower().lstrip(".")
        if ext not in _MEDIA_EXTS:
            results.append({
                "src": src,
                "dst": "",
                "filename": filename,
                "folder": "",
                "category": "",
                "subdir": "",
                "matched": False,
                "tmdb_matched": False,
                "score": 0,
                "skip": True,
                "skip_reason": "not_media"
            })
            continue

        # 3. 匹配
        match = match_catalog(info, index)

        # 4. 构建归档信息
        ainfo = build_archive_info(info, match)

        # 如果 tmdb_name 为 False，且 match 存在，但 build_archive_info 已经用了 match 的 title，
        # 我们需要根据 tmdb_name 参数决定是否使用 match 的 title。
        # 但 build_archive_info 已经执行了，所以我们需要在这里调整。
        if not tmdb_name and match and match.get("title"):
            # 如果 tmdb_name 为 False，应该用原 info 的 title
            ainfo["title"] = info.get("title") or ""

        # 5. 分类
        cls = strm_classify.classify_media(ainfo, categories) if categories else {}
        if not cls:
            cls = {"category": "", "subdir": "", "path": [], "matched": False}

        category = cls.get("category") or ""
        subdir = cls.get("subdir") or ""
        matched = bool(cls.get("matched"))
        # 完整目录段：一级（媒体类型）+ 二级（分类）+ 三级及以下（子目录链）
        cat_path = [p for p in (cls.get("path") or []) if p]
        if not cat_path:
            cat_path = [p for p in (category, subdir) if p]

        # 6. 构建文件名和目录名
        # 归档产物是 .strm 文件，所以 {{fileExt}} 必须渲染成 strm，
        # 而不是源媒体文件的扩展名（mkv/mp4…）—— 否则 Emby 认不出这是 strm。
        ainfo["ext"] = "strm"
        fname = strm_classify.build_filename(ainfo, name_template) if name_template else filename
        if fname and not fname.lower().endswith(".strm"):
            fname = fname + ".strm"
        folder = strm_classify.build_folder_name(ainfo, folder_template) if folder_template else ""

        # 7. 拼装目标路径
        if category_first:
            parts = [out_dir] + cat_path + [folder, fname]
        else:
            parts = [out_dir, folder, fname]

        # 过滤空部分
        parts = [p for p in parts if p]
        dst = os.path.join(*parts) if parts else ""

        # 8. 检查是否已存在
        skip = False
        skip_reason = ""
        if skip_existing and dst and os.path.exists(dst):
            skip = True
            skip_reason = "exists"

        media = ""
        tmdb_id = ""
        if match:
            media = match.get("media") or ainfo.get("media_type") or ""
            tmdb_id = str(match.get("tmdb_id") or ainfo.get("tmdb_id") or "")
        if not media:
            media = "tv" if (info.get("episode") is not None
                             or info.get("season") is not None) else "movie"

        results.append({
            "src": src,
            "dst": dst,
            "filename": fname,
            "folder": folder,
            "category": category,
            "subdir": subdir,
            "matched": matched,
            "tmdb_matched": bool(match),
            "score": match.get("score", 0) if match else 0,
            "media": media,
            "tmdb_id": tmdb_id,
            "size": size,
            "skip": skip,
            "skip_reason": skip_reason
        })

    if weight_rule:
        try:
            results = apply_weight_filter(results, weight_rule, keep_count)
        except Exception:
            pass

    return results


def summarize_plan(plan: list) -> dict:
    """汇总：{"total","matched","unmatched","skipped","by_category": {分类名: 数量}}"""
    if not plan:
        return {
            "total": 0,
            "matched": 0,
            "unmatched": 0,
            "skipped": 0,
            "by_category": {}
        }

    total = len(plan)
    matched = 0
    unmatched = 0
    skipped = 0
    by_category = {}

    for item in plan:
        if not item:
            continue

        if item.get("skip"):
            skipped += 1
            # 被跳过的条目不计入分类统计
            continue
        elif item.get("tmdb_matched"):
            matched += 1
        else:
            unmatched += 1

        cat = item.get("category") or "未分类"
        by_category[cat] = by_category.get(cat, 0) + 1

    return {
        "total": total,
        "matched": matched,
        "unmatched": unmatched,
        "skipped": skipped,
        "by_category": by_category
    }


def _plan_group_key(item):
    """同作品分组键：优先 TMDB id，其次 归一化标题+年份。"""
    tid = str(item.get("tmdb_id") or "").strip()
    if tid:
        return "tmdb:" + tid
    title = norm_key(item.get("title") or "")
    year = item.get("year") or ""
    return "t:%s|%s" % (title, year)


def apply_weight_filter(results, weight_rule, keep_count=1):
    """洗板：同一作品有多个版本时，按权重方案只保留最优的若干个。

    - weight_rule: 权重方案 dict（可为空）
    - keep_count: 同一作品保留几个版本，默认 1
    被淘汰的条目标记 skip=True、skip_reason='lower_version'；
    版本控制开启时，未命中任何版本规则的文件标记 'version_unmatched'。
    """
    if not results or not weight_rule:
        return results
    try:
        from core import strm_weight
    except Exception:
        try:
            import strm_weight
        except Exception:
            return results

    keep = max(1, int(keep_count or 1))
    groups = {}
    for r in results:
        if not isinstance(r, dict) or r.get("skip"):
            continue
        media = r.get("media") or "movie"
        groups.setdefault((_plan_group_key(r), media), []).append(r)

    for (_key, media), items in groups.items():
        rule = _pick_weight_rule(weight_rule, media)
        if not rule:
            continue
        rule = strm_weight.normalize_rule(rule)
        vc_on = bool(rule.get("version_control_enabled"))
        if len(items) <= 1 and not vc_on:
            continue
        metas = []
        for r in items:
            fn = os.path.basename(r.get("src") or "")
            metas.append({
                "filename": fn,
                "path": r.get("src") or "",
                "size": r.get("size") or 0,
                "attrs": strm_weight.build_attrs(fn, r.get("src") or ""),
            })
        ranked = strm_weight.compare_versions(rule, metas)
        rank_of = {}
        score_of = {}
        for i, m in enumerate(ranked):
            rank_of[m.get("path")] = i
            score_of[m.get("path")] = m.get("score_info", {}).get("total", 0)

        if vc_on:
            for r, m in zip(items, metas):
                if strm_weight.version_of(rule, m) is None:
                    r["skip"] = True
                    r["skip_reason"] = "version_unmatched"

        alive = [r for r in items if not r.get("skip")]
        alive.sort(key=lambda r: rank_of.get(r.get("src"), 999999))
        for i, r in enumerate(alive):
            r["weight_score"] = score_of.get(r.get("src"), 0)
            r["weight_rank"] = i + 1
            if i >= keep:
                r["skip"] = True
                r["skip_reason"] = "lower_version"

    return results


def _pick_weight_rule(weight_rule, media):
    """从配置里挑出该媒体类型适用的权重方案。

    支持两种形态：
      - 直接就是一份权重规则（所有类型通用）
      - {"movie": {...}, "tv": {...}}（电影/剧集各用一套）
    """
    if not isinstance(weight_rule, dict) or not weight_rule:
        return None
    if "movie" in weight_rule or "tv" in weight_rule:
        # 明确按类型取：没给该类型的方案就是「这一类不洗板」，不回退到另一类
        sub = weight_rule.get(media)
        return sub if isinstance(sub, dict) and sub else None
    return weight_rule
