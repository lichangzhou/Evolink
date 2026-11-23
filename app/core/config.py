"""配置读写：所有可变参数集中在 data/config.json。

写入安全（2026-10-19）：
- 原子写：先写 .tmp 再 os.replace，容器重启/被杀时不会留下半截 JSON。
  旧实现 write_text 直接覆盖，一旦被打断，文件损坏后 load() 会静默回退
  DEFAULTS —— 用户填的 Key、选库、代理全部「看似被清空」，就是这个原因。
- 滚动备份：每次保存前把旧文件轮换存 3 份（config.bak.1~3.json）。
- 损坏自愈：load() 解析失败时依次尝试备份恢复。
"""
import json
import os
import shutil
import threading
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data"
if not DATA.is_dir():
    DATA.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = DATA / "config.json"
DB_PATH = DATA / "vl.db"

MAX_EMBY_SERVERS = 3        # 设置里最多能配几台 Emby

DEFAULTS = {
    # ⚠️ 出厂默认值不写死任何「别人机器的地址」（IP / 端口 / 域名）。
    #    这产品要发布给所有人用：Emby、代理、元数据源、兜底通道各家都不一样，
    #    写死等于替用户猜。第一次用请到「设置」里填自己的。
    # 老字段：始终跟着 emby_servers 里的第一台走，保证所有取数路径不用改。
    # 出厂留空：Emby 部署在哪、端口多少、是不是有反代，各家都不一样，
    # 写死默认值等于替用户猜。第一次用请到「设置」里填自己的 Emby 地址。
    "emby_url": "",
    "emby_api_key": "",
    "emby_user_id": "",
    # 多服务器：最多 MAX_EMBY_SERVERS 条 {id,name,url,api_key,user_id}
    "emby_servers": [],
    "tmdb_api_key": "",
    "metatube_url": "",
    "port": 9191,
    "poster_cache": "proxy",
    # 代理：proxy_enabled + host + port 为准；旧字段 proxy 继续兼容
    "proxy_enabled": True,
    "proxy_host": "",
    "proxy_port": 7890,
    "proxy": "",
    "tg_channels": [],
    "re0_base": "https://re0.me",
    "javdb_base": "https://javdb.com",
    # JavBus：按片商维度回灌历史（新片仍走 JavDB）
    "javbus_base": "https://www.javbus.com",
    "javbus_year_floor": 2000,   # 回溯到哪一年为止
    "javbus_delay": 0.35,        # 每页间隔（秒），太小容易被限速
    "javbus_max_pages": 900,     # 单个片商最多翻多少页（S1 到 2007 年约 500 页）
    "avbus_min_items": 3,        # 本地该前缀至少有这么多条才去抓它的片商全史
    "av_pages": 10,
    "av_modes": ["new", "rank_monthly"],
    # 比对时是否扫描全部 Emby 库（true = 片子放在哪个库都算已有；false = 只看已映射的库）
    "match_all_libs": False,
    "fallback_channel_url": "",
    "fallback_channel_user": "",
    "fallback_channel_pass": "",
    "resources_cloud": "115",
    # ──────────────────────────────────────────────────────────────
    # RE0 直连通道（core/re0.py）：自己的 re0.me 账号 + CloakBrowser 真实登录。
    # 与上面的兜底借道并行，互不影响；直连优先看这边配没配账号。
    # re0.me 常态屏蔽大陆 IP：容器 host 网络下填 http://127.0.0.1:7890（mihomo）。
    # ──────────────────────────────────────────────────────────────
    "re0_direct_user": "",
    "re0_direct_pass": "",
    "re0_direct_proxy": "",
    # 用户在真实浏览器登录 re0.me 后，用书签小工具把 cf_clearance + 会话 cookie 交来；
    # 注入无头浏览器即可绕过 Cloudflare 对自动化的封锁（无需 xvfb / 无头改有头）。
    "re0_direct_cookies": "",
    # ──────────────────────────────────────────────────────────────
    # 反向代理换链（主流做法）：evolink 在可配端口拦播放器媒体请求，
    # 查 API 拿 item 路径 → 读 strm → 抽 115 路径 → 用客户端 UA 签 CDN 直链 → 302。
    # strm 文件不动。Emby 反代默认关（保持现状不动）；Jellyfin 默认开。
    # ──────────────────────────────────────────────────────────────
    "jellyfin_enabled": True,
    "jellyfin_port": 8099,
    "jellyfin_base": "http://127.0.0.1:8098",
    # ⚠️ 不再写死源码凭据：默认空，强制用户自填（避免反向代理用上公开默认值）
    "jellyfin_api_key": "",
    "jellyfin_user_id": "",
    "jellyfin_db_path": "/jellyfin_data/jellyfin.db",
    "jellyfin_local_prefix": "/CloudNAS/CloudDrive",
    "jellyfin_remote_prefix": "",
    # 回调地址：客户端该用哪个地址连反代。留空 = 按客户端实际访问的地址自动推断。
    # 必须填对，否则客户端会拿 Jellyfin 自报的容器内网地址（172.21.x.x）去取流 → 黑屏/播放失败
    "jellyfin_public_base": "",
    "emby_proxy_enabled": False,       # 默认关，保持现状
    "emby_proxy_port": 8093,
    "emby_proxy_base": "http://127.0.0.1:8096",
    "emby_proxy_api_key": "",
    "emby_proxy_user_id": "",
    "emby_proxy_db_path": "/emby_data/library.db",
    "emby_proxy_local_prefix": "/CloudNAS/CloudDrive",
    "emby_proxy_remote_prefix": "",
    "emby_proxy_public_base": "",
    # Plex 反代（plex-demo 同款思路）：页面/API 全透传，仅拦 /library/parts/<id>/<n>/file
    # 换链 302。strm 模式与实体模式通吃：.strm 读第一行，实体路径直接用。
    # media_parts.file 是 plex 容器视角（/strm/...），读文件前换算成本容器挂载点 /Link/。
    "plex_enabled": False,
    "plex_port": 32411,
    "plex_base": "http://127.0.0.1:32400",
    "plex_public_base": "",
    "plex_db_path": "/plex_data/Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db",
    "plex_local_prefix": "/CloudNAS/CloudDrive",
    "plex_remote_prefix": "",
    "plex_strm_prefix": "/strm/",
    "plex_link_prefix": "/Link/",
    # Plex 账号授权（pin 流程）：token 由用户在 app.plex.tv 登录批准后由 EvoLink 收回，
    # client_id 为本机固定标识，首次发起授权时自动生成并持久化。
    "plex_token": "",
    "plex_client_id": "",
    # ──────────────────────────────────────────────────────────────
    # Playable 反代（自研三位一体服务端，Jellyfin 12.x fork）：复用 jellyfin 反代通道
    # （kind=jellyfin，路径结构 + MediaBrowser 鉴权一致），仅 endpoint / 凭据不同。
    # 默认关；部署时把 ApiKey / UserId 注入 config.json 后即开。
    # ──────────────────────────────────────────────────────────────
    "playable_enabled": False,
    "playable_port": 8092,
    "playable_base": "http://127.0.0.1:8896",
    "playable_public_base": "",
    "playable_local_prefix": "/CloudNAS/CloudDrive",
    "playable_remote_prefix": "",
    "playable_api_key": "",
    "playable_user_id": "",
    "playable_db_path": "",
    # 地区配额权重（10 档，权重不等是有意为之——旧版每档同页数导致每档都是 60 条）
    "region_weights": {
        "大陆": 100, "香港": 45, "台湾": 45, "日本": 90, "韩国": 60,
        "印度": 35, "东南亚": 25, "美国": 130, "欧洲": 90, "其他": 30,
    },
    # 自动增量同步
    "auto": {"enabled": False, "hours": 12, "sync_emby": True,
             "fetch": True, "fetch_av": True, "match": True},
    "list_limit": 40,
    # 重启后自动续跑未完成的数据拉取任务（索引/拉片/榜单/AV 全史/里番/统计/预热/比对）
    "auto_resume": True,
    # 115 网盘（扫码登录后写入 cookie）
    "pan115_cookie": {"UID": "", "CID": "", "SEID": "", "KID": ""},
    "pan115_user": "",
    "pan115_root": "",          # 上次扫描的目录 id，空=根目录
    "poster_cache_dir": "",     # 空则用 data/cache/poster
    # 桶 → 库名列表：只存用户改过的桶，缺省值见 db.DEFAULT_SEL
    "emby_sel": {},
    "hentai_pages": 40,         # 拉取里番清单时翻多少页（每页 50）
    # ──────────────────────────────────────────────────────────────
    # 登录保护：页面（9191）前面加一道账号密码，默认 admin / admin。
    # 口令不存明文：auth_hash = PBKDF2-HMAC-SHA256(密码, auth_salt)。
    # auth_hash 为空 = 用户还没改过，仍按默认 admin/admin 校验。
    # auth_secret 是会话 Cookie 的 HMAC 签名密钥（首次自动生成，重启不变）。
    # auth_enabled=false 可整体关掉登录保护（忘密码时的兜底开关）。
    # ──────────────────────────────────────────────────────────────
    "auth_enabled": True,
    "auth_user": "admin",
    "auth_salt": "",
    "auth_hash": "",
    "auth_secret": "",
    # auth_trust_lan：局域网/回环/私有网段来源免登录（家庭站点默认开；公网仍要求登录）。
    # trust_proxy：仅当前面挂了受信反代时开启，才采信 X-Forwarded-For / X-Real-IP（否则可伪造头冒充局域网）。
    "auth_trust_lan": True,
    "trust_proxy": False,
    # ── Link 清理（dup 详情里标注 strm 是否还对得上 Emby 实际库 + 多选删除）──
    # link_dir：strm 目录在容器内的挂载点。各用户 strm 目录不同，compose 里按实际改左边路径。
    # link_emby_prefix：Emby library.db 里 Path 的前缀（按 Emby 容器挂载点改，默认 /Link/）。
    # emby_library_db：Emby 库 DB 只读挂载点。「对不上」以它为权威判定；不挂则拒绝标注/删除。
    # link_trash_dir：回收站目录，留空 = data/strm_trash。删除一律移回收站（可恢复），不硬删。
    "link_dir": "/app/Link",
    "link_emby_prefix": "/Link/",
    # Emby 是 WAL 模式，必须挂整个 data 目录（连 -wal/-shm 一起读），只挂 library.db 会拿到旧快照。
    # 代码兼容老布局 /emby_library.db，见 server._emby_db_path()
    "emby_library_db": "/emby_data/library.db",
    "link_trash_dir": "",
    # Link 清理删完 strm 后，是否顺手通知 Emby 把对应条目从媒体库删掉
    #（删除弹窗里有勾选框可临时改；没配 Emby 服务器/Key 时自动跳过）
    "link_delete_notify_emby": True,
    # ── strm 生成器（115 目录树直写，备用功能，不碰存量 strm）──
    # strm_gen_dir：输出目录（容器内路径，必须可写；默认 Link 下的独立子目录，与存量隔开）。
    # strm_gen_fmt：pc=pickcode URL 型（免 CD2 挂载，推荐）；path=CD2 路径型。
    # strm_gen_base：pc 型 strm 里写的地址基座（播放器要能直达 evolink），空=按 emby_url 宿主+9191 推。
    # strm_gen_cd2 / strm_gen_prefix：path 型的 CD2 挂载前缀 + 115 根路径前缀（各用户挂载不同，按实际改）。
    # strm_gen_bdmv：skip=跳过蓝光原盘（默认）；main=每个原盘取 STREAM 下最大 m2ts 生成单条目。
    "strm_gen_dir": "/app/Link/gen115",
    "strm_gen_fmt": "pc",
    "strm_gen_base": "",
    "strm_gen_cd2": "/CloudNAS/CloudDrive/",
    "strm_gen_prefix": "115open",
    "strm_gen_bdmv": "skip",
    # EVOLINK:LICENSE:CONFIG
    # 授权：免费版 / 专业版（一次性买断 68 元）。未填 key 即免费版，无本地后门。
    "license": {
        "server": "http://182.255.84.109:9090",
        "key": "",
    },
    # /EVOLINK:LICENSE:CONFIG
}


