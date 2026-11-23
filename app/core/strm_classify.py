# -*- coding: utf-8 -*-
"""
EvoLink 分类设置（规则引擎）模块。

提供媒体文件名的结构化解析、识别词/渲染词替换、相似度匹配、
分词、文件名/目录名模板渲染以及归档分类判定等能力。
仅依赖 Python 标准库。
"""

import os
import re
import difflib
from typing import Optional, List, Dict, Any


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 支持的媒体扩展名
_VALID_EXTS = (
    "mkv", "mp4", "avi", "ts", "m2ts", "iso",
    "wmv", "mov", "flv", "webm", "m4v",
)

# 分辨率标签（统一小写）
_RESOLUTIONS = ("4320p", "2160p", "1080p", "720p", "576p", "480p", "360p")

# 视频编码：匹配模式 -> 输出标识
_VIDEO_CODECS = (
    (r"\bav1\b", "av1"),
    (r"\bh\.?265\b|\bx\.?265\b|\bhevc\b", "h265"),
    (r"\bh\.?264\b|\bx\.?264\b|\bavc\b", "h264"),
)

# 片源：匹配模式 -> 输出标识
_SOURCES = (
    (r"\bblu-?ray\b|\bbd-?rip\b|\bbdrip\b", "bluray"),
    (r"\bweb-?dl\b|\bwebdl\b", "web-dl"),
    (r"\bweb-?rip\b|\bwebrip\b", "web-rip"),
    (r"\bhdtv\b|\bpdtv\b", "hdtv"),
    (r"\bdvd-?rip\b|\bdvdrip\b|\bdvd\b", "dvd"),
    (r"\buhd\b|\bultra\s*hd\b", "uhd"),
)

# 音频编码：匹配模式 -> 输出标识（顺序即优先级，越具体越靠前）
_AUDIO_CODECS = (
    (r"\btruehd\b.*\batmos\b|\batmos\b.*\btruehd\b", "truehd_atmos"),
    (r"\bdts\s*:\s*x\b|\bdts\s*x\b", "dts_x"),
    (r"\bdts\s*-\s*hd\s*ma\b|\bdts\s*hd\s*ma\b", "dts_hd_ma"),
    (r"\btruehd\b", "truehd"),
    (r"\batmos\b", "atmos"),
    (r"\bddp\b", "ddp"),
    (r"\bdtshd\b|\bdts\b", "dts"),
    (r"\bac3\b", "ac3"),
    (r"\baac\b", "aac"),
    (r"\bpcm\b", "pcm"),
    (r"\bflac\b", "flac"),
    (r"\bopus\b", "opus"),
)

# 动态范围：匹配模式 -> 输出标识（顺序即优先级）
_DYNAMIC_RANGES = (
    (r"\bdolby\s*vision\b|\bdv\b", "dolby_vision"),
    (r"\bhdr10\+\b", "hdr10_plus"),
    (r"\bhdr10\b", "hdr10"),
    (r"\bhdr\b", "hdr"),
    (r"\bsdr\b", "sdr"),
)

# 字幕语言：匹配模式 -> 输出标识（顺序即优先级）
_SUBTITLE_LANGS = (
    (r"简繁", "chs_cht"),
    (r"简中|中文字幕|国语|chinese|chs|gb", "chs"),
    (r"繁中|cht", "cht"),
    (r"英文字幕|english|eng", "eng"),
    (r"日文字幕|japanese|jpn", "jpn"),
    (r"韩文字幕|korean|kor", "kor"),
)

# 版本 / Edition：匹配模式 -> 输出标识
_EDITIONS = (
    (r"director'?s\s*cut", "director's cut"),
    (r"\bremux\b", "remux"),
    (r"remastered", "remastered"),
    (r"\bimax\b", "imax"),
    (r"\bextended\b", "extended"),
    (r"ultimate\s*edition", "ultimate edition"),
    (r"\bedition\b", "edition"),
)

