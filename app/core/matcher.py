"""标题归一化 + 番号提取 + 三级匹配。

设计依据（2026-10-18 实测抽样）：
- 电影/剧集/纪录片/演唱会：Tmdb ID 覆盖近乎 100%，优先用 ID 精确匹配。
- AV 库：只有 MetaTube ID，且 Name 常带日期前缀，番号藏在中间 → 必须提取番号当主键。
"""
import difflib
import re
import unicodedata

# 常见繁→简 高频字（只覆盖影响匹配的字，不求完整；完整转换交给 zhconv/opencc 若可用）
_FJ = str.maketrans(
    "國與為這個來時說會對後發現點麼樣開關無線視聽聲畫專題記億萬歲東車馬長門問題"
    "學愛戀願歡樂懼驚險殺戰鬥爭奪圖書館寫讀譯藝術戲劇綜兒童觀眾熱門經典續集"
    "級別總體統計數據資訊網絡結構團隊製作發行動漫創作者導演演員配音員鏡頭",
    "国与为这个来时说会对后发现点么样开关无线视听声画专题记亿万岁东车马长门问题"
    "学爱恋愿欢乐惧惊险杀战斗争夺图书馆写读译艺术戏剧综儿童观众热门经典续集"
    "级别总体统计数据资讯网络结构团队制作发行动漫创作者导演演员配音员镜头",
)

ARTICLES = re.compile(r"^(the|a|an)\s+", re.I)
SEASON = re.compile(r"\s*(第[一二三四五六七八九十\d]+季|season\s*\d+|s\d{1,2})$", re.I)
BRACKETS = re.compile(r"[\(\[【（][^\)\]】）]{0,40}[\)\]】）]")
NON_WORD = re.compile(r"[^\w\u4e00-\u9fff]+", re.U)

# 番号模式，按优先级（高 → 低）
AV_PATTERNS = [
    re.compile(r"\bFC2[-_ ]?PPV[-_ ]?(\d{5,8})\b", re.I),
    re.compile(r"\b(\d{6})[-_](\d{3})\b"),                              # Caribbeancom 123456-789
    re.compile(r"\b([0-9]{0,6}[A-Z]{2,6})[-_ ]?(\d{2,5})\b"),           # SSIS-378 / 259LUXU-1678 / 3DSVR-1816
    re.compile(r"\b(HEYZO|CARIB|1PONDO|10MUSUME|C\d{4})[-_ ]?(\d{3,7})\b", re.I),
]
NOT_CODES = {"1080P", "2160P", "4K", "8K", "HD", "VR", "MP4", "MKV", "AV", "HDTV", "WEB", "DL", "ISO"}
DATE_PREFIX = re.compile(r"\b\d{1,2}[-/.]\d{1,2}[-/. ]")


def normalize(text):
    """标题归一化：繁→简、全角→半角、去标点、去冠词、去季集后缀。"""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text))
    t = t.translate(_FJ)
    t = BRACKETS.sub(" ", t)
    t = SEASON.sub("", t.strip())
    t = t.lower().strip()
    t = ARTICLES.sub("", t)
    t = NON_WORD.sub("", t)
    return t


def extract_av_code(*texts):
    """从片名/路径里提取 AV 番号，返回归一化番号（仅字母数字，大写）。

    注意：中文字符在正则里也算 \\w，直接对原串用 \\b 会失效
    （"SSIS-378新人" 里 378 后面接汉字，\\b 不成立）→ 先把非 ASCII 抹成空格。
    """
    for raw in texts:
        if not raw:
            continue
        t = unicodedata.normalize("NFKC", str(raw)).upper()
        t = re.sub(r"[^\x00-\x7F]+", " ", t)      # 中日文 → 空格
        t = t.replace("_", "-")
        t = DATE_PREFIX.sub(" ", t)               # 抹掉 "04-08" 这类日期前缀
        for i, pat in enumerate(AV_PATTERNS):
            for m in pat.finditer(t):
                if i == 0:
                    return "FC2PPV" + m.group(1)
                body = "".join(g for g in m.groups() if g)
                code = re.sub(r"[^A-Z0-9]", "", body)
                if code in NOT_CODES or len(code) < 4:
                    continue
                return code
    return ""


_LEAD_NUM = re.compile(r"^\d{1,4}(?=[A-Z]{2,6}\d{2,5}$)")

# 115/PT 的发布名专用清理：先砍掉年份、分辨率、编码等噪音再归一化，
# 否则 "沙丘2.2023.2160p.WEB-DL.mkv" 会归一成 "沙丘220232160pwebdl"，跟发行片永远对不上。
_JUNK = re.compile(
    r"(?i)[\.\s_\-\[\]]+(?:2160p|1080p|720p|480p|4k|8k|web[\-.]?dl|webrip|web|bluray|blu\-ray|"
    r"bdrip|hdtv|dvdrip|remux|x264|x265|h\.?264|h\.?265|hevc|avc|aac|ac3|eac3|dts[\-.](?:hd|ma)?|"
    r"truehd|atmos|hdr10\+?|hdr|dovi|dv|10bit|8bit|proper|repack|complete|internal)\b")
