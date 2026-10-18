"""歌曲解析与下载。

两段职责：
1. 本地库索引与匹配 —— 命中即复用，不做任何网络下载；
2. 音源替补下载 —— 按 priority 升序遍历 health != down 的音源，首个成功即止。
"""
import difflib
import json
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from mutagen import File as MFile

from . import lxbridge
from .core import (DATA_DIR, db, db_retry, emit, jget, log_resolver, main_artist,
                   merged_config, norm, now, source_cfg, tkey)

AUDIO_EXT = {".mp3", ".flac", ".m4a", ".mp4", ".ogg", ".oga", ".wav", ".ape", ".opus", ".wma", ".dsf"}
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) TuneSync/1.0"}
TIMEOUT = 30          # 元数据请求
DL_TIMEOUT = 120      # 下载流
MIN_BYTES = 300_000
FUZZY_THRESHOLD = 0.9
SEARCH_MATCH_MIN = 0.6

MAGIC = {
    ".mp3": lambda h: h[:3] == b"ID3" or (len(h) >= 2 and h[0] == 0xFF and (h[1] & 0xE0) == 0xE0),
    ".flac": lambda h: h[:4] == b"fLaC",
    ".m4a": lambda h: b"ftyp" in h,
    ".mp4": lambda h: b"ftyp" in h,
    ".ogg": lambda h: h[:4] == b"OggS",
    ".oga": lambda h: h[:4] == b"OggS",
    ".opus": lambda h: h[:4] == b"OggS",
    ".wav": lambda h: h[:4] == b"RIFF" and h[8:12] == b"WAVE",
    ".dsf": lambda h: h[:4] == b"DSD ",
    ".ape": lambda h: h[:4] == b"MAC ",
    ".wma": lambda h: h[:4] == b"\x30\x26\xb2\x75",
}

_state_lock = threading.Lock()
_idx = {}          # norm_key -> path
_titles = {}       # 归一化标题 -> [path, ...]
_sem = None
_sem_lock = threading.Lock()


def _cfg(section, key, default=None):
    return merged_config().get(section, {}).get(key, default)


def _num(section, key, default, cast=int):
    """取数值配置。注意不能写成 `value or default`——那会把显式的 0 当成未配置。"""
    v = _cfg(section, key)
    if v is None:
        return cast(default)
    try:
        return cast(v)
    except (TypeError, ValueError):
        return cast(default)


# ---------------------------------------------------------------- 本地库索引

def library_root():
    return _cfg("library", "root", "/music")


def download_dir():
    return _cfg("library", "download_dir") or os.path.join(library_root(), "Downloaded")


def tmp_dir():
    d = os.path.join(DATA_DIR, "tmp")
    os.makedirs(d, exist_ok=True)
    return d


def _semaphore():
    global _sem
    with _sem_lock:
        if _sem is None:
            _sem = threading.BoundedSemaphore(max(1, _num("download", "concurrency", 3)))
        return _sem


def _parse_filename(path):
    """无标签文件的兜底解析：'歌手 - 歌名' → (歌手, 歌名)。"""
    base = os.path.splitext(os.path.basename(path))[0]
    parts = re.split(r"\s+[-–—_]\s+", base, 1)
    return (parts[0], parts[1]) if len(parts) == 2 else ("", base)


def _read_tags(path):
    """一次性读出 标题/歌手/专辑/流派/风格/年份/封面。

    无标签文件用文件名兜底解析 歌手 - 歌名。
    """
    out = {"title": "", "artist": "", "album": "", "genre": "", "style": "",
           "year": 0, "cover": ""}
    try:
        m = MFile(path, easy=True)
        if m and m.tags:
            def one(*keys):
                for k in keys:
                    v = m.get(k)
                    if v:
                        return str(v[0] if isinstance(v, (list, tuple)) else v).strip()
                return ""
            out["title"] = one("title")
            out["artist"] = one("artist")
            out["album"] = one("album")
            out["genre"] = one("genre")
            out["style"] = one("style")
            y = re.search(r"(1[89]\d{2}|20\d{2})", one("date", "year", "originaldate"))
            out["year"] = int(y.group(1)) if y else 0
            if "covr" in m or "metadata_block_picture" in m or "APIC:" in m:
                out["cover"] = "1"
    except Exception:
        pass
    if not out["title"]:
        out["artist"], out["title"] = _parse_filename(path)
    return out

def scan_library(full=False):
    """扫描音乐库建索引。full=True 时无视 mtime 全量重建。"""
    root = library_root()
    if not os.path.isdir(root):
        log_resolver.warning("音乐库目录不存在：%s", root)
        return {"total": 0, "new": 0, "error": f"音乐库目录不存在：{root}"}

    found = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in AUDIO_EXT:
                p = os.path.join(dirpath, fn)
                try:
                    found[p] = os.stat(p).st_mtime
                except OSError:
                    continue

    conn = db()
    known = {r["path"]: r["mtime"] for r in conn.execute("SELECT path, mtime FROM local_file")}
    # 已有刮削数据：增量扫描时 mtime 没变就跳过，不用重读标签
    stamp = now()
    changed = 0
    for path, mtime in found.items():
        if not full and known.get(path) == mtime:
            continue
        tags = _read_tags(path)
        title, artist = tags["title"], tags["artist"]
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        # ON CONFLICT DO UPDATE 而不是 INSERT OR REPLACE：REPLACE 会把整行
        # 删掉重插，列清单里没有的 scraped_at/lyrics 被重置成 NULL——而刮削
        # 恰恰会改文件 mtime，下一轮增量扫描必然重扫，已刮标记和歌词标记
        # 就这样被冲掉，统计数字跟着漂移
        conn.execute(
            "INSERT INTO local_file"
            "(path,title,artist,norm_key,mtime,scanned_at,size,format,"
            "album,genre,style,year,cover) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(path) DO UPDATE SET"
            " title=excluded.title, artist=excluded.artist,"
            " norm_key=excluded.norm_key, mtime=excluded.mtime,"
            " scanned_at=excluded.scanned_at, size=excluded.size,"
            " format=excluded.format, album=excluded.album,"
            " genre=excluded.genre, style=excluded.style,"
            " year=excluded.year, cover=excluded.cover",
            (path, title, artist, tkey(title, artist), mtime, stamp, size,
             os.path.splitext(path)[1].lower().lstrip("."),
             tags["album"], tags["genre"], tags["style"], tags["year"], tags["cover"]))
        changed += 1
    removed = 0
    for path in known:
        if path not in found:
            conn.execute("DELETE FROM local_file WHERE path=?", (path,))
            removed += 1
    conn.commit()
    _rebuild_index()
    result = {"total": len(found), "new": changed, "removed": removed}
    log_resolver.info("本地库扫描：共 %s 个文件，更新 %s，移除 %s", *result.values())
    return result


