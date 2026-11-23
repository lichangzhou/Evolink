"""Emby 只读客户端：拉库列表、拉全量条目。只用标准库。

多服务器：配置里 emby_servers 最多 3 台，取数默认走第一台（config.emby_primary）。
probe() 可以拿任意一对「地址 + Key」试连，不读也不改全局配置。
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

ITEM_FIELDS = ("ProviderIds,ProductionYear,PremiereDate,OriginalTitle,ImageTags,CommunityRating,"
               "Path,OfficialRating,MediaSources,MediaStreams")

# Views 里这几类不是媒体库，跳过
SKIP_VIEW_TYPES = ("boxsets", "livetv", "playlists")


class EmbyError(RuntimeError):
    pass


def _req(base, path, params=None, key="", timeout=60):
    base = (base or "").strip().rstrip("/")
    p = dict(params or {})
    if key:
        p["api_key"] = key
    url = f"{base}/emby{path}?{urllib.parse.urlencode(p)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(path, params=None, timeout=60, server=None):
    s = server or config.emby_primary()
    try:
        return _req(s.get("url"), path, params, s.get("api_key") or "", timeout)
    except Exception as e:
        raise EmbyError(f"{path} 请求失败: {e}")


def _call(method, path, params=None, timeout=30, server=None, body=None):
    """发一次任意方法请求，返回 (status, text)。连接层错误直接抛 EmbyError。

    删除类接口用：Emby 对不存在的条目也返 204，光看状态码分不清真假，
    必须配合 item_exists() 复核。
    """
    s = server or config.emby_primary()
    base = (s.get("url") or "").strip().rstrip("/")
    key = s.get("api_key") or ""
    if not base:
        raise EmbyError("Emby 服务器地址未配置")
    p = dict(params or {})
    if key:
        p["api_key"] = key
    url = f"{base}/emby{path}?{urllib.parse.urlencode(p)}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, method=method, data=data, headers={
        "Accept": "application/json", "X-Emby-Token": key,
        "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception:
            return e.code, ""
    except Exception as e:
        raise EmbyError(f"{method} {path} 失败: {e}")


_UID_CACHE = {"ts": 0.0, "uid": ""}


def admin_user_id(server=None, ttl=600):
    """管理员用户 ID（删除条目后复核要用），成功一次缓存 10 分钟。"""
    if _UID_CACHE["uid"] and time.time() - _UID_CACHE["ts"] < ttl:
        return _UID_CACHE["uid"]
    users = _get("/Users", server=server) or []
    uid = pick_user(users, config.load().get("emby_user_id") or "")
    if uid:
        _UID_CACHE.update(ts=time.time(), uid=uid)
    return uid


def delete_item(item_id, server=None, timeout=20):
    """从 Emby 媒体库删除条目（只删库内条目，不碰磁盘上的 strm 文件）。

    注意：Emby 对不存在的 ID 同样返回 204，所以删完必须由调用方复核
    （item_exists() 返 False 才算真删掉）。
    """
    return _call("POST", f"/Items/{int(item_id)}/Delete", timeout=timeout, server=server)


def item_exists(item_id, user_id, server=None, timeout=15):
    """条目是否还在库里（删除后复核用）。连接失败会抛 EmbyError。"""
    st, _ = _call("GET", f"/Users/{user_id}/Items/{item_id}", timeout=timeout, server=server)
    return st == 200


def libraries_of(views):
    """把 Views 结果筛成媒体库清单（去掉合集 / 电视直播 / 播放列表）。"""
    out = []
    for it in (views or {}).get("Items", []):
        t = it.get("CollectionType") or ""
        if t in SKIP_VIEW_TYPES:
            continue
        out.append({"id": it.get("Id"), "name": it.get("Name") or "", "type": t})
    return out


def probe(url, api_key="", timeout=20):
    """按给定的地址 + Key 试连：返回服务器名、版本、库清单。

    设置页「EMBY 服务器」用它自动填名称，也用来校验 Key 对不对。
    """
    base = (url or "").strip().rstrip("/")
    out = {"ok": False, "url": base, "name": "", "version": "", "auth": False,
           "user_id": "", "libraries": [], "error": ""}
    if not base:
        out["error"] = "地址为空"
        return out
    if not base.startswith(("http://", "https://")):
        base = "http://" + base
        out["url"] = base
    try:
        info = _req(base, "/System/Info/Public", {}, "", timeout)
        out["name"] = info.get("ServerName") or ""
        out["version"] = info.get("Version") or ""
    except Exception as e:
        out["error"] = f"连不上服务：{e}"
        return out

    key = (api_key or "").strip()
    if not key:
        out["ok"] = True                    # 服务通，只是还没给钥匙
        out["error"] = "还没填 API Key"
        return out
    try:
        users = _req(base, "/Users", {}, key, timeout) or []
    except Exception as e:
        out["error"] = f"API Key 无效：{e}"
        return out

    uid = pick_user(users)
    if not uid:
        out["error"] = "服务器上没有可用用户"
        return out

    out["user_id"] = uid
    out["auth"] = True
    try:
        views = _req(base, f"/Users/{uid}/Views", {}, key, timeout)
        out["libraries"] = libraries_of(views)
        out["ok"] = True
    except Exception as e:
        out["error"] = f"读库失败：{e}"
    return out


def pick_user(users, prefer=""):
    """挑一个用来查 Views 的用户。

    必须是管理员：普通账号只能看到自己被授权的库，会导致「只显示一个媒体库」。
    prefer（配置里存的 user_id）只有在它本身就是管理员、或服务器上压根没有管理员时才用。
    """
    users = users or []
    admins = [u for u in users
              if (u.get("Policy") or {}).get("IsAdministrator")
              and not (u.get("Policy") or {}).get("IsDisabled")]
    if prefer:
        for u in users:
            if u.get("Id") == prefer:
                if (u.get("Policy") or {}).get("IsAdministrator") or not admins:
                    return prefer
                break
    if admins:
        return admins[0].get("Id") or ""
    for u in users:
        if not (u.get("Policy") or {}).get("IsDisabled"):
            return u.get("Id") or ""
    return users[0].get("Id") if users else ""


def ping():
    return _get("/System/Info")


def default_user():
    """取管理员用户，用于 Views 查询。"""
    cfg = config.load()
    users = _get("/Users")
    uid = pick_user(users, cfg.get("emby_user_id") or "")
    if uid:
        return uid
    return (users[0]["Id"] if users else "")


def libraries():
    uid = default_user()
    data = _get(f"/Users/{uid}/Views")
    return libraries_of(data)


def iter_items(parent_id, page=500, item_types="Movie,Series,Video", server=None):
    """按页迭代某个库下的全部条目。"""
    start = 0
    while True:
        data = _get("/Items", {
            "ParentId": parent_id,
            "Recursive": "true",
            "IncludeItemTypes": item_types,
            "Fields": ITEM_FIELDS,
            "Limit": page,
            "StartIndex": start,
            "EnableImages": "true",
        }, server=server)
        items = data.get("Items", [])
        total = data.get("TotalRecordCount", 0)
        for it in items:
            yield it
        start += len(items)
        if not items or start >= total:
            break


def image_url(item_id, tag, kind="Primary", max_w=320):
    """Emby 4.9+ 不再支持 /Images/{kind}/{maxWidth} 路径式缩放（500），改用 maxWidth 参数。"""
    s = config.emby_primary()
    return (f"{(s.get('url') or '').rstrip('/')}/emby/Items/{item_id}/Images/{kind}"
            f"?maxWidth={max_w}&tag={tag}&api_key={s.get('api_key') or ''}")


def media_sources(item):
    """把一条 Emby 条目的版本清单压成紧凑结构，供排重用。

    返回 [{"h":2160,"b":52000000,"r":"HDR10","s":45.2,"c":"hevc","f":"文件名"}]，
    多版本条目（同一 item 挂多个文件）与多条目（同 tmdb_id 拆成两个 item）都能覆盖。
    """
    out = []
    for ms in (item.get("MediaSources") or []):
        h = b = 0
        rng = ""
        codec = ""
        for st in (ms.get("MediaStreams") or []):
            if st.get("Type") != "Video":
                continue
            h = st.get("Height") or st.get("Width") or 0
            b = st.get("BitRate") or 0
            rng = st.get("VideoRange") or ""
            codec = st.get("Codec") or ""
            break
        size = ms.get("Size") or 0
        out.append({
            "h": int(h or 0),
            "b": int(b or ms.get("Bitrate") or 0),
            "r": rng,
            "s": round(size / 1e9, 2) if size else 0,
            "c": codec,
            "f": (ms.get("Name") or "")[:160],
        })
    return out
