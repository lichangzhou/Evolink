import re
import json
import os
import sys

DEFAULT_RULE = {
    "name": "EvoLink 默认洗板",
    "audioCodec": {
        "truehd_dolby_atmos_7_1": {"name": "TrueHD Dolby Atmos 7.1", "pattern": "TrueHD Dolby Atmos", "score": 10.0, "description": "杜比无损全景声"},
        "dts_x": {"name": "DTS:X", "pattern": "DTS:?X", "score": 10.0, "description": "次世代无损全景声"},
        "dts_hd_ma": {"name": "DTS-HD MA", "pattern": "DTS-?HD MA", "score": 9.0, "description": "DTS 无损"},
        "ddp_atmos": {"name": "DDP Dolby Atmos", "pattern": "DDP.*Atmos|Dolby Atmos", "score": 7.0, "description": "杜比全景声"},
        "flac": {"name": "FLAC", "pattern": "FLAC", "score": 6.0, "description": "无损音轨"},
        "ac3": {"name": "Dolby Digital", "pattern": "AC-?3|Dolby Digital", "score": 3.0, "description": "有损音轨"},
        "aac": {"name": "AAC", "pattern": "AAC", "score": 2.0, "description": "低码率音轨"}
    },
    "audioCodec_weight": 1.2346,
    "videoCodec": {
        "av1": {"name": "AV1", "pattern": "AV1", "score": 10.0, "description": "新一代编码，压缩率最高"},
        "h265": {"name": "H.265", "pattern": "H\\.265|HEVC|x265", "score": 9.0, "description": "H.265，同画质体积更小"},
        "h264": {"name": "H.264", "pattern": "H\\.264|x264", "score": 6.0, "description": "H.264"}
    },
    "videoCodec_weight": 1.0288,
    "color_depth": {
        "12bit": {"name": "12bit", "pattern": "12[ -]?bit|12bit", "score": 9.0, "description": "12bit 色深"},
        "10bit": {"name": "10bit", "pattern": "10[ -]?bit|10bit", "score": 8.0, "description": "10bit 色深"},
        "8bit": {"name": "8bit", "pattern": "8[ -]?bit|8bit", "score": 4.0, "description": "8bit 色深"}
    },
    "color_depth_weight": 0.823,
    "frame_rate": {
        "60fps": {"name": "60fps", "pattern": "60[ ]?fps", "score": 8.0, "description": "高帧率"},
        "50fps": {"name": "50fps", "pattern": "50[ ]?fps", "score": 6.0, "description": "欧版高帧率"},
        "24fps": {"name": "24fps", "pattern": "24[ ]?fps", "score": 7.0, "description": "电影原帧率"},
        "30fps": {"name": "30fps", "pattern": "30[ ]?fps", "score": 3.0, "description": "30 帧"}
    },
    "frame_rate_weight": 0.2586,
    "dolby_vision": {
        "dv": {"name": "Dolby Vision", "pattern": "Dolby[ .]?Vision|\\bDV\\b", "score": 10.0, "description": "杜比视界"},
        "hdr10": {"name": "HDR10", "pattern": "HDR10", "score": 8.0, "description": "HDR10"}
    },
    "dolby_vision_weight": 1.3874,
    "dynamic_range": {
        "hdr10_plus": {"name": "HDR10+", "pattern": "HDR10\\+", "score": 8.5, "description": "HDR10+ 动态元数据"},
        "hdr10": {"name": "HDR10", "pattern": "HDR10", "score": 8.0, "description": "HDR10"},
        "hdr": {"name": "HDR", "pattern": "HDR", "score": 7.0, "description": "HDR"},
        "hlg": {"name": "HLG", "pattern": "HLG", "score": 7.0, "description": "HLG"},
        "sdr": {"name": "SDR", "pattern": "SDR", "score": 0.0, "description": "SDR 标准动态范围"}
    },
    "dynamic_range_weight": 1.3874,
    "resourceType": {
        "UHD": {"name": "Ultra HD", "pattern": "Ultra HD|UHD", "score": 10.0, "description": "超高清蓝光"},
        "Bluray": {"name": "Blu-Ray", "pattern": "Blu-?Ray|BluRay", "score": 9.5, "description": "蓝光"},
        "Remux": {"name": "Remux", "pattern": "Remux", "score": 9.0, "description": "原盘重封装，画质最好、体积最大"},
        "WEB_DL": {"name": "WEB-DL", "pattern": "WEB-?DL", "score": 7.0, "description": "网络高清"},
        "HDTV": {"name": "HDTV", "pattern": "HDTV|Pdtv", "score": 5.0, "description": "电视录制"},
        "WEBRip": {"name": "WEBRip", "pattern": "WEB-?Rip", "score": 4.0, "description": "网络转载，画质最差"},
        "BluRayRip": {"name": "Blu-Ray Rip", "pattern": "Blu-?Rip|BDRip", "score": 3.0, "description": "蓝光压制"}
    },
    "resourceType_weight": 0.823,
    "videoFormat": {
        "screen_4320p": {"name": "4320p", "pattern": "4320p|8K", "score": 12.0, "description": "8K"},
        "screen_2160p": {"name": "2160p", "pattern": "2160p|4K", "score": 10.0, "description": "4K"},
        "screen_1080p": {"name": "1080p", "pattern": "1080p", "score": 7.0, "description": "1080P"},
        "screen_720p": {"name": "720p", "pattern": "720p", "score": 3.0, "description": "720P"},
        "screen_480p": {"name": "480p", "pattern": "480p", "score": 1.0, "description": "480P"}
    },
    "videoFormat_weight": 1.5432,
    "subtitle": {
        "chs": {"name": "中文字幕", "pattern": "中文|简中|国语|CH[S]?|GB", "score": 8.0, "description": "含中文字幕"},
        "cht": {"name": "繁体字幕", "pattern": "繁中|繁体|CHT|BIG5", "score": 6.0, "description": "含繁体中文字幕"}
    },
    "subtitle_weight": 1.1317,
    "file_extension": {
        "mkv": {"name": "mkv", "pattern": "\\.mkv$", "score": 8.0, "description": "容器更宽松，字幕/音轨兼容性最好"},
        "mp4": {"name": "mp4", "pattern": "\\.mp4$", "score": 6.0, "description": "通用容器"}
    },
    "file_extension_weight": 0.4115,
    "effect": {
        "directors_cut": {"name": "导演剪辑版", "pattern": "Director'?s?[ ]?Cut|最终剪辑版|导演剪辑", "score": 9.0, "description": "导演剪辑版更长"},
        "extended": {"name": "加长版", "pattern": "Extended|加长版|特别版", "score": 8.0, "description": "加长版"},
        "remastered": {"name": "重制版", "pattern": "Remastered|重制", "score": 7.0, "description": "重制版"}
    },
    "effect_weight": 1.0,
    "releaseGroup": [
        {"name": "CHD", "pattern": "CHD|CHDBits", "score": 10, "description": "高码率重制组"}
    ],
    "releaseGroup_weight": 1.0288,
    "releaseGroup_blacklist": [],
    "releaseGroup_whitelist": [],
    "custom_attributes": [
        {"name": "多音轨", "pattern": "[1-2]Audios", "score": 5, "description": "含多音轨"}
    ],
    "custom_weight": 0.1,
    "custom_attributes_blacklist": [],
    "custom_attributes_whitelist": [],
    "file_size_score": 8,
    "file_size_weight": 0.2,
    "file_size_blacklist": [],
    "file_size_whitelist": [],
    "always_override_score": 999999,
    "always_override_weight": 0,
    "always_override_blacklist": [],
    "always_override_whitelist": []
}

