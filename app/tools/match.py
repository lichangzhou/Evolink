"""把目标目录与 Emby 库存做匹配，写回 catalog 的 match_* 字段。

比对分成四条轨，按「确定性」从高到低走：
1. AV 番号轨   —— 番号归一成「字母前缀 + 去零数字」后精确相等（两边命名差异最大，但归一后完全可靠）
2. ID 轨       —— tmdb / imdb / tvdb 精确相等
3. 里番标题轨  —— 去掉制作组、OVA、第N幕后再比：相等 > 互为子串 > 高度相似
4. 标题轨      —— 电影/剧集等按归一化标题 + 年份
AV 还有一条弱轨：同厂牌 + 标题几乎完全一致（本地元数据把番号挂错、或合集重发时才会命中），
只标「疑似」，不冒充「已在库」。

比对范围（1022 修正）：默认**严格按分类勾选的库**——「里番」只跟里番桶勾选的库比，
「剧集」只跟剧集各桶勾选的库比，没勾的库一律不参与。设置里打开「跨库兜底」
(match_all_libs) 才回到旧行为：片子放在任何库都算「已在库」。
"""
import difflib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import config, db, matcher


def _build_index(rows, allow=None, hentai_allow=None):
    """把 owned 行建成一套索引。

    allow        —— 允许参与的文件所属库集合；None 表示不限（跨库兜底）
    hentai_allow —— 里番标题倒排只索引这些库；None 表示不建里番索引
    """
    by_tmdb, by_imdb, by_tvdb, by_av, by_norm = {}, {}, {}, {}, {}
    by_avtitle = {}
    hentai = {}
    for d in rows:
        lib = d.get("library") or ""
        if allow is not None and lib not in allow:
            continue
        if d.get("tmdb_id"):
            by_tmdb.setdefault(str(d["tmdb_id"]), d)
        if d.get("imdb_id"):
            by_imdb.setdefault(str(d["imdb_id"]).lower(), d)
        if d.get("tvdb_id"):
            by_tvdb.setdefault(str(d["tvdb_id"]), d)

        # 番号有三个来源：库里存的、路径里提取的、文件名里提取的。
        # Emby 元数据有时张冠李戴（路径 MDSR-0007，Name 却是 AARM-110），只认一个必然漏。
        codes = (d.get("av_code"), matcher.extract_av_code(d.get("path") or ""),
                 matcher.extract_av_code(d.get("name") or ""))
        for cd in codes:
            for k in matcher.av_code_keys(cd):
                by_av.setdefault(k, d)

        if d.get("norm"):
            by_norm.setdefault(d["norm"], []).append(d)

        # 同厂牌 + 同标题的弱轨（只在番号对不上时兜底）
        t = matcher.av_title_norm(d.get("name") or "") or matcher.normalize(d.get("original_title") or "")
        p = matcher.code_prefix(d.get("av_code") or "")
        if p and len(t) >= 8:
            by_avtitle.setdefault(p, []).append((t, d))

        if hentai_allow is not None and lib in hentai_allow:
            # 里番：本地名是「系列名 ～副标题～ 第N幕」，发行片里只有系列名
            # → 归一后做 2-gram 倒排，再用「相等 / 互为子串 / 高相似」判
            seen = set()
            for s in (d.get("name"), d.get("original_title")):
                n = matcher.hentai_clean(s)
                if len(n) < 4 or n in seen:
                    continue
                seen.add(n)
                raw = matcher.hentai_raw(s)
                for i in range(len(n) - 1):
                    hentai.setdefault(n[i:i + 2], []).append((n, raw, d["emby_id"]))
    return {"tmdb": by_tmdb, "imdb": by_imdb, "tvdb": by_tvdb, "av": by_av,
            "avtitle": by_avtitle, "norm": by_norm, "hentai": hentai}