def _rebuild_index():
    idx, titles = {}, {}
    for r in db().execute("SELECT path, norm_key FROM local_file"):
        key = r["norm_key"] or ""
        idx.setdefault(key, r["path"])
        nt = key.split("|")[0]
        if nt:
            titles.setdefault(nt, []).append(r["path"])
    global _idx, _titles
    with _state_lock:
        _idx, _titles = idx, titles


def index_ready():
    with _state_lock:
        return bool(_idx)


def local_match(title, artist):
    """本地库命中返回文件路径，未命中返回 None。"""
    key = tkey(title, artist)
    with _state_lock:
        hit = _idx.get(key)
        titles = dict(_titles)
    if hit:
        return hit
    nt = norm(title)
    if not nt:
        return None
    # 归一化键可能因歌手写法差异而不同，退化为标题维度做相似度比对
    best, best_score = None, 0.0
    for cand, paths in titles.items():
        if abs(len(cand) - len(nt)) > max(3, len(nt) // 4):
            continue
        score = difflib.SequenceMatcher(None, nt, cand).ratio()
        if score > best_score:
            best_score, best = score, paths[0]
    if best_score >= FUZZY_THRESHOLD:
        return best
    return None


def index_file(path, title, artist):
    """新下载的文件直接进索引，避免等到下一轮增量扫描。"""
    try:
        mtime = os.stat(path).st_mtime
        size = os.path.getsize(path)
    except OSError:
        return
    db().execute(
        "INSERT OR REPLACE INTO local_file(path,title,artist,norm_key,mtime,scanned_at,size,format)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (path, title, artist, tkey(title, artist), mtime, now(), size,
         os.path.splitext(path)[1].lower().lstrip(".")))
    db().commit()
    _rebuild_index()


# ---------------------------------------------------------------- 本地库去重

def _under(path, base):
    """path 是否位于 base 目录内（用于限定删除范围，避免误删库外文件）。"""
    if not base:
        return False
    return os.path.abspath(str(path)).startswith(os.path.abspath(str(base)).rstrip("/\\") + os.sep)


def _keep_rank(row, dl):
    """保留优先级，越小越该留下。

    手工整理的（不在下载目录里）优先于流水线下载的副本；其次留新的，
    最后按路径长度兜底保证顺序稳定。
    """
    return (1 if _under(row["path"], dl) else 0,
            -float(row["mtime"] or 0), len(str(row["path"])))


def duplicate_groups(scope="exact", paths=None):
    """找出本地库里的重复曲目，按「该保留哪一份」分组。

    scope="exact"：歌名/歌手归一化后相同，且格式与字节数都一致 —— 同一份文件的
    多份拷贝，删掉没有信息损失。
    scope="song"：只看歌名/歌手，格式或码率不同也算重复（同名不同版本），
    仅供查看，删除前要自己斟酌。

    paths 非空时只算与这些路径同组的记录：界面上勾选了哪几组就只处理哪几组，
    不用每次都把上万行全扫一遍再在 Python 里筛。
    """
    dl = download_dir()
    sql = ("SELECT id, path, title, artist, norm_key, format, size, mtime "
           "FROM local_file")
    args = []
    if paths:
        keys = {r["norm_key"] or "" for r in db().execute(
            "SELECT DISTINCT norm_key FROM local_file WHERE path IN "
            f"({','.join('?' * len(paths))})", paths)}
        keys.discard("")
        if not keys:
            return []
        sql += (f" WHERE norm_key IN ({','.join('?' * len(keys))})"
                " AND norm_key IS NOT NULL")
        args = sorted(keys)
    buckets = {}
    for r in db().execute(sql + " ORDER BY id", args):
        key = r["norm_key"] or ""
        if not key:
            continue
        if scope == "exact":
            # 格式 + 字节数都一致，才是同一份文件的多份拷贝
            key = f"{key}|{r['format'] or ''}|{r['size'] or 0}"
        buckets.setdefault(key, []).append(r)
    groups = []
    for key, rows in buckets.items():
        if len(rows) < 2:
            continue
        rows.sort(key=lambda r: _keep_rank(r, dl))
        groups.append({
            "key": key,
            "title": rows[0]["title"], "artist": rows[0]["artist"],
            "format": rows[0]["format"], "size": rows[0]["size"],
            "keep": dict(rows[0]),
            "duplicates": [dict(r) for r in rows[1:]],
        })
    groups.sort(key=lambda g: -sum(d["size"] or 0 for d in g["duplicates"]))
    return groups


def _plan(paths=None, scope="exact"):
    """算出「该删哪些」的清单。只读索引、不碰磁盘，所以预演是瞬时的。

    paths 为空表示按默认规则处理全部；给了就只删这些路径（界面上勾选后传入），
    但每组里该保留的那一份永远跳过。
    """
    allow = set(paths) if paths else None
    root = library_root()
    plan = []
    # 把 allow 传进 duplicate_groups，让分组直接在 SQL 层按 norm_key 收敛，
    # 库里有上万首歌时不用整表拉进内存
    for g in duplicate_groups(scope, paths=list(allow) if allow else None):
        for d in g["duplicates"]:
            p = d["path"]
            if allow is not None and p not in allow:
                continue
            if not _under(p, root):
                plan.append((d["id"], p, d["title"], d["size"] or 0, "不在音乐库目录内，已跳过"))
                continue
            plan.append((d["id"], p, d["title"], d["size"] or 0, ""))
    return plan


def _drop(row):
    """删一个文件，返回错误信息；空串表示成功。"""
    try:
        os.remove(row[1])
        return ""
    except OSError as e:
        return str(e)[:160]


# 上一次去重的进度，供界面轮询；WS 断了也能靠 /library/dedupe/status 兜底
_dedupe_lock = threading.Lock()
_dedupe_state = {"running": False, "dry_run": True, "done": 0, "total": 0,
                 "removed": 0, "freed_bytes": 0, "failed": 0, "ended_at": None}


