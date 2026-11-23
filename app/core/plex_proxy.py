"""evolink Plex 反向代理（plex-demo 同款思路）：让 Plex 客户端直连 evolink 的
「回调端口」（默认 32411），页面 / API / 海报整段透传给真 Plex（32400），
仅拦媒体流请求 /library/parts/<partid>/<n>/file → 查 Plex DB 拿 part 文件 →
（.strm 则读第一行）→ 映射 115 相对路径 → 用客户端 UA 签 115 CDN 直链 → 302。
strm 文件一字不动。转码请求透传真 Plex 兜底。

与 plex-demo（nginx+njs）的差异，都是有意的：
- 302 头不发 Cache-Control（plex-demo 发 max-age=3600，实测导致播放器快进断裂）；
  带 Accept-Ranges: bytes（让播放器用 Range 做 seek 而不是重拉整条）。
- 自检按「文件」缓存 1 小时、失败 5 分钟免验、绝不 force 重签（复用 reverse_proxy
  的 _sign_and_verify，与 Jellyfin/Emby 反代同一套风控纪律）。
- 不自动跟随后端 3xx（Plex 根路径 / 是 302 → /web/，跟随会把 web app 的 HTML
  塞在 / 下，相对路径资源全 404 → 黑屏）。

端口管理与 reverse_proxy 共享（_SERVERS/_ASSIGNED）：本模块起的反代注册进
reverse_proxy._SERVERS，因此 /api/proxy/status 的 running_ports() 自动包含 Plex。
"""

import hashlib
import os
import re
import socket
import ssl
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core import config
from core import reverse_proxy
from core.reverse_proxy import (_HOP, _NOPROXY, _NoRedirect, _OPENER, _relay,
                                _sign_and_verify, _strm_line_to_rel, log_event)

# /library/parts/<partid>/<n>/file —— partid 就是 Plex DB media_parts.id（已实证）
_PART_RE = re.compile(r"^/library/parts/(\d+)/(\d+)/file$")

# 需要注入伪造 Stream 的接口（让 Plex 官方客户端认为 strm 条目可直拨）
_INJECT_PATH_RE = re.compile(
    r"/library/metadata|/library/sections/\d+|/hubs/|/playQueues|"
    r"/video/:/transcode/universal/decision"
)

# 2026-10-27 起不再注入伪造流（原 plex-demo 4K DoVi / 后改 H264+AAC）：
# 客户端 direct play 时直接 302 读真实字节流、自行 probe 音视频轨，
# 元数据保持与影片真实一致。直拨靠两件事：parts key 归一化为相对路径 +
# decision 强制直拨。（2026-10-29：绝对化改归一化，绝对 key 在 VIDHUB 上
# 会拼出 /ttp://... 畸形路径 404）


def _norm_parts(text):
    """把 /library/parts/ 的 key 统一归一化为【相对路径】（Plex 官方标准格式）。

    2026-10-29 起废弃"绝对化"策略：绝对 key（key="http://192.168.1.10:32411/..."）
    在部分客户端（VIDHUB 实测）会被按相对路径拼接，产出 /ttp://... 畸形请求 → 404。
    相对 key 客户端会用"当前正在使用的连接"自行拼接——元数据能浏览到就说明该连接
    打得通反代，播放请求自然落在反代上，无需再强制绝对地址。"""
    text = re.sub(r'key="https?://[^"]*?(/library/parts/)', r'key="\1', text)
    text = re.sub(r'("key"\s*:\s*")https?://[^"]*?(/library/parts/)',
                  lambda m: m.group(1) + m.group(2), text)
    return text


# 2026-10-29 伪装流（用户拍板，恢复 plex-demo 思路）：strm 探测不出流信息，
# Media 无 codec/container、Part 无 Stream 子元素 → 官方客户端 profile 判定
# "未知格式"，只能请求 HLS 转码会话（本机 start.m3u8 历史 0 成功、TPU 必拒）。
# 伪装成 mkv/h264/aac 让客户端直拨；真实字节流由客户端自己 probe，302 给 115 CDN。
_FAKE_MEDIA_ATTRS = ('videoCodec="h264" audioCodec="aac" audioProfile="lc" '
                     'container="mkv" videoProfile="high" videoResolution="1080" '
                     'videoFrameRate="24p"')