def load_owned_index(c, only_libs=None):
    """按分类建索引：每个分类只收「该分类勾选的库」里的本地条目。

    严格模式（默认）下，里番的索引里只有里番桶勾选的库（如 Adult-动画），
    AV 的索引里只有 AV 桶勾选的库（如 Adult-亚洲 / Adult-亚洲无码），
    所以没勾的库（短剧、Adult-欧美、三级片……）永远不会被比对到。
    打开「跨库兜底」后 8 个分类共用一套全库索引，恢复旧行为。

    only_libs：用户在「比对」面板里勾选的 Emby 库。非空时每个分类的索引
    再与该集合取交集 —— 只勾「欧美电影」就只拿它的库存去比电影清单。
    """
    cfg = config.load()
    cross = bool(cfg.get("match_all_libs", False))
    rows = [dict(r) for r in c.execute("SELECT * FROM owned")]
    only = {l for l in (only_libs or []) if l}

    if cross and not only:
        all_idx = _build_index(rows, None, set(db.libs_for("hentai")) | set(db.libs_for_cat("动画")))
        return {cat: all_idx for cat in db.CAT_BUCKETS}

    per = {}
    for cat in db.CAT_BUCKETS:
        allow = set(db.libs_for_cat(cat))
        if only:
            allow &= only
            if not allow:
                per[cat] = _build_index([], set(), None)
                continue
        h_allow = set(db.libs_for("hentai")) if cat == "里番" else None
        if only and h_allow is not None:
            h_allow &= only
        per[cat] = _build_index(rows, allow, h_allow)
    return per


def full_index(c):
    """兜底：catalog 里出现未知分类时用的全库索引。"""
    rows = [dict(r) for r in c.execute("SELECT * FROM owned")]
    return _build_index(rows, None, None)


def hentai_hit(cn, index):
    """里番标题判定，返回 (emby_id, score)。

    发行片「エルフィーナ」 vs 本地「[ピンクパイナップル] エルフィーナ～淫夜へ…～ 第一幕」：
    归一成本地「エルフィーナ淫夜へ」后互为子串 → 96 分（本地是它的某一集，算有）。
    子串对不上时再比「本地名开头」——系列名总在开头，副标题再长也不影响。
    """
    if not cn or len(cn) < 4:
        return None, 0.0
    pool = set()
    for i in range(len(cn) - 1):
        pool.update(index.get(cn[i:i + 2], ()))
    best, bid = 0, None
    for on, raw, eid in pool:
        s = matcher.hentai_score(cn, on)
        if s < 88:
            s = max(s, matcher.hentai_prefix_score(cn, raw))
        if s > best:
            best, bid = s, eid
            if s >= 100:
                break
    return bid, float(best)


def av_title_hit(ct, cands, own_code):
    """同厂牌 + 标题几乎一致 → 疑似同一部（返回 (emby_id, score, 本地番号)）。"""
    if not ct or len(ct) < 8:
        return None, 0.0, ""
    best, bid, bcode = 0.0, None, ""
    for on, d in cands:
        sm = difflib.SequenceMatcher(None, ct, on)
        if sm.quick_ratio() < 0.9:
            continue
        r = sm.ratio()
        if r > best:
            best, bid, bcode = r, d["emby_id"], d.get("av_code") or ""
    if best >= 0.93:
        return bid, 78.0, bcode
    return None, 0.0, ""


def libs_to_cats(libs):
    """勾选的 Emby 库 → 要比对的 catalog 分类集合。

    先看库在不在里番桶（hentai）里：Adult-动画 这类库按库名关键词会落进 AV，
    但它装的是里番，要比的是「里番」清单，不是整个 AV 清单。
    """
    from tools import sync_emby          # 延迟导入：避免与 core 包循环
    h_libs = set(db.libs_for("hentai"))
    cats = set()
    for l in (libs or []):
        if not l:
            continue
        cats.add("里番" if l in h_libs else sync_emby.category_of(l))
    return cats


