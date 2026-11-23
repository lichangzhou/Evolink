"""CloakBrowser 管理器：下载、Ed25519 验签、解压、启动参数。

CloakBrowser = C++ 源码层改指纹的 Chromium，过 Cloudflare 比注入 JS 脚本稳。
re0.me 做了加密会话绑定，接口不能脱离浏览器单独调，所以 RE0 直连必须靠它。

规格（照 115-Station internal/api/re0browser.go 重写成 Python）：
- 下载：GitHub Releases `CloakHQ/cloakbrowser`，路径
  chromium-v{版本}/cloakbrowser-linux-{x64|arm64}.tar.gz，备用 cloakbrowser.dev
- 校验：同目录 SHA256SUMS + SHA256SUMS.sig（Ed25519，公钥写死）；
  manifest 的 version= 行必须等于请求的版本（防降级攻击）
- 许可：free to use, no redistribution → **运行时下载到数据目录，不进镜像**
- 启动：--fingerprint=<种子> --fingerprint-platform=windows，去掉 --enable-automation；
  容器里 /dev/shm 只有 64MB，要 --disable-dev-shm-usage；别加 --disable-gpu
  （没有 WebGL 是很明显的机器人特征）
- 只支持 Linux x64 / arm64（官方只发这两个预编译包）
"""
import base64
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import threading
import time
import urllib.error
import urllib.request

# 与 npm cloakbrowser 0.6.2 的 PLATFORM_CHROMIUM_VERSIONS 一致；升级时核对 GitHub 上真有这个 release
CLOAK_VERSION_X64 = "146.0.7680.177.5"
CLOAK_VERSION_ARM64 = "146.0.7680.177.3"
# 官方 SHA256SUMS 的 Ed25519 签名公钥（config.js 的 BINARY_SIGNING_PUBKEYS）
CLOAK_SIGNING_KEY = "MKFKwIhUcKWq5xTuNA0Ovg99njcDEcEJvmWYYhApvaU="

GITHUB_REPO = "CloakHQ/cloakbrowser"
GITHUB_DL = "https://github.com/CloakHQ/cloakbrowser/releases/download"
MIRROR_DL = "https://cloakbrowser.dev"

USER_AGENT = "evolink/1.1 (re0-web-channel)"

_PROXY = ""           # 下载走代理（GitHub Releases 国内直连不通，NAS 上填 mihomo）


def set_proxy(proxy):
    """设置下载代理（空 = 直连）。保存设置 / 启动时调。"""
    global _PROXY
    _PROXY = (proxy or "").strip()


_lock = threading.Lock()
_state = {"state": "missing", "version": "", "platform": "", "path": "",
          "done": 0, "total": 0, "speed": 0, "source": "", "error": "",
          "reason": "", "updated_at": 0}
_busy = False
_root = ""            # <DATA_DIR>/cloakbrowser，init() 时设
_seed = ""            # 指纹种子（持久化，固定指纹比每次随机更稳）
_verify_key = None    # nacl VerifyKey，惰性初始化（没装 pynacl 时延迟报错）


def _platform():
    """(tag, ok)：只有 Linux x64 / arm64 有预编译包。"""
    import platform
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "linux-x64", True
    if machine in ("aarch64", "arm64"):
        return "linux-arm64", True
    return "linux-" + machine, False


def version():
    if os.environ.get("CLOAKBROWSER_VERSION", "").strip():
        v = os.environ["CLOAKBROWSER_VERSION"].strip()
        if re.match(r"^[0-9]+(?:\.[0-9]+){3,4}$", v):
            return v
    return CLOAK_VERSION_ARM64 if _platform()[0] == "linux-arm64" else CLOAK_VERSION_X64


def _bin_dir(v=None):
    return os.path.join(_root, "chromium-" + (v or version()))


def _bin_path(v=None):
    return os.path.join(_bin_dir(v), "chrome")


def _marker(v=None):
    return os.path.join(_bin_dir(v), ".verified")


def _archive_name(tag):
    return "cloakbrowser-" + tag + ".tar.gz"