WEIGHT_KEYS = {"custom_attributes": "custom_weight"}

ATTR_KEYS = [
    "videoFormat", "audioCodec", "videoCodec", "color_depth", "frame_rate",
    "dolby_vision", "dynamic_range", "resourceType", "subtitle",
    "file_extension", "effect", "releaseGroup", "custom_attributes"
]


def _as_name_list(v):
    """把黑名单 / 白名单的值统一成字符串数组。

    兼容三种历史形态：
      - ["a", "b"]                   正常
      - {"name": "blacklist", ...}   早期被误当成评分条目转成的对象 → 视为空名单
      - {"a": 1, "b": 2}             键值字典 → 取键名
    """
    if v is None:
        return []
    if isinstance(v, list):
        out = []
        for x in v:
            if isinstance(x, dict):
                s = x.get("pattern") or x.get("name") or ""
            else:
                s = x
            s = str(s).strip()
            if s:
                out.append(s)
        return out
    if isinstance(v, dict):
        if set(v.keys()) <= {"name", "pattern", "score", "description"}:
            return []
        return [str(k).strip() for k in v.keys() if str(k).strip()]
    s = str(v).strip()
    return [s] if s else []


def sanitize_rule(rule):
    """修复历史脏数据：把被误当成评分条目的 blacklist / whitelist 还原成数组。

    不改变任何评分条目与权重，只洗掉名单形态上的污染。
    """
    if not isinstance(rule, dict):
        return rule
    out = json.loads(json.dumps(rule))
    for key in ATTR_KEYS:
        val = out.get(key)
        if not isinstance(val, dict):
            continue
        for bw in ("blacklist", "whitelist"):
            if bw in val:
                val[bw] = _as_name_list(val[bw])
    return out


