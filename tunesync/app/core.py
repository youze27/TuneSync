"""基础设施：配置、SQLite 状态库、日志、进度事件总线、文本归一化。

设计约束：单容器零外部依赖，所有状态落在 data/tunesync.db（SQLite/WAL）。
配置优先级：config.yaml  <  kv 表中的运行时覆盖（UI 保存）。
"""
import asyncio
import json
import os
import random
import re
import socket
import sqlite3
import threading
import time
import unicodedata
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

import yaml

APP_DIR = os.path.dirname(os.path.abspath(__file__))       # .../tunesync/app
BASE = os.path.dirname(APP_DIR)                            # .../tunesync
DATA_DIR = os.environ.get("TUNESYNC_DATA", os.path.join(BASE, "data"))
CFG_PATH = os.environ.get("TUNESYNC_CONFIG", os.path.join(BASE, "config", "config.yaml"))
CAT_PATH = os.environ.get("TUNESYNC_CATEGORIES", os.path.join(BASE, "config", "categories.yaml"))
DB_PATH = os.path.join(DATA_DIR, "tunesync.db")

# ---------------------------------------------------------------- 配置


def load_yaml(p):
    try:
        with open(p, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def _deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


CONFIG = load_yaml(CFG_PATH)
CATEGORIES = load_yaml(CAT_PATH).get("categories", [])
CATMAP = {c["code"]: c for c in CATEGORIES if c.get("code")}


# ---------------------------------------------------------------- 宿主地址

# 配置里凡是写 127.0.0.1 的地方，指的都是「和 TuneSync 同一台机器上的服务」。
# 裸跑时那是对的主机地址；但在容器里 127.0.0.1 指向容器自己，于是所有音源、
# Navidrome、飞牛网关都会连不通——而且表现为「音源一直失败」，很难一眼看出原因。
#
# 所以这里把回环地址统一换成宿主地址，config.yaml 里就只需要写 127.0.0.1 一处，
# 不必为容器和裸机维护两份配置。
#
# 优先级：TUNESYNC_HOST 环境变量 > host.docker.internal > 自动探测的局域网 IP。
# 服务若以另一个容器运行（同一 compose 网络），用容器名当 TUNESYNC_HOST 即可。

_LOOPBACK = {"127.0.0.1", "localhost", "0.0.0.0", "::1", "[::1]", ""}
_host_alias = None
_in_container = None


def running_in_container():
    """是否跑在容器里。裸机/开发机不该改写 127.0.0.1——那里的回环地址本来就是对的，
    换成局域网 IP 反而会打断只监听 127.0.0.1 的本地音源。"""
    global _in_container
    if _in_container is None:
        if os.path.exists("/.dockerenv"):
            _in_container = True
        else:
            try:
                with open("/proc/1/cgroup", encoding="utf-8") as f:
                    c = f.read()
                _in_container = any(k in c for k in ("docker", "kubepods", "containerd", "lxc"))
            except OSError:
                _in_container = False
    return _in_container


def _detect_host():
    v = (os.environ.get("TUNESYNC_HOST") or "").strip()
    if v:
        return v
    if not running_in_container():
        return ""
    # compose 里配了 extra_hosts: host-gateway 时一定能解析，
    # 比探测局域网 IP 稳（多网卡、VPN、网桥网段都可能导致探测出错）
    try:
        socket.getaddrinfo("host.docker.internal", None)
        return "host.docker.internal"
    except OSError:
        pass
    # 退回「默认路由所在的网卡地址」。UDP connect 不发包，只是让内核选路。
    for probe in ("10.255.255.255", "1.1.1.1"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(0.2)
            s.connect((probe, 1))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        except OSError:
            continue
        finally:
            s.close()
    return ""


def host_alias():
    """本机（宿主）在网络请求里应该使用的地址；探测不到时返回空串。"""
    global _host_alias
    if _host_alias is None:
        _host_alias = _detect_host()
    return _host_alias


def localise_url(url):
    """把 URL 里的回环主机名换成宿主地址；非 URL 或本来就是远端地址则原样返回。"""
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return url
    try:
        p = urlsplit(url)
    except ValueError:
        return url
    host = p.hostname or ""
    if host.lower() not in _LOOPBACK:
        return url
    alias = host_alias()
    if not alias:
        return url
    port = f":{p.port}" if p.port else ""
    userinfo = ""
    if p.username:
        userinfo = p.username + (f":{p.password}" if p.password else "") + "@"
    return urlunsplit((p.scheme, f"{userinfo}{alias}{port}", p.path, p.query, p.fragment))


def _apply_host(node):
    """返回一棵改写过回环 URL 的新树，原地不改。

    必须不可变：merged_config() 里的 _deep_merge 只做顶层浅拷贝，
    cfg["targets"] 常常就是 CONFIG["targets"] 本身。原地改写会把模块级
    CONFIG 永久替换成宿主地址，之后再也回不到 127.0.0.1——
    对测试尤其致命：第一个开启改写的用例会污染后面所有用例。
    """
    if isinstance(node, dict):
        return {k: (localise_url(v) if isinstance(v, str) else _apply_host(v))
                for k, v in node.items()}
    if isinstance(node, list):
        return [localise_url(v) if isinstance(v, str) else _apply_host(v) for v in node]
    return node


# CONFIG 保持原样：宿主地址改写只发生在 merged_config() 返回的视图上。
# 直接改写模块级 CONFIG 会让「本机跑」和「容器里跑」互相污染——测试尤其致命。


def merged_config():
    """config.yaml 与 UI 运行时覆盖合并后的视图。"""
    cfg = _deep_merge(CONFIG, kv_get("settings") or {})
    # 推送平台有独立的写入端点，这里合并进同一视图
    t = kv_get("targets")
    if t:
        cfg["targets"] = _deep_merge(cfg.get("targets", {}), t)
    return _apply_host(cfg)


def source_cfg(raw):
    """解析音源的 cfg 并把回环地址换成宿主地址。

    音源存在 SQLite 里而不是配置树里，所以不在 merged_config() 的改写范围内。
    存的是用户原样输入的字符串，只在读取时改写——界面上看到的也是实际会用到的地址。
    """
    try:
        cfg = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return _apply_host(cfg) if isinstance(cfg, dict) else {}


def merged_targets():
    t = merged_config().get("targets", {})
    return {k: (v or {}) for k, v in t.items()}


# ---------------------------------------------------------------- 数据库

_local = threading.local()


def db():
    """线程本地连接。sqlite3 的连接不能跨线程共享，因此每个线程一个。"""
    if not hasattr(_local, "conn"):
        os.makedirs(DATA_DIR, exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=True)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        # 刮削/下载高并发写时 8s 不够，retry 兜底之外先把窗口拉大
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA foreign_keys=ON")
        _local.conn = c
    return _local.conn


def db_retry(fn, tries=6, base=0.15):
    """SQLite 锁冲突重试。

    WAL 是「多读单写」：刮削 8 个 worker 同时 write_scrape_tags 时，
    busy_timeout 也可能等不到锁直接 OperationalError('database is locked')。
    对这种错误做指数退避重试；其他错误（如约束冲突）不在此列，直接抛。
    """
    for i in range(tries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or i == tries - 1:
                raise
            time.sleep(base * (2 ** i) + random.random() * 0.05)


SCHEMA = """
CREATE TABLE IF NOT EXISTS fetch_task(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, platforms TEXT, categories TEXT,
  count_per INTEGER DEFAULT 3, cron_expr TEXT, enabled INTEGER DEFAULT 1,
  push_mode TEXT DEFAULT 'llm', source_platforms TEXT DEFAULT '["all"]',
  custom_prompt TEXT DEFAULT '', scene_mode TEXT DEFAULT 'separate',
  tracks_per INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS fetch_run(
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER, started_at TEXT,
  finished_at TEXT, status TEXT DEFAULT 'running', summary TEXT);
CREATE TABLE IF NOT EXISTS run_playlist(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, platform TEXT, category TEXT,
  name TEXT, cover_url TEXT, source_id TEXT, track_total INTEGER DEFAULT 0,
  local_hit INTEGER DEFAULT 0, downloaded INTEGER DEFAULT 0, skipped INTEGER DEFAULT 0,
  status TEXT DEFAULT 'pending', error TEXT);
CREATE TABLE IF NOT EXISTS download_job(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_playlist_id INTEGER, title TEXT, artist TEXT,
  status TEXT DEFAULT 'pending', source TEXT, file_path TEXT, error TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS local_file(
  id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT UNIQUE, title TEXT, artist TEXT,
  norm_key TEXT, mtime REAL, album TEXT, genre TEXT, style TEXT, year INTEGER,
  cover TEXT, scraped_at TEXT, lyrics TEXT);
CREATE INDEX IF NOT EXISTS idx_norm ON local_file(norm_key);
CREATE TABLE IF NOT EXISTS playlist(
  id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT, source_id TEXT, name TEXT,
  category TEXT, UNIQUE(platform, source_id));
CREATE TABLE IF NOT EXISTS source(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, priority INTEGER DEFAULT 1,
  enabled INTEGER DEFAULT 1, health TEXT DEFAULT 'healthy', fail_count INTEGER DEFAULT 0,
  ok_count INTEGER DEFAULT 0, err_count INTEGER DEFAULT 0,
  last_error TEXT, last_check TEXT, cfg TEXT);
CREATE TABLE IF NOT EXISTS app_user(
  id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, password_hash TEXT,
  created_at TEXT, last_login TEXT);
CREATE TABLE IF NOT EXISTS auth_session(
  token TEXT PRIMARY KEY, user_id INTEGER, created_at TEXT, expires_at TEXT);
CREATE INDEX IF NOT EXISTS idx_session_user ON auth_session(user_id);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""

# 老版本库可能缺列，启动时补齐（ADD COLUMN 幂等）
MIGRATIONS = [
    ("local_file", "scanned_at", "TEXT"),
    ("local_file", "size", "INTEGER"),
    ("local_file", "format", "TEXT"),
    ("local_file", "album", "TEXT"),
    ("local_file", "genre", "TEXT"),
    ("local_file", "style", "TEXT"),
    ("local_file", "year", "INTEGER"),
    ("local_file", "cover", "TEXT"),
    ("local_file", "scraped_at", "TEXT"),
    ("local_file", "lyrics", "TEXT"),
    ("fetch_run", "task_name", "TEXT"),
    ("fetch_task", "push_mode", "TEXT DEFAULT 'llm'"),
    ("fetch_task", "source_platforms", "TEXT DEFAULT '[\"all\"]'"),
    ("fetch_task", "custom_prompt", "TEXT DEFAULT ''"),
    # separate = 每个场景各推一轮；merge = 所有场景合成一个主题只推一轮
    ("fetch_task", "scene_mode", "TEXT DEFAULT 'separate'"),
    # 每张歌单的曲目数，0 表示沿用 llm.tracks_per_playlist 全局值
    ("fetch_task", "tracks_per", "INTEGER DEFAULT 0"),
    # 音源累计成功/失败次数：算历史成功率用（fail_count 是连续失败、会被清零，算不了）
    ("source", "ok_count", "INTEGER DEFAULT 0"),
    ("source", "err_count", "INTEGER DEFAULT 0"),
]

# 依赖 MIGRATIONS 新增列的索引，必须在补列之后建。
# 放在 SCHEMA 里的话，老库上 CREATE INDEX 会先于 ALTER TABLE 报
# "no such column: genre"，容器直接起不来。
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_lf_genre ON local_file(genre);
CREATE INDEX IF NOT EXISTS idx_lf_year ON local_file(year);
"""


def init_db():
    c = db()
    c.executescript(SCHEMA)
    for table, col, decl in MIGRATIONS:
        cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    c.executescript(INDEXES)
    c.commit()
    seed_sources()


def recover_stale_runs():
    """把上次进程非正常退出留下的 running 记录收敛掉。

    容器被杀、崩溃、OOM 时 run 会永远卡在 running，UI 上看着像还在跑。
    启动时进程里不可能有正在跑的 run，直接判 failed。
    """
    c = db()
    n = c.execute(
        "UPDATE fetch_run SET status='failed', finished_at=?,"
        " summary=? WHERE status='running'",
        (now(), json.dumps({"fatal": "进程异常退出，该次运行未正常结束"},
                           ensure_ascii=False))).rowcount
    c.execute("UPDATE run_playlist SET status='failed', error=? "
              "WHERE status IN ('pending','processing')", ("进程异常退出",))
    c.commit()
    if n:
        log_api.warning("发现 %s 条上次异常退出的运行记录，已标记为 failed", n)
    return n


def seed_sources():
    """首次启动把 config.yaml 的 sources 示例灌进库。

    只播种一次（用 kv 打标记），否则用户删光音源后重启容器示例又会回来。
    """
    c = db()
    if kv_get("seeded_sources"):
        return 0
    kv_set("seeded_sources", True)
    if c.execute("SELECT COUNT(*) n FROM source").fetchone()["n"]:
        return 0
    added = 0
    for s in CONFIG.get("sources") or []:
        if not s.get("name") or not (s.get("cfg") or {}).get("search"):
            continue
        c.execute("INSERT OR IGNORE INTO source(name,priority,enabled,cfg) VALUES(?,?,?,?)",
                  (s["name"], int(s.get("priority", 1)), int(s.get("enabled", True)),
                   json.dumps(s["cfg"], ensure_ascii=False)))
        added += 1
    c.commit()
    return added


def kv_get(k, default=None):
    r = db().execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    if not r:
        return default
    try:
        return json.loads(r["v"])
    except (TypeError, ValueError):
        return default


def kv_set(k, v):
    db().execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)",
                 (k, json.dumps(v, ensure_ascii=False)))
    db().commit()


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 日志

try:  # loguru 为首选；缺失时退回标准库，保证最小环境也能跑
    from loguru import logger

    LOG_DIR = os.path.join(DATA_DIR, "logs")
    os.makedirs(LOG_DIR, exist_ok=True)
    logger.remove()
    logger.add(os.path.join(LOG_DIR, "tunesync-{time:YYYY-MM-DD}.log"),
               rotation="00:00", retention="14 days", encoding="utf-8",
               enqueue=False, backtrace=False, diagnose=False,
               format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {extra[module]:<8} | {level:<7} | {message}")
    logger.configure(extra={"module": "app"})

    def _log(module):
        return logger.bind(module=module)
except ImportError:  # pragma: no cover - 仅在未安装 loguru 时触发
    import logging

    LOG_DIR = os.path.join(DATA_DIR, "logs")
    os.makedirs(LOG_DIR, exist_ok=True)
    _root = logging.getLogger("tunesync")
    _root.setLevel(logging.INFO)
    if not _root.handlers:
        _fh = logging.FileHandler(os.path.join(LOG_DIR, "tunesync.log"), encoding="utf-8")
        _fh.setFormatter(logging.Formatter("%(asctime)s | %(name)-8s | %(levelname)-7s | %(message)s"))
        _root.addHandler(_fh)

    def _log(module):
        return _root  # noqa: F811


log_pipeline = _log("pipeline")
log_fetcher = _log("fetcher")
log_resolver = _log("resolver")
log_scraper = _log("scraper")
log_writer = _log("writer")
log_api = _log("api")


# ---------------------------------------------------------------- 进度事件总线

class EventHub:
    """线程 → asyncio 的单向广播。流水线在工作线程里 emit，WebSocket 在事件循环里消费。"""

    def __init__(self, maxsize=20000):
        self._subs = set()
        self._loop = None
        self._maxsize = maxsize
        self._lock = threading.Lock()

    def bind_loop(self, loop):
        self._loop = loop

    def subscribe(self, loop=None):
        q = asyncio.Queue(maxsize=self._maxsize)
        with self._lock:
            self._subs.add(q)
        if loop and not self._loop:
            self._loop = loop
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._subs.discard(q)

    @property
    def listeners(self):
        return len(self._subs)

    def publish(self, msg):
        with self._lock:
            targets = list(self._subs)
            loop = self._loop
        if not targets:
            return 0

        def _put():
            for q in targets:
                try:
                    q.put_nowait(msg)
                except asyncio.QueueFull:  # 慢消费者：丢最旧的一条，不阻塞流水线
                    try:
                        q.get_nowait()
                        q.put_nowait(msg)
                    except (asyncio.QueueEmpty, asyncio.QueueFull):
                        pass

        if loop is None:      # 无事件循环（CLI / 测试）：同步投递，语义保持一致
            _put()
            return len(targets)
        try:
            loop.call_soon_threadsafe(_put)
            return len(targets)
        except RuntimeError:  # 循环已关闭
            return 0


hub = EventHub()


def emit(msg):
    """向所有 WebSocket 客户端广播一条进度消息。"""
    return hub.publish(msg)


# ---------------------------------------------------------------- 归一化匹配

try:  # 繁简转换可选，缺失时只做字符级归一
    import opencc

    _CC = opencc.OpenCC("t2s")
except Exception:  # pragma: no cover
    _CC = None

_BR = re.compile(r"[（(\[【{《].*?[）)\]】}》]", re.S)
_FEAT = re.compile(r"(?i)\s*(?:feat\.?|ft\.?|featuring|with)\s.*$")
_SEP = re.compile(r"[^\w一-鿿]+")
_TRAIL = re.compile(r"[\s\-_.]+$")


def norm(s):
    """标题/歌手归一化：繁简→全半角→去括注→去 feat.→折叠空白→小写。"""
    s = unicodedata.normalize("NFKC", str(s or ""))
    if _CC is not None:
        s = _CC.convert(s)
    s = _BR.sub("", s)
    s = _FEAT.sub("", s)
    s = _SEP.sub("", s)
    return _TRAIL.sub("", s).lower()


def main_artist(artist):
    """多歌手串（'A/B'、'A,B'、'A & B'）取主歌手。"""
    for sep in ("/", ",", "、", "&", " feat. ", ";"):
        if sep in str(artist or ""):
            artist = str(artist).split(sep)[0]
    return str(artist or "").strip()


def tkey(title, artist):
    return norm(title) + "|" + norm(main_artist(artist))


def jget(data, path):
    """点分 JSON 路径，支持数组下标；失败返回 None。"""
    if not path:
        return None
    try:
        for p in str(path).split("."):
            if isinstance(data, list):
                data = data[int(p)]
            elif isinstance(data, dict):
                data = data.get(p)
            else:
                return None
        return data
    except (ValueError, TypeError, IndexError):
        return None


def safe_name(s):
    return _TRAIL.sub("", re.sub(r'[\\/:*?"<>|]', "_", str(s or "")))[:80] or "unknown"