def init(data_dir, proxy=""):
    """启动时调：记数据目录、清上次没解压完的临时目录、报告本地状态。"""
    global _root, _seed
    set_proxy(proxy)
    _root = os.path.join(data_dir, "cloakbrowser")
    try:
        for name in os.listdir(_root):
            if name.endswith(".tmp") and os.path.isdir(os.path.join(_root, name)):
                shutil.rmtree(os.path.join(_root, name), ignore_errors=True)
    except FileNotFoundError:
        pass
    except Exception:
        pass
    # 指纹种子固定：会话/指纹漂移也是风控信号
    seed_file = os.path.join(_root, "fingerprint.seed")
    try:
        if os.path.exists(seed_file):
            _seed = open(seed_file).read().strip()
        if not _seed:
            _seed = base64.b16encode(os.urandom(16)).decode().lower()
            os.makedirs(_root, exist_ok=True)
            with open(seed_file, "w") as f:
                f.write(_seed)
    except Exception:
        _seed = base64.b16encode(os.urandom(16)).decode().lower()


def installed():
    """本地有没有装好（校验标记 + 可执行文件都在）。只看本地，不发请求。"""
    p = os.environ.get("CLOAKBROWSER_BINARY_PATH", "").strip()
    if p:
        return (p, True) if os.access(p, os.X_OK) else ("", False)
    if not _root:
        return "", False
    v = version()
    if not os.path.exists(_marker(v)):
        return "", False
    binp = _bin_path(v)
    if not (os.path.exists(binp) and os.access(binp, os.X_OK)):
        return "", False
    return binp, True


def status():
    with _lock:
        s = dict(_state)
        busy = _busy
    tag, ok = _platform()
    s["platform"], s["version"] = tag, version()
    if not ok and not os.environ.get("CLOAKBROWSER_BINARY_PATH", "").strip():
        s["state"] = "unsupported"
        s["error"] = "这个平台没有 CloakBrowser 预编译包（只有 Linux x64 / arm64）"
        return s
    if not busy:
        binp, has = installed()
        if has:
            s["state"], s["path"] = "ready", binp
        elif s["state"] in ("ready", ""):
            s["state"] = "missing"
    return s


def _set(**kw):
    with _lock:
        _state.update(kw)
        _state["updated_at"] = int(time.time())


def ensure(reason=""):
    """本地没有就在后台下载（幂等）。返回是否已可用。"""
    global _busy
    if not _root:
        return False
    if not _platform()[1]:
        return False
    binp, has = installed()
    if has:
        return True
    with _lock:
        if _busy:
            return False
        _busy = True
        _state.update(state="downloading", reason=reason, error="", done=0, total=0, speed=0)
    t = threading.Thread(target=_install_worker, args=(reason,), daemon=True)
    t.start()
    return False


def reinstall(reason=""):
    """删掉本地的再下（界面「重新下载」）。"""
    global _busy
    with _lock:
        if _busy:
            return "正在下载中"
    v = version()
    shutil.rmtree(_bin_dir(v), ignore_errors=True)
    ensure(reason or "手动重新下载")
    return ""


def _install_worker(reason):
    global _busy
    try:
        t0 = time.time()
        _set(state="downloading")
        install()
        binp, _ = installed()
        _set(state="ready", path=binp or "", error="")
        _prune_old(version())
        _log(f"CloakBrowser {version()} 已就绪（用时 {time.time()-t0:.0f}s）")
    except Exception as e:
        _set(state="error", error=str(e))
        _log(f"CloakBrowser 准备失败: {e}")
    finally:
        with _lock:
            global _busy
            _busy = False


def _prune_old(keep):
    """删掉别的版本（升级换了版本号后旧的 200MB 留着没用）。"""
    try:
        ents = os.listdir(_root)
    except Exception:
        return
    for name in ents:
        p = os.path.join(_root, name)
        if os.path.isdir(p) and name.startswith("chromium-") and name != "chromium-" + keep:
            shutil.rmtree(p, ignore_errors=True)
            _log(f"删除旧版本浏览器 {name}")
    for name in ents:
        if name.endswith(".part") and keep not in name:
            try:
                os.remove(os.path.join(_root, name))
            except Exception:
                pass


# ---------------- 下载 / 校验 ----------------

def _sources(v, tag):
    archive = _archive_name(tag)
    direct = f"{GITHUB_DL}/chromium-v{v}"
    mirror = f"{MIRROR_DL}/chromium-v{v}"
    return [
        {"name": "GitHub Releases", "sums": direct + "/SHA256SUMS",
         "sig": direct + "/SHA256SUMS.sig", "archive": direct + "/" + archive, "api": False},
        {"name": "cloakbrowser.dev", "sums": mirror + "/SHA256SUMS",
         "sig": mirror + "/SHA256SUMS.sig", "archive": mirror + "/" + archive, "api": False},
    ]


