# -*- coding: utf-8 -*-
"""115 路径型 302 换链（路径映射 + 双层缓存 + 干净 302 头的完整方案）。

【为什么要有这个模块】
EvoLink 原来只实现了「pickcode 型」换链（strm 里写 /api/redirect115/<pickcode>），
而现存 32 万个 strm 全部是 **路径型** 的：
    /CloudNAS/CloudDrive/115open/Video/Movie/xxx/xxx.mkv
这种 strm 走不到 pickcode 那条路 —— 必须由服务端在播放时
「路径 → 定位符 → 直链」逐级解析。

【实际换链链路（从运行日志逐行还原，非推测）】
    收到请求 GET /api/v1/reverse/emby/emby/videos/{itemId}/original.mp4
    → Emby ID: {x-emby-source 指定的实例}
    → 提取的参数: {DeviceId, MediaSourceId, PlaySessionId, api_key}
    → 转发请求到Emby服务器: http://emby/Items?Ids={itemId}&Fields=Path,MediaSources&Limit=1
    → 已缓存MediaSources响应: {itemId}
    → Emby源文件: /CloudNAS/CloudDrive/115open/Video/.../xxx.mp4
    → 缓存不存在或已过期,重新获取重定向URL
    → #重定向助手# 路径变化，使用云盘方式获取重定向链接，原始路径: /CloudNAS/...
    → #115助手# 【115open】 获取下载链接成功 path='/Video/.../xxx.mp4', url_len=383
    → 重定向到: https://cdnfhnfile.115cdn.net/.../xxx.mp4?t=...&u=...
    → 302（Location = 该 CDN 直链）

要点（都来自上面这段日志与同类实现的配置约定）：
  1. **本地挂载前缀 → 115 路径** 靠 filepath_mapping（例如写成
     `/CloudNAS/CloudDrive/115open/ => 115open/`）。映射后剩下的
     `115open/Video/...` 才是 115 侧路径；再剥掉首段存储名 `115open`
     得到助手日志里的 `path='/Video/.../xxx.mp4'`。
  2. **两层缓存**：
       - MediaSources 响应缓存（itemId → Emby 源文件路径），短 TTL
       - 重定向 URL 缓存（路径 → CDN 直链），TTL = url_cache_expire（默认 1800）
     日志里「使用缓存的MediaSources响应」和「使用缓存的重定向URL」是两种不同缓存。
  3. **use_original 回退**：重定向换链失败时，不报错，回退到「原始 URL」
     （同类实现里就叫 use_original，默认 true）。
     实测日志里就有「这不是播放请求,使用原始URL: http://.../videos/1/stream.mkv」。
  4. **302 头要干净**：只发 Location，不发 Cache-Control（否则播放器缓存旧
     Location，一快进就断）。Range 由播放器自己带，115 直链原生支持。
  5. **UA 不绑死**：这类实现的 UA 是透传播放器自己的 UA（日志里
     user-agent: libmpv / Yamby/2.1.0.11(Android 都出现过）。
     115 的 k 签名绑定「签发时用的 UA」，所以换链时必须带上播放器当前 UA，
     且缓存要按 (路径, UA) 分开存 —— 这也是 pan115_downurl 现有的做法。

【风控铁律（沿用既有交接文档 §3.2）】
  - on-demand：只有播放请求才换链，禁止后台预签发 / 批量扫描换链。
  - 缓存命中直接返回，绝不重复签发。
  - 失败不重试：换链失败直接抛，由上层按 use_original 回退，绝不循环轰炸 115。
"""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config
from . import pan115
from . import pan115_downurl

# 115 千万别走代理（与 pan115 一致，避免出口差异触发风控）
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 重定向 URL 缓存 TTL（秒）。url_cache_expire 默认 1800。
URL_CACHE_TTL = 1800
# MediaSources 响应缓存 TTL（秒）。用短缓存，避免每次媒体请求都打播放器 API。
MEDIA_CACHE_TTL = 300

