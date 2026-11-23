"""EvoLink 服务：Emby 虚拟影视库 + 缺口比对。

零第三方依赖，只用标准库，方便塞进 alpline 容器。
启动: python3 server.py
"""
import base64
import csv
import datetime
import hashlib
import ipaddress
import shutil
import socket
import sqlite3
import subprocess
import hmac
import io
import json
import math
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import xml.etree.ElementTree as ET
from html import unescape

# 115 扫码登录：并发轮询时只让第一个真正去换 cookie，避免重复打 115
_QR_LOCK = threading.Lock()
_QR_DONE = {}

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from core import avres, config, db, emby, hdhub, jellyfin, matcher, pan115  # noqa: E402
from core import pan115_downurl  # noqa: E402
# EVOLINK:LICENSE:IMPORT
try:  # 授权模块：纯标准库；缺失时降级为免费版，绝不放行全部功能
    from core import license_ops  # noqa: E402
except Exception:  # pragma: no cover
    license_ops = None
# /EVOLINK:LICENSE:IMPORT
from core import pan115_swap  # noqa: E402
from core import strm_lib  # noqa: E402
from core import strm_sync  # noqa: E402
from core import strm_webhook  # noqa: E402
try:  # 新增模块（归档/权重/分类），缺失时降级但不崩
    from core import strm_classify  # noqa: E402
    from core import strm_archive  # noqa: E402
    from core import strm_weight  # noqa: E402
except Exception:  # pragma: no cover
    strm_classify = None
    strm_archive = None
    strm_weight = None
from core import downurl_panel  # noqa: E402
from core import reverse_proxy  # noqa: E402
from core import plex_proxy  # noqa: E402
from core import re0 as re0direct  # noqa: E402
from core import re0browser  # noqa: E402


# ============================= Plex 账号授权（pin 流程） =============================
# Plex 不同于 Emby/Jellyfin 的 api_key 鉴权：它用 plex.tv 账号 token（X-Plex-Token）。
# 标准做法：EvoLink 向 plex.tv 要一个 pin → 前端打开 app.plex.tv 授权页让用户登录批准
# → EvoLink 轮询 pin 收回 token。token 存配置后，即可调本地 Plex 服务器列媒体库/条目。
def _plex_create_pin(client_id):
    """向 plex.tv 申请一个授权 pin，返回 (pin_id, code, clientIdentifier)。"""
    url = ("https://plex.tv/api/v2/pins?strong=true"
           "&X-Plex-Client-Identifier=%s&X-Plex-Product=EvoLink"
           "&X-Plex-Version=1.0&X-Plex-Platform=Web&X-Plex-Device-Name=EvoLink"
           % client_id)
    req = urllib.request.Request(url, data=b"", headers={"Accept": "application/xml"})
    with urllib.request.urlopen(req, timeout=25) as r:
        xml = r.read().decode("utf-8", "replace")
    e = ET.fromstring(xml)
    return e.get("id"), e.get("code"), e.get("clientIdentifier")


def _plex_poll_pin(pin_id, client_id):
    """轮询 pin 授权结果；已批准则返回 (authToken, username)，否则返回 ('', '')。"""
    url = "https://plex.tv/api/v2/pins/%s?X-Plex-Client-Identifier=%s" % (pin_id, client_id)
    req = urllib.request.Request(url, headers={"Accept": "application/xml"})
    with urllib.request.urlopen(req, timeout=25) as r:
        xml = r.read().decode("utf-8", "replace")
    e = ET.fromstring(xml)
    token = e.get("authToken") or ""
    u = e.find("user")
    username = (u.get("username") or u.get("title") or "") if u is not None else ""
    return token, username


def _plex_list_libraries(token, base):
    """用 token 调本地 Plex 服务器列出媒体库，返回 [{key,title,type,agent,path}]。"""
    url = "%s/library/sections" % (base or "http://127.0.0.1:32400").rstrip("/")
    req = urllib.request.Request(url, headers={"X-Plex-Token": token,
                                               "Accept": "application/xml"})
    with urllib.request.urlopen(req, timeout=25) as r:
        xml = r.read().decode("utf-8", "replace")
    e = ET.fromstring(xml)
    libs = []
    for d in e.findall("Directory"):
        loc = d.find("Location")
        libs.append({
            "key": d.get("key"),
            "title": unescape(d.get("title") or ""),
            "type": d.get("type"),
            "agent": d.get("agent"),
            "path": (loc.get("path") if loc is not None else "") or "",
        })
    return libs


def _plex_media_sources(item):
    """把 Plex 的 <Media>/<Part> 压成与 jellyfin.media_sources 同构的紧凑结构，供排重用。"""
    out = []
    for md in item.findall("Media"):
        parts = md.findall("Part")
        if not parts:
            continue
        f = parts[0].get("file") or ""
        try:
            size = int(parts[0].get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        try:
            bitrate = int(md.get("bitrate") or 0)
        except (TypeError, ValueError):
            bitrate = 0
        for v in md.findall("Video"):
            try:
                h = int(v.get("height") or 0)
            except (TypeError, ValueError):
                h = 0
            out.append({
                "h": h, "b": bitrate, "r": v.get("videoRange") or "",
                "s": round(size / 1e9, 2) if size else 0,
                "c": v.get("codec") or "", "f": f[:160],
            })
            break
        else:
            out.append({"h": 0, "b": bitrate, "r": "", "s": round(size / 1e9, 2) if size else 0,
                        "c": "", "f": f[:160]})
    return out


def _plex_iter_items(section_key, token, base):
    """枚举某 Plex 媒体库的全部条目（电影/剧集），逐条 yield 与 jellyfin.iter_items 同构的 dict。

    emby_id 一律加 "plex:" 前缀：Plex 的 ratingKey 与 Emby 的整型 ID 数值区间重叠
    （实测 Emby 3482-2297975 / Plex 16261-412678），共用 owned 主键会跨源互相覆盖。
    owned.emby_id 全程按字符串处理（前缀不影响任何查询）。

    includeGuids=1：让 Plex 在列表响应里直接带上 <Guid id="imdb://...">，
    免去逐条请求（单条 metadata 要 1.7s，1 万条根本跑不动）。
    """
    base = (base or "http://127.0.0.1:32400").rstrip("/")
    start, size = 0, 1000
    while True:
        url = ("%s/library/sections/%s/all?X-Plex-Token=%s&includeGuids=1"
               "&X-Plex-Container-Start=%d&X-Plex-Container-Size=%d"
               % (base, section_key, token, start, size))
        req = urllib.request.Request(url, headers={"Accept": "application/xml"})
        with urllib.request.urlopen(req, timeout=60) as r:
            xml = r.read().decode("utf-8", "replace")
        mc = ET.fromstring(xml)
        items = mc.findall("Video") + mc.findall("Directory")
        if not items:
            break
        for it in items:
            pids = {}
            for g in it.findall("Guid"):
                gid = g.get("id") or ""
                if "://" in gid:
                    kind, val = gid.split("://", 1)
                    pids[kind.lower()] = val
            path = ""
            for mpart in it.iter("Part"):
                f = mpart.get("file")
                if f:
                    path = f
                    break
            # 剧集（Directory）本身没有 Part：取首集的播放路径，保证 path 列与其他源一致
            if not path and it.get("type") == "show":
                path = _plex_show_first_path(base, token, it.get("ratingKey"))
            thumb = it.get("thumb") or ""
            # 键名对齐 Emby/Jellyfin 侧（year_of 认 year/premiere 小写键）
            yield {
                "Id": "plex:" + (it.get("ratingKey") or ""),
                "Name": unescape(it.get("title") or ""),
                "OriginalTitle": unescape(it.get("originalTitle") or ""),
                "year": it.get("year") or "",
                "premiere": it.get("originallyAvailableAt") or "",
                "Type": it.get("type") or "",
                "Thumb": thumb,
                "ImageTags": {"Primary": thumb} if thumb else {},
                "Path": path,
                "ProviderIds": pids,
                "MediaSources": _plex_media_sources(it),
            }
        if len(items) < size:
            break
        start += size


def _plex_show_first_path(base, token, rating_key):
    """取 Plex 剧集首集的播放路径（剧集自身无 Part）。

    show → 季 → 首集 两跳，单次约 17ms；失败返回空串，不影响整库索引。
    """
    if not rating_key:
        return ""
    base = (base or "http://127.0.0.1:32400").rstrip("/")
    try:
        url = "%s/library/metadata/%s/children?X-Plex-Token=%s" % (base, rating_key, token)
        req = urllib.request.Request(url, headers={"Accept": "application/xml"})
        with urllib.request.urlopen(req, timeout=20) as r:
            xml = r.read().decode("utf-8", "replace")
        e = ET.fromstring(xml)
        seasons = [ch for ch in e if ch.get("type") == "season"]
        for season in seasons[:1]:
            surl = "%s/library/metadata/%s/children?X-Plex-Token=%s" % (
                base, season.get("ratingKey"), token)
            sreq = urllib.request.Request(surl, headers={"Accept": "application/xml"})
            with urllib.request.urlopen(sreq, timeout=20) as sr:
                sxml = sr.read().decode("utf-8", "replace")
            se = ET.fromstring(sxml)
            for ep in se:
                for mpart in ep.iter("Part"):
                    f = mpart.get("file")
                    if f:
                        return f
    except Exception:
        return ""
    return ""


# re0.me 登录态提交（书签跨域 POST）的 CORS 响应头：仅放行 re0.me 源。
_RE0_CAPTURE_CORS = {
    "Access-Control-Allow-Origin": "https://re0.me",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "600",
}

from core.sources import javdb  # noqa: E402
from core.sources import javbus  # noqa: E402
from core.sources import tmdb as tmdbsrc  # noqa: E402

WEB = BASE / "web"
# 版本号只按里程碑涨，不按改动批次涨（2026-10-20 起：1.0 是内测基线）。
# 同一版本下的每次构建由 BUILD 文件区分，格式「YYYYMMDD.N」= 构建日 + 当天第几次，
# 界面显示成「v1.0 · 20261020.3」。BUILD 由 push.sh / 部署脚本自动递增，不用手改。
APP_VERSION = "1.2"
BETA = False

# ---------------- facet 统计缓存 ----------------
# catalog 无写入时，列表页的统计（年代/类型/地区/榜单/状态/子类/番号系列）
# 直接复用上次结果。g/r/l 三个维度要对全表 27 万行做 Python 逐行 split，
# 是 /api/catalog 耗时的大头。失效策略：任何 catalog 写入点调用 facets_flush()
# 使缓存立即失效；TTL 300s 兜底（防极小概率的跨线程竞态脏读）。
_FACET_CACHE = {}
_FACET_LOCK = threading.RLock()
_CAT_VER = 0

# ── Link 清理：dup 详情里标注 strm 是否还对得上 Emby 实际库，可多选删除 ──
# 「对不上」的判定以 Emby library.db（MediaItems.Path）为权威源，
# 不拿 evolink 自己的 owned 快照当依据（快照可能超前/滞后于 Emby 实际）。
_LINK_LOCK = threading.Lock()
_LINK_EMBY_CACHE = {"ts": 0.0, "paths": None}   # Emby Path 集合缓存（读大库不轻，缓存 60s）
_LINK_SCAN_CACHE = {"ts": 0.0, "data": None}    # 闲置扫描结果缓存（TTL 300s）


# ── RE0 直连通道（core/re0.py）：浏览器操作全局串行，单例复用会话 ──
_RE0_CLIENT = None
_RE0_CLIENT_LOCK = threading.Lock()


def _re0_users(cfg):
    """身份槽表 {uid: {...}}；旧全局单槽配置自动迁移成 default 槽。"""
    users = cfg.get("re0_users")
    if not isinstance(users, dict) or not users:
        migrated = re0direct._users_map(cfg)
        users = migrated if migrated else {}
        if users:
            cfg["re0_users"] = users
    return users


def re0_client(uid="default"):
    """RE0 直连客户端：按身份槽 uid 取（每 uid 独立 cookie / 代理 / 浏览器）。"""
    return re0direct.get_re0_client(uid)


# ---------------- RE0 可视登录会话（Xvfb + x11vnc + noVNC） ----------------
# 目的：让用户在浏览器里"看着"NAS 上的真窗口 chromium 手动过 Cloudflare 验证、
# 用 Telegram 登录；登录态留在该身份的 profile 目录里，随后由 grab_cookies()
# 读出全部 cookie（含 HttpOnly，JS 读不到的那种）存进身份槽。
_VNC = {"xvfb": None, "vnc": None, "ws": None, "pwd": "", "port": 6080, "rfb": 5900}
_VNC_LOCK = threading.Lock()
_VNC_DISPLAY = ":99"


def _spawn(cmd):
    return subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)


def _port_open(port, host="127.0.0.1"):
    s = socket.socket()
    s.settimeout(0.6)
    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _xvfb_socket():
    return f"/tmp/.X11-unix/X{_VNC_DISPLAY[1:]}"


def _x_display_ok():
    """真正连一下 X socket —— 只看文件存在会被容器重启后的残留 socket 骗到。"""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(0.6)
    try:
        s.connect(_xvfb_socket())
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _vnc_missing():
    return [b for b in ("Xvfb", "x11vnc", "websockify")
            if not shutil.which(b)]


def _install_vnc_pkgs():
    """容器重建后 apt 装的包会丢，这里补装一次（经 NAS 本机代理）。"""
    proxy = (config.load().get("re0_direct_proxy") or "").strip()
    env = dict(os.environ)
    if proxy:
        env["http_proxy"] = proxy
        env["https_proxy"] = proxy
    try:
        subprocess.run(["apt-get", "update", "-qq"], env=env, timeout=180,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["apt-get", "install", "-y", "--no-install-recommends",
                        "xvfb", "x11vnc", "novnc", "websockify"], env=env,
                       timeout=420, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        return f"自动安装失败：{e}"
    return "" if not _vnc_missing() else "自动安装后仍缺：" + "/".join(_vnc_missing())


def _ensure_vnc_stack():
    """确保 Xvfb / x11vnc / websockify 都在跑。返回 (ok, err)。"""
    if _vnc_missing():
        err = _install_vnc_pkgs()
        if err:
            return False, err
    with _VNC_LOCK:
        if not _x_display_ok():
            # 清掉容器重启后可能残留的 socket / lock，再从干净状态起
            for p in (_xvfb_socket(), f"/tmp/.X{_VNC_DISPLAY[1:]}-lock"):
                try:
                    os.unlink(p)
                except Exception:
                    pass
            _VNC["xvfb"] = _spawn(f"Xvfb {_VNC_DISPLAY} -screen 0 1280x820x24 -nolisten tcp")
            for _ in range(50):
                if _x_display_ok():
                    break
                time.sleep(0.2)
            if not _x_display_ok():
                return False, "Xvfb 启动失败（虚拟屏没起来）"
        if not _port_open(_VNC["rfb"]):
            if not _VNC["pwd"]:
                _VNC["pwd"] = "".join(secrets.choice("abcdefghijkmnpqrstuvwxyz23456789")
                                      for _ in range(8))
                subprocess.run(["x11vnc", "-storepasswd", _VNC["pwd"], "/tmp/re0vnc.pass"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _VNC["vnc"] = _spawn(
                f"x11vnc -display {_VNC_DISPLAY} -rfbauth /tmp/re0vnc.pass "
                f"-rfbport {_VNC['rfb']} -localhost -forever -shared -noxdamage -repeat -quiet")
            for _ in range(50):
                if _port_open(_VNC["rfb"]):
                    break
                time.sleep(0.2)
        if not _port_open(_VNC["port"]):
            _VNC["ws"] = _spawn(
                f"websockify --web=/usr/share/novnc {_VNC['port']} localhost:{_VNC['rfb']}")
            for _ in range(50):
                if _port_open(_VNC["port"]):
                    break
                time.sleep(0.2)
        if not _port_open(_VNC["rfb"]):
            return False, "x11vnc 没起来（检查 /tmp/re0vnc.pass）"
        if not _port_open(_VNC["port"]):
            return False, "noVNC 端口 6080 没起来"
        return True, ""


def _vnc_url(host_header):
    host = (host_header or "127.0.0.1:9191").split(":")[0]
    return (f"http://{host}:{_VNC['port']}/vnc.html?autoconnect=1&resize=scale"
            f"&reconnect=1&path=websockify&password={_VNC['pwd']}")


def _cid_media(d):
    """catalog 行 → (media, tmdb_id, title, category)。AV/里番返回空 media。"""
    media = d.get("media") or ""
    if media not in ("tv", "movie"):
        try:
            media = json.loads(d.get("extra") or "{}").get("media") or ""
        except Exception:
            media = ""
    if media not in ("tv", "movie"):
        media = "tv" if d.get("category") in ("剧集", "综艺") else "movie"
    tmdb_id = d.get("tmdb_id") or ""
    try:
        tmdb_id = int(tmdb_id)
    except (TypeError, ValueError):
        tmdb_id = 0
    return media, tmdb_id, (d.get("title") or ""), (d.get("category") or "")


def _link_cfg():
    """(strm目录, 回收站目录, Emby路径前缀)。路径全部可配，各用户挂载不同。"""
    cfg = config.load()
    ld = (cfg.get("link_dir") or "/app/Link").rstrip("/") or "/app/Link"
    td = (cfg.get("link_trash_dir") or "").strip() or str(config.DATA / "strm_trash")
    pref = cfg.get("link_emby_prefix") or "/Link/"
    return ld, td, pref


def _emby_db_path():
    """Emby library.db 的真实路径（兼容多种挂载布局）。

    Emby 是 WAL 模式：只挂 library.db 主文件，-wal 里未 checkpoint 的事务
    一律读不到（拿到的是旧快照：新收录的影片查不到、刚删的条目还在）。
    所以优先用「整个 data 目录」挂载（/emby_data/library.db，能连 -wal/-shm
    一起读）；兼容只挂了单文件的老布局（/emby_library.db）；用户自定义路径优先。
    """
    cfgp = (config.load().get("emby_library_db") or "").strip()
    if cfgp and cfgp not in ("/emby_data/library.db", "/emby_library.db"):
        return cfgp                                    # 自定义路径，原样返回（不存在就报错用）
    for p in ("/emby_data/library.db", "/emby_library.db"):
        if os.path.exists(p):
            return p
    return cfgp or "/emby_library.db"


def _link_emby_paths():
    """Emby library.db 里全部 .strm 路径集合。读不到返回 (None, 给人看的错误)。"""
    dbp = _emby_db_path()
    if not dbp or not os.path.exists(dbp):
        return None, ("Emby 库 DB 没挂载（当前配置：%s）。\n"
                      "请在 compose 的 volumes 加一行（各用户路径不同，按实际改左边）：\n"
                      "  - <你的Emby数据目录>/data:/emby_data:ro\n"
                      "⚠️ 要挂整个 data 目录、不能只挂 library.db：Emby 是 WAL 模式，\n"
                      "只挂主文件读不到 -wal 里的最新事务，会拿到旧快照。\n"
                      "「是否还对得上库」以这个库为权威判定，不挂就不能标注/删除。"
                      % (dbp or "未配置"))
    with _LINK_LOCK:
        if _LINK_EMBY_CACHE["paths"] is not None and time.time() - _LINK_EMBY_CACHE["ts"] < 60:
            return _LINK_EMBY_CACHE["paths"], ""
    try:
        c = sqlite3.connect("file:%s?mode=ro" % dbp, uri=True)
        c.execute("PRAGMA busy_timeout=5000")
        tbls = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        tbl = "MediaItems" if "MediaItems" in tbls else ("BaseItems" if "BaseItems" in tbls else None)
        if not tbl:
            c.close()
            return None, "Emby 库里找不到 MediaItems / BaseItems 表，无法核对"
        paths = {r[0] for r in c.execute("SELECT Path FROM %s WHERE Path LIKE '%%.strm'" % tbl) if r[0]}
        c.close()
        with _LINK_LOCK:
            _LINK_EMBY_CACHE.update(ts=time.time(), paths=paths)
        return paths, ""
    except Exception as e:
        return None, "读 Emby 库失败：%s" % e


def _emby_ids_for_paths(paths):
    """Emby 视角路径 -> [条目ID]（MediaItems.Path 精确匹配）。

    删完 strm 后通知 Emby 用：Emby 的 API 条目 ID 就是 MediaItems.Id
    （实测按 Path 查 API 返回的 Id 与库表一致），所以一次 SQL 全批反查，
    不用逐条打 API。库没挂/读失败返回 (None, 错误信息)。
    """
    dbp = _emby_db_path()
    if not dbp or not os.path.exists(dbp):
        return None, "Emby 库 DB 没挂载（%s）" % dbp
    out = {}
    try:
        c = sqlite3.connect("file:%s?mode=ro" % dbp, uri=True)
        c.execute("PRAGMA busy_timeout=5000")
        tbls = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        tbl = "MediaItems" if "MediaItems" in tbls else ("BaseItems" if "BaseItems" in tbls else None)
        if not tbl:
            c.close()
            return None, "Emby 库里找不到 MediaItems / BaseItems 表"
        paths = [p for p in paths if p]
        for i in range(0, len(paths), 400):           # SQLite 变量数上限，分批
            chunk = paths[i:i + 400]
            q = "SELECT Path, Id FROM %s WHERE Path IN (%s)" % (tbl, ",".join("?" * len(chunk)))
            for p, iid in c.execute(q, chunk):
                if p and iid is not None and str(iid).strip():
                    out.setdefault(p, []).append(str(iid).strip())
        c.close()
        return out, ""
    except Exception as e:
        return None, "读 Emby 库失败：%s" % e


def _emby_notify_deleted(paths):
    """删完 strm 后同步 Emby 媒体库：按 Path 反查条目 ID，逐条删 + 复核。

    - Emby 对不存在的 ID 也返 204，所以每条删完都用 GET 复核（404 才算真删掉）；
    - Emby 连不上 / 没配 Key / 没挂库：不阻塞文件删除（文件已进回收站），
      原样带回错误信息，前端提示用户去 Emby 手动处理。
    """
    out = {"removed": 0, "not_in_lib": 0, "failed": []}
    srv = config.emby_primary()
    if not (srv.get("url") or "").strip() or not (srv.get("api_key") or "").strip():
        out["error"] = "Emby 服务器或 API Key 未配置（设置页 → EMBY 服务器），已跳过通知"
        return out
    id_map, err = _emby_ids_for_paths(paths)
    if id_map is None:
        out["error"] = err + "，已跳过通知"
        return out
    try:
        uid = emby.admin_user_id()
    except Exception as e:
        out["error"] = "取 Emby 用户失败：%s（无法复核删除结果，已跳过通知）" % e
        return out
    if not uid:
        out["error"] = "Emby 上没有可用用户（无法复核删除结果，已跳过通知）"
        return out
    for p in paths:
        ids = id_map.get(p) or []
        if not ids:
            out["not_in_lib"] += 1            # Emby 库里本来就没有，没啥可通知
            continue
        done, why = False, ""
        for iid in ids:
            try:
                st, _ = emby.delete_item(iid)
                if st >= 300:
                    why = "Emby 删除接口返回 %s" % st
                    continue
                if emby.item_exists(iid, uid):
                    why = "Emby 接口返回成功但条目仍在库里"
                    continue
                done = True
            except Exception as e:
                why = str(e)
        if done:
            out["removed"] += 1
        else:
            out["failed"].append({"p": p, "e": why or "未知原因"})
    return out


_STATS_CACHE = {}
_STATS_LOCK = threading.RLock()

def facets_flush():
    global _CAT_VER
    with _FACET_LOCK:
        _CAT_VER += 1



def _read_build():
    try:
        return (BASE / "BUILD").read_text("utf-8").strip()
    except Exception:
        return ""


BUILD = _read_build()

# 封面代理用：几个图源有防盗链，缺 Referer 一律被拒（AV 库 16 万张 javbus 缩略图、豆瓣海报）
_IMG_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")


def fix_poster_url(url):
    """把失效的封面地址纠正成当前还能取到的那个。

    javdb 早期的缩略图是 `/pics/thumb/xxxx.jpg`（用的是 javbus 那套哈希），后来这批对象
    在 c0.jdbstatic.com 上被清掉了，现在一律回 S3 AccessDenied（403）——「很多封面不显示」
    就是这个原因。同样的哈希在 www.javbus.com 上还在，所以只换主机、路径不动。
    """
    m = re.match(r"^https?://[a-z0-9.-]*jdbstatic\.com(/pics/thumb/.+)$", url or "", re.I)
    if m:
        return "https://www.javbus.com" + m.group(1)
    return url or ""


def _ip_blocked(ip):
    """内网 / 回环 / 链路本地 / 保留 / 多播 / 未指定地址都算「禁止外发」目标。"""
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _poster_host_blocked(url):
    """SSRF 防护：/api/poster 免登录，必须对目标地址做限制，防止服务端被当成
    跳板打内网（路由器/Emby/Plex 管理面）。拒绝：非 http(s)、字面私有/回环 IP、
    localhost、以及解析后落到私有/回环段的域名（防 DNS rebinding 兜底；解析失败放行，
    由 10MB 上限 + image 内容类型校验兜底，避免误杀正常外网图）。
    """
    try:
        p = urllib.parse.urlparse(url or "")
    except Exception:
        return True
    if p.scheme not in ("http", "https"):
        return True
    host = (p.hostname or "").strip().lower()
    if not host:
        return True
    if host in ("localhost", "localhost.localdomain", "0.0.0.0", "::1", "[::1]"):
        return True
    # 字面 IP 直接判
    try:
        if _ip_blocked(ipaddress.ip_address(host)):
            return True
    except ValueError:
        pass
    # 解析后判（best-effort）
    try:
        for info in socket.getaddrinfo(host, None):
            try:
                if _ip_blocked(ipaddress.ip_address(info[4][0])):
                    return True
            except ValueError:
                pass
    except Exception:
        pass
    return False


def _img_headers(url):
    h = {"User-Agent": _IMG_UA}
    u = url or ""
    if "javbus.com" in u:
        h["Referer"] = "https://www.javbus.com/"
    elif "jdbstatic.com" in u or "javdb.com" in u:
        h["Referer"] = "https://javdb.com/"
    elif "doubanio.com" in u or "douban.com" in u:
        h["Referer"] = "https://movie.douban.com/"
    elif "anilist.co" in u:
        h["Referer"] = "https://anilist.co/"
    return h


def poster_cache_path(url):
    """封面磁盘缓存路径 (img, ct)。没配缓存目录时返回 (None, None)。"""
    try:
        cdir = config.cache_dir()
    except Exception:
        return None, None
    key = hashlib.sha1((url or "").encode("utf-8")).hexdigest()
    return cdir / (key + ".img"), cdir / (key + ".ct")


def fetch_poster_bytes(url, timeout=30, use_proxy=True, max_bytes=None):
    """抓一张封面，返回 (data, ctype)。网页接口和「预热封面」共用同一套请求头与代理。

    timeout 给到 30 秒是因为代理节点冷启动时第一条 CONNECT 的 TLS 握手可能拖到 20 秒以上，
    20 秒会把本来能成功的图判成失败（用户侧表现就是「封面转圈然后空白」）。
    max_bytes 限制读取上限（SSRF/资源滥用兜底），超过即抛 IOError。
    """
    proxy = config.proxy_url() if use_proxy else None
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy})) if proxy else urllib.request.build_opener()
    req = urllib.request.Request(url, headers=_img_headers(url))
    with opener.open(req, timeout=timeout) as r:
        ctype = r.headers.get("Content-Type", "image/jpeg")
        if max_bytes:
            data = r.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise IOError("poster too large (>%d bytes)" % max_bytes)
        else:
            data = r.read()
        return data, ctype


_POOL = threading.local()


def fetch_poster_pooled(url, timeout=15):
    """预热专用：每个线程复用一条到代理的 TLS 长连接。

    逐张新建连接时，每张都要重做一次 TLS 握手；走境外节点时这一步要 4-6 秒，
    比图片本身（十几 KB）贵几十倍。复用后同样的图降到 1.5-2 秒。
    连接出错就丢掉重建，下一次调用自动恢复。
    """
    import http.client
    pu = urllib.parse.urlparse(config.proxy_url() or "")
    t = urllib.parse.urlparse(url)
    if t.scheme != "https":
        return fetch_poster_bytes(url, timeout=timeout)
    key = (t.netloc, pu.netloc)
    conn = getattr(_POOL, "conn", None)
    if conn is None or getattr(_POOL, "key", None) != key:
        conn = http.client.HTTPSConnection(pu.hostname, pu.port or 80, timeout=timeout)
        conn.set_tunnel(t.hostname, t.port or 443)
        _POOL.conn, _POOL.key = conn, key
    try:
        conn.request("GET", (t.path or "/") + (("?" + t.query) if t.query else ""),
                     headers=_img_headers(url))
        r = conn.getresponse()
        data = r.read()
        ctype = r.getheader("Content-Type") or "image/jpeg"
        if r.status != 200:
            raise IOError("HTTP %s" % r.status)
        return data, ctype
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        _POOL.conn, _POOL.key = None, None
        raise


def store_poster(url, data, ctype):
    """把抓到的图落盘（先写 .tmp 再改名，避免半张图被当成有效缓存）。"""
    f, m = poster_cache_path(url)
    if not f or not data:
        return
    try:
        tmp = f.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(f)
        m.write_text(ctype or "image/jpeg", "utf-8")
    except Exception:
        pass
TASKS = {}          # name -> {running, log[], started, finished, error}
TASK_THREADS = {}   # name -> Thread，用来判断「上一个任务是不是真的还活着」
RES_CACHE = {}      # cid -> (ts, payload) 资源查询缓存
AVLIB_CACHE = {"t": 0, "d": None}
CATLIB_CACHE = {}    # category -> (ts, data)，普通分类本地库统计
EMBY_LIBS_CACHE = {}  # server_id -> (ts, {name,version,libs,ok,error})，设置页库清单
_LOCK = threading.Lock()
_LOCAL = threading.local()   # 每个任务线程记住自己叫什么，供 prog() / src_prog() 定位


def av_lib_stats(ttl=120):
    """本地 AV 库统计（走 owned，跟发行片 catalog 无关），120 秒缓存。"""
    if AVLIB_CACHE["d"] and time.time() - AVLIB_CACHE["t"] < ttl:
        return AVLIB_CACHE["d"]
    d = db.av_library()
    AVLIB_CACHE["t"] = time.time()
    AVLIB_CACHE["d"] = d
    return d


def cat_lib_stats(category, ttl=120):
    """电影/剧集/动画/纪录片/综艺/演唱会的本地库统计，按分类各自缓存。"""
    cat = (category or "").strip()
    if cat not in db.CAT_BUCKETS or cat in ("AV", "里番"):
        return None
    hit = CATLIB_CACHE.get(cat)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    d = db.cat_library(cat)
    CATLIB_CACHE[cat] = (time.time(), d)
    return d


def clear_lib_caches():
    """改了库里内容 / 改了 Emby 库映射后，把本地库口径的缓存全部作废。"""
    AVLIB_CACHE["t"] = 0
    AVLIB_CACHE["d"] = None
    CATLIB_CACHE.clear()

DEFAULT_SPECS = [
    {"category": "电影", "media": "movie", "total": 400, "sort": "popularity.desc"},
    {"category": "剧集", "media": "tv", "total": 400, "sort": "popularity.desc"},
    {"category": "动画", "media": "both", "total": 300, "sort": "popularity.desc", "with_genres": "16"},
    {"category": "纪录片", "media": "both", "total": 150, "sort": "vote_average.desc", "with_genres": "99"},
    {"category": "综艺", "media": "tv", "total": 120, "sort": "popularity.desc", "with_genres": "10764|10767|10763"},
    {"category": "演唱会", "media": "movie", "total": 80, "sort": "popularity.desc", "with_genres": "10402"},
]

# 全量拉取：把每类的目标条数抬到「基本覆盖一个家庭库的规模」。
# 代价参考：1 万条 ≈ 500 次请求 ≈ 3-5 分钟（走代理更慢），整套跑完约 20-40 分钟。
FULL_SPECS = [
    {"category": "电影", "media": "movie", "total": 20000, "sort": "popularity.desc"},
    {"category": "剧集", "media": "tv", "total": 12000, "sort": "popularity.desc"},
    {"category": "动画", "media": "both", "total": 4000, "sort": "popularity.desc", "with_genres": "16"},
    {"category": "纪录片", "media": "both", "total": 2000, "sort": "vote_average.desc", "with_genres": "99"},
    {"category": "综艺", "media": "tv", "total": 1500, "sort": "popularity.desc", "with_genres": "10764|10767|10763"},
    {"category": "演唱会", "media": "movie", "total": 1000, "sort": "popularity.desc", "with_genres": "10402"},
]
FULL_AV_PAGES = 500          # 「最新发行」「最新字幕」各 500 页 ≈ 2 万个番号
FULL_HENTAI_PAGES = 200      # AniList 每页 50，200 页×3 种排序去重后接近平台上限 5000

AUTO_DEFAULTS = {
    "enabled": False,
    "mode": "daily",        # daily = 每天定时；manual = 只手动
    "time": "00:00",        # 每日触发时间（本地时区）
    "sync_emby": True,
    "fetch": True,
    "fetch_av": True,
    "fetch_hentai": True,
    "fetch_lists": True,
    "match": True,
    "hours": 24,            # 兼容旧配置，不再使用
}


# ---------------- 后台任务 ----------------
def run_task(name, fn):
    """后台起一个任务。名字必须和前端轮询用的显示名一致（如「索引 Emby」）。"""
    with _LOCK:
        cur = TASKS.get(name)
        if cur and cur.get("running"):
            th = TASK_THREADS.get(name)
            if th is not None and th.is_alive():
                return False
            # 线程已经死了但状态没清（异常退出 / 被杀），当作可重跑，避免永久「任务已在运行中」
            cur["running"] = False
        TASKS[name] = {"running": True, "log": [], "started": db.now(), "finished": None,
                       "error": None, "progress": {"done": 0, "total": 0, "label": ""}}

    def log(msg):
        t = TASKS.get(name)
        if not t:
            return
        s = str(msg)
        t["log"].append(s)
        del t["log"][:-300]
        # 进度条：默认把最新一行当日志标签；行里出现 "3/10" 这类数字就顺手当比例
        pr = t.setdefault("progress", {"done": 0, "total": 0, "label": ""})
        pr["label"] = s[-120:]
        m = re.search(r"(\d{1,6})\s*/\s*(\d{1,6})", s)
        if m:
            d0, d1 = int(m.group(1)), int(m.group(2))
            if 0 < d1 <= 200000 and d0 <= d1:
                pr["done"], pr["total"] = d0, d1

    def wrapped():
        ok = False
        try:
            _LOCAL.name = name
            # 串行化后台重活写事务，避免与别的任务抢写锁导致 database is locked
            with db.write_lock():
                fn(log)
            ok = True
        except Exception as e:
            TASKS[name]["error"] = f"{type(e).__name__}: {e}"
            log(f"✗ {e}")
        finally:
            TASKS[name]["running"] = False
            TASKS[name]["finished"] = db.now()
            _LOCAL.name = ""
            resume_done(name, ok)

    th = threading.Thread(target=wrapped, daemon=True)
    TASK_THREADS[name] = th
    th.start()
    return True


def prog(done=0, total=0, label=None):
    """任务内部主动上报进度（比解析日志准）。没有在任务线程里调用时静默忽略。"""
    name = getattr(_LOCAL, "name", "")
    t = TASKS.get(name or "")
    if not t:
        return
    pr = t.setdefault("progress", {"done": 0, "total": 0, "label": ""})
    pr["done"], pr["total"] = max(0, int(done)), max(0, int(total))
    if label is not None:
        pr["label"] = str(label)[:120]


# 每个数据源一条独立进度：前端「数据更新」面板里一源一行进度条
SRC_KEYS = ("emby", "jellyfin", "playable", "plex", "tmdb", "av", "avbus", "hentai", "lists", "match", "posters")
SRC_PROG = {k: {"key": k, "done": 0, "total": 0, "msg": "", "running": False,
                "ok": None, "at": ""} for k in SRC_KEYS}


def src_prog(key, done=None, total=None, msg=None, running=None, ok=None):
    p = SRC_PROG.get(key)
    if not p:
        return
    if done is not None:
        p["done"] = max(0, int(done))
    if total is not None:
        p["total"] = max(0, int(total))
    if msg is not None:
        p["msg"] = str(msg)[:120]
    if running is not None:
        p["running"] = bool(running)
    if ok is not None:
        p["ok"] = bool(ok)
    p["at"] = db.now()


def src_run(key, fn):
    """包一层：跑之前置 running，结束后置 ok / 记录异常。"""
    src_prog(key, 0, 0, "开始…", running=True, ok=None)
    try:
        r = fn()
        src_prog(key, running=False, ok=True, msg="完成")
        return r
    except Exception as e:
        src_prog(key, running=False, ok=False, msg=f"{type(e).__name__}: {e}"[:120])
        raise


# ---------------- 「比对」面板：按库进度 + 重复扫描结果 ----------------
# 与 SRC_PROG（固定 8 个数据源）分开：这里 key 是 Emby 库名，由用户在面板里勾选，
# 不落库（重复扫描是秒级只读操作，结果重启即失效也无所谓）。
LIB_PROG = {}                 # {库名: {"done","total","msg","running","ok"}}
LIB_PROG_LK = threading.Lock()
DUP_RESULT = {}               # {库名: {"groups","summary","scope"}}


def lib_prog_reset(libs):
    with LIB_PROG_LK:
        LIB_PROG.clear()
        for l in libs:
            LIB_PROG[l] = {"done": 0, "total": 0, "msg": "等待", "running": False, "ok": None}


def lib_prog_set(lib, **kw):
    with LIB_PROG_LK:
        p = LIB_PROG.setdefault(lib, {"done": 0, "total": 0, "msg": "", "running": False, "ok": None})
        p.update(kw)


def lib_prog_snapshot():
    with LIB_PROG_LK:
        return {k: dict(v) for k, v in LIB_PROG.items()}


# ---------------- 断点续跑（容器重启后自动接着干） ----------------
# 机制：任务开跑前把「任务名 + 参数」写进 data/tasks.json（原子写）；正常跑完就删掉这条。
# 容器重启 / 被杀时来不及清理 → 记录留在文件里 → 下次启动自动重跑。
# 重跑是安全的：TMDB/AV/里番/榜单都有游标（cursor），预热封面按缓存跳过，比对是幂等的。
RESUME_FILE = config.DATA / "tasks.json"
RESUME_MAX_TRIES = 3                     # 同一个任务连续失败 3 次就不再自动重试
RESUME_ORDER = ("索引 Emby", "索引 Jellyfin", "索引 Playable", "索引 Plex", "全量拉取", "拉取发行片", "拉取榜单", "拉取 AV 番号",
                "拉取 AV 全史", "拉取里番", "统计发行量", "预热封面", "比对")
_RESUME_LK = threading.Lock()

# 英文页用英文任务名轮询 /api/task，而后端任务表以中文名为键。
# 这里把英文名映射到中文任务名，让英文页的进度/日志能正常显示（索引媒体库多选队列也依赖它）。
_TASK_ALIAS = {
    "Index Emby": "索引 Emby",
    "Index Jellyfin": "索引 Jellyfin",
    "Index Playable": "索引 Playable",
    "Index Plex": "索引 Plex",
    "Fetch releases": "拉取发行片",
    "Fetch AV Codes": "拉取 AV 番号",
    "Fetch AV full history": "拉取 AV 全史",
    "Fetch anime": "拉取里番",
    "Fetch charts": "拉取榜单",
    "Count releases": "统计发行量",
    "Warm posters": "预热封面",
    "Full fetch": "全量拉取",
    "Compare": "比对",
    "Update Now": "立即更新",
}