def _norm_server(s, i=0):
    """把一条服务器配置收拾干净；地址没填的直接丢掉。"""
    if not isinstance(s, dict):
        return None
    url = str(s.get("url") or "").strip().rstrip("/")
    if not url:
        return None
    if not (url.startswith("http://") or url.startswith("https://")):
        url = "http://" + url
    return {
        "id": str(s.get("id") or "s%d" % (i + 1)),
        "name": str(s.get("name") or "").strip(),
        "url": url,
        "api_key": str(s.get("api_key") or "").strip(),
        "user_id": str(s.get("user_id") or "").strip(),
    }


def emby_servers(cfg=None):
    """当前配的 Emby 服务器（最多 3 台）。

    老配置只有 emby_url / emby_api_key / emby_user_id，这里自动折成一台，
    不需要用户手动迁移。
    """
    cfg = cfg if isinstance(cfg, dict) else load()
    out = []
    raw = cfg.get("emby_servers")
    if isinstance(raw, list):
        for i, s in enumerate(raw[:MAX_EMBY_SERVERS]):
            n = _norm_server(s, i)
            if n:
                out.append(n)
    if not out and str(cfg.get("emby_url") or "").strip():
        out = [_norm_server({
            "id": "s1", "name": "",
            "url": cfg.get("emby_url"),
            "api_key": cfg.get("emby_api_key"),
            "user_id": cfg.get("emby_user_id"),
        }) or {}]
        out = [s for s in out if s]
    return out


