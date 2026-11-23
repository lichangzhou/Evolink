# -*- coding: utf-8 -*-
"""115 chrome/downurl 换链模块（纯标准库，NAS python 3.11 安全）。

链路（2026-10-20 已与官方 p115cipher 逐字节对齐验证）：
  resolve_file(path) -> pickcode（复用 pan115.resolve_file，webapi 逐级定位）
  POST proapi.115.com/app/chrome/downurl  body=form: data=<rsa_encrypt(json)>
  rsa_decrypt(响应 data) -> 115cdn.net 可播直链

【2026-10-22 关键结论】直链的 k 签名绑定「签发请求的 User-Agent」：
  用 UA=X 签出的链只有用 UA=X 取才是 206，换任何 UA 都是 403 invalid signature。
  所以签发必须透传播放器自己的 UA（见 downurl_url 的 ua 参数与 _clean_ua），
  这也是这类实现长期稳定播放的原因。旧结论「UA 只影响 f=1/f=3」是错的，已推翻。

风控规避（硬性要求，见交接文档 §3.2）：
  - on-demand：只有播放请求才进本模块，禁止后台预签发 / 批量扫描换链。
  - 内存缓存：同一 path 的直链缓存 25 分钟（实测有效期 40-48 分钟，留余量），
    命中直接返回，绝不重复签发。
  - 失败不重试：signing 失败直接抛错由 server 返回 502，禁止循环轰炸 115。

注意：本模块只在播放请求被触发时才发 115 请求；部署/重启本身不连 115。
"""
import base64
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config
from . import pan115

# ---------- RSA 常量（已与 p115cipher 0.0.3 对齐，勿改） ----------
N = 0x8686980c0f5a24c4b9d43020cd2c22703ff3f450756529058b1cf88f09b8602136477198a6e2683149659bd122c33592fdb5ad47944ad1ea4d36c6b172aad6338c3bb6ac6227502d010993ac967d1aef00f0c8e038de2e4d3bc2ec368af2e9f10a6f1eda4f7262f136420c07c331b871bf139f74f3010e3c4fe57df3afb71683
E = 0x10001
RSA_KEY = b"\x8d\xa5\xa5\x8d"
G_key_l = b"x\x06\xadL3\x86]\x18L\x01?F"
G_kts = bytes([0xf0,0xe5,0x69,0xae,0xbf,0xdc,0xbf,0x8a,0x1a,0x45,0xe8,0xbe,0x7d,0xa6,0x73,0xb8,0xde,0x8f,0xe7,0xc4,0x45,0xda,0x86,0xc4,0x9b,0x64,0x8b,0x14,0x6a,0xb4,0xf1,0xaa,0x38,0x01,0x35,0x9e,0x26,0x69,0x2c,0x86,0x00,0x6b,0x4f,0xa5,0x36,0x34,0x62,0xa6,0x2a,0x96,0x68,0x18,0xf2,0x4a,0xfd,0xbd,0x6b,0x97,0x8f,0x4d,0x8f,0x89,0x13,0xb7,0x6c,0x8e,0x93,0xed,0x0e,0x0d,0x48,0x3e,0xd7,0x2f,0x88,0xd8,0xfe,0xfe,0x7e,0x86,0x50,0x95,0x4f,0xd1,0xeb,0x83,0x26,0x34,0xdb,0x66,0x7b,0x9c,0x7e,0x9d,0x7a,0x81,0x32,0xea,0xb6,0x33,0xde,0x3a,0xa9,0x59,0x34,0x66,0x3b,0xaa,0xba,0x81,0x60,0x48,0xb9,0xd5,0x81,0x9c,0xf8,0x6c,0x84,0x77,0xff,0x54,0x78,0x26,0x5f,0xbe,0xe8,0x1e,0x36,0x9f,0x34,0x80,0x5c,0x45,0x2c,0x9b,0x76,0xd5,0x1b,0x8f,0xcc,0xc3,0xb8,0xf5])

DOWNURL = "https://proapi.115.com/app/chrome/downurl"

