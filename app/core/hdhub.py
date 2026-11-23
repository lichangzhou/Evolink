"""兜底资源检索通道：借用已授权的聚合检索服务搜索，返回 re0.me 资源直链。

背景（2026-10-18 实测）：
- 对方服务的「发现 → 搜索资源」调 POST /api/v1/discover/search_resources
- 返回的资源列表里每条带 re0.me/resource/115/{slug} 直链
- 登录：POST /api/v1/login/access-token（OAuth2 表单），JWT 有效期约 30 天

好处：不需要自己过 RE0 的 Cloudflare，也不需要单独申请 RE0 授权——
对方的聚合服务里已经把上游资源站的 OpenAPI 账号授权好了，我们在内网复用。
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

_TOKEN_CACHE = {"token": "", "exp": 0}

# 网盘类型：对方服务返回的 pan_type / resource_url 里的段名（统一小写）
PAN_CN = {
    "115": "115", "alipan": "阿里云盘", "quark": "夸克", "baidu": "百度网盘",
    "guangya": "光鸭", "uc": "UC", "mobile": "移动云盘", "123": "123 云盘",
    "pikpak": "PikPak", "xunlei": "迅雷", "tianyi": "天翼云盘",
    "magnet": "磁力", "ed2k": "电驴",
}
PAN_ORDER = ["115", "alipan", "quark", "baidu", "guangya", "123", "mobile", "tianyi",
             "uc", "xunlei", "pikpak", "magnet", "ed2k"]


def _pan_from_url(url):
    m = re.search(r"/resource/([a-z0-9]+)/", url or "", re.I)
    return m.group(1).lower() if m else ""


def _pan_rank(pan):
    return PAN_ORDER.index(pan) if pan in PAN_ORDER else 99


class HubError(RuntimeError):
    pass


class Hub:
    def __init__(self, cfg=None):
        cfg = cfg or config.load()
        # 配置键：fallback_channel_*（兜底检索通道的地址 / 账号 / 口令）
        self.base = (cfg.get("fallback_channel_url") or "").rstrip("/")
        self.user = cfg.get("fallback_channel_user") or ""
        self.pwd = cfg.get("fallback_channel_pass") or ""
        self.cloud = cfg.get("resources_cloud") or "115"
        if not self.base:
            raise HubError("未配置 兜底通道服务 地址")

    # ---------- 鉴权 ----------
    def _post(self, path, body=None, form=False, token=None, timeout=75):
        # 兜底通道查询冷启动要 20-40 秒（先解析 TMDB 再打远端接口），30 秒会误判超时
        url = self.base + path
        if form:
            data = urllib.parse.urlencode(body or {}).encode()
            ctype = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body or {}, ensure_ascii=False).encode()
            ctype = "application/json"
        headers = {"Content-Type": ctype, "Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read()[:200].decode("utf-8", "ignore")
            raise HubError(f"{path} HTTP {e.code}: {detail}")
        except Exception as e:
            raise HubError(f"{path} 请求失败：{e}")

    def login(self, force=False):
        if not force and _TOKEN_CACHE["token"] and _TOKEN_CACHE["exp"] > time.time() + 60:
            return _TOKEN_CACHE["token"]
        if not self.user or not self.pwd:
            raise HubError("未配置 兜底通道服务 账号密码")
        d = self._post("/api/v1/login/access-token",
                       {"username": self.user, "password": self.pwd}, form=True)
        tok = d.get("access_token") or ""
        if not tok:
            raise HubError("登录失败：未返回 access_token")
        exp = _jwt_exp(tok)
        _TOKEN_CACHE.update(token=tok, exp=exp or (time.time() + 1800))
        return tok

    @staticmethod
    def _pick_resource_section(d):
        """从远端响应里挑出「资源列表」所在的那一段。

        返回体形如 {<某段>: {items:[...], configured, message}, telegram: {...}}。
        这里不写死段名：取第一个 items 里带网盘字段（pan_type / resource_url）的那段；
        都没有就退而取第一个带 items 列表的段。
        """
        fallback = {}
        for v in (d or {}).values():
            if not isinstance(v, dict):
                continue
            its = v.get("items")
            if not isinstance(its, list):
                continue
            for it in its:
                if isinstance(it, dict) and ("pan_type" in it or "resource_url" in it):
                    return v
            if not fallback:
                fallback = v
        return fallback

    # ---------- 资源搜索 ----------
    def _search_once(self, body):
        tok = self.login()
        try:
            d = self._post("/api/v1/discover/search_resources", body, token=tok)
        except HubError as e:
            if "HTTP 401" not in str(e):
                raise
            tok = self.login(force=True)
            d = self._post("/api/v1/discover/search_resources", body, token=tok)

        h = self._pick_resource_section(d)
        items = []
        for it in (h.get("items") or []):
            url = it.get("resource_url") or ""
            pan = (it.get("pan_type") or _pan_from_url(url) or "").lower()
            items.append({
                "title": it.get("title"),
                "slug": it.get("slug") or "",
                "pan": pan,
                "size": it.get("share_size"),
                "resolution": " / ".join(it.get("video_resolution") or []),
                "source": " / ".join(it.get("source") or []),
                "sub_lang": " / ".join(it.get("subtitle_language") or []),
                "sub_type": " / ".join(it.get("subtitle_type") or []),
                "remark": it.get("remark") or "",
                "points": it.get("unlock_points") or 0,
                "unlock_count": it.get("unlock_count") or 0,
                "unlocked": bool(it.get("is_unlocked")),
                "valid": it.get("validate_status") == "valid",
                "valid_msg": it.get("validate_message") or "",
                "checked_at": it.get("last_validated_at") or "",
                "sharer": (it.get("sharer") or "").strip(),
                "created_at": it.get("created_at") or "",
                "url": url,
                "official": bool(it.get("is_official")),
            })
        return {
            "configured": bool(h.get("configured")),
            "message": h.get("message") or "",
            "items": items,
            "telegram": len((d.get("telegram") or {}).get("items") or []),
        }

    def search_resources(self, title, media_type="movie", tmdb_id=None, keyword=None):
        """返回统一结构：{items:[...], telegram, message}。

        远端的 tmdb_id 过滤很严（对不上就只剩 0-1 条），所以：
        先用 tmdb_id 精确搜，结果太少再用纯标题补一轮，合并去重。
        """
        base = {
            "title": keyword or title or "",
            "media_type": media_type or "movie",
            "cloud_type": self.cloud,
            "tmdb_id": int(tmdb_id) if str(tmdb_id or "").isdigit() else None,
        }
        res = self._search_once(base)
        if base["tmdb_id"] and len(res["items"]) < 3:
            try:
                res2 = self._search_once(dict(base, tmdb_id=None))
            except Exception:
                res2 = None
            if res2 and res2["items"]:
                seen = {i["url"] for i in res["items"]}
                for it in res2["items"]:
                    if it["url"] not in seen:
                        res["items"].append(it)
                        seen.add(it["url"])
                res["telegram"] = max(res["telegram"], res2["telegram"])

        res["items"].sort(key=lambda x: (not x["valid"], x["points"], _pan_rank(x["pan"]), -_size_gb(x["size"])))
        pans = {}
        for it in res["items"]:
            p = it["pan"] or "other"
            pans[p] = pans.get(p, 0) + 1
        res["pan_counts"] = pans
        res["pan_labels"] = {k: PAN_CN.get(k, k) for k in pans}
        return res


def _size_gb(s):
    try:
        s = str(s).upper().replace(" ", "")
        if s.endswith("TB"):
            return float(s[:-2]) * 1024
        if s.endswith("GB"):
            return float(s[:-2])
        if s.endswith("MB"):
            return float(s[:-2]) / 1024
    except Exception:
        pass
    return 0.0


def _jwt_exp(tok):
    try:
        import base64
        payload = tok.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("exp") or 0
    except Exception:
        return 0
