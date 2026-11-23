"""
core/strm_sync.py — EvoLink strm 同步引擎
=========================================

解决什么问题
------------
原本由外部工具在本地挂载盘上批量建立 .strm 索引文件（每个 .strm 第一行写
一个可播放地址，播放器 / 媒体库扫目录时按它去拉流）。EvoLink 走的是另一条路：
媒体服务器不直连 115，而是请求 EvoLink 的换链端点拿 302 直链。

本模块把「建立 strm」这件事完整接管过来：
EvoLink 自己扫描 115 目录树 → 挑选要索引的媒体 → 生成 .strm 文本 →
写进本地输出目录。strm 由 EvoLink 独立维护。

三种 strm 格式
--------------
1. path —— CD2 挂载路径型
   {cd2_prefix}/{drive}/{rel_path}
   例：/CloudNAS/CloudDrive/115open/Video/Movie/xxx.mkv
   供仍在用 CloudDrive2 挂载的播放器使用。

2. pc —— pickcode 型
   {base_url}/api/redirect115/{pickcode}
   例：http://192.168.1.10:9191/api/redirect115/csxz1f1ccwkssd46f
   直接写 pickcode，播放时由 EvoLink 换直链。pickcode 绑账号，换号需重建。

3. evolink —— EvoLink 路径换链型
   {base_url}/api/swap115/{quote(drive/rel_path)}
   例：http://192.168.1.10:9191/api/swap115/115open%2FVideo%2FMovie%2Fxxx.mkv
   只写路径不写 pickcode：换号只需重登 EvoLink，存量 strm 不用重建。

设计约束
--------
* 纯标准库（os / sys / json / time / shutil / re / threading / queue /
  urllib.parse），不引入任何第三方包。
* 不自己写数据库 —— 所有持久化都走 core.strm_lib 的函数。
* 不自己扫云盘做换链 —— 只负责生成 strm 文本，换链交给 pan115_swap。
* 单个文件失败不中断整库：错误收集进 errors 列表继续跑。
* 扫描阶段保持单线程（115 接口有风控压力），只有写文件阶段并发。
* 生成的 strm 路径必须落在 outdir 之内，防 ../ 穿越。
"""

from __future__ import annotations

import os
import queue
import threading
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

try:  # 正常部署：core 是包，strm_sync 是 core.strm_sync
    from . import pan115, strm_lib
except ImportError:  # pragma: no cover - 单文件调试时的兜底
    try:
        from core import pan115, strm_lib
    except ImportError:
        import pan115  # type: ignore
        import strm_lib  # type: ignore

try:  # config 只用来兜底取 base_url，取不到不影响主流程
    from . import config
except ImportError:  # pragma: no cover
    config = None  # type: ignore

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_CD2_PREFIX = "/CloudNAS/CloudDrive"
DEFAULT_BASE_URL = "http://127.0.0.1:9191"
DEFAULT_DRIVE_HEAD = "115open"
STRM_EXT = ".strm"

# strm_lib.normalize 把 bdmv 限制在 ("skip", "main")，这里同时兼容布尔写法
_BDMV_OFF = ("", "0", "false", "no", "off", "skip", "none")


# ---------------------------------------------------------------------------
# 内部小工具
# ---------------------------------------------------------------------------

def _to_int(val: Any, default: int = 0) -> int:
    """宽松转 int：失败 / 空值返回 default。"""
    try:
        if val is None or (isinstance(val, str) and not val.strip()):
            return default
        return int(str(val).strip())
    except (TypeError, ValueError):
        return default


def _now() -> str:
    """当前时间戳，格式与 core.db.now() 一致（%Y-%m-%d %H:%M:%S）。"""
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _drive_head(lib: Dict[str, Any]) -> str:
    """
    strm 里作为「路径首段」的名字。

    * 规格文档里这个角色叫 drive（如 115open）；
    * 但 core/strm_lib.py 的 drive 字段是**网盘类型**（115 / 123 / guangya），
      真正的路径前缀在 store_prefix（默认 115open）。
    * 因此这里取 store_prefix 优先、drive 兜底 —— 默认值一致时两者无差别。
    """
    store = str(lib.get("store_prefix") or "").strip().strip("/")
    if store:
        return store
    drive = str(lib.get("drive") or "").strip().strip("/")
    return drive or DEFAULT_DRIVE_HEAD