# 【2026-10-22 实测结论，是本次 403 排查的最终答案，勿再按旧结论改】
# 115 直链的 k 签名严格绑定「签发请求时用的 User-Agent」：
#   用 UA=X 签出的链，只有用 UA=X 去取才是 206；换任何一个 UA 都是 403 invalid signature。
#   （实测 5 组 UA 全部如此：libmpv / 完整 Chrome 串 / Infuse-Direct / VLC / 空串）
# 这正是长期稳定播放的原因：把播放器自己的 UA 带进签发请求，
# 同一个播放器跟随 302 取链时 UA 天然一致。
# 因此本模块不再使用固定 UA，改由调用方把「播放器自己的 UA」透传进来
# （server.py 的 /api/stream 读客户端 User-Agent 头），客户端取链时自动对上。
# 空 UA 也能签（客户端不发 UA 时两边都是空，同样匹配）。
# Referer 不参与校验，但 p115client 从不发，保持不带。
# 注意：不要再引入「固定 UA」——那正是 2026-10-22 之前全部直链 403 的根因。
# 缓存 TTL 25 分钟（实测直链有效期 40-48 分钟），提前 60 秒视为过期重签
CACHE_TTL = 1500
CACHE_MARGIN = 60

# 纯内存缓存：{(path, ua): {"url": str, "expire_at": float}}
# key 必须带 ua：同一个文件用不同 UA 签出的链互不通用（115 的 k 签名绑定 UA）
_CACHE = {}
_CACHE_LOCK = threading.Lock()

# 【2026-11-03 新增】磁盘持久化缓存（跨重启仍命中）
# 实测：重启后首播仍是 0.01~0.05s，因为把直链缓存落盘了；
# 而 EvoLink 纯内存缓存重启即失，首播要 0.7~2.7s（重新解析路径 + 重新签发）。
# 现在把直链缓存写到 data 目录，重启后直接复用（TTL 仍严格生效，过期必重签）。
_DISK_CACHE_FILE = None       # 运行时由 _disk_cache_path() 定位
_DISK_CACHE_ENABLED = True
_DISK_SAVE_TTL = 30           # 变更后最多延迟 30s 落盘（避免高频写盘）
_DISK_DIRTY = {"v": False}
_DISK_LAST_SAVE = {"v": 0.0}

# 【2026-11-03 新增】失败缓存：{(path, ua): (ts, err_msg)}
# 动机：实测 EvoLink 换链失败（如路径不存在）时**不缓存**，播放器重试一次
# 就再打一次 115，连续重试直接把 115 打到风控（与 §3.2「失败不重试」铁律相悖）。
# 同类实现靠 use_original 回退把失败挡在下游；EvoLink 没有原始 URL 可退，
# 所以用短 TTL 的负缓存挡住重试风暴：失败后 60s 内同一 (路径, UA) 直接复用错误，
# 不再打 115。60s 之后允许重试（用户可能刚补好文件）。
FAIL_CACHE_TTL = 60
_FAIL_CACHE = {}

# 115 不走代理（与 pan115 一致，避免出口差异触发风控）
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class DownurlError(RuntimeError):
    pass


# ---------- 磁盘持久化缓存（跨重启仍命中） ----------
def _disk_cache_path():
    """直链缓存的落盘位置：与 EvoLink 其它数据同目录（config.DATA 下）。"""
    try:
        from . import config as _cfg
        data = getattr(_cfg, "DATA", None)
        if data:
            return os.path.join(str(data), "115_downurl_cache.json")
    except Exception:
        pass
    return "/app/data/115_downurl_cache.json"


def _disk_load():
    """启动时把磁盘缓存读进内存（只取未过期的）。"""
    if not _DISK_CACHE_ENABLED:
        return
    p = _disk_cache_path()
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return
    now = time.time()
    n = 0
    with _CACHE_LOCK:
        for k, v in (d or {}).items():
            if not isinstance(v, dict):
                continue
            try:
                exp = float(v.get("expire_at") or 0)
            except Exception:
                continue
            if exp - CACHE_MARGIN > now:
                # key 形如 "path\u0001ua"
                try:
                    path, ua = k.split("\u0001", 1)
                except ValueError:
                    continue
                _CACHE[(path, ua)] = {"url": v.get("url") or "", "expire_at": exp}
                n += 1
    if n:
        print(f"[115] 从磁盘恢复直链缓存 {n} 条", flush=True)


