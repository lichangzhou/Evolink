#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EvoLink 授权判定（免费版 / 专业版，一次性买断 68 元）。

设计前提（与《EvoLink 授权分层方案定稿》一致）：
  1. 未配置激活码 = 免费版（功能受限），绝不退化成「全功能免费」。
  2. 服务端强制判定：前端的按钮灰化、抽屉展示都只是提示，不是安全边界。
  3. 每个请求走 `path_allowed()`：免费白名单放行，其余路由需要专业版。
  4. 校验失败 / 超时 / 模块异常 -> 降级免费版；只有最近一次成功且仍在
     30 天离线宽限内，才允许断网期间保持专业版。
  5. 302 换链端点不在免费白名单里，避免手工构造 strm 绕过。

纯标准库，零第三方依赖。本模块不写 config.json，也不直接碰数据库。
"""
import hashlib
import hmac
import json
import os
import socket
import threading
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent          # app/
DATA = BASE / "data"
CACHE_FILE = DATA / ".license_cache.json"
DEVICE_FILE = DATA / ".device_id"

DEFAULT_SERVER = "http://182.255.84.109:9090"
PRICE_CNY = 68
PLAN_RANK = {"free": 0, "pro": 1}
CACHE_TTL = 300                     # 在线校验结果内存缓存（秒）
OFFLINE_GRACE = 30 * 86400         # 离线宽限 30 天
VERIFY_TIMEOUT = 3.0
_SALT = "evolink-license-cache-v1"

# --------------------------------------------------------------------------- #
# 免费白名单：精确路径
#   - 定稿 §2 全量：会话/基础、添加媒体库、四源索引、添加网盘、对比/查看
#   - 实现级补充（见交付文档「对定稿的边界补充」）：
#       /api/stats   容器 HEALTHCHECK 命中，锁掉会导致容器永远 unhealthy
#       /api/catalog 媒体库主列表的只读接口，免费版「查看结果」必须可用
#       /api/thumb、/api/poster  纯图片代理，锁掉会让免费版整页破图
# --------------------------------------------------------------------------- #
FREE_EXACT = frozenset({
    # 会话 / 基础
    "/api/login", "/api/logout", "/api/settings", "/api/settings/auth",
    "/api/version", "/api/progress", "/api/stats", "/api/dash",
    "/api/fetch-state",
    # 添加媒体库
    "/api/emby/servers", "/api/emby/libs", "/api/emby/select",
    "/api/emby/probe", "/api/emby/map",
    "/api/plex/auth-start", "/api/plex/auth-poll", "/api/plex/libraries",
    "/api/libraries", "/api/lib-list", "/api/lib-prog",
    # 四源索引
    "/api/sync-emby", "/api/sync-plex", "/api/sync-jellyfin",
    "/api/sync-playable", "/api/owned/items", "/api/owned/sources",
    # 对比 / 查看
    "/api/compare", "/api/match", "/api/duplicates", "/api/dup-scan",
    "/api/dup-result", "/api/local/items", "/api/catalog",
    "/api/thumb", "/api/poster",
})

# 免费白名单：前缀
#   /api/115/     网盘登录、目录、单次扫描、纯读比对（定稿 §2）
#   /api/license/ 许可证状态/激活/解绑 —— 必须免费，否则免费版无法激活
FREE_PREFIX = ("/api/115/", "/api/license/")

# 页面加载时的静默轮询：402 时不弹提示（前端 EvoLic 使用）
SILENT_PATHS = frozenset({
    "/api/downurl/status", "/api/progress", "/api/fetch-state",
    "/api/version", "/api/stats",
})

_lock = threading.Lock()
_mem = {"t": 0.0, "key": "", "result": None, "offline": False}


# --------------------------------------------------------------------------- #
# 设备指纹 / 离线缓存
# --------------------------------------------------------------------------- #
def device_id():
    """HOSTNAME + 数据目录 做 SHA256；首次生成后落盘，重启不变。"""
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        if DEVICE_FILE.exists():
            v = DEVICE_FILE.read_text("utf-8").strip()
            if v:
                return v
        raw = "%s|%s" % (socket.gethostname(), str(DATA.resolve()))
        v = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        DEVICE_FILE.write_text(v, "utf-8")
        return v
    except Exception:
        return hashlib.sha256(("evolink|" + socket.gethostname()).encode()).hexdigest()


def _mac(payload):
    secret = hashlib.sha256((_SALT + "|" + device_id()).encode()).digest()
    return hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _save_cache(result, key):
    """最近一次成功校验：带 HMAC 签名，防手工改 JSON 延长宽限。

    缓存必须绑定激活码：否则用户换上一个无效 key 后断网，会误用旧 key 的
    成功缓存继续按专业版放行。说明：这是本地完整性校验，不是加密，也无法
    对抗反编译修改客户端。真正的边界是服务端；离线宽限本质是给买断用户的
    断网容错。
    """
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        body = dict(result or {})
        body["_ts"] = int(time.time())
        body["_key"] = (key or "").strip().upper()
        payload = json.dumps(body, sort_keys=True, ensure_ascii=False)
        CACHE_FILE.write_text(json.dumps({"p": payload, "s": _mac(payload)},
                                         ensure_ascii=False), "utf-8")
        try:
            os.chmod(CACHE_FILE, 0o600)
        except OSError:
            pass
    except Exception:
        pass


def _load_cache(key):
    try:
        obj = json.loads(CACHE_FILE.read_text("utf-8"))
        payload, sig = obj.get("p"), obj.get("s")
        if not payload or not sig or not hmac.compare_digest(sig, _mac(payload)):
            return None
        body = json.loads(payload)
        if not isinstance(body, dict) or not body.get("valid"):
            return None
        if not hmac.compare_digest(str(body.get("_key") or ""),
                                   (key or "").strip().upper()):
            return None
        age = time.time() - int(body.get("_ts") or 0)
        if age < 0 or age > OFFLINE_GRACE:
            return None
        return body
    except Exception:
        return None


def clear_cache():
    try:
        if CACHE_FILE.exists():
            CACHE_FILE.unlink()
    except OSError:
        pass
    with _lock:
        _mem.update({"t": 0.0, "key": "", "result": None, "offline": False})


def _cfg_license(cfg):
    v = (cfg or {}).get("license")
    return v if isinstance(v, dict) else {}


def license_server(cfg):
    """License 验证服务地址，三级优先：

    1. 设置页里显式填的 ``license.server``（最高优先级）
    2. 环境变量 ``EVOLINK_LICENSE_SERVER``（自建验证服务时用）
    3. 内置默认值

    环境变量的存在是为了让社区用户把验证指向自己的服务，而不必改源码。
    """
    explicit = str(_cfg_license(cfg).get("server") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    return (os.environ.get("EVOLINK_LICENSE_SERVER") or DEFAULT_SERVER).rstrip("/")


def license_key(cfg):
    return (_cfg_license(cfg).get("key") or "").strip().upper()


# --------------------------------------------------------------------------- #
# 在线校验
# --------------------------------------------------------------------------- #
def _verify_online(cfg, key):
    url = license_server(cfg) + "/api/license/verify"
    body = json.dumps({"key": key, "device_id": device_id()}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=VERIFY_TIMEOUT) as r:
        raw = r.read(64 * 1024).decode("utf-8", "replace")
    obj = json.loads(raw)
    return obj if isinstance(obj, dict) else {}


def verify(cfg, key=None, force=False):
    """返回服务端校验结果 dict（已归一化为 ``valid`` / ``pro`` / ``offline`` 等）。

    ``valid`` 为 True 且 ``plan`` 达到 pro 且未触顶设备数时才算专业版。
    """
    key = (key if key is not None else license_key(cfg)).strip().upper()
    now = time.time()
    if not key:
        return {"valid": False, "pro": False, "plan": "free",
                "message": "未激活，当前为免费版"}

    if not force:
        with _lock:
            if (_mem["key"] == key and _mem["result"] is not None
                    and now - _mem["t"] < CACHE_TTL):
                return dict(_mem["result"])

    try:
        res = _verify_online(cfg, key)
        # 服务端在设备触顶时仍返回 valid=True，但带 device_limit，必须降级
        ok = bool(res.get("ok", True)) and bool(res.get("valid")) \
            and not bool(res.get("device_limit")) \
            and PLAN_RANK.get(str(res.get("plan") or "").lower(), 0) >= PLAN_RANK["pro"]
        out = {
            "valid": ok, "pro": ok, "offline": False,
            "plan": "pro" if ok else (res.get("plan") or "free"),
            "message": res.get("message") or ("校验通过" if ok else "许可证无效"),
            "device_limit": bool(res.get("device_limit")),
            "devices_used": res.get("devices_used"),
            "max_devices": res.get("max_devices"),
            "expires_at": res.get("expires_at"),
            # 买断服务端显式返回 permanent=true；同时兼容只回 expires_at=0 的旧版本
            "permanent": bool(res.get("permanent"))
                         or res.get("expires_at") in (0, "0", None, ""),
        }
        if ok:
            _save_cache(out, key)
        with _lock:
            _mem.update({"t": now, "key": key, "result": out, "offline": False})
        return dict(out)
    except Exception as e:
        cached = _load_cache(key)
        if cached:
            out = {
                "valid": True, "pro": True, "offline": True, "plan": "pro",
                "message": "离线宽限期内（最近一次校验成功）",
                "device_limit": False,
                "devices_used": cached.get("devices_used"),
                "max_devices": cached.get("max_devices"),
                "expires_at": cached.get("expires_at"),
                "permanent": bool(cached.get("permanent")),
            }
            with _lock:
                _mem.update({"t": now, "key": key, "result": out, "offline": True})
            return dict(out)
        out = {"valid": False, "pro": False, "offline": False, "plan": "free",
               "message": "无法连接授权服务器：%s" % e}
        with _lock:
            _mem.update({"t": now, "key": key, "result": out, "offline": False})
        return dict(out)


def has_pro(cfg):
    if not license_key(cfg):
        return False
    return bool(verify(cfg).get("pro"))


def path_allowed(path, cfg):
    """服务端单点闸门：免费白名单放行，其余 /api/ 路由需要专业版。

    非 /api/ 路径（页面、静态资源、登录页、favicon）一律放行，
    否则连登录页都打不开。302 端点刻意不在白名单内。
    """
    if not path.startswith("/api/"):
        return True
    if path in FREE_EXACT:
        return True
    for pre in FREE_PREFIX:
        if path.startswith(pre):
            return True
    return has_pro(cfg)


def key_mask(key):
    key = (key or "").strip().upper()
    if not key:
        return ""
    parts = key.split("-")
    if len(parts) >= 4:
        return "%s-%s-****-%s" % (parts[0], parts[1], parts[-1])
    return key[:4] + "****" + key[-4:]


def status(cfg):
    """给前端「许可证」抽屉的状态对象；绝不回传明文激活码。"""
    key = license_key(cfg)
    if not key:
        v = {"valid": False, "pro": False, "plan": "free",
             "message": "未激活，当前为免费版"}
    else:
        v = verify(cfg)
    return {
        "plan": "pro" if v.get("pro") else "free",
        "pro": bool(v.get("pro")),
        "valid": bool(v.get("valid")),
        "activated": bool(key),
        "key_mask": key_mask(key),
        "server": license_server(cfg),
        "device_id": device_id()[:12],
        "price": PRICE_CNY,
        "buyout": True,
        "permanent": bool(v.get("permanent")),
        "expires_at": v.get("expires_at"),
        "devices_used": v.get("devices_used"),
        "max_devices": v.get("max_devices"),
        "device_limit": bool(v.get("device_limit")),
        "offline": bool(v.get("offline")),
        "offline_grace_days": OFFLINE_GRACE // 86400,
        "message": v.get("message") or "",
        "free_exact": len(FREE_EXACT),
    }