def resume_load():
    try:
        d = json.loads(RESUME_FILE.read_text("utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _resume_write(d):
    try:
        tmp = RESUME_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, RESUME_FILE)
    except Exception:
        pass


def resume_add(name, params):
    """登记一个「跑起来但还没跑完」的任务。只登记可续跑的任务。"""
    if name not in RESUME_ORDER:
        return
    with _RESUME_LK:
        d = resume_load()
        old = d.get(name) if isinstance(d.get(name), dict) else {}
        d[name] = {"params": params or {}, "at": db.now(),
                   "tries": int(old.get("tries") or 0)}
        _resume_write(d)


def resume_done(name, ok):
    """任务结束：成功就销账；失败就记一次，连续失败到上限后不再自动重试。"""
    if name not in RESUME_ORDER:
        return
    with _RESUME_LK:
        d = resume_load()
        if name not in d:
            return
        if ok:
            d.pop(name, None)
        else:
            d[name]["tries"] = int(d[name].get("tries") or 0) + 1
            d[name]["err_at"] = db.now()
            if d[name]["tries"] >= RESUME_MAX_TRIES:
                d.pop(name, None)          # 连续失败（比如没索引 Emby）就别再刷了
        _resume_write(d)


def build_task(name, p):
    """按「任务名 + 参数」重建任务函数，供重启续跑使用。"""
    p = p or {}
    if name == "索引 Emby":
        return task_sync_emby
    if name == "索引 Jellyfin":
        return task_sync_jellyfin
    if name == "索引 Playable":
        return task_sync_playable
    if name == "索引 Plex":
        return task_sync_plex
    if name == "拉取发行片":
        return lambda log: task_fetch_catalog(log, p.get("specs"), bool(p.get("incremental")))
    if name == "拉取榜单":
        return lambda log: task_fetch_list(log, p.get("keys"), int(p.get("limit") or 40))
    if name == "拉取 AV 番号":
        return lambda log: task_fetch_av(log, p.get("pages"), p.get("modes"),
                                         bool(p.get("incremental")))
    if name == "拉取 AV 全史":
        return lambda log: task_fetch_avbus(log, p.get("year_floor"), p.get("min_items"),
                                            bool(p.get("resume", True)), p.get("prefixes"))
    if name == "拉取里番":
        return lambda log: task_fetch_hentai(log, p.get("pages"), bool(p.get("incremental")))
    if name == "统计发行量":
        return lambda log: task_fetch_counts(log, bool(p.get("resume", True)))
    if name == "预热封面":
        return lambda log: task_warm_posters(log, p.get("scope") or "av", p.get("category") or "",
                                             int(p.get("limit") or 0), int(p.get("workers") or 12),
                                             int(p.get("offset") or 0))
    if name == "全量拉取":
        return task_fetch_full
    if name == "比对":
        return lambda log: task_match(log, p.get("libs"))
    if name == "扫描重复":
        return lambda log: task_dup_scan(log, p.get("libs"))
    return None


def start_task(name, params=None):
    """统一入口：登记续跑记录 + 起任务。returns run_task 的布尔值。"""
    fn = build_task(name, params)
    if fn is None:
        return False
    resume_add(name, params or {})
    ok = run_task(name, fn)
    if not ok:
        # 已经在跑：记录留着（它跑完会自己销账），不影响当前任务
        pass
    return ok


# ---- 上传 .strm 自动分析（换链页）----
_STRM_ANALYZE_LOCK = threading.Lock()
_STRM_ANALYZE_LAST_AT = 0.0
_STRM_ANALYZE_INTERVAL = 20.0        # 试签限流：两次试签至少间隔 20s
_STRM_ERRNO_CN = {
    50015: "文件不存在或已删除——该 pickcode 不在当前登录的 115 账号（115 的 pickcode 按账号隔离，要用朋友自己的账号登录才签得到）",
    99: "115 登录态失效，请重新扫码登录",
    201: "参数错误",
    401: "未授权（登录态或风控）",
    403: "被 115 拒绝（可能触发风控），稍后再试",
    429: "请求过于频繁（115 限流），稍后再试",
}


def _first_line(text):
    for raw in (text or "").split("\n"):
        line = raw.strip()
        if line:
            return line
    return ""


def _analyze_prefixes(cfg):
    """借用已配置反代的「迁移目录」前缀，让分析/试签与真实播放行为一致。

    优先 Jellyfin，其次 Emby，最后 Plex；都没配就用空（按原样路径走）。
    """
    for p in ("jellyfin", "emby_proxy", "plex"):
        lp = (cfg.get(p + "_local_prefix") or "").strip()
        rp = (cfg.get(p + "_remote_prefix") or "").strip()
        if lp or rp:
            return {"local_prefix": lp, "remote_prefix": rp}
    return {}


def api_strm_analyze(body):
    """解析上传的 strm 第一行 → 结构化结果；test=1 时用当前账号试签一次。"""
    content = body.get("content") or ""
    if isinstance(content, bytes):
        content = content.decode("utf-8", "ignore")
    line = _first_line(str(content))[:1000]
    cfg = config.load()
    backend = {"strm_mode": str(cfg.get("strm_mode") or "auto").lower()}
    backend.update(_analyze_prefixes(cfg))
    info = reverse_proxy.strm_line_info(line, backend)
    out = {"ok": True, "line": line[:300], "info": info,
           "logged_in": bool((cfg.get("pan115_cookie") or {}).get("UID"))}
    if not (body.get("test") and info.get("ok") and info.get("rel")):
        return out
    # ---- 试签（铁律：冷却期不签、限流、绝不自动重试）----
    global _STRM_ANALYZE_LAST_AT
    st = downurl_panel.status()
    if st.get("cooling"):
        out["test"] = {"attempted": False, "ok": False,
                       "msg": "115 风控冷却中，暂停试签（约 10 分钟后自动恢复）"}
        return out
    now = time.time()
    with _STRM_ANALYZE_LOCK:
        if now - _STRM_ANALYZE_LAST_AT < _STRM_ANALYZE_INTERVAL:
            out["test"] = {"attempted": False, "ok": False,
                           "msg": "操作过于频繁，请 %ds 后再试" % int(
                               _STRM_ANALYZE_INTERVAL - (now - _STRM_ANALYZE_LAST_AT))}
            return out
        _STRM_ANALYZE_LAST_AT = now
    ua = "evolink-strm-analyze"
    try:
        url = pan115_downurl.downurl_url(info["rel"], ua=ua)
        fetch_ok, fetch_msg = downurl_panel._probe_bytes(url, ua)
        out["test"] = {"attempted": True, "ok": bool(fetch_ok), "errno": 0,
                       "msg": "试签成功，自检%s" % ("通过（直链可用）" if fetch_ok else "失败：%s" % fetch_msg),
                       "url": url[:200], "url_host": urllib.parse.urlparse(url).netloc,
                       "fetch_ok": fetch_ok}
    except pan115_downurl.DownurlError as e:
        msg = str(e)
        import re as _re
        m = _re.search(r"errno=(\d+)", msg)
        errno = int(m.group(1)) if m else None
        cn = _STRM_ERRNO_CN.get(errno)
        out["test"] = {"attempted": True, "ok": False, "errno": errno,
                       "msg": (cn + "（原始返回：" + msg + "）") if cn else msg}
    except Exception as e:  # 网络异常等：不重试，原样上报
        out["test"] = {"attempted": True, "ok": False, "errno": None,
                       "msg": "试签请求失败：%s %s" % (type(e).__name__, e)}
    return out


def resume_all(delay=25):
    """启动后把上次没跑完的任务按顺序续上。"""
    time.sleep(delay)
    cfg = config.load()
    if not cfg.get("auto_resume", True):
        print("[续跑] 已关闭（设置里把「重启后自动续跑」关掉即可）")
        return
    pend = resume_load()
    if not pend:
        return
    # 有「全量拉取」时跳过它包含的子任务，避免同一批数据跑两遍
    if "全量拉取" in pend:
        for k in ("拉取发行片", "拉取 AV 番号", "拉取里番", "比对"):
            pend.pop(k, None)
    names = [n for n in RESUME_ORDER if n in pend]
    print(f"[续跑] 发现 {len(names)} 个未完成任务：{'、'.join(names)}")
    for n in names:
        p = (pend.get(n) or {}).get("params") or {}
        try:
            fn = build_task(n, p)
            if fn is None:
                continue
            if run_task(n, fn):
                print(f"[续跑] 已启动：{n}")
                src_prog(_SRC_OF.get(n, ""), 0, 0, "重启后自动续跑…", running=True, ok=None)
            else:
                print(f"[续跑] 跳过（已在运行）：{n}")
            time.sleep(3)               # 一个接一个起，别在开机瞬间把 CPU/代理打满
        except Exception as e:
            print(f"[续跑] {n} 启动失败：{type(e).__name__}: {e}")


_SRC_OF = {"索引 Emby": "emby", "索引 Jellyfin": "jellyfin", "索引 Playable": "playable", "索引 Plex": "plex",
           "拉取发行片": "tmdb", "拉取榜单": "lists",
           "拉取 AV 番号": "av", "拉取 AV 全史": "avbus", "拉取里番": "hentai",
           "统计发行量": "tmdb", "预热封面": "posters", "比对": "match",
           "全量拉取": "tmdb"}



def task_sync_emby(log):
    from tools import sync_emby
    log("开始索引 Emby 库存…")
    libs = emby.libraries()
    log(f"发现 {len(libs)} 个库")
    total = 0
    failed = []
    c = db.init()
    todo = [l for l in libs if l["type"] not in ("livetv", "playlists", "boxsets")]
    src_prog("emby", 0, len(todo), "准备…", running=True, ok=None)
    for i, lib in enumerate(todo):
        # 单库失败不中止整个索引：大库超时 / 个别条目脏数据只影响这一个库，
        # 其余照常入库，最后统一汇报，重跑「索引 Emby」即可补齐。
        try:
            cat = sync_emby.category_of(lib["name"])
            src_prog("emby", i, len(todo), lib["name"])
            rows, n, pulled_ids = [], 0, []
            for it in emby.iter_items(lib["id"]):
                pi = {k.lower(): v for k, v in (it.get("ProviderIds") or {}).items()}
                name = it.get("Name") or ""
                orig = it.get("OriginalTitle") or ""
                path = it.get("Path") or ""
                tags = it.get("ImageTags") or {}
                rows.append((it["Id"], name, orig, matcher.year_of(it), it.get("PremiereDate") or "",
                             it.get("Type") or "", lib["id"], lib["name"],
                             pi.get("tmdb") or "", pi.get("imdb") or "", pi.get("tvdb") or "",
                             1 if tags.get("Primary") else 0, tags.get("Primary") or "", path,
                             matcher.normalize(name),
                             matcher.extract_av_code(name, orig, path) if cat == "AV" else "",
                             json.dumps(emby.media_sources(it), ensure_ascii=False) if it.get("MediaSources") else "",
                             db.now()))
                n += 1
                pulled_ids.append(it["Id"])
                if len(rows) >= 500:
                    sync_emby._flush(c, rows)
                    rows = []
            if rows:
                sync_emby._flush(c, rows)
            c.commit()
            stale = sync_emby._prune_library(c, lib["id"], lib["name"], pulled_ids)
            c.commit()
            total += n
            log(f"✓ {lib['name']} [{cat}] {n} 条" + (f"，清理失效 {stale} 条" if stale else ""))
            src_prog("emby", i + 1, len(todo), f"{lib['name']} · {n} 条")
        except Exception as e:
            failed.append(lib["name"])
            log(f"✗ {lib['name']} 失败：{e}（其余库继续）")
    if todo and not failed:
        # 库级清理：Emby 里已整个消失的库，残留行一并删（全部库拉取成功才做，防误删）。
        # ⚠️ 必须按 src='emby' 限定，否则会把 jellyfin/playable 来源的 owned 行一起删掉。
        keep = ",".join("?" * len(todo))
        k = c.execute(f"DELETE FROM owned WHERE src='emby' AND lib_id NOT IN ({keep})",
                      [l["id"] for l in todo]).rowcount
        c.commit()
        if k:
            log(f"库级清理：移除 {k} 条已不存在库的残留")
    db.set_meta("owned_synced_at", db.now())
    AVLIB_CACHE["d"] = None          # 库存变了，统计缓存全部失效
    CATLIB_CACHE.clear()
    if failed:
        log(f"完成，共 {total} 条；{len(failed)} 个库失败：{'、'.join(failed)} —— 重跑「索引 Emby」可补齐")
        src_prog("emby", len(todo), len(todo),
                 f"共 {total} 条，{len(failed)} 个库失败", running=False, ok=False)
    else:
        log(f"完成，共 {total} 条")
        src_prog("emby", len(todo), len(todo), f"共 {total} 条", running=False, ok=True)


def _flush_owned(c, rows, src):
    """把一批条目写进 owned，带 src 来源标记。rows 为 18 列元组（与 owned 现有列序一致），src 单独传入。

    ON CONFLICT(emby_id)：Emby 用整型 ID、Jellyfin/Playable 用 GUID，三方 ID 命名空间互不重叠，
    故同一 PK 不会跨源误覆盖；src 列仅用于过滤与清理，确保各源互不干扰。
    """
    c.executemany("""
        INSERT INTO owned (emby_id,name,original_title,year,premiere,type,lib_id,library,
                           tmdb_id,imdb_id,tvdb_id,has_image,image_tag,path,norm,av_code,medias,updated_at,src)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(emby_id) DO UPDATE SET
          name=excluded.name, original_title=excluded.original_title, year=excluded.year,
          premiere=excluded.premiere, type=excluded.type, lib_id=excluded.lib_id,
          library=excluded.library, tmdb_id=excluded.tmdb_id, imdb_id=excluded.imdb_id,
          tvdb_id=excluded.tvdb_id, has_image=excluded.has_image, image_tag=excluded.image_tag,
          path=excluded.path, norm=excluded.norm, av_code=excluded.av_code,
          medias=excluded.medias, updated_at=excluded.updated_at, src=excluded.src
    """, [r + (src,) for r in rows])
    c.commit()


def _prune_owned(c, lib_id, pulled_ids, src):
    """删除某库本次未拉到的 owned 行（仅限 src 来源），并回退 catalog 匹配态，避免「假拥有」。"""
    c.execute("CREATE TEMP TABLE IF NOT EXISTS _jf_pulled(emby_id TEXT PRIMARY KEY)")
    c.execute("DELETE FROM _jf_pulled")
    c.executemany("INSERT OR IGNORE INTO _jf_pulled VALUES (?)", [(i,) for i in pulled_ids])
    gone = [r[0] for r in c.execute(
        "SELECT emby_id FROM owned WHERE src=? AND lib_id=? AND emby_id NOT IN (SELECT emby_id FROM _jf_pulled)",
        (src, lib_id))]
    n = c.execute("DELETE FROM owned WHERE src=? AND lib_id=? AND emby_id NOT IN (SELECT emby_id FROM _jf_pulled)",
                  (src, lib_id)).rowcount
    if gone:
        qmarks = ",".join("?" * len(gone))
        c.execute(
            f"UPDATE catalog SET match_status='missing', match_emby_id=NULL, match_score=0 "
            f"WHERE match_emby_id IN ({qmarks})", gone)
    c.execute("DROP TABLE _jf_pulled")
    return n


def _task_sync_jellyfin_family(log, src, cfg_prefix):
    """通用 Jellyfin 家族索引：src='jellyfin'/'playable'，cfg_prefix='jellyfin'/'playable'。

    走原生 Jellyfin API（core.jellyfin），与 Emby 索引并列、各写各的 src，互不误删。
    """
    from tools import sync_emby
    cfg = config.load()
    if not cfg.get(f"{cfg_prefix}_enabled"):
        log(f"「索引 {src}」未启用：先在设置里打开 {cfg_prefix} 反代/索引开关")
        src_prog(src, 0, 0, "未启用", running=False, ok=False)
        return
    base = cfg.get(f"{cfg_prefix}_base") or ""
    key = cfg.get(f"{cfg_prefix}_api_key") or ""
    uid = cfg.get(f"{cfg_prefix}_user_id") or ""
    if not base or not key:
        log(f"「索引 {src}」缺少 base 或 api_key")
        src_prog(src, 0, 0, "缺少凭据", running=False, ok=False)
        return
    if not uid:
        uid = jellyfin.resolve_user(base, key, "", 20)
    log(f"开始索引 {src} 库存（{base}）…")
    try:
        libs = jellyfin.libraries(base, key, uid)
    except Exception as e:
        log(f"✗ {src} 拉库清单失败：{e}")
        src_prog(src, 0, 0, "拉库失败", running=False, ok=False)
        return
    log(f"发现 {len(libs)} 个库")
    total = 0
    failed = []
    c = db.init()
    todo = [l for l in libs if l["type"] not in ("livetv", "playlists", "boxsets")]
    src_prog(src, 0, len(todo), "准备…", running=True, ok=None)
    for i, lib in enumerate(todo):
        try:
            cat = sync_emby.category_of(lib["name"])
            src_prog(src, i, len(todo), lib["name"])
            rows, n, pulled_ids = [], 0, []
            for it in jellyfin.iter_items(base, key, uid, lib["id"]):
                pi = {k.lower(): v for k, v in (it.get("ProviderIds") or {}).items()}
                name = it.get("Name") or ""
                orig = it.get("OriginalTitle") or ""
                path = it.get("Path") or ""
                tags = it.get("ImageTags") or {}
                rows.append((it["Id"], name, orig,
                             matcher.year_of(it), it.get("PremiereDate") or "",
                             it.get("Type") or "", lib["id"], lib["name"],
                             pi.get("tmdb") or pi.get("tmdbcol") or "", pi.get("imdb") or "", pi.get("tvdb") or "",
                             1 if tags.get("Primary") else 0, tags.get("Primary") or "", path,
                             matcher.normalize(name),
                             matcher.extract_av_code(name, orig, path) if cat == "AV" else "",
                             json.dumps(jellyfin.media_sources(it), ensure_ascii=False) if it.get("MediaSources") else "",
                             db.now()))
                n += 1
                pulled_ids.append(it["Id"])
                if len(rows) >= 500:
                    _flush_owned(c, rows, src)
                    rows = []
            if rows:
                _flush_owned(c, rows, src)
            c.commit()
            stale = _prune_owned(c, lib["id"], pulled_ids, src)
            c.commit()
            total += n
            log(f"✓ {src}/{lib['name']} [{cat}] {n} 条" + (f"，清理失效 {stale} 条" if stale else ""))
            src_prog(src, i + 1, len(todo), f"{lib['name']} · {n} 条")
        except Exception as e:
            failed.append(lib["name"])
            log(f"✗ {src}/{lib['name']} 失败：{e}（其余库继续）")
    if todo and not failed:
        keep = ",".join("?" * len(todo))
        k = c.execute(f"DELETE FROM owned WHERE src=? AND lib_id NOT IN ({keep})",
                      [src] + [l["id"] for l in todo]).rowcount
        c.commit()
        if k:
            log(f"库级清理（{src}）：移除 {k} 条已不存在库的残留")
    db.set_meta("owned_synced_at", db.now())
    AVLIB_CACHE["d"] = None
    CATLIB_CACHE.clear()
    if failed:
        log(f"完成（{src}）：共 {total} 条；{len(failed)} 个库失败：{'、'.join(failed)} —— 重跑可补齐")
        src_prog(src, len(todo), len(todo), f"共 {total} 条，{len(failed)} 个库失败", running=False, ok=False)
    else:
        log(f"完成（{src}）：共 {total} 条")
        src_prog(src, len(todo), len(todo), f"共 {total} 条", running=False, ok=True)


def task_sync_jellyfin(log):
    _task_sync_jellyfin_family(log, "jellyfin", "jellyfin")


def task_sync_playable(log):
    _task_sync_jellyfin_family(log, "playable", "playable")


def _plex_type(t):
    """把 Plex 的 type 归一到 Emby/Jellyfin 的写法（movie→Movie，show→Series）。"""
    t = (t or "").strip()
    return {"movie": "Movie", "show": "Series", "episode": "Episode"}.get(t.lower(), t)


def task_sync_plex(log):
    """索引 Plex 媒体库（只读）：走 Plex 原生 API + X-Plex-Token，写 owned(src='plex')。

    与 Emby/Jellyfin/Playable 各写各的 src，互不误删。Plex 条目 id 为 ratingKey（数字串），
    与 Emby 整型 ID / Jellyfin GUID 命名空间不重叠，PK 不会跨源撞车。
    """
    from tools import sync_emby
    cfg = config.load()
    src = "plex"
    token = (cfg.get("plex_token") or "").strip()
    base = (cfg.get("plex_base") or "http://127.0.0.1:32400").strip()
    if not token:
        log("✗ 「索引 Plex」缺少 token：请先在设置里点「登录 Plex」，授权后自动填入")
        src_prog(src, 0, 0, "未登录", running=False, ok=False)
        return
    log(f"开始索引 plex 库存（{base}）…")
    try:
        libs = _plex_list_libraries(token, base)
    except Exception as e:
        log(f"✗ plex 拉库清单失败：{e}（token 可能已失效，重新登录 Plex 试试）")
        src_prog(src, 0, 0, "拉库失败", running=False, ok=False)
        return
    log(f"发现 {len(libs)} 个库")
    total = 0
    failed = []
    c = db.init()
    # Plex 库 type：movie / show / artist / photo；只索引影视两类
    todo = [l for l in libs if l.get("type") in ("movie", "show")]
    src_prog(src, 0, len(todo), "准备…", running=True, ok=None)
    for i, lib in enumerate(todo):
        try:
            cat = sync_emby.category_of(lib["title"])
            src_prog(src, i, len(todo), lib["title"])
            rows, n, pulled_ids = [], 0, []
            for it in _plex_iter_items(lib["key"], token, base):
                pi = {k.lower(): v for k, v in (it.get("ProviderIds") or {}).items()}
                name = it.get("Name") or ""
                orig = it.get("OriginalTitle") or ""
                path = it.get("Path") or ""
                tags = it.get("ImageTags") or {}
                rows.append((it["Id"], name, orig,
                             matcher.year_of(it), it.get("premiere") or "",
                             _plex_type(it.get("Type") or ""), lib["key"], lib["title"],
                             pi.get("tmdb") or "", pi.get("imdb") or "", pi.get("tvdb") or "",
                             1 if tags.get("Primary") else 0, tags.get("Primary") or "", path,
                             matcher.normalize(name),
                             matcher.extract_av_code(name, orig, path) if cat == "AV" else "",
                             json.dumps(it.get("MediaSources") or [], ensure_ascii=False),
                             db.now()))
                n += 1
                pulled_ids.append(it["Id"])
                if len(rows) >= 500:
                    _flush_owned(c, rows, src)
                    rows = []
            if rows:
                _flush_owned(c, rows, src)
            c.commit()
            stale = _prune_owned(c, lib["key"], pulled_ids, src)
            c.commit()
            total += n
            log(f"✓ plex/{lib['title']} [{cat}] {n} 条" + (f"，清理失效 {stale} 条" if stale else ""))
            src_prog(src, i + 1, len(todo), f"{lib['title']} · {n} 条")
        except Exception as e:
            failed.append(lib["title"])
            log(f"✗ plex/{lib['title']} 失败：{e}（其余库继续）")
    if todo and not failed:
        keep = ",".join("?" * len(todo))
        k = c.execute(f"DELETE FROM owned WHERE src=? AND lib_id NOT IN ({keep})",
                      [src] + [l["key"] for l in todo]).rowcount
        c.commit()
        if k:
            log(f"库级清理（plex）：移除 {k} 条已不存在库的残留")
    db.set_meta("owned_synced_at", db.now())
    AVLIB_CACHE["d"] = None
    CATLIB_CACHE.clear()
    if failed:
        log(f"完成（plex）：共 {total} 条；{len(failed)} 个库失败：{'、'.join(failed)} —— 重跑可补齐")
        src_prog(src, len(todo), len(todo), f"共 {total} 条，{len(failed)} 个库失败", running=False, ok=False)
    else:
        log(f"完成（plex）：共 {total} 条")
        src_prog(src, len(todo), len(todo), f"共 {total} 条", running=False, ok=True)


def _upsert(c, r):
    """把一条目标写进 catalog（存在则更新元数据并重置比对状态）。"""
    facets_flush()
    regions = r.get("regions") or r.get("region") or ""
    genres = r.get("genre_str") or ",".join(r.get("genre_names") or [])
    media = r.get("media") or ""
    lists = r.get("list_key") or ""
    c.execute("""INSERT INTO catalog(cid,source,category,title,original_title,year,release_date,
                  poster,tmdb_id,imdb_id,tvdb_id,rating,popularity,extra,norm,av_code,regions,genres,media,lists,match_status)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'missing')
                 ON CONFLICT(cid) DO UPDATE SET
                  title=excluded.title, original_title=excluded.original_title,
                  year=excluded.year, release_date=excluded.release_date,
                  poster=COALESCE(NULLIF(excluded.poster,''), catalog.poster),
                  rating=COALESCE(excluded.rating, catalog.rating),
                  popularity=MAX(COALESCE(excluded.popularity,0), COALESCE(catalog.popularity,0)),
                  extra=COALESCE(NULLIF(excluded.extra,'{}'), catalog.extra),
                  av_code=COALESCE(NULLIF(excluded.av_code,''), catalog.av_code),
                  regions=CASE WHEN COALESCE(catalog.regions,'')='' THEN excluded.regions
                               WHEN COALESCE(excluded.regions,'')='' THEN catalog.regions
                               WHEN catalog.regions LIKE '%'||excluded.regions||'%' THEN catalog.regions
                               ELSE catalog.regions||','||excluded.regions END,
                  genres=CASE WHEN COALESCE(excluded.genres,'')='' THEN COALESCE(catalog.genres,'')
                              ELSE excluded.genres END,
                  media=CASE WHEN COALESCE(excluded.media,'')='' THEN COALESCE(catalog.media,'')
                             ELSE excluded.media END,
                  lists=CASE WHEN COALESCE(excluded.lists,'')='' THEN COALESCE(catalog.lists,'')
                             WHEN COALESCE(catalog.lists,'')='' THEN excluded.lists
                             WHEN (','||catalog.lists||',') LIKE '%,'||excluded.lists||',%' THEN catalog.lists
                             ELSE catalog.lists||','||excluded.lists END,
                  match_status=CASE WHEN catalog.match_status='owned' THEN 'owned' ELSE 'missing' END""",
              (r["cid"], r.get("source", ""), r.get("category", ""), r.get("title", ""),
               r.get("original_title", ""), r.get("year"), r.get("release_date", ""),
               r.get("poster", ""), str(r.get("tmdb_id") or ""), r.get("imdb_id", ""),
               r.get("tvdb_id", ""), r.get("rating"), r.get("popularity") or 0,
               json.dumps(r.get("extra") or {}, ensure_ascii=False),
               matcher.normalize(r.get("title")), r.get("av_code", ""),
               regions, genres, media, lists))


def _cursor(key):
    """读断点游标（JSON）。读坏了就当没有，不要让一次解析失败卡死整个拉取。"""
    try:
        d = json.loads(db.get_meta(key) or "{}")
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_cursor(key, cur):
    db.set_meta(key, json.dumps(cur, ensure_ascii=False))


def _known_cids(**kw):
    """已有发行片 cid 集合，用来判断「这几页是不是全是老面孔」。"""
    where, args = [], []
    for k, v in kw.items():
        if v:
            where.append(f"{k}=?")
            args.append(v)
    sql = "SELECT cid FROM catalog" + (" WHERE " + " AND ".join(where) if where else "")
    return {r["cid"] for r in db.conn().execute(sql, args)}


# 分类 → [(桶, 该桶对应的 TMDB 地区档)]
# 只抓 Emby 里真有的库：某个桶没勾库（如「其他语电影」「其他剧」），
# 对应地区整档不生成任务，抓取范围自然收窄。
CAT_FETCH_GROUPS = {
    "电影":   [("movie_cn",   ["大陆", "香港", "台湾"]),
               ("movie_jpkr", ["日本", "韩国", "印度"]),
               ("movie_us",   ["美国", "欧洲"])],
    "剧集":   [("tv_cn",      ["大陆", "香港", "台湾"]),
               ("tv_jpkr",    ["日本", "韩国", "印度"]),
               ("tv_us",      ["美国", "欧洲"])],
    "动画":   [("anime_movie", ["大陆", "日本", "美国", "韩国"]),
               ("anime_tv",    ["大陆", "日本", "美国", "韩国"])],
    "纪录片": [("doc_movie",   ["大陆", "日本", "韩国", "美国", "欧洲"])],
    "综艺":   [("variety_cn",  ["大陆", "香港", "台湾"]),
               ("variety_other", ["日本", "韩国", "美国"])],
    "演唱会": [("concert_cn",  ["大陆", "香港", "台湾"]),
               ("concert_other", ["日本", "韩国", "美国", "欧洲"])],
}


def catalog_scope():
    """按 Emby 实际媒体库反推抓取范围：子类 × 地区 × 年份（年份倒序）。

    粒度从「分类」下移到「子类」：这样发行量能精确到「欧美电影每年多少部」，
    而不是把国产、日韩印、欧美混成一个电影总数。没勾库的子类整档不抓。
    """
    sel = db.sel_map()
    c = db.conn()
    specs = []
    for cat, groups in CAT_FETCH_GROUPS.items():
        for key, rs in groups:
            libs = [x for x in (sel.get(key) or []) if x]
            if not libs:
                continue      # 这个子类没映射任何 Emby 库，不抓
            sql = ("SELECT DISTINCT year FROM owned WHERE library IN (%s) "
                   "AND year>=1900 AND year<=2027" % ",".join("?" * len(libs)))
            years = sorted({int(r["year"]) for r in c.execute(sql, libs) if r["year"]},
                           reverse=True)
            if not years:
                this = time.localtime().tm_year
                years = list(range(this, this - 30, -1))
            specs.append({"category": cat, "bucket": key, "regions": list(rs),
                          "years": years, "libs": libs})
    return specs


def _dec_match(year, dec):
    """年份是否落在某个年代档里，口径与前端 chip / SQL 一致。"""
    try:
        y = int(year)
    except (TypeError, ValueError):
        return False
    dec = (dec or "").strip()
    if not dec:
        return True
    if dec == "old":
        return y < 1980
    if dec == "none":
        return False
    if dec.startswith("y") and dec[1:].isdigit():
        k = int(dec[1:])
        return y >= 2026 if k >= 2026 else y == k
    if dec.isdigit():
        k = int(dec)
        return k <= y <= k + 9
    return True


def _catalog_write(c, rows, log=None):
    """把一批 TMDB 结果写进 catalog，返回成功条数。"""
    ins = 0
    for r in rows:
        try:
            _upsert(c, r)
            ins += 1
        except Exception as e:
            if log:
                log(f"跳过 {r.get('title')}: {e}")
    c.commit()
    return ins


def task_fetch_catalog(log, specs=None, incremental=False, resume=False):
    cfg = config.load()
    if not (cfg.get("tmdb_api_key") or "").strip():
        log("未填写 TMDB API Key，跳过发行片拉取（设置 → 基础设置里填你自己的 Key）")
        src_prog("tmdb", running=False, ok=False, msg="未填 TMDB Key")
        return

    src_prog("tmdb", 0, 0, "准备…", running=True, ok=None)
    c = db.init()

    # ---- 增量：只扫最近两年第一页，补新片 ----
    if incremental:
        scope = catalog_scope()
        if not scope:
            log("Emby 里没有可用的媒体库，先在「设置 → EMBY 媒体库」里勾选库。")
            src_prog("tmdb", running=False, ok=False, msg="没有可抓的媒体库")
            return
        for s in scope:
            s["years"] = s["years"][:2]
        log("增量更新：只扫最近两年的第一页，补新片…")
        state = {"ins": 0, "rows": 0}

        def batch(rows):
            state["ins"] += _catalog_write(c, rows)
            state["rows"] += len(rows)

        tmdbsrc.build_year_catalog(scope, [], progress=log, on_batch=batch,
                                   cursors={}, page_limit=1)
        db.set_meta("catalog_fetched_at", db.now())
        log(f"入库 {state['ins']} 条（扫过 {state['rows']} 条结果）")
        src_prog("tmdb", state["rows"], state["rows"], msg=f"入库 {state['ins']} 条",
                 running=False, ok=True)
        return

    # ---- 全量：先算发行量（只读数字，几分钟），再抓热门榜 ----
    scope = catalog_scope()
    if not scope:
        log("Emby 里没有可用的媒体库，先在「设置 → EMBY 媒体库」里勾选库。")
        src_prog("tmdb", running=False, ok=False, msg="没有可抓的媒体库")
        return
    total_jobs = len(tmdbsrc.year_jobs(scope, []))
    plans = {}
    for s in scope:
        plans.setdefault(s["category"], []).append(
            f"{s['bucket']} {s['years'][0]}→{s['years'][-1]}")
    for cat, xs in plans.items():
        log(f"  {cat}：" + "；".join(xs))
    log(f"共 {total_jobs} 档（年 × 子类 × 媒体）：先统计每年的发行量（全部年份），"
        f"再抓 {tmdbsrc.HOT_YEAR_FLOOR} 年之后每年热度前 {tmdbsrc.HOT_PER_YEAR} 条…")

    try:
        _sync_counts(log, scope, resume=resume)
    except Exception as e:
        src_prog("tmdb", running=False, ok=False, msg=f"{type(e).__name__}: {e}")
        raise

    cur = _cursor("fetch_cursor_tmdb") if resume else {}
    state = {"ins": 0, "rows": 0, "batches": 0}

    def batch(rows):
        state["ins"] += _catalog_write(c, rows)
        state["rows"] += len(rows)
        state["batches"] += 1
        db.set_meta("catalog_fetched_at", db.now())
        if state["batches"] % 10 == 0:
            _save_cursor("fetch_cursor_tmdb", cur)

    def step(d, t, label):
        src_prog("tmdb", d, t, label)
        if label.endswith("完成") or "跳过" in label:
            _save_cursor("fetch_cursor_tmdb", cur)

    try:
        stats = tmdbsrc.build_year_catalog(
            scope, [], progress=log, on_step=step, on_batch=batch,
            cursors=cur, resume=resume)
    except Exception as e:
        _save_cursor("fetch_cursor_tmdb", cur)
        src_prog("tmdb", running=False, ok=False, msg=f"{type(e).__name__}: {e}")
        raise
    _save_cursor("fetch_cursor_tmdb", cur)
    done = sum(1 for v in cur.values() if v.get("done"))
    log(f"热门榜入库 {state['ins']} 条（扫过 {state['rows']} 条结果）；"
        f"进度 {done}/{len(cur)} 档，下次「全量续抓」从这里接着跑")
    src_prog("tmdb", stats["jobs"], stats["jobs"],
             msg=f"热门榜入库 {state['ins']} 条", running=False, ok=True)


def _sync_counts(log, scope=None, resume=True):
    """只统计发行量：每档一次请求读 total_results，不落条目。"""
    scope = scope or catalog_scope()
    if not scope:
        log("没有可统计的媒体库，先在图库设置里勾选 Emby 库。")
        return {"jobs": 0, "total": 0}
    cur = _cursor("fetch_cursor_counts") if resume else {}
    c = db.init()
    n = {"i": 0}

    def on_count(cat, bucket, year, total):
        db.count_put(c, cat, bucket, year, total)
        n["i"] += 1
        if n["i"] % 10 == 0:
            c.commit()
            _save_cursor("fetch_cursor_counts", cur)

    src_prog("tmdb", 0, 1000, "统计发行量…", running=True, ok=None)
    log("统计发行量：每年每子类只读一个总数（不拉条目）…")
    res = tmdbsrc.fetch_counts(
        scope, [], progress=log, on_count=on_count, cursors=cur, resume=resume,
        on_step=lambda d, t, l: src_prog("tmdb", d, t, l) if t else None)
    c.commit()
    _save_cursor("fetch_cursor_counts", cur)
    log(f"发行量统计完成：{res['jobs']}/{res['total']} 档")
    src_prog("tmdb", res["total"], res["total"], msg="发行量已更新",
             running=False, ok=True)
    return res


def task_fetch_counts(log, resume=True):
    """单独跑发行量统计（不动条目），给「统计发行量」按钮用。"""
    cfg = config.load()
    if not (cfg.get("tmdb_api_key") or "").strip():
        log("未填写 TMDB API Key，跳过（设置 → 基础设置里填你自己的 Key）")
        src_prog("tmdb", running=False, ok=False, msg="未填 TMDB Key")
        return
    _sync_counts(log, resume=resume)


def _prune_list(c, key, cids):
    """把已经不在本次结果里的条目从这个榜摘掉。

    lists 是逗号分隔的多榜集合：摘完还剩别的榜就只改 lists；摘空且不是本地已有
    的片就删行（留着会在「全部」里变成无主条目，也会被算进发行片）。
    """
    keep = set(cids)
    gone = 0
    for r in c.execute("SELECT cid,lists,match_status FROM catalog "
                       "WHERE (','||COALESCE(lists,'')||',') LIKE ?",
                       (f"%,{key},%",)).fetchall():
        if r["cid"] in keep:
            continue
        rest = [x for x in (r["lists"] or "").split(",") if x and x != key]
        if rest:
            c.execute("UPDATE catalog SET lists=? WHERE cid=?", (",".join(rest), r["cid"]))
        elif r["match_status"] == "owned":
            c.execute("UPDATE catalog SET lists='' WHERE cid=?", (r["cid"],))
        else:
            c.execute("DELETE FROM catalog WHERE cid=?", (r["cid"],))
        gone += 1
    facets_flush()
    return gone


def task_fetch_list(log, keys=None, limit=40):
    """拉榜单（TMDB 官方 / 豆瓣 / 流媒体平台）。

    TMDB / 豆瓣的 TOP250 固定 250 条，与抽屉里的「拉多少条」无关；并且按快照覆盖
    —— 本次没进前 250 的旧条目会从该榜摘掉，否则榜单会变成历史累计。
    """
    from core.sources import lists as lists_src
    keys = keys or [d["key"] for d in lists_src.LIST_DEFS]
    c = db.init()
    total = 0
    src_prog("lists", 0, len(keys), "准备…", running=True, ok=None)
    for i, k in enumerate(keys, 1):
        d = lists_src.BY_KEY.get(k)
        if not d:
            continue
        log(f"▶ {d['name']}")
        src_prog("lists", i - 1, len(keys), d["name"])
        try:
            rows = lists_src.fetch(k, limit=limit, progress=log)
        except Exception as e:
            log(f"  ! {d['name']} 失败：{e}")
            continue
        for r in rows:
            try:
                _upsert(c, r)
                total += 1
            except Exception as e:
                log(f"  跳过 {r.get('title')}: {e}")
        c.commit()
        # 抓空了不清理：多半是接口抽风，不能把整榜清空
        gone = _prune_list(c, k, [r["cid"] for r in rows if r.get("cid")]) if rows else 0
        c.commit()
        log(f"  ✓ {d['name']} {len(rows)} 条" + (f"，摘掉旧条目 {gone}" if gone else ""))
    db.set_meta("lists_fetched_at", db.now())
    log(f"榜单入库完成，共 {total} 条")
    src_prog("lists", len(keys), len(keys), f"入库 {total} 条", running=False, ok=True)


def task_fetch_av(log, pages=None, modes=None, incremental=False, resume=False):
    """从 JavDB 拉日系番号汇总（最新发行 + 月榜），入库为 AV 分类。"""
    cfg = config.load()
    if incremental:
        pages, modes = 1, ["new"]
    pages = int(pages or cfg.get("av_pages") or 10)
    modes = modes or cfg.get("av_modes") or ["new", "rank_monthly"]
    tail = "（增量：只看最新页）" if incremental else ("（续抓：从上次页码继续）" if resume else "")
    log(f"从 JavDB 拉取日系番号：{'+'.join(modes)}，{pages} 页{tail}…")
    src_prog("av", 0, 0, "连接 JavDB…", running=True, ok=None)
    cur = _cursor("fetch_cursor_av") if resume else {}
    known = {r["av_code"] for r in db.conn().execute(
        "SELECT av_code FROM catalog WHERE category='AV' AND COALESCE(av_code,'')<>''")}
    log(f"库内已有 {len(known)} 个番号")
    try:
        rows = javdb.build_av_list(pages=pages, modes=modes, progress=log,
                                   on_step=lambda d, t, l: src_prog("av", d, t, l),
                                   cursors=cur, known=known, resume=resume)
    except Exception as e:
        src_prog("av", running=False, ok=False, msg=f"{type(e).__name__}: {e}")
        raise
    c = db.init()
    for r in rows:
        _upsert(c, r)
    c.commit()
    db.set_meta("av_fetched_at", db.now())
    if resume:
        _save_cursor("fetch_cursor_av", cur)
        log(f"抓取进度已记下：" + "，".join(f"{m} 第 {(v.get('page') or 1)} 页" for m, v in cur.items()))
    total = c.execute("SELECT COUNT(*) n FROM catalog WHERE category='AV'").fetchone()["n"]
    log(f"入库 {len(rows)} 个日系番号；发行片现有 {total} 个")
    src_prog("av", 1, 1, f"入库 {len(rows)} 个番号", running=False, ok=True)


# 本地库里的「假前缀」：路径/文件名残留，不是厂牌
_FAKE_PREFIX = {
    "STRM", "SAMPLE", "CD", "PART", "DISC", "BD", "DVD", "HD", "FHD", "UHD",
    "WEB", "MP4", "MKV", "AVI", "MOV", "SUB", "CHS", "CHT", "KOR", "ENG",
    "UNCENSORED", "LEAK", "HDR", "HEVC", "X264", "X265",
}


def _av_prefix_stats(c):
    """本地 AV 库 → {前缀: (条数, 样本番号)}，按条数降序。

    只看 AV 桶里**用户勾选的库**，不再靠库名关键字猜。以前用 category_of
    判断，凡是库名含「Adult」或「三级」的都算 AV —— 于是「Adult-欧美」
    「欧美-三级」这些没勾的库会混进 AV 统计，用户看到的 AV 里就冒出欧美。
    """
    av_libs = list(db.libs_for("av"))
    if not av_libs:
        return {}
    stats = {}
    q = "SELECT name, path FROM owned WHERE library IN (%s)" % ",".join("?" * len(av_libs))
    for r in c.execute(q, av_libs):
        code = matcher.extract_av_code(r["path"], r["name"])
        if not code:
            continue
        pre = matcher.code_prefix(code)
        if not pre or len(pre) < 2 or pre in _FAKE_PREFIX:
            continue
        a = stats.setdefault(pre, [0, code])
        a[0] += 1
    return dict(sorted(stats.items(), key=lambda kv: -kv[1][0]))


def _upsert_av(c, r, cache=None):
    """AV 行入库：同番号已有（JavDB 或别的源）就并到同一行，不重复计数。

    catalog 的主键是 cid，JavDB 是 `javdb:SSIS001`、JavBus 是 `javbus:SSIS001`，
    不合并的话同一部片会出现两次，直接把这年「发行片数」撑大。
    cache：预加载的 {av_code: cid}，避免逐行查库。
    """
    code = r.get("av_code") or ""
    cache = cache if cache is not None else {}
    cid = cache.get(code) or ""
    if cid:
        old = c.execute("SELECT extra FROM catalog WHERE cid=?", (cid,)).fetchone()
        try:
            oe = json.loads((old["extra"] if old else "") or "{}")
        except Exception:
            oe = {}
        ne = dict(r.get("extra") or {})
        srcs = set(oe.get("sources") or [])
        if oe.get("site"):
            srcs.add(oe["site"])
        if ne.get("site"):
            srcs.add(ne["site"])
        ne["sources"] = sorted(srcs)
        for k, v in oe.items():          # JavDB 的 url/评分等别丢
            if k not in ne or not ne.get(k):
                ne[k] = v
        r = dict(r)
        r["cid"] = cid
        r["extra"] = ne
    else:
        cache[code] = r["cid"]
    _upsert(c, r)
    return bool(cid)


def task_fetch_avbus(log, year_floor=None, min_items=None, resume=True, prefixes=None):
    """按片商维度回灌 JavBus 全史（本地库出现过的前缀才抓）。

    和 JavDB 的分工：JavDB 只覆盖最近几个月的「新片」，这里补 2000 年以来的历史，
    两边按番号合并，最终每年「发行片数」才是完整口径。
    """
    cfg = config.load()
    floor = int(year_floor or cfg.get("javbus_year_floor") or 2000)
    min_n = int(min_items if min_items is not None else (cfg.get("avbus_min_items") or 3))
    c = db.init()
    stats = _av_prefix_stats(c)
    plist = list(prefixes) if prefixes else [p for p, v in stats.items() if v[0] >= min_n]
    if not plist:
        log("本地没有可用的番号前缀：先跑一次「索引 Emby」，且 Emby 里要有 AV 库、"
            "文件名/路径能提取出番号（如 SSIS-378）")
        src_prog("avbus", running=False, ok=False, msg="无本地番号·先索引 Emby")
        return
    cur = _cursor("fetch_cursor_avbus") if resume else {}
    todo = [p for p in plist if not (cur.get(p) or {}).get("done")]
    log(f"按片商回灌 JavBus 历史：本地 {len(stats)} 个前缀，本次要抓 {len(todo)} 个"
        f"（其余已完成）· 回溯到 {floor} 年 · 每页间隔 {cfg.get('javbus_delay') or 0.35}s")
    src_prog("avbus", 0, len(todo), "过年龄确认…", running=True, ok=None)
    samples = {p: stats[p][1] for p in plist}
    cache = {}
    for r in c.execute("SELECT cid, av_code FROM catalog WHERE category='AV' "
                       "AND COALESCE(av_code,'')<>''"):
        cache.setdefault(r["av_code"], r["cid"])
    log(f"库内已有 {len(cache)} 个 AV 番号，同番号会并到一行")
    stat = {"new": 0, "upd": 0, "n": 0}

    def _sink(prefix, rs):
        """每抓完一个片商写一次库 + 存游标；全量几十万条不能囤在内存里。"""
        for r in rs:
            try:
                if _upsert_av(c, r, cache=cache):
                    stat["upd"] += 1
                else:
                    stat["new"] += 1
                stat["n"] += 1
            except Exception as e:
                log(f"跳过 {r.get('title')}: {e}")
        c.commit()
        _save_cursor("fetch_cursor_avbus", cur)

    try:
        rows = javbus.build_av_catalog(
            todo, samples, cur, known=set(), resume=resume, cfg=cfg, year_floor=floor,
            max_pages=int(cfg.get("javbus_max_pages") or 900),
            delay=float(cfg.get("javbus_delay") or 0.35),
            progress=log,
            on_step=lambda d, t, l: src_prog("avbus", d, t, l),
            on_batch=_sink,
        )
    except Exception as e:
        c.commit()
        _save_cursor("fetch_cursor_avbus", cur)
        src_prog("avbus", running=False, ok=False, msg=f"{type(e).__name__}: {e}")
        raise
    _save_cursor("fetch_cursor_avbus", cur)
    new, upd = stat["new"], stat["upd"]
    db.set_meta("avbus_fetched_at", db.now())
    done = sum(1 for v in cur.values() if v.get("done"))
    log(f"入库 {stat['n']} 部：新增 {new}，合并进已有番号 {upd}；片商进度 {done}/{len(cur)}")
    log("按年份看发行片（AV）：")
    for r in c.execute("SELECT year, COUNT(*) n FROM catalog WHERE category='AV' "
                       "GROUP BY year ORDER BY year DESC LIMIT 12"):
        log(f"  {r['year'] or '未知'}：{r['n']} 部")
    log("提示：点「比对」刷新缺口，逐年表就会按新发行片重算")
    src_prog("avbus", len(todo), len(todo), f"新增 {new} · 合并 {upd}", running=False, ok=True)


def task_fetch_hentai(log, pages=None, incremental=False, resume=False):
    """拉里番清单（AniList 成人动画），入库为「里番」分类，再按标题前缀比对本地库。

    Bangumi 的里番标签只有 351 条，覆盖太少；AniList 的 isAdult 过滤一次能翻到
    5000 条（含日文原名 / 年份 / 集数 / 封面），是免注册源里最完整的。
    """
    cfg = config.load()
    pages = int(pages or cfg.get("hentai_pages") or 40)
    if incremental:
        pages = min(pages, 4)
    from core.sources import anilist
    tail = "（增量：只看前几页）" if incremental else ("（续抓：从上次页码继续）" if resume else "")
    log(f"从 AniList 拉里番清单，{pages} 页 × 50{tail}…")
    src_prog("hentai", 0, 0, "连接 AniList…", running=True, ok=None)
    cur = _cursor("fetch_cursor_hentai") if resume else {}
    known = _known_cids(source="anilist")
    log(f"库内已有 {len(known)} 条里番")
    try:
        rows = anilist.fetch(pages=pages, progress=log,
                             on_step=lambda d, t, l: src_prog("hentai", d, t, l),
                             cursors=cur, known=known, resume=resume)
    except Exception as e:
        src_prog("hentai", running=False, ok=False, msg=f"{type(e).__name__}: {e}")
        raise
    c = db.init()
    for r in rows:
        try:
            _upsert(c, r)
        except Exception as e:
            log(f"跳过 {r.get('title')}: {e}")
    c.commit()
    db.set_meta("hentai_fetched_at", db.now())
    # 游标一律记下（不只在 resume 时）：仪表盘/设置里的「拉取进度」和「断点续抓」都依赖它。
    # 以前只在 resume=True 时存，而按钮从不传 resume —— 于是拉完了状态还一直显示「未开始」。
    if cur:
        _save_cursor("fetch_cursor_hentai", cur)
        log("抓取进度已记下：" + "，".join(f"{m} 第 {(v.get('page') or 1)} 页" for m, v in cur.items()))
    total = c.execute("SELECT COUNT(*) n FROM catalog WHERE category='里番'").fetchone()["n"]
    log(f"入库 {len(rows)} 条里番；发行片现有 {total} 条")
    from tools import match
    log("按标题比对本地里番库…")
    n = match.match_hentai(verbose=False)
    log(f"比对完成：已有 {n['owned']} / 缺 {n['missing']}（疑似 {n['likely']}）")
    src_prog("hentai", 1, 1, f"入库 {len(rows)} 条 · 缺 {n['missing']}",
             running=False, ok=True)


def task_warm_posters(log, scope="av", category="", limit=0, workers=12, offset=0):
    """把 catalog 里的封面预先抓下来落盘，之后即使代理挂了也能显示。

    scope: av = 只 AV/里番（javdb 缩略图，单张小）；all = 全部；cat = 指定分类。
    已缓存的直接跳过，所以可以反复跑、也能中断后重跑续上。
    offset 只给调试/分段跑用（从第 N 张开始），平时留 0。
    """
    c = db.init()
    q = ("SELECT DISTINCT poster FROM catalog "
         "WHERE poster IS NOT NULL AND poster<>''")
    args = []
    if scope == "av":
        q += " AND category IN ('AV','里番')"
    elif scope == "cat" and category:
        q += " AND category=?"
        args.append(category)
    q += " ORDER BY category, year DESC"
    off = max(0, int(offset or 0))
    if int(limit or 0) > 0:
        q += " LIMIT ? OFFSET ?"
        args += [int(limit), off]
    elif off:
        q += " LIMIT -1 OFFSET ?"
        args.append(off)
    urls, seen = [], set()
    for r in c.execute(q, args):
        u = fix_poster_url(r["poster"])
        if u and u not in seen:
            seen.add(u)
            urls.append(u)
    total = len(urls)
    if not total:
        log("没有需要预热的封面（catalog 里这个范围没图）")
        src_prog("posters", running=False, ok=False, msg="没有需要预热的封面")
        return
    log(f"开始预热封面：{total} 张（范围 {category or scope}，{workers} 线程，已有缓存自动跳过）")
    src_prog("posters", 0, total, "准备…", running=True, ok=None)
    st = {"done": 0, "ok": 0, "skip": 0, "fail": 0}
    lk = threading.Lock()

    # 代理节点空闲一段时间后，第一条 CONNECT 要做一次冷启动 TLS 握手（实测能拖到 15-40 秒），
    # 后面同一节点才恢复到 2-3 秒。先单独用一条长超时的请求把它焐热，否则前几十张全被判失败。
    log("预热代理节点…")
    try:
        fetch_poster_bytes(urls[0], timeout=60)
        log("代理节点已就绪")
    except Exception as e:
        log(f"代理节点预热未成功（{type(e).__name__}），继续尝试")

    def one(u):
        f, _m = poster_cache_path(u)
        if f is not None and f.exists():
            kind = "skip"
        else:
            kind = "fail"
            for attempt in range(3):        # 重试两次：代理偶发抖动 / 冷启动不记为失败
                try:
                    data, ctype = fetch_poster_pooled(u, timeout=25)
                    store_poster(u, data, ctype)
                    kind = "ok"
                    break
                except Exception:
                    if attempt < 2:
                        time.sleep(0.5)
                    continue
        with lk:
            st["done"] += 1
            st[kind] += 1
            if st["done"] % 25 == 0 or st["done"] == total:
                src_prog("posters", st["done"], total,
                         f"新存 {st['ok']} · 已有 {st['skip']} · 失败 {st['fail']}")
                if st["done"] % 500 == 0:
                    log(f"  {st['done']}/{total}：新存 {st['ok']}，跳过 {st['skip']}，失败 {st['fail']}")

    with ThreadPoolExecutor(max_workers=max(1, min(32, int(workers or 12)))) as ex:
        list(ex.map(one, urls))
    db.set_meta("posters_warmed_at", db.now())
    msg = f"新存 {st['ok']} · 已有 {st['skip']} · 失败 {st['fail']}"
    log(f"封面预热完成：{msg}（共 {total} 张）")
    src_prog("posters", total, total, msg, running=False, ok=(st["fail"] == 0))


def task_fetch_full(log):
    """全量拉取（可续）：TMDB 发行片 → JavDB 番号 → AniList 里番 → 全库比对。

    第一次跑会把目标配额抓满；中途断了 / 手动再点一次，会从上次的页码接着翻，
    已经抓完的档直接跳过。每一步单独兜异常，某一源失败不影响后面的步骤。
    """
    log("=== 全量拉取开始（可续抓；TMDB 按年份倒序，可能数小时）===")
    started = time.time()
    try:
        log("--- 1/4 TMDB 发行片（全量 · 续抓）---")
        task_fetch_catalog(log, specs=FULL_SPECS, incremental=False, resume=True)
    except Exception as e:
        log(f"  ! TMDB 全量失败：{type(e).__name__}: {e}")
    try:
        log(f"--- 2/4 JavDB 番号（{FULL_AV_PAGES} 页 × 2 + 月榜 · 续抓）---")
        task_fetch_av(log, pages=FULL_AV_PAGES,
                      modes=["new", "sub", "rank_monthly"], incremental=False, resume=True)
    except Exception as e:
        log(f"  ! AV 全量失败：{type(e).__name__}: {e}")
    try:
        log("--- 3/4 AniList 里番（全量 · 续抓）---")
        task_fetch_hentai(log, pages=FULL_HENTAI_PAGES, incremental=False, resume=True)
    except Exception as e:
        log(f"  ! 里番全量失败：{type(e).__name__}: {e}")
    try:
        log("--- 4/4 比对（全部 Emby 库）---")
        task_match(log)
    except Exception as e:
        log(f"  ! 比对失败：{type(e).__name__}: {e}")
    mins = (time.time() - started) / 60
    log(f"=== 全量拉取结束，用时 {mins:.1f} 分钟 ===")


def fetch_state():
    """缺口账本：发行量统计到哪一年、热门榜抓到多少档。"""
    c = db.conn()
    have = {r["category"]: r["n"] for r in
            c.execute("SELECT category, COUNT(*) n FROM catalog GROUP BY category")}
    cur = _cursor("fetch_cursor_tmdb")
    cc = _cursor("fetch_cursor_counts")
    agg = {}
    for s in catalog_scope():
        cat, bucket = s["category"], s["bucket"]
        a = agg.setdefault(cat, {"jobs": 0, "done": 0, "cjobs": 0, "cdone": 0,
                                 "buckets": [], "years": []})
        media = tmdbsrc.CATEGORY_SPEC.get(cat, ("movie", {}))[0]
        nm = 2 if media == "both" else 1
        hot = dict(s)
        hot["years"] = [y for y in s["years"] if y >= tmdbsrc.HOT_YEAR_FLOOR]
        a["jobs"] += len(tmdbsrc.year_jobs([hot], []))
        a["cjobs"] += len(s["years"]) * nm
        a["buckets"].append(db.BUCKET_LABEL.get(bucket, bucket))
        a["years"] += list(s["years"])
        pre = "%s|%s|" % (cat, bucket)
        a["done"] += sum(1 for k, v in cur.items()
                         if k.startswith(pre) and isinstance(v, dict) and v.get("done"))
        a["cdone"] += sum(1 for k, v in cc.items()
                          if k.startswith(pre) and isinstance(v, dict) and v.get("done"))
    tmdb = []
    for cat, a in agg.items():
        span = db.count_span(category=cat)
        tmdb.append({
            "category": cat,
            "buckets": a["buckets"],
            "have": have.get(cat, 0),
            "jobs": a["jobs"], "done": a["done"],
            "count_jobs": a["cjobs"], "count_done": a["cdone"],
            "count_years": span["years"],
            "state": ("热门榜已完成" if a["jobs"] and a["done"] >= a["jobs"]
                      else ("热门榜抓取中" if a["done"] else "热门榜未开始")),
            "count_state": ("发行量已统计" if a["cjobs"] and a["cdone"] >= a["cjobs"]
                            else ("发行量统计中" if a["cdone"] else "发行量未统计")),
        })
    avc = _cursor("fetch_cursor_av")
    av_modes = 3
    av_done = sum(1 for m in ("new", "sub", "rank_monthly") if (avc.get(m) or {}).get("done"))
    hc = _cursor("fetch_cursor_hentai")
    h_done = sum(1 for m in ("START_DATE_DESC", "POPULARITY_DESC", "SCORE_DESC")
                 if (hc.get(m) or {}).get("done"))
    return {
        "tmdb": tmdb,
        "hot_per_year": tmdbsrc.HOT_PER_YEAR,
        "hot_year_floor": tmdbsrc.HOT_YEAR_FLOOR,
        "av": {"target": FULL_AV_PAGES * 40 + 60, "have": have.get("AV", 0),
               "jobs": av_modes, "done": av_done,
               "state": "已完成" if av_done >= av_modes else ("续抓中" if av_done else "未开始")},
        "hentai": {"target": 5000, "have": have.get("里番", 0),
                   "jobs": 3, "done": h_done,
                   "state": "已完成" if h_done >= 3 else ("续抓中" if h_done else "未开始")},
    }


def task_match(log, libs=None):
    from tools import match
    only = [l for l in (libs or []) if l]
    if not only:
        # 老行为：全库重扫（一键跑全套 / 定时任务走这条）
        log("开始匹配…")
        src_prog("match", 0, 0, "匹配中…", running=True, ok=None)
        counts = match.match_all(verbose=False)
        for k, v in counts.items():
            log(f"  {k}: {v}")
        db.set_meta("matched_at", db.now())
        log("匹配完成")
        src_prog("match", 1, 1, "完成", running=False, ok=True)
        return
    # 按库比对（「比对」面板勾选）：并集语义一次算清。
    # 为什么不逐库串行跑：catalog 按分类组织、多个库共享同一份清单，
    # 一个库一个库跑会让后者的结果覆盖前者（在 A 库有的片，比 B 库时被误判成缺）。
    from tools import sync_emby
    h_libs = set(db.libs_for("hentai"))
    lib_cat = {l: ("里番" if l in h_libs else sync_emby.category_of(l)) for l in only}
    cats = sorted(set(lib_cat.values()))
    log(f"按库比对：{len(only)} 个库（{'、'.join(cats)}）")
    src_prog("match", 0, 0, "匹配中…", running=True, ok=None)
    lib_prog_reset(only)

    def on_prog(done, total, cat):
        src_prog("match", done, total, f"{cat} {done}/{total}")
        for l in only:
            if lib_cat[l] == cat:
                lib_prog_set(l, done=done, total=total, running=True,
                             msg=f"{cat} · {done}/{total}", ok=None)

    counts = match.match_all(verbose=False, only_libs=only, progress=on_prog)
    for l in only:
        lib_prog_set(l, running=False, ok=True, msg="完成", done=lib_prog_snapshot()[l]["total"])
        log(f"  ✓ {l}")
    for k, v in counts.items():
        log(f"  {k}: {v}")
    db.set_meta("matched_at", db.now())
    log("匹配完成")
    src_prog("match", 1, 1, "完成", running=False, ok=True)


def task_dup_scan(log, libs=None):
    """按库扫重复（「比对」面板 → 比对重复）。只读，不删不移，只列版本和路径。

    判定口径与分类页「一键排重」完全一致：
      非 AV 库 —— 同一 TMDB ID，或同一归一化标题+年份，出现多个文件算一组；
      AV/里番库 —— 同一番号多个文件算一组。
    额外两条防误报（2026-10-24 加）：
      分集文件按集数细分 —— Emby 把每集当成独立条目（纪录片/综艺常见），
        不同集不算重复，同一集的高低码多份才算；
      跨库合并要求同名 —— 不同片子被误刮成同一 tmdb 时不捆一组
        （库内不校验，「龙珠/七龙珠」这种同片不同译名仍算重复）。
    每组列出每个副本的分辨率 / HDR / 大小 / 码率 / 所在库 / 完整路径。
    """
    only = [l for l in (libs or []) if l]
    if not only:
        log("未选择任何库")
        return
    from tools import sync_emby
    log(f"扫描重复：{len(only)} 个库")
    lib_prog_reset(only)
    for i, lib in enumerate(only):
        cat = sync_emby.category_of(lib)
        # 按番号判定只适用于真有番号的库：Adult-动画（里番库）按关键词也归 AV，
        # 但里番条目没有番号，按番号扫会全空 —— 这类库走标题口径。
        by_code = (cat == "AV") and not any(k in lib for k in ("动画", "动漫"))
        lib_prog_set(lib, running=True, msg="扫描中…", ok=None)
        log(f"[{i + 1}/{len(only)}] {lib}…")
        groups = _dup_decorate(db.duplicates(libs=[lib], limit=800, by_code=by_code))
        summary = db.dup_summary(libs=[lib], by_code=by_code)
        DUP_RESULT[lib] = {"groups": groups, "summary": summary,
                           "scope": {"label": lib, "libs": [lib], "by_code": by_code,
                                     "category": cat}}
        n, extra = summary.get("groups", 0), summary.get("extra_files", 0)
        lib_prog_set(lib, running=False, ok=True, kind="dup", groups=n,
                     msg=f"{n} 组重复 · 多出 {extra} 个文件")
        log(f"  ✓ {lib}：{n} 组，多出 {extra} 个文件")
    log("扫描完成")


# ---------------- 115 网盘 ----------------
def task_scan_115(log, root="0", depth=3, roots=None):
    """扫描 115 指定目录，落库后给出与 Emby / 发行片的交叉统计。

    roots：目录树里勾选的多个目录 cid，一次扫完。
    """
    cfg = config.load()
    ck = cfg.get("pan115_cookie") or {}
    if not ck.get("UID"):
        log("✗ 未登录 115，请先扫码")
        return
    if roots:
        log(f"开始扫描 115 选中的 {len(roots)} 个目录，深度 {depth}…")
        items, dirs = [], []
        for r in roots:
            it2, dr2 = pan115.walk(r, ck, depth=int(depth), progress=log)
            items += it2
            dirs += dr2
    else:
        log(f"开始扫描 115 目录 root={root or '0'}，深度 {depth}…")
        items, dirs = pan115.walk(root, ck, depth=int(depth), progress=log)
    vids = [it for it in items if pan115.is_video(it["name"])]
    log(f"目录 {len(dirs)} 个，文件 {len(items)} 个，其中视频 {len(vids)} 个；入库中…")

    c = db.init()
    # 按账号分区：只清当前账号的旧记录（换账号不会动到别的账号的数据）
    acct = str(ck.get("UID") or "")
    c.execute("DELETE FROM pan115 WHERE acct=?", (acct,))
    rows = []
    for it in vids:
        name = it["name"]
        norm = matcher.media_norm(name)
        code = matcher.extract_av_code(name)
        rows.append((it["fid"] or it["path"], it["cid"], it["parent"], name, it["path"],
                     it["size"], 0, it["ext"], norm, code, acct, db.now()))
    c.executemany("""INSERT OR REPLACE INTO pan115
                     (fid,cid,parent,name,path,size,is_dir,ext,norm,av_code,acct,updated_at)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
    c.commit()
    cfg["pan115_root"] = str(root or "0")
    config.save(cfg)
    # 扫描时间也按账号记：换账号后不会显示别人的扫描时间
    db.set_meta(f"pan115_scanned_at:{acct}", db.now())
    log(f"✓ 入库 {len(rows)} 个视频文件，扫描完成（账号 {acct or '未知'}）")


# ---------------- strm 生成器（115 目录树 → 直写 strm，备用功能） ----------------
# 定位：**不碰存量 strm**，输出到独立目录（默认 /app/Link/gen115），给 Emby / Jellyfin / Plex 当新媒体库用。
# 流程：预览（walk 选中目录 → 算好每个 strm 的目标路径与内容，落盘 plan）→ 确认后才写盘（幂等，可重复跑）。
# 账号语义：预览时记下登录的 115 账号；写盘前校验，换过账号必须重新预览
#   （115 的 pickcode 账号隔离，旧账号签的 pc 在新账号下会 errno=50015）。
# 风控：预览/写盘只走 webapi/files（列目录），不碰 files/video 签名；签名只发生在播放时（redirect115 端点）。
_GEN_LOCK = threading.Lock()
_GEN_PLAN = {"items": [], "opts": {}, "acct": "", "at": "", "stats": {}}
_GEN_PLAN_FILE = config.DATA / "strm_gen_plan.json"
_GEN_PLAN_MAX = 500000            # 预览条目上限（防爆内存/磁盘）


def _gen_safe_dir(d):
    """目标目录安检：容器内 /app 下可写区、禁 ..、禁根。返回规范化的绝对路径或 None。"""
    d = str(d or "").strip()
    if not d or "\x00" in d or ".." in d.split("/"):
        return None
    d = os.path.normpath(d)
    # 仅允许已映射的可写区：/app/Link（宿主 /vol1/1000/Link）与 /app/data
    if not (d == "/app/Link" or d.startswith("/app/Link/")
            or d == "/app/data" or d.startswith("/app/data/")):
        return None
    return d


_FS_BROWSE_ROOTS = (
    ("/app/Link", "宿主 /vol1/1000/Link"),
    ("/app/data", "EvoLink 数据目录"),
)


def _fs_in_roots(d):
    return any(d == r or d.startswith(r + "/") for r, _ in _FS_BROWSE_ROOTS)


def _gen_fmt(v):
    """strm 格式归一：evolink / path / pc（其它一律 pc）。"""
    f = str(v or "").strip().lower()
    return f if f in ("evolink", "path", "pc") else "pc"


def _gen_base(cfg):
    """pc 型 strm 里写的地址基座：优先配置；没填就按 emby_url 宿主推（evolink 与 Emby 同在 NAS）。"""
    b = str((cfg or {}).get("strm_gen_base") or "").strip().rstrip("/")
    if b:
        return b
    try:
        u = urllib.parse.urlparse(str((cfg or {}).get("emby_url") or "").strip())
        if u.hostname:
            return f"http://{u.hostname}:9191"
    except Exception:
        pass
    return "http://127.0.0.1:9191"


def _gen_rel(src, keep_first, prefix):
    """115 相对路径 → strm 目标相对路径。

    只剥「与 prefix 相同的首段」（默认 prefix=115open）：从根目录扫的 src 以
    115open/ 开头，剥掉后与存量 Link 结构同构；直接从存储名层勾选扫描的 src
    不以它开头，就不剥（免得把分类目录当存储名误剥）。
    """
    parts = [x for x in str(src or "").strip("/").split("/") if x]
    prefix = (prefix or "").strip("/")
    if not keep_first and prefix and parts and parts[0] == prefix:
        parts = parts[1:]
    return "/".join(parts)


def _gen_path_content(src, cd2, prefix):
    """CD2 路径型 strm 内容：cd2 + prefix + src；src 已含 prefix 时不重复拼。"""
    src = str(src or "").strip("/")
    prefix = (prefix or "").strip("/")
    if prefix and not src.startswith(prefix + "/"):
        src = prefix + "/" + src
    return (cd2 or "").rstrip("/") + "/" + src


def _gen_evolink_content(src, base, prefix):
    """EvoLink 路径型 strm 内容：<base>/api/swap115/<URL 编码的 115 路径>。

    路径**保留存储名**（如 115open/Video/...）：pan115.resolve_file 的契约是
    「首段为存储名，自动跳过」，而 swap115 端点也按同一契约解析。
    与 pc 型相比：pickcode 不落盘 → 换 115 账号不用重建 strm。
    """
    src = str(src or "").strip("/")
    prefix = (prefix or "").strip("/")
    if prefix and not src.startswith(prefix + "/"):
        src = prefix + "/" + src
    if not src:
        return ""
    return "{}/api/swap115/{}".format((base or "").rstrip("/"),
                                       urllib.parse.quote(src, safe=""))


def _gen_bdmv_key(path):
    """BDMV 盘目录键：…/片名/BDMV/STREAM/x.m2ts → …/片名；不是盘内文件返 None。"""
    parts = [x for x in str(path or "").split("/") if x]
    low = [x.lower() for x in parts]
    if "bdmv" in low:
        i = low.index("bdmv")
        if i >= 1:
            return "/" + "/".join(parts[:i])
    return None


def _gen_walk(root, ck, depth, progress=None, max_items=_GEN_PLAN_MAX,
              anchor_root=None, anchor_prefix="", root_paths=None):
    """BFS 遍历（自带翻页，无单目录页数上限——pan115.walk 有 40 页 guard，超大目录会漏）。

    anchor_root / anchor_prefix：
      从 anchor_root（通常是 115 根 cid="0"）开始 BFS，路径前缀以 anchor_prefix 起头，
      这样得到的 it["path"] 是**从根出发的完整 115 路径**（如
      "115open/Video/Movie/欧美电影/阿凡达系列/xxx.mkv"）。
      原因：strm 内容里的路径必须是完整路径才能换链（只给相对路径必然 502）；
      而从「选中的目录」直接 BFS 得到的 path 是相对该目录的，缺上层。
      实现：把要扫的 root cid 记入 target 集合，从根 BFS 时只在命中 target 时
      才开始收集文件，路径前缀自然带上完整层级。
    """
    items, queue, guard = [], [(str(root or "0"), str(anchor_prefix or "").strip("/"), 1)], 0
    target = str(root or "0")
    if anchor_root is not None:
        # 根锚定：从 anchor_root 出发，先反查 target 的完整路径再从 target 收集
        anchor = str(anchor_root)
        pref = str(anchor_prefix or "").strip("/")
        return _gen_walk_by_anchor(anchor, target, pref, ck, depth, progress, max_items,
                                   root_paths=root_paths)
    while queue and len(items) < max_items:
        cid, prefix, lv = queue.pop(0)
        try:
            rows = pan115.list_dir(cid, ck)
        except pan115.Pan115Error as e:
            if progress:
                progress(f"  ! 跳过目录 {prefix or '/'}：{e}")
            continue
        for it in rows:
            it["path"] = f"{prefix}/{it['name']}" if prefix else it["name"]
            if it["is_dir"]:
                if lv < int(depth):
                    queue.append((it["cid"], it["path"], lv + 1))
            else:
                items.append(it)
        guard += 1
        time.sleep(0.1)                        # 目录间留情：列目录虽不敏感，也别连发成机枪
        if guard % 20 == 0 and progress:
            progress(f"  已读 {guard} 个目录，累计 {len(items)} 个文件")
    return items


_GEN_PATH_INDEX = {}          # cid → 完整路径（相对 115 根，不含存储名）
_GEN_PATH_INDEX_TS = 0.0
_GEN_PATH_INDEX_LOCK = threading.Lock()


def _gen_path_index(ck, max_depth=12, max_dirs=4000, progress=None):
    """从 115 根 BFS 建立「cid → 完整路径」索引。

    为什么需要：strm 内容里的 115 路径必须是**完整路径**
    （如 "Video/Movie/欧美电影/阿凡达系列/xxx.mkv"），缺任何一段换链都会 502。
    而用户在目录树上勾选的目录只有 cid，从 cid 出发 BFS 得到的路径是相对的。

    办法：从根目录 BFS 一遍，边走边记 cid→path。目录总量实测约 8700 个，
    list_dir 有 30 分钟目录缓存，第二次几乎瞬时。索引只建目录（不读文件列表），
    用 limit 截断防止超大账号卡死。

    返回 {cid: path} 字典（模块级缓存，5 分钟过期）。
    """
    global _GEN_PATH_INDEX, _GEN_PATH_INDEX_TS
    now = time.time()
    with _GEN_PATH_INDEX_LOCK:
        if _GEN_PATH_INDEX and now - _GEN_PATH_INDEX_TS < 300:
            return _GEN_PATH_INDEX
    idx = {}
    queue = [("0", "", 0)]
    seen = set()
    while queue and len(idx) < max_dirs:
        cid, path, lv = queue.pop(0)
        if cid in seen:
            continue
        seen.add(cid)
        try:
            rows = pan115.list_dir(cid, ck)
        except Exception:
            continue
        for it in rows:
            if not it.get("is_dir"):
                continue
            c = str(it.get("cid") or "")
            if not c:
                continue
            sub = (path + "/" + it["name"]) if path else it["name"]
            idx[c] = sub
            if lv < max_depth:
                queue.append((c, sub, lv + 1))
        if progress and len(seen) % 50 == 0:
            progress(f"  建立路径索引：已读 {len(seen)} 个目录，索引 {len(idx)} 项")
    with _GEN_PATH_INDEX_LOCK:
        _GEN_PATH_INDEX = idx
        _GEN_PATH_INDEX_TS = time.time()
    return idx


def _gen_path_of_cid(cid, ck, max_up=24):
    """由目录 cid 反查它在 115 根下的完整路径（不含存储名）。

    走 _gen_path_index（从根 BFS 建的 cid→path 索引）。
    """
    cid = str(cid or "").strip()
    if not cid or cid == "0":
        return ""
    idx = _gen_path_index(ck)
    p = idx.get(cid)
    if p:
        return p
    # 索引里没有：可能是深度超限或刚建，单独按名字兜底
    name = _gen_name_of_cid(cid, ck)
    if name:
        return _gen_path_by_name(name, ck, expect_cid=cid)
    return ""


def _gen_path_by_name(name, ck, max_up=24, expect_cid=""):
    """按**目录名**反查它在 115 根下的完整路径（不含存储名）。

    实测的 115 接口行为（files/search）：
      search(根cid, "<目录名>") → 命中项里 fc=0 的那条就是该目录本身，
      其 pid = 父目录 id、dp = 父目录名、n = 自己的名字。
    因为 115 里**存在重名目录**（实测：「演唱会」在 Video/Movie 与
    Video/Animation 下各有一个，按名字搜会匹配错），
    所以必须用 expect_cid 校验：只有 cid 对得上才算命中。
    """
    cur_name = str(name or "").strip()
    expect = str(expect_cid or "").strip()
    if not cur_name:
        return ""
    parts = []
    seen_names = set()
    for _ in range(int(max_up)):
        if cur_name in seen_names:
            return ""
        seen_names.add(cur_name)
        try:
            rows, _cnt = pan115._search_page("0", cur_name, ck, limit=100, offset=0)
        except Exception:
            return ""
        hit = None
        for r in (rows or []):
            if str(r.get("fc") or "0") != "0":
                continue
            if (r.get("n") or r.get("fn") or "") != cur_name:
                continue
            if expect and str(r.get("cid")) != expect:
                continue          # 重名目录：cid 对不上就跳过
            hit = r
            break
        if not hit:
            return ""
        parts.append(cur_name)
        parent = str(hit.get("pid") or "")
        if not parent or parent == "0":
            break
        cur_name = str(hit.get("dp") or "")     # 父目录名
        expect = parent                          # 下一轮的期望 cid = 本轮的父
        if not cur_name:
            return ""
    return "/".join(reversed(parts))


def _gen_walk_by_anchor(anchor_cid, target_cid, anchor_prefix, ck, depth,
                         progress=None, max_items=_GEN_PLAN_MAX, root_paths=None):
    """从 anchor_cid（115 根）定位 target_cid，收集其下所有文件，路径带完整前缀。

    步骤：先用 _gen_path_of_cid（走 files/search，逐级向上）反查 target 的完整路径，
    再从 target 正常 BFS 收集文件，并把完整路径作为前缀。
    这样得到的 it["path"] 形如
    "115open/Video/Movie/欧美电影/阿凡达系列/阿凡达：火与烬 (2025)/xxx.mkv"，
    是 strm 内容里换链所需的**完整路径**（缺任何一段都会 502）。

    比「从根往下 BFS 找 target」快得多：根下可能有几百个目录，BFS 要全扫一遍。
    """
    target_cid = str(target_cid or "").strip()
    pref = str(anchor_prefix or "").strip("/")
    # 优先用前端传来的完整路径（最可靠，零额外请求）
    rel = str((root_paths or {}).get(target_cid) or "").strip("/")
    if not rel:
        rel = _gen_path_of_cid(target_cid, ck)
    if not rel:
        if progress:
            progress(f"  ! 未能反查目录 {target_cid} 的完整路径（该目录可能已删除或为空）")
        return []
    full_prefix = (pref + "/" + rel) if pref else rel
    if progress:
        progress(f"  目录完整路径：{full_prefix}")
    return _gen_walk(target_cid, ck, depth, progress=progress,
                     max_items=max_items, anchor_prefix=full_prefix)


def _gen_name_of_cid(cid, ck):
    """查一个目录自己的名字（用路径索引；查不到时用 search 按名字反查兜底）。"""
    cid = str(cid or "").strip()
    if not cid or cid == "0":
        return ""
    idx = _gen_path_index(ck)
    p = idx.get(cid)
    if p:
        return p.rsplit("/", 1)[-1]
    return ""


def _gen_plan_save(plan):
    """plan 落盘（重启后还能接着写盘）；写失败不影响内存里的 plan。"""
    try:
        tmp = _GEN_PLAN_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(plan, ensure_ascii=False), "utf-8")
        os.replace(tmp, _GEN_PLAN_FILE)
    except Exception:
        pass


def _gen_plan_load():
    with _GEN_LOCK:
        if _GEN_PLAN.get("items"):
            return dict(_GEN_PLAN)
    try:
        d = json.loads(_GEN_PLAN_FILE.read_text("utf-8"))
        if isinstance(d, dict) and d.get("items"):
            return d
    except Exception:
        pass
    return {}


def task_strm_gen_plan(log, roots, depth, opts):
    """预览：walk 选中的 115 目录，算出「会写哪些 strm、内容是什么」，**不写盘**。

    roots：目录树勾选的 cid 列表（空 = 根目录）。
    opts：fmt(pc/path) / bdmv(skip/main) / keep_first / outdir / base / cd2 / prefix
    """
    cfg = config.load()
    ck = cfg.get("pan115_cookie") or {}
    if not ck.get("UID"):
        log("✗ 未登录 115，请先扫码")
        return
    roots = [str(x) for x in (roots or []) if str(x).strip()] or ["0"]
    fmt = _gen_fmt(opts.get("fmt"))
    bdmv = "main" if (opts.get("bdmv") == "main") else "skip"
    keep_first = bool(opts.get("keep_first"))
    outdir = _gen_safe_dir(opts.get("outdir")) or str(cfg.get("strm_gen_dir") or "/app/Link/gen115")
    log(f"开始预览：{len(roots)} 个目录，深度 {depth}，格式 {fmt}，BDMV {bdmv}，输出 {outdir}…")
    files = []
    _aprefix = str(opts.get("prefix") or cfg.get("strm_gen_prefix") or "115open")
    _rpaths = opts.get("root_paths") or {}
    for r in roots:
        # 路径必须完整（strm 内容里的路径用于换链，相对路径必然 502）：
        # 优先用前端传来的完整路径，否则从根反查
        files += _gen_walk(r, ck, depth, progress=log,
                           anchor_root="0", anchor_prefix=_aprefix,
                           root_paths=_rpaths)
    vids = [it for it in files if pan115.is_video(it["name"])]
    log(f"共读到 {len(files)} 个文件，其中视频 {len(vids)} 个；筛选中…")
    prog(0, max(1, len(vids)), "筛选")

    # 蓝光原盘：skip=整个跳过（含 iso）；main=每个盘取 STREAM 下最大 m2ts 代表
    bdmv_main, skipped = {}, {"bdmv": 0, "iso": 0}
    if bdmv == "main":
        for it in vids:
            k = _gen_bdmv_key(it["path"])
            if k and "/stream/" in it["path"].lower() and it["name"].lower().endswith(".m2ts"):
                cur = bdmv_main.get(k)
                if not cur or it["size"] > cur["size"]:
                    bdmv_main[k] = it
    sel = []
    for it in vids:
        low = it["path"].lower()
        if bdmv == "skip":
            if "/bdmv/" in low or low.endswith(".iso"):
                skipped["bdmv"] += 1
                continue
        else:
            if _gen_bdmv_key(it["path"]):
                continue                       # 盘内文件由主 m2ts 统一代表
            if low.endswith(".iso"):
                skipped["iso"] += 1
                continue
        sel.append(it)

    # 构造 plan 条目：rel=目标相对路径，name=strm 文件名，src=115 路径，pc=pickcode
    out, no_pc, dup_seen = [], 0, set()
    _prefix = str(opts.get("prefix") or cfg.get("strm_gen_prefix") or "115open")
    for it in sel:
        rel_dir = _gen_rel(it["path"].rsplit("/", 1)[0], keep_first, _prefix)
        stem = it["name"].rsplit(".", 1)[0] if "." in it["name"] else it["name"]
        rel = (rel_dir + "/" if rel_dir else "") + stem + ".strm"
        if rel in dup_seen:                   # 同名同路径去重（ walk 理论上不会重复，保险 ）
            continue
        dup_seen.add(rel)
        if fmt == "pc" and not (it.get("pc") or "").strip():
            no_pc += 1                        # pc 型必须要 pickcode，没有的只能跳过
            continue
        out.append({"rel": rel, "src": it["path"].strip("/"), "name": stem + ".strm",
                    "pc": (it.get("pc") or "").strip(), "size": int(it.get("size") or 0)})
    if bdmv == "main":
        for k, it in bdmv_main.items():
            rel = _gen_rel(k, keep_first, _prefix) + ".strm"
            if rel in dup_seen:
                continue
            dup_seen.add(rel)
            if fmt == "pc" and not (it.get("pc") or "").strip():
                no_pc += 1
                continue
            out.append({"rel": rel, "src": it["path"].strip("/"), "name": k.rsplit("/", 1)[-1] + ".strm",
                        "pc": (it.get("pc") or "").strip(), "size": int(it.get("size") or 0), "dir": True})

    total_size = sum(x["size"] for x in out)
    by_top = {}
    for x in out:
        top = (x["rel"].split("/", 1)[0] if "/" in x["rel"] else "(根)")
        by_top[top] = by_top.get(top, 0) + 1
    top_sorted = sorted(by_top.items(), key=lambda kv: -kv[1])[:8]
    _sbase = _gen_base(cfg)
    _sprefix = str(opts.get("prefix") or cfg.get("strm_gen_prefix") or "115open").strip("/")
    _scd2 = str(opts.get("cd2") or cfg.get("strm_gen_cd2") or "/CloudNAS/CloudDrive/").rstrip("/")
    def _sample_content(x):
        if fmt == "pc":
            return f"{_sbase}/api/redirect115/{x['pc']}"
        if fmt == "evolink":
            return _gen_evolink_content(x.get("src") or "", _sbase, _sprefix)
        return _scd2 + "/" + _sprefix + "/" + x["src"]
    sample = [{"rel": x["rel"], "size": x["size"], "content": _sample_content(x)}
              for x in out[:20]]
    stats = {"total": len(out), "size": total_size, "by_top": top_sorted,
             "no_pc": no_pc, "skipped": skipped, "sample": sample,
             "roots": roots, "depth": int(depth), "fmt": fmt, "bdmv": bdmv,
             "outdir": outdir, "keep_first": keep_first}
    plan = {"items": out, "opts": {"fmt": fmt, "bdmv": bdmv, "keep_first": keep_first,
                                   "outdir": outdir, "base": str(opts.get("base") or ""),
                                   "cd2": str(opts.get("cd2") or ""),
                                   "prefix": str(opts.get("prefix") or "")},
            "acct": str(ck.get("UID") or ""), "at": db.now(), "stats": stats}
    with _GEN_LOCK:
        _GEN_PLAN.clear()
        _GEN_PLAN.update(plan)
    _gen_plan_save(plan)
    gb = total_size / 1073741824
    log(f"✓ 预览完成：将生成 {len(out)} 个 strm（源片合计 {gb:.1f} GB），"
        f"跳过 原盘/iso {skipped['bdmv'] + skipped['iso']} 个，无 pickcode {no_pc} 个")
    log(f"目录分布 Top：{'、'.join(f'{k} {v}' for k, v in top_sorted) or '无'}")
    log("确认无误就点「开始生成」写盘（幂等：已存在且内容一致的 strm 会自动跳过）")


def task_strm_gen(log):
    """写盘：按预览计划生成 strm。幂等（同内容跳过），可中断重跑。"""
    plan = _gen_plan_load()
    if not plan.get("items"):
        log("✗ 没有生成计划，请先「预览生成计划」")
        return
    opts = plan.get("opts") or {}
    cfg = config.load()
    ck = cfg.get("pan115_cookie") or {}
    if not ck.get("UID"):
        log("✗ 未登录 115，请先扫码")
        return
    if plan.get("acct") and str(ck.get("UID") or "") != plan["acct"]:
        log("✗ 预览之后切换过 115 账号，pickcode 已失效，请重新预览")
        return
    fmt = _gen_fmt(opts.get("fmt"))
    outdir = _gen_safe_dir(opts.get("outdir")) or str(cfg.get("strm_gen_dir") or "/app/Link/gen115")
    base = (opts.get("base") or "").strip() or _gen_base(cfg)
    cd2 = (opts.get("cd2") or str(cfg.get("strm_gen_cd2") or "/CloudNAS/CloudDrive/")).strip().rstrip("/") + "/"
    prefix = (opts.get("prefix") or str(cfg.get("strm_gen_prefix") or "115open")).strip("/")
    items = plan["items"]
    total = len(items)
    log(f"开始生成 {total} 个 strm → {outdir}（格式 {fmt}）")
    try:
        os.makedirs(outdir, exist_ok=True)
    except Exception as e:
        log(f"✗ 建目录失败 {outdir}: {e}")
        return
    done = written = skipped = failed = 0
    for it in items:
        if fmt == "pc":
            if not it.get("pc"):
                failed += 1
                done += 1
                continue
            content = f"{base}/api/redirect115/{it['pc']}"
        elif fmt == "evolink":
            content = _gen_evolink_content(it.get("src") or "", base, prefix)
            if not content:
                failed += 1
                done += 1
                continue
        else:
            content = cd2 + prefix + "/" + (it.get("src") or "").strip("/")
        dst = os.path.join(outdir, it["rel"])
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.exists(dst):
                with open(dst, "r", encoding="utf-8", errors="ignore") as f:
                    if f.readline().strip() == content:
                        skipped += 1                       # 已存在且一致 → 跳过（幂等）
                        done += 1
                        if done % 1000 == 0:
                            prog(done, total, f"检查存量 {skipped}")
                        continue
            tmp = dst + ".gen-tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(content + "\n")
            os.replace(tmp, dst)                            # 原子替换，防半截 strm
            written += 1
        except Exception as e:
            failed += 1
            if failed <= 5:
                log(f"  ! 写失败 {it['rel']}: {e}")
        done += 1
        if done % 200 == 0 or done == total:
            prog(done, total, f"已写 {written} / 跳过 {skipped} / 失败 {failed}")
    log(f"✓ 完成：新写 {written}，跳过（已存在且一致）{skipped}，失败 {failed}，共 {done} → {outdir}")
    log("提示：到 Emby / Jellyfin / Plex 对应媒体库点「扫描媒体库文件」，新片才会出现")


# redirect115 端点限流（每 IP 每分钟 60 次；播放一部片子会打几次，宽松放行）
_R115_LOCK = threading.Lock()
_R115_BUCKET = {}
_R115_MAX = 60


# ---------------- 自动增量同步 ----------------
def auto_sync_once(log=None):
    """跑一轮更新：索引 Emby → 增量发行片 → 增量 AV → 里番 → 榜单 → 比对。"""
    log = log or (lambda *a: None)
    cfg = config.load()
    auto = dict(AUTO_DEFAULTS)
    auto.update(cfg.get("auto") or {})
    if auto.get("sync_emby"):
        log("--- 更新：索引 Emby ---")
        task_sync_emby(log)
        # 开启 Emby 自动索引时，一并保持 Jellyfin / Playable 库存同步（三端一致）
        try:
            log("--- 更新：索引 Jellyfin ---")
            task_sync_jellyfin(log)
        except Exception as e:
            log(f"  ! Jellyfin 索引失败：{e}")
        try:
            log("--- 更新：索引 Playable ---")
            task_sync_playable(log)
        except Exception as e:
            log(f"  ! Playable 索引失败：{e}")
        if (config.load().get("plex_token") or "").strip():
            try:
                log("--- 更新：索引 Plex ---")
                task_sync_plex(log)
            except Exception as e:
                log(f"  ! Plex 索引失败：{e}")
    if auto.get("fetch"):
        log("--- 更新：发行片（增量）---")
        task_fetch_catalog(log, incremental=True)
    if auto.get("fetch_av"):
        log("--- 更新：AV 番号（增量）---")
        task_fetch_av(log, incremental=True)
    if auto.get("fetch_hentai"):
        log("--- 更新：里番（增量）---")
        try:
            task_fetch_hentai(log, incremental=True)
        except Exception as e:
            log(f"  ! 里番更新失败：{e}")
    if auto.get("fetch_lists"):
        log("--- 更新：榜单 ---")
        try:
            task_fetch_list(log)
        except Exception as e:
            log(f"  ! 榜单更新失败：{e}")
    if auto.get("match"):
        log("--- 更新：比对 ---")
        task_match(log)
    db.set_meta("auto_run_at", db.now())


def _auto_slot(auto):
    """今天该跑的时间点（已过才返回，格式 YYYY-MM-DD HH:MM）。"""
    try:
        hh, mm = [int(x) for x in str(auto.get("time") or "00:00").split(":")[:2]]
    except Exception:
        hh, mm = 0, 0
    now = time.localtime()
    slot = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, hh, mm, 0, 0, 0, -1))
    if time.time() < slot:
        return None
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(slot))


def scheduler_loop():
    """后台常驻：每天到点跑一轮更新（mode=daily）。"""
    time.sleep(20)   # 启动后先缓 20 秒，避开首次索引
    while True:
        try:
            cfg = config.load()
            auto = dict(AUTO_DEFAULTS)
            auto.update(cfg.get("auto") or {})
            if auto.get("enabled") and (auto.get("mode") or "daily") == "daily":
                slot = _auto_slot(auto)
                if slot and db.get_meta("auto_slot") != slot \
                        and not TASKS.get("auto", {}).get("running") \
                        and not TASKS.get("立即更新", {}).get("running"):
                    def job(lg, _slot=slot):
                        auto_sync_once(lg)
                        db.set_meta("auto_slot", _slot)
                    run_task("立即更新", job)
        except Exception as e:
            print(f"[scheduler] {e}", flush=True)
        time.sleep(60)



# ---------------- 登录保护 ----------------
# 在 evolink 页面（9191）前面加一道账号密码：默认 admin/admin。
# - 口令只存 PBKDF2 哈希（config.json 里没有明文）
# - 会话是无状态签名 Cookie（HMAC-SHA256），容器重启后仍有效，7 天过期
# - 改账号/密码后「凭据版本」变化 → 旧会话自动失效（当前浏览器自动换发新 Cookie）
# - 本机来源（127.0.0.1）放行：docker healthcheck 打的 /api/stats、NAS 本地工具
# - 反代端口（32411 / 32401 / 8099 等）不受影响 —— 那些是独立的 HTTP server

AUTH_COOKIE = "el_session"
AUTH_TTL = 7 * 24 * 3600
AUTH_DEFAULT_USER = "admin"
AUTH_DEFAULT_PASS = "admin"
AUTH_DEFAULT_SALT = "evolink-default-salt"
# 这些路径不需要登录（登录页自己、登录/登出接口、favicon）
AUTH_OPEN_PATHS = ("/login", "/api/login", "/logout", "/api/logout", "/favicon.ico", "/api/thumb", "/api/poster")
# EVOLINK:LICENSE:HELPER
# 授权模块缺失时的最小免费白名单（正常路径由 core.license_ops 提供）
_LICENSE_FREE_MIN = frozenset({
    "/api/login", "/api/logout", "/api/settings", "/api/settings/auth",
    "/api/version", "/api/progress", "/api/stats", "/api/dash",
    "/api/fetch-state", "/api/compare", "/api/match", "/api/catalog",
    "/api/thumb", "/api/poster",
})


def _license_allowed(path, cfg):
    """服务端授权闸门：免费白名单放行，其余 /api/ 路由需要专业版。

    绝不因为模块缺失 / 校验失败就放行全部功能（Docker-Link 的
    enabled=false 语义不适用于 EvoLink：新产品默认必须是免费版）。
    """
    if license_ops is not None:
        return license_ops.path_allowed(path, cfg)
    if not path.startswith("/api/"):
        return True
    if path.startswith("/api/115/") or path.startswith("/api/license/"):
        return True
    return path in _LICENSE_FREE_MIN
# /EVOLINK:LICENSE:HELPER


def _ip_local(ip):
    """是否视为「本机/局域网」可信来源：loopback + RFC1918 私有 + link-local。

    公网 IP 返回 False。bridge 部署下宿主机访问容器时容器看到的来源是网桥网关
    （如 172.17.0.1，属 172.16/12 私有网段），也应免登录。
    """
    ip = (ip or "").strip()
    if ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"):
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_loopback or addr.is_private or addr.is_link_local


def _auth_secret(cfg=None):
    """HMAC 签名密钥：存在 config 里（首次自动生成），重启后保持不变。"""
    cfg = cfg if isinstance(cfg, dict) else config.load()
    s = str(cfg.get("auth_secret") or "").strip()
    if not s:
        s = secrets.token_hex(32)
        try:
            config.update(auth_secret=s)
        except Exception:
            pass
    return s


def _pw_hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"),
                               str(salt).encode("utf-8"), 120000).hex()


def auth_credentials(cfg=None):
    """当前账号 / 盐 / 口令哈希。auth_hash 为空 = 还没改过，仍用默认 admin/admin。"""
    cfg = cfg if isinstance(cfg, dict) else config.load()
    user = str(cfg.get("auth_user") or "").strip() or AUTH_DEFAULT_USER
    salt = str(cfg.get("auth_salt") or "") or AUTH_DEFAULT_SALT
    h = str(cfg.get("auth_hash") or "")
    if not h:
        h = _pw_hash(AUTH_DEFAULT_PASS, salt)
    return user, salt, h


def auth_verify(cfg, user, password):
    """账号口令是否正确。"""
    u, salt, h = auth_credentials(cfg)
    if str(user or "") != u:
        return False
    return hmac.compare_digest(_pw_hash(password or "", salt), h)


def _cred_ver(cfg=None):
    """凭据版本号：改账号/密码后变化 → 旧会话失效。"""
    u, salt, h = auth_credentials(cfg)
    return hashlib.sha256(("%s|%s|%s" % (u, salt, h)).encode()).hexdigest()[:16]


def auth_make_token(user, cfg=None):
    """签发会话 token：payload = 用户|过期|凭据版本，再 HMAC 签名。"""
    cfg = cfg if isinstance(cfg, dict) else config.load()
    exp = int(time.time()) + AUTH_TTL
    raw = base64.urlsafe_b64encode(
        ("%s|%d|%s" % (user, exp, _cred_ver(cfg))).encode()).decode().rstrip("=")
    sig = hmac.new(_auth_secret(cfg).encode(), raw.encode(), hashlib.sha256).hexdigest()
    return raw + "." + sig


def auth_read_token(token, cfg=None):
    """校验签名/有效期/凭据版本；通过返回用户名，否则 None。"""
    try:
        raw, sig = str(token).split(".", 1)
        cfg = cfg if isinstance(cfg, dict) else config.load()
        want = hmac.new(_auth_secret(cfg).encode(), raw.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, want):
            return None
        pad = "=" * (-len(raw) % 4)
        payload = base64.urlsafe_b64decode(raw + pad).decode()
        user, exp, ver = payload.split("|", 2)
        if int(exp) < time.time():
            return None
        if ver != _cred_ver(cfg):
            return None            # 改了账号/密码 → 旧会话作废
        return user
    except Exception:
        return None


# ---------------- HTTP ----------------
class Handler(BaseHTTPRequestHandler):
    server_version = "EvoLink/%s" % APP_VERSION

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200, headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or []):
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    @staticmethod
    def _q_get(src, key, default=""):
        """统一取 GET 查询参数。

        src 可能是两种东西：
          - do_GET 里的取值函数 g（lambda k, d=None: q.get(k, [d])[0]）
          - 一个 dict（POST 解析出来的 body）
        早期有 handler 以为 g 是 dict，直接 body.get(...)，会抛
        AttributeError: 'function' object has no attribute 'get'，
        表现是请求直接把连接掐断（客户端看到 RemoteDisconnected）。
        这里统一处理，两种传入方式都能用。
        """
        v = default
        if callable(src):
            try:
                v = src(key, default)
            except TypeError:
                v = default
        elif isinstance(src, dict):
            v = src.get(key, default)
        if isinstance(v, (list, tuple)):
            v = v[0] if v else default
        return "" if v is None else str(v)

    # ---------------- 登录保护 ----------------
    def _client_ip(self):
        # 仅当用户显式开启 trust_proxy 才采信 X-Forwarded-For / X-Real-IP，
        # 否则攻击者可伪造这些头冒充局域网免登录。
        try:
            if config.load().get("trust_proxy"):
                xff = self.headers.get("X-Forwarded-For")
                if xff:
                    return xff.split(",")[0].strip()
                xri = self.headers.get("X-Real-IP")
                if xri:
                    return xri.strip()
        except Exception:
            pass
        try:
            return self.client_address[0] or ""
        except Exception:
            return ""

    def _is_https(self):
        # 直连时 BaseHTTPRequestHandler 不感知 scheme；靠受信反代透传的 X-Forwarded-Proto 判断
        return (self.headers.get("X-Forwarded-Proto", "") or "").lower() == "https"

    def _session_cookie(self, tok, max_age=AUTH_TTL):
        c = "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d" % (AUTH_COOKIE, tok, max_age)
        if self._is_https():
            c += "; Secure"
        return c

    def _need_login(self, path):
        """这条请求要不要先登录。"""
        if path in AUTH_OPEN_PATHS:
            return False
        # URL 型 strm 的播放落点：播放器不带 evolink 会话，显式放行（端点自带限流 + pickcode 校验）
        if path.startswith("/api/redirect115/"):
            return False
        if path.startswith("/api/swap115/"):
            return False
        try:
            cfg = config.load()
        except Exception:
            return True
        ip = self._client_ip()
        # 局域网站点：loopback + 私有网段免登录（auth_trust_lan 默认开，公网仍要求登录）
        if _ip_local(ip) and cfg.get("auth_trust_lan", True):
            return False
        if not cfg.get("auth_enabled", True):
            return False
        return True

    def _session_user(self):
        try:
            c = SimpleCookie()
            c.load(self.headers.get("Cookie") or "")
        except Exception:
            return None
        m = c.get(AUTH_COOKIE)
        return auth_read_token(m.value) if m else None

    def _require_login(self, path):
        """未登录：API 返 401 JSON，页面 302 跳登录页。放行返回 True。"""
        if not self._need_login(path):
            return True
        if self._session_user():
            return True
        if path.startswith("/api/"):
            self._json({"error": "未登录", "need_login": True}, 401)
        else:
            nxt = urllib.parse.quote(path, safe="")
            self.send_response(302)
            self.send_header("Location", "/login?next=" + nxt)
            self.send_header("Content-Length", "0")
            self.end_headers()
        return False

    # ---------------- 登录相关接口 ----------------
    def api_login(self, body):
        cfg = config.load()
        user = str(body.get("user") or "").strip()
        pw = str(body.get("pass") or "")
        if not auth_verify(cfg, user, pw):
            return self._json({"ok": False, "error": "账号或密码不正确"}, 401)
        tok = auth_make_token(user, cfg)
        return self._json({"ok": True, "user": user}, 200,
                          [("Set-Cookie", self._session_cookie(tok))])

    def api_logout(self):
        return self._json({"ok": True}, 200,
                          [("Set-Cookie", self._session_cookie("", 0))])

    # ───────────── 索引结果：按来源只读浏览 owned ─────────────
    # 四个源都写进同一张 owned；「媒体库 / 仪表盘」是按 library 名称合并的口径，
    # 看不出条目来自哪个源。下面两个接口只做只读 SELECT，不碰索引与比对逻辑。
    _OWNED_SRC_ORDER = ("emby", "plex", "playable", "jellyfin")

    def api_owned_sources(self):
        """每个索引源的条目总数、按媒体库分组明细，以及该源上次索引进度。"""
        c = db.conn()
        rows = c.execute(
            "SELECT COALESCE(src,'emby') s, COALESCE(library,'') lib, COUNT(*) n "
            "FROM owned GROUP BY s, lib ORDER BY s, n DESC").fetchall()
        # 进度是内存态，容器重启后会清空；用每行写入时间兜底，保证「上次索引」始终有据可查。
        last_at = {r[0]: (r[1] or "") for r in c.execute(
            "SELECT COALESCE(src,'emby'), MAX(updated_at) FROM owned "
            "GROUP BY COALESCE(src,'emby')").fetchall()}
        agg = {}
        for r in rows:
            d = agg.setdefault(r["s"], {"src": r["s"], "total": 0, "libs": []})
            d["total"] += r["n"]
            if r["lib"]:
                d["libs"].append({"name": r["lib"], "count": r["n"]})
        keys = [k for k in self._OWNED_SRC_ORDER if k in agg]
        keys += [k for k in agg if k not in self._OWNED_SRC_ORDER]
        for k in keys:
            pr = SRC_PROG.get(k) or {}
            agg[k]["progress"] = {"msg": pr.get("msg", ""), "at": pr.get("at", ""),
                                  "ok": pr.get("ok"), "running": bool(pr.get("running"))}
            agg[k]["last_indexed"] = last_at.get(k, "")
        return self._json({"ok": True, "sources": [agg[k] for k in keys],
                           "total": sum(agg[k]["total"] for k in keys),
                           "last_sync": db.get_meta("owned_synced_at") or ""})

    def api_owned_items(self, g):
        """按来源 / 媒体库 / 关键字分页浏览索引进来的真实条目（只读）。"""
        src = str(g("src", "") or "").strip().lower()
        lib = str(g("lib", "") or "").strip()
        q = str(g("q", "") or "").strip()
        try:
            page = max(1, int(g("page", 1) or 1))
            size = max(1, min(int(g("size", 20) or 20), 100))
        except (TypeError, ValueError):
            return self._json({"ok": False, "error": "bad page/size"}, 400)
        w, a = ["1=1"], []
        if src:
            if src not in self._OWNED_SRC_ORDER:
                return self._json({"ok": False, "error": "unknown src"}, 400)
            w.append("src=?")
            a.append(src)
        if lib:
            w.append("library=?")
            a.append(lib)
        if q:
            w.append("(name LIKE ? OR COALESCE(path,'') LIKE ? OR COALESCE(emby_id,'') LIKE ?)")
            a += ["%" + q + "%"] * 3
        where = " AND ".join(w)
        c = db.conn()
        total = c.execute("SELECT COUNT(*) FROM owned WHERE " + where, a).fetchone()[0]
        rows = c.execute(
            "SELECT emby_id, name, year, type, library, path, tmdb_id, imdb_id, src "
            "FROM owned WHERE " + where +
            " ORDER BY name COLLATE NOCASE, emby_id LIMIT ? OFFSET ?",
            a + [size, (page - 1) * size]).fetchall()
        items = [{"emby_id": r["emby_id"], "name": r["name"], "year": r["year"],
                  "type": r["type"], "library": r["library"], "path": r["path"],
                  "tmdb_id": r["tmdb_id"], "imdb_id": r["imdb_id"], "src": r["src"]}
                 for r in rows]
        return self._json({"ok": True, "total": total, "page": page,
                           "size": size, "items": items})

    # ───────────── Plex 账号授权（pin 流程） ─────────────
    def api_plex_auth_start(self, body):
        """第一步：向 plex.tv 要一个授权 pin，返回给用户打开的 app.plex.tv 授权链接。"""
        cur = config.load()
        cid = str(body.get("client_id") or cur.get("plex_client_id") or "").strip()
        if not cid:
            cid = "evolink-" + secrets.token_hex(8)
        try:
            pin_id, code, cid_real = _plex_create_pin(cid)
        except Exception as e:
            return self._json({"ok": False, "error": f"无法连接 plex.tv：{e}"}, 502)
        if not pin_id or not code:
            return self._json({"ok": False, "error": "plex.tv 未返回 pin"}, 502)
        cur["plex_client_id"] = cid_real or cid
        config.save(cur)
        # 官方授权页：用户在此登录并点「授权 / Connect」，EvoLink 随后轮询取回 token
        # （context[device][...] 用 URL 编码的方括号，注意不要用 % 格式化——串里有字面量 %）
        auth_url = ("https://app.plex.tv/auth#!?clientID=" + urllib.parse.quote(cur["plex_client_id"])
                    + "&code=" + urllib.parse.quote(code)
                    + "&context%5Bdevice%5D%5Bproduct%5D=EvoLink"
                    + "&context%5Bdevice%5D%5Bversion%5D=1.0"
                    + "&context%5Bdevice%5D%5Bplatform%5D=Web"
                    + "&context%5Bdevice%5D%5BdeviceName%5D=EvoLink")
        return self._json({"ok": True, "pin_id": pin_id, "client_id": cur["plex_client_id"],
                           "auth_url": auth_url})

    def api_plex_auth_poll(self, body):
        """第二步：轮询 pin 授权结果，用户批准后收回 token 并写入配置。"""
        pin_id = str(body.get("pin_id") or "").strip()
        cid = str(body.get("client_id") or "").strip()
        if not pin_id:
            return self._json({"ok": False, "error": "缺少 pin_id"}, 400)
        try:
            token, username = _plex_poll_pin(pin_id, cid)
        except Exception as e:
            return self._json({"ok": False, "error": f"轮询失败：{e}"}, 502)
        if not token:
            return self._json({"ok": True, "authorized": False})
        cur = config.load()
        cur["plex_token"] = token
        cur["plex_client_id"] = cid or cur.get("plex_client_id") or ""
        config.save(cur)
        return self._json({"ok": True, "authorized": True, "username": username,
                           "token_len": len(token)})

    def api_plex_libraries(self, body):
        """用已存 token 列本地 Plex 媒体库 —— 证明授权真的生效、库内容拿得到。"""
        cur = config.load()
        token = str(body.get("token") or cur.get("plex_token") or "").strip()
        if not token:
            return self._json({"ok": False, "error": "尚未授权 Plex，请先登录"}, 400)
        base = str(body.get("base") or cur.get("plex_base") or "http://127.0.0.1:32400").strip()
        try:
            libs = _plex_list_libraries(token, base)
        except Exception as e:
            return self._json({"ok": False, "error": f"读取媒体库失败：{e}"}, 502)
        return self._json({"ok": True, "count": len(libs), "libraries": libs})

    def api_auth_settings(self, body):
        """改账号 / 密码。改完凭据版本变化、旧会话失效；当前浏览器换发新 Cookie 保持登录。"""
        cur = config.load()
        user = str(body.get("user") or "").strip()
        pw = str(body.get("pass") or "")
        pw2 = str(body.get("pass2") or "")
        if not user:
            return self._json({"ok": False, "error": "账号不能为空"}, 400)
        if len(pw) < 3:
            return self._json({"ok": False, "error": "密码至少 3 位"}, 400)
        if pw != pw2:
            return self._json({"ok": False, "error": "两次输入的密码不一致"}, 400)
        salt = secrets.token_hex(16)
        cur["auth_user"] = user
        cur["auth_salt"] = salt
        cur["auth_hash"] = _pw_hash(pw, salt)
        cur["auth_enabled"] = True
        config.save(cur)
        tok = auth_make_token(user, cur)
        return self._json({"ok": True, "user": user}, 200,
                          [("Set-Cookie", self._session_cookie(tok))])

    def _file(self, path, ctype=None, cache=0):
        if not path.exists():
            return self._json({"error": "not found"}, 404)
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype or mimetypes.guess_type(str(path))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", f"max-age={cache}" if cache else "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p, q = u.path, urllib.parse.parse_qs(u.query)
        g = lambda k, d=None: q.get(k, [d])[0]

        # EVOLINK:LICENSE:GET
        # 授权闸门：必须在所有路由分发之前（含登录豁免 / webhook / 302 落点），
        # 否则免费用户可手工构造 strm 打 /api/redirect115/ 直接绕过。
        # /api/stats 带分类维度参数时取该维度的统计（国际版为 AV 库维度），
        # 属于专业版能力；裸 /api/stats 仍免费（容器 HEALTHCHECK 命中它）。
        _lp = p
        if p == "/api/stats" and (g("category") or "").strip().upper():
            _lp = "/api/stats/av"

        if p.startswith("/api/") and not _license_allowed(_lp, config.load()):
            return self._json({
                "error": "license_required",
                "message": "该功能需要专业版（一次性买断 68 元），请在「许可证」中激活",
                "need_plan": "pro",
            }, 402)
        if p == "/api/license/status":
            return self._json(license_ops.status(config.load()) if license_ops
                              else {"plan": "free", "pro": False, "activated": False,
                                    "message": "授权模块缺失，当前为免费版"})

        # /EVOLINK:LICENSE:GET
        if p == "/login.html":
            # /login.html 别名重定向到 /login，避免用户手敲 .html 后缀踩 404
            self.send_response(302)
            self.send_header("Location", "/login")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if p == "/login":
            # 已登录就别再看登录页，直接回首页（带 ?next= 时回原目标）
            if self._session_user():
                nxt = g("next", "") or "/"
                if not nxt.startswith("/"):
                    nxt = "/"
                self.send_response(302)
                self.send_header("Location", nxt)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._file(WEB / "login.html", "text/html; charset=utf-8")
        if p == "/favicon.ico":
            return self._json({"error": "not found"}, 404)
        if not self._require_login(p):
            return
        if p == "/" or p == "/index.html":
            return self._file(WEB / "index.html", "text/html; charset=utf-8")
        if p.startswith("/static/"):
            fp = (WEB / p[8:]).resolve()
            web_root = WEB.resolve()
            if fp != web_root and not str(fp).startswith(str(web_root) + os.sep):
                return self._json({"error": "forbidden"}, 403)
            return self._file(fp, cache=3600)
        if p == "/api/stats":
            cat = g("category", "")
            bk = g("bucket", "")
            # stats/compare 在 27 万行库上要 7~8 秒（compare 的 _EFF_JOIN 两次全表分组聚合），
            # 健康探针 4s 超时→容器一直 unhealthy、前端打开也干等。数据只在 catalog 写入时变，
            # 故按 (分类,子类,catalog版本) 缓存 + TTL 兜底；catalog 写入会 facets_flush()
            # 递增 _CAT_VER 使缓存失效。命中缓存不查库，也顺带免疫 integrity_check 抢 IO 的超时。
            # TTL 必须显著大于 healthcheck 探测间隔（镜像里是 60s）。原来也写 60s，
            # 探测时刻刚好落在缓存过期之后 → 每轮都重算 → 容器永远 unhealthy。
            # 取 600s：探测必然命中缓存，前端连续打开仪表盘也不用每次干等 7-8 秒。
            skey = (cat, bk, _CAT_VER)
            _now = time.time()
            with _STATS_LOCK:
                _hit = _STATS_CACHE.get(skey)
            if _hit and _hit[0] > _now:
                return self._json(_hit[1])
            d = db.stats(cat)
            d["av_lib"] = av_lib_stats()
            d["cat_lib"] = cat_lib_stats(cat)
            # 一张表回答「这一年发行片多少 / 本地多少 / 差多少」，前端不再自己拿两套数字相减
            d["compare"] = db.compare(cat, bk)
            with _STATS_LOCK:
                _STATS_CACHE[skey] = (_now + 600, d)
                if len(_STATS_CACHE) > 64:
                    _STATS_CACHE.clear()
            return self._json(d)
        if p == "/api/dash":
            # 仪表盘聚合接口：一次请求给全前端要的数。
            # 之前要发 10 个请求（1 全量 stats + 8 按分类 stats + 反代 + 115），
            # 8 个分类并行实测 4 秒 —— 打开仪表盘等于干等 4 秒，改成后端一次算完。
            _c = config.load()
            _a = dict(_c.get("auto") or {})
            # 复用 /api/stats 的缓存（同一个 key：category="" bucket=""）：
            # 仪表盘和统计页算的是同一个 db.stats("")，没必要再查一遍 27 万行库。
            skey_d = ("", "", _CAT_VER)
            _nowd = time.time()
            with _STATS_LOCK:
                _hitd = _STATS_CACHE.get(skey_d)
            st = _hitd[1] if (_hitd and _hitd[0] > _nowd) else db.stats()
            st["gap_by_category"] = db.gap_by_category()
            st["proxy"] = {
                "ports": reverse_proxy.running_ports(),
                "jellyfin": bool(_c.get("jellyfin_enabled")),
                "jellyfin_port": int(_c.get("jellyfin_port") or 0),
                "plex": bool(_c.get("plex_enabled")),
                "plex_port": int(_c.get("plex_port") or 0),
                "playable": bool(_c.get("playable_enabled")),
                "playable_port": int(_c.get("playable_port") or 0),
            }
            st["auto"] = {"enabled": bool(_a.get("enabled")), "hours": _a.get("hours", 12)}
            return self._json(st)

        if p == "/api/fetch-state":
            return self._json(fetch_state())
        if p == "/api/link-trash":
            return self.api_link_trash()
        if p == "/api/link-scan":
            return self.api_link_scan()
        if p == "/api/strm-gen/status":
            return self.api_strm_gen_status(g)
        if p == "/api/strm/libs":
            return self.api_strm_libs()
        if p == "/api/strm/runs":
            return self.api_strm_runs(g)
        if p == "/api/archive/config":
            return self.api_archive_config({})
        if p == "/api/weight/config":
            return self.api_weight_config({})
        if p == "/api/weight/rules":
            return self.api_weight_rules({})
        if p == "/api/weight/rules/get":
            return self.api_weight_rule_get(g)
        if p == "/api/weight/rules/export":
            return self.api_weight_rule_export(g)
        if p == "/api/classify/config":
            return self.api_classify_config({})
        if p == "/api/classify/templates":
            return self.api_classify_templates({})
        if p == "/api/classify/dict":
            return self.api_classify_dict({})
        if p == "/api/strm/webhook/config":
            return self.api_strm_webhook_config({})
        if p == "/api/strm/webhook/logs":
            return self.api_strm_webhook_logs(g)
        if p == "/api/strm/webhook/test":
            return self.api_strm_webhook_test({})
        if p.startswith("/api/redirect115/"):
            return self.api_redirect115(p[len("/api/redirect115/"):])
        if p.startswith("/api/swap115/"):
            return self.api_swap115(p[len("/api/swap115/"):])
        if p == "/api/av/lib":
            return self._json(db.av_items(q=g("q", ""), lib=g("lib", ""),
                                          series=g("series", ""), decade=g("decade", ""),
                                          scope=g("scope", "jp"),
                                          page=g("page", 1), size=g("size", 100)))
        if p == "/api/local/items":
            return self._json(db.local_items(category=g("category", ""), bucket=g("bucket", ""),
                                             lib=g("lib", ""), series=g("series", ""),
                                             decade=g("decade", ""), q=g("q", ""),
                                             page=g("page", 1), size=g("size", 100)))
        if p in ("/api/emby/select", "/api/emby/libs"):
            return self._json(self.api_emby_select_get())
        if p == "/api/emby/servers":
            return self._json(self.api_emby_servers_get())
        if p == "/api/emby/probe":
            return self._json(emby.probe(g("url", "") or "", g("key", "") or ""))
        if p == "/api/thumb":
            return self.api_thumb(g("id", ""), g("tag", ""), g("w", "240"))
        if p == "/api/libraries":
            try:
                return self._json(emby.libraries())
            except Exception as e:
                return self._json({"error": str(e)}, 502)
        if p == "/api/lib-prog":
            # 「比对」面板：按库进度 + 是否有任务在跑
            t = TASKS.get("比对") or {}
            t2 = TASKS.get("扫描重复") or {}
            return self._json({
                "libs": lib_prog_snapshot(),
                "match_running": bool(t.get("running")),
                "dup_running": bool(t2.get("running")),
            })
        if p == "/api/lib-list":
            # 轻量库清单（只 COUNT owned，几十毫秒）。
            # 不能用 /api/stats：它顺带算 compare()，27 万行库上要一分钟，
            # 比对面板会被拖得打不开。
            c = db.conn()
            aw, aa = db.active_sql("library")
            rows = c.execute("SELECT library, COUNT(*) n FROM owned WHERE " + aw +
                             " GROUP BY library ORDER BY n DESC", aa).fetchall()
            return self._json({"libraries": [{"name": r["library"], "count": r["n"]}
                                             for r in rows]})
        if p == "/api/owned/sources":
            return self.api_owned_sources()
        if p == "/api/owned/items":
            return self.api_owned_items(g)
        if p == "/api/dup-result":
            lib = g("lib", "")
            d = DUP_RESULT.get(lib)
            if d is None:
                return self._json({"ok": False, "error": "没有该库的扫描结果"})
            return self._json({"ok": True, "lib": lib, "groups": d["groups"],
                               "summary": d["summary"], "scope": d["scope"]})
        if p == "/api/catalog":
            return self.api_catalog(g)
        if p == "/api/compare":
            return self._json(self.api_compare(g))
        if p == "/api/version":
            return self._json({"name": "EvoLink", "version": APP_VERSION,
                               "beta": BETA, "build": BUILD})
        if p == "/api/proxy/status":
            return self._json({
                "ports": reverse_proxy.running_ports(),
                "jellyfin_enabled": bool((config.load().get("jellyfin_enabled"))),
                "jellyfin_port": int(config.load().get("jellyfin_port") or 0),
                "emby_proxy_enabled": bool(config.load().get("emby_proxy_enabled")),
                "emby_proxy_port": int(config.load().get("emby_proxy_port") or 0),
                "playable_enabled": bool(config.load().get("playable_enabled")),
                "playable_port": int(config.load().get("playable_port") or 0),
            })
        if p == "/api/proxy/log":
            try:
                lim = int(g("limit", "400"))
            except (TypeError, ValueError):
                lim = 400
            return self._json({"events": reverse_proxy.recent_events(lim),
                               "now": time.time()})
        if p == "/api/settings":
            cfg = config.load()
            # 登录账号回显给前端；口令哈希 / 盐 / 签名密钥一律不下发
            cfg["auth_user"] = auth_credentials(cfg)[0]
            for _k in ("auth_hash", "auth_salt", "auth_secret"):
                cfg.pop(_k, None)
            # 密钥回显脱敏；同时给前端一个「是否已配置」的布尔，避免把掩码误当成已填
            cfg["tmdb_key_set"] = bool((cfg.get("tmdb_api_key") or "").strip())
            cfg["emby_key_set"] = bool((cfg.get("emby_api_key") or "").strip())
            cfg["fallback_channel_pass_set"] = bool((cfg.get("fallback_channel_pass") or "").strip())
            cfg["emby_api_key"] = mask(cfg.get("emby_api_key", ""))
            cfg["tmdb_api_key"] = mask(cfg.get("tmdb_api_key", ""))
            cfg["fallback_channel_pass"] = mask(cfg.get("fallback_channel_pass", ""))
            cfg["re0_direct_pass"] = mask(cfg.get("re0_direct_pass", ""))
            cfg["re0_cookies_set"] = bool((cfg.get("re0_direct_cookies") or "").strip())
            cfg["re0_direct_cookies"] = mask(cfg.get("re0_direct_cookies", ""))
            # EVOLINK:LICENSE:SETTINGS
            # 授权激活码只回掩码，绝不回明文（前端状态请用 /api/license/status）
            _lic = cfg.get("license")
            _lic = _lic if isinstance(_lic, dict) else {}
            cfg["license"] = {
                "server": _lic.get("server") or "http://182.255.84.109:9090",
                "key": license_ops.key_mask(_lic.get("key") or "") if license_ops else "",
                "key_set": bool((_lic.get("key") or "").strip()),
            }
            # /EVOLINK:LICENSE:SETTINGS
            # 身份槽：绝不下发明文 cookie / 密码，只给「是否已配置」
            _ru = cfg.get("re0_users")
            if isinstance(_ru, dict):
                _safe_ru = {}
                for _uid, _s in _ru.items():
                    _s = _s if isinstance(_s, dict) else {}
                    _safe_ru[_uid] = {
                        "name": _s.get("name") or _uid,
                        "user": _s.get("user") or "",
                        "proxy": _s.get("proxy") or "",
                        "cookies_captured": bool((_s.get("cookies") or "").strip()),
                        "pass_set": bool(_s.get("pass")),
                        # 前端用它决定默认走直连还是回落兜底通道
                        "configured": bool(
                            ((_s.get("user") or "").strip() and _s.get("pass"))
                            or (_s.get("cookies") or "").strip()),
                    }
                cfg["re0_users"] = _safe_ru
            cfg["specs"] = cfg.get("specs") or DEFAULT_SPECS
            _a = dict(AUTO_DEFAULTS)
            _a.update(cfg.get("auto") or {})
            cfg["auto"] = _a
            cfg["last_auto"] = db.get_meta("auto_run_at")
            cfg["auto_slot"] = db.get_meta("auto_slot")
            # 重启续跑：给前端「还有哪几个任务没跑完，重启会自动续上」
            _pend = resume_load()
            cfg["resume_pending"] = [{"name": n, "at": (_pend.get(n) or {}).get("at", ""),
                                      "tries": (_pend.get(n) or {}).get("tries", 0)}
                                     for n in RESUME_ORDER if n in _pend]
            return self._json(cfg)
        if p == "/api/task":
            # 任务名以中文为键；英文页用英文名轮询，这里做别名解析（否则英文页进度永远取不到）
            _tn = g("name")
            _tn = _TASK_ALIAS.get(_tn, _tn)
            return self._json(TASKS.get(_tn, {"running": False, "log": []}))
        if p == "/api/progress":
            # 每个数据源一条进度（数据更新面板里一源一行进度条）
            any_run = any(v.get("running") for v in SRC_PROG.values())
            return self._json({"items": SRC_PROG, "running": any_run})
        if p == "/api/poster":
            return self.api_poster(g("u", ""))
        if p == "/api/proxy-test":
            return self.api_proxy_test(g)
        if p == "/api/tmdb-test":
            return self.api_tmdb_test(g)
        if p == "/api/re0-test":
            return self.api_re0_test()
        if p == "/api/re0-direct/status":
            return self.api_re0_direct_status(g)
        if p == "/api/re0-direct/test":
            return self.api_re0_direct_test(g)
        if p == "/api/re0-direct/users":
            return self.api_re0_direct_users()
        if p == "/api/re0-direct/search":
            return self.api_re0_direct_search(g)
        if p == "/api/re0-direct/preview":
            return self.api_re0_direct_preview(g)
        if p == "/api/re0-direct/desktop/status":
            return self.api_re0_direct_desktop_status(g)
        if p == "/api/export":
            return self.api_export(g)
        if p == "/api/duplicates":
            return self.api_duplicates(g)
        if p == "/api/lists":
            return self.api_lists()
        if p == "/api/resources":
            return self.api_resources(g)
        if p == "/api/115/apps":
            return self._json({"ok": True,
                                "apps": [{"code": c, "name": n}
                                         for c, n in pan115.APP_CHOICES]})
        if p == "/api/115/qr":
            return self.api_115_qr()
        if p == "/api/115/qr/status":
            return self.api_115_qr_status(g)
        if p == "/api/115/status":
            return self.api_115_status(g)
        if p == "/api/115/dirs":
            return self.api_115_dirs(g)
        if p == "/api/115/tree":
            return self.api_115_tree(g)
        if p == "/api/fs/ls":
            return self.api_fs_ls(g)
        if p == "/api/115/compare":
            return self.api_115_compare(g)
        if p == "/api/local-dirs":
            return self.api_local_dirs(g)
        if p == "/api/stream":
            raw = g("path", "") or ""
            path = raw.replace("/CloudNAS/CloudDrive/", "").lstrip("/")
            if not path:
                return self._json({"error": "path required"}, 400)
            # 115 直链的 k 签名绑定「签发时用的 User-Agent」：必须用播放器自己的 UA 签，
            # 客户端跟随 302 取链时才会用同一个 UA，否则 CDN 一律 403 invalid signature。
            ua = self.headers.get("User-Agent") or ""
            try:
                url = pan115_downurl.downurl_url(path, ua=ua)
            except Exception as e:
                reverse_proxy.log_event("error", player="evolink", item=path[:120],
                                        path=path[:160], ua=ua[:120], status=0,
                                        note=f"换链失败：{e}")
                return self._json({"error": str(e), "hint": "115 risk control or login expired, retry later"}, 502)
            reverse_proxy.log_event("stream", player="evolink", item=path[:120],
                                    path=path[:160], ua=ua[:120], status=302,
                                    target=path[:200], cdn=url[:200])
            self.send_response(302)
            self.send_header("Location", url)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        r = downurl_panel.handle_get(p)

        if r is not None:

            return self._json(r)

        return self._json({"error": "not found"}, 404)

    def do_OPTIONS(self):
        """仅处理 re0.me 登录态提交的 CORS 预检。"""
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/re0-direct/capture":
            self.send_response(204)
            for k, v in _RE0_CAPTURE_CORS.items():
                self.send_header(k, v)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(405)
        self.end_headers()

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        # 限制请求体大小（4MB），防恶意超大 body 拖垮服务
        if n > 4 * 1024 * 1024:
            self.send_response(413)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"payload too large"}')
            return
        raw = self.rfile.read(n) or b"{}"
        ctype = (self.headers.get("Content-Type") or "").lower()
        if "application/x-www-form-urlencoded" in ctype:
            # 书签提交走表单：https 页面跨域请求 http 会被浏览器按混合内容拦掉，
            # 但表单跳转（顶层导航）是放行的，所以这里要能解析表单体。
            try:
                qs = urllib.parse.parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
                body = {k: (v[0] if v else "") for k, v in qs.items()}
            except Exception:
                body = {}
        else:
            try:
                body = json.loads(raw)
            except Exception:
                body = {}

        # EVOLINK:LICENSE:POST
        # 授权闸门：与 do_GET 同一判定，POST 侧同样放在所有分发之前。
        if u.path.startswith("/api/") and not _license_allowed(u.path, config.load()):
            return self._json({
                "error": "license_required",
                "message": "该功能需要专业版（一次性买断 68 元），请在「许可证」中激活",
                "need_plan": "pro",
            }, 402)

        if u.path == "/api/license/activate":
            if license_ops is None:
                return self._json({"ok": False, "error": "授权模块缺失，无法激活"}, 503)
            _lk = str(body.get("key") or "").strip().upper()
            if not _lk:
                return self._json({"ok": False, "error": "请输入激活码"}, 400)
            _lcfg = config.load()
            _lic = dict(_lcfg.get("license") or {})
            _lic["key"] = _lk
            _lcfg["license"] = _lic
            # 先强制在线校验（带设备指纹），通过后才落盘，绝不「填了就算激活」
            _lres = license_ops.verify(_lcfg, key=_lk, force=True)
            if not _lres.get("pro"):
                return self._json({"ok": False,
                                   "error": _lres.get("message") or "激活码无效",
                                   "detail": _lres}, 403)
            config.save(_lcfg)
            return self._json({"ok": True, "status": license_ops.status(_lcfg)})

        if u.path == "/api/license/clear":
            _lcfg = config.load()
            _lic = dict(_lcfg.get("license") or {})
            _lic["key"] = ""
            _lcfg["license"] = _lic
            config.save(_lcfg)
            if license_ops is not None:
                license_ops.clear_cache()
            return self._json({"ok": True,
                               "status": license_ops.status(_lcfg) if license_ops
                               else {"plan": "free", "pro": False}})

        # /EVOLINK:LICENSE:POST
        # 登录 / 登出 / 改账号密码：这几个接口本身要在未登录状态下可用
        if u.path == "/api/login":
            return self.api_login(body)
        if u.path == "/api/logout":
            return self.api_logout()
        # re0.me 登录态提交：书签小工具从 re0.me 跨域 POST，无 evolink 登录态，
        # 必须在鉴权闸门之前放行；仅接受来自 re0.me 上下文的提交。
        if u.path == "/api/re0-direct/capture":
            return self.api_re0_direct_capture(body)
        # Emby webhook 回调：Emby 不会带 evolink 登录态，必须放在鉴权闸门之前，
        # 改用 query 里的 token 做鉴权（token 在「链接同步 → Emby 删除联动」里生成）。
        if u.path == "/api/emby/webhook":
            return self.api_emby_webhook(body)
        if not self._require_login(u.path):
            return
        if u.path == "/api/re0-direct/config":
            return self.api_re0_direct_config(body)
        if u.path == "/api/strm/lib/save":
            return self.api_strm_lib_save(body)
        if u.path == "/api/strm/lib/delete":
            return self.api_strm_lib_delete(body)
        if u.path == "/api/strm/lib/enable":
            return self.api_strm_lib_enable(body)
        if u.path == "/api/strm/lib/run":
            return self.api_strm_lib_run(body)
        if u.path == "/api/archive/config":
            return self.api_archive_config(body)
        if u.path == "/api/archive/plan":
            return self.api_archive_plan(body)
        if u.path == "/api/archive/apply":
            return self.api_archive_apply(body)
        if u.path == "/api/weight/config":
            return self.api_weight_config(body)
        if u.path == "/api/weight/scan":
            return self.api_weight_scan(body)
        if u.path == "/api/weight/rules/save":
            return self.api_weight_rule_save(body)
        if u.path == "/api/weight/rules/new":
            return self.api_weight_rule_new(body)
        if u.path == "/api/weight/rules/reset":
            return self.api_weight_rule_reset(body)
        if u.path == "/api/weight/rules/import":
            return self.api_weight_rule_import(body)
        if u.path == "/api/weight/rules/delete":
            return self.api_weight_rule_delete(body)
        if u.path == "/api/classify/config":
            return self.api_classify_config(body)
        if u.path == "/api/classify/templates":
            return self.api_classify_templates(body)
        if u.path == "/api/classify/dict":
            return self.api_classify_dict(body)
        if u.path == "/api/strm/webhook/config":
            return self.api_strm_webhook_config(body)
        if u.path == "/api/strm/webhook/test":
            return self.api_strm_webhook_test(body)
        if u.path == "/api/re0-direct/users/add":
            return self.api_re0_direct_users_add(body)
        if u.path == "/api/re0-direct/users/del":
            return self.api_re0_direct_users_del(body)
        if u.path == "/api/re0-direct/login":
            return self.api_re0_direct_login(body)
        if u.path == "/api/re0-direct/unlock":
            return self.api_re0_direct_unlock(body)
        if u.path == "/api/re0-direct/checkin":
            return self.api_re0_direct_checkin(body)
        if u.path == "/api/re0-direct/browser":
            return self.api_re0_direct_browser(body)
        if u.path == "/api/re0-direct/clear":
            return self.api_re0_direct_clear(body)
        if u.path == "/api/re0-direct/desktop/start":
            return self.api_re0_direct_desktop_start(body)
        if u.path == "/api/re0-direct/desktop/save":
            return self.api_re0_direct_desktop_save(body)
        if u.path == "/api/re0-direct/desktop/stop":
            return self.api_re0_direct_desktop_stop(body)
        if u.path == "/api/re0-direct/desktop/solve":
            return self.api_re0_direct_desktop_solve(body)
        if u.path == "/api/re0-direct/test":
            return self.api_re0_direct_test(body)
        if u.path == "/api/link-check":
            return self.api_link_check(body)
        if u.path == "/api/link-delete":
            return self.api_link_delete(body)
        if u.path == "/api/link-trash-restore":
            return self.api_link_trash_restore(body)
        if u.path == "/api/link-trash-purge":
            return self.api_link_trash_purge(body)
        if u.path == "/api/settings/auth":
            return self.api_auth_settings(body)
        if u.path == "/api/settings":
            cur = config.load()
            for k in ("emby_url", "tmdb_api_key", "proxy", "tmdb_lang", "metatube_url",
                      "port", "re0_base", "javdb_base",
                      "fallback_channel_url", "fallback_channel_user",
                      "fallback_channel_pass", "resources_cloud", "re0_direct_user",
                      "re0_direct_pass", "re0_direct_proxy", "re0_direct_cookies"):
                # 带「••」的是脱敏回显，不能当作新值写回（否则真密钥会被覆盖成 b745••••b565）
                if k in body and body[k] and "••" not in str(body[k]):
                    cur[k] = body[k]
            # 反向代理（Jellyfin / Emby 302 换链）：开关/端口/base/api_key/user_id/db_path
            # 这些是明文字段（api_key 仅作兜底、不用于鉴权），无脱敏，照原值写回
            for k in ("jellyfin_enabled", "jellyfin_port", "jellyfin_base", "jellyfin_api_key",
                      "jellyfin_user_id", "jellyfin_db_path", "jellyfin_local_prefix",
                      "jellyfin_remote_prefix", "jellyfin_public_base", "emby_proxy_enabled",
                      "emby_proxy_port", "emby_proxy_base", "emby_proxy_api_key",
                      "emby_proxy_user_id", "emby_proxy_db_path", "emby_proxy_local_prefix",
                      "emby_proxy_remote_prefix", "emby_proxy_public_base",
                      "plex_enabled", "plex_port", "plex_base", "plex_public_base",
                      "plex_db_path", "plex_local_prefix", "plex_remote_prefix",
                      "plex_strm_prefix", "plex_link_prefix", "plex_meta_inject",
                      "plex_token", "plex_client_id",
                      "playable_enabled", "playable_port", "playable_base",
                      "playable_public_base", "playable_local_prefix",
                      "playable_remote_prefix", "playable_api_key",
                      "playable_user_id", "playable_db_path"):
                if k in body:
                    v = body[k]
                    if isinstance(v, str):
                        v = v.strip()
                    cur[k] = v
            for k in ("av_pages", "av_modes", "list_limit"):
                if k in body and body[k]:
                    cur[k] = body[k]
            # 代理：host/port 为准，同时回写旧字段 proxy 以便老代码路径也能用
            if "proxy_enabled" in body:
                cur["proxy_enabled"] = bool(body["proxy_enabled"])
            if body.get("proxy_host") is not None:
                cur["proxy_host"] = str(body.get("proxy_host") or "").strip()
            if body.get("proxy_port") is not None:
                try:
                    cur["proxy_port"] = int(body.get("proxy_port") or 0)
                except (TypeError, ValueError):
                    pass
            if cur.get("proxy_host"):
                cur["proxy"] = f"http://{cur['proxy_host']}:{cur.get('proxy_port') or 7890}"
            if "region_weights" in body and isinstance(body["region_weights"], dict):
                cur["region_weights"] = body["region_weights"]
            if "auto" in body and isinstance(body["auto"], dict):
                a = dict(AUTO_DEFAULTS)
                a.update(cur.get("auto") or {})
                a.update({k: v for k, v in body["auto"].items() if k in AUTO_DEFAULTS})
                cur["auto"] = a
            if "tg_channels" in body and isinstance(body["tg_channels"], list):
                cur["tg_channels"] = [str(x).strip().lstrip("@") for x in body["tg_channels"] if str(x).strip()]
            if "specs" in body:
                cur["specs"] = body["specs"]
            if "match_all_libs" in body:
                cur["match_all_libs"] = bool(body["match_all_libs"])
            if "auto_resume" in body:
                cur["auto_resume"] = bool(body["auto_resume"])
            if "strm_mode" in body and str(body["strm_mode"]) in ("auto", "path", "pc"):
                cur["strm_mode"] = str(body["strm_mode"])
            config.save(cur)
            return self._json({"ok": True})

        if u.path == "/api/proxy/reload":
            # 按当前 config 热重建反代（改端口/开关/地址后免容器重启）
            try:
                reverse_proxy.reload_all(config.load())
                plex_proxy.reload_all(config.load())
                return self._json({"ok": True, "ports": reverse_proxy.running_ports()})
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if u.path == "/api/proxy/log/clear":
            reverse_proxy.clear_events()
            return self._json({"ok": True})

        if u.path == "/api/strm-analyze":
            # 上传 .strm 自动分析：识别格式（CD2 路径型 / Pickcode·URL 型），可选试签一次。
            # 试签遵守换链铁律：风控冷却期内不签、20s 内不重复签、失败绝不自动重试。
            return self._json(api_strm_analyze(body))

        if u.path == "/api/sync-emby":
            return self._json({"ok": start_task("索引 Emby", {})})
        if u.path == "/api/sync-jellyfin":
            return self._json({"ok": start_task("索引 Jellyfin", {})})
        if u.path == "/api/sync-playable":
            return self._json({"ok": start_task("索引 Playable", {})})
        if u.path == "/api/sync-plex":
            return self._json({"ok": start_task("索引 Plex", {})})

        # ── Plex 账号授权（pin 流程）：跳 app.plex.tv 登录 → 收回 token ──
        if u.path == "/api/plex/auth-start":
            return self.api_plex_auth_start(body)
        if u.path == "/api/plex/auth-poll":
            return self.api_plex_auth_poll(body)
        if u.path == "/api/plex/libraries":
            return self.api_plex_libraries(body)

        if u.path == "/api/fetch":
            specs = body.get("specs")
            inc = bool(body.get("incremental"))
            return self._json({"ok": start_task("拉取发行片",
                                                {"specs": specs, "incremental": inc})})

        if u.path == "/api/fetch-list":
            return self._json({"ok": start_task("拉取榜单", {
                "keys": body.get("keys"), "limit": int(body.get("limit") or 40)})})

        if u.path == "/api/region-plan":
            return self._json(self.api_region_plan(body))

        if u.path == "/api/fetch-av":
            return self._json({"ok": start_task("拉取 AV 番号", {
                "pages": body.get("pages"), "modes": body.get("modes"),
                "incremental": bool(body.get("incremental"))})})

        if u.path == "/api/fetch-avbus":
            return self._json({"ok": start_task("拉取 AV 全史", {
                "year_floor": body.get("year_floor"), "min_items": body.get("min_items"),
                "resume": bool(body.get("resume", True)), "prefixes": body.get("prefixes")})})

        if u.path == "/api/fetch-hentai":
            return self._json({"ok": start_task("拉取里番", {
                "pages": body.get("pages"), "incremental": bool(body.get("incremental"))})})

        if u.path == "/api/warm-posters":
            # 封面预热：把 catalog 里的海报预先落盘，之后断代理也能显示
            return self._json({"ok": start_task("预热封面", {
                "scope": (body.get("scope") or "av"), "category": body.get("category") or "",
                "limit": int(body.get("limit") or 0), "workers": int(body.get("workers") or 12),
                "offset": int(body.get("offset") or 0)})})

        if u.path == "/api/fetch-full":
            return self._json({"ok": start_task("全量拉取", {})})

        if u.path == "/api/fetch-counts":
            # 只统计发行量（不动条目）：每档一次请求读 total_results，几分钟跑完全部年份
            return self._json({"ok": start_task("统计发行量", {
                "resume": bool(body.get("resume", True))})})

        if u.path == "/api/fetch-reset":
            # 清空断点游标：下次「全量拉取」从第 1 页重新扫一遍（已有条目只更新不重复）
            for k in ("fetch_cursor_tmdb", "fetch_cursor_counts", "fetch_cursor_av",
                      "fetch_cursor_hentai", "fetch_cursor_avbus"):
                db.set_meta(k, "{}")
            return self._json({"ok": True})

        if u.path in ("/api/emby/select", "/api/emby/map"):
            return self._json(self.api_emby_select_save(body))

        if u.path == "/api/emby/servers":
            return self._json(self.api_emby_servers_save(body))

        if u.path == "/api/auto":
            return self._json(self.api_save_auto(body))

        if u.path == "/api/auto-run":
            return self._json({"ok": run_task("立即更新", auto_sync_once)})

        if u.path == "/api/match":
            libs = [str(x).strip() for x in (body.get("libs") or []) if str(x).strip()]
            return self._json({"ok": start_task("比对", {"libs": libs})})

        if u.path == "/api/dup-scan":
            libs = [str(x).strip() for x in (body.get("libs") or []) if str(x).strip()]
            if not libs:
                return self._json({"ok": False, "error": "未选择任何库"})
            return self._json({"ok": start_task("扫描重复", {"libs": libs})})

        if u.path == "/api/resolve":  # 人工把某条标为"已有"
            c = db.init()
            _st = body.get("status", "owned")
            c.execute("UPDATE catalog SET match_status=?, match_emby_id=?, "
                      "match_score=CASE WHEN ?='owned' THEN 100 ELSE 0 END, matched_at=? WHERE cid=?",
                      (_st, body.get("emby_id"), _st, db.now(), body.get("cid")))
            c.commit()
            facets_flush()
            return self._json({"ok": True})

        if u.path == "/api/115/login":
            return self.api_115_login(body)

        if u.path == "/api/115/logout":
            cfg = config.load()
            cfg["pan115_cookie"] = {"UID": "", "CID": "", "SEID": "", "KID": ""}
            cfg["pan115_user"] = ""
            config.save(cfg)
            return self._json({"ok": True})

        if u.path == "/api/115/scan":
            # roots：目录树里勾选的多个目录（不传就只扫 root）
            roots = body.get("roots")
            if isinstance(roots, list) and roots:
                roots = [str(x) for x in roots if str(x).strip()]
            else:
                roots = None
            return self._json({"ok": run_task("scan115", lambda log: task_scan_115(
                log, str(body.get("root") or "0"), int(body.get("depth") or 12), roots=roots))})

        if u.path == "/api/strm-gen/plan":
            return self.api_strm_gen_plan(body)
        if u.path == "/api/strm-gen/run":
            return self.api_strm_gen_run(body)

        r = downurl_panel.handle_post(u.path)

        if r is not None:

            return self._json(r)

        return self._json({"error": "not found"}, 404)

    # ---- 具体接口 ----
    def api_compare(self, g):
        """当前筛选下的三件事：发行片多少、本地库多少、还缺多少。

        列表筛什么，这三张卡就跟着算什么 —— 分类、子类（桶）、年代、类型、地区、
        片型、榜单、AV 系列、搜索词全部生效。本地库在 owned 上按同一套口径统计
        （桶对应的 Emby 库 + 同一段年代），不再拿「整个库的总量」充数。
        """
        c = db.conn()
        where, args = [], []
        cat = (g("category") or "").strip()
        cw, ca = db.cat_scope(cat)
        where.append(cw)
        args += ca
        bk = (g("bucket") or "").strip()
        if bk:
            bw, ba = db.bucket_where(bk)
            where += bw
            args += ba
        kw = (g("q") or "").strip()
        if kw:
            where.append("(c.title LIKE ? OR c.original_title LIKE ? OR c.av_code LIKE ?)")
            args += [f"%{kw}%"] * 3
        genre = (g("genre") or "").strip()
        if genre:
            where.append("(','||COALESCE(c.genres,'')||',') LIKE ?")
            args.append(f"%,{genre},%")
        region = (g("region") or "").strip()
        if region:
            where.append("(','||COALESCE(c.regions,'')||',') LIKE ?")
            args.append(f"%,{region},%")
        media = (g("media") or "").strip()
        if media:
            where.append("c.media=?")
            args.append(media)
        lst = (g("list") or "").strip()
        if lst:
            where.append("(','||COALESCE(c.lists,'')||',') LIKE ?")
            args.append(f"%,{lst},%")
        srv = (g("series") or "").strip().upper()
        if srv:
            where.append("UPPER(COALESCE(c.av_code,'')) LIKE ?")
            args.append(srv + "%")
        dec = (g("decade") or "").strip()
        dw, da = db.catalog_dec_where(dec)
        if dw:
            where.append(dw)
            args += da
        w = (" WHERE " + " AND ".join(where)) if where else ""
        r = c.execute(f"SELECT COUNT(*) n, SUM(CASE WHEN c.match_status='missing' THEN 1 ELSE 0 END) m "
                      f"FROM catalog c{w}", args).fetchone()

        # 本地库：桶给了就用桶的库，否则按分类映射；一个都没映射就是 0，不能退化成全库
        if bk:
            libs = list(db.sel_map().get(bk) or [])
        elif cat and cat != "all":
            libs = list(db.libs_for_cat(cat))
        else:
            libs = db._all_regular_libs()
        if not bk and region:
            # 电影/剧集这类库是按地区拆的，地区筛选要把本地库一起收窄
            by_region = db.libs_for_region(cat, region)
            if by_region is not None:
                libs = by_region
        lw, la = db._lib_sql(libs)
        cond, largs = [lw], list(la)
        ldw, lda = db.owned_dec_where(dec, db.eff_year_sql())
        if ldw:
            cond.append(ldw)
            largs += lda
        if kw:
            cond.append("(name LIKE ? OR original_title LIKE ? OR path LIKE ?)")
            largs += [f"%{kw}%"] * 3
        if srv:
            cond.append("UPPER(COALESCE(av_code,'')) LIKE ?")
            largs.append(srv + "%")
        if media in ("movie", "tv"):
            # owned 只有 Movie / Series 两型，动画电影与动画剧集靠它区分
            cond.append("type=?")
            largs.append("Movie" if media == "movie" else "Series")
        local = c.execute("SELECT COUNT(*) n FROM owned o" + (db._EFF_JOIN if ldw else "") +
                          " WHERE " + " AND ".join(cond), largs).fetchone()["n"]

        # ---- 两套口径分开给 ----
        # 热门榜：catalog 里的条目（现在每年每子类只留热度前 500）
        # 全量发行量：cat_count 里存的总数（一个请求拿到的 total_results），不存条目
        picked = r["n"] or 0
        hot_missing = r["m"] or 0
        hot = {"picked": picked, "owned": max(0, picked - hot_missing),
               "gap": hot_missing, "local": local}

        counts = db.counts_by_year(bucket=bk, category=cat)
        # 类型 / 地区 / 片型 / 榜单 / 搜索这些条件没法作用在「一个总数」上，
        # 一旦加了就只能给热门榜，不能假装全量发行量也跟着筛了
        refine = bool(genre or region or media or lst or srv or kw)
        releases = 0
        if counts and not refine:
            releases = sum(n for y, n in counts.items() if _dec_match(y, dec))

        # 覆盖度：分类下每个子类都统计过才算「完整」。只统计了一半时，
        # 数字仍给，但标成「≥」，免得用户以为这一年就这么多部。
        want = [bk] if bk else [k for k, _rs in CAT_FETCH_GROUPS.get(cat, [])]
        covered = [k for k in want
                   if any(_dec_match(y, dec) for y in db.counts_by_year(bucket=k))]
        partial = bool(want) and len(covered) < len(want)
        ys = sorted(y for y in counts if _dec_match(y, dec)) if counts else []
        full = {"releases": releases, "local": local,
                "gap": max(0, releases - local),
                "ready": bool(counts) and not refine and not partial,
                "partial": partial, "covered": len(covered), "want": len(want),
                "yfrom": ys[0] if ys else None, "yto": ys[-1] if ys else None,
                "nyears": len(ys),
                "years": (db.count_span(bucket=bk, category=cat) if counts else {}),
                "refined": refine}

        return {"catalog": picked, "missing": hot_missing, "local": local,
                "libs": libs, "decade": dec, "full": full, "hot": hot}

    def api_catalog(self, g):
        c = db.conn()
        st = g("status", "")
        # 核心基线：分类 / 子类 / 搜索。所有 facet（含状态计数）都从它出发，
        # 这样状态行、年代行、类型行才会跟着分类一起变，而不是永远显示全站总数。
        core_where, core_args = [], []
        cat = g("category", "")
        cw, ca = db.cat_scope(cat)
        core_where.append(cw)
        core_args += ca
        kw = (g("q") or "").strip()
        if kw:
            core_where.append("(c.title LIKE ? OR c.original_title LIKE ? OR c.av_code LIKE ?)")
            core_args += [f"%{kw}%"] * 3

        # 子类（动画电影 / 国语剧 / 欧美电影…）：单独作为一维，方便算「每个子类」的 facet
        bk = (g("bucket") or "").strip()
        b_where, b_args = db.bucket_where(bk) if bk else ([], [])

        # 基础基线：核心 + 子类 + 状态（列表与年代 facet 用）
        base_where = list(core_where) + list(b_where)
        base_args = list(core_args) + list(b_args)
        if st and st != "all":
            base_where.append("c.match_status=?")
            base_args.append(st)

        # 类型 / 地区（二级筛选，作用于分类之内）
        genre = (g("genre") or "").strip()
        region = (g("region") or "").strip()
        g_where, g_args = [], []
        if genre:
            g_where.append("(','||COALESCE(c.genres,'')||',') LIKE ?")
            g_args.append(f"%,{genre},%")
        r_where, r_args = [], []
        if region:
            r_where.append("(','||COALESCE(c.regions,'')||',') LIKE ?")
            r_args.append(f"%,{region},%")
        # 片型（movie / tv）——动画、纪录片这类 both 分类靠它区分电影版与剧集版
        media = (g("media") or "").strip()
        m_where, m_args = [], []
        if media:
            m_where.append("c.media=?")
            m_args.append(media)

        # 榜单：一个条目可属多个榜
        lst = (g("list") or "").strip()
        l_where, l_args = [], []
        if lst:
            l_where.append("(','||COALESCE(c.lists,'')||',') LIKE ?")
            l_args.append(f"%,{lst},%")

        # AV 系列：番号前缀（IPZZ-001 → IPZZ）。只在 AV 分类下有意义
        srv = (g("series") or "").strip().upper()
        s_where, s_args = [], []
        if srv:
            s_where.append("UPPER(COALESCE(c.av_code,'')) LIKE ?")
            s_args.append(srv + "%")

        # 年代：y2026..y2010 逐年，2000/1990/1980 按年代段，old(<1980)，none
        dec = (g("decade") or "").strip()
        dec_where, dec_args = [], []
        if dec == "old":
            dec_where.append("(c.year IS NOT NULL AND c.year > 0 AND c.year < 1980)")
        elif dec == "none":
            dec_where.append("(c.year IS NULL OR c.year = 0)")
        elif dec.startswith("y") and dec[1:].isdigit():
            y = int(dec[1:])
            if y >= 2026:      # 2026 档兜住所有更晚的（预告片/未上映标记）
                dec_where.append("(c.year >= ?)")
                dec_args.append(2026)
            else:
                dec_where.append("c.year = ?")
                dec_args.append(y)
        elif dec.isdigit():
            y = int(dec)
            dec_where.append("(c.year >= ? AND c.year <= ?)")
            dec_args += [y, y + 9]

        def _w(*groups):
            parts, params = [], []
            for ws, ps in groups:
                parts += ws
                params += ps
            return ("WHERE " + " AND ".join(parts)) if parts else "", params

        # 每个 facet 的基线都不含自身维度，切换时才不会互相锁死
        w, a = _w((base_where, base_args), (g_where, g_args), (r_where, r_args),
                  (m_where, m_args), (l_where, l_args), (s_where, s_args), (dec_where, dec_args))
        w_dec, a_dec = _w((base_where, base_args), (g_where, g_args), (r_where, r_args),
                          (m_where, m_args), (l_where, l_args), (s_where, s_args))
        # 类型计数随地区/片型变、地区随类型/片型变、片型随类型/地区变，但都不随自己变
        w_g, a_g = _w((base_where, base_args), (r_where, r_args), (m_where, m_args),
                      (l_where, l_args), (s_where, s_args), (dec_where, dec_args))
        w_r, a_r = _w((base_where, base_args), (g_where, g_args), (m_where, m_args),
                      (l_where, l_args), (s_where, s_args), (dec_where, dec_args))
        w_m, a_m = _w((base_where, base_args), (g_where, g_args), (r_where, r_args),
                      (l_where, l_args), (s_where, s_args), (dec_where, dec_args))
        w_l, a_l = _w((base_where, base_args), (g_where, g_args), (r_where, r_args),
                      (m_where, m_args), (s_where, s_args), (dec_where, dec_args))
        w_s, a_s = _w((base_where, base_args), (g_where, g_args), (r_where, r_args),
                      (m_where, m_args), (l_where, l_args), (dec_where, dec_args))
        # 状态分布：不含状态条件本身，但含年代/类型/地区/片型等其它筛选，
        # 顶部「全部 / 缺 / 已在库…」才会跟着降到当前筛选的条数
        w_st, a_st = _w((core_where, core_args), (b_where, b_args), (g_where, g_args), (r_where, r_args),
                        (m_where, m_args), (l_where, l_args), (s_where, s_args), (dec_where, dec_args))

        size = min(int(g("size", 60)), 200)
        page = max(int(g("page", 1)), 1)
        # facet 统计缓存：key 带筛选组合与 catalog 版本号，TTL 300s 兜底
        ckey = (_CAT_VER, cat, kw, bk, st, genre, region, media, lst, srv, dec)
        with _FACET_LOCK:
            _hit = _FACET_CACHE.get(ckey)
        if _hit and _hit[0] > time.time():
            total, facets = _hit[1], _hit[2]
        else:
            total = c.execute(f"SELECT COUNT(*) n FROM catalog c {w}", a).fetchone()["n"]

            # 年代分布（不含年代条件本身，这样切换年代时数字不会变空）
            # 2010 之后的每个年份单独一档（y2026…y2010），更早的按年代段归并
            facets = {}
            for r in c.execute(f"""SELECT CASE
                                     WHEN c.year IS NULL OR c.year = 0 THEN 'none'
                                     WHEN c.year >= 2026 THEN 'y2026'
                                     WHEN c.year >= 2010 THEN 'y'||c.year
                                     WHEN c.year >= 2000 THEN '2000'
                                     WHEN c.year >= 1990 THEN '1990'
                                     WHEN c.year >= 1980 THEN '1980'
                                     ELSE 'old' END AS d, COUNT(*) n
                                   FROM catalog c {w_dec} GROUP BY d""", a_dec):
                facets[str(r["d"])] = r["n"]

            # 类型 / 地区 / 榜单 分布
            gfacet, rfacet, lfacet = {}, {}, {}
            for r in c.execute(f"SELECT c.genres g FROM catalog c {w_g}", a_g):
                for x in (r["g"] or "").split(","):
                    if x:
                        gfacet[x] = gfacet.get(x, 0) + 1
            for r in c.execute(f"SELECT c.regions rg FROM catalog c {w_r}", a_r):
                for x in (r["rg"] or "").split(","):
                    if x:
                        rfacet[x] = rfacet.get(x, 0) + 1
            for r in c.execute(f"SELECT c.lists ls FROM catalog c {w_l}", a_l):
                for x in (r["ls"] or "").split(","):
                    if x:
                        lfacet[x] = lfacet.get(x, 0) + 1
            g_total = c.execute(f"SELECT COUNT(*) n FROM catalog c {w_g}", a_g).fetchone()["n"]
            r_total = c.execute(f"SELECT COUNT(*) n FROM catalog c {w_r}", a_r).fetchone()["n"]
            mfacet, m_total = {}, 0
            for r in c.execute(f"SELECT c.media md, COUNT(*) n FROM catalog c {w_m} GROUP BY c.media", a_m):
                if r["md"]:
                    mfacet[r["md"]] = r["n"]
                    m_total += r["n"]

            # AV 系列分布：番号去掉尾部数字即系列（IPZZ001 → IPZZ、259LUXU1678 → 259LUXU）
            stfacet = {}
            for r in c.execute(f"SELECT COALESCE(c.match_status,'missing') ms, COUNT(*) n "
                               f"FROM catalog c {w_st} GROUP BY 1", a_st):
                stfacet[r["ms"]] = r["n"]

            # 子类分布：不含子类条件本身，但含年代/类型/地区等其它筛选，
            # 这样选了 2026 之后「欧美电影」chip 上的数字也是 2026 的
            bfacet = {}
            if cat and cat != "all":
                sw = ["c.match_status=?"] if (st and st != "all") else []
                sa = [st] if sw else []
                for k in [k for k, cc, _l in db.BUCKETS if cc == cat]:
                    bw2, ba2 = db.bucket_where(k)
                    if not bw2:
                        continue
                    w_bk, a_bk = _w((core_where, core_args), (sw, sa), (g_where, g_args),
                                    (r_where, r_args), (m_where, m_args), (l_where, l_args),
                                    (s_where, s_args), (dec_where, dec_args), (bw2, ba2))
                    bfacet[k] = c.execute(f"SELECT COUNT(*) n FROM catalog c {w_bk}",
                                          a_bk).fetchone()["n"]

            sfacet = {}
            if cat == "AV":
                for r in c.execute(f"SELECT UPPER(COALESCE(c.av_code,'')) ac FROM catalog c {w_s}", a_s):
                    code = r["ac"] or ""
                    if not code:
                        continue
                    s = re.sub(r"\d+$", "", code)
                    if len(s) >= 2:
                        sfacet[s] = sfacet.get(s, 0) + 1

            facets = {"decade": facets, "base_total": sum(facets.values()),
                      "status": stfacet, "status_total": sum(stfacet.values()),
                      "buckets": bfacet,
                      "genres": gfacet, "genre_total": g_total,
                      "regions": rfacet, "region_total": r_total,
                      "lists": lfacet,
                      "series": sfacet,
                      "media": mfacet, "media_total": m_total}
            with _FACET_LOCK:
                _FACET_CACHE[ckey] = (time.time() + 300, total, facets)
                # 防缓存无限增长：不同筛选组合会持续累积键，超阈值整体清空重建
                if len(_FACET_CACHE) > 512:
                    _FACET_CACHE.clear()

        rows = c.execute(
            f"""SELECT c.*, o.name AS owned_name, o.library AS owned_library, o.emby_id AS owned_id
                FROM catalog c LEFT JOIN owned o ON o.emby_id=c.match_emby_id
                {w} ORDER BY
                  /* 各来源 popularity 量纲差太多（TMDB 几十~几百，AniList 上万），
                     取对数压平；AV/里番再降权，避免「全部」被成人内容霸屏。
                     ⚠️ 这里必须与 db.py 的 idx_cat_sortkey 表达式索引逐字一致，
                     SQLite 才会走索引；写 c.sort_key 会因 VIRTUAL 生成列无索引而全表排序。 */
                  (CASE WHEN COALESCE(c.popularity,0)>0 THEN log(c.popularity+1) ELSE 0 END) DESC,
                  c.year DESC, c.rating DESC LIMIT ? OFFSET ?""",
            a + [size, (page - 1) * size]).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["extra"] = json.loads(d.get("extra") or "{}")
            except Exception:
                d["extra"] = {}
            d["genre_list"] = [x for x in (d.get("genres") or "").split(",") if x]
            d["region_list"] = [x for x in (d.get("regions") or "").split(",") if x]
            d["list_list"] = [x for x in (d.get("lists") or "").split(",") if x]
            out.append(d)
        return self._json({"total": total, "page": page, "size": size, "items": out,
                           "facets": facets})

    def api_poster(self, url):
        """海报代理 + 磁盘缓存。

        TMDB/豆瓣图在国内直连不通，统一走服务端代理（走 mihomo）。
        首次拉取后落盘，之后同图直接读文件——外网访问不用每次都等 10 秒。
        SSRF 防护：目标地址禁止为内网/回环（见 _poster_host_blocked）。
        """
        if not re.match(r"^https?://", url or ""):
            return self._json({"error": "bad url"}, 400)
        # SSRF：拒绝打内网/回环地址
        if _poster_host_blocked(url):
            return self._json({"error": "forbidden target"}, 403)
        url = fix_poster_url(url)
        f, m = poster_cache_path(url)
        if f is not None and f.exists():
            try:
                return self._send_bytes(f.read_bytes(),
                                        m.read_text("utf-8").strip() if m.exists() else "image/jpeg")
            except Exception:
                pass
        try:
            data, ctype = fetch_poster_bytes(url, max_bytes=10 * 1024 * 1024)
            # 只当图片用，非图片内容（HTML 报错页/重定向体）一律拒
            if not (ctype or "").lower().startswith("image/"):
                return self._json({"error": "not an image"}, 502)
            store_poster(url, data, ctype)
            self._send_bytes(data, ctype)
        except Exception:
            # 不把内部异常原文外抛（可能含内网 IP/路径）
            self._json({"error": "fetch failed"}, 502)

    # ── Link 清理：标注 / 多选删除 / 回收站 ────────────────────
    # ---------------- strm 媒体库（strm 建立由 EvoLink 独立完成） ----------------

    def api_strm_libs(self):
        """列出所有 strm 媒体库（含各自的汇总统计与最近一次运行结果）。

        scan_modes 返回两种扫描模式的说明，前端选择器直接用：
        local（本地挂载/CD2，推荐，零 115 请求）与 api（115 接口扫目录树）。
        """
        libs = strm_lib.list_libs()
        return self._json({
            "libs": [strm_lib.summary(l) for l in libs],
            "runs": strm_lib.recent_runs(limit=30),
            "scan_modes": strm_lib.SCAN_MODE_INFO,
        })

    def api_strm_lib_save(self, body):
        """新增 / 更新一个 strm 媒体库。body 即库字段（见 core/strm_lib.py）。"""
        try:
            rec = strm_lib.save_lib(body or {})
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:
            return self._json({"error": f"保存失败：{e}"}, 500)
        return self._json({"lib": strm_lib.summary(rec)})

    def api_strm_lib_delete(self, body):
        """删除一个 strm 媒体库（只删库记录，不删已生成的 strm 文件）。"""
        lid = str((body or {}).get("id") or "")
        if not lid:
            return self._json({"error": "缺少 id"}, 400)
        if not strm_lib.delete_lib(lid):
            return self._json({"error": "库不存在"}, 404)
        return self._json({"ok": True})

    def api_strm_lib_enable(self, body):
        """启用 / 停用一个库（停用后不参与定时同步与手动全部同步）。"""
        b = body or {}
        ok = strm_lib.set_lib_enabled(str(b.get("id") or ""), bool(b.get("enabled")))
        if not ok:
            return self._json({"error": "库不存在"}, 404)
        return self._json({"ok": True})

    def api_strm_lib_run(self, body):
        """立即同步一个库（后台线程执行，进度看 /api/strm/runs）。

        body: {"id": 库id, "dry_run": bool(可选)}
        dry_run=True 只扫描并统计将要写出的文件，不落盘。
        """
        b = body or {}
        lib_id = str(b.get("id") or "")
        lib = strm_lib.get_lib(lib_id)
        if not lib:
            return self._json({"error": "库不存在"}, 404)
        dry = bool(b.get("dry_run"))

        def _job():
            try:
                r = strm_sync.sync_lib(lib_id, dry_run=dry)
                print(f"[strm] 手动同步 {lib.get('name')}: {r.get('status')} "
                      f"scanned={r.get('scanned')} written={r.get('written')} "
                      f"skipped={r.get('skipped')} errors={len(r.get('errors') or [])}",
                      flush=True)
            except Exception as e:
                print(f"[strm] 手动同步失败 {lib.get('name')}: {e}", flush=True)

        threading.Thread(target=_job, daemon=True).start()
        return self._json({"started": True, "lib_id": lib_id, "dry_run": dry,
                           "name": lib.get("name")})

    def api_strm_runs(self, g):
        """运行记录：?lib_id=（可省，省略返回全部库的）&limit=（默认 20）。"""
        lib_id = g("lib_id", "")
        try:
            limit = int(g("limit", "20"))
        except (TypeError, ValueError):
            limit = 20
        return self._json({"runs": strm_lib.recent_runs(
            lib_id or None, limit=max(1, min(200, limit)))})

    # ---------------- Emby webhook 删除联动 ----------------

    WH_CONFIG_KEY = "emby_webhook"
    ARCHIVE_CONFIG_KEY = "strm_archive"
    WEIGHT_CONFIG_KEY = "strm_weight"
    CLASSIFY_CONFIG_KEY = "strm_classify"

    DEFAULT_ARCHIVE_CFG = {
        "src_dir": "",
        "out_dir": "",
        "store_prefix": "115open",
        "cd2_prefix": "/CloudNAS/CloudDrive",
        "base_url": "",
        "fmt": "evolink",
        "name_template": "{{title}} ({{year}}) {{resolution}}.{{source}}.{{dynamicRange}}.{{videoCodec}}.{{audioCodec}}.{{fileExt}}",
        "folder_template": "{{title}} ({{year}})",
        "category_first": True,
        "tmdb_name": True,
        "match_threshold": 0.72,
        "skip_existing": True,
        "limit": 2000,
        # 洗板：同一作品有多个版本时只留最优的几个。
        # 电影与剧集可以各选一套权重方案（留空 = 不洗板）。
        "weight_rule_movie": "",
        "weight_rule_tv": "",
        "keep_count": 1,
    }

    DEFAULT_CLASSIFY_CFG = {
        # 分类规则不预置：不同人的分类习惯不同（地区向 / 类型向 / 混合），
        # 强行给一套默认值反而是替用户做决定。
        # 面板里提供「分类模板」供用户一键套用，套用后仍可自由增删改。
        "categories": {
            "movie": {},
            "tv": {},
        },
        "media_type": {
            "enabled": True,
            "movie": "电影",
            "tv": "剧集",
        },
        "rules": {
            "enable_word_search": True,
            "enable_similarity_match": True,
            "similarity_threshold": 0.8,
            "keyword_links": [],
            "render_word_links": [],
            "release_group_links": [],
            "capturing_group_links": [],
            "block_words": [],
        },
    }

    MEDIA_EXTS = (".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".iso",
                  ".wmv", ".mov", ".flv", ".webm", ".m4v")

    def _wh_cfg(self):
        """读 webhook 配置（token / enabled）。"""
        try:
            cfg = config.load() or {}
        except Exception:
            cfg = {}
        wh = cfg.get(self.WH_CONFIG_KEY) or {}
        if not isinstance(wh, dict):
            wh = {}
        return wh

    def _wh_save(self, wh):
        """把 webhook 配置写回 config 文件。"""
        cfg = config.load() or {}
        cfg[self.WH_CONFIG_KEY] = wh
        config.save(cfg)

    def _wh_url(self, token):
        """拼回调 URL。

        容器里 gethostbyname(gethostname()) 往往返回 127.0.0.1，Emby 拿这个地址
        是访问不到的。所以优先用「用户当前访问 EvoLink 的地址」（请求的 Host 头），
        其次用配置的 base_url，最后才退回本机 IP。
        """
        host = ""
        try:
            host = str(self.headers.get("Host") or "").strip()
        except Exception:
            host = ""
        if not host or host.startswith("127.") or host.startswith("localhost"):
            host = ""
        if not host:
            try:
                cfg = config.load() or {}
                base = str(cfg.get("base_url") or "").strip().rstrip("/")
                if base:
                    m = re.match(r"^https?://([^/]+)", base)
                    if m:
                        host = m.group(1)
            except Exception:
                host = ""
        if not host:
            try:
                ip = socket.gethostbyname(socket.gethostname())
                if ip and not ip.startswith("127."):
                    host = ip
            except Exception:
                host = ""
        if not host:
            host = "127.0.0.1:9191"
        if not re.match(r"^\d+\.\d+\.\d+\.\d+(:\d+)?$", host) and ":" not in host:
            host = host + ":9191"
        return "http://%s/api/emby/webhook?token=%s" % (host, urllib.parse.quote(token, safe=""))

    def api_strm_webhook_config(self, body):
        """GET：拿当前 webhook 配置；POST：改 token / 开关。"""
        b = body or {}
        wh = self._wh_cfg()
        token = str(wh.get("token") or "").strip()
        enabled = bool(wh.get("enabled", True))

        # 首次访问自动生成 token 并落盘，省得用户自己编
        if not token:
            token = secrets.token_urlsafe(24)
            wh["token"] = token
            wh["enabled"] = enabled
            self._wh_save(wh)

        if b.get("regen"):
            token = secrets.token_urlsafe(24)
            wh["token"] = token
            self._wh_save(wh)
        elif b.get("token") is not None:
            nt = str(b.get("token") or "").strip()
            if nt and nt != token:
                token = nt
                wh["token"] = token
                self._wh_save(wh)
        if "enabled" in b:
            enabled = bool(b.get("enabled"))
            wh["enabled"] = enabled

        # 受管目录：Emby 删条目时，只有落在这些目录下的 strm 才会被清理。
        # 用户没建媒体库时，靠这个也能用删除联动。
        managed_dirs = []
        if "managed_dirs" in b:
            raw = b.get("managed_dirs")
            if isinstance(raw, str):
                raw = raw.replace("\r", "\n").replace(",", "\n").split("\n")
            if isinstance(raw, (list, tuple)):
                for x in raw:
                    v = str(x or "").strip()
                    if v and v not in managed_dirs:
                        managed_dirs.append(v)
            wh["managed_dirs"] = managed_dirs
        else:
            managed_dirs = wh.get("managed_dirs") or []
            if isinstance(managed_dirs, str):
                managed_dirs = [x.strip() for x in managed_dirs.split(",") if x.strip()]

        self._wh_save(wh)

        # 顺带返回已配置的媒体库，方便前端提示用户
        try:
            libs = strm_lib.list_libs()
            lib_dirs = [{"id": x.get("id"), "name": x.get("name"), "outdir": x.get("outdir")}
                        for x in libs]
        except Exception:
            lib_dirs = []

        return self._json({"ok": True, "token": token, "enabled": enabled,
                            "url": self._wh_url(token),
                            "managed_dirs": managed_dirs,
                            "libs": lib_dirs})

    def api_emby_webhook(self, body):
        """接收 Emby 的 webhook 事件，按删除类型清理 strm 与空目录。

        鉴权用 query 里的 token（Emby 不会带 evolink 登录态）。
        """
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        token = (q.get("token") or [""])[0].strip()
        wh = self._wh_cfg()
        expect = str(wh.get("token") or "").strip()

        if not expect:
            return self._json({"ok": False, "error": "webhook 未启用：请先在面板里生成 token"}, 503)
        if not wh.get("enabled", True):
            return self._json({"ok": False, "error": "webhook 已停用"}, 403)
        if not token or not hmac.compare_digest(token, expect):
            return self._json({"ok": False, "error": "token 不正确"}, 403)

        try:
            libs = strm_lib.list_libs()
        except Exception as e:
            return self._json({"ok": False, "error": "读取媒体库列表失败：%s" % e}, 500)

        dry = bool((body or {}).get("dry_run"))
        managed_dirs = wh.get("managed_dirs") or []
        if isinstance(managed_dirs, str):
            managed_dirs = [x.strip() for x in managed_dirs.replace("\n", ",").split(",") if x.strip()]
        try:
            res = strm_webhook.handle_event(body or {}, libs, dry_run=dry,
                                            managed_dirs=managed_dirs)
        except Exception as e:
            return self._json({"ok": False, "error": "处理失败：%s" % e}, 500)

        # 落一条日志，方便面板里回看处理历史
        try:
            self._wh_log(res, token_ok=True)
        except Exception:
            pass
        try:
            json.dumps(res, ensure_ascii=False)
        except Exception:
            res = {"ok": False, "error": "结果序列化失败"}
        return self._json(res, 200)

    def _wh_log(self, res, token_ok=True):
        """写一条 webhook 处理日志。"""
        try:
            conn = db.conn()
        except Exception:
            return
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS strm_webhook_log (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts        TEXT NOT NULL,
                    event     TEXT DEFAULT '',
                    type      TEXT DEFAULT '',
                    name      TEXT DEFAULT '',
                    action    TEXT DEFAULT '',
                    targets   TEXT DEFAULT '',
                    reason    TEXT DEFAULT '',
                    deleted   TEXT DEFAULT '',
                    errors    TEXT DEFAULT ''
                )"""
            )
            conn.execute(
                "INSERT INTO strm_webhook_log "
                "(ts, event, type, name, action, targets, reason, deleted, errors) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    str(res.get("event") or ""),
                    str(res.get("type") or ""),
                    str(res.get("name") or ""),
                    str(res.get("action") or ""),
                    json.dumps(res.get("targets") or [], ensure_ascii=False),
                    str(res.get("reason") or ""),
                    json.dumps(res.get("deleted") or [], ensure_ascii=False),
                    json.dumps(res.get("errors") or [], ensure_ascii=False),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def api_strm_webhook_test(self, body):
        """自测删除联动：造一个模拟的 Emby 删除事件，验证处理链路。

        body.dry_run 为 True 时只返回删除计划，不真删。
        可选 body.item_path 指定一个真实 strm 路径来试跑。
        """
        b = body or {}
        mode = str(b.get("mode") or "delete").strip().lower()
        dry = bool(b.get("dry_run", True))
        wh = self._wh_cfg()
        token = str(wh.get("token") or "").strip()

        if mode == "ping":
            return self._wh_ping(token, wh)

        try:
            libs = strm_lib.list_libs()
        except Exception as e:
            return self._json({"ok": False, "error": "读取媒体库列表失败：%s" % e}, 500)

        managed_dirs = wh.get("managed_dirs") or []
        if isinstance(managed_dirs, str):
            managed_dirs = [x.strip() for x in managed_dirs.split(",") if x.strip()]

        item_path = str(b.get("item_path") or "").strip()
        if not item_path:
            # 没指定路径时，用受管目录下的第一个 strm 做试跑
            item_path = self._pick_sample_strm(managed_dirs, libs)

        if not item_path:
            # 找不到样本不算错误：端点、token、配置都可能是好的，
            # 只是用户还没填受管目录 / 库里没有可试跑的 strm。
            return self._json({
                "ok": True,
                "mode": "delete",
                "dry_run": dry,
                "action": "no_sample",
                "token_set": bool(token),
                "message": "回调链路就绪，但没有可试跑的 strm 样本。"
                           "请在上面的「受管目录」里填一个存在的目录，"
                           "或在下面的输入框里直接指定一个 strm 路径。",
            })

        payload = {
            "NotificationType": "library.deleted",
            "Item": {
                "Type": str(b.get("item_type") or "Movie"),
                "Id": "00000000-0000-0000-0000-000000000000",
                "Name": "EvoLink 自测条目",
                "Path": item_path,
            },
        }

        try:
            res = strm_webhook.handle_event(payload, libs, dry_run=dry,
                                            managed_dirs=managed_dirs)
        except Exception as e:
            return self._json({"ok": False, "error": "处理失败：%s" % e}, 500)

        res = dict(res)
        res["ok"] = True
        res["test_path"] = item_path
        res["dry_run"] = dry
        res["token_set"] = bool(token)
        return self._json(res)

    def _wh_ping(self, token, wh):
        """连通自测：确认 token 已配置、回调端点真的能收到并通过鉴权。"""
        if not token:
            return self._json({"ok": False, "error": "还没有生成 token：点下面的「生成 token」再测一次。"}, 400)
        if not wh.get("enabled", True):
            return self._json({"ok": False, "error": "webhook 已停用：先在上方打开开关再测。"}, 400)
        try:
            port = int(self.server.server_port)
        except Exception:
            try:
                port = int(os.environ.get("PORT") or 9191)
            except Exception:
                port = 9191
        url = "http://127.0.0.1:%d/api/emby/webhook?token=%s" % (
            port, urllib.parse.quote(token))
        t0 = time.time()
        try:
            req = urllib.request.Request(url, data=b"{}",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                code = resp.getcode()
                raw = resp.read(400).decode("utf-8", "replace")
        except Exception as e:
            return self._json({"ok": False, "error": "回调端点自测失败：%s" % e}, 200)
        ms = int((time.time() - t0) * 1000)
        try:
            j = json.loads(raw)
        except Exception:
            j = {}
        ok = bool(j.get("ok"))
        msg = ("回调端点可达、token 校验通过（%dms）。"
               "这只说明 EvoLink 侧能收到请求；Emby 后台仍需手动添加一次 Webhook 通知。" % ms
               ) if ok else ("回调端点返回：%s" % raw[:200])
        return self._json({"ok": ok, "mode": "ping", "http_status": code,
                            "elapsed_ms": ms, "callback_url": self._wh_url(token),
                            "message": msg}, 200)

    def _wh_ping(self, token, wh):
        """连通自测：确认 token 已配置、回调端点真的能收到并通过鉴权。"""
        if not token:
            return self._json({"ok": False, "error": "还没有生成 token：点下面的「生成 token」再测一次。"}, 400)
        if not wh.get("enabled", True):
            return self._json({"ok": False, "error": "webhook 已停用：先在上方打开开关再测。"}, 400)
        try:
            port = int(self.server.server_port)
        except Exception:
            try:
                port = int(os.environ.get("PORT") or 9191)
            except Exception:
                port = 9191
        url = "http://127.0.0.1:%d/api/emby/webhook?token=%s" % (
            port, urllib.parse.quote(token))
        t0 = time.time()
        try:
            req = urllib.request.Request(url, data=b"{}",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                code = resp.getcode()
                raw = resp.read(400).decode("utf-8", "replace")
        except Exception as e:
            return self._json({"ok": False, "error": "回调端点自测失败：%s" % e}, 200)
        ms = int((time.time() - t0) * 1000)
        try:
            j = json.loads(raw)
        except Exception:
            j = {}
        ok = bool(j.get("ok"))
        msg = ("回调端点可达、token 校验通过（%dms）。"
               "这只说明 EvoLink 侧能收到请求；Emby 后台仍需手动添加一次 Webhook 通知。" % ms
               ) if ok else ("回调端点返回：%s" % raw[:200])
        return self._json({"ok": ok, "mode": "ping", "http_status": code,
                            "elapsed_ms": ms, "callback_url": self._wh_url(token),
                            "message": msg}, 200)

    def _pick_sample_strm(self, managed_dirs, libs) -> str:
        """在受管目录 / 媒体库输出目录下找一个 .strm 作为自测样本。"""
        dirs = []
        for d in (managed_dirs or []):
            if isinstance(d, str) and d.strip():
                dirs.append(d.strip())
        for lib in (libs or []):
            if isinstance(lib, dict):
                od = lib.get("outdir")
                if isinstance(od, str) and od.strip():
                    dirs.append(od.strip())
        for d in dirs:
            try:
                for name in sorted(os.listdir(d)):
                    p = os.path.join(d, name)
                    if os.path.isdir(p):
                        for sub in sorted(os.listdir(p)):
                            sp = os.path.join(p, sub)
                            if os.path.isfile(sp) and sp.lower().endswith(".strm"):
                                return sp
                    elif name.lower().endswith(".strm"):
                        return p
            except OSError:
                continue
        return ""

    def api_strm_webhook_logs(self, g):
        """最近的处理日志：?limit=（默认 20）。"""
        try:
            limit = int(g("limit", "20"))
        except (TypeError, ValueError):
            limit = 20
        limit = max(1, min(200, limit))
        rows = []
        try:
            conn = db.conn()
        except Exception as e:
            return self._json({"logs": [], "error": str(e)})
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS strm_webhook_log (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts        TEXT NOT NULL,
                    event     TEXT DEFAULT '',
                    type      TEXT DEFAULT '',
                    name      TEXT DEFAULT '',
                    action    TEXT DEFAULT '',
                    targets   TEXT DEFAULT '',
                    reason    TEXT DEFAULT '',
                    deleted   TEXT DEFAULT '',
                    errors    TEXT DEFAULT ''
                )"""
            )
            rs = conn.execute(
                "SELECT ts, event, type, name, action, targets, reason, deleted, errors "
                "FROM strm_webhook_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            for r in rs:
                d = dict(r)
                for k in ("targets", "deleted", "errors"):
                    try:
                        d[k] = json.loads(d.get(k) or "[]")
                    except Exception:
                        d[k] = []
                rows.append(d)
        finally:
            conn.close()
        return self._json({"logs": rows})
    # ------------------------------------------------------------------
    # 归档刮削 / 权重洗板 / 分类设置
    # ------------------------------------------------------------------

    def _app_cfg(self, key, default):
        """读一个「模块配置」：config 文件里取，不足的部分用默认值补齐。"""
        try:
            cfg = config.load() or {}
        except Exception:
            cfg = {}
        v = cfg.get(key)
        if not isinstance(v, dict):
            v = {}
        merged = dict(default)
        merged.update(v)
        return merged

    def _app_save(self, key, val):
        """把「模块配置」写回 config 文件。"""
        try:
            cfg = config.load() or {}
        except Exception:
            cfg = {}
        cfg[key] = val
        config.save(cfg)

    def _media_scan(self, src_dir, limit=2000):
        """遍历本地目录收集媒体文件，返回 [{path, filename, size}]。"""
        out = []
        src_dir = str(src_dir or "").strip()
        if not src_dir or not os.path.isdir(src_dir):
            return out
        for root, dirs, files in os.walk(src_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fn in files:
                if not fn.lower().endswith(self.MEDIA_EXTS):
                    continue
                fp = os.path.join(root, fn)
                try:
                    sz = os.path.getsize(fp)
                except OSError:
                    sz = 0
                out.append({"path": fp, "filename": fn, "size": sz})
                if len(out) >= limit:
                    return out
        return out

    def api_archive_config(self, body):
        """GET/POST：归档刮削的扫描源、输出目录、命名模板等配置。"""
        cfg = self._app_cfg(self.ARCHIVE_CONFIG_KEY, self.DEFAULT_ARCHIVE_CFG)
        b = body or {}
        changed = False
        for k, v in b.items():
            if k in cfg and cfg.get(k) != v:
                cfg[k] = v
                changed = True
        if changed:
            self._app_save(self.ARCHIVE_CONFIG_KEY, cfg)
        return self._json({"ok": True, "config": cfg})

    def api_archive_plan(self, body):
        """扫描 → 识别 → 匹配 TMDB → 分类，产出计划（不写盘）。"""
        if strm_archive is None:
            return self._json({"ok": False, "error": "归档模块未就绪"}, 500)
        b = body or {}
        cfg = self._app_cfg(self.ARCHIVE_CONFIG_KEY, self.DEFAULT_ARCHIVE_CFG)
        for k in ("src_dir", "out_dir", "name_template", "folder_template",
                  "match_threshold", "limit", "category_first", "tmdb_name",
                  "skip_existing", "fmt", "store_prefix", "cd2_prefix", "base_url",
                  "weight_rule_movie", "weight_rule_tv", "keep_count"):
            if k in b:
                cfg[k] = b[k]
        # 计划与写入共用同一份配置：本次传入的参数必须落盘，
        # 否则「先生成计划 → 再执行写入」时 apply 从磁盘读到旧 out_dir，
        # 会把每一条都误判成「目标越界」。
        self._app_save(self.ARCHIVE_CONFIG_KEY, cfg)
        src_dir = str(cfg.get("src_dir") or "").strip()
        out_dir = str(cfg.get("out_dir") or "").strip()
        if not src_dir:
            return self._json({"ok": False, "error": "请先填写「扫描源目录」"}, 400)
        if not out_dir:
            return self._json({"ok": False, "error": "请先填写「strm 输出目录」"}, 400)
        try:
            limit = max(1, min(50000, int(cfg.get("limit") or 2000)))
        except (TypeError, ValueError):
            limit = 2000
        files = self._media_scan(src_dir, limit)
        if not files:
            return self._json({"ok": True, "plan": [], "summary": {},
                               "scanned": 0, "msg": "该目录下没找到媒体文件"})
        try:
            index = strm_archive.load_catalog_index()
        except Exception as e:
            return self._json({"ok": False, "error": "读取 TMDB 索引失败：%s" % e}, 500)
        cls_cfg = self._app_cfg(self.CLASSIFY_CONFIG_KEY, self.DEFAULT_CLASSIFY_CFG)
        # 分类配置里的「一级目录 = 媒体类型」单独存在 media_type 字段，
        # 而 classify_media 期望它在 categories 里，这里补齐，
        # 否则归档路径会丢掉「电影 / 剧集」这一层。
        categories = dict(cls_cfg.get("categories") or {})
        mt_cfg = cls_cfg.get("media_type")
        if isinstance(mt_cfg, dict):
            categories["media_type"] = mt_cfg
        # 洗板：电影 / 剧集各取一套权重方案
        wcfg = self._app_cfg(self.WEIGHT_CONFIG_KEY, {"rules": {}, "active": ""})
        wrules = wcfg.get("rules") or {}
        mr = str(cfg.get("weight_rule_movie") or "").strip()
        tr = str(cfg.get("weight_rule_tv") or "").strip()
        weight_sel = {
            "movie": wrules.get(mr) if mr else None,
            "tv": wrules.get(tr) if tr else None,
        }
        try:
            keep_count = max(1, int(cfg.get("keep_count") or 1))
        except (TypeError, ValueError):
            keep_count = 1
        try:
            plan = strm_archive.plan_archive(
                files, index, cls_cfg.get("categories") or {},
                str(cfg.get("name_template") or ""),
                str(cfg.get("folder_template") or ""),
                out_dir=out_dir,
                category_first=bool(cfg.get("category_first")),
                tmdb_name=bool(cfg.get("tmdb_name")),
                skip_existing=bool(cfg.get("skip_existing", True)),
                weight_rule=weight_sel,
                keep_count=keep_count)
        except Exception as e:
            return self._json({"ok": False, "error": "生成计划失败：%s" % e}, 500)
        try:
            summary = strm_archive.summarize_plan(plan)
        except Exception:
            summary = {}
        return self._json({"ok": True, "plan": plan[:8000], "summary": summary,
                           "scanned": len(files), "truncated": len(plan) > 8000})

    def _guess_base_url(self) -> str:
        """推断 EvoLink 的对外地址（生成 evolink 型 strm 时用）。

        依次尝试：归档面板填的 base_url → 全局配置 base_url →
        本次请求的 Host 头。都没有就返回空串。
        """
        try:
            cfg = config.load() or {}
        except Exception:
            cfg = {}
        v = str(cfg.get("base_url") or "").strip().rstrip("/")
        if v:
            return v
        # 用请求的 Host 头，这样用户从浏览器访问 EvoLink 时生成的 strm
        # 里就是他自己访问的地址，不用手动再填一遍。
        try:
            host = self.headers.get("Host") or ""
        except Exception:
            host = ""
        host = str(host).strip()
        if not host:
            return ""
        try:
            port = int(self.server.server_address[1])
        except Exception:
            port = 0
        scheme = "http"
        try:
            if port == 443:
                scheme = "https"
        except Exception:
            pass
        return "%s://%s" % (scheme, host)

    def _pan_rel_path(self, src: str, store: str, cd2: str, src_dir: str) -> str:
        """从容器内的源文件路径，反推出「网盘内相对路径」。

        src 形如 /CloudNAS/CloudDrive/115open/Video/Movie/xxx.mkv，
        期望返回 Video/Movie/xxx.mkv（store=115open 之后的全部）。
        找不到 store 时退回相对 src_dir 的路径。
        """
        p = str(src or "").replace("\\", "/")
        if store:
            idx = p.find("/" + store + "/")
            if idx >= 0:
                return p[idx + len(store) + 2:]
            if p.startswith(store + "/"):
                return p[len(store) + 1:]
        # 退路：相对 src_dir
        if src_dir:
            try:
                return os.path.relpath(src, src_dir).replace(os.sep, "/")
            except ValueError:
                pass
        return os.path.basename(src)

    def api_archive_apply(self, body):
        """按计划写 strm（方案 A：只在本地生成 strm，不动网盘文件）。"""
        b = body or {}
        plan = b.get("plan") or []
        dry = bool(b.get("dry_run"))
        cfg = self._app_cfg(self.ARCHIVE_CONFIG_KEY, self.DEFAULT_ARCHIVE_CFG)
        fmt = str(cfg.get("fmt") or "evolink").strip().lower()
        cd2 = str(cfg.get("cd2_prefix") or "/CloudNAS/CloudDrive").strip().rstrip("/")
        store = str(cfg.get("store_prefix") or "115open").strip("/")
        base_url = str(cfg.get("base_url") or "").strip().rstrip("/")
        src_dir = str(cfg.get("src_dir") or "").strip()
        # 输出目录为空时不做越界校验（os.path.abspath("") 会变成当前工作目录，
        # 那样每条计划都会被判成越界）。
        _out = str(cfg.get("out_dir") or "").strip()
        out_root = os.path.abspath(_out) if _out else ""
        written = skipped = 0
        written = skipped = 0
        errors = []
        for item in plan:
            if not isinstance(item, dict):
                continue
            if item.get("skip"):
                skipped += 1
                continue
            dst = str(item.get("dst") or "").strip()
            src = str(item.get("src") or "").strip()
            if not dst or not src:
                skipped += 1
                continue
            if out_root and not os.path.abspath(dst).startswith(out_root + os.sep):
                errors.append("目标越界：%s" % dst)
                continue
            # strm 里要写的是「网盘内相对路径」（如 115open/Video/Movie/xxx.mkv），
            # 换链端点才能定位到 115 上的文件。
            # src 是容器内 CD2 挂载路径，形如 /CloudNAS/CloudDrive/<store>/<网盘路径>，
            # 所以要从 src 里截出 <store> 之后的那一段，不能相对 src_dir 算
            # —— 否则会丢掉 store 到文件之间的中间目录。
            rel = self._pan_rel_path(src, store, cd2, src_dir)
            full = ("%s/%s" % (store, rel)) if store else rel
            if fmt == "evolink":
                if not base_url:
                    base_url = self._guess_base_url()
                if not base_url:
                    errors.append("未配置 base_url，无法生成 evolink 型 strm（请在归档面板填写 EvoLink 的对外地址）")
                    continue
                content = "%s/api/swap115/%s" % (base_url, urllib.parse.quote(full, safe=""))
            elif fmt == "pc":
                errors.append("pc 型需要 pickcode，归档流程暂不支持，请用 evolink 或 path")
                continue
            else:
                content = "%s/%s" % (cd2, full)
            try:
                if not dry:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    tmp = dst + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as fh:
                        fh.write(content + "\n")
                    os.replace(tmp, dst)
                written += 1
            except Exception as e:
                errors.append("%s：%s" % (dst, e))
        return self._json({"ok": True, "written": written, "skipped": skipped,
                           "errors": errors[:50], "dry_run": dry})

    def api_weight_config(self, body):
        """GET/POST：权重洗板的权重表配置。"""
        cfg = self._app_cfg(self.WEIGHT_CONFIG_KEY, {"weights": {}, "keep_count": 1})
        b = body or {}
        changed = False
        for k in ("weights", "keep_count"):
            if k in b and cfg.get(k) != b[k]:
                cfg[k] = b[k]
                changed = True
        if changed:
            self._app_save(self.WEIGHT_CONFIG_KEY, cfg)
        defaults = {}
        if strm_weight is not None:
            try:
                defaults = strm_weight.normalize_weight_config({}).get("dims", {})
            except Exception:
                defaults = {}
        return self._json({"ok": True, "config": cfg, "defaults": defaults})

    BUILTIN_WEIGHT_RULE_ID = "builtin_default"

    def _builtin_weight_rule(self):
        """内置默认洗板方案（每次都从代码生成，保证是最新且不被用户改坏）。"""
        rule = json.loads(json.dumps(strm_weight.DEFAULT_RULE))
        rule["name"] = "EvoLink 默认洗板"
        rule["id"] = self.BUILTIN_WEIGHT_RULE_ID
        rule["builtin"] = True
        rule["updated"] = ""
        return rule

    def _ensure_builtin_rule(self, rules):
        """保证内置默认方案始终存在于方案表里（缺失或过期都刷新）。"""
        builtin = self._builtin_weight_rule()
        cur = rules.get(self.BUILTIN_WEIGHT_RULE_ID)
        if not isinstance(cur, dict) or not cur.get("builtin"):
            rules[self.BUILTIN_WEIGHT_RULE_ID] = builtin
            return True
        return False

    def _weight_rules(self):
        """读所有权重方案。返回 (rules: {id: rule}, active: id)。"""
        cfg = self._app_cfg(self.WEIGHT_CONFIG_KEY, {"rules": {}, "active": ""})
        rules = cfg.get("rules")
        if not isinstance(rules, dict):
            rules = {}
        if self._ensure_builtin_rule(rules):
            self._weight_rules_save(rules, str(cfg.get("active") or ""))
        return rules, str(cfg.get("active") or "")

    def _weight_rules_save(self, rules, active=""):
        cfg = self._app_cfg(self.WEIGHT_CONFIG_KEY, {"rules": {}, "active": ""})
        cfg["rules"] = rules
        if active:
            cfg["active"] = active
        self._app_save(self.WEIGHT_CONFIG_KEY, cfg)
        return cfg

    def api_weight_rules(self, body):
        """GET：列出所有权重洗板方案（内置默认方案始终排在最前）。"""
        rules, active = self._weight_rules()
        out = []
        for rid, r in rules.items():
            if not isinstance(r, dict):
                continue
            out.append({"id": rid, "name": r.get("name") or rid,
                        "updated": r.get("updated") or "",
                        "builtin": bool(r.get("builtin"))})
        out.sort(key=lambda x: (not x.get("builtin"), x.get("name") or ""))
        if not active or active not in rules:
            active = self.BUILTIN_WEIGHT_RULE_ID
        return self._json({"ok": True, "rules": out, "active": active})

    def api_weight_rule_get(self, body):
        """GET ?id=xxx：取一份方案全文。body 可以是 GET 取值函数 g，也可以是 dict。"""
        rid = self._q_get(body, "id").strip()
        rules, active = self._weight_rules()
        r = rules.get(rid)
        if not isinstance(r, dict):
            return self._json({"ok": False, "error": "方案不存在"}, 404)
        # 取用时洗掉历史脏数据（名单被误当成评分条目）
        r = strm_weight.sanitize_rule(r)
        return self._json({"ok": True, "id": rid, "rule": r, "active": active})

    def api_weight_rule_new(self, body):
        """POST：新建一份方案（复制内置默认方案作为起点）。body: {name?}"""
        if strm_weight is None:
            return self._json({"ok": False, "error": "权重模块未就绪"}, 500)
        b = body or {}
        name = str(b.get("name") or "我的洗板方案").strip() or "我的洗板方案"
        rule = self._builtin_weight_rule()
        rule["name"] = name
        rule.pop("builtin", None)
        rid = secrets.token_hex(16)
        rule["id"] = rid
        rule["updated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rules, active = self._weight_rules()
        rules[rid] = rule
        self._weight_rules_save(rules, rid)
        return self._json({"ok": True, "id": rid, "name": name})

    def api_weight_rule_reset(self, body):
        """POST：把内置默认方案恢复成出厂内容（不影响用户自建方案）。"""
        if strm_weight is None:
            return self._json({"ok": False, "error": "权重模块未就绪"}, 500)
        rules, active = self._weight_rules()
        rules[self.BUILTIN_WEIGHT_RULE_ID] = self._builtin_weight_rule()
        self._weight_rules_save(rules, active or self.BUILTIN_WEIGHT_RULE_ID)
        return self._json({"ok": True, "id": self.BUILTIN_WEIGHT_RULE_ID})

    def api_weight_rule_save(self, body):
        """POST：保存 / 新建一份方案。body: {id?, name, rule}"""
        if strm_weight is None:
            return self._json({"ok": False, "error": "权重模块未就绪"}, 500)
        b = body or {}
        rule = b.get("rule")
        if not isinstance(rule, dict):
            return self._json({"ok": False, "error": "缺少方案内容"}, 400)
        try:
            rule = strm_weight.normalize_rule(rule)
            rule = strm_weight.sanitize_rule(rule)
        except Exception:
            pass
        name = str(b.get("name") or rule.get("name") or "未命名方案").strip()
        rid = str(b.get("id") or "").strip()
        rules, active = self._weight_rules()
        if rid == self.BUILTIN_WEIGHT_RULE_ID:
            return self._json({"ok": False,
                               "error": "内置默认方案不能直接改，请点「另存为」复制一份再修改"}, 400)
        if not rid:
            rid = secrets.token_hex(16)
        rule["name"] = name
        rule["id"] = rid
        rule.pop("builtin", None)
        rule["updated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rules[rid] = rule
        self._weight_rules_save(rules, active or rid)
        return self._json({"ok": True, "id": rid, "name": name})

    def api_weight_rule_import(self, body):
        """POST：导入一份权重洗板 JSON。body: {text, name?}"""
        if strm_weight is None:
            return self._json({"ok": False, "error": "权重模块未就绪"}, 500)
        b = body or {}
        text = b.get("text")
        if not isinstance(text, str) or not text.strip():
            return self._json({"ok": False, "error": "请先选择 JSON 文件或粘贴 JSON 文本"}, 400)
        try:
            rule = strm_weight.import_rule_json(text)
            rule = strm_weight.sanitize_rule(rule)
        except ValueError as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        except Exception as e:
            return self._json({"ok": False, "error": "导入失败：%s" % e}, 400)
        name = str(b.get("name") or rule.get("name") or "导入方案").strip()
        rule["name"] = name
        rid = secrets.token_hex(16)
        rule["id"] = rid
        rule["updated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rules, active = self._weight_rules()
        rules[rid] = rule
        self._weight_rules_save(rules, active or rid)
        return self._json({"ok": True, "id": rid, "name": name, "rule": rule})

    def api_weight_rule_export(self, body):
        """GET ?id=xxx：导出方案为 JSON 文本。body 可以是 GET 取值函数 g，也可以是 dict。"""
        rid = self._q_get(body, "id").strip()
        rules, _active = self._weight_rules()
        r = rules.get(rid)
        if not isinstance(r, dict):
            return self._json({"ok": False, "error": "方案不存在"}, 404)
        try:
            text = strm_weight.export_rule_json(strm_weight.sanitize_rule(r)) if strm_weight else ""
        except Exception as e:
            return self._json({"ok": False, "error": "导出失败：%s" % e}, 500)
        return self._json({"ok": True, "id": rid, "name": r.get("name") or rid,
                            "json": text})

    def api_weight_rule_delete(self, body):
        """POST：删除一份方案。body: {id}"""
        rid = str((body or {}).get("id") or "").strip()
        if rid == self.BUILTIN_WEIGHT_RULE_ID:
            return self._json({"ok": False, "error": "内置默认方案不能删除"}, 400)
        rules, active = self._weight_rules()
        if rid not in rules:
            return self._json({"ok": False, "error": "方案不存在"}, 404)
        rules.pop(rid, None)
        if active == rid:
            active = self.BUILTIN_WEIGHT_RULE_ID
        self._weight_rules_save(rules, active)
        return self._json({"ok": True})

    def api_weight_scan(self, body):
        """扫描目录，把同一媒体的多个版本按权重方案分组打分排序。"""
        if strm_weight is None:
            return self._json({"ok": False, "error": "权重模块未就绪"}, 500)
        b = body or {}
        cfg = self._app_cfg(self.WEIGHT_CONFIG_KEY, {"rules": {}, "active": ""})
        rules = cfg.get("rules") or {}
        rid = str(b.get("rule_id") or cfg.get("active") or "").strip()
        rule = rules.get(rid) if rid else None
        if not isinstance(rule, dict):
            rule = {}
        src_dir = str(b.get("src_dir") or "").strip()
        if not src_dir:
            return self._json({"ok": False, "error": "请先填写扫描源目录"}, 400)
        try:
            limit = max(1, min(50000, int(b.get("limit") or 2000)))
        except (TypeError, ValueError):
            limit = 2000
        files = self._media_scan(src_dir, limit)
        if not files:
            return self._json({"ok": True, "groups": [], "scanned": 0,
                               "msg": "该目录下没找到媒体文件"})
        items = []
        for f in files:
            try:
                info = strm_classify.parse_release_name(f["filename"])
            except Exception:
                info = {}
            items.append({"name": f["filename"], "filename": f["filename"],
                          "path": f["path"], "size": f["size"],
                          "info": info or {},
                          "attrs": strm_weight.build_attrs(f["filename"], f["path"])})
        try:
            groups = strm_weight.group_by_media(items, rule)
        except Exception as e:
            return self._json({"ok": False, "error": "分组失败：%s" % e}, 500)
        out = []
        for key, g in list(groups.items())[:500]:
            vs = g.get("versions") or []
            out.append({
                "key": key,
                "title": g.get("title"),
                "year": g.get("year"),
                "season": g.get("season"),
                "count": len(vs),
                "best": vs[0] if vs else None,
                "versions": [{"name": v.get("name"), "path": v.get("path"),
                              "size": v.get("size"),
                              "total": (v.get("score_info") or {}).get("total", 0),
                              "parts": (v.get("score_info") or {}).get("parts", {}),
                              "rank": i + 1}
                             for i, v in enumerate(vs)]})
        return self._json({"ok": True, "groups": out, "scanned": len(items),
                            "rule_id": rid, "rule_name": (rule or {}).get("name") or ""})

    def api_classify_config(self, body):
        """GET/POST：分类设置（分类规则 + 识别词/替换规则）。"""
        cfg = self._app_cfg(self.CLASSIFY_CONFIG_KEY, self.DEFAULT_CLASSIFY_CFG)
        b = dict(body or {})
        # 兼容早期扁平结构：{movie, tv, kw} -> {categories:{movie,tv}, rules:{keyword_links}}
        if ("movie" in b or "tv" in b) and "categories" not in b:
            b["categories"] = {"movie": b.pop("movie") or {}, "tv": b.pop("tv") or {}}
        if "kw" in b and "rules" not in b:
            b["rules"] = {"keyword_links": b.pop("kw") or []}
        changed = False
        for k in ("categories", "rules", "media_type"):
            if k in b and cfg.get(k) != b[k]:
                cfg[k] = b[k]
                changed = True
        if changed:
            self._app_save(self.CLASSIFY_CONFIG_KEY, cfg)
        return self._json({"ok": True, "config": cfg})

    # 分类模板：只是「一键套用」的起点，套用后用户仍可自由增删改。
    # 目录层级：一级 = 媒体类型；二级 = 这里配的分类；三级及以下 = 规则里的子目录。
    # 所以这里配的都是「区域 / 语言 / 内容形态」级别的分类，
    # 「动作片 / 喜剧片 / 科幻片」这种类型细分属于三级，放在子目录里。
    # 规则语义：
    #   - 过滤字段（与关系）：genre_ids / genres / original_language /
    #     origin_country / production_countries / regions / year / keywords
    #   - 目录生成字段（不过滤）：series_actors / series_directors / series_keywords
    #   - 空规则 = 兜底，匹配一切，所以必须放最后一条。
    CLASSIFY_TEMPLATES = [
        {
            "id": "by_region_language",
            "name": "按地区与语言划分",
            "desc": "二级目录按出品地区 / 原始语言分：纪录片、演唱会、动漫、国产、日韩印、欧美。类型细分留给三级子目录。",
            "media_type": {"enabled": True, "movie": "电影", "tv": "剧集"},
            "movie": {
                "纪录片": {"genre_ids": "99,-10402",
                           "desc": "纪录片，排除音乐纪录片（负号表示排除）"},
                "演唱会": {"genre_ids": "10402",
                           "desc": "演唱会、音乐会、歌剧等现场演出"},
                "国语动漫": {"genre_ids": "16",
                             "original_language": "zh,cn,bo,za",
                             "desc": "中文片名或国语配音的动画电影"},
                "原声动漫": {"genre_ids": "16",
                             "desc": "其它语言的动画电影"},
                "国产电影": {"original_language": "zh,cn,bo,za",
                             "desc": "中文片名的电影"},
                "日韩印电影": {"origin_country": "JP,KP,KR,TH,VN,ID,MY,SG,IN,PH",
                               "desc": "日韩及东南亚、南亚出品的电影"},
                "欧美电影": {"desc": "兜底：以上都不符合的都归到这里，规则要放最后"}
            },
            "tv": {
                "国漫剧集": {"genre_ids": "16",
                             "origin_country": "CN,TW,HK",
                             "desc": "中国内地及港澳台出品的动画剧集"},
                "动漫剧集": {"genre_ids": "16",
                             "origin_country": "JP,US,FR,GB,DE,ES,IT,KP,PT,RU,UK",
                             "desc": "其它国家出品的动画剧集"},
                "纪录片": {"genre_ids": "99", "desc": "纪录片剧集"},
                "综艺": {"genre_ids": "10764,10767", "desc": "综艺、真人秀"},
                "近期国产剧": {"origin_country": "CN,TW,HK,SG", "year": "2020-2099",
                              "desc": "2020 年及以后（含 2020）的国产剧；年份缺失的不会落这里"},
                "国产剧": {"origin_country": "CN,TW,HK,SG",
                          "desc": "2020 年以前以及年份缺失的国产剧"},
                "日韩剧": {"origin_country": "JP,KP,KR,TH,VN,ID", "desc": "日韩剧集"},
                "欧美剧": {"origin_country": "US,FR,GB,DE,ES,IT,NL,PT,RU,UK,CO",
                           "desc": "欧美剧集"},
                "未分类": {"desc": "兜底：以上都不符合的都归到这里，规则要放最后"}
            }
        },
        {
            "id": "by_type_sub",
            "name": "类型下沉为三级",
            "desc": "二级目录按地区分，动作片 / 喜剧片 / 科幻片这些类型细分放在三级子目录里。",
            "media_type": {"enabled": True, "movie": "电影", "tv": "剧集"},
            "movie": {
                "欧美电影": {
                    "desc": "非亚洲地区的电影",
                    "sub": {
                        "动作片": {"genres": "动作"},
                        "喜剧片": {"genres": "喜剧"},
                        "科幻片": {"genres": "科幻"},
                        "惊悚片": {"genres": "惊悚"},
                        "动画片": {"genres": "动画"},
                        "纪录片": {"genre_ids": "99"}
                    }
                },
                "国产电影": {
                    "original_language": "zh,cn,bo,za",
                    "desc": "中文片名的电影",
                    "sub": {
                        "动作片": {"genres": "动作"},
                        "喜剧片": {"genres": "喜剧"},
                        "科幻片": {"genres": "科幻"},
                        "动画片": {"genres": "动画"}
                    }
                },
                "港台电影": {"origin_country": "HK,TW", "desc": "中国香港、中国台湾出品的电影"},
                "日韩电影": {"origin_country": "JP,KP,KR", "desc": "日韩出品的电影"},
                "其他电影": {"desc": "兜底：以上都不符合的都归到这里"}
            },
            "tv": {
                "国产剧": {
                    "origin_country": "CN,TW,HK,SG",
                    "desc": "国产剧集",
                    "sub": {
                        "科幻剧": {"genres": "科幻"},
                        "悬疑剧": {"genres": "悬疑"},
                        "喜剧剧": {"genres": "喜剧"},
                        "动漫剧": {"genres": "动画"}
                    }
                },
                "欧美剧": {
                    "origin_country": "US,FR,GB,DE,ES,IT,NL,PT,RU,UK,CO",
                    "desc": "欧美剧集",
                    "sub": {
                        "科幻剧": {"genres": "科幻"},
                        "悬疑剧": {"genres": "悬疑"},
                        "喜剧剧": {"genres": "喜剧"},
                        "动漫剧": {"genres": "动画"}
                    }
                },
                "日韩剧": {"origin_country": "JP,KP,KR", "desc": "日韩剧集"},
                "其他剧集": {"desc": "兜底：以上都不符合的都归到这里"}
            }
        },
        {
            "id": "blank",
            "name": "空白模板",
            "desc": "什么都不预设，从零开始自己搭分类规则。",
            "media_type": {"enabled": True, "movie": "电影", "tv": "剧集"},
            "movie": {},
            "tv": {}
        }
    ]

    def api_classify_dict(self, body):
        """GET：TMDB 类型 / 语种 / 国家地区字典，供界面「查看字段字典」展示。"""
        if strm_classify is None:
            return self._json({"ok": False, "error": "分类模块未就绪"}, 500)
        return self._json({
            "ok": True,
            "genres": strm_classify.TMDB_GENRES,
            "languages": strm_classify.TMDB_LANGUAGES,
            "countries": strm_classify.TMDB_COUNTRIES,
        })

    def api_classify_templates(self, body):
        """GET：可选的分类模板清单（供前端「从模板选择」用）。"""
        return self._json({"ok": True, "templates": self.CLASSIFY_TEMPLATES})


    def api_link_scan(self):
        """全量扫描 Link 目录：找出 Emby 实际库没有引用的闲置 strm（可多选删）。

        与 /api/link-check 的分工：check 只核对 dup 详情里已有路径（快）；
        scan 遍历整个 strm 目录做全量对比（32 万文件约 10-20 秒），抓的是
        「从没进过 Emby 库」的真闲置。结果缓存 5 分钟，删除后失效。
        """
        ld, td, pref = _link_cfg()
        if not os.path.isdir(ld):
            return self._json({"error": "strm 目录不存在：%s（compose 挂载，各用户路径不同）" % ld}, 400)
        emby, err = _link_emby_paths()
        if emby is None:
            return self._json({"error": err}, 400)
        with _LINK_LOCK:
            hit = _LINK_SCAN_CACHE.get("data")
            if hit and hit[0] > time.time():
                return self._json(hit[1])
        actual = {}   # 相对路径 -> 大小
        stack = [ld]
        while stack:
            d = stack.pop()
            try:
                with os.scandir(d) as it:
                    for e in it:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                stack.append(e.path)
                            elif e.name.endswith(".strm"):
                                actual[os.path.relpath(e.path, ld)] = e.stat().st_size
                        except OSError:
                            pass
            except OSError:
                pass
        idle = {p: s for p, s in actual.items() if (pref + p) not in emby}
        # 按一级目录分组
        groups = {}
        for p, s in idle.items():
            k = p.split(os.sep)[0] if os.sep in p else "(根目录)"
            g = groups.setdefault(k, {"name": k, "n": 0, "bytes": 0})
            g["n"] += 1; g["bytes"] += s
        out = {"ok": True,
               "total": len(actual), "used_n": len(actual) - len(idle),
               "idle_n": len(idle), "idle_bytes": sum(idle.values()),
               "groups": sorted(groups.values(), key=lambda g: -g["n"]),
               "items": [{"p": p, "s": s} for p, s in sorted(idle.items())],
               "link_dir": ld, "trash_dir": td,
               "scanned_at": datetime.datetime.now().strftime("%m-%d %H:%M")}
        with _LINK_LOCK:
            _LINK_SCAN_CACHE.update(ts=time.time(), data=(time.time() + 300, out))
        return self._json(out)

    def api_link_check(self, body):
        """批量核对路径是否还在 Emby 实际库里（dup 详情页标注用）。"""
        paths = [p for p in (body.get("paths") or []) if isinstance(p, str) and p.strip()]
        emby, err = _link_emby_paths()
        if emby is None:
            return self._json({"error": err}, 400)
        return self._json({"ok": True, "results": {p: (p in emby) for p in paths}})

    def api_link_delete(self, body):
        """多选删除：移入回收站（可恢复，不硬删）。

        两档语义（前端分流，弹窗文案不同）：
        - 库中缺失的文件：Emby 已无引用，删了最安全；
        - 仍在库的副本（删重复片的日常操作）：需 allow_in_lib=true 才放行。
        路径非法 / 非 .strm / 文件不存在：一律拒绝。

        notify_emby（弹窗勾选框，默认取配置 link_delete_notify_emby）：
        删完顺手通知 Emby 把对应条目从媒体库删掉，免得播放器里留「媒体缺失」的僵尸条目。
        文件删除不依赖通知结果——通知失败只记录，不影响回收站。
        """
        paths = [p for p in (body.get("paths") or []) if isinstance(p, str) and p.strip()]
        allow_in_lib = bool(body.get("allow_in_lib"))
        if not paths:
            return self._json({"error": "没有要删的路径"}, 400)
        notify = body.get("notify_emby")
        if notify is None:
            notify = config.load().get("link_delete_notify_emby", True)
        notify = bool(notify)
        ld, td, pref = _link_cfg()
        if not os.path.isdir(ld):
            return self._json({"error": "strm 目录不存在：%s（compose 挂载，各用户路径不同）" % ld}, 400)
        emby, err = _link_emby_paths()
        if emby is None:
            return self._json({"error": err}, 400)
        moved, skipped, failed, warned = [], [], [], []
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        for p in paths:
            if not p.endswith(".strm"):
                failed.append({"p": p, "e": "不是 .strm，拒绝"}); continue
            if ".." in p.replace("\\", "/").split("/"):
                failed.append({"p": p, "e": "非法路径"}); continue
            if p in emby and not allow_in_lib:
                skipped.append({"p": p, "e": "仍在 Emby 库中，已跳过"}); continue
            if p in emby:
                warned.append(p)
            rel = p[len(pref):] if p.startswith(pref) else p.lstrip("/")
            src = os.path.join(ld, rel)
            if not os.path.isfile(src):
                failed.append({"p": p, "e": "文件不存在（可能已删）"}); continue
            dst = os.path.join(td, stamp, rel)
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)
                moved.append(p)
            except Exception as e:
                failed.append({"p": p, "e": str(e)})
        # 删过文件，扫描结果失效；通知过 Emby 后，「是否在库」的缓存也失效
        with _LINK_LOCK:
            _LINK_SCAN_CACHE.update(ts=0.0, data=None)
            if notify and moved:
                _LINK_EMBY_CACHE.update(ts=0.0, paths=None)
        emby_res = None
        if notify and moved:
            emby_res = _emby_notify_deleted(moved)
        return self._json({"ok": True, "moved": moved, "skipped": skipped,
                           "failed": failed, "warned": warned, "trash": td,
                           "emby": emby_res})

    def api_link_trash(self):
        """回收站列表（相对回收站的路径 + 大小 + 时间）。"""
        _, td, _ = _link_cfg()
        items, total = [], 0
        if os.path.isdir(td):
            for root, _dirs, files in os.walk(td):
                for f in files:
                    fp = os.path.join(root, f)
                    try:
                        st = os.stat(fp)
                    except OSError:
                        continue
                    items.append({"p": os.path.relpath(fp, td), "s": st.st_size,
                                  "m": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%m-%d %H:%M")})
                    total += st.st_size
        items.sort(key=lambda x: x["m"], reverse=True)
        return self._json({"ok": True, "items": items[:2000], "n": len(items),
                           "total_bytes": total, "trash": td})

    def api_link_trash_restore(self, body):
        """从回收站恢复（按回收站内相对路径，移回 strm 目录原位）。

        回收站内存放路径是「<删除时间戳>/<原相对路径>」，恢复时去掉第一层时间戳。
        """
        ld, td, _ = _link_cfg()
        ok, failed = [], []
        for rel in (body.get("paths") or []):
            rel = str(rel or "")
            parts = rel.replace("\\", "/").split("/")
            if len(parts) < 2 or ".." in parts or not parts[0]:
                failed.append({"p": rel, "e": "非法路径"}); continue
            orig = "/".join(parts[1:])          # 第一段是删除时间戳目录，不是原路径
            src = os.path.join(td, rel)
            dst = os.path.join(ld, orig)
            try:
                if os.path.exists(dst):
                    failed.append({"p": rel, "e": "原位已有同名文件"}); continue
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)
                ok.append(rel)
            except Exception as e:
                failed.append({"p": rel, "e": str(e)})
        return self._json({"ok": True, "restored": ok, "failed": failed})

    def api_link_trash_purge(self, body):
        """彻底清空回收站（需 confirm=true，二次确认由前端弹窗把关）。"""
        if not body.get("confirm"):
            return self._json({"error": "请确认后再次操作"}, 400)
        _, td, _ = _link_cfg()
        try:
            if os.path.isdir(td):
                shutil.rmtree(td)
            return self._json({"ok": True})
        except Exception as e:
            return self._json({"error": str(e)}, 500)

    def api_thumb(self, item_id, tag, max_w="240"):
        """Emby 本地条目海报代理 + 磁盘缓存（本地库浏览用，直连不走代理）。"""
        if not item_id:
            return self._json({"error": "bad id"}, 400)
        key = hashlib.sha1(f"embythumb:{item_id}:{tag}:{max_w}".encode()).hexdigest()
        try:
            cdir = config.cache_dir()
        except Exception:
            cdir = None
        if cdir:
            f = cdir / (key + ".img")
            m = cdir / (key + ".ct")
            if f.exists():
                try:
                    return self._send_bytes(f.read_bytes(),
                                            m.read_text("utf-8").strip() if m.exists() else "image/jpeg")
                except Exception:
                    pass
        try:
            url = emby.image_url(item_id, tag or "", max_w=int(max_w))
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
                                        timeout=15) as r:
                data, ctype = r.read(), r.headers.get("Content-Type", "image/jpeg")
            if cdir and data:
                try:
                    tmp = cdir / (key + ".tmp")
                    tmp.write_bytes(data)
                    tmp.replace(cdir / (key + ".img"))
                    (cdir / (key + ".ct")).write_text(ctype, "utf-8")
                except Exception:
                    pass
            self._send_bytes(data, ctype)
        except Exception as e:
            self._json({"error": str(e)}, 502)

    def _send_bytes(self, data, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=2592000")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def api_proxy_test(self, g):
        """测代理连通性（可带 host/port 先测后存；off=1 测直连）。"""
        cfg = config.load()
        if g("off") == "1":
            proxy = ""
        else:
            host = (g("host") or "").strip() or str(cfg.get("proxy_host") or "")
            port = (g("port") or "").strip() or str(cfg.get("proxy_port") or "")
            proxy = config.proxy_url({"proxy_enabled": True, "proxy_host": host,
                                      "proxy_port": port, "proxy": f"http://{host}:{port}"}) if host else ""
        target = g("url") or "https://www.gstatic.com/generate_204"
        t0 = time.time()
        try:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})) if proxy else urllib.request.build_opener()
            req = urllib.request.Request(target, headers={"User-Agent": "EvoLink/1.0"})
            with opener.open(req, timeout=10) as r:
                r.read(64)
                code = r.status
            return self._json({"ok": True, "status": code, "ms": int((time.time() - t0) * 1000),
                               "proxy": proxy or "直连", "target": target})
        except Exception as e:
            return self._json({"ok": False, "ms": int((time.time() - t0) * 1000),
                               "proxy": proxy or "直连", "target": target,
                               "error": f"{type(e).__name__}: {e}"})

    # ---- TMDB Key 自检：可先测后存，直接把 401 的原因翻译成人话 ----
    def api_tmdb_test(self, g):
        cfg = config.load()
        key = (g("key") or "").strip()
        if not key or "••" in key:          # 掩码 = 前端没改，用已存的
            key = (cfg.get("tmdb_api_key") or "").strip()
        if not key:
            return self._json({"ok": False, "error": "还没填 Key：粘贴后点「测试」即可（不必先保存）"})
        from core.sources import tmdb as _t
        t0 = time.time()
        try:
            cli = _t.TMDB(key=key)
            cli.get("/configuration")       # 最轻的鉴权端点，401 就是 Key 本身被拒
            return self._json({"ok": True, "ms": int((time.time() - t0) * 1000),
                               "kind": "v4 读取令牌" if cli._is_v4() else "v3 API Key"})
        except Exception as e:
            return self._json({"ok": False, "ms": int((time.time() - t0) * 1000),
                               "error": str(e)})

    # ---- RE0 / 兜底通道自检 ----
    def api_re0_test(self):
        """用一部已知有资源的片试查，验证兜底检索通道是否可用。"""
        try:
            sm = hdhub.Hub()
            res = sm.search_resources("沙丘2", "movie", 693134)
        except Exception as e:
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 502)
        items = res.get("items") or []
        return self._json({
            "ok": bool(items),
            "count": len(items),
            "telegram": res.get("telegram") or 0,
            "message": res.get("message") or "",
            "configured": res.get("configured"),
            "pan_counts": res.get("pan_counts") or {},
            "samples": [it.get("title") for it in items[:3]],
        })

    # ---- RE0 直连通道（per-user 身份槽：每 uid 独立 cookie + 独立代理） ----
    @staticmethod
    def _re0_uid(body=None, g=None):
        """从请求里取身份槽 uid（缺省 default）。"""
        uid = ""
        if isinstance(body, dict):
            uid = body.get("uid") or ""
        if not uid and g is not None:
            try:
                uid = g("uid") or ""
            except Exception:
                uid = ""
        return re0direct._safe_uid(uid) or "default"

    def api_re0_direct_status(self, g=None):
        """直连通道状态：配置 / 浏览器 / 登录态（按 uid）。"""
        uid = self._re0_uid(g=g)
        try:
            return self._json(re0_client(uid).status())
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 502)

    def api_re0_direct_test(self, g=None):
        """通道自检：代理出口 → Cloudflare → 登录态 → 真查一部片（《沙丘2》）。

        分段返回，哪一段红就说明问题在哪。GET / POST 都收（GET 方便 curl 排查）。
        """
        body = g if isinstance(g, dict) else None
        uid = self._re0_uid(body=body, g=None if body else g)

        def q(k, dflt=""):
            v = None
            if body:
                v = body.get(k)
            elif g is not None:
                try:
                    v = g(k)
                except Exception:
                    v = None
            v = (str(v) if v is not None else "").strip()
            return v or dflt

        tmdb_id = q("tmdb_id", "693134")
        media_type = q("media_type", "movie")
        label = q("label", "沙丘2")
        try:
            r = re0_client(uid).test_channel(media_type, tmdb_id, label)
            r["ok"] = bool(r.get("ok"))
            return self._json(r)
        except Exception as e:
            return self._json({"ok": False, "uid": uid, "steps": [],
                               "message": "自检失败：%s" % e}, 502)

    def api_re0_direct_users(self):
        """身份槽列表。cookie/密码只回「是否已配置」，绝不回明文。"""
        cfg = config.load()
        users = _re0_users(cfg)
        out = []
        for uid, s in users.items():
            s = s if isinstance(s, dict) else {}
            out.append({
                "uid": uid,
                "name": s.get("name") or uid,
                "user": s.get("user") or "",
                "proxy": s.get("proxy") or "",
                "cookies_captured": bool((s.get("cookies") or "").strip()),
                "configured": bool(
                    ((s.get("user") or "").strip() and s.get("pass"))
                    or (s.get("cookies") or "").strip()),
            })
        out.sort(key=lambda x: (x["uid"] != "default", x["uid"]))
        return self._json({"ok": True, "users": out, "count": len(out)})

    def api_re0_direct_users_add(self, body):
        """新建身份槽。uid 即隔离键，用作 profile 目录名。"""
        body = body if isinstance(body, dict) else {}
        uid = re0direct._safe_uid(body.get("uid"))
        if not uid:
            return self._json({"ok": False, "error": "uid 不能为空（字母/数字/_/-）"}, 400)
        cfg = config.load()
        users = _re0_users(cfg)
        if uid in users:
            return self._json({"ok": False, "error": f"身份槽 {uid} 已存在"}, 409)
        users[uid] = {
            "name": str(body.get("name") or uid).strip()[:40] or uid,
            "proxy": str(body.get("proxy") or "").strip(),
            "user": "",
            "pass": "",
            "cookies": "",
        }
        cfg["re0_users"] = users
        config.save(cfg)
        print(f"[RE0直连] 新建身份槽 {uid}", flush=True)
        return self._json({"ok": True, "uid": uid})

    def api_re0_direct_users_del(self, body):
        """删除身份槽：连同 cookie / 代理 / 浏览器登录态一起清掉。"""
        body = body if isinstance(body, dict) else {}
        uid = re0direct._safe_uid(body.get("uid"))
        if not uid:
            return self._json({"ok": False, "error": "missing uid"}, 400)
        cfg = config.load()
        users = _re0_users(cfg)
        if uid not in users:
            return self._json({"ok": False, "error": "身份槽不存在"}, 404)
        users.pop(uid, None)
        cfg["re0_users"] = users
        config.save(cfg)
        re0direct.drop_re0_client(uid)
        try:
            import shutil
            shutil.rmtree(str(config.DATA / f"re0_profile_{uid}"), ignore_errors=True)
        except Exception:
            pass
        print(f"[RE0直连] 删除身份槽 {uid}", flush=True)
        return self._json({"ok": True, "uid": uid})

    def api_re0_direct_config(self, body):
        """保存某个身份的账号 / 代理。密码空串 = 不改。"""
        body = body if isinstance(body, dict) else {}
        uid = self._re0_uid(body=body)
        cfg = config.load()
        users = _re0_users(cfg)
        slot = users.get(uid)
        if not isinstance(slot, dict):
            slot = {"name": uid, "user": "", "pass": "", "proxy": "", "cookies": ""}
            users[uid] = slot
        for k in ("user", "proxy"):
            if k in body and body[k] is not None:
                slot[k] = str(body[k]).strip()
        if body.get("name") is not None:
            nm = str(body.get("name")).strip()[:40]
            if nm:
                slot["name"] = nm
        if body.get("pass"):
            slot["pass"] = str(body["pass"])
        cfg["re0_users"] = users
        if uid == "default":
            # 兼容旧全局键（历史数据 / 老读取方）
            cfg["re0_direct_user"] = slot.get("user") or ""
            cfg["re0_direct_proxy"] = slot.get("proxy") or ""
            if body.get("pass"):
                cfg["re0_direct_pass"] = slot.get("pass") or ""
        config.save(cfg)
        if uid == "default":
            re0browser.set_proxy(slot.get("proxy") or "")
        return self._json({"ok": True, "uid": uid})

    def api_re0_direct_login(self, body=None):
        """打开站点并登录（已登录则直接返回）。按 uid 的账号 / profile 登录。"""
        uid = self._re0_uid(body=body)
        try:
            return self._json(re0_client(uid).login())
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 502)

    def api_re0_direct_search(self, g):
        """按条目查 RE0 直连资源（结构对齐兜底通道，前端复用）。"""
        uid = self._re0_uid(g=g)
        cid = g("cid") or ""
        c = db.conn()
        if cid.startswith("o:"):
            r = c.execute("SELECT av_code,name FROM owned WHERE emby_id=?", (cid[2:],)).fetchone()
            d = {"category": "AV", "av_code": (r["av_code"] if r else ""),
                 "title": (r["name"] if r else "")}
        else:
            r = c.execute("SELECT * FROM catalog WHERE cid=?", (cid,)).fetchone()
            if not r:
                return self._json({"error": "not found"}, 404)
            d = dict(r)
        if d.get("category") in ("AV", "里番"):
            return self._json({"items": [], "message": "RE0 直连只查影视（TMDB 条目），AV/里番请用兜底通道或磁力搜索"})
        media, tmdb_id, title, _cat = _cid_media(d)
        if not tmdb_id:
            return self._json({"items": [], "message": f"条目没有 TMDB ID（{title}），RE0 按 TMDB 条目查资源"})
        cli = re0_client(uid)
        try:
            items = cli.search(media, tmdb_id)
        except Exception as e:
            return self._json({"ok": False, "error": str(e), "items": []}, 502)
        base = cli.base
        for it in items:
            it["url"] = f"{base}/resource/115/{it['slug']}"
        return self._json({"items": items, "channel": "re0-direct", "uid": uid,
                           "query": {"title": title, "media": media, "tmdb_id": tmdb_id}})

    def api_re0_direct_unlock(self, body):
        """解锁一条资源，拿 115 分享链接（可能一批 ed2k）。"""
        body = body if isinstance(body, dict) else {}
        uid = self._re0_uid(body=body)
        slug = (body.get("slug") or "").strip()
        if not slug:
            return self._json({"error": "missing slug"}, 400)
        try:
            points = int(body.get("points") or 0)
        except (TypeError, ValueError):
            points = 0
        try:
            return self._json(re0_client(uid).unlock(slug, points))
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 502)

    def api_re0_direct_preview(self, g):
        """文件预览（不解锁、不花积分）。"""
        uid = self._re0_uid(g=g)
        try:
            return self._json(re0_client(uid).preview(g("slug") or ""))
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 502)

    def api_re0_direct_checkin(self, body=None):
        """每日签到。"""
        uid = self._re0_uid(body=body)
        try:
            return self._json(re0_client(uid).checkin())
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 502)

    def api_re0_direct_browser(self, body):
        """浏览器下载管理：download / reinstall。"""
        action = (body.get("action") or "").strip()
        if action == "download":
            re0browser.ensure("界面手动触发")
            return self._json({"ok": True})
        if action == "reinstall":
            err = re0browser.reinstall("界面重新下载")
            return self._json({"ok": not err, "error": err})
        return self._json({"error": "unknown action"}, 400)

    def api_re0_direct_capture(self, body):
        """书签小工具从 re0.me 跨域提交登录态（cf_clearance + 会话 cookie）。
        仅接受来自 re0.me 上下文的提交；按 uid 存进对应身份槽，互不覆盖。
        两种提交通道：JSON（fetch）与表单跳转（https 页面跨域写 http 时，浏览器只放行后者）。"""
        is_form = "application/x-www-form-urlencoded" in (self.headers.get("Content-Type") or "").lower()

        def _reply(ok, msg, code=200, n=0, uid=""):
            if is_form:
                color = "#3B6D11" if ok else "#A32D2D"
                title = "提交成功" if ok else "提交失败"
                html = (
                    "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
                    "<title>RE0 登录态提交</title>"
                    "<body style=\"margin:0;height:100vh;display:flex;align-items:center;justify-content:center;"
                    "background:#f6f5f1;font-family:-apple-system,'PingFang SC',sans-serif\">"
                    f"<div style='text-align:center;padding:28px 34px;background:#fff;border-radius:12px;"
                    f"border:1px solid #e3e1da;max-width:420px'>"
                    f"<div style='font-size:17px;font-weight:600;color:{color};margin-bottom:10px'>{title}</div>"
                    f"<div style='font-size:14px;color:#5F5E5A;line-height:1.6'>{msg}</div>"
                    "<div style='font-size:13px;color:#888780;margin-top:14px'>这个标签页可以直接关掉。</div>"
                    "</div></body></html>"
                ).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                for k, v in _RE0_CAPTURE_CORS.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(html)
                return
            payload = {"ok": True, "captured": n, "uid": uid} if ok else {"ok": False, "error": msg}
            return self._json(payload, code, headers=list(_RE0_CAPTURE_CORS.items()))

        origin = (self.headers.get("Origin") or "")
        referer = (self.headers.get("Referer") or "")
        if "re0.me" not in origin and "re0.me" not in referer:
            return _reply(False, "仅允许从 re0.me 页面提交", 403)
        cookies = ""
        if isinstance(body, dict):
            cookies = (body.get("cookies") or "")
        if not cookies:
            return _reply(False, "没有拿到 cookie —— 请确认当前页面已登录 re0.me", 400)
        cookies = str(cookies).strip()
        if len(cookies) > 8192:
            return _reply(False, "cookie 过长", 413)
        uid = self._re0_uid(body=body)
        cfg = config.load()
        users = _re0_users(cfg)
        slot = users.get(uid)
        if not isinstance(slot, dict):
            slot = {"name": uid, "user": "", "pass": "", "proxy": ""}
            users[uid] = slot
        slot["cookies"] = cookies
        cfg["re0_users"] = users
        if uid == "default":
            cfg["re0_direct_cookies"] = cookies      # 兼容旧读取方
        config.save(cfg)
        # 该身份的浏览器已起着 → 立刻热注入，不用等下次启动
        try:
            cli = re0_client(uid)
            if getattr(cli, "_ctx", None) is not None:
                cli._submit(cli._inject_cookies, timeout=20)
        except Exception:
            pass
        n = len([p for p in cookies.split(";") if p.strip()])
        print(f"[RE0直连] 身份 {uid} 收到登录态提交，{n} 个 cookie（含 cf_clearance）", flush=True)
        return _reply(True, f"身份「{uid}」已收到 {n} 个 cookie（含 cf_clearance）", 200, n=n, uid=uid)

    def api_re0_direct_clear(self, body=None):
        """清除某个身份已提交的登录态（cookie 过期或想换账号时）。"""
        uid = self._re0_uid(body=body)
        cfg = config.load()
        users = _re0_users(cfg)
        slot = users.get(uid)
        if not isinstance(slot, dict):
            return self._json({"ok": False, "error": "身份槽不存在"}, 404)
        slot["cookies"] = ""
        cfg["re0_users"] = users
        if uid == "default":
            cfg["re0_direct_cookies"] = ""
        config.save(cfg)
        print(f"[RE0直连] 已清除身份 {uid} 的登录态", flush=True)
        return self._json({"ok": True, "uid": uid})

    # ---- RE0 可视登录会话（网页 VNC 里手动过 CF + Telegram 登录） ----
    def api_re0_direct_desktop_start(self, body):
        """开一个"看得见"的浏览器窗口，返回 noVNC 地址。"""
        uid = self._re0_uid(body=body)
        ok, err = _ensure_vnc_stack()
        if not ok:
            return self._json({"ok": False, "error": err}, 500)
        cli = re0_client(uid)
        try:
            info = cli._submit(cli.open_desktop, timeout=240)
        except Exception as e:
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 502)
        print(f"[RE0直连] 身份 {uid} 打开可视登录窗口", flush=True)
        return self._json({"ok": True, "uid": uid, "vnc_url": _vnc_url(self.headers.get("Host")),
                           "width": 1280, "height": 820, "page": info})

    def api_re0_direct_desktop_save(self, body):
        """把可视会话里的 cookie（含 HttpOnly）导出并保存到该身份槽，然后关窗口。"""
        uid = self._re0_uid(body=body)
        cli = re0_client(uid)
        try:
            r = cli._submit(cli.grab_cookies, timeout=120)
        except Exception as e:
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 502)
        if not r.get("ok"):
            return self._json(r, 400)
        try:
            cli._submit(cli.close_desktop, timeout=60)
        except Exception:
            pass
        print(f"[RE0直连] 身份 {uid} 已从可视会话保存 {r.get('count')} 个 cookie", flush=True)
        return self._json(r)

    def api_re0_direct_desktop_stop(self, body):
        """关掉可视会话（不保存）。"""
        uid = self._re0_uid(body=body)
        cli = re0_client(uid)
        try:
            cli._submit(cli.close_desktop, timeout=60)
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 502)
        return self._json({"ok": True, "uid": uid})

    def api_re0_direct_desktop_solve(self, body):
        """一键过 Cloudflare 验证：在可视会话里自动点 Turnstile 复选框。"""
        uid = self._re0_uid(body=body)
        cli = re0_client(uid)
        try:
            r = cli._submit(cli.solve_challenge, timeout=180)
        except Exception as e:
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 502)
        print(f"[RE0直连] 身份 {uid} 自动过验证 -> ok={r.get('ok')} url={r.get('url')}", flush=True)
        return self._json(r)

    def api_re0_direct_desktop_status(self, g):
        uid = self._re0_uid(g=g)
        cli = re0_client(uid)
        try:
            cli._reload_cfg()
            alive = cli._ctx is not None and getattr(cli, "_desktop", False)
            url = ""
            if alive:
                info = cli._submit(cli.desktop_touch, timeout=30)
                url = info.get("url") or ""
        except Exception:
            alive, url = False, ""
        return self._json({"ok": True, "uid": uid, "open": bool(alive), "page_url": url,
                           "vnc_ready": _port_open(_VNC["port"])})

    # ---- 115 网盘 ----
    def api_115_qr(self):
        try:
            d = pan115.qr_new()
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 502)
        b64 = base64.b64encode(d["image"]).decode()
        return self._json({"ok": True, "uid": d["uid"], "time": d["time"], "sign": d["sign"],
                           "image": f"data:{d['ctype']};base64,{b64}",
                           "apps": [{"code": c, "name": n} for c, n in pan115.APP_CHOICES]})

    def api_115_qr_status(self, g):
        uid, tm, sign = g("uid", ""), g("time", ""), g("sign", "")
        app = g("app", "web") or "web"
        if not uid:
            return self._json({"ok": False, "error": "缺少 uid"}, 400)
        done = _QR_DONE.get(uid)
        if done:
            return self._json(done)
        try:
            st = pan115.qr_status(uid, tm, sign)
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 502)
        if st["status"] != 2:
            return self._json({"ok": True, "status": st["status"], "msg": st["msg"]})
        try:
            with _QR_LOCK:
                if uid in _QR_DONE:
                    return self._json(_QR_DONE[uid])
                info = pan115.qr_login(uid, app)
                cfg = config.load()
                cfg["pan115_cookie"] = info["cookie"]
                cfg["pan115_user"] = info.get("user_name") or ""
                config.save(cfg)
                out = {"ok": True, "status": 2, "logged_in": True,
                       "user": info.get("user_name") or "", "app": app}
                _QR_DONE[uid] = out
                return self._json(out)
        except Exception as e:
            return self._json({"ok": False, "status": 2, "error": str(e)}, 502)

    def api_115_login(self, body):
        uid = str(body.get("uid") or "")
        app = str(body.get("app") or "web") or "web"
        if not uid:
            return self._json({"ok": False, "error": "缺少 uid"}, 400)
        try:
            info = pan115.qr_login(uid, app)
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 502)
        cfg = config.load()
        cfg["pan115_cookie"] = info["cookie"]
        cfg["pan115_user"] = info.get("user_name") or ""
        config.save(cfg)
        return self._json({"ok": True, "user": info.get("user_name") or ""})

    _BOTH = ("EXISTS(SELECT 1 FROM owned o WHERE (p.av_code!='' AND o.av_code=p.av_code) "
             "OR (p.norm!='' AND o.norm=p.norm))")

    def api_115_status(self, g=None):
        g = g or (lambda k, d="": d)
        # catalog 27.5 万行 × 相关子查询，即便有索引也要 20s+ → 默认不算，
        # 否则面板一开就卡死、遮罩不撤（什么都点不上）；前端拿 null 再后台补。
        full = (g("full", "0") or "0") == "1"
        cfg = config.load()
        ck = cfg.get("pan115_cookie") or {}
        acct = str(ck.get("UID") or "")
        c = db.conn()
        # 只统计**当前登录账号**的扫描结果：换账号后不会显示别人的网盘内容
        aw = "p.acct=?" if acct else "1=1"
        aa = [acct] if acct else []
        tot = c.execute(f"SELECT COUNT(*) n FROM pan115 p WHERE p.is_dir=0 AND {aw}", aa).fetchone()["n"]
        both = c.execute(f"SELECT COUNT(*) n FROM pan115 p WHERE p.is_dir=0 AND {aw} AND {self._BOTH}",
                         aa).fetchone()["n"]
        only = c.execute(f"SELECT COUNT(*) n FROM pan115 p WHERE p.is_dir=0 AND {aw} AND NOT {self._BOTH}",
                         aa).fetchone()["n"]
        cat_only = None
        if full:
            cat_only = c.execute(
                "SELECT COUNT(*) n FROM catalog c WHERE c.match_status='missing' AND NOT EXISTS("
                "SELECT 1 FROM pan115 p WHERE p.acct=? AND ((p.av_code!='' AND p.av_code=c.av_code) "
                "OR (p.norm!='' AND p.norm=c.norm)))", [acct] if acct else [""]).fetchone()["n"]
        return self._json({
            "ok": True,
            "logged_in": bool(ck.get("UID")),
            "user": cfg.get("pan115_user") or "",
            "acct": acct,
            "root": cfg.get("pan115_root") or "0",
            "apps": [{"code": c, "name": n} for c, n in pan115.APP_CHOICES],
            "scanned_at": db.get_meta(f"pan115_scanned_at:{acct}"),
            "counts": {"total": tot, "both": both, "pan_only": only, "catalog_only": cat_only},
        })

    def api_115_dirs(self, g):
        cid = g("cid", "0") or "0"
        try:
            rows = pan115.list_dir(cid)
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 502)
        dirs = sorted([{"cid": it["cid"], "name": it["name"]} for it in rows if it["is_dir"]],
                      key=lambda x: x["name"])
        return self._json({"ok": True, "cid": cid, "dirs": dirs,
                           "files": sum(1 for it in rows if not it["is_dir"])})

    def api_fs_ls(self, g):
        """浏览容器内已映射的可写目录（strm 输出目录选择器用）。

        只列子目录，路径限白名单根（/app/Link、/app/data），防任意目录穿越。
        """
        d = os.path.normpath(str(g("dir", "") or "").strip() or "/app/Link")
        if not _fs_in_roots(d):
            return self._json({"ok": False,
                               "error": "仅可浏览已映射可写区（/app/Link、/app/data）"}, 400)
        if not os.path.isdir(d):
            return self._json({"ok": False, "error": "目录不存在"}, 404)
        try:
            names = sorted((e.name for e in os.scandir(d)
                            if e.is_dir() and not e.name.startswith(".")),
                           key=str.lower)
        except OSError as e:
            return self._json({"ok": False, "error": str(e)}, 500)
        return self._json({"ok": True, "dir": d, "writable": True,
                           "roots": [{"path": r, "label": lb} for r, lb in _FS_BROWSE_ROOTS],
                           "dirs": [{"name": n, "path": d.rstrip("/") + "/" + n}
                                    for n in names]})

    def api_115_tree(self, g):
        """115 目录树：返回某目录下的子目录（供前端树形选择器展开）。

        比「进网盘逐级翻」省事：树上点开哪层就取哪层，不用跳来跳去。
        files 给该目录直属的视频文件数（方便判断值不值得展开）。
        """
        cid = g("cid", "0") or "0"
        try:
            rows = pan115.list_dir(cid)
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 502)
        dirs = sorted([{"cid": it["cid"], "name": it["name"]}
                       for it in rows if it.get("is_dir")], key=lambda x: x["name"])
        vids = [it for it in rows if not it.get("is_dir") and pan115.is_video(it["name"])]
        total = sum(1 for it in vids if it["name"].lower().endswith((".mkv", ".mp4", ".avi", ".mov",
                                                                   ".ts", ".m2ts", ".wmv", ".flv", ".rmvb")))
        return self._json({"ok": True, "cid": cid, "dirs": dirs,
                           "videos": len(vids), "total": total})

    def api_local_dirs(self, g):
        """列出本地某目录下的子目录（供前端目录选择器逐级浏览 / 选挂载点）。"""
        import os
        path = g("path", "/") or "/"
        if not os.path.isabs(path) or not os.path.isdir(path):
            return self._json({"ok": False, "error": "无效路径: " + str(path)}, 400)
        try:
            items = []
            for n in sorted(os.listdir(path)):
                fp = os.path.join(path, n)
                if os.path.isdir(fp) and not n.startswith('.'):
                    items.append({"name": n, "dir": True})
            return self._json({"ok": True, "dirs": items})
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 500)

    def api_115_compare(self, g):
        mode = (g("status") or "pan_only").strip()
        kw = (g("q") or "").strip()
        size = min(int(g("size", 100)), 500)
        page = max(int(g("page", 1)), 1)
        off = (page - 1) * size
        c = db.conn()
        # 只显示当前登录账号的扫描结果（换账号不串内容）
        acct = str((config.load().get("pan115_cookie") or {}).get("UID") or "")
        if mode in ("pan_only", "both"):
            cond = "NOT " if mode == "pan_only" else ""
            where = f"WHERE p.is_dir=0 AND p.acct=? AND {cond}{self._BOTH}"
            args = [acct]
            if kw:
                where += " AND p.name LIKE ?"
                args.append(f"%{kw}%")
            rows = c.execute(f"SELECT p.* FROM pan115 p {where} ORDER BY p.size DESC LIMIT ? OFFSET ?",
                             args + [size, off]).fetchall()
            total = c.execute(f"SELECT COUNT(*) n FROM pan115 p {where}", args).fetchone()["n"]
            items = [{"name": r["name"], "path": r["path"], "size": r["size"], "ext": r["ext"]}
                     for r in rows]
        else:  # catalog_only：发行片里缺、115 里也没有
            where = ("WHERE c.match_status='missing' AND NOT EXISTS(SELECT 1 FROM pan115 p "
                     "WHERE p.acct=? AND ((p.av_code!='' AND p.av_code=c.av_code) "
                     "OR (p.norm!='' AND p.norm=c.norm)))")
            args = [acct]
            if kw:
                where += " AND c.title LIKE ?"
                args.append(f"%{kw}%")
            rows = c.execute(f"SELECT c.title,c.year,c.category,c.tmdb_id FROM catalog c {where} "
                             f"ORDER BY c.popularity DESC LIMIT ? OFFSET ?", args + [size, off]).fetchall()
            total = c.execute(f"SELECT COUNT(*) n FROM catalog c {where}", args).fetchone()["n"]
            items = [{"name": r["title"], "year": r["year"], "category": r["category"],
                      "tmdb_id": r["tmdb_id"]} for r in rows]
        return self._json({"total": total, "page": page, "size": size, "mode": mode, "items": items})

    # ---- strm 生成器（备用功能：115 目录树直写 strm，不碰存量）----
    def api_strm_gen_plan(self, body):
        """预览生成计划：walk 选中目录，算好每个 strm 的路径与内容，不写盘。"""
        roots = body.get("roots")
        if isinstance(roots, list) and roots:
            roots = [str(x) for x in roots if str(x).strip()]
        else:
            roots = None
        cfg = config.load()
        # root_paths：cid → 完整 115 路径（前端目录选择器/目录树给）。
        # strm 内容里的路径必须完整，缺一段换链就 502；有它就直接用，不用反查。
        rp = body.get("rootPaths")
        root_paths = {}
        if isinstance(rp, dict):
            for k, v in rp.items():
                if str(k).strip() and str(v or "").strip():
                    root_paths[str(k).strip()] = str(v).strip().strip("/")
        elif isinstance(rp, list):
            for it in rp:
                if isinstance(it, dict):
                    c = str(it.get("cid") or "").strip()
                    p = str(it.get("path") or "").strip().strip("/")
                    if c and p:
                        root_paths[c] = p
        opts = {
            "fmt": _gen_fmt(body.get("fmt")),
            "bdmv": "main" if body.get("bdmv") == "main" else "skip",
            "keep_first": bool(body.get("keep_first")),
            "outdir": _gen_safe_dir(body.get("outdir")) or str(cfg.get("strm_gen_dir") or "/app/Link/gen115"),
            "base": str(body.get("base") or "").strip(),
            "cd2": str(body.get("cd2") or "").strip(),
            "prefix": str(body.get("prefix") or "").strip(),
            "root_paths": root_paths,
        }
        ok = run_task("strmGenPlan", lambda log: task_strm_gen_plan(
            log, roots, int(body.get("depth") or 12), opts))
        return self._json({"ok": ok, "error": "" if ok else "任务已在运行中"})

    def api_strm_gen_run(self, body):
        """按预览计划写盘（幂等：已存在且内容一致的 strm 自动跳过）。"""
        ok = run_task("strmGen", task_strm_gen)
        return self._json({"ok": ok, "error": "" if ok else "任务已在运行中"})

    def api_strm_gen_status(self, g):
        plan = _gen_plan_load()
        st = plan.get("stats") or {}
        return self._json({"ok": True, "has_plan": bool(plan.get("items")),
                           "at": plan.get("at") or "", "acct": plan.get("acct") or "",
                           "stats": {k: st.get(k) for k in
                                     ("total", "size", "by_top", "no_pc", "skipped",
                                      "sample", "roots", "depth", "fmt", "bdmv",
                                      "outdir", "keep_first")}})

    def api_redirect115(self, pc):
        """URL 型 strm 的落点：/api/redirect115/<pickcode> → 302 到 115cdn 直链。

        免登录：播放器（Emby/Jellyfin 容器）不带 evolink 会话，容器来源本就是私有网段；
        这里显式放行并加每 IP 限流。铁律不破：签完自检、失败不重签、302 头干净。
        """
        pc = urllib.parse.unquote(pc or "")
        if not pan115_downurl.is_pickcode(pc):
            return self._json({"error": "bad pickcode"}, 400)
        ip = self._client_ip()
        now = time.time()
        with _R115_LOCK:
            b = _R115_BUCKET.get(ip)
            if not b or now - b[0] > 60:
                if len(_R115_BUCKET) > 4096:      # 防字典无限涨
                    _R115_BUCKET.clear()
                _R115_BUCKET[ip] = [now, 1]
            else:
                b[1] += 1
                if b[1] > _R115_MAX:
                    return self._json({"error": "too many requests"}, 429)
        ua = self.headers.get("User-Agent") or ""
        try:
            url, ok, note = reverse_proxy._sign_and_verify(pc, ua)
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 502)
        if not url:
            return self._json({"error": note or "签发失败"}, 502)
        if not ok:
            # 自检没过（多半 115 限流期）：链已签出，但没有实体文件可退回透传，诚实报错、绝不重签
            return self._json({"error": note or "自检失败"}, 502)
        # 302 头要干净（与反代一致）：不发 Cache-Control（否则播放器缓存旧 Location，一快进就断），
        # 带 Accept-Ranges 让播放器用 Range 做 seek。
        self.send_response(302)
        self.send_header("Location", url)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ------------------------------------------------------------------
    # 路径型 302 换链（路径映射 + 双层缓存 + 干净 302 头的完整方案）
    #
    # 背景：现存 32 万个 strm 全部是**路径型**（第一行写的是网盘绝对路径）
    #   /CloudNAS/CloudDrive/115open/Video/Movie/xxx/xxx.mkv
    # /api/redirect115/<pickcode> 那条路只吃 pickcode，路径型走不通。
    # 同类实现的做法（从运行日志逐行还原）：
    #   ① 用 filepath_mapping 把本地挂载前缀换成 115 前缀，再剥掉首段存储名
    #      → /CloudNAS/CloudDrive/115open/ => 115open/  ⇒  115open/Video/... ⇒ Video/...
    #   ② 查重定向 URL 缓存（TTL 1800，url_cache_expire）
    #   ③ 未命中 → 115 助手按路径签直链（带播放器自己的 UA）
    #   ④ 302 Location = CDN 直链；换链失败按 use_original 回退原始 URL
    #
    # 本端点：**不需要播放器数据库**（不像反代要先查 Jellyfin/Plex 的 SQLite），
    # 路径本身就是全部输入 —— 所以 Jellyfin/Plex/Emby/任何播放器都能直接用。
    # ------------------------------------------------------------------
    def api_swap115(self, raw_path):
        """路径型 strm 的落点：/api/swap115/<云盘路径> → 302 到 115cdn 直链。

        这是 EvoLink 独有的第三种 strm 格式（另两种是 CD2 挂载路径、pickcode）：
        strm 里只写**云盘路径**，pickcode 不落盘，运行时由 EvoLink 解析路径 →
        取 pickcode → 签发直链 → 302。

        为什么要有它：
        - pickcode 绑账号（115 的 errno=50015 账号隔离），换账号整库 strm 全废；
          路径型不受影响，换号只需重新登录 EvoLink，**32 万 strm 不用重建**。
        - 换链逻辑全部收敛在 EvoLink 一处，不再散落到 CD2 / 各播放器配置里。

        免登录：播放器容器不带会话，来源本就是私有网段；显式放行 + 每 IP 限流。
        铁律不破：签完自检、失败不重签、302 头干净。

        【2026-11-03 修正】旧实现用 filepath_mapping 把 /CloudNAS/CloudDrive/115open/ →
        115open/ 之后又剥掉首段存储名，而 pan115.resolve_file 的契约是「首段为存储名
        自动跳过」——两边各剥一次，必然找不到文件。现改为：统一剥掉本地前缀后，
        保留存储名交给 resolve_file 处理（它自己会跳过首段）。
        """
        path = urllib.parse.unquote(raw_path or "").strip()
        if not path:
            return self._json({"error": "path required"}, 400)
        # 允许 strm 里写 /CloudNAS/CloudDrive/115open/... 或裸 115open/...
        path = path.replace("/CloudNAS/CloudDrive/", "").lstrip("/")
        if not path:
            return self._json({"error": "path required"}, 400)
        ip = self._client_ip()
        now = time.time()
        with _R115_LOCK:
            b = _R115_BUCKET.get(ip)
            if not b or now - b[0] > 60:
                if len(_R115_BUCKET) > 4096:      # 防字典无限涨
                    _R115_BUCKET.clear()
                _R115_BUCKET[ip] = [now, 1]
            else:
                b[1] += 1
                if b[1] > _R115_MAX:
                    return self._json({"error": "too many requests"}, 429)
        ua = self.headers.get("User-Agent") or ""
        ck = (config.load().get("pan115_cookie") or {})
        if not ck.get("UID"):
            return self._json({"error": "115 未登录，请先在面板扫码登录"}, 401)
        try:
            # 路径 → pickcode（带缓存）→ 签发（带缓存 + 自检）
            url = pan115_swap.swap_path_to_url(path, ua=ua, ck=ck)
        except Exception as e:
            reverse_proxy.log_event("error", player="evolink", item=path[:120],
                                    path=path[:160], ua=ua[:120], status=0,
                                    note=f"路径换链失败：{e}")
            return self._json({"error": str(e),
                                "hint": "115 risk control or login expired, retry later"}, 502)
        if not url:
            return self._json({"error": "签发失败"}, 502)
        reverse_proxy.log_event("stream", player="evolink", item=path[:120],
                                path=path[:160], ua=ua[:120], status=302,
                                target=path[:200], cdn=url[:200])
        self.send_response(302)
        self.send_header("Location", url)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def api_export(self, g):
        """导出缺失清单（只按当前分类 + 可选地区/年份/榜单/状态过滤）。

        preview=1 返回 JSON（页面里的清单预览）；否则返回 CSV：
        序号, 影片名, 年份, TMDB编号。
        """
        category = (g("category") or "").strip()
        status = (g("status") or "missing").strip()
        list_key = (g("list") or "").strip()
        year = (g("year") or "").strip()
        bucket = (g("bucket") or "").strip()
        regions = [x.strip() for x in (g("regions") or "").split(",") if x.strip()]
        preview = g("preview") == "1"
        limit = min(int(g("limit", 3000) or 3000), 20000)

        where, args = ["1=1"], []
        cw, ca = db.cat_scope(category)
        where.append(cw)
        args += ca
        if status and status != "all":
            where.append("match_status=?")
            args.append(status)
        if list_key:
            where.append("(','||COALESCE(lists,'')||',') LIKE ?")
            args.append(f"%,{list_key},%")
        if year:
            if year == "old":
                where.append("year BETWEEN 1900 AND 1979")
            elif year == "none":
                where.append("(year IS NULL OR year<1900 OR year>2027)")
            elif year.startswith("y") and year[1:].isdigit():
                where.append("year=?")
                args.append(int(year[1:]))
            elif year.endswith("s") and year[:-1].isdigit():
                y = int(year[:-1])
                where.append("year BETWEEN ? AND ?")
                args += [y, y + 9]
            elif year.isdigit():
                y = int(year)
                if len(year) == 4 and year.endswith("0"):     # 2000/1990/1980 = 十年
                    where.append("year BETWEEN ? AND ?")
                    args += [y, y + 9]
                else:
                    where.append("year=?")
                    args.append(y)
        if regions:
            where.append("(" + " OR ".join("(','||COALESCE(regions,'')||',') LIKE ?"
                                           for _ in regions) + ")")
            args += [f"%,{r},%" for r in regions]

        c = db.conn()
        sql = ("SELECT title, original_title, year, tmdb_id, category, match_status "
               "FROM catalog WHERE " + " AND ".join(where) +
               " ORDER BY year DESC, title LIMIT ?")
        rows = c.execute(sql, args + [limit]).fetchall()
        total = c.execute("SELECT COUNT(*) n FROM catalog WHERE " + " AND ".join(where),
                          args).fetchone()["n"]

        label = db.BUCKET_LABEL.get(bucket) or category or "全库"
        if preview:
            return self._json({
                "total": total, "shown": len(rows), "label": label,
                "items": [{"i": i + 1, "title": r["title"], "year": r["year"],
                           "tmdb_id": r["tmdb_id"] or "", "category": r["category"] or "",
                           "status": r["match_status"] or ""}
                          for i, r in enumerate(rows)],
            })

        buf = io.StringIO()
        wr = csv.writer(buf)
        wr.writerow(["序号", "影片名", "年份", "TMDB编号"])
        for i, r in enumerate(rows, 1):
            wr.writerow([i, r["title"], r["year"] or "", r["tmdb_id"] or ""])
        data = ("\ufeff" + buf.getvalue()).encode("utf-8")
        fname = f"{label}-{status}.csv"
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition",
                         "attachment; filename*=UTF-8''" + urllib.parse.quote(fname))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _dup_scope(self, category, bucket, lib, libs, by_code):
        cat = db.BUCKET_CAT.get(bucket) or (category if category and category != "all" else "")
        label = db.BUCKET_LABEL.get(bucket) or cat or (lib or "全库")
        return {"category": cat, "bucket": bucket, "label": label,
                "libs": list(libs), "by_code": bool(by_code), "lib": lib}

    def api_duplicates(self, g):
        """一键排重：同一部片在库里存在多个文件（高低码 / HDR·SDR / 多语言版本）。

        范围由 category / bucket 决定（分类页右上角入口）：
          bucket 有值 → 只扫这个三级分类勾选的 Emby 库
          bucket 为空 → 扫该二级分类下全部子类的库
          两者都空   → 全库（深链兜底）
        AV / 里番 自动按番号判定，不走标题归一化。
        """
        category = (g("category") or "").strip()
        bucket = (g("bucket") or "").strip()
        lib = (g("lib") or "").strip()          # 兼容老参数：单个库名
        limit = min(int(g("limit", 200)), 800)
        by_code = db.is_av_scope(category, bucket) or (g("mode") or "") == "av"
        scoped = bool(lib or bucket or (category and category != "all"))
        if lib:
            libs = (lib,)
        elif scoped:
            libs = db.dup_libs(category, bucket)
        else:
            libs = ()
        scope = self._dup_scope(category, bucket, lib, libs, by_code)
        if scoped and not libs:
            # 分类下没勾任何库：返回空范围，避免界面上出现全库数字
            return self._json({"summary": {"groups": 0, "extra_files": 0, "items": 0,
                                           "av_groups": 0, "by_library": []},
                               "groups": [], "scope": scope})

        groups = _dup_decorate(db.duplicates(libs=libs, limit=limit, by_code=by_code))
        return self._json({"summary": db.dup_summary(libs=libs, by_code=by_code),
                           "groups": groups, "scope": scope})

    def api_lists(self):
        """榜单目录 + 当前已入库计数。"""
        from core.sources import lists as lists_src
        c = db.conn()
        counts = {}
        for r in c.execute("SELECT lists ls FROM catalog WHERE COALESCE(lists,'')!=''"):
            for x in (r["ls"] or "").split(","):
                if x:
                    counts[x] = counts.get(x, 0) + 1
        groups = {}
        for d in lists_src.LIST_DEFS:
            groups.setdefault(d["group"], []).append({
                "key": d["key"], "name": d["name"], "category": d["category"],
                "count": counts.get(d["key"], 0), "top": int(d.get("top") or 0)})
        return self._json({"groups": [{"name": k, "items": v} for k, v in groups.items()],
                           "fetched_at": db.get_meta("lists_fetched_at")})

    # ---------- EMBY 设置：服务器 / 媒体库 ----------
    def _server_libs(self, s, ttl=600):
        """一台 Emby 服务器的库清单（带缓存）。返回 {name,version,libs,ok,error}。"""
        sid = s.get("id") or s.get("url") or ""
        hit = EMBY_LIBS_CACHE.get(sid)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        r = emby.probe(s.get("url"), s.get("api_key") or "")
        data = {"name": r.get("name") or "", "version": r.get("version") or "",
                "libs": r.get("libraries") or [], "ok": bool(r.get("auth")),
                "error": r.get("error") or ""}
        EMBY_LIBS_CACHE[sid] = (time.time(), data)
        return data

    def api_emby_servers_get(self):
        cfg = config.load()
        out = []
        for s in config.emby_servers(cfg):
            live = self._server_libs(s)
            out.append({
                "id": s["id"], "name": s.get("name") or live["name"] or "",
                "url": s["url"], "api_key": mask(s.get("api_key") or ""),
                "version": live["version"], "ok": live["ok"],
                "error": live["error"], "libs": live["libs"],
            })
        return {"servers": out, "max": config.MAX_EMBY_SERVERS}

    def api_emby_servers_save(self, body):
        servers = body.get("servers")
        if not isinstance(servers, list):
            return {"ok": False, "error": "servers 必须是数组"}
        if len(servers) > config.MAX_EMBY_SERVERS:
            return {"ok": False, "error": f"最多 {config.MAX_EMBY_SERVERS} 台"}
        old = {s["id"]: s for s in config.emby_servers()}
        merged = []
        for i, s in enumerate(servers):
            s = dict(s or {})
            k = str(s.get("api_key") or "")
            if "••••" in k:                  # 前端显示的是掩码，表示没改过
                s["api_key"] = (old.get(str(s.get("id") or "")) or {}).get("api_key", "")
            merged.append(s)
        keep = config.save_emby_servers(merged)
        EMBY_LIBS_CACHE.clear()
        clear_lib_caches()
        return {"ok": True, "servers": [dict(x, api_key=mask(x["api_key"])) for x in keep]}

    def api_emby_select_get(self):
        servers = []
        names = []
        for s in config.emby_servers():
            live = self._server_libs(s)
            servers.append({"id": s["id"], "name": s.get("name") or live["name"] or s["url"],
                            "url": s["url"], "ok": live["ok"], "error": live["error"],
                            "libs": live["libs"]})
            names += [l["name"] for l in live["libs"]]
        rows = db.sel_rows(names)
        rows["servers"] = servers
        rows["excluded"] = list(db.excluded_libs())
        return rows

    def api_emby_select_save(self, body):
        sel = body.get("sel")
        if not isinstance(sel, dict):
            return {"ok": False, "error": "sel 必须是对象"}
        cfg = config.load()
        # 存全量快照：每个桶都落盘，勾选状态从此是唯一真源。
        # 以前「跟缺省一致就不落盘」会让用户看到的是默认值、不是自己勾的，
        # 也导致 emby_sel 长期是空对象 —— 谁勾了什么都查不出来。
        base = db.sel_map()
        cur = {}
        for k in db.BUCKET_LABEL:
            v = sel.get(k, base.get(k) or [])
            cur[k] = [str(x).strip() for x in (v or []) if str(x).strip()]
        cfg["emby_sel"] = cur
        config.save(cfg)
        clear_lib_caches()
        return {"ok": True, "sel": db.sel_map(), "active": list(db.active_libs())}

    def api_region_plan(self, body):
        """按 TMDB 各地区实际条目总量校准配额权重。"""
        media = body.get("media") or "movie"
        total = int(body.get("total") or 400)
        extra = {}
        if body.get("with_genres"):
            extra["with_genres"] = str(body["with_genres"])
        try:
            totals = tmdbsrc.region_totals(media, extra)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        # 开方压缩：TMDB 里美国和欧洲的条目量碾压其他区（6282 : 250），
        # 直接等比会让大陆只剩个位数。开方后既保留体量差异，又不至于把小产区挤没。
        weights = {k: max(1, round(math.sqrt(v) * 10)) for k, v in totals.items()}
        quota = tmdbsrc.plan_quota(total, weights)
        return {"ok": True, "totals": totals, "weights": weights, "quota": quota}

    def api_save_auto(self, body):
        cfg = config.load()
        auto = dict(AUTO_DEFAULTS)
        auto.update(cfg.get("auto") or {})
        for k in AUTO_DEFAULTS:
            if k in body:
                auto[k] = body[k]
        cfg["auto"] = auto
        config.save(cfg)
        return {"ok": True, "auto": auto, "last_run": db.get_meta("auto_run_at")}

    def api_resources(self, g):
        """查某条目标在 RE0（经兜底检索通道）上的可用资源。"""
        cid = g("cid") or ""
        c = db.conn()
        if cid.startswith("o:"):          # 本地库条目（owned），按 av_code 查磁力
            r = c.execute("SELECT av_code,name FROM owned WHERE emby_id=?", (cid[2:],)).fetchone()
            d = {"category": "AV", "av_code": (r["av_code"] if r else ""),
                 "title": (r["name"] if r else "")}
        else:
            r = c.execute("SELECT * FROM catalog WHERE cid=?", (cid,)).fetchone()
            if not r:
                return self._json({"error": "not found"}, 404)
            d = dict(r)
        if d.get("category") in ("AV", "里番"):
            # AV/里番 不走兜底通道（那边没有这两类）。AV 按番号查公开 BT 索引站，
            # 里番没番号、退回标题主干查；都走代理，结构与兜底通道那套统一。
            hit = RES_CACHE.get(cid)
            if hit and time.time() - hit[0] < 600:
                return self._json(hit[1])
            try:
                res = avres.search(d.get("av_code") or "", d.get("title") or "")
            except Exception as e:
                res = {"kind": "magnet", "code": d.get("av_code") or "", "items": [],
                       "counts": {}, "message": f"{type(e).__name__}: {e}"}
            res["configured"] = True
            res["query"] = {"av_code": d.get("av_code"), "title": d.get("title")}
            RES_CACHE[cid] = (time.time(), res)
            if len(RES_CACHE) > 800:
                RES_CACHE.clear()
            return self._json(res)
        # 命中缓存（10 分钟内同一 cid 不重复请求，省得浪费限速额度）
        hit = RES_CACHE.get(cid)
        if hit and time.time() - hit[0] < 600:
            return self._json(hit[1])
        media = d.get("media") or ""
        if media not in ("tv", "movie"):
            try:
                media = json.loads(d.get("extra") or "{}").get("media") or ""
            except Exception:
                media = ""
        if media not in ("tv", "movie"):
            media = "tv" if d.get("category") in ("剧集", "综艺") else "movie"
        try:
            res = hdhub.Hub().search_resources(d.get("title"), media, d.get("tmdb_id"))
        except Exception as e:
            return self._json({"configured": False, "message": f"{type(e).__name__}: {e}",
                               "items": [], "telegram": 0})
        res["query"] = {"title": d.get("title"), "media_type": media, "tmdb_id": d.get("tmdb_id")}
        RES_CACHE[cid] = (time.time(), res)
        if len(RES_CACHE) > 800:
            RES_CACHE.clear()
        return self._json(res)




def mask(s):
    if not s:
        return ""
    return s[:4] + "••••" + s[-4:] if len(s) > 10 else "••••"


def _res_label(h):
    """把视频高度换算成大家熟悉的档位（4K 片的实际高度是 2076，直接显示没意义）。"""
    try:
        h = int(h or 0)
    except (TypeError, ValueError):
        return ""
    if h >= 1800:
        return "4K"
    if h >= 900:
        return "1080P"
    if h >= 650:
        return "720P"
    if h >= 400:
        return "480P"
    if h:
        return f"{h}P"
    return ""


def _dup_decorate(groups):
    """给重复分组补上展示信息：解析 medias JSON，生成「4K/1080P」「HDR」「x–y GB」标签。

    api_duplicates（分类页排重）和 task_dup_scan（比对面板按库扫重复）共用。
    """
    out = []
    for gp in groups:
        versions, heights, ranges = [], [], set()
        for it in gp["items"]:
            try:
                ms = json.loads(it.get("medias") or "[]")
            except Exception:
                ms = []
            it["medias"] = ms
            for m in ms:
                lab = _res_label(m.get("h"))
                if lab and lab not in heights:
                    heights.append(lab)
                if m.get("r"):
                    ranges.add(m["r"])
                if m.get("s"):
                    versions.append(m["s"])
        tags = []
        if len(heights) > 1:
            tags.append("/".join(heights))
        if len(ranges) > 1:
            tags.append("+".join(sorted(ranges)))
        if len(versions) > 1 and max(versions) - min(versions) >= 2:
            tags.append(f"{min(versions):.1f}–{max(versions):.1f}GB")
        gp["tags"] = tags
        gp["total_gb"] = round(sum(versions), 1) if versions else 0
        out.append(gp)
    return out


def db_health_worker(delay=60, every=12 * 3600):
    """后台做数据库体检 + 自愈坏索引。

    为什么需要：老库里可能残留**结构对不上的孤儿索引**（建在已变成生成列的
    列上），这种索引会让每一次写入都抛 `database disk image is malformed`，
    结果就是「抓到了但一条都进不去」。

    为什么放后台：integrity_check 在 27 万行的库上要 ~8 秒，不能挡住启动。

    修法：先 REINDEX（保住索引）→ 复检 → 还坏就 DROP INDEX（索引可重建）。
    """
    time.sleep(delay)
    first = True
    while True:
        try:
            rep = db.fix_broken_indexes()
            if rep.get("reindexed") or rep.get("dropped"):
                print(f"[db] 自愈：重建索引 {rep.get('reindexed')}，删除坏索引 {rep.get('dropped')}；"
                      f"复检结果「{rep.get('detail')}」", flush=True)
            elif not rep.get("ok"):
                print(f"[db] 体检发现异常：{rep.get('detail')} {rep.get('errors')}", flush=True)
            elif first:
                # 正常时只在第一轮打一行，之后静默（免得日志刷屏）
                print(f"[db] 体检完成：{rep.get('detail')}（无异常，坏索引自愈已就位）", flush=True)
            # 大量拉取/比对之后数据分布会变，刷新执行计划统计，
            # 否则 SQLite 会按旧统计选错计划（27 万行库上一个查询能差出 60 秒）。
            db.conn().execute("ANALYZE")
        except Exception as e:
            print(f"[db] 体检异常：{e}", flush=True)
        first = False
        time.sleep(every)


def strm_cron_loop():
    """strm 媒体库定时巡检：每 30 秒查一次，到点（cron）且启用的库自动同步。

    与 scheduler_loop 分开：strm 同步一跑可能几十秒到几分钟，
    独立线程避免阻塞其它定时任务；run_due_libs 内部保证单次只跑该跑的库。
    """
    while True:
        try:
            done = strm_sync.run_due_libs()
            for r in done:
                print(f"[strm] 定时同步 {r.get('name')}: {r.get('status')} "
                      f"written={r.get('written')} skipped={r.get('skipped')} "
                      f"errors={len(r.get('errors') or [])}", flush=True)
        except Exception as e:
            print(f"[strm] 定时巡检异常：{e}", flush=True)
        time.sleep(30)


def main():
    db.init()
    cfg = config.load()
    port = int(os.environ.get("PORT") or cfg.get("port") or 9191)
    # strm 媒体库建表 / 老库迁移（幂等；含 src_path 列的 ALTER 补齐）
    try:
        strm_lib.init()
    except Exception as e:
        print(f"[strm] 建表失败：{e}", flush=True)
    # 115 直链 / 路径缓存落盘（重启后直接命中已签的链与已解析的路径）
    try:
        pan115_downurl.init_disk_cache()
    except Exception as e:
        print(f"[115] 磁盘缓存初始化失败：{e}", flush=True)
    try:
        pan115.init_disk_cache()
        pan115.start_pc_worker()
    except Exception as e:
        print(f"[115] 路径缓存初始化失败：{e}", flush=True)
    threading.Thread(target=scheduler_loop, daemon=True).start()
    threading.Thread(target=strm_cron_loop, daemon=True).start()  # strm 媒体库定时巡检
    threading.Thread(target=resume_all, daemon=True).start()   # 重启后自动续跑未完成任务
    threading.Thread(target=db_health_worker, daemon=True).start()  # 后台体检：坏索引自愈
    # 反向代理换链：Jellyfin / Emby / Playable 各自在可配端口拦媒体请求做 302（主流做法）
    def _start_proxy(prefix, kind):
        if not cfg.get(prefix + "_enabled"):
            return
        if not cfg.get(prefix + "_api_key"):
            print(f"[proxy] {kind} 未配置 api_key，跳过", flush=True)
            return
        reverse_proxy.start_proxy(prefix, {
            "kind": kind,
            "base": cfg.get(prefix + "_base") or "",
            "api_key": cfg.get(prefix + "_api_key") or "",
            "user_id": cfg.get(prefix + "_user_id") or "",
            "db_path": cfg.get(prefix + "_db_path") or "",
            "local_prefix": cfg.get(prefix + "_local_prefix") or "",
            "remote_prefix": cfg.get(prefix + "_remote_prefix") or "",
            "public_base": cfg.get(prefix + "_public_base") or "",
        }, int(cfg.get(prefix + "_port") or 0))

    # RE0 直连：CloakBrowser 数据目录 + 下载代理（GitHub Releases 国内直连不通）
    try:
        re0browser.init(str(config.DATA), (cfg.get("re0_direct_proxy") or "").strip())
    except Exception as e:
        print(f"[RE0] re0browser 初始化失败：{e}", flush=True)

    try:
        _start_proxy("jellyfin", "jellyfin")
        _start_proxy("emby_proxy", "emby")
        # Playable（自研三位一体服务端，Jellyfin 12.x fork）：复用 jellyfin 通道（kind=jellyfin）
        _start_proxy("playable", "jellyfin")
        # Plex 反代（plex-demo 同款思路）：不需要 api_key（客户端自带 token，
        # part→strm 映射直读 Plex DB），启用 + 有端口就起
        if cfg.get("plex_enabled") and int(cfg.get("plex_port") or 0) > 0:
            plex_proxy.start_proxy("plex", plex_proxy._backend_from_cfg(cfg),
                                   int(cfg.get("plex_port")))
    except Exception as e:
        print(f"[proxy] 启动反代异常：{e}", flush=True)

    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"EvoLink 运行在 http://127.0.0.1:{port}")
    print(f"Emby: {cfg['emby_url']}   DB: {config.DB_PATH}")
    auto = dict(AUTO_DEFAULTS); auto.update(cfg.get("auto") or {})
    print(f"自动更新: {'开' if auto.get('enabled') else '关'} · 每 {auto.get('hours')} 小时")
    srv.serve_forever()


if __name__ == "__main__":
    main()