def _disk_save():
    """把内存缓存落盘（只写未过期的）。"""
    if not _DISK_CACHE_ENABLED:
        return
    now = time.time()
    out = {}
    with _CACHE_LOCK:
        for (path, ua), v in _CACHE.items():
            try:
                if v["expire_at"] - CACHE_MARGIN > now:
                    out[path + "\u0001" + (ua or "")] = {
                        "url": v["url"], "expire_at": v["expire_at"]}
            except Exception:
                continue
    try:
        p = _disk_cache_path()
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception:
        pass
    _DISK_DIRTY["v"] = False
    _DISK_LAST_SAVE["v"] = now


def _disk_mark_dirty():
    """标记缓存有变更，由后台线程定期落盘。

    【2026-11-03 修正】原来只在「首次 30s 内」才立即写盘，之后全靠 worker 15s 轮询。
    但 worker 只在 _DISK_DIRTY 为真时才写，而 _DISK_DIRTY 在**缓存命中**路径上
    根本不会被置位（命中直接 return）→ 缓存文件一直是空的。
    改为：第一次变更立即落盘，之后按 _DISK_SAVE_TTL 节流。
    """
    _DISK_DIRTY["v"] = True
    now = time.time()
    if _DISK_LAST_SAVE["v"] <= 0 or now - _DISK_LAST_SAVE["v"] >= _DISK_SAVE_TTL:
        _disk_save()


def disk_cache_stats():
    with _CACHE_LOCK:
        n = len(_CACHE)
    return {"mem": n, "path": _disk_cache_path(), "enabled": _DISK_CACHE_ENABLED}


# ---------- 加解密（与 p115cipher 对齐） ----------
def xor_bytes(src, key):
    src = bytes(src)
    key = bytes(key)
    out = bytearray()
    i = len(src) & 0b11
    if i:
        a = int.from_bytes(src[:i], "big")
        b = int.from_bytes(key[:i], "big")
        out += (a ^ b).to_bytes(i, "big")
    for j in range(i, len(src), len(key)):
        block = src[j:j + len(key)]
        blen = len(block)
        a = int.from_bytes(block, "big")
        b = int.from_bytes(key[:blen], "big")
        out += (a ^ b).to_bytes(blen, "big")
    return bytes(out)


def rsa_pub(data, block_size=128):
    out = bytearray()
    for i in range(0, len(data), block_size - 11):
        block = data[i:i + block_size - 11]
        pad = b"\x00" + b"\x02" * (block_size - 2 - len(block)) + b"\x00" + block
        m = int.from_bytes(pad, "big")
        out += pow(m, E, N).to_bytes(block_size, "big")
    return bytes(out)


def rsa_encrypt(data):
    tmp = xor_bytes(data, RSA_KEY)[::-1]
    xd = b"\x00" * 16 + xor_bytes(tmp, G_key_l)
    return base64.b64encode(rsa_pub(xd)).decode("ascii")


def rsa_gen_key(rand_key, sk_len=4):
    rk = bytes(rand_key)
    xk = bytearray(sk_len)
    length = sk_len * (sk_len - 1)
    index = 0
    for i in range(sk_len):
        x = (rk[i] + G_kts[index]) & 0xff
        xk[i] = G_kts[length] ^ x
        length -= sk_len
        index += sk_len
    return bytes(xk)


def rsa_decrypt(cipher_data, block_size=128):
    raw = base64.b64decode(cipher_data)
    data = bytearray()
    for i in range(0, len(raw), block_size):
        block = bytes(raw[i:i + block_size])
        p = pow(int.from_bytes(block, "big"), E, N)
        b = p.to_bytes((p.bit_length() + 7) >> 3, "big")
        data += b[b.index(0) + 1:]
    view = memoryview(bytes(data))
    randkey = view[:16]
    key_l = rsa_gen_key(randkey, 12)
    tmp = xor_bytes(view[16:], key_l)[::-1]
    return bytes(xor_bytes(tmp, RSA_KEY))


