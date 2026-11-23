"""Emby webhook 删除联动模块。

接收 Emby 的 webhook 事件，当媒体被删除时，联动清理 EvoLink 生成的
strm 目录结构：
- 删电影 → 删掉该电影整个目录
- 删整部剧（Emby 逐集删除）→ 删掉剧集目录
- 删单集 → 只删该集的 .strm，不删目录

本模块只依赖标准库，不 import server.py（避免循环依赖）。
"""

import os
import shutil
import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("evolink.strm_webhook")

# 视为「删除」事件的事件名（统一小写、去掉分隔符后比较）
_REMOVAL_EVENT_KEYS = {
    "libraryitemremoved",
    "itemremoved",
    "removed",
    # Emby 4.10 的通知事件 ID（GET /Notifications/Types 里叫「媒体已移除」）
    "librarydeleted",
    "collectionitemsremoved",
    "deepdelete",
    "itemdeleted",
    "itemremovedfromlibrary",
}

# 视为「删除」事件的原始事件名（大小写不敏感）
_REMOVAL_EVENT_RAW = {
    "library.item.removed",
    "itemremoved",
    "library.item.removed",
    "item.removed",
    "removed",
    # Emby 4.10 实际发出的 NotificationType
    "library.deleted",
    "collection.items.removed",
    "deep.delete",
    "item.deleted",
}


def _norm_event_key(event: str) -> str:
    """把事件名归一化：小写、去掉点/下划线/空格，便于容错比较。"""
    if not event:
        return ""
    s = str(event).strip().lower()
    for ch in (".", "_", " ", "-", "/"):
        s = s.replace(ch, "")
    return s


def _norm_path(p: str) -> str:
    """对路径做 normpath 归一化，兼容 Windows / Linux 风格。"""
    if not p:
        return ""
    return os.path.normpath(str(p))


def _is_under(base: str, target: str) -> bool:
    """判断 target 是否位于 base 之下（含 base 本身）。

    两者都先做 normpath 归一化。
    """
    b = _norm_path(base)
    t = _norm_path(target)
    if not b or not t:
        return False
    if t == b:
        return True
    # 用 base + 分隔符 做前缀判断，避免 /app/Link/Movie 误匹配 /app/Link/MovieX
    sep = os.sep
    if not b.endswith(sep):
        b_prefix = b + sep
    else:
        b_prefix = b
    return t.startswith(b_prefix)


def _has_strm_in(dir_path: str) -> bool:
    """默认 exists_checker：判断目录里是否还有 .strm 文件。"""
    try:
        for name in os.listdir(dir_path):
            if name.lower().endswith(".strm"):
                return True
    except OSError:
        return False
    return False