# 路径 → (ts, url)
_URL_CACHE = {}
_URL_CACHE_LOCK = threading.Lock()
# itemId → (ts, [mediaSource 路径列表])
_MEDIA_CACHE = {}
_MEDIA_CACHE_LOCK = threading.Lock()


class SwapError(RuntimeError):
    """换链失败。调用方应按 use_original 回退，不要重试。"""


# ---------------- 本地前缀 → 115 路径 的映射 ----------------
def parse_mapping(mapping):
    """解析 filepath_mapping 风格的映射：`本地前缀 => 115前缀`。

    支持多行，每行一条。返回 [(local, remote), ...]。
    映射是「前缀替换」，左边最长优先匹配。
    """
    out = []
    for raw in str(mapping or "").splitlines():
        line = raw.strip()
        if not line or "=>" not in line:
            continue
        left, right = line.split("=>", 1)
        left, right = left.strip(), right.strip()
        if not left:
            continue
        out.append((left, right))
    # 长前缀优先，避免 /a/ 先于 /a/b/ 命中
    out.sort(key=lambda kv: -len(kv[0]))
    return out


def apply_mapping(path, mapping):
    """把本地挂载路径按 filepath_mapping 转成 115 侧路径。

    `/CloudNAS/CloudDrive/115open/Video/x.mkv` + `/CloudNAS/CloudDrive/115open/ => 115open/`
    → `115open/Video/x.mkv`

    【2026-11-03 修正】原实现在映射后**又剥掉首段存储名**，得到 `Video/x.mkv`。
    但 115 账号的真实根目录（cid=0）下直接就是 `Video/Photo/Music/...`，
    **根本不存在 `115open` 这一层**（`115open` 只是 CloudDrive2 的本地挂载别名）。
    而 pan115.resolve_file 的契约是「首段为存储名，自动跳过」——
    于是两边各剥一次 → `Movie/...` → 必然「未找到」。
    实测对照：
        resolve_file("115open/Video/Movie/...mkv") → OK (pc=bidcx2u4k2knx8lvu)
        resolve_file("Video/Movie/...mkv")           → 未找到: Movie (in cid=0)
    故这里**保留存储名不动**，交给 resolve_file 统一处理。
    """
    p = (path or "").strip()
    if not p:
        return ""
    for local, remote in parse_mapping(mapping):
        if p.startswith(local):
            p = remote.rstrip("/") + "/" + p[len(local):].lstrip("/")
            break
    return p.lstrip("/")


# ---------------- 重定向 URL 缓存 ----------------
def url_cache_get(key):
    now = time.time()
    with _URL_CACHE_LOCK:
        hit = _URL_CACHE.get(key)
        if hit and now - hit[0] < URL_CACHE_TTL:
            return hit[1]
        if hit:
            _URL_CACHE.pop(key, None)
    return None


def url_cache_put(key, url):
    with _URL_CACHE_LOCK:
        if len(_URL_CACHE) > 20000:      # 防字典无限涨
            _URL_CACHE.clear()
        _URL_CACHE[key] = (time.time(), url)


def url_cache_clear():
    with _URL_CACHE_LOCK:
        _URL_CACHE.clear()


def url_cache_size():
    with _URL_CACHE_LOCK:
        return len(_URL_CACHE)


# ---------------- MediaSources 缓存 ----------------
def media_cache_get(item_id):
    now = time.time()
    with _MEDIA_CACHE_LOCK:
        hit = _MEDIA_CACHE.get(str(item_id))
        if hit and now - hit[0] < MEDIA_CACHE_TTL:
            return list(hit[1])
        if hit:
            _MEDIA_CACHE.pop(str(item_id), None)
    return None


def media_cache_put(item_id, paths):
    with _MEDIA_CACHE_LOCK:
        if len(_MEDIA_CACHE) > 20000:
            _MEDIA_CACHE.clear()
        _MEDIA_CACHE[str(item_id)] = (time.time(), list(paths))