# ---------- cookie / user_id ----------
def _cookie_dict(ck=None):
    return ck or (config.load().get("pan115_cookie") or {})


def _user_id(ck):
    uid = str(ck.get("UID") or "")
    return uid.split("_")[0] or "0"


def _cookie_header(ck):
    return "; ".join(f"{k}={ck.get(k)}" for k in ("UID", "CID", "SEID", "KID") if ck.get(k))


def _clean_ua(ua):
    """清洗播放器 UA：去控制字符（防 HTTP 头注入）、截断、保证可 latin-1 编码。

    内容本身一个字符都不改——115 要求「签发用的 UA」与「取链用的 UA」完全一致，
    任何改写（哪怕只是大小写或首尾空白）都会导致 CDN 403 invalid signature。
    """
    if not ua:
        return ""
    ua = str(ua)
    ua = "".join(ch for ch in ua if 0x20 <= ord(ch) != 0x7f)
    ua = ua.strip()
    if len(ua) > 512:
        ua = ua[:512]
    try:
        ua.encode("latin-1")
    except UnicodeEncodeError:
        ua = ua.encode("latin-1", "replace").decode("latin-1")
    return ua


# ---------- 换链主入口 ----------
def is_pickcode(s):
    """115 pickcode 特征：纯字母数字、无斜杠、无点、10~40 位。

    URL 型 strm（如 http://nas:8115/api/redirect115/<pickcode>）提取出来的就是它；
    带斜杠或带扩展名的一定是路径，不会误判。导出供 reverse_proxy 的 strm 解析用。
    """
    s = (s or "").strip()
    return bool(s) and "/" not in s and "." not in s and 10 <= len(s) <= 40 and s.isalnum()


