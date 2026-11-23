"""RE0 直连通道：账号密码 + CloakBrowser 真实登录 re0.me，查资源 / 解锁 / 预览 / 签到。

与兜底借道（core/hdhub.py）并行的第二条 RE0 通道：
- 兜底借道：复用聚合检索服务里已授权好的上游资源站账号，冷启动 20-40s
- 本模块直连：自己的 re0.me 账号，浏览器常驻会话持久化，没有 OpenAPI 申请门槛
  （2026-10-14 起 RE0 停止开放 OpenAPI 申请，且只对 V 用户开放，网页登录是唯一路）

技术要点（照 115-Station internal/api/re0web.go 重写成 Python）：
- 站点做了加密会话绑定，接口不能脱离浏览器单独调 → 一切操作都在页面里发生
- 站点校验点击的 isTrusted → 点击一律走 Playwright 的 click / mouse.click（CDP 输入事件）
- 资源列表从 Next.js 的 RSC 数据（self.__next_f.push 脚本块）里读，不执行页面脚本
- 文件预览借站点页面 webpack 模块里的 signedFetch（普通 fetch 带 Cookie 也 401）
- 浏览器是 CloakBrowser（core/re0browser.py 管理），Playwright 非线程安全 →
  所有浏览器操作全局串行，在单个 worker 线程里执行（ThreadPoolExecutor(max_workers=1)）
"""
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from . import config
from . import re0browser

RE0_DEFAULT_BASE = "https://re0.me"
LOGIN_PATH = "/login"
BROWSER_IDLE_SECONDS = 600       # 浏览器空闲 10 分钟关掉（登录态在 profile 里，不丢）
SEARCH_WAIT_SECONDS = 50         # 等资源列表流式输出（页面常先渲染「加载中」骨架 + 首屏下懒加载，要给它时间）
UNLOCK_WAIT_SECONDS = 30         # 点了解锁等分享链接
CF_WAIT_SECONDS = 90             # 过 Cloudflare 最长等待


class Re0Error(RuntimeError):
    pass


# ---------------- RSC 数据解析（self.__next_f.push） ----------------

# 读页面内嵌的 Next.js 数据分片，不执行脚本
_CHUNKS_JS = """() => [...document.scripts].flatMap(s => {
	const t = (s.textContent || '').trim(), pre = 'self.__next_f.push(';
	if (!t.startsWith(pre)) return [];
	try {
		const c = JSON.parse(t.slice(pre.length, t.lastIndexOf(')')));
		return c[0] === 1 && typeof c[1] === 'string' ? [c[1]] : [];
	} catch { return []; }
})"""

_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
_REF_RE = re.compile(r"^\$[0-9a-fA-F]+$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_HREF_RE = re.compile(r"/resource/115/([A-Za-z0-9_-]+)/?$")
_EMPTY_MSG_RE = re.compile(r"(?i)资源|resource")


class _Unresolved:
    pass


def _resolve(v, records, seen=None):
    """RSC 引用解析：值是 "$3a" 这样的字符串时去 records 里取，带环保护。"""
    if not isinstance(v, str) or not _REF_RE.match(v):
        return v
    k = v[1:]
    if k not in records:
        return _Unresolved()
    seen = seen or set()
    if k in seen:
        return _Unresolved()
    return _resolve(records[k], records, seen | {k})


def _walk_maps(v):
    """产出一棵树里所有的 dict（深度优先，防爆炸用显式栈）。"""
    stack = [v]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            yield x
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)


def parse_page_resources(raw):
    """从 Next.js RSC 数据里找资源分组（带 groupData 的对象，按网盘分组，取 115）。

    数据是「十六进制id:JSON」一行一条，值里的 "$3a" 引用另一条。
    返回 (rows, ok)；ok=False 表示还没读全（有引用解析不出来），调用方接着等。
    """
    records = {}
    for line in raw.split("\n"):
        key, sep, val = line.partition(":")
        if not sep or not _HEX_RE.match(key):
            continue
        try:
            records[key] = json.loads(val)
        except Exception:
            continue

    groups, empty = [], False
    for rec in records.values():
        for m in _walk_maps(rec):
            gd = m.get("groupData")
            if gd is not None:
                g = _resolve(gd, records)
                if isinstance(g, _Unresolved):
                    return None, False
                if isinstance(g, dict):
                    groups.append(g)
            if _empty_marker(m, records):
                empty = True
    if not groups:
        # 没资源的片不给 groupData，而是给空状态组件
        return [], empty
    rows = []
    for g in groups:
        lst = _resolve(g.get("115"), records)
        if isinstance(lst, _Unresolved):
            return None, False
        if g.get("115") is None:
            continue  # 这一组没有 115 资源
        if not isinstance(lst, list):
            return None, False
        for r in lst:
            rv = _resolve(r, records)
            if isinstance(rv, _Unresolved):
                return None, False
            if not isinstance(rv, dict):
                continue
            full = {}
            for k, v in rv.items():
                x = _resolve(v, records)
                if isinstance(x, _Unresolved):
                    return None, False
                full[k] = x
            rows.append(full)
    return normalize_rows(rows), True


def _empty_marker(m, records):
    """网盘一栏的空状态：{"kind":"empty","error":{"message":"暂无资源",…}}。"""
    if m.get("kind") != "empty":
        return False
    e = _resolve(m.get("error"), records)
    if not isinstance(e, dict):
        return False
    msg = str(e.get("message") or "")
    return bool(_EMPTY_MSG_RE.search(msg))


def _s(v):
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (int, float)):
        return str(v)
    return ""


def _sl(v):
    if isinstance(v, list):
        return [x for x in (_s(i) for i in v) if x]
    s = _s(v)
    return [s] if s else []


def _loose_int(v):
    try:
        return int(float(v)), True
    except Exception:
        return 0, False


