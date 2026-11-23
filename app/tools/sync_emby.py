"""把 Emby 现有库全量索引到本地 SQLite（只读，不修改 Emby）。

用法: python3 -m tools.sync_emby [--full]
"""
import json
import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from core import db, emby, matcher

# 库名 → 归一化类别
CATEGORY_MAP = [
    ("电影", "电影"), ("剧", "剧集"), ("动漫", "动画"), ("国漫", "动画"),
    ("纪录片", "纪录片"), ("综艺", "综艺"), ("演唱会", "演唱会"),
    ("三级", "AV"), ("Adult", "AV"), ("AV", "AV"), ("有码", "AV"),
    ("无码", "AV"), ("成人", "AV"), ("短剧", "短剧"),
]


def category_of(lib_name):
    for k, v in CATEGORY_MAP:
        if k in lib_name:
            return v
    return "其他"


def _prune_library(c, lib_id, lib_name, pulled_ids):
    """删除某库中本次未拉到的 owned 行（Emby 侧已删/移出该库）。
    只在整库拉取成功（无异常）后调用——半路失败的库不删，防误删。
    同时回退这些 emby_id 对应的 catalog 匹配状态，避免「假拥有」（删了还显示已在库）。"""
    c.execute("CREATE TEMP TABLE IF NOT EXISTS _pulled(emby_id TEXT PRIMARY KEY)")
    c.execute("DELETE FROM _pulled")
    c.executemany("INSERT OR IGNORE INTO _pulled VALUES (?)", [(i,) for i in pulled_ids])
    # 先抓出要删的 emby_id，待会回退 catalog 匹配状态
    gone = [r[0] for r in c.execute(
        "SELECT emby_id FROM owned WHERE lib_id=? AND emby_id NOT IN (SELECT emby_id FROM _pulled)",
        (lib_id,))]
    n = c.execute("DELETE FROM owned WHERE lib_id=? AND emby_id NOT IN (SELECT emby_id FROM _pulled)",
                  (lib_id,)).rowcount
    if gone:
        # 这些片子在 Emby 侧已经没了 → 取消它们在 catalog 上的「已在库」标记
        qmarks = ",".join("?" * len(gone))
        c.execute(
            f"UPDATE catalog SET match_status='missing', match_emby_id=NULL, match_score=0 "
            f"WHERE match_emby_id IN ({qmarks})", gone)
    c.execute("DROP TABLE _pulled")
    return n


def main():
    t0 = time.time()
    c = db.init()
    libs = emby.libraries()
    print(f"发现 {len(libs)} 个库")
    total = 0
    for lib in libs:
        if lib["type"] in ("livetv", "playlists", "boxsets"):
            print(f"跳过 {lib['name']} ({lib['type']})")
            continue
        cat = category_of(lib["name"])
        n = 0
        rows = []
        pulled_ids = []
        for it in emby.iter_items(lib["id"]):
            pi = {k.lower(): v for k, v in (it.get("ProviderIds") or {}).items()}
            name = it.get("Name") or ""
            orig = it.get("OriginalTitle") or ""
            path = it.get("Path") or ""
            tags = it.get("ImageTags") or {}
            rows.append((
                it["Id"], name, orig,
                matcher.year_of(it), it.get("PremiereDate") or "",
                it.get("Type") or "", lib["id"], lib["name"],
                pi.get("tmdb") or pi.get("tmdbcol") or "", pi.get("imdb") or "", pi.get("tvdb") or "",
                1 if tags.get("Primary") else 0, tags.get("Primary") or "",
                path,
                matcher.normalize(name),
                # 番号只对 AV 库提取：电影/剧集片名里的英文词+数字会被误判
                # （"阿波罗13号"→APOLLO13、"2001太空漫游"→HDR10）
                matcher.extract_av_code(name, orig, path) if cat == "AV" else "",
                json.dumps(emby.media_sources(it), ensure_ascii=False) if it.get("MediaSources") else "",
                db.now(),
            ))
            n += 1
            pulled_ids.append(it["Id"])
            if len(rows) >= 500:
                _flush(c, rows)
                rows = []
                print(f"  {lib['name']}: {n}", flush=True)
        if rows:
            _flush(c, rows)
        c.commit()
        stale = _prune_library(c, lib["id"], lib["name"], pulled_ids)
        c.commit()
        total += n
        print(f"✓ {lib['name']} [{cat}] {n} 条" + (f"，清理失效 {stale} 条" if stale else ""), flush=True)

    db.set_meta("owned_synced_at", db.now())
    db.set_meta("owned_count", total)
    print(f"\n完成：{total} 条，耗时 {time.time()-t0:.1f}s")
    print(db.stats())


def _flush(c, rows):
    c.executemany("""
        INSERT INTO owned (emby_id,name,original_title,year,premiere,type,lib_id,library,
                           tmdb_id,imdb_id,tvdb_id,has_image,image_tag,path,norm,av_code,medias,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(emby_id) DO UPDATE SET
          name=excluded.name, original_title=excluded.original_title, year=excluded.year,
          premiere=excluded.premiere, library=excluded.library, tmdb_id=excluded.tmdb_id,
          imdb_id=excluded.imdb_id, tvdb_id=excluded.tvdb_id, has_image=excluded.has_image,
          image_tag=excluded.image_tag, path=excluded.path, norm=excluded.norm,
          av_code=excluded.av_code, medias=excluded.medias, updated_at=excluded.updated_at
    """, rows)
    c.commit()


if __name__ == "__main__":
    main()