def match_all(verbose=True, only_cat=None, rescan=True, only_libs=None, progress=None):
    """重跑比对。rescan=True 时连「已入库」的也重算（换了匹配算法必须重算）。

    only_libs：只比对勾选的 Emby 库（并集语义一次算清，避免多库互相覆盖）。
        catalog 取这些库所属分类的并集，owned 索引只收这些库的条目。
    progress：可选回调 progress(done, total, cur_cat)，每 500 行 + 末行触发，
        供「比对」面板画每个库的进度条。
    """
    t0 = time.time()
    c = db.init()
    only = [l for l in (only_libs or []) if l]
    idxmap = load_owned_index(c, only)
    if only_cat:
        rows = c.execute("SELECT * FROM catalog WHERE category=?", (only_cat,)).fetchall()
    elif only:
        cats = sorted(libs_to_cats(only))
        qs = ",".join("?" * len(cats))
        rows = c.execute(f"SELECT * FROM catalog WHERE category IN ({qs})", cats).fetchall()
    elif rescan:
        rows = c.execute("SELECT * FROM catalog").fetchall()
    else:
        rows = c.execute("SELECT * FROM catalog WHERE match_status IS NULL OR match_status='missing'").fetchall()
        if not rows:
            rows = c.execute("SELECT * FROM catalog").fetchall()
    counts = {"owned": 0, "likely": 0, "missing": 0, "unmatchable": 0}
    how = {}
    upd = []
    n_total = len(rows)
    for i, r in enumerate(rows):
        d = dict(r)
        cat = (d.get("category") or "").strip()
        if cat in idxmap:
            idx = idxmap[cat]
        else:
            idx = idxmap.get("*")
            if idx is None:
                idx = idxmap["*"] = full_index(c)
        status, eid, score, why = "missing", None, 0.0, ""

        # 0) 里番轨：按归一化标题（相等 / 子串 / 高相似）
        if cat == "里番" and idx["hentai"]:
            cn = matcher.hentai_clean(d.get("title") or "")
            e_id, sc = hentai_hit(cn, idx["hentai"])
            if sc >= 88:
                status, eid, score, why = "owned", e_id, sc, "里番标题"
            elif sc >= 78:
                status, eid, score, why = "likely", e_id, sc, "里番标题(近似)"

        # 1) AV 番号轨
        if status == "missing" and d["av_code"]:
            hit = None
            for v in matcher.av_code_keys(d["av_code"]):
                hit = idx["av"].get(v)
                if hit:
                    break
            if hit:
                status, eid, score, why = "owned", hit["emby_id"], 100.0, "番号"

        # 1b) AV 弱轨：同一厂牌 + 标题几乎一致（本地番号被元数据挂错 / 合集重发）
        if status == "missing" and d["av_code"]:
            p = matcher.code_prefix(d["av_code"])
            ct = matcher.normalize(d.get("original_title") or d.get("title") or "")
            e_id, sc, bcode = av_title_hit(ct, idx["avtitle"].get(p) or [], d["av_code"])
            if e_id:
                status, eid, score = "likely", e_id, sc
                why = f"同厂牌同标题（本地 {bcode}）"

        # 2) ID 精确轨
        if status == "missing":
            for key, field in (("tmdb", "tmdb_id"), ("imdb", "imdb_id"), ("tvdb", "tvdb_id")):
                v = d.get(field)
                if not v:
                    continue
                v = str(v).lower() if key == "imdb" else str(v)
                hit = idx[key].get(v)
                if hit:
                    status, eid, score, why = "owned", hit["emby_id"], 100.0, key.upper()
                    break

        # 3) 标题轨（同 norm 下再比年份）
        if status == "missing":
            if not d["av_code"] and matcher.is_unmatchable(d):
                status, eid, score = "unmatchable", None, 0.0
            else:
                cands = idx["norm"].get(d["norm"] or "", [])
                st, e_id, sc = matcher.classify(d, cands)
                status, eid, score = st, e_id, sc or 0.0
                if st in ("owned", "likely"):
                    why = "标题"

        counts[status] += 1
        if why:
            # 「同厂牌同标题（本地 JUR135）」这类按前缀归并，否则依据统计会被几十条明细刷屏
            k = why.split("（")[0]
            how[k] = how.get(k, 0) + 1
        upd.append((status, eid, score, why, db.now(), d["cid"]))
        if progress is not None and (i % 500 == 0 or i == n_total - 1):
            try:
                progress(i + 1, n_total, cat)
            except Exception:
                pass

    c.executemany("UPDATE catalog SET match_status=?, match_emby_id=?, match_score=?, "
                  "match_how=?, matched_at=? WHERE cid=?", upd)
    c.commit()
    # 大量 UPDATE 之后把 WAL 压回主库：否则 -wal 涨到近百 MB，
    # 之后每个读查询都要先合并 WAL，stats / 仪表盘会被拖到十几秒。
    try:
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        pass
    if verbose:
        total = sum(counts.values())
        print(f"匹配完成 {total} 条，耗时 {time.time()-t0:.1f}s")
        for k, v in counts.items():
            print(f"  {k:8s} {v:6d}  ({v/max(total,1)*100:.1f}%)")
        if how:
            print("  依据：" + "　".join(f"{k} {v}" for k, v in sorted(how.items(), key=lambda x: -x[1])))
    if not only_cat:
        db.set_meta("matched_at", db.now())
    return counts


def match_hentai(verbose=True):
    """只重跑里番发行片的比对（拉完 AniList 清单后调用）。"""
    return match_all(verbose=verbose, only_cat="里番")


if __name__ == "__main__":
    match_all()