def _opener():
    handlers = []
    if _PROXY:
        handlers.append(urllib.request.ProxyHandler({"http": _PROXY, "https": _PROXY}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))  # 显式不用环境变量代理
    return urllib.request.build_opener(*handlers)


def _fetch(url, limit=1 << 20, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with _opener().open(req, timeout=timeout) as r:
        return r.read(limit)


def _verify_signature(manifest, sig_b64):
    """核 SHA256SUMS 的 Ed25519 签名（.sig 是 base64 文本）。"""
    global _verify_key
    try:
        sig = base64.b64decode(sig_b64.strip())
        key = base64.b64decode(CLOAK_SIGNING_KEY)
        if len(sig) != 64 or len(key) != 32:
            raise ValueError("长度不对")
        if _verify_key is None:
            from nacl.signing import VerifyKey
            _verify_key = VerifyKey(key)
        _verify_key.verify(manifest, sig)
    except ImportError:
        raise RuntimeError("缺少 pynacl（pip install pynacl），无法验签")
    except Exception:
        raise RuntimeError("SHA256SUMS 签名校验失败，拒绝使用这个来源的文件")


def _manifest_version(text):
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("version="):
            return line[len("version="):].strip()
    return ""


def _parse_checksums(text):
    out = {}
    for line in text.splitlines():
        m = re.match(r"(?i)^([a-f0-9]{64})\s+\*?(.+)$", line.strip())
        if m:
            out[m.group(2).strip()] = m.group(1).lower()
    return out


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _download(src, part, total_hint=0):
    """下到 part 文件，已有部分用 Range 续（服务器不认 Range 就从头来）。"""
    have = os.path.getsize(part) if os.path.exists(part) else 0
    headers = {"User-Agent": USER_AGENT}
    if have > 0:
        headers["Range"] = f"bytes={have}-"
    req = urllib.request.Request(src["archive"], headers=headers)
    try:
        resp = _opener().open(req, timeout=120)
    except urllib.error.HTTPError as e:
        if e.code == 416 and have > 0:
            return  # 已经下全了（上次下完没来得及校验就退出）
        raise RuntimeError(f"下载返回 HTTP {e.code}")
    with resp:
        code = resp.status
        if code == 200:
            mode, have = "wb", 0
        elif code == 206 and have > 0:
            mode = "ab"
        else:
            raise RuntimeError(f"下载返回 HTTP {code}")
        total = have + int(resp.headers.get("Content-Length") or 0) or total_hint
        _set(total=total)
        done = have
        t0 = time.time()
        last = t0
        with open(part, mode) as f:
            while True:
                b = resp.read(256 << 10)
                if not b:
                    break
                f.write(b)
                done += len(b)
                now = time.time()
                if now - last >= 1:
                    _set(done=done, speed=int(len(b) / (now - last)))
                    last = now
        _set(done=done, speed=0)


def _extract(archive, dst):
    """解 tar.gz。绝对路径与带 .. 的条目跳过，符号链接只认指向包内的。"""
    n = 0
    with tarfile.open(archive, "r:gz") as tf:
        for m in tf:
            name = os.path.normpath(m.name)
            if m.name.startswith("/") or name == ".." or name.startswith("../"):
                _log(f"跳过可疑的包内路径: {m.name}")
                continue
            target = os.path.join(dst, name)
            if m.isdir():
                os.makedirs(target, exist_ok=True)
            elif m.isfile():
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with tf.extractfile(m) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                os.chmod(target, (m.mode & 0o777) | 0o600)
                n += 1
            elif m.issym() or m.islnk():
                lnk = os.path.normpath(os.path.join(os.path.dirname(name), m.linkname))
                if lnk.startswith("../") or os.path.isabs(m.linkname):
                    _log(f"跳过包外符号链接: {m.name}")
                    continue
                try:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    if os.path.lexists(target):
                        os.remove(target)
                    os.symlink(m.linkname, target)
                except Exception:
                    pass
    return n


def _flatten(root):
    """包内可能多套一层目录（cloakbrowser-xxx/chrome），把 chrome 所在层提到 root。"""
    for dirpath, _dirs, files in os.walk(root):
        if "chrome" in files and os.access(os.path.join(dirpath, "chrome"), os.X_OK):
            if os.path.abspath(dirpath) == os.path.abspath(root):
                return
            # 把这一层的内容挪到 root
            for name in os.listdir(dirpath):
                s = os.path.join(dirpath, name)
                d = os.path.join(root, name)
                if os.path.exists(d):
                    continue
                shutil.move(s, d)
            return
        if dirpath != root and any(f == "chrome" for f in files):
            return


def install():
    """同步安装（供测试与后台线程调用）。"""
    tag, ok = _platform()
    if not ok:
        raise RuntimeError(f"平台 {tag} 没有预编译包")
    v = version()
    os.makedirs(_root, exist_ok=True)
    archive = _archive_name(tag)
    errs = []
    for src in _sources(v, tag):
        try:
            _set(source=src["name"], error="")
            _log(f"尝试来源：{src['name']}")
            # 1. 先拿签名过的 manifest：验不过就不下 200MB
            sums = _fetch(src["sums"])
            sig = _fetch(src["sig"], limit=4096)
            _verify_signature(sums, sig)
            text = sums.decode("utf-8", "replace")
            got_v = _manifest_version(text)
            if got_v != v:
                raise RuntimeError(f"签名清单声明的版本是 {got_v!r}，要的是 {v}，拒绝使用（可能是降级攻击）")
            want = _parse_checksums(text).get(archive, "")
            if not want:
                raise RuntimeError(f"签名清单里没有 {archive}")
            _log(f"签名校验通过（Ed25519），{archive} 期望 sha256={want[:16]}…")
            # 2. 下载（断点续传）
            part = os.path.join(_root, f"_download-{v}-{tag}.tar.gz.part")
            _set(state="downloading")
            _download(src, part)
            # 3. 核对 sha256
            _set(state="verifying")
            got = _sha256_file(part)
            if got != want:
                os.remove(part)
                raise RuntimeError(f"sha256 不符（下到 {got[:16]}…，期望 {want[:16]}…），已删除")
            _log("sha256 校验通过")
            # 4. 解压到临时目录，成功了再改名
            _set(state="extracting")
            dst = _bin_dir(v)
            tmp = dst + ".tmp"
            shutil.rmtree(tmp, ignore_errors=True)
            n = _extract(part, tmp)
            _flatten(tmp)
            binp = os.path.join(tmp, "chrome")
            if not os.path.exists(binp):
                shutil.rmtree(tmp, ignore_errors=True)
                raise RuntimeError("解压后没找到 chrome 可执行文件")
            os.chmod(binp, 0o755)
            shutil.rmtree(dst, ignore_errors=True)
            os.rename(tmp, dst)
            with open(_marker(v), "w") as f:
                f.write(f"{v} {want}\n")
            os.remove(part)
            _log(f"解压完成：{n} 个文件 → {dst}")
            return
        except Exception as e:
            _log(f"来源 {src['name']} 失败: {e}")
            errs.append(f"{src['name']}：{e}")
    raise RuntimeError("；".join(errs))


def launch_args(proxy=""):
    """Chromium 启动参数（照 115-station 实测能过 Cloudflare 的组合）。"""
    args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",           # 容器 /dev/shm 默认 64MB
        "--test-type",                       # 消掉 "unsupported command-line flag: --no-sandbox" 提示条
        "--fingerprint=" + (_seed or "evolink"),
        "--fingerprint-platform=windows",
        # 注意：**不要**在这里加 --disable-features。它会覆盖 Playwright 自己那串默认
        # disable-features（含 ThirdPartyStoragePartitioning 等），实测会导致 CF 挑战页
        # 不渲染 Turnstile 复选框、只在"非交互式重试"里死循环。
        "--lang=en-US",      # CF 对中文区域返回无复选框的简化挑战页，英文才会渲染 Turnstile widget
    ]
    # rod/playwright 默认会带一串自动化特征参数；CloakBrowser 官方包装层同样去掉
    for bad in ("--enable-automation",):
        pass  # 我们自建 args，本来就不带
    if proxy:
        # Chromium 的 --proxy-server 不支持带账号密码的代理，去掉账号部分照用
        m = re.match(r"^(https?|socks5?)://(?:[^@/]+@)?(.+)$", proxy)
        if m:
            args.append("--proxy-server=" + m.group(1) + "://" + m.group(2))
    return args


def _log(msg):
    try:
        print(f"[CloakBrowser] {msg}", flush=True)
    except Exception:
        pass
