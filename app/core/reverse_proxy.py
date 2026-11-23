"""evolink 反向代理：让 Jellyfin / Emby 客户端直接连 evolink 的代理端口，
由代理拦截媒体请求 → 查播放器 API 拿 item 路径 → 读 strm → 抽 115 路径 →
用客户端 UA 签 115 CDN 直链 → 302。strm 文件一字不动（主流做法）。

- 非媒体请求：整段反向代理到后端播放器（127.0.0.1:8098 / 8096），UI 照常可用。
- 媒体请求命中 .strm：换链 302；命中普通文件：透传后端（走播放器原路径）。
- 115 未登录 / 风控：优雅回退到透传（片子照常能播，只是不走 CDN 302）。

零第三方依赖，纯标准库，配合 server.py 的 ThreadingHTTPServer 起第二个监听。
"""

import collections
import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

from core import pan115_downurl

# item id：Jellyfin/Emby 都是 GUID 形如 8-4-4-4-12 的十六进制，个别老接口用纯数字
# 只拦「真正的媒体流」路径：/Videos/{id}/stream(.xxx) 与 /Videos/{id}/original.mp4
# 不能拦 AdditionalParts / Subtitles / Chapters / main.m3u8 这类要 JSON 或要转码的接口
#
# 【2026-11-03 新增：Emby 支持 + 前缀/大小写兼容】
# 1) id 形态：Jellyfin/Plex 是 GUID，Emby 是**整数**（实测 Emby 库 MediaItems.Id=1029350）。
#    原正则只认 GUID/32位hex → Emby 请求全部匹配不上 → 反代永远走不到换链分支，
#    直接透传，于是 EvoLink 无法接管 Emby。
# 2) 路径形态：常见的反代规则是
#      path /emby/videos/* /videos/* /emby/Audio/* /audio/*
#    即**前缀 /emby 可选**、**大小写混合**（Emby 客户端用小写 /videos/）。
#    EvoLink 反代原样把 self.path 转发给后端，所以这 4 种形态都要能匹配。
# 3) 还要容忍结尾的查询串之前的一切（/Videos/{id}/original.mp4?api_key=…）。
_ITEM_RE = re.compile(
    r"^/(?:emby/)?(?:Videos|Audio)/((?:[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})|[0-9A-Fa-f]{32}|[0-9]{1,20})"
    r"/(?:stream(?:\.[A-Za-z0-9]+)?|original\.[A-Za-z0-9]+)$",
    re.IGNORECASE)
_ITEMS_DL_RE = re.compile(r"^/(?:emby/)?Items/([0-9A-Fa-f-]+|[0-9]{1,20})/Download", re.IGNORECASE)
# PlaybackInfo：Jellyfin 对路径型 strm 探测必挂，这里由反代自己合成
_PLAYBACK_RE = re.compile(r"^/(?:emby/)?Items/([0-9A-Za-z-]+|[0-9]{1,20})/PlaybackInfo", re.IGNORECASE)

_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}

# item_id -> (timestamp, 115相对路径 或 None)  短 TTL 缓存，避免每次媒体请求都打播放器 API
_CACHE = {}
_CACHE_TTL = 300
_CACHE_LOCK = threading.Lock()

# port -> (httpd, thread)  运行中的反代实例，便于「应用并重启反代」时热替换
_SERVERS = {}
_SERVERS_LOCK = threading.Lock()
# 播放器名 -> 当前监听端口（改端口时用来停掉旧端口，避免泄漏）
_ASSIGNED = {}
_ASSIGNED_LOCK = threading.Lock()

# ---- 302 事件日志（115 栏目「302 日志」页读这个）----
_EVENTS = collections.deque(maxlen=800)
_EVENTS_LOCK = threading.Lock()
# 115路径 -> 验过的时间。**按文件缓存，不按 UA**：
# 一次播放里播放器常换 UA（AppleCoreMedia → Lavf → CloudDrive），若按 UA 缓存
# 就会每个 UA 都重验一次，白白多次打 CDN、也更容易被 115 判风控。
# 验过一次说明「115 这条链当下是好的」，后续同文件直接信任，验过不再验。
_VERIFIED = {}
_VERIFIED_LOCK = threading.Lock()
_VERIFY_TTL = 3600
# 最近一次「自检失败」的时间。失败后一段时间内不再自检（115 正在限流，
# 继续试只会加深风控），直接信任链或退回透传。
_VERIFY_FAIL = {}
_VERIFY_FAIL_TTL = 300


def log_event(kind, **fields):
    """记一条反代事件。kind: media / playbackinfo / ws / passthrough / error / stream

    同一文件 + 同一类型的连续事件**合并计数**（日志里就是一条
    「使用缓存的重定向URL」对应几十个分片请求）。合并后日志页不会被
    分片请求刷屏，也避免看着像在「频繁换流」。
    """
    ev = {"ts": time.time(), "kind": kind}
    ev.update(fields)
    with _EVENTS_LOCK:
        if kind == "media":
            sig = (fields.get("target"), fields.get("ua"), fields.get("status"))
            for old in reversed(_EVENTS):
                if old.get("kind") != "media":
                    break
                if (old.get("target"), old.get("ua"), old.get("status")) == sig:
                    # 同一文件的连续 302 → 计数 +1，不新增行
                    old["n"] = int(old.get("n") or 1) + 1
                    old["ts"] = ev["ts"]
                    return old
        _EVENTS.append(ev)
    return ev