# 用于判断「发行组」是否其实是已知标签的正则集合
_KNOWN_TAG_RE = re.compile(
    r"("
    r"4320p|2160p|1080p|720p|576p|480p|360p"
    r"|av1|h\.?265|x\.?265|hevc|h\.?264|x\.?264|avc"
    r"|dts\s*:\s*x|dts\s*x|dts\s*-\s*hd\s*ma|dts\s*hd\s*ma|truehd|atmos|ddp|dtshd|dts|ac3|aac|pcm|flac|opus"
    r"|dolby\s*vision|dv|hdr10\+|hdr10|hdr|sdr"
    r"|简繁|简中|繁中|中文字幕|国语|chinese|chs|cht|gb|英文字幕|english|eng|日文字幕|japanese|jpn|韩文字幕|korean|kor"
    r"|director'?s\s*cut|remux|remastered|imax|extended|ultimate\s*edition|edition"
    r"|blu-?ray|bd-?rip|web-?dl|web-?rip|hdtv|dvd-?rip|uhd|ultra\s*hd"
    r"|10\s*bit|8\s*bit|\d{2,3}fps"
    r"|\b\d{4}\b"
    r"|\bs\d{1,2}\b|\be\d{1,3}\b|\bep\d{1,3}\b"
    r"|第\s*\d+\s*季|第\s*\d+\s*集"
    r")",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _empty_info() -> Dict[str, Any]:
    """构造一个所有字段都为空/None 的解析结果骨架。"""
    return {
        "title": "",
        "year": None,
        "season": None,
        "episode": None,
        "episode_offset": None,
        "resolution": "",
        "source": "",
        "video_codec": "",
        "audio_codec": "",
        "dolby_vision": "",
        "dynamic_range": "",
        "subtitle_lang": "",
        "release_group": "",
        "edition": "",
        "ext": "",
        "raw": "",
    }


def _first_match(text: str, patterns: List[tuple]) -> Optional[str]:
    """按顺序在 text 中查找第一个命中的模式，返回其输出标识。"""
    for pattern, label in patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return label
    return None


def _all_matches(text: str, patterns: List[tuple]) -> List[str]:
    """
    收集所有命中的标签标识（按 patterns 顺序），命中后把该段文本抠掉再继续，
    避免 "hdr10" 被 "hdr" 重复命中。

    例："DV.HDR10" + DYNAMIC_RANGES -> ["dolby_vision", "hdr10"]
    """
    out: List[str] = []
    rest = str(text or "")
    for pattern, label in patterns:
        m = re.search(pattern, rest, flags=re.IGNORECASE)
        if not m:
            continue
        out.append(label)
        rest = rest[: m.start()] + " " + rest[m.end():]
    return out


def _parse_season_episode(text: str) -> Dict[str, Optional[int]]:
    """从文本中解析季/集信息，返回 season/episode/episode_offset。"""
    result: Dict[str, Optional[int]] = {"season": None, "episode": None, "episode_offset": None}

    # S01E02 / S01.E02 / S01 E02 连写
    m = re.search(r"\bS(\d{1,2})\s*[.\-_ ]?\s*E(\d{1,3})(?:\s*\+\s*(\d{1,3}))?", text, flags=re.IGNORECASE)
    if m:
        result["season"] = int(m.group(1))
        result["episode"] = int(m.group(2))
        if m.group(3):
            result["episode_offset"] = int(m.group(3))
        return result

    # 单独的 S01
    m = re.search(r"\bS(\d{1,2})\b", text, flags=re.IGNORECASE)
    if m:
        result["season"] = int(m.group(1))

    # 单独的 E02 / EP02（含 offset）
    m = re.search(r"\bE(?:P)?(\d{1,3})(?:\s*\+\s*(\d{1,3}))?", text, flags=re.IGNORECASE)
    if m:
        result["episode"] = int(m.group(1))
        if m.group(2):
            result["episode_offset"] = int(m.group(2))

    # Season 1 / 第1季
    m = re.search(r"season\s*(\d{1,2})|第\s*(\d{1,2})\s*季", text, flags=re.IGNORECASE)
    if m and result["season"] is None:
        result["season"] = int(m.group(1) or m.group(2))

    # 第02集
    m = re.search(r"第\s*(\d{1,3})\s*集", text)
    if m and result["episode"] is None:
        result["episode"] = int(m.group(1))

    return result


def _extract_release_group(text: str) -> str:
    """
    提取发行组：取最后一个「点分段」，若段内含 - / ~ 再取其后半部分。

    例：
        "Movie.2020.1080p.BluRay.x264-GROUP"  -> "GROUP"
        "Movie.2020.1080p.WEB-DL.x265"        -> ""   (x265 是已知标签)
        "Movie~GROUP"                         -> "GROUP"
    """
    segs = [s for s in re.split(r"\.", str(text or "")) if s.strip()]
    if not segs:
        return ""
    last = segs[-1].strip()
    if "-" in last:
        candidate = last.rsplit("-", 1)[-1].strip()
    elif "~" in last:
        candidate = last.rsplit("~", 1)[-1].strip()
    else:
        candidate = last
    if not candidate:
        return ""
    # 命中已知标签（分辨率/编码/片源等）就不是发行组
    if _KNOWN_TAG_RE.search(candidate):
        return ""
    # 发行组至少要有 2 个字母
    if not re.search(r"[A-Za-z]{2,}", candidate):
        return ""
    return candidate


def _clean_title(text: str) -> str:
    """清理片名：去首尾空白与分隔符，点号/下划线/连字符转空格。"""
    text = str(text or "").strip()
    # 点号分隔的词转空格（英文 "Breaking.Bad" → "Breaking Bad"，中文 "阿凡达.火与烬" 同理）
    text = re.sub(r"(?<=\w)\.(?=\w)", " ", text)
    text = re.sub(r"[_\-]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"^[\s\-_.(\[]+", "", text)
    text = re.sub(r"[\s\-_.)\]]+$", "", text)
    return text.strip()


# ---------------------------------------------------------------------------
# 1. normalize_rules
# ---------------------------------------------------------------------------

def normalize_rules(cfg: dict) -> dict:
    """
    把配置规整成标准结构，缺省值补齐。

    参数:
        cfg: 原始配置 dict（可为 None 或空）。

    返回:
        规整后的 dict，包含所有标准 key，且类型/取值范围已校正。
        各 *_links 项被解析成 {"from": 识别词, "to": 渲染词}。
    """
    cfg = cfg or {}

    def _as_bool(v: Any, default: bool) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(v)
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on")
        return default

    def _clamp(v: Any, lo: float, hi: float, default: float) -> float:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, f))

    def _clamp_int(v: Any, lo: int, hi: int, default: int) -> int:
        try:
            i = int(v)
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, i))

    def _as_str_list(v: Any) -> List[str]:
        if v is None:
            return []
        if isinstance(v, str):
            v = [v]
        if not isinstance(v, (list, tuple)):
            return []
        out: List[str] = []
        for item in v:
            s = str(item).strip()
            if s:
                out.append(s)
        return out

    def _parse_links(v: Any) -> List[Dict[str, str]]:
        """解析 '识别词 => 渲染词' 形式的链接规则。"""
        raw = _as_str_list(v)
        links: List[Dict[str, str]] = []
        for item in raw:
            if "=>" not in item:
                continue
            left, right = item.split("=>", 1)
            left = left.strip()
            right = right.strip()
            if not left or not right:
                continue
            links.append({"from": left, "to": right})
        return links

    return {
        "enable_word_search": _as_bool(cfg.get("enable_word_search"), False),
        "enable_similarity_match": _as_bool(cfg.get("enable_similarity_match"), True),
        "similarity_threshold": _clamp(cfg.get("similarity_threshold"), 0.0, 1.0, 0.85),
        "enable_tmdbinfo_persist": _as_bool(cfg.get("enable_tmdbinfo_persist"), False),
        "custom_keywords": _as_str_list(cfg.get("custom_keywords")),
        "custom_release_groups": _as_str_list(cfg.get("custom_release_groups")),
        "custom_render_words": _as_str_list(cfg.get("custom_render_words")),
        "custom_capturing_groups": _as_str_list(cfg.get("custom_capturing_groups")),
        "keyword_links": _parse_links(cfg.get("keyword_links")),
        "release_group_links": _parse_links(cfg.get("release_group_links")),
        "render_word_links": _parse_links(cfg.get("render_word_links")),
        "capturing_group_links": _parse_links(cfg.get("capturing_group_links")),
        "custom_words_update_time": str(cfg.get("custom_words_update_time") or "30 2 * * *"),
        "cache_expire_time": _clamp_int(cfg.get("cache_expire_time"), 1, 720, 24),
    }