_FAKE_STREAMS = (
    '<Stream id="1" streamType="1" selected="1" default="1" codec="h264" index="0" '
    'profile="high" level="41" pixelAspectRatio="1" height="1080" width="1920" '
    'frameRate="24" frameRateMode="cfr" scanType="progressive" '
    'displayTitle="1080p (H.264)" />'
    '<Stream id="2" streamType="2" selected="1" default="1" codec="aac" index="1" '
    'profile="lc" channels="2" audioChannelLayout="stereo" bitDepth="16" '
    'bitrate="192" samplingRate="48000" displayTitle="Stereo (AAC)" />')


def _fake_streams(text):
    """仅对【无 codec 信息】的 Media（即 strm 条目）注入伪装属性与 Stream 子元素；
    真实体文件的元数据（自带 videoCodec / Stream）一字不动。"""
    def _fix_media(m):
        tag = m.group(0)
        if 'videoCodec=' in tag:
            return tag
        return tag.replace('<Media ', '<Media ' + _FAKE_MEDIA_ATTRS + ' ', 1)

    def _fix_part(m):
        inner = m.group(1)
        if 'streamType=' in inner or 'container="mkv"' in inner:
            return m.group(0)
        if 'container=' in inner:
            inner = re.sub(r'container="[^"]*"', 'container="mkv"', inner)
        else:
            inner += ' container="mkv"'
        return '<Part ' + inner + '>' + _FAKE_STREAMS + '</Part>'

    text = re.sub(r'<Media [^>]*>', _fix_media, text)
    text = re.sub(r'<Part ([^>]*?)/>', _fix_part, text)
    return text

# partid -> (ts, 115相对路径 或 None)  短 TTL 缓存，避免每个分片请求都查 Plex DB
# （plex-demo 的 partInfoDict 同款；播放器 7 秒能发 10 个分片请求）
_PART_CACHE = {}
_PART_CACHE_TTL = 300        # 命中的缓存 5 分钟
_PART_MISS_TTL = 30          # 没查到的只缓存 30 秒（plex 重扫后尽快自愈）
_PART_CACHE_LOCK = threading.Lock()


def _part_file(partid, db_path):
    """Plex DB（只读）查 media_parts.file。plex 容器视角路径，如 /strm/... 或 /CloudNAS/..."""
    if not db_path:
        return None
    try:
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            cur = c.cursor()
            cur.execute("SELECT file FROM media_parts WHERE id=? LIMIT 1", (int(partid),))
            row = cur.fetchone()
            return row[0] if row and row[0] else None
        finally:
            c.close()
    except Exception:
        return None


def _resolve_part(partid, backend):
    """partid → 115 定位符（115 相对路径或 pickcode）。拿不到返回 None。

    media_parts.file 是 plex 容器内视角：
    - .strm 结尾 → strm 模式。文件在 plex 容器里是 /strm/...，本容器挂在 /Link/，
      先做前缀换算再读第一行（第一行 = 实体路径，如 /CloudNAS/CloudDrive/115open/...，
      或 URL 型 strm 的 http://.../api/redirect115/<pickcode>）。
    - 其余 → 实体模式（CD2 直挂），file 直接就是实体路径。

    strm 第一行两种形态都吃（2026-10-24 起，见 reverse_proxy.strm_line_info）。
    """
    now = time.time()
    with _PART_CACHE_LOCK:
        hit = _PART_CACHE.get(partid)
        if hit and now - hit[0] < _PART_CACHE_TTL:
            return hit[1]
    rel = None
    f = _part_file(partid, backend.get("db_path"))
    if f:
        if f.lower().endswith(".strm"):
            local = f
            sp = backend.get("strm_prefix") or "/strm/"
            lp = backend.get("link_prefix") or "/Link/"
            if sp and local.startswith(sp):
                local = lp + local[len(sp):]
            try:
                with open(local, "r", errors="ignore") as fh:
                    line = fh.readline().strip()
            except Exception:
                line = ""
            entity = line
        else:
            entity = f
        if entity:
            rel = _strm_line_to_rel(entity, backend)
    with _PART_CACHE_LOCK:
        _PART_CACHE[partid] = (now, rel)
    return rel