def emby_primary(cfg=None):
    """主服务器：索引、缩略图、比对默认都走它。"""
    lst = emby_servers(cfg)
    return lst[0] if lst else {"id": "s1", "name": "", "url": "", "api_key": "", "user_id": ""}


def save_emby_servers(servers):
    """写回服务器列表，并把第一台同步到老字段（emby_url/emby_api_key/emby_user_id）。"""
    cfg = load()
    clean = []
    for i, s in enumerate(list(servers or [])[:MAX_EMBY_SERVERS]):
        n = _norm_server(s, i)
        if n:
            clean.append(n)
    cfg["emby_servers"] = clean
    if clean:
        cfg["emby_url"] = clean[0]["url"]
        cfg["emby_api_key"] = clean[0]["api_key"]
        cfg["emby_user_id"] = clean[0]["user_id"]
    save(cfg)
    return clean


def _env(name, default=""):
    return (os.environ.get(name) or default).strip()


# 「关掉」类取值：空串 / none / off / false / 0 都算关
_OFF = ("", "none", "off", "false", "0", "no")


def _seed_from_env():
    """首次启动（还没有 config.json）时，用环境变量写一份初始配置。

    只在文件不存在时执行一次；之后一切都以界面里的设置为准，
    免得环境变量永久压制用户在设置页改过的值。

    这么做的原因：DEFAULTS 里带的是开发机内网地址（Emby / 代理 / 兜底通道），
    换到别人的 NAS 上必须清空，否则所有外部请求都会打到不存在的地址上。
    """
    if CONFIG_PATH.exists():
        return
    import copy
    cfg = copy.deepcopy(DEFAULTS)

    # 这些默认值只对我自己的局域网成立，换机器必须先清掉
    cfg.update({
        "emby_url": "", "emby_api_key": "", "emby_user_id": "", "emby_servers": [],
        "metatube_url": "", "fallback_channel_url": "",
        "fallback_channel_user": "", "fallback_channel_pass": "",
        "proxy_enabled": False, "proxy_host": "", "proxy_port": "", "proxy": "",
        "poster_cache_dir": "",
    })

    emby_url = _env("EMBY_URL")
    emby_key = _env("EMBY_API_KEY")
    if emby_url:
        cfg["emby_url"], cfg["emby_api_key"] = emby_url, emby_key
        cfg["emby_servers"] = [{
            "id": "s1", "name": _env("EMBY_NAME") or "Emby",
            "url": emby_url, "api_key": emby_key, "user_id": "",
        }]
    if _env("TMDB_API_KEY"):
        cfg["tmdb_api_key"] = _env("TMDB_API_KEY")
    if _env("FALLBACK_CHANNEL_URL"):
        cfg["fallback_channel_url"] = _env("FALLBACK_CHANNEL_URL")
        cfg["fallback_channel_user"] = _env("FALLBACK_CHANNEL_USER")
        cfg["fallback_channel_pass"] = _env("FALLBACK_CHANNEL_PASS")

    # 代理：给了地址才开；明确写 none/off 或不给 = 不走代理
    proxy = _env("PROXY_URL")
    if proxy.lower() not in _OFF:
        if not proxy.startswith(("http://", "https://")):
            proxy = "http://" + proxy
        cfg["proxy_enabled"], cfg["proxy"] = True, proxy
        host, _, port = proxy.split("://", 1)[1].partition(":")
        cfg["proxy_host"], cfg["proxy_port"] = host, port or "7890"

    try:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
        print(f"[config] 首次启动，已按环境变量生成 {CONFIG_PATH}", flush=True)
    except Exception as e:
        print(f"[config] 写初始配置失败：{e}", flush=True)