def normalize_rule(rule):
    if not isinstance(rule, dict) or not rule:
        return json.loads(json.dumps(DEFAULT_RULE))

    new_rule = json.loads(json.dumps(rule))
    
    for key in ATTR_KEYS:
        if key not in new_rule:
            new_rule[key] = [] if key in ("releaseGroup", "custom_attributes") else {}
        
        weight_key = f"{key}_weight"
        if weight_key not in new_rule:
            new_rule[weight_key] = 0.0
        else:
            try:
                new_rule[weight_key] = float(new_rule[weight_key])
            except (ValueError, TypeError):
                new_rule[weight_key] = 0.0
        
        attr_val = new_rule[key]
        if isinstance(attr_val, dict):
            for k, v in list(attr_val.items()):
                # blacklist / whitelist 是「名单」不是「评分条目」，
                # 必须保持数组形态。早期版本把它们当普通条目转成了对象，
                # 导致前端读取时崩溃、名单内容丢失。
                if k in ("blacklist", "whitelist"):
                    new_rule[key][k] = _as_name_list(v)
                    continue
                if not isinstance(v, dict):
                    new_rule[key][k] = {"name": k, "pattern": k, "score": 0.0, "description": ""}
                else:
                    if "name" not in v: new_rule[key][k]["name"] = k
                    if "pattern" not in v: new_rule[key][k]["pattern"] = k
                    if "score" not in v: new_rule[key][k]["score"] = 0.0
                    if "description" not in v: new_rule[key][k]["description"] = ""
                    try: new_rule[key][k]["score"] = float(new_rule[key][k]["score"])
                    except: new_rule[key][k]["score"] = 0.0
        elif isinstance(attr_val, list):
            for i, v in enumerate(attr_val):
                if not isinstance(v, dict):
                    new_rule[key][i] = {"name": f"item_{i}", "pattern": f"item_{i}", "score": 0.0, "description": ""}
                else:
                    if "name" not in v: new_rule[key][i]["name"] = f"item_{i}"
                    if "pattern" not in v: new_rule[key][i]["pattern"] = f"item_{i}"
                    if "score" not in v: new_rule[key][i]["score"] = 0.0
                    if "description" not in v: new_rule[key][i]["description"] = ""
                    try: new_rule[key][i]["score"] = float(new_rule[key][i]["score"])
                    except: new_rule[key][i]["score"] = 0.0
    
    for special_key in ["file_size_score", "file_size_weight", "always_override_score", "always_override_weight"]:
        if special_key not in new_rule:
            new_rule[special_key] = 0.0
        else:
            try: new_rule[special_key] = float(new_rule[special_key])
            except: new_rule[special_key] = 0.0
            
    for list_key in ["releaseGroup_blacklist", "releaseGroup_whitelist", "custom_attributes_blacklist", "custom_attributes_whitelist", "file_size_blacklist", "file_size_whitelist", "always_override_blacklist", "always_override_whitelist"]:
        if list_key not in new_rule:
            new_rule[list_key] = []
        elif not isinstance(new_rule[list_key], list):
            new_rule[list_key] = []
            
    return new_rule


def _match_any(text, patterns):
    if not patterns or not text:
        return False
    for p in patterns:
        if not p:
            continue
        try:
            if re.search(p, text, re.I):
                return True
        except re.error:
            continue
    return False


def attribute_score(rule, attr, value):
    if value is None or value == "":
        return 0.0
    
    value_str = str(value)
    
    blacklist_key = f"{attr}_blacklist"
    whitelist_key = f"{attr}_whitelist"
    
    blacklist = rule.get(blacklist_key, [])
    if isinstance(blacklist, list) and _match_any(value_str, blacklist):
        return 0.0
        
    whitelist = rule.get(whitelist_key, [])
    if isinstance(whitelist, list) and whitelist:
        if not _match_any(value_str, whitelist):
            return 0.0
            
    entries = rule.get(attr, {})
    max_score = 0.0
    
    if isinstance(entries, dict):
        for key, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            pattern = entry.get("pattern", "")
            if not pattern:
                continue
            try:
                if re.search(pattern, value_str, re.I):
                    score = float(entry.get("score", 0.0))
                    if score > max_score:
                        max_score = score
            except re.error:
                continue
    elif isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            pattern = entry.get("pattern", "")
            if not pattern:
                continue
            try:
                if re.search(pattern, value_str, re.I):
                    score = float(entry.get("score", 0.0))
                    if score > max_score:
                        max_score = score
            except re.error:
                continue
                
    return max_score