# ---------------------------------------------------------------------------
# 2. parse_release_name
# ---------------------------------------------------------------------------

# 片名截断标记：片名 = 第一个技术标签之前的内容
_CUT_PATTERNS = (
    r"\bS\d{1,2}\s*[.\-_ ]?\s*E\d{1,3}(?:\s*\+\s*\d{1,3})?",
    r"\bS\d{1,2}\b",
    r"\bE(?:P)?\d{1,3}(?:\s*\+\s*\d{1,3})?",
    r"第\s*\d{1,2}\s*季",
    r"第\s*\d{1,3}\s*集",
    r"\b(?:4320|2160|1080|720|576|480|360)p\b",
    r"\b(?:8|10)[\s\-]?bit\b",
    r"\b\d{2,3}(?:\.\d)?fps\b",
    r"\bh\.?265\b|\bx\.?265\b|\bhevc\b|\bh\.?264\b|\bx\.?264\b|\bavc\b|\bav1\b",
    r"\btruehd\b|\batmos\b|\bdts\b|\bddp\b|\bac3\b|\baac\b|\bpcm\b|\bflac\b|\bopus\b",
    r"\bdolby\s*vision\b|\bdv\b|\bhdr10\+\b|\bhdr10\b|\bhdr\b|\bsdr\b",
    r"\bblu-?ray\b|\bbd-?rip\b|\bweb-?dl\b|\bweb-?rip\b|\bhdtv\b|\bdvd-?rip\b|\buhd\b|\bultra\s*hd\b",
    r"\bremux\b|\bremastered\b|\bdirector'?s\s*cut\b|\bimax\b|\bextended\b|\bultimate\s*edition\b",
    r"\b\d\.\d\b(?=\s|$)",
)


def _cut_at_tag(text: str) -> str:
    """把片名截断到第一个技术标签之前的位置。"""
    best = None
    for pat in _CUT_PATTERNS:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m and (best is None or m.start() < best):
            best = m.start()
    if best is None:
        return text
    return text[:best]