def recent_events(limit=400, kind=None):
    with _EVENTS_LOCK:
        items = list(_EVENTS)
    if kind:
        items = [e for e in items if e.get("kind") == kind]
    return items[-int(limit):]


def clear_events():
    with _EVENTS_LOCK:
        _EVENTS.clear()


def _strip_prefix(line, local_prefix="", remote_prefix=""):
    """strm 内容第一行可能是 /CloudNAS/CloudDrive/115open/...，抽成 115 相对路径。

    local_prefix / remote_prefix 对应「迁移目录」映射（本地挂载前缀 → 115 前缀），
    在设置页可填；默认把 /CloudNAS/CloudDrive 这段去掉，剩下 115open/... 直接就是 115 相对路径。
    """
    p = (line or "").strip()
    if local_prefix and p.startswith(local_prefix):
        p = p[len(local_prefix):]
    if remote_prefix:
        p = remote_prefix.rstrip("/") + "/" + p.lstrip("/")
    else:
        p = p.lstrip("/")
    return p


# URL 型 strm 里常见的「挂载段」——URL path 去掉这一段之后才是 115 路径
# （alist /d/、webdav /webdav/、evolink 早期自建网关 /s/、朋友的 /api/redirect115/<pc>）
_URL_MOUNT_SEGS = ("/d/", "/webdav/", "/s/", "/strm/", "/api/redirect115/")


def strm_line_info(line, backend=None):
    """解析 strm 第一行 → 结构化信息（换链页「上传 .strm 自动分析」用）。

    kind:
      pickcode —— URL 型且末段是 pickcode（如 http://nas:8115/api/redirect115/<pc>）；
                 直接用**当前登录账号**签（115 的 pickcode 账号隔离，非本账号返回 50015）。
      path     —— CD2 路径型（/CloudNAS/CloudDrive/115open/...），或 URL 里藏路径
                 （剥掉 /d/、/webdav/ 等挂载段后按路径走）。
      unknown  —— 空行 / 无法解析。
    rel：最终交给签名链路的定位符（pickcode 或 115 相对路径）。
    mode：生效的 strm_mode（backend.get("strm_mode")，缺省 auto）。
    """
    backend = backend or {}
    mode = str(backend.get("strm_mode") or "auto").lower()
    if mode not in ("auto", "path", "pc"):
        mode = "auto"
    p = (line or "").strip().lstrip("\ufeff").strip()
    if not p:
        return {"ok": False, "kind": "unknown", "raw": (line or "")[:300], "rel": None,
                "mode": mode, "note": "strm 第一行为空"}
    if p.startswith("http://") or p.startswith("https://"):
        u = urllib.parse.urlparse(p)
        upath = urllib.parse.unquote(u.path or "")
        last = upath.rstrip("/").rsplit("/", 1)[-1] if upath.strip("/") else ""
        gw = u.netloc or ""
        if mode != "path" and (mode == "pc" or pan115_downurl.is_pickcode(last)):
            return {"ok": True, "kind": "pickcode", "raw": p[:300], "pickcode": last,
                    "gateway": gw, "rel": last, "mode": mode,
                    "note": "URL 型 strm（pickcode 直达，按当前登录的 115 账号签）"}
        q = upath
        for seg in _URL_MOUNT_SEGS:
            i = q.find(seg)
            if i >= 0:
                q = q[i + len(seg) - 1:]   # 保留前导 /
                break
        rel = _strip_prefix(q, backend.get("local_prefix"), backend.get("remote_prefix"))
        return {"ok": True, "kind": "path", "raw": p[:300], "path": rel, "gateway": gw,
                "rel": rel, "mode": mode,
                "note": "URL 型 strm（URL 内是路径，剥挂载段后按 CD2 路径型走）"}
    rel = _strip_prefix(p, backend.get("local_prefix"), backend.get("remote_prefix"))
    return {"ok": True, "kind": "path", "raw": p[:300], "path": rel, "rel": rel,
            "mode": mode, "note": "CD2 路径型 strm"}


def _strm_line_to_rel(line, backend=None):
    """strm 第一行 -> 115 定位符（115 相对路径或 pickcode）。URL 型和路径型都吃。"""
    return strm_line_info(line, backend).get("rel")


def _item_api_url(backend, item_id):
    base = backend["base"].rstrip("/")
    key = backend["api_key"]
    uid = backend["user_id"]
    if backend["kind"] == "jellyfin":
        # Jellyfin：用户态 item 接口
        return f"{base}/Users/{uid}/Items/{item_id}?api_key={key}"
    # Emby：/Items/{id} 直接带 api_key
    return f"{base}/Items/{item_id}?api_key={key}&Fields=Path,MediaSources"