def file_size_score(rule, size, max_size):
    if max_size <= 0 or size <= 0:
        return 0.0
    base_score = float(rule.get("file_size_score", 0.0))
    return base_score * (size / max_size)


def score_item(rule, meta, peers=None):
    rule = normalize_rule(rule)
    attrs = meta.get("attrs", {})
    parts = {}
    total = 0.0
    
    for attr in ATTR_KEYS:
        value = attrs.get(attr, "")
        score = attribute_score(rule, attr, value)
        weight = float(rule.get(WEIGHT_KEYS.get(attr, attr + "_weight"), 0.0))
        parts[attr] = {
            "score": score,
            "weight": weight,
            "value": value
        }
        total += score * weight
        
    size = meta.get("size", 0)
    if peers is None:
        fs_score = float(rule.get("file_size_score", 0.0))
    else:
        max_size = 0
        for p in peers:
            s = p.get("size", 0)
            if s > max_size:
                max_size = s
        fs_score = file_size_score(rule, size, max_size)
        
    fs_weight = float(rule.get("file_size_weight", 0.0))
    total += fs_score * fs_weight
    
    override = False
    ao_weight = float(rule.get("always_override_weight", 0.0))
    if ao_weight > 0:
        ao_blacklist = rule.get("always_override_blacklist", [])
        ao_whitelist = rule.get("always_override_whitelist", [])
        
        filename = meta.get("filename", "")
        path = meta.get("path", "")
        check_text = f"{filename} {path}"
        
        hit_black = _match_any(check_text, ao_blacklist) if isinstance(ao_blacklist, list) else False
        hit_white = _match_any(check_text, ao_whitelist) if isinstance(ao_whitelist, list) and ao_whitelist else True
        
        if not hit_black and hit_white:
            override = True
            original_total = total
            total = float(rule.get("always_override_score", 999999))
            parts["always_override"] = {
                "score": float(rule.get("always_override_score", 999999)),
                "weight": ao_weight,
                "value": check_text
            }
            
    return {
        "total": total,
        "parts": parts,
        "file_size": fs_score,
        "override": override
    }

import re
import json


def version_of(rule, meta):
    if not rule.get("version_control_enabled"):
        return None

    versions = rule.get("version_control_versions", [])
    if not versions:
        return None

    attrs = meta.get("attrs", {})
    filename = meta.get("filename", "")

    best_version = None
    best_score = None

    for version in versions:
        include_conditions = version.get("include_conditions", [])
        exclude_conditions = version.get("exclude_conditions", [])
        file_name_regex = version.get("file_name_regex", "")

        include_satisfied = True
        for cond in include_conditions:
            attr_name = cond.get("attribute")
            values = cond.get("values", [])
            if not attr_name or not values:
                include_satisfied = False
                break

            attr_value = attrs.get(attr_name)
            if attr_value is None:
                include_satisfied = False
                break

            matched = False
            for val_key in values:
                # 查找规则中该属性组的条目
                attr_group = rule.get(attr_name, {})
                entry = attr_group.get(val_key)
                if not entry:
                    continue
                pattern = entry.get("pattern")
                if not pattern:
                    continue
                try:
                    if re.search(pattern, str(attr_value), re.I):
                        matched = True
                        break
                except re.error:
                    continue

            if not matched:
                include_satisfied = False
                break

        if not include_satisfied:
            continue

        exclude_satisfied = False
        for cond in exclude_conditions:
            attr_name = cond.get("attribute")
            values = cond.get("values", [])
            if not attr_name or not values:
                continue

            attr_value = attrs.get(attr_name)
            if attr_value is None:
                continue

            matched = False
            for val_key in values:
                attr_group = rule.get(attr_name, {})
                entry = attr_group.get(val_key)
                if not entry:
                    continue
                pattern = entry.get("pattern")
                if not pattern:
                    continue
                try:
                    if re.search(pattern, str(attr_value), re.I):
                        matched = True
                        break
                except re.error:
                    continue

            if matched:
                exclude_satisfied = True
                break

        if exclude_satisfied:
            continue

        if file_name_regex:
            try:
                if not re.search(file_name_regex, filename, re.I):
                    continue
            except re.error:
                continue

        # 计算得分：include 条件数最多，exclude 条件数最少
        score = (len(include_conditions), -len(exclude_conditions))
        if best_score is None or score > best_score:
            best_score = score
            best_version = version.get("name")

    return best_version