def dedupe_state():
    with _dedupe_lock:
        return dict(_dedupe_state)


def _set_state(**kw):
    with _dedupe_lock:
        _dedupe_state.update(kw)


def dedupe(paths=None, dry_run=True, scope="exact"):
    """删除重复文件并同步索引。

    性能要点（原先删除阶段会卡死）：音乐库在 NAS 上，os.remove 是网络 IO，
    逐个同步删几千个文件能拖到分钟级，而 SQLite 又把整批删除攒在一个超长写
    事务里，最后一次 commit。这里改成先算清单、并发删文件、索引删除分批提交，
    同时用 dedupe_state 把进度抛给界面。

    scope 透传给 _plan/duplicate_groups：exact 只删同一份文件的多份拷贝，
    song 连同名不同码率/不同版本一起删。默认 exact，界面行为不变。
    """
    t0 = time.time()
    plan = _plan(paths, scope)
    total = len(plan)
    removed, freed, failed = [], 0, []
    _set_state(running=True, dry_run=dry_run, done=0, total=total,
               removed=0, freed_bytes=0, failed=0, ended_at=None)
    emit({"type": "dedupe_progress", "dry_run": dry_run, "done": 0,
          "total": total, "removed": 0, "freed_bytes": 0, "failed": 0})

    blocked = [p for p in plan if p[4]]
    if dry_run:
        for fid, path, title, size, err in plan:
            if err:
                failed.append({"path": path, "error": err})
                continue
            removed.append({"path": path, "title": title, "size": size})
            freed += size
    else:
        todo = [p for p in plan if not p[4]]
        ok = set()
        if todo:
            # 8 路并发：网络文件系统上并发删比串行快一个数量级
            with ThreadPoolExecutor(max_workers=8) as pool:
                for row, err in zip(todo, pool.map(_drop, todo)):
                    if err:
                        failed.append({"path": row[1], "error": err})
                    else:
                        ok.add(row[0])
        for fid, path, title, size, err in blocked:
            failed.append({"path": path, "error": err})
        # 索引删除分批提交：一次删几千行会让 SQLite 长时间持有写锁，
        # 期间扫描/推送全被堵住。
        ids = sorted(ok)
        for i in range(0, len(ids), 500):
            db().executemany("DELETE FROM local_file WHERE id=?",
                             [(x,) for x in ids[i:i + 500]])
            db().commit()
        for fid, path, title, size, err in plan:
            if fid in ok:
                removed.append({"path": path, "title": title, "size": size})
                freed += size

    done = len(removed) + len(failed)
    _set_state(running=False, done=done, removed=len(removed), freed_bytes=freed,
               failed=len(failed), ended_at=now())
    emit({"type": "dedupe_progress", "dry_run": dry_run, "done": done,
          "total": total, "removed": len(removed), "freed_bytes": freed,
          "failed": len(failed)})
    if removed and not dry_run:
        _rebuild_index()
    log_resolver.info("本地去重：%s %s 份，释放 %.1f MB，失败 %s，耗时 %.1f 秒",
                      "预演将删除" if dry_run else "已删除", len(removed),
                      freed / 1048576, len(failed), time.time() - t0)
    return {"dry_run": dry_run, "removed": len(removed), "freed_bytes": freed,
            "files": removed, "failed": failed, "elapsed": round(time.time() - t0, 1)}


def dedupe_async(paths=None, dry_run=True):
    """后台跑去重，避免同步 def 占住线程池线程直到请求超时。"""
    with _dedupe_lock:
        if _dedupe_state["running"]:
            raise RuntimeError("去重任务正在运行")
        # 先在这里占位再起线程：线程启动有延迟，不占位的话两次连点会都通过检查
        _dedupe_state.update(running=True, dry_run=dry_run, done=0, total=0,
                             removed=0, freed_bytes=0, failed=0, ended_at=None)
    threading.Thread(target=_dedupe_bg, args=(paths, dry_run),
                     name="dedupe", daemon=True).start()
    return dedupe_state()


def _dedupe_bg(paths, dry_run):
    try:
        dedupe(paths=paths, dry_run=dry_run)
    except Exception as e:                      # noqa: BLE001
        log_resolver.error("去重任务异常：%s", e, exc_info=True)
        _set_state(running=False, ended_at=now())


# ---------------------------------------------------------------- 音源

def get_sources(only_enabled=True):
    q = "SELECT * FROM source"
    if only_enabled:
        q += " WHERE enabled=1"
    q += " ORDER BY priority, id"
    return db().execute(q).fetchall()


def _quality_chain():
    """首选音质失败就按 quality_fallback 逐级降级重试（2.3 / 2.7）。"""
    primary = str(_cfg("download", "quality", "320"))
    chain = [primary]
    for fallback in _cfg("download", "quality_fallback", ["128", "96"]) or []:
        chain.append(str(fallback))
    return chain


def _search(source, keyword):
    cfg = source_cfg(source["cfg"])
    tmpl = cfg.get("search")
    if not tmpl:
        raise RuntimeError("音源未配置 search 模板")
    # safe="" 必须给：默认 quote 不转义 / & ?，分类名里出现 "R&B" 会截断查询串
    url = tmpl.format(kw=requests.utils.quote(keyword, safe=""),
                      limit=cfg.get("limit", 20))
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    # list_path 为空/"$" 表示结果就是顶层数组（GD 音乐台等返回裸数组，没有包裹字段）
    lp = cfg.get("list_path")
    items = (data if lp in (None, "", "$") else jget(data, lp)) or []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        artist = it.get(cfg.get("artist_field", "artist"), "")
        if isinstance(artist, list):
            artist = "/".join(a.get("name", "") if isinstance(a, dict) else str(a) for a in artist)
        sid = it.get(cfg.get("id_field", "id"), "")
        if sid in (None, ""):
            continue
        out.append({
            "id": str(sid),
            "title": str(it.get(cfg.get("title_field", "name"), "")),
            "artist": str(artist or ""),
            "album": str(it.get(cfg.get("album_field", "album"), "") or ""),
            "cover": it.get(cfg.get("cover_field", "cover"), ""),
            "lrc": it.get(cfg.get("lrc_field", "lrc"), ""),
            "raw": it,
        })
    return out