# ---------------- 核心：路径 → 直链 ----------------
def swap_path_to_url(path, ua="", ck=None, mapping="", force=False):
    """115 路径 → 可播 CDN 直链。

    path：115 侧路径，形如 `Video/Movie/xxx/xxx.mkv`（已剥掉存储名）。
    ua  ：播放器的 User-Agent，原样透传（115 的 k 签名绑定签发时的 UA）。
    失败抛 SwapError，**不重试**。

    流程与常见实现一致：
      1. 查重定向 URL 缓存（按 (路径, UA)），命中直接返回，绝不重复签发。
      2. 未命中 → 解析路径拿 pickcode → 调 115 签发 → 写缓存 → 返回。
    """
    path = (path or "").strip().lstrip("/")
    if not path:
        raise SwapError("115 路径为空")

    ck_key = (path, ua or "")
    if not force:
        hit = url_cache_get(ck_key)
        if hit:
            return hit

    try:
        url = pan115_downurl.downurl_url(path=path, ck=ck, ua=ua)
    except pan115_downurl.DownurlError as e:
        raise SwapError(str(e)) from e
    except Exception as e:
        raise SwapError(f"{type(e).__name__}: {e}") from e

    if not url:
        raise SwapError("115 未返回直链")
    url_cache_put(ck_key, url)
    return url


def swap_strm_line(strm_line, ua="", ck=None, mapping="", force=False):
    """strm 第一行 → CDN 直链（路径型专用）。

    这就是「路径→定位符」与「定位符→直链」两步合起来的等价物。
    """
    p = (strm_line or "").strip().lstrip("﻿").strip()
    if not p:
        raise SwapError("strm 第一行为空")
    rel = apply_mapping(p, mapping)
    if not rel:
        raise SwapError(f"无法从 strm 解析 115 路径：{p[:120]}")
    return swap_path_to_url(rel, ua=ua, ck=ck, force=force)


def swap_by_item(item_id, emby_base, emby_key, ua="", ck=None, mapping="",
                 field="Path,MediaSources", limit=1):
    """Emby itemId → CDN 直链（还原 /reverse/emby 链路）。

    步骤：
      1. 用 MediaSources 缓存（短 TTL），miss 才打 Emby API。
      2. 取 Emby 源文件路径（MediaSources[].Path 优先，退回 item.Path）。
      3. 剥本地前缀 → 115 路径。
      4. 换链（带重定向 URL 缓存）。
    """
    item_id = str(item_id or "").strip()
    if not item_id:
        raise SwapError("缺少 item id")

    paths = media_cache_get(item_id)
    if not paths:
        q = urllib.parse.urlencode({
            "Ids": item_id, "Fields": field, "Limit": str(limit),
            "api_key": emby_key or "",
        })
        url = f"{str(emby_base).rstrip('/')}/Items?{q}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with _NO_PROXY.open(req, timeout=25) as r:
                d = json.loads(r.read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as e:
            raise SwapError(f"Emby API HTTP {e.code}") from e
        except Exception as e:
            raise SwapError(f"Emby API 失败：{type(e).__name__} {e}") from e
        items = d.get("Items") or []
        if not items:
            raise SwapError(f"Emby 未找到 item {item_id}")
        it = items[0]
        paths = []
        for ms in (it.get("MediaSources") or []):
            if ms.get("Path"):
                paths.append(ms["Path"])
        if not paths and it.get("Path"):
            paths.append(it["Path"])
        if not paths:
            raise SwapError(f"Emby item {item_id} 没有 MediaSources.Path")
        media_cache_put(item_id, paths)

    src = paths[0]
    rel = apply_mapping(src, mapping)
    if not rel:
        raise SwapError(f"无法从 Emby 源文件解析 115 路径：{src[:120]}")
    return swap_path_to_url(rel, ua=ua, ck=ck)