def compare_versions(rule, items):
    if not items:
        return []

    scored_items = []
    for item in items:
        item_copy = dict(item)
        score_info = score_item(rule, item_copy, peers=items)
        item_copy["score_info"] = score_info
        scored_items.append(item_copy)

    scored_items.sort(key=lambda x: (-x["score_info"]["total"], -x.get("size", 0)))
    return scored_items


def pick_best(rule, items, keep_count=1):
    if keep_count <= 0:
        return []
    result = compare_versions(rule, items)
    return result[:keep_count]


def group_by_media(items, rule=None):
    if rule is None:
        rule = normalize_rule(None)

    groups = {}
    for item in items:
        info = item.get("info", {})
        title = info.get("title")
        year = info.get("year")
        season = info.get("season")

        if not title:
            name = item.get("name", "")
            # 取第一个数字前部分
            match = re.match(r"^(.*?)(\d|$)", name)
            if match:
                title = match.group(1).strip()
            else:
                title = name.strip()

        if title is None:
            title = ""
        if year is None:
            year = ""
        if season is None:
            season = ""

        # 归一化：去空格转小写
        norm_title = re.sub(r"\s+", "", str(title)).lower()
        norm_year = str(year).strip().lower()
        norm_season = str(season).strip().lower()

        gkey = f"{norm_title}|{norm_year}|{norm_season}"

        if gkey not in groups:
            groups[gkey] = {
                "title": title,
                "year": year,
                "season": season,
                "versions": []
            }

        groups[gkey]["versions"].append(item)

    # 只保留版本数 > 1 的分组，并排序
    result = {}
    for gkey, group in groups.items():
        versions = group["versions"]
        if len(versions) > 1:
            sorted_versions = compare_versions(rule, versions)
            result[gkey] = {
                "title": group["title"],
                "year": group["year"],
                "season": group["season"],
                "versions": sorted_versions
            }

    return result