def _pick(results, title, artist):
    """在线搜索结果里挑最相似的一条。

    歌手明确不符时大幅降权：歌名对得上但唱的是别人（如同名翻唱）不应被当成命中。
    """
    nt, na = norm(title), norm(main_artist(artist))
    best, best_score = None, 0.0
    for it in results[:8]:
        score = difflib.SequenceMatcher(None, nt, norm(it["title"])).ratio()
        n_art = norm(it["artist"])
        if na and n_art:
            if na in n_art or n_art in na:
                score += 0.15
            else:
                score *= 0.5
        if score > best_score:
            best_score, best = score, it
    return (best, best_score) if best and best_score >= SEARCH_MATCH_MIN else (None, 0.0)


def _audio_url(source, song_id, quality, raw=None):
    cfg = source_cfg(source["cfg"])
    # LX 音源脚本：取链接交给沙箱里的 JS（search 仍走 http 模板拿 musicInfo）
    if (cfg.get("kind") or "http") == "lx":
        return _lx_url(source, cfg, raw, quality)
    tmpl = cfg.get("url")
    if not tmpl:
        raise RuntimeError("音源未配置 url 模板")
    # 各音源的音质参数命名不同（GD 音乐台的 flac 写作 999），支持映射表换算
    q = (cfg.get("quality_map") or {}).get(str(quality), str(quality))
    url = tmpl.format(id=requests.utils.quote(str(song_id), safe=""), quality=q)
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    for pth in (cfg.get("url_paths") or []):
        v = jget(r.json(), pth)
        if isinstance(v, str) and v.startswith("http"):
            return v
    return None


# TuneSync 音质 → lx 音质名降级链（lx 桌面版音质名：128k/320k/flac/flac24bit）
LX_QUALITY_CHAINS = {
    "flac": ["flac24bit", "flac", "320k"],
    "320": ["320k", "128k"],
    "128": ["128k"],
    "96": ["128k"],
}
_lx_script_cache: dict = {}          # script_url -> (fetched_at, content)
_LX_CACHE_TTL = 600                  # 社区脚本可能更新，URL 拉取结果缓存 10 分钟


