"""115 网盘：扫码登录 + 目录扫描（纯标准库）。
登录流程（2026-10-18 实测可用）：
1. GET  qrcodeapi.115.com/api/1.0/web/1.0/token         → uid / time / sign
2. GET  qrcodeapi.115.com/api/1.0/web/1.0/qrcode?uid=   → PNG 二维码
3. GET  qrcodeapi.115.com/get/status/?uid&time&sign     → data.status 0未扫/1已扫/2已确认
4. POST qrcodeapi.115.com/app/1.0/{app}/1.0/login/qrcode/ (account=uid)
   → data.cookie{UID,CID,SEID,KID}；app = 登录端类型(web/115android/115ios/...)

取目录：
GET webapi.115.com/files?aid=1&cid=&show_dir=1&limit=1150&offset=0
返回 data[]：n=名称 s=大小 cid=目录id fid=文件id d=1 表示目录
"""
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 115Browser/25.0")

# 115 千万别走代理（会触发风控 / 也被墙外出口拒绝）
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))

QR_BASE = "https://qrcodeapi.115.com"
PASSPORT = "https://passportapi.115.com"
WEBAPI = "https://webapi.115.com"

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".rmvb", ".wmv", ".mov",
             ".flv", ".iso", ".mpg", ".mpeg", ".webm", ".m4v", ".bdmv"}

# 扫码登录可选的客户端类型（app 代码 → 显示名），与官方 p115client 一致
APP_CHOICES = [
    ("web", "网页版"),
    ("desktop", "115 浏览器(桌面端)"),
    ("115android", "115(Android端)"),
    ("android", "115生活(Android端)"),
    ("115ios", "115(iOS端)"),
    ("ios", "115生活(iOS端)"),
    ("115ipad", "115(iPad端)"),
    ("tv", "115网盘(Android电视端)"),
    ("qandroid", "115管理(Android端)"),
    ("qios", "115管理(iOS端)"),
    ("wechatmini", "115生活(微信小程序)"),
    ("alipaymini", "115生活(支付宝小程序)"),
    ("windows", "115生活(Windows端)"),
    ("mac", "115生活(macOS端)"),
    ("linux", "115生活(Linux端)"),
    ("harmony", "115(Harmony端)"),
]


class Pan115Error(RuntimeError):
    pass


