


"""
core/strm_lib.py — EvoLink 媒体库（Strm Library）持久化模型
==============================================================

职责
----
为 EvoLink 提供「媒体库」的增删改查、运行记录、字段规范化、
排除词/扩展名匹配、cron 到期判断，以及将库记录转换为
strm-gen 所需的 opts 字典。

设计说明
--------
* EvoLink 的能力边界：无软链接、无元数据拷贝、
  无 Webhook 观察者、无 CD2 中转。本模块只保留 EvoLink
  实际支持的字段，并额外支持 123 网盘 / 光鸭网盘（drive 字段）。
* 数据持久化在 /app/data/vl.db（SQLite），通过 core.db 提供的
  conn() / write_lock() / now() 访问，不直接操作文件。
* 所有写操作走 db.write_lock() 上下文管理器保证串行化。
* 纯标准库实现，无第三方依赖。

数据表
------
* strm_lib      —— 媒体库定义（一行一个库）
* strm_lib_run  —— 每次同步的运行记录（审计 / 前端展示）
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import time
import uuid
import datetime
from typing import Any, Dict, List, Optional, Set

from core import db

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_DRIVE_WHITELIST = ("115", "123", "guangya")
_FMT_WHITELIST = ("path", "pc", "evolink")
_BDMV_WHITELIST = ("skip", "main")

# 扫描模式说明（前端选择器直接用）：
# local = 遍历本地 CD2 挂载目录，零 115 API 请求，风控零影响（推荐）；
# api   = 走 115 接口逐级拉目录树，无需 CD2 挂载，但大库高频请求有风控风险，
#         建议只用于小范围库或增量补扫。
SCAN_MODE_INFO = {
    "local": {
        "label": "本地挂载（CD2，推荐）",
        "desc": "直接遍历 CD2/CloudDrive2 挂载目录建立 strm，全程不请求 115，"
                "永远不会触发风控。需要 evolink 容器挂载 CD2 路径（默认 /CloudNAS）。"
                "支持 path / evolink 格式，不支持 pc（pc 需要 pickcode）。",
        "need_pickcode": False,
    },
    "api": {
        "label": "115 接口扫目录树",
        "desc": "无需 CD2 挂载，通过 115 接口逐级拉目录树建 strm。"
                "注意：大库全量扫描会高频请求 115，有触发风控的风险，"
                "建议仅用于小范围媒体库或增量补扫；风控期间自动降级到搜索接口。",
        "need_pickcode": True,
    },
}

# 表字段白名单（normalize 时只保留这些键）
_COLUMNS: List[str] = [
    "id", "name", "drive", "src_cid", "src_path", "outdir", "fmt",
    "cd2_prefix", "store_prefix", "base_url", "bdmv",
    "exclude_words", "strm_ext", "depth", "threads",
    "scan_mode", "local_dir",
    "scheduled", "cron", "force", "delete_confirm",
    "keep_first", "enabled", "last_run_at", "last_run_stat",
    "created_at", "updated_at",
]

# 布尔型字段
_BOOL_FIELDS = ("scheduled", "force", "delete_confirm", "keep_first", "enabled")

# 输出目录合法前缀
_OUTDIR_PREFIXES = ("/app/Link", "/app/data")


# ---------------------------------------------------------------------------
# 1. 建表
# ---------------------------------------------------------------------------

def init() -> None:
    """幂等建表。在应用启动时调用一次即可。"""
    conn = db.conn()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS strm_lib (
                id              TEXT PRIMARY KEY,
                name            TEXT NOT NULL,
                drive           TEXT DEFAULT '115',
                src_cid         TEXT DEFAULT '',
                src_path        TEXT DEFAULT '',
                outdir          TEXT DEFAULT '',
                fmt             TEXT DEFAULT 'path',
                cd2_prefix      TEXT DEFAULT '/CloudNAS/CloudDrive',
                store_prefix    TEXT DEFAULT '115open',
                base_url        TEXT DEFAULT '',
                bdmv            TEXT DEFAULT 'skip',
                exclude_words   TEXT DEFAULT '',
                strm_ext        TEXT DEFAULT '',
                depth           INTEGER DEFAULT 5,
                threads         INTEGER DEFAULT 2,
                scan_mode       TEXT DEFAULT 'local',
                local_dir       TEXT DEFAULT '',
                scheduled       INTEGER DEFAULT 0,
                cron            TEXT DEFAULT '',
                force           INTEGER DEFAULT 0,
                delete_confirm  INTEGER DEFAULT 1,
                keep_first      INTEGER DEFAULT 0,
                enabled         INTEGER DEFAULT 1,
                last_run_at     TEXT DEFAULT '',
                last_run_stat   TEXT DEFAULT '',
                created_at      TEXT DEFAULT '',
                updated_at      TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS strm_lib_run (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                lib_id       TEXT NOT NULL,
                started_at   TEXT NOT NULL,
                finished_at  TEXT DEFAULT '',
                ok           INTEGER DEFAULT 0,
                created      INTEGER DEFAULT 0,
                updated      INTEGER DEFAULT 0,
                skipped      INTEGER DEFAULT 0,
                removed      INTEGER DEFAULT 0,
                failed       INTEGER DEFAULT 0,
                message      TEXT DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_strm_lib_run_lib ON strm_lib_run(lib_id);
            """
        )
        # 迁移：老库没有 src_path 列时补上（幂等）
        cols = [r[1] for r in conn.execute("PRAGMA table_info(strm_lib)").fetchall()]
        for col, ddl in (("src_path", "TEXT DEFAULT ''"),
                         ("scan_mode", "TEXT DEFAULT 'local'"),
                         ("local_dir", "TEXT DEFAULT ''")):
            if cols and col not in cols:
                conn.execute(f"ALTER TABLE strm_lib ADD COLUMN {col} {ddl}")
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2. CRUD
# ---------------------------------------------------------------------------