def _lx_script(cfg):
    """cfg.script 支持 http(s) URL（自动拉取+缓存）或直接粘贴的脚本文本。"""
    s = (cfg.get("script") or "").strip()
    if not s:
        raise RuntimeError("LX 音源未配置脚本（cfg.script 填脚本 URL 或粘贴脚本文本）")
    if s.lower().startswith(("http://", "https://")):
        cached = _lx_script_cache.get(s)
        if cached and time.time() - cached[0] < _LX_CACHE_TTL:
            return cached[1]
        r = requests.get(s, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        content = r.text
        if len(content.strip()) < 10:
            raise RuntimeError("拉取音源脚本失败：内容为空")
        _lx_script_cache[s] = (time.time(), content)
        return content
    return s


# LX 音源默认搜索模板：GD 音乐台按平台换 source（均实测可用，免部署）
LX_SEARCH_SOURCES = {"wy": "netease", "kg": "kugou", "kw": "kuwo",
                     "tx": "tencent", "mg": "migu"}
DEFAULT_LX_SEARCH = ("https://music-api.gdstudio.xyz/api.php?types=search&source={src}"
                     "&name={kw}&count={limit}&pages=1")

# GD 音乐台不支持 source=kuwo（400），kw 平台的 LX 脚本改配酷我官方搜索
# （与「酷我音乐」http 源同款模板，返回 abslist，id 在 MUSICRID 且带 MUSIC_ 前缀）
KUWO_LX_SEARCH = ("http://search.kuwo.cn/r.s?client=kt&all={kw}&pn=0&rn={limit}&ft=music"
                  "&rformat=json&encoding=utf8&vipver=1&mobi=1&strategy=2012&cluster=0"
                  "&uid=2574109560&ver=kwplayer_ar_8.5.4.2&vermerge=1")
KUWO_LX_FIELDS = {"list_path": "abslist", "id_field": "MUSICRID",
                  "title_field": "NAME", "artist_field": "ARTIST", "album_field": "ALBUM"}

_LX_ID_KEYS = ("id", "songmid", "rid", "hash", "copyrightId", "songid", "url_id")


def _lx_music_info(raw):
    """社区 LX 脚本按平台读特定字段（wy/tx→songmid、kg→hash、kw→rid、mg→copyrightId），
    而 search 模板返回的条目字段名不统一（GD 音乐台只有 id、酷我官方搜索是 MUSICRID）。
    这里把已知 id 字段互为别名补齐（不覆盖已有值）：只要 search 的 source 与
    cfg.platform 匹配（如 platform=wy 配 source=netease），补出的就是平台自己的 id。
    """
    if not isinstance(raw, dict):
        return raw
    mi = dict(raw)
    val = next((str(mi[k]) for k in _LX_ID_KEYS if mi.get(k)), None)
    if not val:
        # 酷我官方搜索：MUSICRID="MUSIC_228908" → 剥前缀得到平台 id
        mr = str(mi.get("MUSICRID") or "")
        if mr.lower().startswith("music_"):
            val = mr[6:]
    if val:
        for k in _LX_ID_KEYS:
            mi.setdefault(k, val)
    return mi


def _lx_url(source, cfg, raw, quality):
    """走 LX 沙箱：按音质降级链逐个调用脚本的 musicUrl。"""
    script = _lx_script(cfg)
    lxbridge.BRIDGE.ensure(script, name=source["name"])
    errors = []
    for t in LX_QUALITY_CHAINS.get(str(quality), [f"{quality}k"]):
        try:
            url = lxbridge.BRIDGE.call(cfg.get("platform", "wy"), "musicUrl",
                                       {"musicInfo": _lx_music_info(raw), "type": t})
            if url:
                return url
            errors.append(f"{t}: 无链接")
        except Exception as e:
            errors.append(f"{t}: {str(e)[:80]}")
    raise RuntimeError("; ".join(errors) or "LX 脚本未返回链接")


def probe_source(source):
    """探活：固定关键词跑一遍 search → 取链接。返回 (ok, err)。"""
    try:
        kw = _cfg("source_health", "probe_keyword", "周杰伦 晴天")
        results = _search(source, kw)
        if not results:
            raise RuntimeError("搜索无结果")
        for quality in _quality_chain():
            url = _audio_url(source, results[0]["id"], quality, raw=results[0].get("raw"))
            if url:
                return True, None
        raise RuntimeError("未取到下载链接")
    except Exception as e:
        return False, str(e)[:200]


def record_probe(source_id, ok, err=None):
    threshold = int(_cfg("source_health", "fail_threshold", 3) or 3)
    conn = db()
    row = conn.execute("SELECT fail_count FROM source WHERE id=?", (source_id,)).fetchone()
    if not row:
        return None
    fails = 0 if ok else (row["fail_count"] or 0) + 1
    health = "healthy" if ok else ("down" if fails >= threshold else "degraded")
    # ok_count/err_count 只增不减，给「历史成功率」用；fail_count 是连续失败计数，
    # 成功就清零，拿去算成功率会得到 100% 的假数据
    conn.execute("UPDATE source SET health=?, fail_count=?, last_error=?, last_check=?,"
                 " ok_count=COALESCE(ok_count,0)+?, err_count=COALESCE(err_count,0)+? WHERE id=?",
                 (health, fails, None if ok else str(err)[:200], now(),
                  1 if ok else 0, 0 if ok else 1, source_id))
    conn.commit()
    return health


def check_all_sources():
    for s in get_sources(only_enabled=False):
        ok, err = probe_source(s)
        health = record_probe(s["id"], ok, err)
        log_resolver.info("音源「%s」探活：%s %s", s["name"], "正常" if ok else "失败", err or "")
        emit({"type": "source-health", "source": s["name"], "health": health,
              "ok": ok, "error": err})


# ---------------------------------------------------------------- 下载与校验

def verify_file(path):
    """魔数 + 体积 + 时长三关，任一不过视为失败。"""
    min_bytes = _num("download", "min_bytes", MIN_BYTES)
    min_seconds = _num("download", "min_duration_sec", 10, float)
    try:
        if os.path.getsize(path) < min_bytes:
            return False, f"文件过小（<{min_bytes} 字节）"
    except OSError as e:
        return False, f"无法读取：{e}"
    ext = os.path.splitext(path)[1].lower()
    check = MAGIC.get(ext)
    if check is None:
        return False, f"不支持的格式 {ext}"
    with open(path, "rb") as f:
        head = f.read(16)
    if not check(head):
        return False, f"魔数与扩展名不符（{ext}）"
    try:
        m = MFile(path)
        length = m.info.length if (m and m.info) else 0
    except Exception:
        length = 0
    if length < min_seconds:
        return False, f"时长不足（{length:.0f}s）"
    return True, None


def _fetch(url, dest):
    with requests.get(url, headers=UA, stream=True, timeout=DL_TIMEOUT) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(65536):
                if chunk:
                    f.write(chunk)
    return os.path.getsize(dest)


def _ext_from(url, meta):
    ext = os.path.splitext(url.split("?")[0].split("#")[0])[1].lower()
    if ext in AUDIO_EXT:
        return ext
    # 有些音源不带扩展名，用返回的 content-type 猜
    ct = (meta.get("raw") or {}).get("type") or ""
    return {"mp3": ".mp3", "flac": ".flac", "m4a": ".m4a", "aac": ".m4a",
            "ogg": ".ogg", "wav": ".wav"}.get(str(ct).lower(), ".mp3")


def _try_source(source, title, artist, album):
    """在一个音源上跑完整链路。返回 (dest, meta) 或抛出异常。"""
    results = _search(source, f"{title} {artist}".strip())
    best, score = _pick(results, title, artist)
    if not best:
        raise RuntimeError("搜索无匹配结果")
    errors = []
    for quality in _quality_chain():
        try:
            url = _audio_url(source, best["id"], quality, raw=best.get("raw"))
            if not url:
                errors.append(f"音质 {quality}: 无下载链接")
                continue
            tmp = os.path.join(tmp_dir(), f"dl_{os.getpid()}_{int(time.time() * 1000)}")
            try:
                _fetch(url, tmp)
                ext = _ext_from(url, best)
                final = tmp + ext
                os.replace(tmp, final)
                ok, why = verify_file(final)
                if not ok:
                    os.remove(final)
                    errors.append(f"音质 {quality}: {why}")
                    continue
                best["quality"] = quality
                return final, best
            except Exception:
                if os.path.exists(tmp):
                    os.remove(tmp)
                raise
        except Exception as e:
            errors.append(f"音质 {quality}: {str(e)[:80]}")
    raise RuntimeError("; ".join(errors) or "下载失败")


def place_file(tmp, meta):
    """把临时文件落到 音乐库/Downloaded/歌手/歌手 - 歌名.ext，并登记索引。"""
    from .core import safe_name

    artist = safe_name(main_artist(meta.get("artist")) or "未知歌手")
    title = safe_name(meta.get("title") or "未知歌曲")
    ext = os.path.splitext(tmp)[1] or ".mp3"
    folder = os.path.join(download_dir(), artist)
    os.makedirs(folder, exist_ok=True)
    dest = os.path.join(folder, f"{artist} - {title}{ext}")
    n = 1
    while os.path.exists(dest):
        if os.path.getsize(dest) == os.path.getsize(tmp):
            os.remove(tmp)   # 同一首歌已存在，直接复用
            index_file(dest, meta.get("title", ""), meta.get("artist", ""))
            return dest, True
        dest = os.path.join(folder, f"{artist} - {title} ({n}){ext}")
        n += 1
    shutil.move(tmp, dest)
    index_file(dest, meta.get("title", ""), meta.get("artist", ""))
    return dest, False


def resolve_track(title, artist, album="", sid=None):
    """解析单首歌：本地命中 → 复用；否则多音源替补下载。

    返回 dict: {status: local|downloaded|skipped, path, source, error, meta}
    """
    if not index_ready():
        try:
            scan_library()
        except Exception as e:
            log_resolver.warning("下载前索引重建失败：%s", e)

    hit = local_match(title, artist)
    if hit:
        return {"status": "local", "path": hit, "source": "本地库", "error": None,
                "meta": {"title": title, "artist": artist, "album": album}}

    sources = [s for s in get_sources() if s["health"] != "down"]
    if not sources:
        return {"status": "skipped", "path": None, "source": None,
                "error": "没有可用音源（全部熔断或未配置）", "meta": {}}

    reasons = []
    with _semaphore():
        for s in sources:
            try:
                tmp, meta = _try_source(s, title, artist, album)
            except Exception as e:
                record_probe(s["id"], False, e)
                reasons.append(f"{s['name']}: {str(e)[:80]}")
                continue
            record_probe(s["id"], True)
            dest, existed = place_file(tmp, meta)
            log_resolver.info("下载完成 %s - %s ← %s%s", title, artist, s["name"],
                              "（已存在，复用）" if existed else "")
            meta = dict(meta)
            meta.update({"title": meta.get("title") or title,
                         "artist": meta.get("artist") or artist,
                         "album": meta.get("album") or album,
                         "sid": sid})
            return {"status": "downloaded", "path": dest, "source": s["name"],
                    "error": None, "meta": meta}
    log_resolver.warning("全部音源失败：%s - %s（%s）", title, artist, "; ".join(reasons))
    return {"status": "skipped", "path": None, "source": None,
            "error": "; ".join(reasons)[:300] or "所有音源均失败", "meta": {}}


# ---------------------------------------------------------------- 手动刮削

_scrape_lock = threading.Lock()
_scrape_state = {"running": False, "done": 0, "total": 0, "ok": 0, "failed": 0,
                 "ended_at": None, "errors": [], "logs": []}


def scrape_state():
    with _scrape_lock:
        return {**_scrape_state, "errors": list(_scrape_state["errors"]),
                "logs": list(_scrape_state["logs"])}


def _set_scrape(**kw):
    with _scrape_lock:
        _scrape_state.update(kw)


# ── AI 批量刮削：LLM 补年份/流派/风格 ────────────────────────
# 与在线刮削（六源搜索）互补：线上源拿不到的年份/流派交给模型批量补。
_ai_lock = threading.Lock()
_ai_state = {"running": False, "done": 0, "total": 0, "ok": 0, "failed": 0,
             "ended_at": None, "errors": [], "logs": []}


def ai_scrape_state():
    with _ai_lock:
        return {**_ai_state, "errors": list(_ai_state["errors"]),
                "logs": list(_ai_state["logs"])}


def _set_ai(**kw):
    with _ai_lock:
        _ai_state.update(kw)


def ai_scrape_candidates(scope="missing", limit=0):
    """AI 刮削的候选：缺年份或流派/风格的文件；scope=all 时全库都过一遍。"""
    cols = "id,path,title,artist,album,genre,style,year,cover"
    if scope == "all":
        sql = f"SELECT {cols} FROM local_file ORDER BY id"
        rows = db().execute(sql + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
    else:
        sql = (f"SELECT {cols} FROM local_file "
               "WHERE year IS NULL OR year=0 "
               "OR genre IS NULL OR genre='' "
               "OR style IS NULL OR style='' ORDER BY id")
        rows = db().execute(sql + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
    return rows


def ai_scrape_async(scope="missing", limit=0):
    """后台跑 AI 刮削，避免同步请求占住线程直到超时。"""
    with _ai_lock:
        if _ai_state["running"]:
            raise RuntimeError("AI 刮削任务正在运行")
        _ai_state.update(running=True, done=0, total=0, ok=0, failed=0,
                         ended_at=None, errors=[], logs=[])
    threading.Thread(target=_ai_scrape_bg, args=(scope, limit),
                     name="ai-scrape", daemon=True).start()
    return ai_scrape_state()


def _ai_scrape_bg(scope, limit):
    from . import recommender, scraper

    try:
        rows = [dict(r) for r in ai_scrape_candidates(scope=scope, limit=limit)]
        total = len(rows)
        _set_ai(total=total)
        cfg = recommender._llm_cfg()
        # 一批装多少首：批越大调用次数越少，但单次响应超时/解析失败浪费也越大，
        # 默认 20 首一批；rpm 限制批间间隔，默认 10 → 每批至少隔 6 秒
        batch_n = max(1, int(cfg.get("ai_batch") or 20))
        rpm = max(1, int(cfg.get("rpm") or 10))
        delay = 60.0 / rpm
        ok = failed = done = 0
        errors, logs = [], []
        for off in range(0, total, batch_n):
            chunk = rows[off:off + batch_n]
            items = [{"i": i + 1, "title": c.get("title") or os.path.basename(c["path"]),
                      "artist": c.get("artist") or "", "album": c.get("album") or ""}
                     for i, c in enumerate(chunk)]
            got = {}
            try:
                got = recommender.enrich_tracks(items)
            except Exception as e:                 # 整批失败：逐首记错，继续下一批
                log_resolver.warning("AI 刮削一批（%s 首）失败：%s", len(chunk), e)
                errors.append({"path": "", "error": f"一批 {len(chunk)} 首请求失败：{str(e)[:150]}"})
            for i, c in enumerate(chunk):
                done += 1
                meta = got.get(str(i + 1)) or {}
                year = int(meta.get("year") or 0)
                genre = (meta.get("genre") or "").strip()
                style = (meta.get("style") or "").strip()
                name = c.get("title") or os.path.basename(c["path"])
                if not (year or genre or style):
                    failed += 1
                    err = "模型没有返回该曲目的元数据"
                    errors.append({"path": c["path"], "error": err})
                    logs.append({"i": done, "total": total, "name": name,
                                 "artist": c.get("artist") or "", "ok": False,
                                 "fields": [], "source": "AI", "error": err, "logs": []})
                    continue
                fields = []
                if year and not c.get("year"):
                    fields.append("年份")
                if genre and not (c.get("genre") or "").strip():
                    fields.append("流派")
                if style and not (c.get("style") or "").strip():
                    fields.append("风格")
                try:
                    # 只补缺失字段：库里已有的值原样带上回写，避免把好数据冲掉
                    new_meta = {"year": year or c.get("year") or 0,
                                "genre": genre or (c.get("genre") or ""),
                                "style": style or (c.get("style") or ""),
                                "album": c.get("album") or "",
                                "cover": c.get("cover") or ""}
                    scraper._write_tags(c["path"], new_meta)
                    write_scrape_tags(c["path"], new_meta)
                    ok += 1
                    logs.append({"i": done, "total": total, "name": name,
                                 "artist": c.get("artist") or "", "ok": True,
                                 "fields": fields or ["无新增"], "source": "AI",
                                 "error": "", "logs": []})
                except Exception as e:             # 单首写标签失败不影响其他
                    failed += 1
                    errors.append({"path": c["path"], "error": str(e)[:160]})
                    logs.append({"i": done, "total": total, "name": name,
                                 "artist": c.get("artist") or "", "ok": False,
                                 "fields": [], "source": "AI",
                                 "error": f"写标签失败：{str(e)[:140]}", "logs": []})
            _set_ai(done=done, ok=ok, failed=failed,
                    errors=errors[-200:], logs=logs[-300:])
            if off + batch_n < total:
                time.sleep(delay)                  # RPM 限速：批与批之间至少隔 60/rpm 秒
        emit({"type": "progress", "stage": "scrape",
              "detail": f"AI 刮削完成：成功 {ok} / 失败 {failed}"})
    except Exception as e:                         # noqa: BLE001
        # 异常必须落进状态里，否则前端只看到 running 卡死或 0/0，无从排查
        log_resolver.error("AI 刮削任务异常：%s", e, exc_info=True)
        _set_ai(running=False, ended_at=now(),
                errors=[{"path": "", "error": f"AI 刮削任务异常：{e}"}],
                logs=[{"i": 0, "total": 0, "name": "AI 刮削任务异常终止",
                       "artist": "", "ok": False, "fields": [], "source": "",
                       "error": str(e)[:300], "logs": []}])
    else:
        _set_ai(running=False, ended_at=now())


def scrape_stats():
    """待刮削规模的概览：已刮削 / 缺流派 / 缺年份 / 缺封面 / 缺歌词。"""
    r = db().execute(
        "SELECT COUNT(*) total, "
        "SUM(CASE WHEN scraped_at IS NOT NULL AND scraped_at<>'' THEN 1 ELSE 0 END) done, "
        "SUM(CASE WHEN genre IS NULL OR genre='' THEN 1 ELSE 0 END) no_genre, "
        "SUM(CASE WHEN year IS NULL OR year=0 THEN 1 ELSE 0 END) no_year, "
        "SUM(CASE WHEN cover IS NULL OR cover='' THEN 1 ELSE 0 END) no_cover, "
        "SUM(CASE WHEN lyrics IS NULL OR lyrics='' THEN 1 ELSE 0 END) no_lrc "
        "FROM local_file").fetchone()
    return {"total": r["total"] or 0, "done": r["done"] or 0,
            "no_genre": r["no_genre"] or 0, "no_year": r["no_year"] or 0,
            "no_cover": r["no_cover"] or 0, "no_lrc": r["no_lrc"] or 0}


def scrape_candidates(scope="missing", limit=0):
    """挑出要手动刮削的文件。

    scope：missing 缺任一字段（流派/年份/封面/歌词/没刮过）/ all 全库重来。
    每行带 todo 布尔：True 表示仍有字段缺口。统计口径与它一致，
    避免「统计说缺几千个流派、清单却显示没得刮」的矛盾。
    """
    cols = "id,path,title,artist,scraped_at,genre,year,cover,lyrics"
    if scope == "all":
        sql = f"SELECT {cols} FROM local_file ORDER BY id"
        rows = db().execute(sql + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
    else:
        sql = (f"SELECT {cols} FROM local_file "
               "WHERE scraped_at IS NULL OR scraped_at='' "
               "OR genre IS NULL OR genre='' "
               "OR year IS NULL OR year=0 "
               "OR cover IS NULL OR cover='' "
               "OR lyrics IS NULL OR lyrics='' ORDER BY id")
        rows = db().execute(sql + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["todo"] = not (d.get("scraped_at") and d.get("genre")
                         and d.get("year") and d.get("cover") and d.get("lyrics"))
        out.append(d)
    return out


def write_scrape_tags(path, meta):
    """把刮削结果（专辑/流派/风格/年份/封面/歌词）回写到 local_file 索引。

    用 UPSERT：刚下载完的文件此刻已经 index_file 登记过，但 index_file 只写
    标题歌手，后面刮削出来的标签要能直接补在同一行上。

    歌词只记「有/无」（meta 里 lrc 或 lyrics 非空即算有），正文留在同名 .lrc 文件里。

    高并发刮削时多个 worker 同时写库，busy_timeout 等不到锁会抛
    'database is locked'——包一层 db_retry 退避重试，不让单首失败炸掉整轮。
    """
    if not path:
        return
    lyrics = "1" if (meta.get("lyrics") or meta.get("lrc")) else ""

    def _w():
        conn = db()
        conn.execute(
            "INSERT INTO local_file(path,title,artist,album,genre,style,year,cover,scraped_at,lyrics) "
            "VALUES(?,'','',?,?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET album=excluded.album,genre=excluded.genre,"
            "style=excluded.style,year=excluded.year,cover=excluded.cover,"
            "scraped_at=excluded.scraped_at,"
            "lyrics=CASE WHEN excluded.lyrics<>'' THEN excluded.lyrics ELSE local_file.lyrics END",
            (path, meta.get("album") or "", meta.get("genre") or "", meta.get("style") or "",
             int(meta.get("year") or 0), meta.get("cover") or "", now(), lyrics))
        conn.commit()
    db_retry(_w)


def _scrape_one(row):
    """刮削一个文件并回写索引。返回 (错误串, 明细 dict)。

    绝不抛异常：pool.map 会把第一个异常原样抛给消费方，一首歌炸了整轮刮削
    就跟着「异常终止」——所有失败都必须收敛成错误串逐首记录。
    """
    from . import scraper

    info = {"path": row["path"],
            "title": row["title"] or os.path.basename(row["path"]),
            "artist": row["artist"] or "", "fields": [], "source": "", "logs": []}
    try:
        detail = scraper.scrape(row["path"],
                                {"title": row["title"] or "", "artist": row["artist"] or ""})
        write_scrape_tags(row["path"], detail.get("meta") or {})
        info.update({"fields": scraper.label_hits(detail.get("hits") or {}),
                     "source": detail.get("source") or "",
                     "logs": detail.get("logs") or []})
        return "; ".join(detail.get("errors") or []), info
    except Exception as e:                      # noqa: BLE001
        log_resolver.warning("刮削单首异常 %s：%s", row["path"], e)
        return f"刮削异常：{str(e)[:180]}", info


def scrape_files(scope="missing", limit=0):
    """批量刮削：并发跑，边跑边回写索引并抛进度。

    全自动，不支持挑选子集——点开始就按「范围 + 上限」整批跑。

    稳定机制（应对几千首的大库）：
    - 并发 worker（scraper.concurrency，默认 6 上限 8）；
    - 单首超时 track_timeout（默认 180s）：超时的歌记失败跳过，不拖住整轮；
    - 停滞看门狗 stall_timeout（默认 600s）：长时间没有任何一首完成，
      判定卡死，剩余全部记超时失败并结束，前端不会永远转圈；
    - 每首的异常全部收敛成失败记录（见 _scrape_one），不会整轮崩掉。
    """
    rows = scrape_candidates(scope, limit=limit)
    total = len(rows)
    _set_scrape(running=True, done=0, total=total, ok=0, failed=0,
                ended_at=None, errors=[], logs=[])
    emit({"type": "scrape_progress", "done": 0, "total": total, "ok": 0, "failed": 0})
    if not total:
        # 空候选竞态兜底：路由预检通过后文件又被清掉时走到这，
        # 必须把状态落回结束并留一条说明，否则前端永远显示 0/0「没执行」
        _set_scrape(running=False, ended_at=now(),
                    logs=[{"i": 0, "total": 0, "name": "没有待刮削的文件",
                           "artist": "", "ok": True, "fields": [], "source": "",
                           "error": None, "logs": []}])
        emit({"type": "scrape_progress", "done": 0, "total": 0, "ok": 0,
              "failed": 0, "line": "没有待刮削的文件"})
        return {"total": 0, "ok": 0, "failed": 0, "errors": [], "logs": [], "elapsed": 0}
    ok, failed, logs = 0, [], []
    t0 = time.time()
    track_timeout = max(30, _num("scraper", "track_timeout", 180))
    stall_timeout = max(track_timeout, _num("scraper", "stall_timeout", 600))
    aborted = False

    def _record(row, err, info):
        """统一记账：成功/失败计数、明细日志、进度事件、共享状态。"""
        nonlocal ok
        name = info["title"]
        if err:
            failed.append({"path": row["path"], "error": err})
            head = f"✗ {name}"
        else:
            ok += 1
            head = f"✓ {name}"
        got = "、".join(info["fields"]) or "无新增字段"
        logs.append({"i": ok + len(failed), "total": total, "name": name,
                     "artist": info["artist"], "ok": not err,
                     "fields": info["fields"], "source": info["source"],
                     "error": err, "logs": info["logs"]})
        emit({"type": "scrape_progress", "done": ok + len(failed), "total": total,
              "ok": ok, "failed": len(failed), "current": row["path"],
              "line": f"{head} · {got}" + (f" · {err}" if err else ""),
              "fields": info["fields"], "source": info["source"]})
        _set_scrape(done=ok + len(failed), ok=ok, failed=len(failed), logs=logs[-300:])

    pool = ThreadPoolExecutor(max_workers=min(_num("scraper", "concurrency", 4), 8))
    try:
        futs = {pool.submit(_scrape_one, r): r for r in rows}
        started = {f: time.time() for f in futs}
        pending = set(futs)
        last_beat = time.time()
        while pending:
            progressed = False
            for f in list(pending):
                row = futs[f]
                if f.done():
                    pending.discard(f)
                    progressed = True
                    try:
                        err, info = f.result()
                    except Exception as e:   # _scrape_one 已兜底，这里是最后一道保险
                        err, info = (f"任务异常：{str(e)[:120]}",
                                     {"path": row["path"],
                                      "title": row["title"] or os.path.basename(row["path"]),
                                      "artist": row["artist"] or "",
                                      "fields": [], "source": "", "logs": []})
                    _record(row, err, info)
                elif time.time() - started[f] > track_timeout:
                    # 单首超时：线程杀不掉，记失败并放弃等待（后台线程跑完就自然消亡，
                    # 其网络请求都有 TIMEOUT，不会真挂一辈子）
                    pending.discard(f)
                    progressed = True
                    _record(row, f"刮削超时（单首超过 {track_timeout}s）",
                            {"path": row["path"],
                             "title": row["title"] or os.path.basename(row["path"]),
                             "artist": row["artist"] or "",
                             "fields": [], "source": "", "logs": []})
            if progressed:
                last_beat = time.time()
            elif time.time() - last_beat > stall_timeout:
                # 停滞看门狗：全部 worker 都卡死（比如整个外网不通且请求不返回），
                # 剩余歌全部记失败收尾，绝不让 running 永远卡着
                aborted = True
                for f in pending:
                    row = futs[f]
                    _record(row, f"刮削停滞超过 {stall_timeout}s，任务被看门狗终止",
                            {"path": row["path"],
                             "title": row["title"] or os.path.basename(row["path"]),
                             "artist": row["artist"] or "",
                             "fields": [], "source": "", "logs": []})
                pending.clear()
                log_resolver.error("刮削停滞超过 %ss，看门狗终止剩余任务", stall_timeout)
            else:
                time.sleep(0.5)
    finally:
        # wait=False：被放弃的超时线程不阻塞收尾；cancel_futures 取消还没开始的
        pool.shutdown(wait=False, cancel_futures=True)
    _set_scrape(running=False, ended_at=now(), errors=failed[:200], logs=logs[-300:])
    try:
        _rebuild_index()
    except Exception as e:
        log_resolver.warning("刮削后重建索引失败：%s", e)
    log_resolver.info("手动刮削：%s/%s 成功，失败 %s，耗时 %.1f 秒%s",
                      ok, total, len(failed), time.time() - t0,
                      "（看门狗提前终止）" if aborted else "")
    return {"total": total, "ok": ok, "failed": len(failed),
            "errors": failed[:200], "logs": logs[-300:],
            "elapsed": round(time.time() - t0, 1)}


def scrape_async(scope="missing", limit=0):
    """后台跑刮削，避免同步请求占住线程直到超时。"""
    with _scrape_lock:
        if _scrape_state["running"]:
            raise RuntimeError("刮削任务正在运行")
        _scrape_state.update(running=True, done=0, total=0, ok=0, failed=0,
                             ended_at=None, errors=[], logs=[])
    threading.Thread(target=_scrape_bg, args=(scope, limit),
                     name="scrape", daemon=True).start()
    return scrape_state()


def _scrape_bg(scope, limit):
    try:
        scrape_files(scope, limit=limit)
    except Exception as e:                      # noqa: BLE001
        # 异常必须落进状态里，否则前端只看到 running 卡死或 0/0，无从排查
        log_resolver.error("刮削任务异常：%s", e, exc_info=True)
        _set_scrape(running=False, ended_at=now(),
                    errors=[{"path": "", "error": f"刮削任务异常：{e}"}],
                    logs=[{"i": 0, "total": 0, "name": "刮削任务异常终止",
                           "artist": "", "ok": False, "fields": [], "source": "",
                           "error": str(e)[:300], "logs": []}])