_YEAR_IN = re.compile(r"(?:^|[\.\s_\(\[])((?:19|20)\d{2})(?:[\.\s_\)\]]|$)")
_EPISODE = re.compile(r"(?i)[\.\s_\-]*s\d{1,2}(?:e\d{1,3})?")


def media_norm(name):
    """把网盘/PT 的发布名归一成可用于比对的标题。"""
    if not name:
        return ""
    t = str(name)
    t = re.sub(r"\.[A-Za-z0-9]{2,4}$", " ", t)          # 去扩展名
    t = BRACKETS.sub(" ", t)                             # 去 [xxx] (xxx)
    m = _YEAR_IN.search(t)
    if m:
        t = t[:m.start()]
    t = _JUNK.sub(" ", t)
    t = _EPISODE.sub(" ", t)
    return normalize(t)


def av_code_variants(code):
    """同一番号的多种写法，任一对上即视为同一部。

    例：014NITR025 → {014NITR025, NITR025}；259LUXU1678 → {259LUXU1678, LUXU1678}
    """
    if not code:
        return []
    out = {code}
    m = _LEAD_NUM.match(code)
    if m and len(code) - m.end() >= 5:
        out.add(code[m.end():])
    return list(out)


# ---------------- 番号规范化（精准比对用） ----------------
# JavDB 写 SSIS-378，本地可能是 SSIS378 / SSIS-0378 / SSIS-378-C（中字）/
# 259LUXU-1678 / [JDXA] SSIS-378；只看「字母前缀 + 数字」，去掉前导零和后缀，
# 两边才能落到同一个键上。实测库里 128 条 Emby 元数据把番号挂错（路径是 MDSR-0007，
# Name 却是 AARM-110），所以索引要同时吃 av_code、路径、文件名三处。
_CODE_SHAPE = re.compile(r"^(\d*)([A-Z]+)(\d+)([A-Z]*)$")
_CODE_TAIL = re.compile(r"(?:UNCENSORED|LEAK|CH|UC|C|U)$")
_LEAD_CODE = re.compile(
    r"^\s*(?:\[[^\]]{0,24}\]\s*)?(?:[0-9]{0,6}[A-Za-z]{2,6}[-_ ]?\d{2,5}[-_ ]?[A-Za-z]{0,3}[-_ ]*)")


def av_code_keys(code):
    """把一个番号摊成一组可比较的键：字母前缀 + 去前导零的数字（+ 可选后缀）。

    SSIS-378 → {SSIS378}；SSIS-0378 → {SSIS378}；SSIS-378-C → {SSIS378, SSIS378C}
    259LUXU-1678 → {LUXU1678}（前面的 259 只是厂牌序号，去掉才跟 JavDB 对得上）
    认不出番号形状时返回空集合，调用方直接跳过。
    """
    s = re.sub(r"[^A-Z0-9]", "", (code or "").upper())
    m = _CODE_SHAPE.match(s)
    if not m:
        return set()
    pre, num, tail = m.group(2), m.group(3), m.group(4)
    if not num:
        return set()
    n = str(int(num))
    keys = {pre + n}
    if tail:
        keys.add(pre + n + tail)
        t = _CODE_TAIL.sub("", tail)
        if t:
            keys.add(pre + n + t)
    return keys


def code_prefix(code):
    """取番号的字母前缀（SSIS-378 → SSIS），用于「同厂牌 + 同标题」这类弱比对。"""
    m = re.match(r"^(\d*)([A-Z]+)", re.sub(r"[^A-Z0-9]", "", (code or "").upper()))
    return m.group(2) if m else ""


def av_title_norm(name):
    """AV 片名归一化：先砍掉开头的番号（本地名常是「ABF-306 日文标题」）。"""
    return normalize(_LEAD_CODE.sub("", name or ""))


# ---------------- 里番标题归一化 ----------------
# 本地命名：[制作组] 日文标题 [番号] ＃1 / 第2幕 / vol.3 / THE ANIMATION
# 发行片名：AniList 的纯系列名，没有集数、没有制作组 → 归一后要么相等、要么互为子串
_H_EP = re.compile(r"(第[一二三四五六七八九十\d]{1,3}[幕話话集部]|#\d{1,3}|ep?\.?\d{1,3}(?:v\d)?|vol\.?\d{1,2})")
_H_HEAD = re.compile(r"^(?:ova|oad|theanimation|animation)")


def hentai_clean(text):
    """里番标题归一：去制作组方括号、去 OVA/THE ANIMATION、去集数标记。"""
    t = normalize(text)
    t = _H_HEAD.sub("", t)
    t = _H_EP.sub("", t)
    t = re.sub(r"(?:theanimation)$", "", t)
    t2 = re.sub(r"\d{1,3}$", "", t)
    return t2 if len(t2) >= 4 else t