def downurl_url(path="", ck=None, force=False, ua="", pc=""):
    """115 定位符 -> 115cdn.net 可播直链（带 25 分钟内存缓存，on-demand）。

    两种入参形态（2026-10-24 新增 pickcode 直达，支持 URL 型 strm）：
      path：115 相对路径，形如 115open/Video/.../xxx.mkv（已去掉本地前缀）；
            —— 内部先 resolve_file 逐级定位拿 pickcode，再签。
      pc  ：115 pickcode（URL 型 strm 里直接带的那个）；
            —— 跳过逐级目录定位，直接签。pickcode 也可以当 path 传
               （无斜杠无点、10~40 位字母数字时自动识别为 pickcode）。
    账号语义：签谁的链取决于 config 里**当前登录**的 pan115_cookie，
      换个人登录就按谁的账号走，无任何硬编码（115 的 pickcode 账号隔离，
      非本账号的 pickcode 会返回 errno=50015）。
    ua：播放器的 User-Agent，必须由调用方从播放请求原样透传。
        115 的 k 签名绑定签发时用的 UA，用错 UA 签出的链客户端取不到（403 invalid signature）；
        缓存也按 (path|pickcode, ua) 分开存，不同播放器的链互不通用。
    失败抛 DownurlError，由 server 转 502；本函数不做任何重试。
    """
    path = (path or "").strip()
    pc = (pc or "").strip()
    if not pc and path and is_pickcode(path):
        pc, path = path, ""
    if not pc and not path:
        raise DownurlError("path 为空")
    ua = _clean_ua(ua)
    ck_key = (pc or path, ua)

    now = time.time()
    if not force:
        with _CACHE_LOCK:
            hit = _CACHE.get(ck_key)
            if hit and now < hit["expire_at"] - CACHE_MARGIN:
                return hit["url"]
        # 负缓存：刚失败过就直接复用错误，不打 115（防重试风暴触发风控）
        with _CACHE_LOCK:
            fhit = _FAIL_CACHE.get(ck_key)
        if fhit and now - fhit[0] < FAIL_CACHE_TTL:
            raise DownurlError(fhit[1])

    ck = _cookie_dict(ck)
    if not ck.get("UID"):
        raise DownurlError("115 未登录（pan115_cookie 为空），请先在面板扫码登录")

    # 1) 拿 pickcode：路径型才需要逐级定位；pickcode 型直达
    if not pc:
        try:
            f = pan115.resolve_file_cached(path, ck)
        except Exception as e:
            msg = f"路径解析失败：{e}"
            with _CACHE_LOCK:
                _FAIL_CACHE[ck_key] = (time.time(), msg)
            raise DownurlError(msg)
        pc = f.get("pc") or ""
        if not pc:
            raise DownurlError(f"未取到 pickcode：{f.get('name')}")

    # 2) POST chrome/downurl（pickcode + user_id，两种形态共用同一通道）
    payload = json.dumps({"pickcode": pc, "user_id": int(_user_id(ck))}).encode()
    body = urllib.parse.urlencode({"data": rsa_encrypt(payload)}).encode()
    req = urllib.request.Request(
        DOWNURL, data=body, method="POST",
        headers={
            "User-Agent": ua,
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": _cookie_header(ck),
            "Accept": "*/*",
        },
    )
    try:
        with _NO_PROXY.open(req, timeout=30) as resp:
            r = json.loads(resp.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        # 403/405 等：直接抛出，不重试
        msg = f"downurl HTTP {e.code}（可能触发 115 风控，勿重复刷新）"
        with _CACHE_LOCK:
            _FAIL_CACHE[ck_key] = (time.time(), msg)
        raise DownurlError(msg)
    except Exception as e:
        msg = f"downurl 请求失败：{type(e).__name__} {e}"
        with _CACHE_LOCK:
            _FAIL_CACHE[ck_key] = (time.time(), msg)
        raise DownurlError(msg)

    if not r.get("state") or not r.get("data"):
        errno = r.get("errno")
        msg = r.get("msg") or r.get("error") or ""
        # 高频错误码给可操作人话（仍不做任何重试，遵守 §3.2 风控铁律：绝不 force 重签）
        if errno == 99 or "重新登录" in str(msg) or "登录" in str(msg):
            err = "115 登录态已失效（SEID 过期），请在「115 网盘」页重新扫码登录后再播"
        elif errno == 50015:
            err = "该资源不属于当前登录的 115 账号（pickcode 账号隔离），请切换到资源所属账号扫码登录后再播"
        else:
            err = f"115 拒绝签发：errno={errno} {msg}（可能触发风控，勿重复刷新，稍后再试）"
        with _CACHE_LOCK:
            _FAIL_CACHE[ck_key] = (time.time(), err)
        raise DownurlError(err)

    # 3) 解密取 url
    try:
        dec = json.loads(rsa_decrypt(r["data"]))
        uidk = list(dec.keys())[0]
        url = dec[uidk]["url"]["url"]
    except Exception as e:
        msg = f"响应解密失败：{type(e).__name__} {e}"
        with _CACHE_LOCK:
            _FAIL_CACHE[ck_key] = (time.time(), msg)
        raise DownurlError(msg)

    if not url:
        msg = "解密结果无 url"
        with _CACHE_LOCK:
            _FAIL_CACHE[ck_key] = (time.time(), msg)
        raise DownurlError(msg)

    with _CACHE_LOCK:
        _CACHE[ck_key] = {"url": url, "expire_at": now + CACHE_TTL}
        _FAIL_CACHE.pop(ck_key, None)     # 成功了就清掉负缓存
    _disk_mark_dirty()
    return url


def cache_clear():
    """清空内存直链缓存（调试/换 cookies 后用，正常播放不需要调用）。"""
    with _CACHE_LOCK:
        _CACHE.clear()
        _FAIL_CACHE.clear()
    _disk_save()


# ---------- 后台落盘线程 + 启动加载 ----------
def _disk_worker():
    """每 30s 检查一次：有变更就落盘。进程退出前由 atexit 兜底保存。"""
    while True:
        time.sleep(15)
        try:
            if _DISK_DIRTY["v"]:
                _disk_save()
        except Exception:
            pass


def init_disk_cache():
    """进程启动时调用：加载磁盘缓存 + 起后台落盘线程 + 注册退出保存。"""
    try:
        _disk_load()
    except Exception:
        pass
    try:
        import atexit
        atexit.register(_disk_save)
    except Exception:
        pass
    try:
        t = threading.Thread(target=_disk_worker, daemon=True)
        t.start()
    except Exception:
        pass