def _get(url, cookie=None, timeout=20, raw=False):
    hdr = {"User-Agent": UA, "Accept": "*/*", "Referer": "https://115.com/"}
    if cookie:
        hdr["Cookie"] = cookie
    req = urllib.request.Request(url, headers=hdr)
    try:
        with _NO_PROXY.open(req, timeout=timeout) as f:
            data = f.read()
            if raw:
                return data, f.headers.get("Content-Type", "image/png")
            return json.loads(data.decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        raise Pan115Error(f"HTTP {e.code} {url}")
    except Exception as e:
        raise Pan115Error(f"{type(e).__name__}: {e} ({url})")


def _post(url, form, timeout=20):
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with _NO_PROXY.open(req, timeout=timeout) as f:
            return json.loads(f.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        raise Pan115Error(f"HTTP {e.code} {url}")
    except Exception as e:
        raise Pan115Error(f"{type(e).__name__}: {e}")


# ---------------- 登录 ----------------
def qr_new():
    """申请一个新二维码，返回 uid/time/sign + PNG 原始字节。"""
    d = _get(QR_BASE + "/api/1.0/web/1.0/token")
    data = d.get("data") or {}
    uid = data.get("uid") or ""
    if not uid:
        raise Pan115Error(f"申请二维码失败：{d}")
    img, ctype = _get(f"{QR_BASE}/api/1.0/web/1.0/qrcode?uid={uid}", raw=True)
    return {
        "uid": uid,
        "time": data.get("time"),
        "sign": data.get("sign"),
        "image": img,
        "ctype": ctype or "image/png",
    }


def qr_status(uid, tm, sign):
    """返回 {status: 0未扫 / 1已扫码待确认 / 2已确认, msg}"""
    q = urllib.parse.urlencode({"uid": uid, "time": tm, "sign": sign})
    # 115 的 status 接口是长轮询（最多挂 30s），超时必须给够，否则永远读不到
    d = _get(f"{QR_BASE}/get/status/?{q}", timeout=40)
    inner = d.get("data") or {}
    return {"status": int(inner.get("status") or 0), "msg": inner.get("msg") or "",
            "code": d.get("code")}


def _parse_cookie_str(s):
    """把 "UID=..; CID=..; SEID=..; KID=.." 解析成 dict。"""
    out = {}
    for part in str(s or "").split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip()
        if k:
            out[k] = v.strip()
    return out


def qr_login(uid, app="web"):
    """扫码确认后换取 cookie。返回 {UID,CID,SEID,KID} + 昵称。

    app = 登录端类型（web / 115android / 115ios / 115ipad / tv / qandroid /
    wechatmini / alipaymini ...），决定 115 发哪种客户端会话。
    走官方端点 qrcodeapi.115.com/app/1.0/{app}/1.0/login/qrcode/。
    """
    app = (app or "web").strip() or "web"
    if app == "desktop":
        app = "web"                      # 官方同款：desktop 归一到 web
    d = None
    err = ""
    for url in (f"{QR_BASE}/app/1.0/{app}/1.0/login/qrcode/",
                f"{PASSPORT}/app/1.0/{app}/1.0/login/qrcode"):
        try:
            d = _post(url, {"account": uid})
            break
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            d = None
    if d is None:
        raise Pan115Error(f"换 cookie 失败：{err}")
    data = d.get("data") or {}
    ck = data.get("cookie") or {}
    if isinstance(ck, str):
        ck = _parse_cookie_str(ck)
    if not ck.get("UID"):
        raise Pan115Error(f"登录失败(app={app})：{d.get('message') or d.get('error') or d}")
    return {
        "cookie": {k: str(ck.get(k) or "") for k in ("UID", "CID", "SEID", "KID")},
        "user_id": data.get("user_id"),
        "user_name": data.get("user_name") or "",
        "app": app,
    }


def cookie_header(ck):
    return "; ".join(f"{k}={ck.get(k)}" for k in ("UID", "CID", "SEID", "KID") if ck.get(k))


# ---------------- 目录 ----------------
# 【2026-11-03 实测】路径解析是 EvoLink 换链偏慢的根因：
#   resolve_file 逐级 list_dir，且 list_dir 会把整个目录全量翻页拉完
#   （实测「演唱会」目录 461 项 → 末级一次 list_dir 就要 1.3s，
#     完整路径 4 级合计 2.26s + 签发 0.42s ≈ 3.1s）。
#   而同类实现首播仅 0.6~1.0s、命中 3ms —— 它有 per-drive 文件信息缓存。
# 对策：给 list_dir 加**按 cid 的目录缓存**，命中直接返回，不再打 115。
#   TTL 取 1800s（与通行做法的 cache_expire、url_cache_expire 同值）。
#   风险：目录内容变更最多延迟一个 TTL 才可见 —— 对「播放前解析路径」完全可接受
#   （媒体库不会在播放过程中变化）。
# 失效：换 cookie 时目录结构可能变（不同账号），故缓存键含 UID。
DIR_CACHE_TTL = 1800
_DIR_CACHE = {}          # {(uid, cid): (ts, rows)}
_DIR_CACHE_LOCK = threading.Lock()


def dir_cache_clear():
    """清空目录缓存（换账号 / 手工刷新后调用）。"""
    with _DIR_CACHE_LOCK:
        _DIR_CACHE.clear()


def dir_cache_size():
    with _DIR_CACHE_LOCK:
        return len(_DIR_CACHE)


def _dir_cache_key(cid, ck):
    return (str(ck.get("UID") or ""), str(cid or "0"))


# ---------------- 风控备用通道：files/search + files/getid ----------------
# 【2026-11-03 关键实测结论，决定了 EvoLink 能否在 115 风控下正常换链】
#
#   接口                          风控态表现          说明
#   webapi.115.com/files          405 HTML 风控页     ✗ 列目录被完全拦截
#   webapi.115.com/files/search   200 正常            ✓ 可按名搜索、能拿 pickcode
#   webapi.115.com/files/getid    200 正常            ✓ 按路径一次返回目录 cid
#   proapi.115.com/.../downurl    200 正常            ✓ 签发直链一直可用
#
# 结论：**只有「列目录」这一个接口被封**。因此把「逐级列目录找文件」
# 换成「getid 拿目录 cid + search 精确匹配文件名」，即可完全绕开风控，
# 且请求数从 6 次降到 2 次。抗风控用的正是同一套官方接口。
#
# 保留 list_dir 作为回退：非风控时它更快（一次拿全目录 + 命中目录缓存）。

def _search_page(cid, keyword, ck, limit=50, offset=0):
    """调 files/search，返回 (rows, count)。分页由调用方处理。"""
    ck = ck or (config.load().get("pan115_cookie") or {})
    q = urllib.parse.urlencode({
        "aid": "1", "cid": str(cid or "0"), "search_value": keyword,
        "show_dir": "1", "limit": str(limit), "offset": str(offset),
        "format": "json",
    })
    d = _get(f"{WEBAPI}/files/search?{q}", cookie=cookie_header(ck))
    if not isinstance(d, dict):
        raise Pan115Error(f"search 返回异常：{d}")
    return (d.get("data") or []), int(d.get("count") or 0)


def _norm_search_row(it, parent_cid):
    """把 files/search 的一行规整成与 list_dir 一致的结构。

    search 返回的字段语义与 files 不同（实测）：
      文件项（fc=1）：cid = 所在目录 id，pc = pickcode，s = 大小
      目录项（fc=0）：cid = 目录自身 id，pid = 父目录 id，dp = 父目录名
    """
    name = it.get("n") or it.get("fn") or it.get("name") or ""
    fc = str(it.get("fc") or "0")
    is_dir = (fc == "0")
    ext = ""
    if not is_dir and "." in name:
        ext = "." + name.rsplit(".", 1)[-1].lower()
    return {
        "fid": str(it.get("fid") or it.get("cid") or ""),
        "cid": str(it.get("cid") or ""),
        "parent": str(it.get("pid") or parent_cid or "0"),
        "name": name,
        "size": int(it.get("s") or it.get("size") or 0),
        "is_dir": is_dir,
        "ext": ext,
        "pc": str(it.get("pc") or ""),
    }


def search_list(cid="0", ck=None, limit=500):
    """用 files/search 列出某目录的直接子项（风控态下替代 list_dir）。

    search_value="." 是 115 搜索的「匹配全部」语义（p115client 默认值），
    配合 cid 参数即可拿到该目录的全部内容。实测 count 即目录项总数。
    """
    ck = ck or (config.load().get("pan115_cookie") or {})
    cid = str(cid or "0")
    out, offset, guard = [], 0, 0
    while guard < 60:
        guard += 1
        rows, count = _search_page(cid, ".", ck, limit=limit, offset=offset)
        if not rows:
            break
        for it in rows:
            r = _norm_search_row(it, cid)
            # 只保留直接子项：文件 cid==本目录；目录 pid==本目录
            if r["is_dir"]:
                if r["parent"] != cid:
                    continue
            else:
                if r["cid"] != cid:
                    continue
            out.append(r)
        offset += len(rows)
        if offset >= count or len(rows) < limit:
            break
        time.sleep(0.12)
    return out


def getid(path, ck=None, cid="0"):
    """files/getid：按路径一次返回目录 id（风控态正常）。

    实测：路径必须是「相对根目录」且**不含首段存储名**（如 115open/... 会返回 0）；
    含文件名的路径返回 0（只支持目录）。找不到时返回 0。
    """
    ck = ck or (config.load().get("pan115_cookie") or {})
    q = urllib.parse.urlencode({"path": path, "cid": str(cid or "0"), "fmt": "json"})
    try:
        d = _get(f"{WEBAPI}/files/getid?{q}", cookie=cookie_header(ck))
    except Pan115Error:
        return "0"
    if not isinstance(d, dict) or not d.get("state"):
        return "0"
    return str(d.get("id") or "0")


def search_pick(cid, name, ck=None):
    """在指定目录 cid 内精确查找名为 name 的文件，返回规整后的项（找不到 None）。

    走 files/search（风控态正常）。目录项与文件项分开取，避免同名歧义。
    """
    ck = ck or (config.load().get("pan115_cookie") or {})
    cid = str(cid or "0")
    offset, guard = 0, 0
    file_hit = None
    dir_hit = None
    while guard < 40:
        guard += 1
        rows, count = _search_page(cid, name, ck, limit=50, offset=offset)
        if not rows:
            break
        for it in rows:
            n = it.get("n") or it.get("fn") or it.get("name") or ""
            if n != name:
                continue
            r = _norm_search_row(it, cid)
            if r["is_dir"]:
                if r["parent"] == cid and dir_hit is None:
                    dir_hit = r
            else:
                if r["cid"] == cid and file_hit is None:
                    file_hit = r
        if file_hit is not None:
            break
        offset += len(rows)
        if offset >= count or len(rows) < 50:
            break
    return file_hit, dir_hit


def list_dir(cid="0", ck=None, limit=1150, use_cache=True):
    """列一个目录，自动翻页。返回 [{fid,cid,name,size,is_dir,ext,parent}]

    use_cache=True（默认）：目录列表走进程内缓存（DIR_CACHE_TTL 秒），
    命中直接返回，不打 115 —— 这是把首播耗时从 3.1s 压到亚秒级的关键。

    【2026-11-03 风控实测】webapi.115.com/files 列目录接口在风控态下返回
    HTTP 405（HTML 风控页），完全不可用；而 files/search 与 files/getid 两个
    接口在同状态下**完全正常**（200）。因此本函数在 files 接口失败时
    自动降级到 search 通道（见 search_list），保证风控下仍能遍历/解析。
    """
    ck = ck or (config.load().get("pan115_cookie") or {})
    ch = cookie_header(ck)
    if use_cache:
        key = _dir_cache_key(cid, ck)
        now = time.time()
        with _DIR_CACHE_LOCK:
            hit = _DIR_CACHE.get(key)
            if hit and now - hit[0] < DIR_CACHE_TTL:
                return list(hit[1])
    out, offset, guard = [], 0, 0
    while guard < 40:
        guard += 1
        q = urllib.parse.urlencode({
            "aid": "1", "cid": cid or "0", "show_dir": "1",
            "limit": str(limit), "offset": str(offset), "o": "user_ptime", "asc": "0", "fc_mix": "0", "format": "json",
        })
        try:
            d = _get(f"{WEBAPI}/files?{q}", cookie=ch)
        except Pan115Error:
            # 风控态：files 列目录被 405 拦截 → 降级到 search 通道
            out = search_list(cid, ck)
            break
        if not d.get("state"):
            raise Pan115Error(f"读取目录失败：{d.get('error') or d}")
        rows = d.get("data") or []
        for it in rows:
            name = it.get("n") or it.get("fn") or it.get("name") or ""
            # 115 files 接口：目录项无 fid（文件才有 fid），d 字段不可靠
            is_dir = not it.get("fid") or str(it.get("is_dir") or "0") == "1"
            ext = ""
            if not is_dir and "." in name:
                ext = "." + name.rsplit(".", 1)[-1].lower()
            out.append({
                "fid": str(it.get("fid") or it.get("cid") or ""),
                "cid": str(it.get("cid") or ""),
                "parent": str(cid or "0"),
                "name": name,
                "size": int(it.get("s") or it.get("size") or 0),
                "is_dir": is_dir,
                "ext": ext,
                "pc": str(it.get("pc") or ""),
            })
        count = int(d.get("count") or 0)
        offset += len(rows)
        if not rows or offset >= count:
            break
        time.sleep(0.15)
    if use_cache:
        with _DIR_CACHE_LOCK:
            if len(_DIR_CACHE) > 4096:
                _DIR_CACHE.clear()
            _DIR_CACHE[_dir_cache_key(cid, ck)] = (time.time(), list(out))
    return out


def walk(root="0", ck=None, depth=3, max_items=20000, progress=None):
    """广度优先扫描目录树。返回 (items, dirs)。

    depth 是层数：1 只扫根目录下的直接内容。
    """
    ck = ck or (config.load().get("pan115_cookie") or {})
    items, dirs = [], []
    queue = [(root, "", 1)]
    while queue and len(items) < max_items:
        cid, prefix, lv = queue.pop(0)
        try:
            rows = list_dir(cid, ck)
        except Pan115Error as e:
            if progress:
                progress(f"  ! 跳过目录 {prefix or '/'}：{e}")
            continue
        for it in rows:
            it["path"] = f"{prefix}/{it['name']}"
            if it["is_dir"]:
                dirs.append(it)
                if lv < depth:
                    queue.append((it["cid"], it["path"], lv + 1))
            else:
                items.append(it)
        if progress:
            progress(f"  {prefix or '/'} 读到 {len(rows)} 项（累计 {len(items)} 文件）")
        if len(items) >= max_items:
            break
    return items, dirs


def is_video(name):
    return ("." in name) and ("." + name.rsplit(".", 1)[-1].lower()) in VIDEO_EXT


# ---------------- 直链（302 播放用） ----------------
# files/video 等接口要求 115Browser UA，普通 Chrome UA 会被 405 拒绝
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 115Browser/25.0")


def _get_br(url, cookie=None, timeout=20):
    """带 115Browser UA 的 GET（换链接口用，同样不走代理）"""
    hdr = {"User-Agent": _BROWSER_UA, "Accept": "*/*", "Referer": "https://115.com/"}
    if cookie:
        hdr["Cookie"] = cookie
    req = urllib.request.Request(url, headers=hdr)
    try:
        with _NO_PROXY.open(req, timeout=timeout) as f:
            return json.loads(f.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        raise Pan115Error(f"HTTP {e.code} {url}")
    except Exception as e:
        raise Pan115Error(f"{type(e).__name__}: {e} ({url})")


def resolve_file(path, ck=None):
    """按 115 相对路径（如 115open/Video/Movie/xxx.mkv）解析到文件，返回含 pc 的项。

    path 首段为存储名/根别名（如 115open），自动跳过。

    【2026-11-03 重写】抗 115 风控。原先逐级 list_dir（webapi/files）在风控态
    全部 405，导致 EvoLink 换链退化成透传（Emby 拿到 200 而不是 302）。
    现在两条通道：
      A. 快路径（抗风控）：files/getid 一次拿到**父目录 cid**，
         再 files/search 在该 cid 内精确匹配文件名 → 2 次请求，且风控态可用。
      B. 回退：逐级 list_dir（非风控时一次拿全目录更快，且能命中目录缓存）。
    A 失败（路径不存在 / 接口异常）时自动降级到 B，B 也失败才抛错。
    """
    ck = ck or (config.load().get("pan115_cookie") or {})
    parts = [p for p in path.split("/") if p][1:]  # 去掉首段存储名
    if not parts:
        raise Pan115Error("路径为空")

    # ---- A. getid + search 快路径 ----
    try:
        fname = parts[-1]
        parent = "/".join(parts[:-1])
        if parent:
            dcid = getid(parent, ck)
        else:
            dcid = "0"
        if dcid and dcid != "0":
            fhit, _dhit = search_pick(dcid, fname, ck)
            if fhit and fhit.get("pc"):
                return fhit
    except Exception:
        pass  # 静默降级到 B 通道

    # ---- B. 逐级 list_dir 回退 ----
    cid = "0"
    for i, name in enumerate(parts):
        rows = list_dir(cid, ck)
        hit = next((it for it in rows if it["name"] == name), None)
        if hit is None:
            raise Pan115Error(f"未找到: {name} (in cid={cid})")
        if i == len(parts) - 1:
            if hit["is_dir"]:
                raise Pan115Error(f"{name} 是目录，不是文件")
            return hit
        if not hit["is_dir"]:
            raise Pan115Error(f"{name} 不是目录")
        cid = hit["cid"]
    raise Pan115Error("路径解析失败")


# 路径 → (ts, {name, pc, cid, ...}) 的解析结果缓存
_PC_CACHE = {}
_PC_CACHE_LOCK = threading.Lock()


def pc_cache_clear():
    with _PC_CACHE_LOCK:
        _PC_CACHE.clear()


def pc_cache_size():
    with _PC_CACHE_LOCK:
        return len(_PC_CACHE)


# ---------- 磁盘持久化（跨重启仍命中） ----------
# 实测重启后首播仍 0.01~0.05s：把「文件信息」缓存落盘了。
# EvoLink 若只靠内存缓存，重启后首播要 0.7~2.7s（重走 list_dir + 重新签发）。
# 这里把「路径 → pickcode」结果落盘（目录列表不落盘：体积大且刷新快）。
_PC_DISK_FILE = None
_PC_DIRTY = {"v": False}
_PC_LAST_SAVE = {"v": 0.0}
_PC_SAVE_TTL = 30


def _pc_disk_path():
    try:
        from . import config as _cfg
        data = getattr(_cfg, "DATA", None)
        if data:
            return os.path.join(str(data), "115_pc_cache.json")
    except Exception:
        pass
    return "/app/data/115_pc_cache.json"


def pc_disk_load():
    try:
        with open(_pc_disk_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return
    now = time.time()
    n = 0
    with _PC_CACHE_LOCK:
        for k, v in (d or {}).items():
            if not isinstance(v, dict):
                continue
            ts = float(v.get("ts") or 0)
            if now - ts < DIR_CACHE_TTL:
                _PC_CACHE[k] = (ts, v.get("item") or {})
                n += 1
    if n:
        print(f"[115] 从磁盘恢复路径缓存 {n} 条", flush=True)


def pc_disk_save():
    now = time.time()
    out = {}
    with _PC_CACHE_LOCK:
        for k, (ts, item) in _PC_CACHE.items():
            out[k] = {"ts": ts, "item": item}
    try:
        p = _pc_disk_path()
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception:
        pass
    _PC_DIRTY["v"] = False
    _PC_LAST_SAVE["v"] = now


def _pc_mark_dirty():
    _PC_DIRTY["v"] = True
    if _PC_LAST_SAVE["v"] <= 0 or time.time() - _PC_LAST_SAVE["v"] >= _PC_SAVE_TTL:
        pc_disk_save()


def init_disk_cache():
    """启动时加载磁盘上的路径缓存。"""
    try:
        pc_disk_load()
    except Exception:
        pass


def _pc_worker():
    while True:
        time.sleep(15)
        try:
            if _PC_DIRTY["v"]:
                pc_disk_save()
        except Exception:
            pass


def start_pc_worker():
    try:
        t = threading.Thread(target=_pc_worker, daemon=True)
        t.start()
    except Exception:
        pass
    try:
        import atexit
        atexit.register(pc_disk_save)
    except Exception:
        pass


def resolve_file_cached(path, ck=None):
    """resolve_file + 路径→pickcode 缓存（推荐用这个）。

    缓存键含 UID（换账号后自动失效）。TTL 与目录缓存一致（1800s）。
    """
    ck = ck or (config.load().get("pan115_cookie") or {})
    # 缓存键用**字符串**（"uid\x01path"）：tuple 不能直接做 JSON 的 key，
    # 落盘会静默失败（异常被 except 吞掉），缓存文件永远是空的。
    key = str(ck.get("UID") or "") + "\u0001" + str(path or "")
    now = time.time()
    with _PC_CACHE_LOCK:
        hit = _PC_CACHE.get(key)
        if hit and now - hit[0] < DIR_CACHE_TTL:
            return dict(hit[1])
    f = resolve_file(path, ck)
    with _PC_CACHE_LOCK:
        if len(_PC_CACHE) > 20000:
            _PC_CACHE.clear()
        _PC_CACHE[key] = (now, dict(f))
    _pc_mark_dirty()
    return f


def direct_url(path, ck=None):
    """115 相对路径 → 直链 URL（302 用）。

    优先 webapi.115.com/files/video 的 origin_file_url（VIP 通道，大文件可用），
    非视频/异常回退 files/download。全程走 _NO_PROXY，避免 115 风控。
    """
    ck = ck or (config.load().get("pan115_cookie") or {})
    ch = cookie_header(ck)
    f = resolve_file(path, ck)
    pc = f.get("pc") or ""
    if not pc:
        raise Pan115Error(f"无 pickcode: {f['name']}")
    r = _get_br(f"{WEBAPI}/files/video?pickcode={pc}", cookie=ch)
    url = r.get("origin_file_url") if r.get("state") else None
    msg1 = r.get("msg") or ""
    if not url:
        r2 = _get_br(f"{WEBAPI}/files/download?pickcode={pc}", cookie=ch)
        url = r2.get("file_url") if r2.get("state") else None
        msg2 = r2.get("msg") or ""
    if not url:
        raise Pan115Error(f"换链失败: {msg1 or msg2 or 'unknown'}")
    return url