def import_rule_json(text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("JSON 内容为空")

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise ValueError("JSON 格式不正确")

    rule = normalize_rule(data)
    if "name" not in rule or not rule["name"]:
        rule["name"] = "导入配置"

    return rule


def export_rule_json(rule):
    normalized = normalize_rule(rule)
    return json.dumps(normalized, ensure_ascii=False, indent=2)


_AUGMENT_RULES = [
    (r"\bDoVi\b|\bDV\b|Dolby[ .]?Vision", "Dolby Vision"),
    (r"\bBluRay\b", "Blu-Ray"),
    (r"\bx265\b|\bHEVC\b", "H.265"),
    (r"\bx264\b", "H.264"),
    (r"\bTrueHD[ .]?Atmos\b", "TrueHD Dolby Atmos"),
    (r"\bDTS[ ._-]?HD[ ._-]?MA\b", "DTS-HD MA"),
    (r"\bDTS[ ._-]?X\b", "DTS:X"),
    (r"\bDDP[ ._-]?5[ ._-]?1\b|DDP.*Atmos", "DDP Dolby Atmos"),
    (r"\bAC[ ._-]?3\b", "Dolby Digital"),
    (r"\bWEB[ ._-]?DL\b", "WEB-DL"),
    (r"\bWEB[ ._-]?Rip\b", "WEBRip"),
    (r"\bUltra[ .]?HD\b", "Ultra HD"),
    (r"\bSDR\b", "SDR"),
    (r"\bHLG\b", "HLG"),
    (r"\bHDR10\+", "HDR10+"),
    (r"\b10[ ._-]?bit\b", "10-bit"),
    (r"\b12[ ._-]?bit\b", "12-bit"),
    (r"\b8[ ._-]?bit\b", "8-bit"),
]


def augment_text(text):
    """把文件名里的常见缩写展开成标准写法，便于规则里的 pattern 命中。

    例：DV -> Dolby Vision、BluRay -> Blu-Ray、x265 -> H.265。
    """
    if not text:
        return ""
    out = str(text)
    for pat, rep in _AUGMENT_RULES:
        try:
            out = re.sub(pat, rep, out, flags=re.I)
        except re.error:
            continue
    return out


def build_attrs(filename, path=""):
    """给评分函数准备属性字典：所有维度都用（标准化后的）文件名去匹配。

    权重规则里的 pattern 就是针对文件名/元数据文本匹配的。
    """
    text = augment_text(filename)
    if path:
        text = text + " " + str(path)
    return {attr: text for attr in ATTR_KEYS}


# 维度键 -> 权重键。custom_attributes 的权重键叫 custom_weight，是特例。
def weight_key_of(attr_key):
    if attr_key in WEIGHT_KEYS:
        return WEIGHT_KEYS[attr_key]
    return attr_key + "_weight"


def _entry_of(name, pattern, score, description):
    try:
        sc = float(score)
    except (TypeError, ValueError):
        sc = 0.0
    return {"name": name or "", "pattern": pattern or "",
            "score": sc, "description": description or ""}


def _entries_from_rule_value(attr_key, value):
    """把 rule 里某个维度的原始值转成 {条目key: 条目dict}。

    对象维度是 {key: {name,pattern,score,description}}；
    数组维度（releaseGroup / custom_attributes）是 [{...}]，用 name 当键。
    """
    out = {}
    if isinstance(value, dict):
        for k, v in value.items():
            if k in ("blacklist", "whitelist"):
                continue
            if isinstance(v, dict):
                out[k] = _entry_of(v.get("name", k), v.get("pattern", k),
                                   v.get("score"), v.get("description"))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            if not isinstance(v, dict):
                continue
            key = str(v.get("name") or ("item_%d" % i))
            out[key] = _entry_of(v.get("name", key), v.get("pattern", key),
                                 v.get("score"), v.get("description"))
    return out


def normalize_weight_config(cfg):
    """把权重表配置规范化，返回 {'dims': {...}, 'keep_count': int}。

    dims 结构（给前端渲染/编辑用，与 rule 同源但更扁平）：
        {维度键: {
            'weight':    维度权重(float),
            'entries':   {条目key: {'name','pattern','score','description'}},
            'blacklist': [...],
            'whitelist': [...]
        }}

    cfg 可以是 None / dict。dim 数据来源优先级：
        cfg['dims'][维度键] > cfg[维度键] > DEFAULT_RULE 的默认值。
    维度键支持两种写法：
        {'entries': {...}, 'weight': n, 'blacklist': [...], 'whitelist': [...]}
        直接就是条目表 {条目key: {...}}
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    src = cfg.get("dims")
    if not isinstance(src, dict):
        src = {}
    direct = {k: v for k, v in cfg.items() if k in ATTR_KEYS}

    dims = {}
    for key in ATTR_KEYS:
        wkey = weight_key_of(key)
        base = DEFAULT_RULE.get(key)
        entries = _entries_from_rule_value(key, base)
        bl = list(base.get("blacklist") or []) if isinstance(base, dict) else []
        wl = list(base.get("whitelist") or []) if isinstance(base, dict) else []
        try:
            weight = float(DEFAULT_RULE.get(wkey) or 0.0)
        except (TypeError, ValueError):
            weight = 0.0

        raw = src.get(key, direct.get(key))
        if isinstance(raw, dict):
            if isinstance(raw.get("entries"), dict):
                new_entries = _entries_from_rule_value(key, raw.get("entries"))
                if new_entries:
                    entries = new_entries
                bl = list(raw.get("blacklist") or bl)
                wl = list(raw.get("whitelist") or wl)
                try:
                    weight = float(raw.get("weight", weight))
                except (TypeError, ValueError):
                    pass
            else:
                new_entries = _entries_from_rule_value(key, raw)
                if new_entries:
                    entries = new_entries
                if isinstance(raw.get("blacklist"), list):
                    bl = list(raw.get("blacklist"))
                if isinstance(raw.get("whitelist"), list):
                    wl = list(raw.get("whitelist"))
        elif isinstance(raw, list):
            new_entries = _entries_from_rule_value(key, raw)
            if new_entries:
                entries = new_entries

        if wkey in src:
            try:
                weight = float(src[wkey])
            except (TypeError, ValueError):
                pass
        elif wkey in cfg:
            try:
                weight = float(cfg[wkey])
            except (TypeError, ValueError):
                pass

        dims[key] = {"weight": weight, "entries": entries,
                     "blacklist": bl, "whitelist": wl}

    try:
        keep_count = int(cfg.get("keep_count") or 1)
    except (TypeError, ValueError):
        keep_count = 1
    if keep_count < 1:
        keep_count = 1
    return {"dims": dims, "keep_count": keep_count}