def list_libs(drive: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    列出所有媒体库。

    Parameters
    ----------
    drive : 可选，按网盘类型过滤（'115' / '123' / 'guangya'）。

    Returns
    -------
    list[dict]
    """
    conn = db.conn()
    try:
        if drive:
            rows = conn.execute(
                "SELECT * FROM strm_lib WHERE drive = ? ORDER BY name",
                (drive,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM strm_lib ORDER BY name"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_lib(lib_id: str) -> Optional[Dict[str, Any]]:
    """
    按 id 获取单个库。

    Returns
    -------
    dict | None
    """
    conn = db.conn()
    try:
        row = conn.execute(
            "SELECT * FROM strm_lib WHERE id = ?", (lib_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def save_lib(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    新增或更新一个媒体库。

    * id 为空 → 生成 uuid4 hex，INSERT
    * id 非空 → UPDATE（保留 created_at，刷新 updated_at）

    Parameters
    ----------
    data : 原始字段字典（会经过 normalize 校验）

    Returns
    -------
    dict —— 保存后的完整记录
    """
    rec = normalize(data)
    now = db.now()

    if not rec.get("id"):
        rec["id"] = uuid.uuid4().hex
        rec["created_at"] = now
        rec["updated_at"] = now
        cols = [c for c in _COLUMNS if c in rec]
        placeholders = ", ".join("?" for _ in cols)
        col_str = ", ".join(cols)
        values = [rec[c] for c in cols]
        with db.write_lock():
            conn = db.conn()
            try:
                conn.execute(
                    f"INSERT INTO strm_lib ({col_str}) VALUES ({placeholders})",
                    values,
                )
                conn.commit()
            finally:
                conn.close()
    else:
        # 保留 created_at
        existing = get_lib(rec["id"])
        if existing is None:
            raise ValueError(f"库 {rec['id']} 不存在")
        rec["created_at"] = existing["created_at"]
        rec["updated_at"] = now
        set_clause = ", ".join(f"{c} = ?" for c in _COLUMNS if c != "id")
        values = [rec[c] for c in _COLUMNS if c != "id"]
        values.append(rec["id"])
        with db.write_lock():
            conn = db.conn()
            try:
                conn.execute(
                    f"UPDATE strm_lib SET {set_clause} WHERE id = ?",
                    values,
                )
                conn.commit()
            finally:
                conn.close()

    return get_lib(rec["id"])  # type: ignore[return-value]


def delete_lib(lib_id: str) -> bool:
    """
    删除一个媒体库（同时删除其运行记录）。

    Returns
    -------
    bool —— 是否实际删除了行
    """
    with db.write_lock():
        conn = db.conn()
        try:
            cur = conn.execute("DELETE FROM strm_lib WHERE id = ?", (lib_id,))
            conn.execute("DELETE FROM strm_lib_run WHERE lib_id = ?", (lib_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def set_lib_enabled(lib_id: str, enabled: bool) -> bool:
    """
    启用 / 禁用一个库。

    Returns
    -------
    bool —— 是否更新成功
    """
    val = 1 if enabled else 0
    with db.write_lock():
        conn = db.conn()
        try:
            cur = conn.execute(
                "UPDATE strm_lib SET enabled = ?, updated_at = ? WHERE id = ?",
                (val, db.now(), lib_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def record_run(lib_id: str, **kw: Any) -> int:
    """
    写入一条运行记录，并回写库的 last_run_at / last_run_stat。

    Parameters
    ----------
    lib_id : 库 id
    **kw   : started_at, finished_at, ok, created, updated,
             skipped, removed, failed, message

    Returns
    -------
    int —— 新插入行的自增 id
    """
    now = db.now()
    started_at = kw.get("started_at", now)
    finished_at = kw.get("finished_at", now)
    ok = int(kw.get("ok", 0))
    created = int(kw.get("created", 0))
    updated = int(kw.get("updated", 0))
    skipped = int(kw.get("skipped", 0))
    removed = int(kw.get("removed", 0))
    failed = int(kw.get("failed", 0))
    message = str(kw.get("message", ""))

    stat = json.dumps({
        "ok": ok, "created": created, "updated": updated,
        "skipped": skipped, "removed": removed, "failed": failed,
    }, ensure_ascii=False)

    with db.write_lock():
        conn = db.conn()
        try:
            cur = conn.execute(
                """INSERT INTO strm_lib_run
                   (lib_id, started_at, finished_at, ok, created, updated,
                    skipped, removed, failed, message)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (lib_id, started_at, finished_at, ok, created, updated,
                 skipped, removed, failed, message),
            )
            conn.execute(
                "UPDATE strm_lib SET last_run_at = ?, last_run_stat = ?, updated_at = ? WHERE id = ?",
                (finished_at, stat, now, lib_id),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()


def recent_runs(lib_id: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
    """
    查询最近的运行记录。

    Parameters
    ----------
    lib_id : 可选，按库过滤
    limit  : 返回条数上限

    Returns
    -------
    list[dict]
    """
    conn = db.conn()
    try:
        if lib_id:
            rows = conn.execute(
                "SELECT * FROM strm_lib_run WHERE lib_id = ? ORDER BY id DESC LIMIT ?",
                (lib_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM strm_lib_run ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3. 字段规范化
# ---------------------------------------------------------------------------

def _to_bool(val: Any) -> int:
    """将各种布尔表示统一为 0/1。"""
    if isinstance(val, bool):
        return 1 if val else 0
    if isinstance(val, (int, float)):
        return 1 if val else 0
    if isinstance(val, str):
        return 1 if val.strip().lower() in ("1", "true", "yes", "on") else 0
    return 0


def _clamp(val: Any, lo: int, hi: int, default: int = 0) -> int:
    """转 int 并夹紧到 [lo, hi]。转换失败返回 default（default 缺省为 0）。"""
    try:
        v = int(val)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _normalize_csv(val: Any) -> str:
    """
    逗号分隔字符串规范化：去空白、去空项、去重、英文逗号连接。
    """
    if not val:
        return ""
    if isinstance(val, (list, tuple)):
        items = [str(x).strip() for x in val]
    else:
        items = [x.strip() for x in str(val).split(",")]
    seen: Set[str] = set()
    out: List[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return ",".join(out)


def normalize(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    将原始输入字典规范化为合法记录。

    * 只保留 _COLUMNS 白名单内的键
    * 各字段做类型 / 范围 / 枚举校验
    * 非法值抛 ValueError

    Returns
    -------
    dict —— 规范化后的字段字典
    """
    rec: Dict[str, Any] = {}

    # --- 只保留白名单字段 ---
    for key in _COLUMNS:
        if key in data:
            rec[key] = data[key]

    # --- name ---
    name = str(rec.get("name", "")).strip()
    if not name:
        raise ValueError("库名不能为空")
    rec["name"] = name

    # --- drive ---



    drive = str(rec.get("drive") or "115").strip().lower()
    if drive not in _DRIVE_WHITELIST:
        raise ValueError("网盘类型只能是 115 / 123 / guangya")
    rec["drive"] = drive

    rec["src_cid"] = str(rec.get("src_cid") or "").strip()
    # src_path：起点目录相对 115 根的路径（如 Video/Movie/欧美电影/阿凡达系列），
    # path / evolink 两种格式生成 strm 时必须拼在 rel_path 前面
    rec["src_path"] = "/".join(
        p for p in str(rec.get("src_path") or "").replace("\\", "/").split("/")
        if p and p not in (".", "..")
    )

    # scan_mode：local = 遍历本地 CD2 挂载目录（零 115 请求，
    # 风控零影响，推荐）；api = 走 115 接口扫（仅小范围/增量用）。
    # 未显式指定时：有 local_dir → local，否则 api（向后兼容老数据/旧接口调用）。
    mode_raw = str(rec.get("scan_mode") or "").strip().lower()
    ld_raw = str(rec.get("local_dir") or "").strip()
    if not mode_raw:
        mode = "local" if ld_raw else "api"
    elif mode_raw in ("local", "api"):
        mode = mode_raw
    else:
        raise ValueError("scan_mode 只能是 local 或 api")
    rec["scan_mode"] = mode
    # local_dir：本地挂载起点目录（容器内路径，如
    # /CloudNAS/CloudDrive/115open/Video/Movie），local 模式必填
    if ld_raw and not ld_raw.startswith("/"):
        raise ValueError("local_dir 必须是容器内绝对路径")
    if mode == "local" and not ld_raw:
        raise ValueError("scan_mode=local 需要配置 local_dir（本地挂载起点目录）")
    rec["local_dir"] = ld_raw.rstrip("/")

    outdir = str(rec.get("outdir") or "").strip()
    if outdir and not (outdir == "/app/Link" or outdir.startswith("/app/Link/")
                        or outdir == "/app/data" or outdir.startswith("/app/data/")):
        raise ValueError("输出目录必须在 /app/Link 或 /app/data 下")
    rec["outdir"] = outdir

    fmt = str(rec.get("fmt") or "path").strip().lower()
    if fmt not in _FMT_WHITELIST:
        raise ValueError("strm 格式只能是 path / pc / evolink")
    rec["fmt"] = fmt

    bdmv = str(rec.get("bdmv") or "skip").strip().lower()
    if bdmv not in _BDMV_WHITELIST:
        raise ValueError("BDMV 策略只能是 skip 或 main")
    rec["bdmv"] = bdmv

    rec["cd2_prefix"] = str(rec.get("cd2_prefix") or "/CloudNAS/CloudDrive").strip().rstrip("/")
    rec["store_prefix"] = str(rec.get("store_prefix") or "115open").strip().strip("/")
    rec["base_url"] = str(rec.get("base_url") or "").strip().rstrip("/")

    rec["exclude_words"] = _normalize_csv(rec.get("exclude_words"))
    # 扩展名白名单统一去掉前导点并转小写再去重（".MKV" 与 "mkv" 视为同一项）
    rec["strm_ext"] = _normalize_csv(
        [("." + str(x).strip().lstrip(".")).lower() if str(x).strip() else ""
         for x in _normalize_csv(rec.get("strm_ext")).split(",")]
    ).lstrip(".")

    rec["depth"] = _clamp(_to_int(rec.get("depth"), 5), 1, 20, 5)
    rec["threads"] = _clamp(_to_int(rec.get("threads"), 2), 1, 16, 2)

    for f in _BOOL_FIELDS:
        if f in rec:
            rec[f] = 1 if _to_bool(rec[f]) else 0
    # 缺省的布尔字段补默认
    rec.setdefault("scheduled", 0); rec.setdefault("force", 0)
    rec.setdefault("delete_confirm", 1); rec.setdefault("keep_first", 0)
    rec.setdefault("enabled", 1)

    cron = str(rec.get("cron") or "").strip()
    if cron:
        parts = cron.split()
        if len(parts) != 5:
            raise ValueError("cron 必须是 5 段（分 时 日 月 周）")
    rec["cron"] = cron

    rec["last_run_at"] = str(rec.get("last_run_at") or "").strip()
    rec["last_run_stat"] = str(rec.get("last_run_stat") or "").strip()
    rec["created_at"] = str(rec.get("created_at") or "").strip()
    rec["updated_at"] = str(rec.get("updated_at") or "").strip()
    return rec


def _to_int(v, default=0) -> int:
    """宽松转 int：失败或为空返回 default。"""
    try:
        if v is None or (isinstance(v, str) and not v.strip()):
            return default
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def is_excluded(name: str, exclude_words: str) -> bool:
    """判断文件名/路径是否命中排除词。

    - exclude_words 为空 → False（不排除）
    - 不含 '*' → 子串匹配（不区分大小写）
    - 含 '*' → fnmatch 风格匹配（两边都 lower）
    - 多个词用逗号分隔，任一命中即 True
    """
    if not exclude_words:
        return False
    name_lower = name.lower()
    for word in exclude_words.split(","):
        word = word.strip()
        if not word:
            continue
        if "*" in word:
            if fnmatch.fnmatch(name_lower, word.lower()):
                return True
        else:
            if word.lower() in name_lower:
                return True
    return False


def ext_allowed(name: str, strm_ext: str) -> bool:
    """判断扩展名是否在白名单内。

    - strm_ext 为空 → True（交由调用方用 pan115.is_video 判定）
    - 取最后一个 '.' 之后的扩展名（lower），与白名单比较
    - 白名单项可带点可不带点（两边都去点 + lower 比较）
    """
    if not strm_ext:
        return True
    # 取扩展名：最后一个 '.' 之后
    dot_idx = name.rfind(".")
    if dot_idx < 0:
        return False
    ext = name[dot_idx + 1:].lower().strip(".")
    if not ext:
        return False
    for item in strm_ext.split(","):
        item = item.strip().lower().strip(".")
        if not item:
            continue
        if ext == item:
            return True
    return False


def _cron_field_match(field: str, value: int) -> bool:
    """单个 cron 字段（分/时/日/月/周）与取值是否匹配。

    支持：'*'、'5'、'1,2,3'、'1-5'、'*/2'、'a-b/n'
    周字段额外支持 0 和 7 都表示周日（统一按 0 处理：value%7）
    """
    field = field.strip()
    if not field:
        return False
    for part in field.split(","):
        part = part.strip()
        if not part:
            continue
        # 解析 step
        step = 1
        if "/" in part:
            part, step_str = part.split("/", 1)
            try:
                step = max(1, int(step_str.strip()))
            except ValueError:
                step = 1
        if part == "*":
            # */n → 从 0 开始每 n 个
            if value % step == 0:
                return True
        elif "-" in part:
            lo_str, hi_str = part.split("-", 1)
            try:
                lo = int(lo_str.strip())
                hi = int(hi_str.strip())
            except ValueError:
                continue
            if lo > hi:
                lo, hi = hi, lo
            if lo <= value <= hi and (value - lo) % step == 0:
                return True
        else:
            # 单个值
            try:
                if int(part) == value:
                    return True
            except ValueError:
                pass
    return False


def cron_due(cron_expr: str, last_run_at: str = "", now=None) -> bool:
    """判断某个库此刻是否该跑。

    - cron_expr 为空 → False
    - 解析 5 段 → (分, 时, 日, 月, 周)
    - 把 now 向下取整到分钟，若该分钟匹配 cron，且该分钟严格晚于 last_run_at → True
    - last_run_at 为空或解析失败 → 视为从未运行过，只要当前分钟匹配就 True
    - last_run_at 用 db.now() 同款格式（%Y-%m-%d %H:%M:%S）
    """
    cron_expr = (cron_expr or "").strip()
    if not cron_expr:
        return False
    parts = cron_expr.split()
    if len(parts) != 5:
        return False

    if now is None:
        now = datetime.datetime.now()
    # 向下取整到分钟
    now = now.replace(second=0, microsecond=0)

    minute = now.minute
    hour = now.hour
    day = now.day
    month = now.month
    # Python weekday: 0=Mon … 6=Sun；cron: 0=Sun, 1=Mon … 6=Sat
    cron_weekday = (now.weekday() + 1) % 7

    # 逐字段匹配
    if not _cron_field_match(parts[0], minute):
        return False
    if not _cron_field_match(parts[1], hour):
        return False
    if not _cron_field_match(parts[2], day):
        return False
    if not _cron_field_match(parts[3], month):
        return False
    if not _cron_field_match(parts[4], cron_weekday % 7):
        return False

    # 当前分钟匹配，再检查是否已跑过
    if not last_run_at:
        return True
    try:
        last = datetime.datetime.strptime(last_run_at, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return True
    # 当前分钟严格晚于上次运行时间
    return now > last


def lib_to_gen_opts(lib: Dict[str, Any]) -> Dict[str, Any]:
    """把库记录转成 strm-gen 用的 opts 字典。

    返回键：fmt, bdmv, keep_first, outdir, base, cd2, prefix, depth,
            exclude_words, strm_ext, threads, drive, src_cid
    """
    cd2 = lib.get("cd2_prefix", "/CloudNAS/CloudDrive")
    if not cd2.endswith("/"):
        cd2 += "/"
    return {
        "fmt": lib.get("fmt", "path"),
        "bdmv": lib.get("bdmv", "skip"),
        "keep_first": bool(lib.get("keep_first")),
        "outdir": lib.get("outdir", ""),
        "base": lib.get("base_url", ""),
        "cd2": cd2,
        "prefix": lib.get("store_prefix", "115open"),
        "depth": lib.get("depth", 5),
        "exclude_words": lib.get("exclude_words", ""),
        "strm_ext": lib.get("strm_ext", ""),
        "threads": lib.get("threads", 2),
        "drive": lib.get("drive", "115"),
        "src_cid": lib.get("src_cid", ""),
        "src_path": lib.get("src_path", ""),
        "scan_mode": lib.get("scan_mode", "local"),
        "local_dir": lib.get("local_dir", ""),
    }


# drive -> 中文名（前端展示用）
_DRIVE_LABEL = {"115": "115网盘", "123": "123网盘", "guangya": "光鸭网盘"}


def summary(lib: Dict[str, Any]) -> Dict[str, Any]:
    """给前端用的精简摘要（不含 last_run_stat 原文，只给解析后的结构）。"""
    stat = {}
    raw = str(lib.get("last_run_stat") or "").strip()
    if raw:
        try:
            stat = json.loads(raw)
            if not isinstance(stat, dict):
                stat = {}
        except Exception:
            stat = {}
    drive = str(lib.get("drive") or "115").strip().lower()
    return {
        "id": lib.get("id", ""),
        "name": lib.get("name", ""),
        "drive": drive,
        "source_label": _DRIVE_LABEL.get(drive, drive),
        "outdir": lib.get("outdir", ""),
        "fmt": lib.get("fmt", "path"),
        "bdmv": lib.get("bdmv", "skip"),
        "depth": lib.get("depth", 5),
        "scheduled": bool(lib.get("scheduled")),
        "cron": lib.get("cron", ""),
        "enabled": bool(lib.get("enabled")),
        "exclude_words": lib.get("exclude_words", ""),
        "strm_ext": lib.get("strm_ext", ""),
        "threads": lib.get("threads", 2),
        "force": bool(lib.get("force")),
        "keep_first": bool(lib.get("keep_first")),
        "last_run_at": lib.get("last_run_at", ""),
        "last_stat": stat,
    }