def _base_url(lib: Dict[str, Any], base_url: Optional[str] = None) -> str:
    """strm 里写的网关前缀：显式参数 > 库配置 > 全局配置 > 兜底常量。"""
    for cand in (base_url, lib.get("base_url")):
        v = str(cand or "").strip().rstrip("/")
        if v:
            return v
    if config is not None:
        try:
            v = str((config.load() or {}).get("base_url") or "").strip().rstrip("/")
            if v:
                return v
        except Exception:
            pass
    return DEFAULT_BASE_URL


def _bdmv_enabled(lib: Dict[str, Any]) -> bool:
    """bdmv 开关：兼容 bool 与 strm_lib 的 skip / main 枚举。"""
    raw = lib.get("bdmv")
    if isinstance(raw, bool):
        return raw
    s = str(raw if raw is not None else "").strip().lower()
    if s in _BDMV_OFF:
        return False
    return True


def _keep_first(lib: Dict[str, Any]) -> int:
    """保留前 N 层目录作为输出子目录（strm_lib 里存成布尔，落库为 0/1）。"""
    return max(0, _to_int(lib.get("keep_first"), 0))


def _is_safe_target(base: str, target: str) -> bool:
    """目标路径必须落在 base 之内（realpath 前缀判断，防 ../ 穿越 / 软链逃逸）。"""
    try:
        rb = os.path.realpath(base)
        rt = os.path.realpath(target)
    except OSError:
        return False
    return rt == rb or rt.startswith(rb + os.sep)


def _bdmv_stream_item(rel: str, name: str) -> bool:
    """判断是否 BDMV 盘内的主流文件：…/xxx.BDMV/BDMV/STREAM/*.m2ts。"""
    if not str(name).lower().endswith(".m2ts"):
        return False
    parts = [p.lower() for p in str(rel).split("/")]
    for i, seg in enumerate(parts[:-1]):
        if seg.endswith(".bdmv"):
            return parts[i + 1:i + 3] == ["bdmv", "stream"]
    return False


# ---------------------------------------------------------------------------
# 1. 生成 strm 内容
# ---------------------------------------------------------------------------

def build_content(rel_path: str, item: Dict[str, Any],
                  lib: Dict[str, Any], base_url: Optional[str] = None) -> str:
    """
    生成 .strm 第一行内容（不含结尾换行）。

    Parameters
    ----------
    rel_path : 相对路径（**不含 drive 首段**），如 Video/Movie/xxx.mkv
    item     : 扫描到的目录项，pc 型需要其中的 "pc"
    lib      : 媒体库记录
    base_url : 覆盖库里的 base_url

    Returns
    -------
    str

    Raises
    ------
    ValueError —— pc 型缺 pickcode 时抛出（由上层记为失败条目）
    """
    fmt = str(lib.get("fmt") or "path").strip().lower()
    rel = str(rel_path or "").strip().strip("/")
    head = _drive_head(lib)
    # src_path = 起点目录相对 115 根的路径（walk 只给相对起点的路径，
    # 而换链 / CD2 挂载都需要从根开始的全路径，缺它生成的 strm 解析必然失败）
    mid = str(lib.get("src_path") or "").strip().strip("/")
    full = "{}/{}".format(mid, rel) if mid else rel

    if fmt == "path":
        cd2 = str(lib.get("cd2_prefix") or DEFAULT_CD2_PREFIX).strip().rstrip("/")
        return "{}/{}/{}".format(cd2, head, full) if full else "{}/{}".format(cd2, head)

    if fmt == "pc":
        pc = str((item or {}).get("pc") or "").strip()
        if not pc:
            raise ValueError("缺少 pickcode，无法生成 pc 型 strm")
        return "{}/api/redirect115/{}".format(_base_url(lib, base_url), pc)

    if fmt == "evolink":
        full_with_head = "{}/{}".format(head, full) if full else head
        return "{}/api/swap115/{}".format(
            _base_url(lib, base_url), urllib.parse.quote(full_with_head, safe=""))

    raise ValueError("不支持的 strm 格式：{}".format(fmt))