def _id_variants(item_id):
    """Jellyfin 库里的 Id 存带横线 GUID，而 mediaSourceId 是不带横线的 hex —— 两种都要能查到。

    【2026-11-03】Emby 的 item id 是**纯数字**（如 1029350），既不是 GUID 也不是 hex，
    这里要原样保留，不能拿去做 GUID 变形（否则 32 位判断会误伤纯数字串）。
    """
    i = (item_id or "").strip()
    if not i:
        return []
    if i.isdigit():
        return [i]
    und = i.replace("-", "")
    out = [i]
    dash = (und[:8] + "-" + und[8:12] + "-" + und[12:16] + "-" + und[16:20] + "-" + und[20:]
            if len(und) == 32 else und)
    for v in (und, dash):
        if v and v not in out:
            out.append(v)
    return out


def _item_row(item_id, backend):
    """从播放器自带库里取 (Name, Path)。取不到返回 (None, None)。

    【2026-11-03 新增 Emby 支持】
    三种播放器的库表结构完全不同，缺一不可：
      Jellyfin / Plex → BaseItems / TypedBaseItems，Id 是 GUID 字符串
      Emby           → MediaItems，Id 是**整数**，且库里还有大量目录行
    原实现只查 BaseItems/TypedBaseItems → 在 Emby 上必然查不到任何路径，
    于是反代永远走不到换链分支（直接透传），EvoLink 就无法接管 Emby。
    Emby 的 MediaItems 65 万行里目录和文件混在一起，靠 Path LIKE '%.strm' 过滤。
    """
    db_path = backend.get("db_path")
    if not db_path:
        return None, None
    try:
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            cur = c.cursor()
            tables = set()
            try:
                tables = {r[0] for r in cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
            except Exception:
                pass
            # Emby：MediaItems（Id 为整数）
            if "MediaItems" in tables:
                for v in _id_variants(item_id):
                    try:
                        cur.execute(
                            "SELECT Name, Path FROM MediaItems "
                            "WHERE Id = ? AND Path LIKE '%.strm' LIMIT 1", (v,))
                        row = cur.fetchone()
                        if row:
                            return row[0], row[1]
                        # Id 是整数列，传字符串会不匹配 → 再试一次整数
                        try:
                            iv = int(str(v))
                        except Exception:
                            continue
                        cur.execute(
                            "SELECT Name, Path FROM MediaItems "
                            "WHERE Id = ? AND Path LIKE '%.strm' LIMIT 1", (iv,))
                        row = cur.fetchone()
                        if row:
                            return row[0], row[1]
                    except Exception:
                        continue
            # Jellyfin / Plex
            for _tbl in ("BaseItems", "TypedBaseItems"):
                if tables and _tbl not in tables:
                    continue
                for v in _id_variants(item_id):
                    try:
                        cur.execute(
                            f"SELECT Name, Path FROM {_tbl} WHERE upper(Id)=upper(?) LIMIT 1",
                            (v,))
                        row = cur.fetchone()
                        if row:
                            return row[0], row[1]
                    except Exception:
                        continue
        finally:
            c.close()
    except Exception:
        pass
    return None, None


def _resolve_strm(item_id, backend):
    """返回 115 定位符（115 相对路径或 pickcode），拿不到或不是 strm 返回 None。

    优先读播放器自带 SQLite 库（db_path，只读挂载），免 API 鉴权、最稳；
    db_path 缺失时再退回 API（Emby 等）。

    strm 第一行两种形态都吃（2026-10-24 起）：
      CD2 路径型 /CloudNAS/CloudDrive/115open/... → 115 相对路径；
      URL 型 http://.../api/redirect115/<pickcode> → pickcode（直达签名）。
    """
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(item_id)
        if hit and now - hit[0] < _CACHE_TTL:
            return hit[1]
    rel = None
    _name, spath = _item_row(item_id, backend)
    if spath and spath.endswith(".strm"):
        try:
            with open(spath, "r", errors="ignore") as f:
                line = f.readline().strip()
            if line:
                rel = _strm_line_to_rel(line, backend)
        except Exception:
            rel = None
    if rel is None and backend.get("api_key"):
        # API 兜底（少数没有 db 挂载的场景）
        try:
            _h = {"Accept": "application/json"}
            # Jellyfin / Playable fork（12.x）自 10.8 起弃用 api_key 查询参数鉴权，
            # 只认 Authorization: MediaBrowser Token="..."（带引号）。Emby 两种都收。
            if backend.get("kind") == "jellyfin" and backend.get("api_key"):
                _h["Authorization"] = 'MediaBrowser Token="%s"' % backend["api_key"]
            req = urllib.request.Request(
                _item_api_url(backend, item_id), headers=_h
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8", "ignore"))
            path = data.get("Path") or ""
            if not path and data.get("MediaSources"):
                path = (data["MediaSources"][0] or {}).get("Path") or ""
            if path and path.endswith(".strm"):
                with open(path, "r", errors="ignore") as f:
                    line = f.readline().strip()
                if line:
                    rel = _strm_line_to_rel(line, backend)
        except Exception:
            rel = None
    with _CACHE_LOCK:
        _CACHE[item_id] = (now, rel)
    return rel


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """绝不自动跟随 3xx。

    Jellyfin 的根路径 / 是 302 → /web/index.html（web app 本体）。如果反代替浏览器
    跟了这个跳转，就会把 web app 的 HTML 塞在 / 下，而它的 JS/CSS 都是相对路径
    （runtime.bundle.js 等）→ 解析成 /runtime.bundle.js → 404 → app 起不来 → 黑屏。
    所以 3xx 必须原样交回浏览器，由浏览器自己跳。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)
# 验证直链时必须直连（不走 mihomo），否则出口 IP 变了会被 CDN 判 403
_NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _item_strm_path_via_api(item_id, backend):
    """db_path 缺失时，通过播放器 API 拿 item 的 strm 本地路径（用于合成 PlaybackInfo）。

    与 _resolve_strm 的 API 兜底层对齐：Jellyfin / Playable fork（12.x）自 10.8 起
    弃用 api_key 查询参数鉴权，只认 Authorization: MediaBrowser Token="..."；
    Emby 两种都收。拿到的 Path 必须是 .strm 才返回，否则返回 None —— 交给后端原生
    PlaybackInfo 处理普通文件（普通文件不需要合成）。
    """
    if not backend.get("api_key"):
        return None
    # 轻量重试：EvoLink→Playable API 偶发超时时重试一次，消除车机首次播放
    # 因 PlaybackInfo 合成失败而卡住的瞬态（Jellyfin 12.x 对路径型 strm 探测会挂死，
    # 合成失败只能透传，车机就会卡在取可播地址这一步）。
    for _ in range(2):
        try:
            _h = {"Accept": "application/json"}
            if backend.get("kind") == "jellyfin" and backend.get("api_key"):
                _h["Authorization"] = 'MediaBrowser Token="%s"' % backend["api_key"]
            req = urllib.request.Request(_item_api_url(backend, item_id), headers=_h)
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8", "ignore"))
            path = data.get("Path") or ""
            if not path and data.get("MediaSources"):
                path = (data["MediaSources"][0] or {}).get("Path") or ""
            if path and path.endswith(".strm"):
                return path
            return None
        except Exception:
            continue
    return None


def _playback_info(item_id, backend):
    """合成 PlaybackInfo 响应，绕开 Jellyfin 对「路径型 strm」的探测。

    为什么需要：Jellyfin 12.1 的 ProbeProvider 只认「远程 strm」（内容是 http:// 开头），
    路径型 strm 会被判成 "Ignoring invalid or non-remote .strm path" → 探测抛
    NullReferenceException → POST /Items/{id}/PlaybackInfo 挂满 100 秒超时，客户端就播不了。
    Emby 4.10 支持路径型 strm，所以一切正常。

    我们自己合成：item → strm（播放器库里查）→ strm 第一行 = 115 路径 → 换算容器/大小。
    客户端拿到的 mediaSourceId 与 Jellyfin 原生一致（不带横线的 hex），
    随后请求 /Videos/{id}/stream?static=true&mediaSourceId=… 由反代 302 到 115 CDN。
    """
    name, spath = _item_row(item_id, backend)
    # 【方案 B · 2026-11-12】当 db_path 缺失 / 本地库查不到 strm 时，改走播放器 API
    # 兜底拿 strm 的本地路径，使车机经反代也能拿到合成 PlaybackInfo，绕开 Jellyfin 12.x
    # 对「路径型 strm」的挂死探测（NullReferenceException + 100s 超时）。
    if not spath or not spath.endswith(".strm"):
        spath = _item_strm_path_via_api(item_id, backend)
    # Emby 4.10 支持路径型 strm，它自己的 PlaybackInfo 更全（带 MediaStreams/时长），
    # 别用合成版把它盖掉；只对 Jellyfin 这种「探测会挂」的走合成。
    if backend.get("kind") != "jellyfin":
        return None
    if not spath or not spath.endswith(".strm"):
        return None
    try:
        with open(spath, "r", errors="ignore") as f:
            line = f.readline().strip()
    except Exception:
        return None
    if not line:
        return None

    ext = os.path.splitext(line)[1].lstrip(".").lower() or "mkv"
    size = 0
    # 115 路径在容器内的本地映射（evolink 只读挂了 /CloudNAS 时才有）
    if line.startswith("/CloudNAS"):
        try:
            size = os.path.getsize(line)
        except Exception:
            size = 0
    msid = (item_id or "").replace("-", "").lower()
    mime = {"mkv": "video/x-matroska", "mp4": "video/mp4", "avi": "video/x-msvideo",
            "mov": "video/quicktime", "ts": "video/mp2t", "m2ts": "video/mp2t",
            "webm": "video/webm", "flv": "video/x-flv", "wmv": "video/x-ms-wmv",
            "rmvb": "application/vnd.rn-realmedia-vbr"}.get(ext, "video/" + ext)
    src = {
        "Protocol": "File",
        "Id": msid,
        "Path": line,
        "Type": "Default",
        "Container": ext,
        "Size": size,
        "Name": os.path.splitext(os.path.basename(line))[0],
        "IsRemote": False,
        "HasMixedProtocols": False,
        "RunTimeTicks": 0,
        "SupportsTranscoding": True,
        "SupportsDirectStream": True,
        "SupportsDirectPlay": True,
        "IsInfiniteStream": False,
        "RequiresOpening": False,
        "RequiresClosing": False,
        "RequiresLooping": False,
        "SupportsProbing": False,
        "MediaStreams": [],
        "Formats": [],
        "Bitrate": 0,
        "RequiredHttpHeaders": [],
        "DirectStreamUrl": f"/Videos/{item_id}/stream?static=true&mediaSourceId={msid}",
        "AddApiKeyToDirectStreamUrl": False,
        "TranscodingUrl": None,
        "ReadAtNativeFramerate": False,
        "DefaultAudioStreamIndex": -1,
        "ItemId": item_id,
        "MimeType": mime,
    }
    return {"MediaSources": [src], "PlaySessionId": _new_guid(),
            "ErrorCode": None, "SupportsDirectStream": True,
            "SupportsDirectPlay": True, "SupportsTranscoding": True}


def _verify_cdn(url, ua):
    """用**同一个 UA** 试取 1 字节，确认这条 115 直链真的能用。

    115 偶发给出「看起来正常但取不到」的链（同 UA 跟随也是 403）。
    不验就发出去 = 播放器拿到坏链 → 闪一下就退回去。必须验过再发。
    """
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": ua, "Range": "bytes=0-0"})
        with _NOPROXY.open(req, timeout=25) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def _sign_and_verify(rel, ua):
    """签链 + 自检。返回 (url, ok, note)。

    对齐的做法：**签一次、验一次、长期复用**，绝不反复折腾 115。

    - 签名本身由 pan115_downurl 按 (路径, UA) 缓存 25 分钟，不额外打 115 API。
    - 自检结果按**文件**缓存 1 小时（不按 UA）—— 一次播放里播放器常换 UA
      （AppleCoreMedia → Lavf → CloudDrive），按 UA 缓存会每个 UA 都重验一次，
      白白多次打 CDN，也更容易被 115 判风控。
    - **自检失败不再 force 重签**：重签就是再打一次 115 API，正是风控的来源。
      失败就直接退回透传（播放器从 NAS 读，片子照常能播，只是慢一点）。
    """
    now = time.time()
    try:
        url = pan115_downurl.downurl_url(rel, ua=ua)
    except Exception as e:
        return None, False, f"签发失败：{e}"

    with _VERIFIED_LOCK:
        ts = _VERIFIED.get(rel)
        fail_ts = _VERIFY_FAIL.get(rel)
    if ts and now - ts < _VERIFY_TTL:
        return url, True, "已验过"
    # 最近刚验失败过（115 正在限流）：别再试了，直接信任链，交给播放器
    if fail_ts and now - fail_ts < _VERIFY_FAIL_TTL:
        return url, True, "限流期免验"
    if _verify_cdn(url, ua):
        with _VERIFIED_LOCK:
            _VERIFIED[rel] = now
            _VERIFY_FAIL.pop(rel, None)
        return url, True, "自检通过"
    # 验不过 → 不重签（省一次 115 API），标记限流、暂缓自检，退回透传
    with _VERIFIED_LOCK:
        _VERIFY_FAIL[rel] = now
        _VERIFIED.pop(rel, None)
    return url, False, "自检失败（退回透传）"


def _new_guid():
    import uuid
    return str(uuid.uuid4())


def _relay(a, b):
    """双向字节转发（WebSocket 隧道用）。任一端断开即结束。"""
    import threading as _t

    def pump(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except Exception:
            pass
        finally:
            try:
                dst.shutdown(1)
            except Exception:
                pass

    t1 = _t.Thread(target=pump, args=(a, b), daemon=True)
    t2 = _t.Thread(target=pump, args=(b, a), daemon=True)
    t1.start(); t2.start()
    t1.join(); t2.join()
    try:
        b.close()
    except Exception:
        pass


def _rewrite_server_info(data, public_base):
    """把 Jellyfin 自报的地址（LocalAddress，多半是容器内网）换成我们给客户端的地址。
    客户端用 LocalAddress 拼取流 URL，不改就会打到 172.21.x.x 这种走不到的地址。"""
    try:
        obj = json.loads(data.decode("utf-8"))
    except Exception:
        return data
    if not isinstance(obj, dict):
        return data
    changed = False
    for key in ("LocalAddress", "BaseUrl", "PublicBaseUrl"):
        v = obj.get(key)
        if isinstance(v, str) and v.startswith("http"):
            obj[key] = public_base
            changed = True
    la = obj.get("LocalAddresses")
    if isinstance(la, list):
        for i, v in enumerate(la):
            if isinstance(v, str) and v.startswith("http"):
                la[i] = public_base
                changed = True
    if not changed:
        return data
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _make_handler(backend):
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "evolink-proxy/1.0"

        # ---- 媒体判定 ----
        def _media_item_id(self):
            p = self.path.split("?", 1)[0]
            m = _ITEM_RE.match(p) or _ITEMS_DL_RE.match(p)
            return m.group(1) if m else None

        # ---- 302 换链 ----
        def _redirect(self, url):
            # 302 响应头要「干净」：
            # - 不发 Cache-Control（既不 no-store 也不 max-age）。播放器一旦缓存了
            #   这次重定向，快进/seek 时会直接复用旧 Location，而那条 115 URL 上的
            #   连接与 t 参数可能已失效 → 一快进就断。也不发这个头。
            # - 带 Accept-Ranges: bytes，明确告诉播放器「本资源支持 Range」，
            #   它才会用 Range 头做 seek 而不是重新拉整条流。
            self.send_response(302)
            self.send_header("Location", url)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", "0")
            self.end_headers()

        # ---- 公共地址（「回调地址」概念） ----
        def _public_base(self):
            """客户端该用哪个地址来连我们。
            优先用配置里填的「回调地址」；没填就从 Host 头自动推断
            （客户端是通过哪个地址连过来的，就还给它哪个地址）。"""
            cfgd = (backend.get("public_base") or "").strip()
            if cfgd:
                return cfgd.rstrip("/")
            host = self.headers.get("Host") or ""
            if host:
                return "http://" + host
            return backend["base"].rstrip("/")

        # ---- WebSocket 透传（Jellyfin 的 /socket 必须要） ----
        def _proxy_websocket(self):
            import socket as _socket
            pu = urllib.parse.urlparse(backend["base"])
            chost, cport = pu.hostname, pu.port or 80
            # 把客户端的 Upgrade 请求原样转给后端（只留握手必需的头）
            keep = ("upgrade", "connection", "sec-websocket-key", "sec-websocket-version",
                    "sec-websocket-protocol", "sec-websocket-extensions", "origin",
                    "user-agent", "cookie", "authorization", "x-emby-authorization")
            lines = ["GET %s HTTP/1.1" % self.path]
            for k in self.headers.keys():
                if k.lower() in keep:
                    lines.append("%s: %s" % (k, self.headers[k]))
            lines.append("Host: %s" % pu.netloc)
            # 浏览器没法给 WebSocket 加自定义头，而 Jellyfin 12.1 的 /socket 只认
            # Authorization 头（查 api_key / accessToken 查询参数一律 403）。
            # 所以客户端没带认证时，由反代用自己的 API Key 代为补上 —— 顺带比直连 Jellyfin 还顺。
            has_auth = any(k.lower() in ("authorization", "x-emby-authorization", "x-emby-token")
                           for k in self.headers.keys())
            if not has_auth and backend.get("api_key"):
                lines.append('Authorization: MediaBrowser Client="evolink-proxy", '
                             'Device="evolink-proxy", DeviceId="evolink-proxy", '
                             'Version="1.0.0", Token="%s"' % backend["api_key"])
                print(f"[proxy] {backend['kind']} WebSocket 客户端未带认证，"
                      f"反代代填 Authorization", flush=True)
            raw = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8", "ignore")
            try:
                up = _socket.create_connection((chost, cport), timeout=30)
                up.settimeout(None)
                up.sendall(raw)
            except Exception as e:
                print(f"[proxy] {backend['kind']} WebSocket 连接后端失败：{e}", flush=True)
                try:
                    self.send_error(502)
                except Exception:
                    pass
                return
            # 读后端的 101 响应
            buf = b""
            try:
                up.settimeout(30)
                while b"\r\n\r\n" not in buf:
                    chunk = up.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
            except Exception:
                buf = b""
            if not buf.startswith(b"HTTP/1.1 101"):
                # 把后端的真实状态回给客户端（别一律 502），方便排查
                head, _sep, rest = buf.partition(b"\r\n\r\n")
                try:
                    code = int(head.split(b" ")[1])
                except Exception:
                    code = 502
                print(f"[proxy] {backend['kind']} WebSocket 后端未返回 101：{head[:60]!r}", flush=True)
                try:
                    up.close()
                    self.send_response(code)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(rest)))
                    self.end_headers()
                    if rest:
                        self.wfile.write(rest)
                except Exception:
                    pass
                return
            # 原样回给客户端
            try:
                self.wfile.flush()
                self.connection.sendall(buf)
            except Exception:
                try:
                    up.close()
                except Exception:
                    pass
                return
            self.close_connection = True
            print(f"[proxy] {backend['kind']} WebSocket 已建立 → {backend['base']}{self.path}",
                  flush=True)
            log_event("ws", player=backend.get("kind"), item="", path=self.path[:160],
                      ua=(self.headers.get("User-Agent") or "")[:120], status=101,
                      note="WebSocket 已建立")
            _relay(self.connection, up)

        # ---- 整段反向代理到后端播放器 ----
        def _proxy_pass(self):
            target = backend["base"].rstrip("/") + self.path
            headers = {}
            for k in self.headers.keys():
                if k.lower() in _HOP:
                    continue
                headers[k] = self.headers[k]
            # 重写 Host 为后端
            pu = urllib.parse.urlparse(backend["base"])
            headers["Host"] = pu.netloc
            body = None
            if self.command in ("POST", "PUT") and "Content-Length" in self.headers:
                try:
                    body = self.rfile.read(int(self.headers["Content-Length"]))
                except Exception:
                    body = None
            # 服务器自报地址要改成「回调地址」：否则客户端会拿容器内网地址
            # （如 http://172.21.0.2:8096）去取流，局域网根本走不到 → 黑屏/播放失败
            fix_info = self.path.split("?", 1)[0] in ("/System/Info", "/System/Info/Public")
            try:
                req = urllib.request.Request(
                    target, data=body, method=self.command, headers=headers
                )
                with _OPENER.open(req, timeout=120) as r:
                    status = r.status
                    resp_headers = r.headers
                    if fix_info:
                        data = r.read()
                        new = _rewrite_server_info(data, self._public_base())
                        self.send_response(status)
                        for k in resp_headers.keys():
                            if k.lower() in _HOP or k.lower() == "content-length":
                                continue
                            self.send_header(k, resp_headers[k])
                        self.send_header("Content-Length", str(len(new)))
                        self.send_header("Connection", "close")
                        self.end_headers()
                        self.wfile.write(new)
                        self.close_connection = True
                        return
                    self.send_response(status)
                    for k in resp_headers.keys():
                        if k.lower() in _HOP:
                            continue
                        self.send_header(k, resp_headers[k])
                    self.send_header("Connection", "close")
                    self.end_headers()
                    try:
                        while True:
                            buf = r.read(65536)
                            if not buf:
                                break
                            self.wfile.write(buf)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
            except urllib.error.HTTPError as e:
                # 3xx：原样把跳转交回浏览器（Location 必须带上，否则浏览器拿到空页）
                if 300 <= e.code < 400:
                    loc = ""
                    try:
                        loc = e.headers.get("Location") or ""
                    except Exception:
                        loc = ""
                    self.send_response(e.code)
                    if loc:
                        self.send_header("Location", loc)
                    self.send_header("Content-Length", "0")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    return
                # 后端明确返回错误（如 404/401），原样回给客户端
                self.send_response(e.code)
                self.send_header("Connection", "close")
                self.end_headers()
            except Exception as e:
                try:
                    self.send_response(502)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Connection", "close")
                    self.send_header("Content-Length", str(len(str(e).encode())))
                    self.end_headers()
                    self.wfile.write(str(e).encode("utf-8"))
                except Exception:
                    pass
            self.close_connection = True

        # ---- 统一入口 ----
        def _dispatch(self):
            # WebSocket 升级请求（Jellyfin 的 /socket）单独走隧道
            up = (self.headers.get("Upgrade") or "").lower()
            if up == "websocket":
                self._proxy_websocket()
                return
            # PlaybackInfo 自己合成（绕开 Jellyfin 对路径型 strm 的挂死探测）
            m_pb = _PLAYBACK_RE.match(self.path.split("?", 1)[0])
            if m_pb:
                data = _playback_info(m_pb.group(1), backend)
                if data is not None:
                    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(body)
                    self.close_connection = True
                    print(f"[proxy] {backend['kind']} PlaybackInfo 已合成（跳过 Jellyfin 探测）："
                          f"{m_pb.group(1)}", flush=True)
                    log_event("playbackinfo", player=backend.get("kind"),
                              item=m_pb.group(1), path=self.path[:160],
                              ua=(self.headers.get("User-Agent") or "")[:120],
                              status=200, note="已合成（跳过 Jellyfin 探测）",
                              size=(data.get("MediaSources") or [{}])[0].get("Size"),
                              container=(data.get("MediaSources") or [{}])[0].get("Container"))
                    return
            item_id = self._media_item_id()
            if item_id and self.command in ("GET", "HEAD"):
                rel = _resolve_strm(item_id, backend)
                if rel:
                    ua = self.headers.get("User-Agent") or ""
                    url, ok, note = _sign_and_verify(rel, ua)
                    if not ok:
                        # 签出来的是坏链（115 限流/偶发无效）→ 退回透传，让 Jellyfin 自己从 NAS 读
                        print(f"[proxy] {backend['kind']} 换链自检失败（{note}），回退透传 {item_id}",
                              flush=True)
                        log_event("error", player=backend.get("kind"), item=item_id,
                                  path=self.path[:160], ua=ua[:120], status=0,
                                  note=f"{note}，退回透传")
                        self._proxy_pass()
                        return
                    # 换链成功：留一行日志，真实客户端播放时可在 docker logs 里看到
                    print(f"[proxy] {backend['kind']} 媒体请求 {item_id} → 302 换链"
                          f"（{rel}，UA={ua[:40]}，{note}）", flush=True)
                    log_event("media", player=backend.get("kind"), item=item_id,
                              path=self.path[:160], ua=ua[:120], status=302,
                              target=rel[:200], cdn=url[:200], note=note)
                    self._redirect(url)
                    return
                # 没解析出 115 路径 → 只能透传给播放器
                log_event("passthrough", player=backend.get("kind"), item=item_id,
                          path=self.path[:160], ua=(self.headers.get("User-Agent") or "")[:120],
                          status=0, note="未解析到 115 路径（不是 strm 或库里查不到）")
            self._proxy_pass()

        def do_GET(self):
            self._dispatch()

        def do_HEAD(self):
            self._dispatch()

        def do_POST(self):
            self._dispatch()

        def log_message(self, *args):
            pass  # 代理日志靠 evolink 主日志，这里静默

    return _Handler


def _stop_port(port):
    """停掉某个端口上的反代（热替换时用）。"""
    with _SERVERS_LOCK:
        s = _SERVERS.pop(port, None)
    if s:
        httpd, _t = s
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            pass
        print(f"[proxy] 已停止 :{port} 上的反代", flush=True)


def stop_all():
    for port in list(_SERVERS.keys()):
        _stop_port(port)
    with _ASSIGNED_LOCK:
        _ASSIGNED.clear()


def running_ports():
    with _SERVERS_LOCK:
        return sorted(_SERVERS.keys())


def _backend_from_cfg(cfg, prefix, kind):
    return {
        "kind": kind,
        "base": (cfg.get(prefix + "_base") or "").strip(),
        "api_key": (cfg.get(prefix + "_api_key") or "").strip(),
        "user_id": (cfg.get(prefix + "_user_id") or "").strip(),
        "db_path": (cfg.get(prefix + "_db_path") or "").strip(),
        "local_prefix": (cfg.get(prefix + "_local_prefix") or "").strip(),
        "remote_prefix": (cfg.get(prefix + "_remote_prefix") or "").strip(),
        "public_base": (cfg.get(prefix + "_public_base") or "").strip(),
        # strm 格式：auto（按第一行自动判断，默认）/ path（强制 CD2 路径型）/ pc（强制 pickcode 型）
        "strm_mode": str(cfg.get(prefix + "_strm_mode") or cfg.get("strm_mode") or "auto").lower(),
    }


def reload_all(cfg):
    """按当前 config 重建两个反代（Jellyfin / Emby）。
    启用且填了 api_key 才起；否则停掉对应端口（含上次分配的旧端口）。
    端口可随时在设置页改，热替换不会泄漏旧端口。"""
    plan = [
        ("jellyfin_enabled", "jellyfin", "jellyfin", "jellyfin_port"),
        ("emby_proxy_enabled", "emby_proxy", "emby", "emby_proxy_port"),
        # Playable（自研三位一体服务端，Jellyfin 12.x fork）：复用 jellyfin 反代通道
        # （kind=jellyfin，路径结构 + MediaBrowser 鉴权一致），仅 endpoint / 凭据不同。
        ("playable_enabled", "playable", "jellyfin", "playable_port"),
    ]
    for enabled_key, prefix, kind, port_key in plan:
        port = int(cfg.get(port_key) or 0)
        if cfg.get(enabled_key) and cfg.get(prefix + "_api_key") and port > 0:
            start_proxy(prefix, _backend_from_cfg(cfg, prefix, kind), port)
        else:
            if port > 0:
                _stop_port(port)
            with _ASSIGNED_LOCK:
                old = _ASSIGNED.get(prefix)
            if old and old != port:
                _stop_port(old)


def start_proxy(name, backend, port):
    """起一个反向代理监听（后台线程）。backend 至少含 kind/base/api_key/user_id。
    name 为播放器标识（jellyfin / emby_proxy），用于改端口时停掉该播放器旧端口，
    避免泄漏。同端口若已存在旧实例也会先停掉（热替换，设置页改端口/开关后免容器重启）。"""
    from http.server import ThreadingHTTPServer

    try:
        port = int(port)
    except (TypeError, ValueError):
        print(f"[proxy] {name} 端口无效：{port}", flush=True)
        return False
    with _ASSIGNED_LOCK:
        old = _ASSIGNED.get(name)
    if old is not None and old != port:
        _stop_port(old)
    _stop_port(port)
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", port), _make_handler(backend))
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        with _SERVERS_LOCK:
            _SERVERS[port] = (srv, t)
        with _ASSIGNED_LOCK:
            _ASSIGNED[name] = port
        print(f"[proxy] {name} 反代已监听 :{port} -> {backend['base']}", flush=True)
        return True
    except Exception as e:
        print(f"[proxy] {name} 反代启动失败（端口 {port}）：{e}", flush=True)
        return False