def normalize_rows(rows):
    """页面数据 → 统一资源结构（与兜底通道对齐，前端复用）。只留 115 与 ed2k。"""
    out, seen = [], set()
    for r in rows:
        pan = "115"
        w = r.get("website")
        if w is not None:
            ws = str(w).lower()
            if ws in ("115", "ed2k"):
                pan = ws
            else:
                continue
        slug = r.get("slug") if isinstance(r.get("slug"), str) else ""
        if not _SLUG_RE.match(slug or ""):
            m = _HREF_RE.search(str(r.get("href") or ""))
            if not m:
                continue
            slug = m.group(1)
        if slug in seen:
            continue
        seen.add(slug)
        points, _ = _loose_int(r.get("unlock_points"))
        item = {
            "title": _s(r.get("remark")) or _s(r.get("title")) or "未命名",
            "slug": slug,
            "pan": pan,
            "size": _s(r.get("share_size")) or _s(r.get("size")),
            "resolution": " / ".join(_sl(r.get("video_resolution"))),
            "source": " / ".join(_sl(r.get("source"))),
            "sub_lang": " / ".join(_sl(r.get("subtitle_language"))),
            "sub_type": " / ".join(_sl(r.get("subtitle_type"))),
            "remark": _s(r.get("remark")),
            "points": points or 0,
            "unlocked": any(bool(r.get(k)) for k in
                            ("is_unlocked", "unlocked", "is_owned", "already_owned")),
            "created_at": _s(r.get("submitted_at")) or _s(r.get("created_at")) or _s(r.get("posted_at")),
            "url": "",
            "channel": "re0-direct",
        }
        if not item["resolution"]:
            item["resolution"] = _s(r.get("resolution"))
        out.append(item)
    return out


def parse_json_resources(body):
    """兜底：页面自己发的 JSON 响应里也可能带着资源列表（{data:[…]}）。"""
    try:
        env = json.loads(body)
    except Exception:
        return []
    data = env.get("data") if isinstance(env, dict) else None
    return normalize_rows(data if isinstance(data, list) else [])


# ---------------- 页面 JS ----------------

# 从页面上找 115 分享链接（链接 / 输入框 / 正文），ED2K 资源找 ed2k 链接
_SHARE_JS = """() => {
	const ok = v => {
		try {
			const u = new URL(v);
			return ['http:', 'https:'].includes(u.protocol) && ['115.com', '115cdn.com', 'anxia.com'].includes(u.hostname) &&
				/^\\/s\\/[^/\\s]+\\/?$/.test(u.pathname) && !u.username && !u.password;
		} catch { return false; }
	};
	for (const el of document.querySelectorAll('a[href], input, textarea')) {
		const v = el.tagName === 'A' ? el.href : el.value;
		if (v && ok(v.trim())) return v.trim();
	}
	for (const v of (document.body?.innerText || '').match(/https?:\\/\\/[^\\s<>"']+/g) || []) if (ok(v)) return v;
	for (const v of (document.body?.innerText || '').match(/ed2k:\\/\\/\\|file\\|[^\\r\\n]+/g) || []) return v;
	return '';
}"""

# 站点自己的 signedFetch：会话握手、请求签名都在里面，Go/Python 侧重写不了，只能借。
# 它在 webpack 模块里（导出名 signedFetch），运行时没暴露模块缓存，只有工厂表 .m：
# 按源码里的「signedFetch:()=>」找到模块 id 再 require。
_SIGNED_JS = """async (path, body) => {
  let req
  try {
    const chunks = self.webpackChunk_N_E
    if (!chunks) return { status: 0, text: '页面里没有 webpack 运行时（站点可能改版）' }
    chunks.push([[Symbol('s115')], {}, (r) => { req = r }])
  } catch (e) { return { status: 0, text: '拿不到 webpack 运行时：' + e } }
  let sf
  for (const id of Object.keys((req && req.m) || {})) {
    let src = ''
    try { src = String(req.m[id]) } catch {}
    if (!src.includes('signedFetch:()=>')) continue
    try { const ex = req(id); if (ex && typeof ex.signedFetch === 'function') { sf = ex.signedFetch; break } } catch {}
  }
  if (!sf) return { status: 0, text: '页面里找不到站点的 signedFetch（站点可能改版）' }
  const get = body == null
  const headers = () => {
    const h = new Headers({ 'Accept': 'application/json, text/plain, */*' })
    if (!get) h.set('Content-Type', 'application/json')
    const m = document.cookie.match(/(?:^|;\\s*)csrf_access_token=([^;]+)/)
    if (m) h.set('X-CSRF-TOKEN', decodeURIComponent(m[1]))
    return h
  }
  const send = () => sf(path, get ? { method: 'GET', credentials: 'include', headers: headers() }
    : { method: 'POST', credentials: 'include', headers: headers(), body: JSON.stringify(body) })
  try {
    let r = await send()
    if (r.status === 401) {
      let code = ''
      try {
        const j = await r.clone().json()
        code = typeof j.code === 'string' ? j.code : typeof j.error_code === 'string' ? j.error_code : ''
      } catch {}
      if (!code) {
        const rr = await sf('/api/public/auth/refresh', { method: 'POST', credentials: 'include', cache: 'no-store',
          headers: { 'Content-Type': 'application/json', 'x-skip-auth-refresh': 'true' } })
        if (rr.ok) r = await send()
      }
    }
    return { status: r.status, text: await r.text() }
  } catch (e) { return { status: 0, text: '' + e } }
}"""

# Cloudflare 挑战页检测
_BODY_TEXT_JS = "() => (document.body && document.body.innerText || '').trim()"

# 正文里出现这些词才算「RE0 确实没有这部片」；页面还停在加载态时不算。
_EMPTY_HINTS = ("暂无资源", "暂无片源", "还没有资源", "还没有人发布", "没有找到资源",
                "未找到资源", "资源正在路上", "No resources")

# 加载中的痕迹：出现就不算空
_LOADING_HINTS = ("加载中", "Loading", "正在加载")


def page_says_empty(body):
    """正文是否明确在说「没有资源」。加载中/骨架屏一律不算。"""
    if not body:
        return False
    if any(h in body for h in _LOADING_HINTS):
        return False
    return any(h in body for h in _EMPTY_HINTS)


_CHALLENGE_JS = """() => Boolean(
	window._cf_chl_opt ||
	document.querySelector('#challenge-running, #challenge-form') ||
	Array.prototype.some.call(document.querySelectorAll('iframe'), function (f) {
		return (f.getAttribute('src') || '').indexOf('challenges.cloudflare.com') >= 0;
	})
)"""

# 登录错误提示（toast / alert）
_LOGIN_HINT_JS = """() => {
	for (const sel of ['[role="alert"]', '[data-slot="toast"]', '.error', '[class*="error"]', '[class*="Error"]']) {
		const el = document.querySelector(sel);
		const t = el && (el.innerText || '').trim();
		if (t) return t;
	}
	return '';
}"""

# 受控输入框（React）的值没留住时：用原生 setter 写值再发 input / change
_NATIVE_SET_JS = """function(v) {
	const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
	setter.call(this, v);
	this.dispatchEvent(new Event('input', {bubbles: true}));
	this.dispatchEvent(new Event('change', {bubbles: true}));
}"""

