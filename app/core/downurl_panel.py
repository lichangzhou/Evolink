"""evolink 115 换链面板后端（离线骨架，不碰 115 逻辑本身）。

部署位置：app/core/downurl_panel.py（与 pan115_downurl.py 同目录）。
server.py 只需 4 行接入（见 server_patch_snippet.py.txt）。

已对照 NAS 上活代码校准（2026-10-22）：
  - pan115_downurl._CACHE 是模块级内存缓存 dict；clear 函数是 cache_clear()。
  - DownurlError(RuntimeError) 只携带「字符串消息」，没有 .errno/.msg 属性；
    故 errno 用正则从消息里提取（取不到为 None），msg 用 str(e)。
  - config 用 core.config.load() 取最新字典（server.py 内亦如此）。
  - downurl_url(path, ck=None, force=False, ua="")：ua 是播放器的 User-Agent，
    115 的 k 签名绑定签发时用的 UA，客户端必须用同一个 UA 取链（见 pan115_downurl 说明）。

本模块只做：状态查询 / 单次手动测试（服务端 60s 限流 + 连续 3 次 errno 标记 cooling）
/ 清缓存。**绝不在导入时、也不在 status() 里发任何 115 请求。**
"""

import re
import time
import threading
from urllib.parse import urlparse

from core import config as _cfgmod
from core import pan115_downurl as p115

# ---- 模块 API 适配（已按活代码校准）----
def _cache_dict():
    """拿到 pan115_downurl 的内存缓存 dict；取不到返回空 dict。"""
    for name in ("_CACHE", "cache", "CACHE", "mem_cache"):
        obj = getattr(p115, name, None)
        if isinstance(obj, dict):
            return obj
    getter = getattr(p115, "cache_info", None)
    if callable(getter):
        try:
            info = getter()
            if isinstance(info, dict) and "__entries__" in info:
                return info["__entries__"]
        except Exception:
            pass
    return {}

_DOWNURL_ERR = getattr(p115, "DownurlError", Exception)


# ---- 测试限流 + cooling 跟踪（进程内内存，重启即清）----
_TEST_LOCK = threading.Lock()
_LAST_TEST_AT = 0.0
_TEST_INTERVAL = 60.0          # 单次手动测试的服务端限流
_CONSEC_ERR = 0
_COOLING = False
_COOLING_UNTIL = 0.0
_COOLING_WINDOW = 600          # cooling 持续 10 分钟

# 单次测试样本：交接手册登记的试播集（仅 path，不触发任何请求）
SAMPLE_PATH = ("115open/Video/TV SHOW/欧美剧/堡垒：戴安娜 (2024)/Season 1/"
               "堡垒：戴安娜 - S01E01 - 第 1 集.mkv")

# 最近一次结果（供 status 展示）
_LAST = {"ok": None, "errno": None, "msg": None, "at": 0, "host": None}


def _cookie_present():
    try:
        cfg = _cfgmod.load()
    except Exception:
        return False
    if not isinstance(cfg, dict):
        return False
    ck = cfg.get("pan115_cookie") or {}
    return bool(ck) and all(ck.get(k) for k in ("UID", "CID", "SEID", "KID"))


def _min_ttl():
    d = _cache_dict()
    now = time.time()
    ttls = [max(0, (v.get("expire_at") - now)) for v in d.values()
            if isinstance(v, dict) and "expire_at" in v]
    return int(min(ttls)) if ttls else 0


def status():
    """GET /api/downurl/status —— 只读，零 115 请求。"""
    return {
        "logged_in": _cookie_present(),
        "user": None,                       # chrome 接口不返回用户名
        "cache_n": len(_cache_dict()),
        "ttl_s": _min_ttl(),
        "last": dict(_LAST),
        "mode": "chrome-downurl",
        "cooling": bool(_COOLING and time.time() < _COOLING_UNTIL),
        "test_interval_s": int(_TEST_INTERVAL),
        "strm_mode": str((_cfgmod.load() or {}).get("strm_mode") or "auto").lower(),
    }


def test_once(ua="evolink-panel-test"):
    """POST /api/downurl/test —— 单次手动签发测试（服务端 60s 限流）。

    不只签名，还用同一个 UA 抓一段字节：115 的 k 签名绑定签发时用的 UA，
    只签不取的话，就算签出了链也不知道客户端能不能真的拿到。
    """
    global _LAST_TEST_AT, _CONSEC_ERR, _COOLING, _COOLING_UNTIL
    now = time.time()
    with _TEST_LOCK:
        if now - _LAST_TEST_AT < _TEST_INTERVAL:
            return {
                "ok": False, "errno": None, "msg": "操作过于频繁，请稍候",
                "retry_after": int(_TEST_INTERVAL - (now - _LAST_TEST_AT)),
            }
        _LAST_TEST_AT = now
        try:
            url = p115.downurl_url(SAMPLE_PATH, ua=ua)
            fetch_ok, fetch_msg = _probe_bytes(url, ua)
            _CONSEC_ERR = 0 if fetch_ok else _CONSEC_ERR + 1
            ok = fetch_ok
            msg = "ok" if ok else f"签名成功但取链失败：{fetch_msg}"
            _LAST.update(ok=ok, errno=0 if ok else -1, msg=msg, at=int(now),
                         host=urlparse(url).netloc)
            if not ok and _CONSEC_ERR >= 3:
                _COOLING = True
                _COOLING_UNTIL = now + _COOLING_WINDOW
            return {"ok": ok, "url": url, "errno": 0 if ok else -1, "msg": msg,
                    "fetch_ok": fetch_ok, "fetch_msg": fetch_msg,
                    "cooling": bool(_COOLING)}
        except _DOWNURL_ERR as e:
            msg = str(e)
            m = re.search(r"errno=(\d+)", msg)
            errno = int(m.group(1)) if m else None
            _CONSEC_ERR += 1
            _LAST.update(ok=False, errno=errno, msg=msg, at=int(now), host=None)
            if _CONSEC_ERR >= 3:
                _COOLING = True
                _COOLING_UNTIL = now + _COOLING_WINDOW
            return {"ok": False, "errno": errno, "msg": msg,
                    "cooling": bool(_COOLING), "retry_after": int(_TEST_INTERVAL)}


def _probe_bytes(url, ua, nbytes=2048):
    """用与签发相同的 UA 抓一段字节，验证直链真的可用。返回 (ok, msg)。"""
    import urllib.error
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": ua,
            "Range": f"bytes=0-{nbytes - 1}",
        })
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            if r.status == 206 and len(body) > 0:
                return True, f"206 {len(body)}B"
            return False, f"status={r.status} len={len(body)}"
    except urllib.error.HTTPError as e:
        try:
            detail = e.read()[:120].decode("utf-8", "ignore")
        except Exception:
            detail = ""
        return False, f"HTTP {e.code} {detail}"
    except Exception as e:
        return False, f"{type(e).__name__} {e}"


def clear_cache():
    """POST /api/downurl/cache/clear —— 清内存缓存。"""
    d = _cache_dict()
    n = len(d)
    try:
        d.clear()
    except Exception:
        clearer = getattr(p115, "cache_clear", None) or getattr(p115, "clear_cache", None)
        if callable(clearer):
            try:
                clearer()
            except Exception:
                pass
    return {"ok": True, "cleared": n}


# ---- server.py 接入入口（4 行）----
def handle_get(p):
    if p == "/api/downurl/status":
        return status()
    return None


def handle_post(p):
    if p == "/api/downurl/test":
        return test_once()
    if p == "/api/downurl/cache/clear":
        return clear_cache()
    return None