_LOCK = threading.Lock()
# 版本化缓存：文件 mtime+size 未变则直接返回，避免每个 HTTP 请求都读盘+解析 JSON
_CFG_CACHE = {"data": None, "mtime": 0.0, "size": -1}


def _load_json(path):
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except Exception:
        return None


def load():
    # 版本化缓存：文件 mtime+size 未变则直接返回缓存，避免每个 HTTP 请求都读盘+解析 JSON
    global _CFG_CACHE
    path = CONFIG_PATH
    if not path.exists():
        _seed_from_env()
    mtime = size = 0
    if path.exists():
        try:
            st = path.stat()
            mtime, size = st.st_mtime, st.st_size
        except OSError:
            pass
    if _CFG_CACHE["data"] is not None and _CFG_CACHE["mtime"] == mtime and _CFG_CACHE["size"] == size:
        return dict(_CFG_CACHE["data"])
    cfg = dict(DEFAULTS)
    data = _load_json(path) if path.exists() else None
    if data is None and path.exists():
        # 主文件损坏（半截 JSON 等）：依次尝试备份自愈
        for i in (1, 2, 3):
            bak = DATA / f"config.bak.{i}.json"
            data = _load_json(bak) if bak.exists() else None
            if data is not None:
                try:
                    shutil.copy2(bak, path)
                except OSError:
                    pass
                break
    if data:
        cfg.update(data)
    for k in ("emby_api_key", "emby_user_id"):
        if not cfg.get(k):
            cfg[k] = os.environ.get(k.upper(), "")
    _CFG_CACHE = {"data": cfg, "mtime": mtime, "size": size}
    return dict(cfg)