_ED2K_RE = re.compile(r"^ed2k://\|file\|[^|\r\n]+\|\d+\|[0-9a-fA-F]{32}\|")


# ---------------- 身份槽（per-user 隔离） ----------------
#
# 每个身份槽（uid）独立保存 cookies / proxy / 账号密码，并拥有独立的
# 浏览器 profile 目录与 worker 线程池 —— 两个 uid 的登录态与出口代理互不串扰。

def _safe_uid(uid):
    """uid 只允许字母数字下划线横线，防目录穿越。"""
    return re.sub(r"[^A-Za-z0-9_-]", "", str(uid or ""))[:40]


def _users_map(cfg=None):
    """config["re0_users"] → {uid: {cookies,proxy,user,pass,name}}；兼容旧全局键。"""
    cfg = cfg if cfg is not None else config.load()
    users = cfg.get("re0_users")
    if not isinstance(users, dict):
        users = {}
    if not users:
        # 旧单槽数据迁移：有内容才建 default 槽，空配置不建（避免假槽）
        legacy = {
            "cookies": (cfg.get("re0_direct_cookies") or "").strip(),
            "proxy": (cfg.get("re0_direct_proxy") or "").strip(),
            "user": (cfg.get("re0_direct_user") or "").strip(),
            "pass": cfg.get("re0_direct_pass") or "",
        }
        if any(str(v).strip() for v in legacy.values()):
            users = {"default": dict(legacy, name="默认身份")}
    return users


def _slot_cfg(cfg, uid):
    """取某个身份槽的配置（不存在返回空字典）。"""
    slot = _users_map(cfg).get(uid) or {}
    return slot if isinstance(slot, dict) else {}


def _slot_proxy(cfg, slot):
    """本身份访问 re0.me 用的代理 → (地址, 来源)。

    留空就跟随「基础设置 → HTTP 代理」（那是全站统一出口，re0 直连也吃它），
    只有显式填了才用自己那条（多租户场景：朋友各走各的机场）。
    来源用于 UI 提示：slot=单独指定 / global=跟随全局 / none=直连。
    """
    p = (slot.get("proxy") or "").strip()
    if p:
        return p, "slot"
    try:
        g = (config.proxy_url(cfg) or "").strip()
    except Exception:
        g = ""
    return g, ("global" if g else "none")


_CLIENTS = {}
_CLIENTS_LOCK = threading.Lock()


def get_re0_client(uid="default"):
    """按 uid 取客户端；不存在则新建（每 uid 一个浏览器 + 一个 profile 目录）。"""
    uid = _safe_uid(uid) or "default"
    with _CLIENTS_LOCK:
        c = _CLIENTS.get(uid)
        if c is None:
            c = Re0Client(uid=uid)
            _CLIENTS[uid] = c
        else:
            c._reload_cfg()
        return c


def drop_re0_client(uid):
    """删除身份槽时回收客户端：关浏览器 + 停线程池。"""
    uid = _safe_uid(uid) or "default"
    with _CLIENTS_LOCK:
        c = _CLIENTS.pop(uid, None)
    if c is None:
        return
    try:
        c._submit(c._close_browser, timeout=20)
    except Exception:
        pass
    try:
        c._exec.shutdown(wait=False)
    except Exception:
        pass


# ---------------- 客户端 ----------------

