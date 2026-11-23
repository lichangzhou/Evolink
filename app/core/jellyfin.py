"""Jellyfin / Playable 原生只读客户端（自研三位一体服务端基于 Jellyfin 12.x fork）。

与 core/emby.py 的差异：
  - API 路径不带 /emby 前缀（Jellyfin 原生 /Items、/Users/{id}/Views、/Library/VirtualFolders）。
  - 鉴权必须用 Authorization: MediaBrowser Token="KEY"（带引号）；Jellyfin/Playable 拒 api_key 查询参数。
  - 同一套客户端可用于 Jellyfin 本体与 Playable，仅 base / api_key / user_id 不同。
仅用标准库。
"""
import json
import urllib.error
import urllib.parse
import urllib.request

ITEM_FIELDS = ("ProviderIds,ProductionYear,PremiereDate,OriginalTitle,ImageTags,CommunityRating,"
               "Path,OfficialRating,MediaSources,MediaStreams")

# 这些 CollectionType 不是媒体库，跳过
SKIP_COLLECTION = ("livetv", "playlists", "boxsets", "music", "photos", "books", "folders")


class JellyfinError(RuntimeError):
    pass


def _headers(key, user_id=""):
    h = {
        "Accept": "application/json",
        "Authorization": 'MediaBrowser Token="%s"' % key,
    }
    if user_id:
        h["UserId"] = user_id
    return h


def _req(base, path, params=None, key="", user_id="", timeout=60):
    base = (base or "").strip().rstrip("/")
    p = dict(params or {})
    if user_id:
        p.setdefault("userId", user_id)
    url = "%s%s?%s" % (base, path, urllib.parse.urlencode(p))
    req = urllib.request.Request(url, headers=_headers(key, user_id))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise JellyfinError("%s HTTP %s: %s" % (path, e.code, e.read().decode("utf-8", "replace")[:200]))
    except Exception as e:
        raise JellyfinError("%s 请求失败: %s" % (path, e))


def resolve_user(base, key, prefer="", timeout=20):
    """拿管理员用户 ID（索引需要 UserId 作用域）。优先用 prefer，否则挑第一个管理员。"""
    try:
        users = _req(base, "/Users", {}, key, "", timeout) or []
    except Exception:
        return prefer
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


def libraries(base, key, user_id="", timeout=30):
    """返回 [{id, name, type}]。优先 /Users/{uid}/Views（带用户作用域）；无 uid 时先解析管理员再查。"""
    if not user_id:
        user_id = resolve_user(base, key, "", timeout)
    if not user_id:
        return []
    data = _req(base, "/Users/%s/Views" % user_id, {}, key, user_id, timeout)
    out = []
    for it in (data or {}).get("Items", []):
        t = (it.get("CollectionType") or "").lower()
        if t in SKIP_COLLECTION:
            continue
        out.append({"id": it.get("Id"), "name": it.get("Name") or "", "type": t})
    return out


def iter_items(base, key, user_id, parent_id, page=500, item_types="Movie,Series", timeout=60):
    """按页迭代某库下全部 Movie/Series 条目（Jellyfin 原生 Items 接口，递归）。"""
    start = 0
    while True:
        data = _req(base, "/Items", {
            "ParentId": parent_id,
            "Recursive": "true",
            "IncludeItemTypes": item_types,
            "Fields": ITEM_FIELDS,
            "Limit": page,
            "StartIndex": start,
            "EnableImages": "true",
        }, key, user_id, timeout)
        items = data.get("Items", [])
        total = data.get("TotalRecordCount", 0)
        for it in items:
            yield it
        start += len(items)
        if not items or start >= total:
            break


def media_sources(item):
    """同 emby.media_sources：把版本清单压成紧凑结构，供排重用。"""
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


def probe(base, api_key="", timeout=20):
    """试连：返回服务名、版本、库清单。设置页用它填名称 + 校验 Key。"""
    base = (base or "").strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = "http://" + base
    out = {"ok": False, "url": base, "name": "", "version": "", "auth": False,
           "user_id": "", "libraries": [], "error": ""}
    if base in ("http://", "https://"):
        out["error"] = "地址为空"
        return out
    try:
        info = _req(base, "/System/Info/Public", {}, "", "", timeout)
        out["name"] = info.get("ServerName") or ""
        out["version"] = info.get("Version") or ""
    except Exception as e:
        out["error"] = "连不上服务：%s" % e
        return out
    key = (api_key or "").strip()
    if not key:
        out["ok"] = True
        out["error"] = "还没填 API Key"
        return out
    try:
        uid = resolve_user(base, key, "", timeout)
        if not uid:
            out["error"] = "服务器上没有可用管理员用户"
            return out
        out["user_id"] = uid
        out["auth"] = True
        out["libraries"] = libraries(base, key, uid, timeout)
        out["ok"] = True
    except Exception as e:
        out["error"] = "读库失败：%s" % e
    return out