def hentai_score(cn, on):
    """里番标题相似度分档：100 完全相同 / 96·90 互为子串 / 88·80 高度相似 / 0 不相关。

    短名阈值为 4：日系系列名常常正好 4 个字（黒の教室 / 姉汁 / 催眠学園），
    卡在 5 会把这类「本地明显有」的全判成缺（实测漏 100+ 条）。
    """
    if not cn or not on or len(cn) < 4 or len(on) < 4:
        return 0
    if cn == on:
        return 100
    if cn in on or on in cn:
        short = min(len(cn), len(on))
        if short >= 7:
            return 96
        if short >= 4:
            return 90
        return 0
    if len(cn) < 8 or len(on) < 8:
        return 0
    r = difflib.SequenceMatcher(None, cn, on).ratio()
    if r >= 0.93:
        return 88
    if r >= 0.86:
        return 80
    return 0


# 本地里番名形如「[制作组] 系列名 第N话「副标题」[番号]」——系列名永远在最前面。
# 发行片名（AniList 纯系列名）常常和本地名差一两个字（白川姉妹 / 白川三姉妹、カナン / ましかるカナン），
# 整串比相似度会被后面长长的副标题拉垮 → 单独比「前 len(cn) 个字」。
_H_LEAD = re.compile(r"^\s*[\[\(【][^\]\)】]{0,40}[\]\)】]\s*")
_H_KEEP = re.compile(r"[^0-9a-z\u3040-\u30ff\u4e00-\u9fff]+")


def hentai_raw(text):
    """里番本地名的「开头原文」归一：只去掉开头的 [制作组]，保留副标题与顺序。"""
    t = unicodedata.normalize("NFKC", str(text or ""))
    t = _H_LEAD.sub("", t)
    return _H_KEEP.sub("", t.lower())


def hentai_prefix_score(cn, raw):
    """发行片系列名 vs 本地名开头；89 = 几乎一致（算有），82 = 差一两个字（疑似）。"""
    if not cn or not raw or len(cn) < 6:
        return 0
    seg = raw[:len(cn)]
    if len(seg) < len(cn) - 2:
        return 0
    r = difflib.SequenceMatcher(None, cn, seg).ratio()
    if r >= 0.92:
        return 89
    if r >= 0.86:
        return 82
    return 0


def year_of(item):
    y = item.get("year") or item.get("ProductionYear")
    if y:
        try:
            return int(str(y)[:4])
        except ValueError:
            pass
    d = item.get("premiere") or item.get("PremiereDate") or item.get("release_date") or ""
    m = re.match(r"(\d{4})", str(d))
    return int(m.group(1)) if m else None


def score_pair(cat, own):
    """给一条目标 vs 一条库存打分。0 表示不可能，>=1 表示确定。"""
    score = 0.0
    ct, ot = cat.get("tmdb_id"), own.get("tmdb_id")
    if ct and ot and str(ct) == str(ot):
        return 100.0
    ci, oi = cat.get("imdb_id"), own.get("imdb_id")
    if ci and oi and str(ci).lower() == str(oi).lower():
        return 100.0
    cv, ov = cat.get("tvdb_id"), own.get("tvdb_id")
    if cv and ov and str(cv) == str(ov):
        return 100.0

    cy, oy = year_of(cat), year_of(own)
    yr_ok = bool(cy and oy and abs(cy - oy) <= 1)
    if cy and oy and abs(cy - oy) > 2:
        return 0.0

    cn, on = cat.get("norm") or normalize(cat.get("title")), own.get("norm") or normalize(own.get("name"))
    co = normalize(cat.get("original_title"))
    oo = normalize(own.get("original_title"))

    if cn and on and cn == on:
        score = 70.0
        if not (cy and oy):
            # 双方都没有年份（短剧、欧美 AV 站片常见）→ 无矛盾信息，视为确定
            score = 88.0 if (not cy and not oy) else 80.0
    elif cn and on and (cn in on or on in cn) and min(len(cn), len(on)) >= 4:
        score = 55.0
    elif co and oo and co == oo:
        score = 65.0
    elif co and on and co == on:
        score = 60.0
    elif cn and oo and cn == oo:
        score = 60.0

    if score and yr_ok:
        score += 25
    elif score and cy and oy:
        score -= 15
    return score


def classify(cat, candidates):
    """三级判定：owned(确定) / likely(疑似) / missing(缺) / unmatchable(无法比对)。

    阈值说明：85 分对应"标题完全相等 + 双方均无年份"，这类应算确定；
    50~85 是包含关系或单边年份，交给人工确认。
    """
    best, best_id = 0.0, None
    for own in candidates:
        s = score_pair(cat, own)
        if s > best:
            best, best_id = s, own.get("emby_id")
    if best >= 85:
        return "owned", best_id, best
    if best >= 50:
        return "likely", best_id, best
    return "missing", None, best or None


def is_unmatchable(item):
    """名字归一化后为空（如 "[FemdomEmpire.com] -"），无法参与标题比对。"""
    return not (item.get("norm") or normalize(item.get("title") or item.get("name")))