def save(cfg):
    with _LOCK:
        try:
            if CONFIG_PATH.exists() and CONFIG_PATH.stat().st_size > 0 \
                    and _load_json(CONFIG_PATH) is not None:
                for i in (2, 1):                # bak2→bak3, bak1→bak2
                    a = DATA / f"config.bak.{i}.json"
                    b = DATA / f"config.bak.{i + 1}.json"
                    if a.exists():
                        a.replace(b)
                shutil.copy2(CONFIG_PATH, DATA / "config.bak.1.json")
        except OSError:
            pass
        tmp = CONFIG_PATH.with_name("config.json.tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp, CONFIG_PATH)
        _CFG_CACHE["data"] = None  # 配置已变，下次 load 重新读盘


def update(**kw):
    cfg = load()
    cfg.update({k: v for k, v in kw.items() if v is not None})
    save(cfg)
    return cfg


def proxy_url(cfg=None):
    """统一的代理地址。返回 "" 表示不走代理。

    优先级：proxy_enabled 关闭 → 不用；proxy_host/port → 拼 http://host:port；
    否则回落到旧字段 proxy（老配置无需迁移）。
    """
    cfg = cfg if isinstance(cfg, dict) else load()
    if cfg.get("proxy_enabled") is False:
        return ""
    host = str(cfg.get("proxy_host") or "").strip()
    if host:
        port = str(cfg.get("proxy_port") or "").strip()
        if host.startswith("http://") or host.startswith("https://"):
            return host if port in host else f"{host.rstrip('/')}:{port or '7890'}"
        return f"http://{host}:{port or '7890'}"
    return str(cfg.get("proxy") or "").strip()


def cache_dir():
    """海报磁盘缓存目录。"""
    import os
    d = (os.environ.get("POSTER_CACHE_DIR") or load().get("poster_cache_dir") or "").strip()
    p = Path(d) if d else (DATA / "cache" / "poster")
    p.mkdir(parents=True, exist_ok=True)
    return p