class Re0Client:
    """所有浏览器操作全局串行，在单个 worker 线程里执行（Playwright 非线程安全）。"""

    def __init__(self, uid="default", cfg=None):
        self.uid = _safe_uid(uid) or "default"
        cfg = cfg if cfg is not None else config.load()
        self.base = (cfg.get("re0_base") or RE0_DEFAULT_BASE).rstrip("/")
        slot = _slot_cfg(cfg, self.uid)
        self.user = (slot.get("user") or "").strip()
        self.pwd = slot.get("pass") or ""
        self.proxy, self._proxy_source = _slot_proxy(cfg, slot)
        self.cookies = (slot.get("cookies") or "").strip()
        self.profile_dir = str(config.DATA / f"re0_profile_{self.uid}")
        self._exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="re0")
        self._pw = None
        self._ctx = None
        self._last_use = 0.0
        self._janitor_started = False
        self._headless = True       # 当前上下文是"无头"还是"有画面"
        self._desktop = False       # 是否处于手动登录（可视）会话中

    # ---------- 配置 ----------
    def configured(self):
        # 有账号密码（自动登录）或已交来登录态 cookie（用户自己登录）都算就绪
        return bool((self.user and self.pwd) or self.cookies)

    def _reload_cfg(self):
        """让设置页的改动能生效（不重建线程池与会话目录）。按自己的 uid 读槽。"""
        try:
            cfg = config.load()
            self.base = (cfg.get("re0_base") or RE0_DEFAULT_BASE).rstrip("/")
            slot = _slot_cfg(cfg, self.uid)
            self.user = (slot.get("user") or "").strip()
            self.pwd = slot.get("pass") or ""
            self.proxy, self._proxy_source = _slot_proxy(cfg, slot)
            self.cookies = (slot.get("cookies") or "").strip()
        except Exception:
            pass

    # ---------- cookie 注入（绕 Cloudflare） ----------
    @staticmethod
    def _parse_cookies(s):
        """'a=1; b=2' → Playwright add_cookies 需要的列表（re0.me 域）。"""
        out = []
        for part in (s or "").split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            k, v = part.split("=", 1)
            k, v = k.strip(), v.strip()
            if not k:
                continue
            out.append({"name": k, "value": v, "domain": "re0.me", "path": "/"})
        return out

    def _inject_cookies(self):
        """把用户真实浏览器交来的 cf_clearance + 会话 cookie 注入当前上下文。
        Cloudflare 看到合法 clearance 即放行，不再卡「请稍候」，也无头改有头。"""
        if not self.cookies:
            return 0
        ck = self._parse_cookies(self.cookies)
        if not ck:
            return 0
        try:
            self._ctx.add_cookies(ck)
            self._log(f"已注入 {len(ck)} 个 cookie（含 cf_clearance）绕过 Cloudflare")
            return len(ck)
        except Exception as e:
            self._log(f"注入 cookie 失败：{e}")
            return 0

    # ---------- worker 线程内的浏览器生命周期 ----------
    def _ensure_browser(self, headless=True):
        """（worker 线程）起 persistent context，已起着就复用。
        headless=False 时需要有 X 显示（DISPLAY，通常是 :99 上的 Xvfb），
        用于"手动登录会话"——用户能看见并操作浏览器。"""
        self._last_use = time.time()
        if self._ctx is not None:
            try:
                self._ctx.pages  # 探一下
                if getattr(self, "_headless", True) == headless:
                    return
            except Exception:
                pass
            # 模式变了（无头↔有画面）：同一 profile 不能两个进程同时开，先关
            self._close_browser_locked()
        binp, ok = re0browser.installed()
        if not ok:
            re0browser.ensure("RE0 直连需要浏览器")
            raise Re0Error("CloakBrowser 还没准备好（正在下载，约 210MB），稍后再试")
        from playwright.sync_api import sync_playwright
        if self._pw is None:
            self._pw = sync_playwright().start()
        os.makedirs(self.profile_dir, exist_ok=True)
        args = re0browser.launch_args(self.proxy)
        env = None
        if not headless:
            disp = (os.environ.get("DISPLAY") or "").strip() or ":99"
            os.environ["DISPLAY"] = disp
            env = dict(os.environ)
            env["DISPLAY"] = disp
            args += ["--window-size=1280,820", "--window-position=0,0"]
        try:
            self._ctx = self._pw.chromium.launch_persistent_context(
                self.profile_dir,
                executable_path=binp,
                headless=headless,
                args=args,
                env=env,
                viewport={"width": 1366, "height": 900} if headless else None,
                ignore_default_args=["--enable-automation"],
            )
        except Exception as e:
            raise Re0Error(f"浏览器启动失败: {e}")
        self._headless = headless
        # 启动后立即注入用户真实浏览器交来的 cookie（cf_clearance 等），绕过 CF
        self._inject_cookies()

    def _ensure_browser_auto(self):
        """后台操作（搜索 / 预览 / 解锁 / 签到）用：已有可见会话就复用它。

        之前这些路径一律 `_ensure_browser()`（默认 headless=True），窗口开着时
        会把可视会话关掉重建 —— 用户正在 noVNC 里登录，点一下搜索窗口就没了。
        """
        if self._ctx is not None and not getattr(self, "_headless", True):
            return self._ensure_browser(headless=False)
        return self._ensure_browser(headless=True)

    def _close_browser_locked(self):
        for closer, attr in ((lambda: self._ctx.close(), "_ctx"), (lambda: self._pw.stop(), "_pw")):
            obj = getattr(self, attr)
            if obj is not None:
                try:
                    closer()
                except Exception:
                    pass
                setattr(self, attr, None)

    def _close_browser(self, why=""):
        """（worker 线程）关浏览器。登录态在 profile 里，不丢。"""
        self._close_browser_locked()

    def close_idle(self):
        """空闲超时关浏览器（janitor 调）。手动登录会话不自动关。"""
        if getattr(self, "_desktop", False):
            return
        if self._ctx is not None and time.time() - self._last_use > BROWSER_IDLE_SECONDS:
            self._submit(self._close_browser, timeout=30)

    # ---------- 手动登录（可视会话，配 Xvfb + noVNC） ----------
    @staticmethod
    def _safe_title(page):
        try:
            return page.title()
        except Exception:
            return ""

    def open_desktop(self):
        """开一个有画面的浏览器，供用户在网页 VNC 里点验证、用 Telegram 登录。"""
        self._ensure_browser(headless=False)
        page = self._page()
        try:
            if not (page.url or "").startswith("http"):
                page.goto(self.base, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            self._log(f"可视会话打开页面失败：{e}")
        self._desktop = True
        self._last_use = time.time()
        return {"ok": True, "url": page.url, "title": self._safe_title(page)}

    def desktop_touch(self):
        """（worker）刷新活跃时间并回报当前页面，防止可视会话被判定空闲。"""
        self._last_use = time.time()
        page = self._page()
        return {"ok": True, "url": page.url, "title": self._safe_title(page)}

    def grab_cookies(self):
        """把浏览器里 re0.me 的全部 cookie（含 HttpOnly）导出并写进当前身份槽。"""
        self._ensure_browser(headless=False)
        self._last_use = time.time()
        try:
            cks = self._ctx.cookies()
        except Exception:
            cks = []
        mine = [c for c in cks if "re0.me" in (c.get("domain") or "")]
        use = mine or cks
        if not use:
            return {"ok": False, "error": "浏览器里还没有 cookie（登录可能没成功）", "count": 0}
        s = "; ".join(f"{c['name']}={c['value']}" for c in use)
        cfg = config.load()
        users = _users_map(cfg)
        slot = users.get(self.uid)
        if not isinstance(slot, dict):
            slot = {"name": self.uid, "user": "", "pass": "", "proxy": ""}
            users[self.uid] = slot
        slot["cookies"] = s
        cfg["re0_users"] = users
        if self.uid == "default":
            cfg["re0_direct_cookies"] = s
        config.save(cfg)
        self.cookies = s
        names = [c["name"] for c in use]
        return {"ok": True, "count": len(use), "names": names,
                "has_clearance": "cf_clearance" in names}

    def close_desktop(self):
        """关掉可视会话（登录态留在 profile 里，不丢）。"""
        self._desktop = False
        self._close_browser_locked()
        return {"ok": True}

    def start_janitor(self):
        if self._janitor_started:
            return
        self._janitor_started = True
        t = threading.Thread(target=self._janitor, daemon=True)
        t.start()

    def _janitor(self):
        while True:
            time.sleep(60)
            try:
                self.close_idle()
            except Exception:
                pass

    # ---------- RPC ----------
    def _submit(self, fn, timeout=180):
        """把操作投递给唯一 worker 线程执行，等结果。

        自愈（2026-10-27）：worker 是单线程，一旦卡死在某个 Playwright 同步调用里
        （CDP 连接断而不关时 page.screenshot/goto 会永久挂起），整个队列就永久堵塞，
        之后所有请求都停在「正在查询」。超时后不再只报错，而是重建 worker + 杀浏览器。
        """
        self._reload_cfg()
        self.start_janitor()
        fut = self._exec.submit(fn)
        try:
            return fut.result(timeout=timeout)
        except TimeoutError:
            self._recover_wedged()
            raise Re0Error("RE0 操作超时（后台已自动重建浏览器通道，请重试一次）")
        except Re0Error:
            raise
        except Exception as e:
            raise Re0Error(f"{type(e).__name__}: {e}")

    def _recover_wedged(self):
        """worker 被卡死的 Playwright 调用堵死时的自愈：
        1) pkill 掉占用本 profile 的浏览器进程 —— CDP 断连会让卡住的调用立刻抛错返回，
           旧线程随即脱困闲置（它引用的 future 已被放弃，不会再干扰别人）；
        2) 换一个新的单线程池接管后续任务；
        3) 作废旧 pw/ctx 句柄，下次 _ensure_browser 重新起浏览器（登录态在 profile，不丢）。"""
        try:
            self._log("worker 疑似卡死，自动重建：杀浏览器进程 + 换 worker 线程")
        except Exception:
            pass
        try:
            subprocess.run(["pkill", "-f", f"--user-data-dir={self.profile_dir}"],
                           capture_output=True, timeout=10)
        except Exception:
            pass
        try:
            old = self._exec
            self._exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="re0")
            old.shutdown(wait=False)
        except Exception:
            pass
        self._pw = None
        self._ctx = None

    # ---------- 页面基础 ----------
    def _page(self):
        if self._ctx.pages:
            p = self._ctx.pages[0]
        else:
            p = self._ctx.new_page()
        return p

    def _on_login(self, page):
        try:
            u = urllib.parse.urlparse(page.url)
            return u.path.rstrip("/") == LOGIN_PATH
        except Exception:
            return False

    def _wait_cloudflare(self, page):
        """有 Cloudflare 验证页就等它过（最长 90 秒），期间试地点验证框。"""
        deadline = time.time() + CF_WAIT_SECONDS
        detected, clicks, next_click = False, 0, 0.0
        while time.time() < deadline:
            try:
                chal = page.evaluate(_CHALLENGE_JS)
            except Exception:
                time.sleep(0.5)
                continue
            if not chal:
                return
            if not detected:
                detected = True
                self._log("遇到 Cloudflare 验证，等自动通过或点验证框")
            if clicks < 3 and time.time() > next_click:
                if self._click_cf_checkbox(page):
                    clicks += 1
                next_click = time.time() + 5
            time.sleep(0.5)
        if detected:
            raise Re0Error("Cloudflare 验证 90 秒内没通过，检查网络 / 代理到 challenges.cloudflare.com 是否通畅")

    def _on_challenge(self, page):
        """当前页面是不是 Cloudflare 挑战页。"""
        try:
            return bool(page.evaluate(_CHALLENGE_JS))
        except Exception:
            return False

    def solve_challenge(self):
        """（worker）一键过 Cloudflare 验证：自动点 Turnstile 复选框，成功后停在站内页面。
        实测节奏：点**一次**后要静等 6~10 秒让 CF 下发 cf_clearance，反复点反而会被判机器、
        把已签发的 clearance 弄失效。所以最多点 2 次，其余时间纯等。"""
        self._ensure_browser(headless=False)
        self._last_use = time.time()
        page = self._page()
        t0 = time.time()

        def cnames():
            try:
                return [c.get("name") for c in self._ctx.cookies()
                        if "re0.me" in (c.get("domain") or "")]
            except Exception:
                return []

        clicks = 0
        last_click = -99
        for i in range(16):
            if not self._on_challenge(page):
                break
            if "cf_clearance" in cnames():
                break                      # 已签发，别再点
            # 第一次点击前先等 1 个节拍让 iframe 挂载；两次点击之间隔 ~7.5s
            if clicks < 2 and i >= 1 and i - last_click >= 5:
                if self._click_cf_checkbox(page):
                    clicks += 1
                    last_click = i
            try:
                page.wait_for_timeout(1500)
            except Exception:
                time.sleep(1.0)

        names = cnames()
        # 有 clearance 但页面还挂在挑战标记上 → 带着它重新进一次
        if "cf_clearance" in names and self._on_challenge(page):
            try:
                page.goto(self.base + "/", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3500)
            except Exception as e:
                self._log(f"带 clearance 重进失败：{e}")
        still = self._on_challenge(page)
        names = cnames()
        diag = {}
        try:
            diag = page.evaluate("""() => {
                const ifr = [...document.querySelectorAll('iframe')]
                    .map(f => (f.src || '').slice(0, 90)).filter(Boolean);
                const body = (document.body && document.body.innerText || '').replace(/\\s+/g, ' ').slice(0, 160);
                return {cf_opt: Boolean(window._cf_chl_opt),
                        challenge_node: Boolean(document.querySelector('#challenge-running, #challenge-form')),
                        iframes: ifr, text: body, title: document.title,
                        href: location.href.slice(0, 90)};
            }""")
        except Exception as e:
            diag = {"err": str(e)[:80]}
        try:
            os.makedirs("/app/data/shots", exist_ok=True)
            page.screenshot(path="/app/data/shots/desktop.png")
            diag["shot"] = True
        except Exception as e:
            diag["shot_err"] = str(e)[:80]
        self._log(f"自动过验证：{'通过' if not still else '未通过'}（点了 {clicks} 次），"
                  f"cookie={names}，用时 {time.time() - t0:.1f}s")
        return {"ok": not still, "url": page.url, "title": self._safe_title(page),
                "cf_clearance": "cf_clearance" in names, "cookies": names,
                "clicks": clicks, "elapsed": round(time.time() - t0, 1), "diag": diag,
                "error": "" if not still else
                "验证没通过：已点复选框但没有跳转，通常是代理出口被 Cloudflare 判定高风险"}

    def _click_cf_checkbox(self, page):
        """验证框在 challenges.cloudflare.com 的 iframe 里还套着 Shadow DOM。
        实测：input 常是 0 尺寸（click 会 not-visible 超时），必须按 iframe 绝对坐标
        + 相对位置点左侧复选框（垂直居中）。返回是否点了下去。"""
        for fr in page.frames:
            if "challenges.cloudflare.com" not in (fr.url or ""):
                continue
            # 先试 Playwright 穿透 shadow DOM 的元素点击（force 跳过可见性检查）
            try:
                cb = fr.locator("input[type=checkbox]").first
                if cb.count():
                    cb.click(timeout=1500, force=True, no_wait_after=True)
                    return True
            except Exception:
                pass
            # 兜底：iframe 绝对框 + (25, 高度一半) —— 复选框就在 widget 左侧垂直居中
            try:
                bb = fr.frame_element().bounding_box()
                if bb and bb.get("width", 0) > 80:
                    cx = bb["x"] + 25
                    cy = bb["y"] + bb["height"] / 2.0
                    page.mouse.move(cx - 12, cy - 12)
                    page.wait_for_timeout(280)
                    page.mouse.move(cx, cy, steps=6)
                    page.wait_for_timeout(160)
                    page.mouse.click(cx, cy)
                    return True
            except Exception as e:
                self._log(f"按坐标点验证框失败：{e}")
        return False

    def _goto(self, page, url):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            raise Re0Error(f"打开 {url} 失败: {e}")
        # Next.js 流式页面可能迟迟不触发 load，不等它
        try:
            page.wait_for_load_state("load", timeout=30000)
        except Exception:
            pass
        self._wait_cloudflare(page)

    # ---------- 登录 ----------
    def _do_login(self, force=False):
        """（worker 线程）打开站点，被跳 /login 就填表提交。已登录直接返回。"""
        if not self.configured():
            raise Re0Error("未配置 RE0 直连账号密码")
        self._ensure_browser_auto()
        page = self._page()
        self._goto(page, self.base + "/")
        if not self._on_login(page):
            self._log("已有登录态，无需重新登录")
            return {"ok": True, "message": "已有登录态"}
        return self._login(page)

    def _login(self, page):
        user_sel = ("input[name='username'], input[name='email'], input[type='email'], "
                    "input[autocomplete='username'], input[placeholder*='邮箱'], "
                    "input[placeholder*='email'], input[placeholder*='用户名']")
        pass_sel = "input[name='password'], input[type='password']"
        try:
            page.wait_for_selector(user_sel, state="visible", timeout=15000)
            page.wait_for_selector(pass_sel, state="visible", timeout=15000)
        except Exception:
            raise Re0Error(f"登录页上没找到账号/密码输入框（{page.url}），站点可能改版")
        for attempt in range(3):
            page.fill(user_sel, "")
            page.fill(pass_sel, "")
            page.fill(user_sel, self.user)
            page.fill(pass_sel, self.pwd)
            time.sleep(0.5)  # 受控表单重渲染后两项一起复核
            ok = True
            for sel, val in ((user_sel, self.user), (pass_sel, self.pwd)):
                got = page.eval_on_selector(sel, "el => el.value") or ""
                if got != val:
                    ok = False
                    # 值没留住：用原生 setter 写
                    try:
                        page.eval_on_selector(sel, _NATIVE_SET_JS, val)
                    except Exception:
                        pass
            if ok:
                break
            self._log(f"第 {attempt + 1} 次复核：表单值没填上")
        submitted = False
        try:
            btn = page.locator("button[type='submit']").first
            if btn.count() and btn.is_visible():
                btn.click(timeout=5000)
                submitted = True
        except Exception:
            pass
        if not submitted:
            try:
                btn = page.get_by_role("button", name=re.compile(r"^\s*(登录|登 录|Login|Sign in)\s*$")).first
                if btn.count():
                    btn.click(timeout=5000)
                    submitted = True
            except Exception:
                pass
        if not submitted:
            try:
                page.press(pass_sel, "Enter")
                submitted = True
            except Exception:
                pass
        deadline = time.time() + 30
        while time.time() < deadline:
            if not self._on_login(page):
                self._log("登录成功")
                return {"ok": True, "message": "登录成功"}
            hint = ""
            try:
                hint = page.evaluate(_LOGIN_HINT_JS) or ""
            except Exception:
                pass
            if hint:
                raise Re0Error(f"登录失败：{hint[:120]}")
            time.sleep(0.3)
        raise Re0Error("登录超时（30 秒还在登录页），请检查账号密码")

    def login(self, force=False):
        return self._submit(lambda: self._do_login(force), timeout=240)

    # ---------- 搜索 ----------
    def _do_search(self, media_type, tmdb_id):
        if media_type not in ("movie", "tv") or not tmdb_id:
            raise Re0Error("RE0 按 TMDB 条目查资源，需要有效的类型与编号")
        self._ensure_browser_auto()
        page = self._page()
        # 兜底：页面自己发的 JSON 响应里也可能带着资源列表
        captured = []

        def on_response(resp):
            try:
                if resp.status == 200 and "json" in (resp.headers.get("content-type") or ""):
                    captured.append(resp)
            except Exception:
                pass
        page.on("response", on_response)
        try:
            self._goto(page, f"{self.base}/tmdb/{media_type}/{tmdb_id}")
            last_chunks = ""
            deadline = time.time() + SEARCH_WAIT_SECONDS
            empty_since = 0.0
            last_scroll = 0.0
            _last_joined = ""
            while time.time() < deadline:
                if self._on_login(page):
                    self._log("登录态失效（被跳回登录页），重新登录")
                    self._login(page)
                    self._goto(page, f"{self.base}/tmdb/{media_type}/{tmdb_id}")
                try:
                    chunks = page.evaluate(_CHUNKS_JS) or []
                except Exception:
                    chunks = []
                joined = "\n".join(chunks)
                _last_joined = joined
                if joined:
                    try:
                        os.makedirs("/app/data/dump", exist_ok=True)
                        open(f"/app/data/dump/re0_tmdb_{tmdb_id}.txt", "w").write(joined)
                    except Exception:
                        pass
                    rows, finished = parse_page_resources(joined)
                    if finished and rows:
                        self._log(f"页面数据里读到 {len(rows)} 条资源")
                        return rows
                    if finished and not rows:
                        # RSC 里的空态组件有两种：一种是骨架屏占位（页面正文还是「加载中」），
                        # 一种是真的没资源。**只有正文明确说了没有资源才算空**，否则继续等。
                        # 之前这里直接当空返回，导致慢一点的片 2 秒就被判成「0 条」。
                        try:
                            body = page.evaluate(_BODY_TEXT_JS) or ""
                        except Exception:
                            body = ""
                        if page_says_empty(body):
                            self._log("页面明确显示没有资源，确实为空")
                            self._diagnose_empty(page, tmdb_id, joined)
                            return []
                        if not empty_since:
                            empty_since = time.time()
                for resp in captured:
                    try:
                        rows = parse_json_resources(resp.text())
                    except Exception:
                        continue
                    if rows:
                        self._log(f"接口响应里读到 {len(rows)} 条资源")
                        return rows
                captured.clear()
                # 资源区在首屏之下，站点可能懒加载（IntersectionObserver）：定期滚到底触发推送。
                # 2026-10-27 实测：页面完全渲染但资源数据 30 秒不流出，滚动后即出。
                if time.time() - last_scroll > 6:
                    try:
                        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                    except Exception:
                        pass
                    last_scroll = time.time()
                time.sleep(0.35)
            # 超时收尾：分清「被拦截」和「真没加载出来」，别混成一句话
            if self._on_challenge(page):
                raise Re0Error(
                    "被 Cloudflare 拦住了（页面停在验证页）。到设置里点「⚡ 自动过验证」，"
                    "或重新登录一次再试")
            self._diagnose_empty(page, tmdb_id, _last_joined)
            raise Re0Error(f"RE0 页面 {SEARCH_WAIT_SECONDS} 秒内没出资源列表（{page.url}），请稍后再试")
        finally:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass

    def _diagnose_empty(self, page, tmdb_id, raw):
        """0 条时把原始 RSC 数据落盘（供离线分析为何解析为 0 条），同时留页面证据。"""
        try:
            os.makedirs("/app/data/dump", exist_ok=True)
            open(f"/app/data/dump/re0_tmdb_{tmdb_id}.txt", "w").write(raw or "")
        except Exception:
            pass
        try:
            title = self._safe_title(page)
            body = page.evaluate("() => (document.body && document.body.innerText) || ''")
            ch = self._on_challenge(page)
            try:
                n_nextf = page.evaluate(
                    "() => [...document.scripts].filter(s => (s.textContent||'').trim().startsWith('self.__next_f.push(')).length") or 0
                n_res_sec = page.evaluate(
                    "() => { const els = [...document.querySelectorAll('h2,h3,section,div')];"
                    " return els.filter(e => /网盘|资源/.test((e.innerText||'').slice(0,30))).length }") or 0
            except Exception:
                n_nextf = n_res_sec = -1
            self._log(f"0 条诊断：url={page.url} title={title!r} body={len(body)} "
                      f"rawlen={len(raw or '')} nextf={n_nextf} 资源区标题={n_res_sec} challenge={ch} head={body[:80]!r}")
            try:
                os.makedirs("/app/data/shots", exist_ok=True)
                page.screenshot(path=f"/app/data/shots/zero-{tmdb_id}.png", timeout=8000)
            except Exception:
                pass
        except Exception as e:
            self._log(f"0 条诊断失败：{e}")

    def search(self, media_type, tmdb_id):
        return self._submit(lambda: self._do_search(media_type, tmdb_id), timeout=240)

    # ---------- 解锁 ----------
    def _is_share_url(self, v):
        try:
            u = urllib.parse.urlparse((v or "").strip())
            if u.scheme not in ("http", "https"):
                return False
            if u.username or u.password:
                return False
            if u.hostname not in ("115.com", "115cdn.com", "anxia.com"):
                return False
            return bool(re.match(r"^/s/[^/\s]+/?$", u.path or ""))
        except Exception:
            return False

    def _do_unlock(self, slug, points=0):
        if not _SLUG_RE.match(slug or ""):
            raise Re0Error("RE0 资源编号无效")
        self._ensure_browser_auto()
        page = self._page()
        self._goto(page, f"{self.base}/resource/115/{slug}")
        submitted = False
        deadline = time.time() + UNLOCK_WAIT_SECONDS
        link = ""
        while time.time() < deadline:
            if self._on_login(page):
                self._log("登录态失效，重新登录")
                self._login(page)
                self._goto(page, f"{self.base}/resource/115/{slug}")
            cur = page.url or ""
            if self._is_share_url(cur):
                link = cur  # 解锁后整页跳到了 115
                break
            try:
                s = page.evaluate(_SHARE_JS) or ""
            except Exception:
                s = ""
            if self._is_share_url(s):
                link = s.strip()
                break
            if _ED2K_RE.match((s or "").strip()):
                link = s.strip()
                break
            # 解锁失败的红色 toast
            try:
                toast = page.locator('[data-slot="toast"].toast--danger, [role="alert"]').first
                if toast.count():
                    t = (toast.inner_text(timeout=1000) or "").strip()
                    if t:
                        raise Re0Error(f"RE0 解锁失败：{t[:120]}")
            except Re0Error:
                raise
            except Exception:
                pass
            if not submitted:
                try:
                    btn = page.get_by_role("button", name=re.compile(r"^\s*(确认解锁|确定解锁|Confirm unlock)\s*$")).first
                    if btn.count() and btn.is_visible():
                        dis = btn.get_attribute("disabled")
                        if dis is None:
                            btn.click(timeout=5000)
                            submitted = True
                            deadline = time.time() + UNLOCK_WAIT_SECONDS
                            note = f"已花 {points} 积分" if points else "免费资源，没花积分"
                            self._log(f"已点「确认解锁」（{note}），等分享链接")
                except Exception:
                    pass
            time.sleep(0.25)
        if not link:
            if submitted:
                raise Re0Error(f"点了解锁但 {UNLOCK_WAIT_SECONDS} 秒内没拿到 115 / ed2k 链接（{page.url}）")
            raise Re0Error(f"页面上既没有分享链接也没有「确认解锁」按钮（{page.url}）")
        links = _split_links(link)
        code = ""
        if not _ED2K_RE.match(links[0]):
            try:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
                code = (q.get("password") or q.get("pwd") or [""])[0]
            except Exception:
                pass
        self._log(f"解锁成功：{slug} → {links[0][:60]}{' 等 %d 条' % len(links) if len(links) > 1 else ''}")
        return {"links": links, "code": code, "spent": submitted}

    def unlock(self, slug, points=0):
        return self._submit(lambda: self._do_unlock(slug, points), timeout=240)

    # ---------- 文件预览（不解锁、不花积分） ----------
    def _do_preview(self, slug):
        if not _SLUG_RE.match(slug or ""):
            raise Re0Error("资源编号无效")
        self._ensure_browser_auto()
        page = self._page()
        self._goto(page, f"{self.base}/resource/115/{slug}")
        if self._on_login(page):
            self._login(page)
            self._goto(page, f"{self.base}/resource/115/{slug}")
        try:
            res = page.evaluate(_SIGNED_JS, "/api/customer/resources/" + slug + "/file-list")
        except Exception as e:
            raise Re0Error(f"页面里调用文件列表接口失败: {e}")
        status = int((res or {}).get("status") or 0)
        text = (res or {}).get("text") or ""
        if status == 0:
            raise Re0Error(text or "拿不到 signedFetch")
        try:
            env = json.loads(text)
        except Exception:
            raise Re0Error(f"文件列表返回不是 JSON（HTTP {status}）：{text[:120]}")
        if not (200 <= status < 300) or env.get("success") is False:
            raise Re0Error(f"文件列表接口报错（HTTP {status}）：{(env.get('message') or env.get('msg') or text)[:120]}")
        raw = env.get("data") if isinstance(env.get("data"), (dict, list)) else env
        files = raw.get("files") if isinstance(raw, dict) else None
        if not isinstance(files, list):
            raise Re0Error("文件列表返回格式认不出（站点可能改版）")
        return {"files": files, "share_title": raw.get("share_title") or "",
                "slug": slug}

    def preview(self, slug):
        return self._submit(lambda: self._do_preview(slug), timeout=180)

    # ---------- 每日签到 ----------
    def _do_checkin(self):
        self._ensure_browser_auto()
        page = self._page()
        self._goto(page, self.base + "/")
        if self._on_login(page):
            self._login(page)
            self._goto(page, self.base + "/")
        # 签到按钮：找可见的、文案含「签到」的按钮
        try:
            btn = page.get_by_role("button", name=re.compile(r"签到")).first
            if not btn.count():
                btn = page.locator("button:has-text('签到')").first
            if btn.count() and btn.is_visible():
                btn.click(timeout=5000)
                time.sleep(2)
                hint = ""
                try:
                    hint = page.evaluate(_LOGIN_HINT_JS) or ""
                except Exception:
                    pass
                return {"ok": True, "message": hint or "已点签到"}
            return {"ok": False, "message": "首页没找到签到按钮（可能今天已签到）"}
        except Exception as e:
            raise Re0Error(f"签到失败: {e}")

    def checkin(self):
        return self._submit(lambda: self._do_checkin(), timeout=180)

    # ---------- 通道自检（设置页「测试直连」） ----------
    @staticmethod
    def _proxy_egress(proxy, timeout=15):
        """经（或不经过）代理访问 re0.me/cdn-cgi/trace → (ip, loc, colo)。

        这是最省的独立探针：不启浏览器，1~3 秒就能判断代理到底通不通、出口在哪国。
        """
        handlers = []
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        op = urllib.request.build_opener(*handlers)
        req = urllib.request.Request("https://re0.me/cdn-cgi/trace",
                                     headers={"User-Agent": "Mozilla/5.0"})
        body = op.open(req, timeout=timeout).read().decode("utf-8", "replace")
        d = {}
        for line in body.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip()
        return d.get("ip", ""), d.get("loc", ""), d.get("colo", "")

    def _do_test(self, media_type="movie", tmdb_id="693134", label="沙丘2"):
        """（worker）四段自检：代理出口 → Cloudflare → 登录态 → 真查一部片。

        分段返回，失败能立刻指到具体环节（哪一段红就是哪一段的问题），
        比"整体成功/失败"有用得多。
        """
        t0 = time.time()
        steps = []
        res = {"uid": self.uid, "proxy": self.proxy, "proxy_source": self._proxy_source,
               "steps": steps, "ok": False, "message": ""}

        def add(name, ok, detail=""):
            steps.append({"name": name, "ok": bool(ok), "detail": str(detail)[:280]})
            return ok

        def done(msg=""):
            res["message"] = msg
            res["elapsed"] = round(time.time() - t0, 1)
            return res

        # ① 出口 IP（不启浏览器）
        try:
            ip, loc, colo = self._proxy_egress(self.proxy)
        except Exception as e:
            add("代理出口", False, "连不上：%s" % e)
            return done("代理不通：检查「基础设置 → HTTP 代理」，或本身份单独指定的代理地址 / 端口")
        add("代理出口", bool(ip), "%s · %s · %s" % (ip or "?", loc or "?", colo or "?"))
        if not ip:
            return done("代理没返回出口 IP，先修代理")
        if (loc or "").upper() == "CN":
            add("出口地区", False, "出口在中国大陆（%s）" % ip)
            return done("出口在中国大陆：re0.me 屏蔽大陆 IP，必须走海外代理，"
                        "或把 re0.me 的分流规则指向海外节点")

        # ② 浏览器 + Cloudflare
        try:
            self._ensure_browser_auto()
            page = self._page()
            self._goto(page, self.base + "/")
        except Exception as e:
            add("Cloudflare", False, str(e))
            return done("打不开 re0.me：%s" % e)
        chal = self._on_challenge(page)
        add("Cloudflare", not chal,
            "验证已过" if not chal else "停在挑战页（%s）" % (self._safe_title(page) or "?"))
        if chal:
            return done("Cloudflare 没过：点「⚡ 自动过验证」（窗口要开着），"
                        "或代理出口被 CF 标记了，换个节点")

        # ③ 登录态
        if self._on_login(page):
            add("登录态", False, "被跳转到 /login")
            return done("CF 过了，但 re0.me 没登录：点「① 打开登录窗口」用 Telegram 授权登录，"
                        "再点「② 保存登录态」")
        add("登录态", True, "未被跳转，浏览器里是登录态")

        # ④ 真查一部片（沙丘2，TMDB 693134）
        try:
            rows = self._do_search(media_type, tmdb_id)
        except Exception as e:
            msg = str(e)
            add("实查资源", False, msg)
            if "登录" in msg or "login" in msg.lower():
                return done("浏览器里其实不是登录态：重新打开窗口用 Telegram 登录，再保存")
            return done("页面能打开、也像登录态，但没读出资源列表：%s" % msg)
        add("实查资源", bool(rows), "《%s》读到 %d 条" % (label, len(rows)))
        if not rows:
            return done("通道是通的，但《%s》没查到资源（可能该片下架或站点改版），换部片再试" % label)
        res["ok"] = True
        res["items"] = len(rows)
        return done("通道正常：《%s》查到 %d 条资源 · 出口 %s" % (label, len(rows), loc or "?"))

    def test_channel(self, media_type="movie", tmdb_id="693134", label="沙丘2"):
        return self._submit(lambda: self._do_test(media_type, tmdb_id, label), timeout=300)

    # ---------- 状态 ----------
    def status(self):
        st = re0browser.status()
        out = {
            "uid": self.uid,
            "configured": self.configured(),
            "user": self.user,
            "proxy": self.proxy,
            "proxy_source": self._proxy_source,
            "browser": st,
            "cookies_captured": bool(self.cookies),
            "logged_in": os.path.isdir(self.profile_dir) and bool(os.listdir(self.profile_dir)),
        }
        return out

    def _log(self, msg):
        try:
            print(f"[RE0直连] {msg}", flush=True)
        except Exception:
            pass


def _split_links(s):
    """解锁结果拆成一条条链接：一条资源可能给出一批 ed2k（一集一条）。"""
    s = (s or "").strip()
    if "ed2k://" not in s:
        return [s] if s else []
    out, seen = [], set()
    for part in s.split("ed2k://")[1:]:
        m = _ED2K_RE.match("ed2k://" + part)
        if not m:
            continue
        link = m.group(0)
        fields = link.split("|")
        key = fields[4].lower() if len(fields) > 4 else link
        if key not in seen:
            seen.add(key)
            out.append(link)
    return out or ([s] if s else [])