# ---------------------------------------------------------------------------
# 2. 相对路径
# ---------------------------------------------------------------------------

def rel_path(item: Dict[str, Any], lib: Dict[str, Any]) -> str:
    """
    扫描到的目录项 → 相对路径字符串（**不含 drive 首段**）。

    115 根目录下直接是 Video / Photo / …，不存在 115open 这一层；
    但若起点选在存储名层，首段仍会被剥掉，保证与存量 Link 结构同构。
    """
    raw = str((item or {}).get("path") or (item or {}).get("name") or "")
    parts = [p for p in raw.replace("\\", "/").split("/") if p and p != "."]
    head = _drive_head(lib)
    if head and parts and parts[0] == head:
        parts = parts[1:]
    return "/".join(parts)


# ---------------------------------------------------------------------------
# 3. 选文件
# ---------------------------------------------------------------------------

def select_files(items: List[Dict[str, Any]], lib: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    从扫描结果里挑出要生成 strm 的文件。

    规则（依次判定）：
      1. 必须是文件（is_dir 为假）；
      2. 扩展名在白名单内（strm_ext 为空时退化为 pan115.is_video 判定）；
      3. 名字不含排除词（exclude_words 支持逗号分隔 / 通配符）；
      4. bdmv 开启时额外接受 .iso 与 .bdmv/BDMV/STREAM/*.m2ts。
    """
    ext_list = str(lib.get("strm_ext") or "").strip()
    exclude = str(lib.get("exclude_words") or "").strip()
    bdmv_on = _bdmv_enabled(lib)

    out: List[Dict[str, Any]] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        if it.get("is_dir"):
            continue
        name = str(it.get("name") or "").strip()
        if not name:
            continue

        if ext_list:
            allowed = strm_lib.ext_allowed(name, ext_list)
        else:
            allowed = pan115.is_video(name)

        if not allowed:
            # bdmv 开启 → 额外接受 .iso 与 BDMV 盘内主流 .m2ts
            if not bdmv_on:
                continue
            rp = rel_path(it, lib)
            low = name.lower()
            if not (low.endswith(".iso") or _bdmv_stream_item(rp, name)):
                continue

        if exclude and strm_lib.is_excluded(name, exclude):
            continue

        out.append(it)
    return out


# ---------------------------------------------------------------------------
# 4. 扫描
# ---------------------------------------------------------------------------

def scan_lib(lib_id: str, ck: Optional[Dict[str, Any]] = None,
             progress: Optional[Any] = None) -> Dict[str, Any]:
    """
    扫描一个库的媒体目录树，返回 {"files": [...], "dirs": [...], "count": n}。

    两种扫描模式（lib["scan_mode"]）：
    * local（默认，推荐）—— 直接遍历本地 CD2 挂载目录（lib["local_dir"]），
      **全程零 115 API 请求，风控零影响**。实现方式：
      同样是扫 /CloudNAS/CloudDrive/... 本地挂载来建 strm。
      项结构：{"name","path","is_dir","size","ext"}，path 为相对 local_dir 的路径；
      无 pickcode —— pc 格式不支持 local 模式（写盘时会逐条报错跳过）。
    * api —— 走 pan115.walk 打 115 接口扫（仅小范围/增量用，大库会触发风控）。

    两种模式扫描阶段都固定单线程（local 遍历本地盘不构成 API 压力，
    但统一行为避免误解；只有写盘阶段并发）。
    """
    lib = strm_lib.get_lib(lib_id)
    if not lib:
        raise ValueError("媒体库不存在：{}".format(lib_id))

    depth = max(1, min(20, _to_int(lib.get("depth"), 5)))

    def _prog(msg: str) -> None:
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    if str(lib.get("scan_mode") or "local").strip().lower() == "local":
        items, dirs = _walk_local(lib, depth, _prog)
        return {"files": list(items), "dirs": list(dirs), "count": len(items)}

    root = str(lib.get("src_cid") or "0").strip() or "0"
    items, dirs = pan115.walk(root=root, ck=ck, depth=depth, progress=progress)
    return {"files": list(items), "dirs": list(dirs), "count": len(items)}


def _walk_local(lib: Dict[str, Any], depth: int,
                progress: Optional[Any] = None) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    遍历本地 CD2 挂载目录（零 115 API 请求）。

    返回 (items, dirs)，结构与 pan115.walk 同构：
      item = {"name", "path"(相对 local_dir), "is_dir", "size", "ext", "fid", "cid", "pc"}
    其中 fid/cid/pc 置空 —— local 模式不与 115 交互，pc 格式不可用。
    """
    root = str(lib.get("local_dir") or "").strip()
    if not root:
        raise ValueError("scan_mode=local 需要 local_dir")
    if not os.path.isdir(root):
        raise ValueError("local_dir 不存在或不可访问：{}（检查 evolink 容器是否挂载了 CD2 路径）".format(root))

    items: List[Dict[str, Any]] = []
    dirs: List[Dict[str, Any]] = []
    root = os.path.realpath(root)
    stack: List[Tuple[str, int]] = [(root, 1)]
    while stack:
        cur, lv = stack.pop(0)
        rel_dir = os.path.relpath(cur, root)
        if rel_dir == ".":
            rel_dir = ""
        try:
            entries = sorted(os.scandir(cur), key=lambda e: e.name)
        except OSError as e:
            progress("  ! 读取目录失败 {}：{}".format(rel_dir or "/", e))
            continue
        for e in entries:
            n = e.name
            full = e.path
            try:
                is_dir = e.is_dir(follow_symlinks=False)
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            rel = "{}/{}".format(rel_dir, n) if rel_dir else n
            ext = os.path.splitext(n)[1].lower()
            if is_dir:
                d = {"name": n, "path": rel, "is_dir": True, "size": 0,
                     "ext": "", "fid": "", "cid": "", "pc": ""}
                dirs.append(d)
                if lv < depth:
                    stack.append((full, lv + 1))
            else:
                items.append({"name": n, "path": rel, "is_dir": False,
                              "size": st.st_size, "ext": ext,
                              "fid": "", "cid": "", "pc": ""})
        progress("  [local] {} -> 累计 {} 文件".format(rel_dir or "/", len(items)))
    return items, dirs


# ---------------------------------------------------------------------------
# 5. 写 strm
# ---------------------------------------------------------------------------

def _write_one(path: str, content: str) -> None:
    """写单个 strm（先写临时文件再改名，避免写一半被扫描到）。"""
    tmp = path + ".tmp-sync"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def _list_strm_in(dirpath: str) -> List[str]:
    """列出目录下的 .strm 文件（只取文件，不递归）。"""
    try:
        names = os.listdir(dirpath)
    except OSError:
        return []
    out = []
    for n in names:
        if not n.lower().endswith(STRM_EXT):
            continue
        p = os.path.join(dirpath, n)
        if os.path.isfile(p):
            out.append(p)
    return out


def write_strm(files: List[Dict[str, Any]], lib: Dict[str, Any],
               dry_run: bool = False) -> Dict[str, Any]:
    """
    把文件列表写成 strm。

    * 输出目录 = os.path.join(outdir, 子目录)，子目录取相对路径前 N 段
      （N = keep_first，0 为平铺）；
    * 文件名 = 相对路径最后一段 + ".strm"；
    * force=True 时先删同目录下的旧 .strm（只删 .strm 文件，不删目录）；
    * delete_confirm=True 时不真删，而是把待删清单放进 need_confirm 交给上层确认；
    * 目标路径必须落在 outdir 之内，否则记为错误条目并跳过。

    dry_run=True 时只做校验与统计，不落盘：「planned」为将要写出的数量，
    「written」恒为 0。

    Returns
    -------
    dict —— {"written": n, "skipped": n, "removed": n, "errors": [...],
             "need_confirm": [...], "planned": n, "dry_run": bool}
    """
    outdir = str(lib.get("outdir") or "").strip()
    if not outdir:
        raise ValueError("媒体库未配置输出目录 outdir")

    base = os.path.abspath(os.path.expanduser(outdir))
    keep = _keep_first(lib)
    force = bool(lib.get("force"))
    delete_confirm = bool(lib.get("delete_confirm"))
    threads = max(1, min(16, _to_int(lib.get("threads"), 1)))

    errors: List[str] = []
    need_confirm: List[str] = []
    seen: set = set()
    tasks: List[Tuple[Dict[str, Any], str, str]] = []

    # ---- 第一步：算目标路径（串行，先把非法路径挑出来）----
    for it in files or []:
        name = str((it or {}).get("name") or "")
        try:
            rp = rel_path(it, lib)
        except Exception as e:
            errors.append("{}: 计算相对路径失败 {}".format(name, e))
            continue
        if not rp:
            errors.append("{}: 相对路径为空".format(name))
            continue
        parts = rp.split("/")
        if any(p in ("..", ".") for p in parts):
            errors.append("{}: 非法路径（包含 .. ），已拦截".format(rp))
            continue
        fname = parts[-1] + STRM_EXT
        subdirs = parts[:-1][:keep]
        target = os.path.join(base, *subdirs) if subdirs else base
        target = os.path.join(target, fname)
        if not _is_safe_target(base, target):
            errors.append("{}: 目标路径越界，已拦截".format(rp))
            continue
        if target in seen:
            continue
        seen.add(target)
        tasks.append((it, rp, target))

    # ---- 第二步：force 清理旧 strm ----
    removed = 0
    if force and tasks:
        for d in sorted({os.path.dirname(t[2]) for t in tasks}):
            for p in _list_strm_in(d):
                if delete_confirm:
                    need_confirm.append(p)
                    continue
                if dry_run:
                    continue
                try:
                    os.remove(p)
                    removed += 1
                except OSError as e:
                    errors.append("{}: 删除旧 strm 失败 {}".format(p, e))

    # ---- 第三步：写盘（threads > 1 时并发）----
    counters = {"written": 0, "skipped": 0}
    lock = threading.Lock()

    def _worker(q: "queue.Queue[Any]") -> None:
        while True:
            try:
                it, rp, target = q.get_nowait()
            except queue.Empty:
                return
            try:
                content = build_content(rp, it, lib) + "\n"
                d = os.path.dirname(target)
                if d and not os.path.isdir(d):
                    os.makedirs(d, exist_ok=True)
                if not _is_safe_target(base, target):
                    with lock:
                        errors.append("{}: 写入前路径校验失败".format(rp))
                    continue
                if os.path.isfile(target):
                    try:
                        with open(target, "r", encoding="utf-8", errors="ignore") as f:
                            if f.read() == content:
                                with lock:
                                    counters["skipped"] += 1
                                continue
                    except OSError:
                        pass
                _write_one(target, content)
                with lock:
                    counters["written"] += 1
            except Exception as e:
                with lock:
                    errors.append("{}: 写入失败 {}: {}".format(rp, type(e).__name__, e))
            finally:
                q.task_done()

    planned = len(tasks)
    if tasks and not dry_run:
        q: "queue.Queue[Any]" = queue.Queue()
        for t in tasks:
            q.put(t)
        n = min(threads, len(tasks))
        workers = [threading.Thread(target=_worker, args=(q,), daemon=True) for _ in range(n)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()

    return {
        "written": counters["written"],
        "skipped": counters["skipped"],
        "removed": removed,
        "errors": errors,
        "need_confirm": need_confirm,
        "planned": planned,
        "dry_run": bool(dry_run),
    }


# ---------------------------------------------------------------------------
# 6. 跑一个库
# ---------------------------------------------------------------------------

def sync_lib(lib_id: str, dry_run: bool = False, ck: Optional[Dict[str, Any]] = None,
             progress: Optional[Any] = None) -> Dict[str, Any]:
    """
    完整跑一个库：扫描 → 选文件 → 写 strm → 记录运行结果。

    Returns
    -------
    dict —— {"lib_id", "name", "files", "scanned", "written", "skipped", "removed",
             "planned", "errors", "need_confirm", "started_at", "finished_at", "status"}

    status: ok（无错误）/ partial（有错但写出了文件）/ error（整体失败）
    """
    started = _now()
    lib = strm_lib.get_lib(lib_id)
    if not lib:
        raise ValueError("媒体库不存在：{}".format(lib_id))

    # path / evolink 两种格式需要从 115 根开始的全路径，src_path 必填（提前失败，避免整库逐条报错）
    fmt = str(lib.get("fmt") or "path").strip().lower()
    if fmt in ("path", "evolink") and not str(lib.get("src_path") or "").strip().strip("/"):
        raise ValueError("fmt={} 需要配置 src_path（起点目录相对 115 根的路径，如 Video/Movie）".format(fmt))

    result: Dict[str, Any] = {
        "lib_id": lib_id,
        "name": str(lib.get("name") or ""),
        "files": 0,
        "scanned": 0,
        "written": 0,
        "skipped": 0,
        "removed": 0,
        "planned": 0,
        "errors": [],
        "need_confirm": [],
        "started_at": started,
        "finished_at": "",
        "status": "error",
    }

    status = "error"
    try:
        scan = scan_lib(lib_id, ck=ck, progress=progress)
        selected = select_files(scan.get("files") or [], lib)
        result["files"] = len(selected)
        result["scanned"] = scan.get("count", 0)

        w = write_strm(selected, lib, dry_run=dry_run)
        result["written"] = w["written"]
        result["skipped"] = w["skipped"]
        result["removed"] = w["removed"]
        result["planned"] = w.get("planned", 0)
        result["errors"] = list(w["errors"])
        result["need_confirm"] = list(w["need_confirm"])

        if not w["errors"]:
            status = "ok"
        elif w["written"] > 0:
            status = "partial"
        else:
            status = "error"
    except Exception as e:
        result["errors"].append("{}: {}".format(type(e).__name__, e))
        status = "error"

    result["status"] = status
    result["finished_at"] = _now()

    try:
        strm_lib.record_run(
            lib_id,
            started_at=result["started_at"],
            finished_at=result["finished_at"],
            ok=1 if status == "ok" else 0,
            created=result["written"],
            updated=0,
            skipped=result["skipped"],
            removed=result["removed"],
            failed=len(result["errors"]),
            message="; ".join(result["errors"])[:500],
        )
    except Exception:
        pass

    return result


# ---------------------------------------------------------------------------
# 7. 定时 / 全量
# ---------------------------------------------------------------------------

def _parse_now(now: Any) -> Any:
    """now 支持 datetime 或 "%Y-%m-%d %H:%M:%S" 字符串。"""
    if not isinstance(now, str):
        return now
    import datetime as _dt
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return _dt.datetime.strptime(now, fmt)
        except ValueError:
            continue
    return now


def _run_one(lib: Dict[str, Any], ck: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """跑单个库；异常也转成一条 error 结果，不影响其它库。"""
    try:
        return sync_lib(lib.get("id"), ck=ck)
    except Exception as e:
        return {
            "lib_id": lib.get("id"),
            "name": lib.get("name", ""),
            "files": 0, "scanned": 0, "written": 0, "skipped": 0, "removed": 0,
            "planned": 0,
            "errors": ["{}: {}".format(type(e).__name__, e)],
            "need_confirm": [], "started_at": _now(), "finished_at": _now(),
            "status": "error",
        }


def run_due_libs(now: Any = None, ck: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    跑所有「到点」的库：enabled 且 scheduled，且 cron_due 判定该跑了。

    Returns
    -------
    list[dict] —— 每个被执行库的 sync_lib 结果
    """
    out: List[Dict[str, Any]] = []
    now = _parse_now(now)
    for lib in strm_lib.list_libs():
        if not lib.get("enabled"):
            continue
        if not lib.get("scheduled"):
            continue
        try:
            due = strm_lib.cron_due(
                str(lib.get("cron") or "").strip(),
                str(lib.get("last_run_at") or "").strip(),
                now,
            )
        except Exception:
            due = False
        if not due:
            continue
        out.append(_run_one(lib, ck))
    return out


def run_all(now: Any = None, ck: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    无条件跑所有 enabled 的库（手动「全部同步」按钮用）。

    Returns
    -------
    list[dict] —— 每个库的 sync_lib 结果
    """
    out: List[Dict[str, Any]] = []
    for lib in strm_lib.list_libs():
        if not lib.get("enabled"):
            continue
        out.append(_run_one(lib, ck))
    return out