def parse_release_name(filename: str) -> dict:
    """
    解析一个媒体文件名，提取结构化信息。

    参数:
        filename: 文件名或完整路径（兼容 Windows 反斜杠）。

    返回:
        包含 title/year/season/episode/episode_offset/resolution/video_codec/
        audio_codec/dolby_vision/dynamic_range/subtitle_lang/release_group/
        edition/ext/raw 的 dict。无法确定的字段留空/None。
    """
    info = _empty_info()

    if filename is None:
        return info

    raw = str(filename)
    info["raw"] = raw

    # 兼容 Windows 反斜杠，取最后一段
    normalized = raw.replace("\\", "/")
    base = os.path.basename(normalized)
    if not base:
        return info

    # 扩展名
    lower_base = base.lower()
    for ext in _VALID_EXTS:
        if lower_base.endswith("." + ext):
            info["ext"] = ext
            base = base[: -(len(ext) + 1)]
            break

    # 年份：优先括号，其次点分段，最后独立 token（多候选取最后一个）
    year_span = None
    m = re.search(r"[(\[]\s*(19\d{2}|20\d{2})\s*[)\]]", base)
    if m:
        info["year"] = int(m.group(1))
        year_span = m.span()
    else:
        m = re.search(r"(?:^|\.)(19\d{2}|20\d{2})(?=\.|$)", base)
        if m:
            info["year"] = int(m.group(1))
            year_span = m.span(1)
        else:
            cands = list(re.finditer(r"(?<![.\w])(19\d{2}|20\d{2})(?![.\w])", base))
            # 只有一个候选且位于文件名末尾时不认为是年份（避免 "Blade Runner 2049"）
            if len(cands) > 1:
                info["year"] = int(cands[-1].group(1))
                year_span = cands[-1].span(1)
            elif len(cands) == 1 and cands[0].end() < len(base.rstrip()):
                info["year"] = int(cands[0].group(1))
                year_span = cands[0].span(1)

    # 季/集
    se = _parse_season_episode(base)
    info["season"] = se["season"]
    info["episode"] = se["episode"]
    info["episode_offset"] = se["episode_offset"]

    # 分辨率
    info["resolution"] = _first_match(base, [(r"\b" + r + r"\b", r) for r in _RESOLUTIONS]) or ""

    # 片源
    info["source"] = _first_match(base, _SOURCES) or ""

    # 视频编码
    info["video_codec"] = _first_match(base, _VIDEO_CODECS) or ""

    # 音频编码
    info["audio_codec"] = _first_match(base, _AUDIO_CODECS) or ""

    # 动态范围（可能同时存在 DV + HDR10，用 + 连接）
    drs = _all_matches(base, _DYNAMIC_RANGES)
    info["dynamic_range"] = "+".join(drs)
    if "dolby_vision" in drs:
        info["dolby_vision"] = "dolby_vision"

    # 字幕语言
    info["subtitle_lang"] = _first_match(base, _SUBTITLE_LANGS) or ""

    # 版本 / Edition
    info["edition"] = _first_match(base, _EDITIONS) or ""

    # 发行组
    info["release_group"] = _extract_release_group(base)

    # 片名：截断到第一个技术标签之前
    title = base
    # 去掉年份所在的那一段（仅当它是独立的括号段 / 点分段）
    if year_span:
        title = title[: year_span[0]] + " " + title[year_span[1]:]
    title = _cut_at_tag(title)

    info["title"] = _clean_title(title)
    return info


# ---------------------------------------------------------------------------
# 3. apply_links
# ---------------------------------------------------------------------------

def apply_links(rules: dict, terms: dict) -> dict:
    """
    把「识别词 → 渲染词」的替换规则应用到已解析出的词条上。

    参数:
        rules: normalize_rules 的输出。
        terms: parse_release_name 的输出。

    返回:
        新的 dict（不修改入参），其中 title/release_group 已按规则替换，
        并附带 applied 列表记录每次命中。
    """
    rules = rules or {}
    result: Dict[str, Any] = dict(terms or {})
    applied: List[Dict[str, str]] = []

    def _apply(links: List[Dict[str, str]], field: str) -> None:
        value = result.get(field)
        if not isinstance(value, str) or not value:
            return
        for link in links or []:
            src = (link or {}).get("from", "")
            dst = (link or {}).get("to", "")
            if not src:
                continue
            new_value = re.sub(re.escape(src), dst, value, flags=re.IGNORECASE)
            if new_value != value:
                value = new_value
                applied.append({"from": src, "to": dst, "field": field})
        result[field] = value

    _apply(rules.get("keyword_links"), "title")
    _apply(rules.get("release_group_links"), "release_group")
    _apply(rules.get("render_word_links"), "title")
    _apply(rules.get("capturing_group_links"), "release_group")

    result["applied"] = applied
    return result


# ---------------------------------------------------------------------------
# 4. similarity
# ---------------------------------------------------------------------------

def similarity(a: str, b: str) -> float:
    """
    计算两个字符串的相似度（0.0 ~ 1.0）。

    参数:
        a: 字符串 A。
        b: 字符串 B。

    返回:
        相似度 float；任一为空返回 0.0。
    """
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, str(a).lower(), str(b).lower()).ratio()


# ---------------------------------------------------------------------------
# 5. match_title
# ---------------------------------------------------------------------------