def _make_handler(backend):
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "evolink-proxy/1.0"

        # ---- 302 换链（头要「干净」：不发 Cache-Control，带 Accept-Ranges）----
        def _redirect(self, url):
            self.send_response(302)
            self.send_header("Location", url)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", "0")
            self.end_headers()

        # ---- 公共地址（「回调地址」概念，留空按 Host 头推断）----
        def _public_base(self):
            cfgd = (backend.get("public_base") or "").strip()
            if cfgd:
                return cfgd.rstrip("/")
            host = self.headers.get("Host") or ""
            if host:
                return "http://" + host
            return backend["base"].rstrip("/")

        # ---- 播放器媒体 URL 基址：按实际连接定协议（https 客户端给 https 绝对地址）----
        def _media_base(self):
            cfgd = (backend.get("public_base") or "").strip()
            if cfgd:
                return cfgd.rstrip("/")
            host = self.headers.get("Host") or ""
            if not host:
                return ""
            scheme = "https" if isinstance(self.connection, ssl.SSLSocket) else "http"
            if host.split(":")[0] == "plex.7359577.xyz":
                scheme = "https"
            return scheme + "://" + host

        # ---- WebSocket 透传（Plex Web 的 /:/websockets/... 要走隧道）----
        def _proxy_websocket(self):
            import socket as _socket
            pu = urllib.parse.urlparse(backend["base"])
            chost, cport = pu.hostname, pu.port or 80
            keep = ("upgrade", "connection", "sec-websocket-key", "sec-websocket-version",
                    "sec-websocket-protocol", "sec-websocket-extensions", "origin",
                    "user-agent", "cookie", "authorization", "x-plex-token")
            # 强制 WebSocket 用 owner token（Plex WS 只认 query token，SPA 可能带旧/失效 token）
            if _PLEX_TOKEN:
                import re as _re
                if "X-Plex-Token=" in self.path:
                    self.path = _re.sub(r"X-Plex-Token=[^&]*",
                                        "X-Plex-Token=" + _PLEX_TOKEN, self.path)
                else:
                    self.path += ("&" if "?" in self.path else "?") + "X-Plex-Token=" + _PLEX_TOKEN
            lines = ["GET %s HTTP/1.1" % self.path]
            for k in self.headers.keys():
                if k.lower() in keep:
                    lines.append("%s: %s" % (k, self.headers[k]))
            lines.append("Host: %s" % pu.netloc)
            raw = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8", "ignore")
            try:
                up = _socket.create_connection((chost, cport), timeout=30)
                up.settimeout(None)
                up.sendall(raw)
            except Exception as e:
                print(f"[proxy] plex WebSocket 连接后端失败：{e}", flush=True)
                try:
                    self.send_error(502)
                except Exception:
                    pass
                return
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
                head, _sep, rest = buf.partition(b"\r\n\r\n")
                try:
                    code = int(head.split(b" ")[1])
                except Exception:
                    code = 502
                print(f"[proxy] plex WebSocket 后端未返回 101：{head[:60]!r}", flush=True)
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
            print(f"[proxy] plex WebSocket 已建立 → {backend['base']}{self.path}", flush=True)
            log_event("ws", player="plex", item="", path=self.path[:160],
                      ua=(self.headers.get("User-Agent") or "")[:120], status=101,
                      note="WebSocket 已建立")
            _relay(self.connection, up)

        # 把后端自报地址（如 http://127.0.0.1:32400）改写成客户端实际访问的 32411 地址，
        # 否则浏览器拿到 127.0.0.1:32400 会指向自己电脑而断链。
        def _rewrite_loc(self, loc):
            try:
                base = backend["base"].rstrip("/")
                if loc.startswith(base):
                    host = self.headers.get("Host") or ""
                    if host:
                        return "http://" + host + loc[len(base):]
            except Exception:
                pass
            return loc

        # ---- 整段反向代理到真 Plex（不跟随 3xx，原样交回客户端）----
        def _proxy_pass(self):
            path = self.path.split("?", 1)[0]
            target = backend["base"].rstrip("/") + self.path
            # decision 强制直拨：禁止转码决策（否则真 Plex 去读 strm 文本必然失败）
            if (_meta_inject_on()
                    and "/video/:/transcode/universal/decision" in path
                    and "directPlay=1" not in self.path):
                sep = "&" if "?" in target else "?"
                target += sep + "directPlay=1&directStream=1"
            headers = {}
            for k in self.headers.keys():
                if k.lower() in _HOP:
                    continue
                headers[k] = self.headers[k]
            pu = urllib.parse.urlparse(backend["base"])
            headers["Host"] = pu.netloc
            # 自动注入 Plex token：让局域网客户端免登录即可用 32411 Web/API
            if _PLEX_TOKEN:
                headers["X-Plex-Token"] = _PLEX_TOKEN
            # 强制后端返回未压缩响应：否则注入分支把响应体重序列化为明文后，
            # 仍带着后端给的 Content-Encoding: gzip 头，浏览器解压明文会失败
            # （ERR_CONTENT_DECODING_FAILED）→ 媒体库空白/“出现意外错误”。
            headers["Accept-Encoding"] = "identity"

            body = None
            if self.command in ("POST", "PUT") and "Content-Length" in self.headers:
                try:
                    body = self.rfile.read(int(self.headers["Content-Length"]))
                except Exception:
                    body = None
            # 是否需要改写响应（注入伪造 Stream，让官方客户端认为可直拨）
            need_inject = (self.command in ("GET", "HEAD")
                           and bool(_INJECT_PATH_RE.search(path)))
            try:
                req = urllib.request.Request(
                    target, data=body, method=self.command, headers=headers
                )
                with _OPENER.open(req, timeout=120) as r:
                    status = r.status
                    resp_headers = r.headers
                    ctype = resp_headers.get("Content-Type", "") or ""
                    # 改写分支：读全量 → 注入 → 重发（仅小 XML/JSON 响应，绝不含媒体流）
                    if need_inject and status == 200:
                        try:
                            raw = r.read()
                            ce = (resp_headers.get("Content-Encoding") or "").lower()
                            if "gzip" in ce:
                                import gzip as _gzip
                                try:
                                    raw = _gzip.decompress(raw)
                                except Exception:
                                    pass
                            text = raw.decode("utf-8", "ignore")
                            if _meta_inject_on():
                                # key 归一化 + strm 条目伪装成 mkv/h264/aac；
                                # 真实体文件元数据保持与影片真实一致
                                text = _fake_streams(_norm_parts(text))
                            raw = text.encode("utf-8", "ignore")
                        except Exception:
                            raw = b""
                        self.send_response(status)
                        for k in resp_headers.keys():
                            kl = k.lower()
                            if kl in _HOP or kl in ("content-length",
                                                    "transfer-encoding",
                                                    "content-encoding"):
                                continue
                            self.send_header(k, resp_headers[k])
                        self.send_header("Content-Length", str(len(raw)))
                        self.send_header("Connection", "close")
                        self.end_headers()
                        if self.command != "HEAD" and raw:
                            self.wfile.write(raw)
                        self.close_connection = True
                        return
                    # 常规分支：流式透传
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
                # 3xx：原样把跳转交回客户端（Location 必须带上，否则客户端拿到空页）
                if 300 <= e.code < 400:
                    loc = ""
                    try:
                        loc = e.headers.get("Location") or ""
                    except Exception:
                        loc = ""
                    if loc:
                        loc = self._rewrite_loc(loc)
                    self.send_response(e.code)
                    if loc:
                        self.send_header("Location", loc)
                    self.send_header("Content-Length", "0")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    return
                # 其他错误（典型：401 未带 token）：状态码 + 响应头 + body 原样回传。
                # Plex 401 的 body 是一段 JS（跳 web/index.html），丢了浏览器就白屏。
                try:
                    err_body = e.read()
                except Exception:
                    err_body = b""
                try:
                    self.send_response(e.code)
                    for k in e.headers.keys():
                        kl = k.lower()
                        if kl in _HOP or kl in ("content-length",
                                                "transfer-encoding"):
                            continue
                        self.send_header(k, e.headers[k])
                    self.send_header("Content-Length", str(len(err_body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    if err_body and self.command != "HEAD":
                        self.wfile.write(err_body)
                except Exception:
                    pass
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

        # ---- 媒体流换链 ----
        def _part_flow(self, partid):
            rel = _resolve_part(partid, backend)
            if not rel:
                log_event("passthrough", player="plex", item=partid,
                          path=self.path[:160],
                          ua=(self.headers.get("User-Agent") or "")[:120], status=0,
                          note="未解析到 115 路径（part 不存在或 strm 读不到）")
                self._proxy_pass()
                return
            ua = self.headers.get("User-Agent") or ""
            url, ok, note = _sign_and_verify(rel, ua)
            if not ok:
                # 签出来的是坏链（115 限流/偶发无效）→ 退回透传（实体库仍能播）
                print(f"[proxy] plex 换链自检失败（{note}），回退透传 part={partid}",
                      flush=True)
                log_event("error", player="plex", item=partid, path=self.path[:160],
                          ua=ua[:120], status=0, note=f"{note}，退回透传")
                self._proxy_pass()
                return
            print(f"[proxy] plex 媒体请求 part={partid} → 302 换链"
                  f"（{rel}，UA={ua[:40]}，{note}）", flush=True)
            log_event("media", player="plex", item=partid, path=self.path[:160],
                      ua=ua[:120], status=302, target=rel[:200], cdn=url[:200],
                      note=note)
            self._redirect(url)

        # ---- start.m3u8 拦截：302 到 part 换链端点（plex-demo 同款思路）----
        def _db_lookup_part(self, ratingkey, offset):
            """metadata ratingKey → media_parts.id（mediaIndex+partIndex 简化为一维 offset）。"""
            db = backend.get("db_path")
            if not db:
                return None
            try:
                c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                try:
                    cur = c.cursor()
                    cur.execute(
                        "SELECT mp.id FROM media_items mi "
                        "JOIN media_parts mp ON mp.media_item_id = mi.id "
                        "WHERE mi.metadata_item_id = ? ORDER BY mi.id, mp.id "
                        "LIMIT 1 OFFSET ?", (int(ratingkey), max(0, int(offset))))
                    row = cur.fetchone()
                    return row[0] if row else None
                finally:
                    c.close()
            except Exception:
                return None

        def _start_flow(self):
            """官方客户端（iOS/AppleCoreMedia 等）对 protocol=hls 一律先请求
            /video/:/transcode/universal/start.m3u8 建 HLS 会话；真 Plex 侧 strm 的
            转码会话必被 TPU 拒（"session lacking permission to transcode" → 400，
            本机历史 0 成功）。plex-demo 时代 start.m3u8 与 parts/file 同挂一个
            redirect 处理器，客户端跟 302 拿真实字节流自行 probe，播放正常。
            这里同样 302 到相对 part URL（客户端按当前连接自动拼 host/scheme）。"""
            try:
                _, _, qs = self.path.partition("?")
                q = urllib.parse.parse_qs(qs)
                m = re.search(r"/library/metadata/(\d+)", (q.get("path") or [""])[0])
                if not m:
                    self._proxy_pass()
                    return
                mi = int((q.get("mediaIndex") or ["0"])[0] or 0)
                pi = int((q.get("partIndex") or ["0"])[0] or 0)
                rk = int(m.group(1))
                partid = self._db_lookup_part(rk, mi + pi)
                if not partid:
                    log_event("passthrough", player="plex", item=rk,
                              path=self.path[:160],
                              ua=(self.headers.get("User-Agent") or "")[:120], status=0,
                              note="start.m3u8 未解析到 partid，退回透传")
                    self._proxy_pass()
                    return
                ts = 0
                try:
                    c = sqlite3.connect(f"file:{backend.get('db_path')}?mode=ro", uri=True)
                    try:
                        cur = c.cursor()
                        cur.execute("SELECT added_at FROM metadata_items WHERE id=?",
                                    (rk,))
                        row = cur.fetchone()
                        ts = int(row[0]) if row and row[0] else 0
                    finally:
                        c.close()
                except Exception:
                    pass
                tok = (q.get("X-Plex-Token") or [""])[0] or _PLEX_TOKEN or ""
                loc = f"/library/parts/{partid}/{ts}/file"
                if tok:
                    loc += "?X-Plex-Token=" + tok
                ua = self.headers.get("User-Agent") or ""
                print(f"[proxy] plex start.m3u8 拦截 rk={rk} part={partid} → 302"
                      f"（UA={ua[:40]}）", flush=True)
                log_event("media", player="plex", item=partid, path=self.path[:160],
                          ua=ua[:120], status=302,
                          note="start.m3u8 拦截 → part 换链")
                self._redirect(loc)
            except Exception:
                self._proxy_pass()

        # ---- 统一入口 ----
        def _dispatch(self):
            # WebSocket 升级请求（Plex Web 的 /:/websockets/...）单独走隧道
            up = (self.headers.get("Upgrade") or "").lower()
            if up == "websocket":
                self._proxy_websocket()
                return
            # 裸根路径「/」→ 302 跳到 /web/（恢复「打开端口自动进网页」的体验，
            # 否则浏览器会直接显示 Plex 的 XML 根响应）
            if self.command in ("GET", "HEAD"):
                rp = self.path.split("?", 1)[0]
                if rp in ("/", ""):
                    self.send_response(302)
                    self.send_header("Location", "/web/")
                    self.send_header("Content-Length", "0")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    return
            # 媒体流：/library/parts/<partid>/<n>/file
            m = _PART_RE.match(self.path.split("?", 1)[0])
            if m and self.command in ("GET", "HEAD"):
                self._part_flow(m.group(1))
                return
            # start.m3u8：官方客户端 HLS 会话入口——真 Plex 侧 strm 转码会话
            # 必被 TPU 拒（历史 0 成功），拦截 302 到 part 换链（plex-demo 同款）
            rp2 = self.path.split("?", 1)[0]
            if ("/video/:/transcode/universal/start.m3u8" in rp2
                    and self.command in ("GET", "HEAD")):
                self._start_flow()
                return
            # 其余（页面/API/海报/转码/字幕…）整段透传真 Plex
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


def _backend_from_cfg(cfg):
    return {
        "kind": "plex",
        "base": (cfg.get("plex_base") or "http://127.0.0.1:32400").strip(),
        "db_path": (cfg.get("plex_db_path") or "").strip(),
        "local_prefix": (cfg.get("plex_local_prefix") or "").strip(),
        "remote_prefix": (cfg.get("plex_remote_prefix") or "").strip(),
        "public_base": (cfg.get("plex_public_base") or "").strip(),
        "strm_prefix": (cfg.get("plex_strm_prefix") or "/strm/").strip(),
        "link_prefix": (cfg.get("plex_link_prefix") or "/Link/").strip(),
        # strm 格式：auto（按第一行自动判断，默认）/ path（强制 CD2 路径型）/ pc（强制 pickcode 型）
        "strm_mode": str(cfg.get("plex_strm_mode") or cfg.get("strm_mode") or "auto").lower(),
    }


_TLS_DIR = "/app/data/plex_proxy_tls"
# 2026-10-27 实测 plex.tv 发布的 plex.direct machine 段（plex.tv 内部算法，不可本地复现）
_TLS_MACH_FALLBACK = "0cefd5b3f96349dfbda35dd406d9609e"
_TLS_EXTRA_DNS = ("plex.7359577.xyz",)


def _host_ip():
    """运行 EvoLink 这台机器在局域网里的 IPv4。

    优先级：``EVOLINK_HOST_IP`` 环境变量 > 自动探测出口网卡 IP > 127.0.0.1。

    plex.direct 自签证书的 SAN 必须写这个 IP（plex.tv 按 IP 发布 https 候选域名），
    换机器部署时用环境变量固定即可，不必改源码。
    """
    env = (os.environ.get("EVOLINK_HOST_IP") or "").strip()
    if env:
        return env
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = (s.getsockname()[0] or "").strip()
        finally:
            s.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return "127.0.0.1"


def _plex_direct_mach():
    """plex.direct 域名里的 machine 段：从 plex.tv resources API 解析真实值，
    拿不到（离线/无 token）就回退实测缓存。"""
    try:
        tok = (_PLEX_TOKEN or "").strip()
        if tok:
            req = urllib.request.Request(
                "https://plex.tv/api/resources?includeHttps=1&includeRelay=0",
                headers={"X-Plex-Token": tok, "User-Agent": "EvoLink/1.0",
                         "Accept": "application/xml"})
            with urllib.request.urlopen(req, timeout=6) as r:
                body = r.read().decode("utf-8", "replace")
            hip = _host_ip()
            m = re.search(r'address="%s"[^>]*' % re.escape(hip)
                          + r'(?:uri="https://([\w.-]+\.plex\.direct):\d+")'
                          + r'|address="%s\.([\w.-]+)\.plex\.direct"' % re.escape(hip.replace(".", "-")), body)
            if not m:
                m2 = re.search(r'(\w{32})\.plex\.direct', body)
                if m2:
                    return m2.group(1)
            elif m.group(1):
                return m.group(1).split(".")[0]
            elif m.group(2):
                return m.group(2)
    except Exception:
        pass
    return _TLS_MACH_FALLBACK


def _ensure_tls_ctx():
    """生成/加载自签证书。plex.tv 只给客户端发布 https 候选（plex.direct 域名），
    而本反代原先是纯 http，客户端 TLS 握手全灭直接 s1001。Plex 客户端对
    plex.direct 域名不验 CA（官方机制），自签证书即可让 https 候选可用。"""
    cert = os.path.join(_TLS_DIR, "cert.pem")
    key = os.path.join(_TLS_DIR, "key.pem")
    try:
        os.makedirs(_TLS_DIR, exist_ok=True)
        if not (os.path.exists(cert) and os.path.exists(key)):
            mh = _plex_direct_mach()
            # plex.direct 证书的 SAN 必须写运行 Plex 的那台机器的 IP。
            host_ip = _host_ip()
            ip_dash = host_ip.replace(".", "-")
            san = ",".join([
                "DNS:%s.%s.plex.direct" % (ip_dash, mh),
                "DNS:*.%s.plex.direct" % mh,
                "DNS:%s.%s.plex.direct" % (ip_dash, _TLS_MACH_FALLBACK),
                "DNS:*.%s.plex.direct" % _TLS_MACH_FALLBACK,
            ] + ["DNS:" + d for d in _TLS_EXTRA_DNS] + ["IP:" + host_ip])
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                 "-keyout", key, "-out", cert, "-days", "3650",
                 "-subj", "/CN=%s.plex.direct" % ip_dash,
                 "-addext", "subjectAltName=" + san],
                check=True, capture_output=True, timeout=60)
            print("[proxy] TLS 自签证书已生成（SAN 覆盖 plex.direct/%s）" % mh, flush=True)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        return ctx
    except Exception as e:
        print(f"[proxy] TLS 不可用，退回纯 http：{e}", flush=True)
        return None


class _DualStackServer(ThreadingHTTPServer):
    """http/https 同端口：peek 连接首字节，0x16 = TLS ClientHello 则按 SSL 处理。"""
    ssl_ctx = None

    def get_request(self):
        sock, address = self.socket.accept()
        if self.ssl_ctx is not None:
            try:
                sock.settimeout(10)
                if sock.recv(1, socket.MSG_PEEK) == b"\x16":
                    sock = self.ssl_ctx.wrap_socket(sock, server_side=True)
            except (OSError, ValueError):
                try:
                    sock.close()
                except Exception:
                    pass
                raise  # OSError 会被 _handle_request_noblock 吞掉，不伤主循环
        return sock, address


def start_proxy(name, backend, port):
    """起 Plex 反代（后台线程）。端口注册进 reverse_proxy._SERVERS/_ASSIGNED，
    因此 /api/proxy/status、_stop_port、改端口防泄漏都与 Jellyfin/Emby 同一套。"""
    from http.server import ThreadingHTTPServer

    try:
        port = int(port)
    except (TypeError, ValueError):
        print(f"[proxy] {name} 端口无效：{port}", flush=True)
        return False
    with reverse_proxy._ASSIGNED_LOCK:
        old = reverse_proxy._ASSIGNED.get(name)
    if old is not None and old != port:
        reverse_proxy._stop_port(old)
    reverse_proxy._stop_port(port)
    try:
        srv = _DualStackServer(("0.0.0.0", port), _make_handler(backend))
        srv.ssl_ctx = _ensure_tls_ctx()
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        with reverse_proxy._SERVERS_LOCK:
            reverse_proxy._SERVERS[port] = (srv, t)
        with reverse_proxy._ASSIGNED_LOCK:
            reverse_proxy._ASSIGNED[name] = port
        print(f"[proxy] {name} 反代已监听 :{port} -> {backend['base']}", flush=True)
        return True
    except Exception as e:
        print(f"[proxy] {name} 反代启动失败（端口 {port}）：{e}", flush=True)
        return False


def stop_proxy(name):
    with reverse_proxy._ASSIGNED_LOCK:
        old = reverse_proxy._ASSIGNED.get(name)
    if old:
        reverse_proxy._stop_port(old)
    with reverse_proxy._ASSIGNED_LOCK:
        reverse_proxy._ASSIGNED.pop(name, None)


def _meta_inject_on():
    """「Metadata 替换」开关（设置页 PLEX 板块）：控制 parts key 绝对化 +
    decision 强制直拨。关 = 反代退化为纯地址透传，官方客户端无法直拨 strm。默认开。"""
    try:
        return config.load().get("plex_meta_inject", True) is not False
    except Exception:
        return True


def reload_all(cfg=None):
    """按当前 config 重建 Plex 反代（设置页「应用并重启反代」热生效，免容器重启）。"""
    cfg = cfg or config.load()
    port = int(cfg.get("plex_port") or 0)
    backend = _backend_from_cfg(cfg)
    # 32411 = 主 Plex 反代（2026-10-27 起 32401 plex-demo legacy 口已下线）
    if cfg.get("plex_enabled") and port > 0:
        start_proxy("plex", backend, port)
        try:  # 清掉历史遗留的 32401 监听
            reverse_proxy._stop_port(32401)
        except Exception:
            pass
    else:
        if port > 0:
            reverse_proxy._stop_port(port)
        reverse_proxy._stop_port(32401)
        stop_proxy("plex")


def status():
    with reverse_proxy._ASSIGNED_LOCK:
        port = reverse_proxy._ASSIGNED.get("plex")
    return {"enabled": bool(config.get("plex_enabled")),
            "port": port, "running": port in reverse_proxy.running_ports()}


# 自动从 Plex 配置读取 token：让局域网客户端免登录即可用 32411 Web/API
_PLEX_TOKEN = None


def _load_plex_token():
    import glob
    for pat in ("/plex_data/**/Preferences.xml",
                "/vol1/1000/docker/plex/**/Preferences.xml"):
        for f in glob.glob(pat, recursive=True):
            try:
                t = open(f, encoding="utf-8", errors="ignore").read()
            except Exception:
                continue
            m = re.search(r'PlexOnlineToken="([^"]*)"', t)
            if m and m.group(1):
                return m.group(1)
    return None


_PLEX_TOKEN = _load_plex_token()