def parse_emby_event(payload: Any) -> Dict[str, str]:
    """从 Emby webhook 的 JSON 载荷里提取关键信息。

    参数:
        payload: Emby webhook 的 JSON 字典（可能为空 / 非 dict）。

    返回:
        形如 {"event", "type", "item_id", "name", "path",
              "series_name", "season", "episode"} 的字典，
        字段缺失时为空字符串，不抛异常。
    """
    result: Dict[str, str] = {
        "event": "",
        "type": "",
        "item_id": "",
        "name": "",
        "path": "",
        "series_name": "",
        "season": "",
        "episode": "",
    }

    if not isinstance(payload, dict):
        return result

    # 事件名：NotificationType / Event / Type
    event = ""
    for key in ("NotificationType", "Event", "Type"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            event = v.strip()
            break
    result["event"] = event

    # 条目：Item / ItemId / Id
    item: Any = None
    if isinstance(payload.get("Item"), dict):
        item = payload["Item"]
    elif isinstance(payload.get("ItemId"), (str, int)):
        # 只有 Id，没有完整 Item
        result["item_id"] = str(payload["ItemId"])
    elif isinstance(payload.get("Id"), (str, int)):
        result["item_id"] = str(payload["Id"])

    if isinstance(item, dict):
        # 类型
        t = item.get("Type")
        if isinstance(t, str) and t.strip():
            result["type"] = t.strip()

        # id
        iid = item.get("Id")
        if isinstance(iid, (str, int)):
            result["item_id"] = str(iid)

        # 名称
        name = item.get("Name")
        if isinstance(name, str) and name.strip():
            result["name"] = name.strip()

        # 路径
        path = item.get("Path")
        if isinstance(path, str) and path.strip():
            result["path"] = path.strip()

        # Episode 额外字段
        series_name = item.get("SeriesName")
        if isinstance(series_name, str) and series_name.strip():
            result["series_name"] = series_name.strip()

        season = item.get("ParentIndexNumber")
        if season is not None:
            result["season"] = str(season)

        episode = item.get("IndexNumber")
        if episode is not None:
            result["episode"] = str(episode)

    return result


def is_removal_event(ev: Dict[str, str]) -> bool:
    """判断是否是需要处理的「删除」事件。

    参数:
        ev: parse_emby_event 返回的字典。

    返回:
        仅当事件名属于 library.item.removed / itemremoved 这类时返回 True。
    """
    if not isinstance(ev, dict):
        return False
    event = ev.get("event", "")
    if not event:
        return False
    raw = str(event).strip().lower()
    if raw in _REMOVAL_EVENT_RAW:
        return True
    return _norm_event_key(event) in _REMOVAL_EVENT_KEYS


def find_owner_lib(path: str, libs: List[Dict[str, Any]],
                  managed_dirs: Optional[List[str]] = None) -> Optional[Dict[str, str]]:
    """给定一个 strm 文件的磁盘路径，找出它属于哪个媒体库。

    判据：
      1. 媒体库有 outdir，且 path 位于该 outdir 之下（含相等）→ 属于该库；
      2. 没有匹配到媒体库时，退到「受管目录」列表 —— 只要 path 位于某个
         受管目录之下就算命中（用户没建媒体库时也能用删除联动）。

    多个候选都匹配时，选 outdir 最长（最具体）的那个。

    参数:
        path: strm 文件的磁盘路径。
        libs: 媒体库字典列表（strm_lib 表记录）。
        managed_dirs: 受管目录列表（字符串），可选。

    返回:
        {"lib_id", "lib_name", "outdir", "rel"}，rel 是相对 outdir 的相对路径；
        不匹配任何库返回 None。
    """
    if not path or not isinstance(libs, list):
        libs = libs if isinstance(libs, list) else []

    norm_path = _norm_path(path)
    if not norm_path:
        return None

    best: Optional[Dict[str, str]] = None
    best_len = -1

    for lib in libs:
        if not isinstance(lib, dict):
            continue
        outdir = lib.get("outdir")
        if not isinstance(outdir, str) or not outdir.strip():
            continue
        norm_outdir = _norm_path(outdir)
        if not norm_outdir:
            continue
        if not _is_under(norm_outdir, norm_path):
            continue
        # 选 outdir 最长（最具体）的
        if len(norm_outdir) > best_len:
            best_len = len(norm_outdir)
            rel = os.path.relpath(norm_path, norm_outdir)
            best = {
                "lib_id": str(lib.get("id", "")),
                "lib_name": str(lib.get("name", "")),
                "outdir": norm_outdir,
                "rel": rel,
            }

    # 没匹配到媒体库 → 退到受管目录
    if best is None and isinstance(managed_dirs, list):
        for d in managed_dirs:
            if not isinstance(d, str) or not d.strip():
                continue
            norm_dir = _norm_path(d)
            if not norm_dir or not _is_under(norm_dir, norm_path):
                continue
            if len(norm_dir) > best_len:
                best_len = len(norm_dir)
                best = {
                    "lib_id": "",
                    "lib_name": os.path.basename(norm_dir.rstrip(os.sep)) or norm_dir,
                    "outdir": norm_dir,
                    "rel": os.path.relpath(norm_path, norm_dir),
                }

    return best


def plan_delete(
    item_path: str,
    item_type: str,
    lib: Dict[str, Any],
    exists_checker: Optional[Callable[[str], bool]] = None,
) -> Dict[str, Any]:
    """核心删除决策。

    参数:
        item_path: strm 文件的磁盘路径。
        item_type: 条目类型（Movie / Episode / Series / Audio / 其他）。
        lib: 所属媒体库字典（含 outdir）。
        exists_checker: 回调 exists_checker(dir_path) -> bool，
            返回该目录里是否还有 .strm 文件；默认用 os.listdir 检查。

    返回:
        {"action": "delete_file"|"delete_dir"|"delete_season_dir"|"skip",
         "targets": [绝对路径, ...],
         "reason": str}
    """
    # 注意：这里不要把 exists_checker 填成默认值 _has_strm_in。
    # _has_strm_in 会把「当前这个 .strm」也算进去，恒返回 True，
    # 导致「删最后一集」永远判成还有别的集、永远不升级成删 Season 目录。
    # 默认路径要走下面「排除当前文件」的计数逻辑；只有调用方显式传了
    # exists_checker 才完全采信它的结论。
    custom_checker = exists_checker

    norm_path = _norm_path(item_path)
    outdir = _norm_path(str(lib.get("outdir", ""))) if lib else ""

    def _safe(target: str) -> bool:
        """安全阀：target 必须在 outdir 之下，且不允许出现 .. 段。"""
        if not target or not outdir:
            return False
        t = _norm_path(target)
        if ".." in t.split(os.sep):
            return False
        return _is_under(outdir, t)

    itype = (item_type or "").strip().lower()

    if itype == "series":
        # 删整部剧：Series 的 Item.Path 本身就是剧集目录，
        # 但兼容误传 .strm 文件路径的情况（此时取其所在目录）。
        if norm_path.lower().endswith(".strm"):
            target_dir = os.path.dirname(norm_path)
        else:
            target_dir = norm_path
        if not _safe(target_dir):
            return {"action": "skip", "targets": [], "reason": "路径越界，已拦截"}
        return {
            "action": "delete_dir",
            "targets": [target_dir],
            "reason": "删除整部剧目录",
        }

    if itype == "season":
        # 删一季：Season 的 Item.Path 是季目录，兼容误传 .strm 的情况。
        if norm_path.lower().endswith(".strm"):
            target_dir = os.path.dirname(norm_path)
        else:
            target_dir = norm_path
        if not _safe(target_dir):
            return {"action": "skip", "targets": [], "reason": "路径越界，已拦截"}
        return {
            "action": "delete_dir",
            "targets": [target_dir],
            "reason": "删除季目录",
        }

    if itype == "movie":
        # 删该 .strm 文件所在目录
        target_dir = os.path.dirname(norm_path)
        if not _safe(target_dir):
            return {"action": "skip", "targets": [], "reason": "路径越界，已拦截"}
        return {
            "action": "delete_dir",
            "targets": [target_dir],
            "reason": "删除电影目录",
        }

    if itype == "episode":
        # 目标 .strm 文件所在目录就是 Season 目录
        season_dir = os.path.dirname(norm_path)
        if not _safe(season_dir):
            return {"action": "skip", "targets": [], "reason": "路径越界，已拦截"}

        # 判定要在删除该集 strm 之前做：
        # 「除了当前这个 .strm 之外，Season 目录里还有没有别的 .strm」
        #   → 没有别的 → 该集是最后一集 → 升级为删整个 Season 目录
        #   → 还有别的 → 只删当前这一个 .strm
        #
        # exists_checker 语义：exists_checker(dir_path) -> bool
        #   返回 True  = 目录里除当前文件外还有别的 .strm
        #   返回 False = 没有别的 .strm（最后一集）
        # 默认实现 _has_strm_in 会把当前文件也算进去，
        # 所以默认路径下用「排除当前文件」的方式自己数一遍；
        # 传入了自定义 exists_checker 则完全采信它的结论（便于打桩测试）。
        if custom_checker is not None:
            has_other_strm = bool(custom_checker(season_dir))
        else:
            try:
                entries = os.listdir(season_dir)
                current_name = os.path.basename(norm_path)
                has_other_strm = False
                for name in entries:
                    if name == current_name:
                        continue
                    if name.lower().endswith(".strm"):
                        has_other_strm = True
                        break
            except OSError:
                has_other_strm = False

        if not has_other_strm:
            # 目录里除了当前 .strm 之外没有别的 .strm → 删 Season 目录
            return {
                "action": "delete_season_dir",
                "targets": [season_dir],
                "reason": "Season 目录下已无其他 .strm，删除整个 Season 目录",
            }
        else:
            # 只删该 .strm 文件
            return {
                "action": "delete_file",
                "targets": [norm_path],
                "reason": "删除单集 .strm 文件",
            }

    # Series / Audio / 其他类型
    return {
        "action": "skip",
        "targets": [],
        "reason": "类型 %s 不需要联动删除" % (item_type or "未知"),
    }


def handle_event(
    payload: Any,
    libs: List[Dict[str, Any]],
    dry_run: bool = False,
    exists_checker: Optional[Callable[[str], bool]] = None,
    remover: Optional[Callable[[str, bool], None]] = None,
    managed_dirs: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """端到端处理一个 webhook 事件。

    参数:
        payload: Emby webhook 的 JSON 字典。
        libs: 媒体库字典列表。
        dry_run: 若为 True，不真删，只返回计划。
        exists_checker: 回调 exists_checker(dir_path) -> bool。
        remover: 回调 remover(path, is_dir) -> None，
            默认用 os.remove / shutil.rmtree。
        managed_dirs: 受管目录列表（可选）。没有匹配到媒体库时，
            只要路径在这些目录下也按受管处理。

    返回:
        {"ok": bool, "action": str, "targets": [...], "reason": str,
         "deleted": [...], "errors": [...], "event": str, "type": str, "name": str}
    """
    base_result: Dict[str, Any] = {
        "ok": False,
        "action": "",
        "targets": [],
        "reason": "",
        "deleted": [],
        "errors": [],
        "event": "",
        "type": "",
        "name": "",
    }

    # 1. 解析事件
    ev = parse_emby_event(payload)
    base_result["event"] = ev.get("event", "")
    base_result["type"] = ev.get("type", "")
    base_result["name"] = ev.get("name", "")

    if not is_removal_event(ev):
        base_result["ok"] = True
        base_result["action"] = "ignore"
        base_result["reason"] = "非删除事件"
        return base_result

    # 2. 检查 path
    if not ev.get("path"):
        base_result["ok"] = False
        base_result["action"] = "skip"
        base_result["reason"] = "载荷里没有 Item.Path"
        return base_result

    # 3. 找所属媒体库
    owner = find_owner_lib(ev["path"], libs, managed_dirs)
    if owner is None:
        base_result["ok"] = False
        base_result["action"] = "skip"
        base_result["reason"] = ("该路径不属于任何已配置的媒体库或受管目录"
                                 "（请在「删除联动 / webhook」里填写受管目录）")
        return base_result

    # 4. 制定删除计划
    plan = plan_delete(ev["path"], ev["type"], owner, exists_checker)
    base_result["action"] = plan.get("action", "skip")
    base_result["targets"] = plan.get("targets", [])
    base_result["reason"] = plan.get("reason", "")

    # 5. skip
    if plan.get("action") == "skip":
        base_result["ok"] = True
        return base_result

    # 6. dry_run
    if dry_run:
        base_result["ok"] = True
        base_result["deleted"] = []
        return base_result

    # 7. 真删
    if remover is None:
        def remover(path: str, is_dir: bool) -> None:
            if is_dir:
                shutil.rmtree(path)
            else:
                os.remove(path)

    action = plan.get("action", "")
    targets: List[str] = plan.get("targets", [])

    for target in targets:
        # 8. 删除前确认目标确实存在
        if not os.path.exists(target):
            base_result["reason"] = "目标不存在，跳过: %s" % target
            continue

        is_dir = action in ("delete_dir", "delete_season_dir")
        try:
            remover(target, is_dir)
            base_result["deleted"].append(target)
        except Exception as e:
            base_result["errors"].append("%s: %s" % (target, str(e)))
            logger.warning("删除失败 %s: %s", target, e)

    base_result["ok"] = len(base_result["errors"]) == 0
    return base_result