def match_title(name: str, candidates: list, rules: dict, threshold: float = None) -> dict:
    """
    把 name 和候选标题列表做相似度匹配，取最高分。

    参数:
        name: 待匹配名称。
        candidates: 候选标题列表。
        rules: normalize_rules 的输出（用于取默认阈值）。
        threshold: 相似度阈值；None 时取 rules 中的 similarity_threshold。

    返回:
        {"index": int, "score": float, "matched": str}
        index 为 -1 表示未匹配。
    """
    rules = rules or {}
    if threshold is None:
        threshold = rules.get("similarity_threshold", 0.85)

    if not candidates:
        return {"index": -1, "score": 0.0, "matched": ""}

    best_index = -1
    best_score = 0.0
    best_matched = ""

    for i, cand in enumerate(candidates):
        score = similarity(name, cand)
        if score > best_score:
            best_score = score
            best_index = i
            best_matched = str(cand)

    if best_score < threshold:
        return {"index": -1, "score": best_score, "matched": ""}

    return {"index": best_index, "score": best_score, "matched": best_matched}


# ---------------------------------------------------------------------------
# 6. split_keywords
# ---------------------------------------------------------------------------

def split_keywords(text: str, rules: dict) -> list:
    """
    启用分词搜索时，把文本按分隔符拆成词块。

    参数:
        text: 待拆分文本。
        rules: normalize_rules 的输出。

    返回:
        去空、去重（保持原顺序）的词块列表；
        未启用分词时直接返回 [text]。
    """
    rules = rules or {}
    if not rules.get("enable_word_search"):
        return [text]

    if text is None:
        return []

    # 分隔符：空格 . _ - · [ ] ( ) ,
    parts = re.split(r"[\s.\-_·\[\]()，,]+", str(text))
    seen = set()
    out: List[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


# ---------------------------------------------------------------------------
# 7. build_filename
# ---------------------------------------------------------------------------

# 占位符 -> info 字段 的映射
_PLACEHOLDER_MAP = {
    "title": "title",
    "year": "year",
    "tmdbid": "tmdb_id",
    "season": "season",
    "episode": "episode",
    "resolution": "resolution",
    "source": "source",
    "videoCodec": "video_codec",
    "audioCodec": "audio_codec",
    "dynamicRange": "dynamic_range",
    "edition": "edition",
    "releaseGroup": "release_group",
    "fileExt": "ext",
}


def _render_template(info: dict, template: str) -> str:
    """按 {{key}} 占位符渲染模板，找不到的替换为空串，并压缩空格。"""
    if not template:
        return ""

    def _replace(match: "re.Match") -> str:
        key = match.group(1).strip()
        field = _PLACEHOLDER_MAP.get(key)
        if field is None:
            return ""
        value = (info or {}).get(field)
        if value is None:
            return ""
        return str(value)

    rendered = re.sub(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}", _replace, template)
    # 压缩连续空格
    rendered = re.sub(r"\s+", " ", rendered)
    # 空占位符会在分隔点之间留下空洞："a..b" / "a. .b" → 压成一个点
    rendered = re.sub(r"\.(\s*\.)+", ".", rendered)
    # 去掉结尾多余的分隔点/空格："a." → "a"
    rendered = re.sub(r"[.\s]+$", "", rendered)
    return rendered.strip()


def build_filename(info: dict, template: str, season_template: str = None) -> str:
    """
    按模板拼出最终文件名。

    参数:
        info: 解析结果 dict。
        template: 文件名模板，占位符形如 {{title}}。
        season_template: 季/集模板（可选，当前实现中未单独使用，
            保留以兼容签名）。

    返回:
        渲染后的文件名；template 为空返回空串。
    """
    if not template:
        return ""
    return _render_template(info, template)


# ---------------------------------------------------------------------------
# 8. build_folder_name
# ---------------------------------------------------------------------------

def build_folder_name(info: dict, template: str) -> str:
    """
    按模板拼出目录名（不带扩展名）。

    参数:
        info: 解析结果 dict。
        template: 目录名模板。

    返回:
        渲染后的目录名（去掉末尾扩展名部分）。
    """
    if not template:
        return ""
    rendered = _render_template(info, template)
    # 去掉末尾的扩展名（如 .mkv）
    rendered = re.sub(r"\.[A-Za-z0-9]+$", "", rendered)
    return rendered.strip()


# ---------------------------------------------------------------------------
# 9. classify_media
# ---------------------------------------------------------------------------

def _split_csv(value: Any) -> List[str]:
    """把逗号分隔的值拆成去空、去空白的列表。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [s.strip() for s in str(value).split(",") if s.strip()]


def _match_genre_ids(config_value: Any, info_genre_ids: Any) -> bool:
    """
    匹配 genre_ids：逗号分隔的数字，带负号表示排除。
    例如 "99,-10402" 表示含 99 且不含 10402。
    """
    tokens = _split_csv(config_value)
    if not tokens:
        return True

    info_ids = set()
    if isinstance(info_genre_ids, (list, tuple, set)):
        for v in info_genre_ids:
            try:
                info_ids.add(int(v))
            except (TypeError, ValueError):
                continue
    elif info_genre_ids is not None:
        try:
            info_ids.add(int(info_genre_ids))
        except (TypeError, ValueError):
            pass

    for token in tokens:
        negative = token.startswith("-")
        num_str = token.lstrip("-")
        try:
            num = int(num_str)
        except ValueError:
            continue
        if negative:
            if num in info_ids:
                return False
        else:
            if num not in info_ids:
                return False
    return True


def _match_any(config_value: Any, info_value: Any) -> bool:
    """逗号分隔的候选值，命中任一即满足。"""
    candidates = _split_csv(config_value)
    if not candidates:
        return True

    if info_value is None:
        return False
    if isinstance(info_value, (list, tuple, set)):
        info_set = {str(v).strip().lower() for v in info_value}
    else:
        # 字符串可能是 "大陆,香港" 这种逗号分隔多值
        info_set = {s.strip().lower() for s in str(info_value).split(",") if s.strip()}

    for cand in candidates:
        if cand.lower() in info_set:
            return True
    return False


def _cond_matches(cond: dict, info: dict):
    """判断一条分类条件是否命中 info。

    多个条件之间是「与」关系；负号（"-10402"）表示排除该类型。
    cond 为空（没有任何条件）时视为兜底规则，匹配所有媒体。
    （规则按顺序生效、第一条命中即止，所以兜底规则要放最后一条。）

    参与过滤的字段：genre_ids / genres / original_language /
    origin_country / production_countries / regions / year / keywords。

    不参与过滤的字段：series_actors / series_directors / series_keywords。
    这三个只是「子目录生成器」—— 配了「刘德华,李连杰」意思是
    命中这些演员时在当前分类下建一个「演员名」子目录，
    并不会把没配的演员排除掉。所以不配任何过滤条件时，
    这条规则对所有媒体都成立。

    返回: (是否命中, 命中的系列关键词)
    """
    cond = cond or {}
    info = info or {}

    info_genre_ids = info.get("genre_ids")
    info_language = info.get("original_language")
    info_countries = info.get("production_countries")
    if info_countries is None:
        info_countries = info.get("origin_country")
    title = str(info.get("title") or "")

    has_condition = False

    if cond.get("genre_ids"):
        has_condition = True
        if not _match_genre_ids(cond["genre_ids"], info_genre_ids):
            return False, ""

    if cond.get("original_language"):
        has_condition = True
        if not _match_any(cond["original_language"], info_language):
            return False, ""

    if cond.get("production_countries"):
        has_condition = True
        if not _match_any(cond["production_countries"], info_countries):
            return False, ""

    if cond.get("origin_country"):
        has_condition = True
        if not _match_any(cond["origin_country"], info_countries):
            return False, ""

    # 中文类型名（如 "科幻"）—— 与 catalog.genres 匹配
    if cond.get("genres"):
        has_condition = True
        if not _match_any(cond["genres"], info.get("genres")):
            return False, ""

    # 中文地区名（如 "大陆"/"美国"）—— 与 catalog.regions 匹配
    if cond.get("regions"):
        has_condition = True
        if not _match_any(cond["regions"], info.get("regions")):
            return False, ""

    # 年份区间（如 "2020-2099"）
    if cond.get("year"):
        has_condition = True
        if not _match_year(cond["year"], info.get("year")):
            return False, ""

    # 标题关键词（不区分大小写，命中任意一个即可）
    if cond.get("keywords"):
        has_condition = True
        low = title.lower()
        hit = False
        for kw in _split_csv(cond["keywords"]):
            if kw and kw.lower() in low:
                hit = True
                break
        if not hit:
            return False, ""

    # 系列关键词：只用来给「XX系列」子目录命名，不参与是否命中的判断。
    # 演员 / 导演 同理（见下方注释）。
    series_hit = ""
    if cond.get("series_keywords"):
        kws = _split_csv(cond["series_keywords"])
        low = title.lower()
        for kw in kws:
            if kw.lower() in low:
                series_hit = kw
                break

    # 一条规则什么条件都没填 = 兜底规则：匹配所有媒体。
    # 规则按顺序生效、第一条命中即止，所以兜底规则要放在最后一条。
    return True, series_hit


def _match_year(spec: Any, year: Any) -> bool:
    """匹配年份区间。

    支持 "2020-2099" / "2020-" / "-2019" / "1999,2005" / "2005"。
    媒体没有年份信息时视为不命中（避免把无年份的老片错分进新片分类）。
    """
    s = str(spec or "").strip()
    if not s:
        return True
    try:
        y = int(float(year))
    except (TypeError, ValueError):
        return False
    for part in re.split(r"[,，\s]+", s):
        if not part:
            continue
        if "-" in part:
            lo_s, _, hi_s = part.partition("-")
            try:
                lo = int(lo_s) if lo_s.strip() else None
            except ValueError:
                lo = None
            try:
                hi = int(hi_s) if hi_s.strip() else None
            except ValueError:
                hi = None
            if lo is None and hi is None:
                continue
            if lo is not None and y < lo:
                continue
            if hi is not None and y > hi:
                continue
            return True
        else:
            try:
                if int(part) == y:
                    return True
            except ValueError:
                continue
    return False


def _hit_person(cond: dict, info: dict, field: str) -> str:
    """命中演员 / 导演时返回其名字（用于生成「演员名」「导演名」子目录）。"""
    want = _split_csv(cond.get(field))
    if not want:
        return ""
    have = info.get(field)
    if have is None:
        return ""
    if isinstance(have, (list, tuple, set)):
        names = {str(v).strip() for v in have}
    else:
        names = {str(have).strip()}
    for w in want:
        if w in names:
            return w
    return ""


def _sub_chain(sub_rules: dict, info: dict) -> list:
    """按顺序匹配子分类规则，返回目录名链（可多层嵌套）。

    每条子规则形如 {"genres": "动作", "sub": {...}}，sub 里还能再放子规则，
    于是目录可以是「二级分类 / 三级分类 / 四级分类」。
    """
    if not isinstance(sub_rules, dict):
        return []
    for sub_name, sub_cond in sub_rules.items():
        sub_cond = sub_cond or {}
        matched, series_hit = _cond_matches(sub_cond, info)
        if not matched:
            continue
        chain = [sub_name]
        # 演员 / 导演 / 系列 子目录
        extra = _hit_person(sub_cond, info, "series_actors")
        if not extra:
            extra = _hit_person(sub_cond, info, "series_directors")
        if not extra and series_hit:
            extra = "%s系列" % series_hit
        if extra:
            chain.append(extra)
        deeper = sub_cond.get("sub")
        if isinstance(deeper, dict):
            chain.extend(_sub_chain(deeper, info))
        return chain
    return []


def classify_media(info: dict, categories: dict) -> dict:
    """
    根据分类配置决定归档到哪个分类目录。

    目录层级模型：
        一级目录 = 媒体类型（电影 / 剧集），由 categories["media_type"] 决定，可关闭；
        二级目录 = 分类规则命中（地区 / 语言 / 类型 / 年份 / 关键词 的组合）；
        三级及以下 = 规则里的子目录 sub（可继续嵌套），以及演员名 / 导演名 / 系列名目录。

    参数:
        info: 媒体信息，需含 media_type / genres / regions / year 等。
        categories: 分类配置，形如
            {"movie": {...}, "tv": {...},
             "media_type": {"enabled": true, "movie": "电影", "tv": "剧集"}}
            每条分类规则形如 {"genres": "动作", "regions": "大陆", "sub": {...}}。

    返回:
        {"category": 二级目录名, "subdir": 旧的单层子目录名,
         "path": 完整目录段列表, "matched": bool}
        未匹配时各字段为空，matched 为 False。

    规则按配置里的顺序依次匹配，第一条命中的生效，因此更具体的规则要放前面，
    不带任何条件的兜底规则放最后。
    """
    info = info or {}
    categories = categories or {}

    media_type = info.get("media_type")
    type_categories = categories.get(media_type, {}) or {}
    if not isinstance(type_categories, dict):
        type_categories = {}

    # 一级目录：媒体类型
    mt_cfg = categories.get("media_type") or {}
    head = ""
    if isinstance(mt_cfg, dict) and mt_cfg.get("enabled"):
        head = str(mt_cfg.get(media_type) or "").strip()

    for category_name, cond in type_categories.items():
        cond = cond or {}

        matched, series_hit = _cond_matches(cond, info)
        if not matched:
            continue

        chain = _sub_chain(cond.get("sub") or {}, info)

        # 一级规则自己带的演员 / 导演 / 系列 子目录（没有命中任何子规则时用）
        if not chain:
            extra = _hit_person(cond, info, "series_actors")
            if not extra:
                extra = _hit_person(cond, info, "series_directors")
            if not extra and series_hit:
                extra = "%s系列" % series_hit
            if extra:
                chain = [extra]

        path = ([head] if head else []) + [category_name] + chain
        subdir = chain[0] if chain else ""
        return {"category": category_name, "subdir": subdir,
                "path": path, "matched": True}

    return {"category": "", "subdir": "", "path": [], "matched": False}


# ---------------------------------------------------------------------------
# 10. TMDB 字典（供界面「查看字段字典」展示，用户填条件时对照）
# ---------------------------------------------------------------------------

# TMDB 类型 ID（genre_ids）。部分类型的中英文名在 TMDB 官方接口里不一致，
# 这里同时给出通用中文名，填写时用中文名或数字 ID 都行。
TMDB_GENRES = [
    {"id": "28", "name": "动作", "en": "Action"},
    {"id": "12", "name": "冒险", "en": "Adventure"},
    {"id": "16", "name": "动画", "en": "Animation"},
    {"id": "35", "name": "喜剧", "en": "Comedy"},
    {"id": "80", "name": "犯罪", "en": "Crime"},
    {"id": "99", "name": "纪录片", "en": "Documentary"},
    {"id": "18", "name": "剧情", "en": "Drama"},
    {"id": "10751", "name": "家庭", "en": "Family"},
    {"id": "14", "name": "奇幻", "en": "Fantasy"},
    {"id": "36", "name": "历史", "en": "History"},
    {"id": "27", "name": "恐怖", "en": "Horror"},
    {"id": "10402", "name": "音乐", "en": "Music"},
    {"id": "9648", "name": "悬疑", "en": "Mystery"},
    {"id": "10749", "name": "爱情", "en": "Romance"},
    {"id": "878", "name": "科幻", "en": "Science Fiction"},
    {"id": "10770", "name": "电视电影", "en": "TV Movie"},
    {"id": "53", "name": "惊悚", "en": "Thriller"},
    {"id": "10752", "name": "战争", "en": "War"},
    {"id": "37", "name": "西部", "en": "Western"},
    {"id": "10765", "name": "科幻奇幻", "en": "Sci-Fi & Fantasy"},
    {"id": "10768", "name": "战争政治", "en": "War & Politics"},
]

# TMDB 原始语言代码（original_language）
TMDB_LANGUAGES = [
    {"code": "zh", "name": "中文"}, {"code": "cn", "name": "中文"},
    {"code": "yue", "name": "粤语"}, {"code": "bo", "name": "藏语"},
    {"code": "za", "name": "壮语"}, {"code": "en", "name": "英语"},
    {"code": "ja", "name": "日语"}, {"code": "ko", "name": "朝鲜语"},
    {"code": "th", "name": "泰语"}, {"code": "hi", "name": "印地语"},
    {"code": "id", "name": "印尼语"}, {"code": "vi", "name": "越南语"},
    {"code": "ru", "name": "俄语"}, {"code": "uk", "name": "乌克兰语"},
    {"code": "fr", "name": "法语"}, {"code": "de", "name": "德语"},
    {"code": "it", "name": "意大利语"}, {"code": "es", "name": "西班牙语"},
    {"code": "pt", "name": "葡萄牙语"}, {"code": "nl", "name": "荷兰语"},
    {"code": "sv", "name": "瑞典语"}, {"code": "da", "name": "丹麦语"},
    {"code": "no", "name": "挪威语"}, {"code": "fi", "name": "芬兰语"},
    {"code": "pl", "name": "波兰语"}, {"code": "cs", "name": "捷克语"},
    {"code": "el", "name": "希腊语"}, {"code": "tr", "name": "土耳其语"},
    {"code": "ar", "name": "阿拉伯语"}, {"code": "he", "name": "希伯来语"},
    {"code": "fa", "name": "波斯语"}, {"code": "hi", "name": "印地语"},
]

# TMDB 国家 / 地区代码（origin_country / production_countries）
TMDB_COUNTRIES = [
    {"code": "CN", "name": "中国内地"}, {"code": "TW", "name": "中国台湾"},
    {"code": "HK", "name": "中国香港"}, {"code": "MO", "name": "中国澳门"},
    {"code": "US", "name": "美国"}, {"code": "GB", "name": "英国"},
    {"code": "JP", "name": "日本"}, {"code": "KR", "name": "韩国"},
    {"code": "KP", "name": "朝鲜"}, {"code": "SG", "name": "新加坡"},
    {"code": "TH", "name": "泰国"}, {"code": "IN", "name": "印度"},
    {"code": "FR", "name": "法国"}, {"code": "DE", "name": "德国"},
    {"code": "IT", "name": "意大利"}, {"code": "ES", "name": "西班牙"},
    {"code": "PT", "name": "葡萄牙"}, {"code": "NL", "name": "荷兰"},
    {"code": "RU", "name": "俄罗斯"}, {"code": "UA", "name": "乌克兰"},
    {"code": "AU", "name": "澳大利亚"}, {"code": "CA", "name": "加拿大"},
    {"code": "BR", "name": "巴西"}, {"code": "MX", "name": "墨西哥"},
    {"code": "AR", "name": "阿根廷"}, {"code": "CL", "name": "智利"},
    {"code": "IE", "name": "爱尔兰"}, {"code": "SE", "name": "瑞典"},
    {"code": "NO", "name": "挪威"}, {"code": "DK", "name": "丹麦"},
    {"code": "FI", "name": "芬兰"}, {"code": "PL", "name": "波兰"},
    {"code": "AT", "name": "奥地利"}, {"code": "BE", "name": "比利时"},
    {"code": "CH", "name": "瑞士"}, {"code": "GR", "name": "希腊"},
    {"code": "TR", "name": "土耳其"}, {"code": "IL", "name": "以色列"},
    {"code": "IR", "name": "伊朗"}, {"code": "NZ", "name": "新西兰"},
    {"code": "MY", "name": "马来西亚"}, {"code": "VN", "name": "越南"},
    {"code": "ID", "name": "印度尼西亚"}, {"code": "PH", "name": "菲律宾"},
    {"code": "SA", "name": "沙特阿拉伯"}, {"code": "AE", "name": "阿联酋"},
]
