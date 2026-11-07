"""听歌画像：只读 Spindle / Navidrome / ListenBrainz，回答「用户最近到底在听什么」。

数据源（全部只读，绝不写入）：
* Spindle      ``play_events(played_at, nd_track_id, source)`` —— 实时打点；
* Navidrome    ``scrobbles`` / ``annotation`` / ``media_file`` / ``playlist`` —— 播放时间线、
                累计最爱、曲目元信息、曲库里已有的歌单名；
* ListenBrainz ``/1/user/{user}/listens`` + ``/playlists/createdfor`` —— 其它设备的 scrobble
                与官方每日推荐历史（账号与 token 自动从 Navidrome 的 user / user_props 读取）。

Spindle live 事件、Navidrome scrobbles、ListenBrainz listens 经常是同一次播放，
按「分钟 + 歌名 + 歌手」去重后再统计。曲库 genre 大多为空，所以「口味 / 心情变化」
用更可靠的信号推断：年代分布、歌手集中度、重复播放率、深夜占比、新歌手占比、
近 7 天 vs 上一周期的变化，以及**曲库里已有歌单的主题**（防止每次都生成「夏夜兜风」）。

产物 profile() 既给模型看（text，可直接塞进 system prompt 的中文），也给前端
「个性化」面板看（结构化字段）。结果缓存 5 分钟，避免每次对话都扫一遍大库。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime

import requests

from .core import log_api, merged_config

_CACHE = {"at": 0.0, "data": None}
_LBZ_CACHE = {"at": 0.0, "data": None}
_TTL = 300
_LBZ = "https://api.listenbrainz.org/1"


def _cfg():
    c = merged_config().get("taste") or {}
    lb = c.get("listenbrainz") or {}
    return {
        "enabled": c.get("enabled", True) is not False,
        "spindle_db": os.environ.get("SPINDLE_DB") or c.get("spindle_db") or "/spindle/stats.db",
        "navidrome_db": os.environ.get("NAVIDROME_DB") or c.get("navidrome_db") or "/nddata/navidrome.db",
        "lookback_days": max(1, int(c.get("lookback_days", 30))),
        "recent_limit": max(5, int(c.get("recent_limit", 30))),
        "user": str(c.get("user") or "").strip(),
        "lbz_enabled": lb.get("enabled", True) is not False,
        "lbz_user": str(lb.get("user") or "").strip(),
        "lbz_token": os.environ.get("LISTENBRAINZ_TOKEN") or str(lb.get("token") or "").strip(),
        "playlist_limit": max(0, int(c.get("playlist_limit", 18))),
    }


def _uri(path):
    return os.path.abspath(path).replace("?", "%3f").replace("#", "%23")


def _open(path):
    """只读打开一个可能正被别的进程写着的 WAL 库。

    :ro 挂载下 SQLite 有时无法打开 -shm，这时把 db/wal/shm 快照到临时目录再读，
    保证「读不到」不会让智能体整轮失败。
    """
    if not path or not os.path.isfile(path):
        return None, None
    try:
        con = sqlite3.connect(f"file:{_uri(path)}?mode=ro", uri=True, timeout=3)
        con.row_factory = sqlite3.Row
        con.execute("SELECT 1").fetchone()
        return con, None
    except Exception as e:
        log_api.debug("只读直连 %s 失败，改用快照：%s", path, e)
    tmp = tempfile.mkdtemp(prefix="ts-taste-")
    try:
        dst = os.path.join(tmp, "snapshot.db")
        for suffix in ("", "-wal", "-shm"):
            src = path + suffix
            if os.path.isfile(src):
                shutil.copy2(src, dst + suffix)
        con = sqlite3.connect(f"file:{_uri(dst)}?mode=ro", uri=True, timeout=3)
        con.row_factory = sqlite3.Row
        con.execute("SELECT 1").fetchone()
        return con, tmp
    except Exception as e:
        log_api.warning("听歌画像无法读取 %s：%s", path, e)
        shutil.rmtree(tmp, ignore_errors=True)
        return None, None


def _close(con, tmp):
    try:
        if con:
            con.close()
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def _nd_identity(cfg):
    """ListenBrainz 用户名 / token 从 Navidrome 库自动取，不在配置里存密钥。"""
    user, token = cfg["lbz_user"], cfg["lbz_token"]
    con, tmp = _open(cfg["navidrome_db"])
    if not con:
        return user, token
    try:
        if not user:
            r = con.execute("SELECT user_name FROM user ORDER BY id LIMIT 1").fetchone()
            user = (r["user_name"] if r else "") or ""
        if not token:
            r = con.execute("SELECT value FROM user_props WHERE key='ListenBrainzSessionKey' LIMIT 1").fetchone()
            token = (r["value"] if r else "") or ""
    except Exception as e:
        log_api.debug("读取 Navidrome 用户身份失败：%s", e)
    finally:
        _close(con, tmp)
    return user, token


def _lbz_get(path, params, token, timeout=5):
    headers = {"User-Agent": "TuneSync/1.0 (self-hosted music agent)"}
    if token:
        headers["Authorization"] = f"Token {token}"
    r = requests.get(_LBZ + path, params=params, headers=headers, timeout=timeout)
    if r.status_code in (204, 404):
        return {}
    r.raise_for_status()
    return r.json() or {}


def _lbz_sources(cfg, force=False):
    """并行拉 ListenBrainz 最近听歌 + 官方推荐歌单，任何一个失败都不影响其它源。"""
    if not cfg["lbz_enabled"]:
        return [], [], []
    if not force and _LBZ_CACHE["data"] is not None and time.time() - _LBZ_CACHE["at"] < _TTL:
        return _LBZ_CACHE["data"]
    user, token = _nd_identity(cfg)
    if not user:
        return [], [], []
    cutoff = int(time.time()) - cfg["lookback_days"] * 86400

    def listens():
        d = _lbz_get(f"/user/{user}/listens", {"count": 100}, token)
        out = []
        for l in (d.get("payload") or {}).get("listens") or []:
            ts = int(l.get("listened_at") or 0)
            if ts < cutoff:
                continue
            m = l.get("track_metadata") or {}
            info = m.get("additional_info") or {}
            out.append((ts, m.get("track_name") or "", m.get("artist_name") or "",
                        m.get("release_name") or "", int(info.get("year") or 0), ""))
        return out

    def createdfor():
        d = _lbz_get(f"/user/{user}/playlists/createdfor", {"count": 20, "offset": 0}, token)
        return [str(p.get("title") or p.get("name") or "")
                for p in (d.get("playlists") or []) if (p.get("title") or p.get("name"))]

    def stats():
        d = _lbz_get(f"/stats/user/{user}/artists", {"range": "all_time", "count": 10}, token)
        return [(a.get("artist_name") or "", int(a.get("listen_count") or 0))
                for a in ((d.get("payload") or {}).get("artists") or []) if a.get("artist_name")]

    result = {"listens": [], "createdfor": [], "artists": []}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {"listens": pool.submit(listens), "createdfor": pool.submit(createdfor),
                   "artists": pool.submit(stats)}
        # 三个请求并行，统一最多等 7 秒；逐个 result(timeout=7) 会串行累加成 21 秒。
        wait(list(futures.values()), timeout=7)
        for key, fut in futures.items():
            if not fut.done():
                log_api.debug("ListenBrainz %s 超时", key)
                continue
            try:
                result[key] = fut.result()
            except Exception as e:
                log_api.debug("ListenBrainz %s 不可用：%s", key, str(e)[:100])
    data = (result["listens"], result["createdfor"], result["artists"])
    _LBZ_CACHE["at"], _LBZ_CACHE["data"] = time.time(), data
    return data


def _media_meta(path, ids):
    meta = {}
    con, tmp = _open(path)
    if not con:
        return meta
    try:
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            if not chunk:
                continue
            sql = ("SELECT id,title,artist,album,year,genre FROM media_file "
                   f"WHERE id IN ({','.join('?' * len(chunk))})")
            for r in con.execute(sql, chunk):
                meta[r["id"]] = (r["title"] or "", r["artist"] or "",
                                 r["album"] or "", int(r["year"] or 0), r["genre"] or "")
    except Exception as e:
        log_api.debug("读取曲目元信息失败：%s", e)
    finally:
        _close(con, tmp)
    return meta


def _timeline(cfg, force=False):
    """[(ts,title,artist,album,year,genre)] 倒序 + 已生效的数据源列表。"""
    cutoff = int(time.time()) - cfg["lookback_days"] * 86400
    raw, sources = [], []

    con, tmp = _open(cfg["navidrome_db"])
    if con:
        try:
            for r in con.execute(
                "SELECT s.submission_time AS ts, m.title, m.artist, m.album, m.year, m.genre "
                "FROM scrobbles s LEFT JOIN media_file m ON m.id = s.media_file_id "
                "WHERE s.submission_time >= ? ORDER BY s.submission_time DESC LIMIT 800",
                (cutoff,),
            ):
                raw.append((int(r["ts"] or 0), r["title"] or "", r["artist"] or "",
                            r["album"] or "", int(r["year"] or 0), r["genre"] or ""))
            sources.append("Navidrome")
        except Exception as e:
            log_api.debug("读取 Navidrome scrobbles 失败：%s", e)
        finally:
            _close(con, tmp)

    con, tmp = _open(cfg["spindle_db"])
    if con:
        try:
            sql = "SELECT played_at, nd_track_id, user FROM play_events WHERE played_at > 0 AND played_at >= ?"
            args = [cutoff]
            if cfg["user"]:
                sql += " AND user = ?"
                args.append(cfg["user"])
            sql += " ORDER BY played_at DESC LIMIT 800"
            events = list(con.execute(sql, args))
            meta = _media_meta(cfg["navidrome_db"], [r["nd_track_id"] for r in events])
            for r in events:
                title, artist, album, year, genre = meta.get(r["nd_track_id"]) or ("", "", "", 0, "")
                raw.append((int(r["played_at"]), title, artist, album, year, genre))
            sources.append("Spindle")
        except Exception as e:
            log_api.debug("读取 Spindle play_events 失败：%s", e)
        finally:
            _close(con, tmp)

    try:
        lbz, _created, _artists = _lbz_sources(cfg, force)
        if lbz:
            raw += lbz
            sources.append("ListenBrainz")
    except Exception as e:
        log_api.debug("ListenBrainz 合并失败：%s", e)

    seen, out = set(), []
    for r in sorted(raw, key=lambda x: x[0], reverse=True):
        if not r[1]:
            continue
        key = (r[0] // 60, r[1], r[2])
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out, sources


def _favorites(cfg):
    con, tmp = _open(cfg["navidrome_db"])
    if not con:
        return []
    try:
        return [f"{r['title']} - {r['artist']}" for r in con.execute(
            "SELECT m.title, m.artist, a.play_count, a.starred, a.rating "
            "FROM annotation a JOIN media_file m ON m.id = a.item_id "
            "WHERE a.starred = 1 OR a.rating >= 4 OR a.play_count >= 2 "
            "ORDER BY a.play_count DESC, a.rating DESC LIMIT 12")
            if r["title"]]
    except Exception as e:
        log_api.debug("读取收藏失败：%s", e)
        return []
    finally:
        _close(con, tmp)


def _library_playlists(cfg):
    """曲库已有的歌单名 + 抽样曲目。用来告诉模型「别再造第 8 张夏夜兜风了」。"""
    limit = cfg["playlist_limit"]
    if not limit:
        return []
    con, tmp = _open(cfg["navidrome_db"])
    if not con:
        return []
    try:
        rows = list(con.execute(
            "SELECT id, name, song_count FROM playlist ORDER BY updated_at DESC LIMIT ?", (limit,)))
        out = []
        for r in rows:
            sample = [f"{t['title']}-{t['artist']}" for t in con.execute(
                "SELECT m.title, m.artist FROM playlist_tracks pt "
                "JOIN media_file m ON m.id = pt.media_file_id "
                "WHERE pt.playlist_id = ? ORDER BY pt.id LIMIT 4", (r["id"],))]
            out.append({"name": r["name"] or "", "count": int(r["song_count"] or 0),
                        "sample": sample})
        return out
    except Exception as e:
        log_api.debug("读取曲库歌单失败：%s", e)
        return []
    finally:
        _close(con, tmp)


def _primary(artist):
    for sep in ("、", "/", "＆", "&", ",", "，"):
        artist = artist.split(sep)[0]
    return artist.strip()


def _era(year):
    if not year or year < 1900:
        return "年代不详"
    return f"{(int(year) // 10) * 10} 年代"


def _slot(hour):
    if hour < 6:
        return "深夜"
    if hour < 10:
        return "清晨"
    if hour < 18:
        return "白天"
    if hour < 22:
        return "傍晚"
    return "夜间"


def _window(rows, now_ts, a, b):
    hi, lo = now_ts - a * 86400, now_ts - b * 86400
    return [r for r in rows if lo <= r[0] < hi]


def _avg_year(rows):
    ys = [r[4] for r in rows if 1900 <= r[4] <= 2100]
    return round(sum(ys) / len(ys)) if ys else 0


def _night_ratio(rows):
    if not rows:
        return 0.0
    return sum(1 for r in rows if datetime.fromtimestamp(r[0]).hour >= 22
               or datetime.fromtimestamp(r[0]).hour < 6) / len(rows)


def _insights(rows, now_ts):
    out = []
    if not rows:
        return out
    titles = [r[1] for r in rows]
    artists = [_primary(r[2]) for r in rows if r[2]]
    out.append(f"近 {max(1, (now_ts - rows[-1][0]) // 86400 + 1)} 天记录 {len(rows)} 次播放，"
               f"覆盖 {len(set(titles))} 首歌 / {len(set(artists))} 位歌手")

    eras = Counter(_era(r[4]) for r in rows)
    known = sum(v for k, v in eras.items() if k != "年代不详")
    if known >= 3:
        top = [k for k, _ in eras.most_common(2) if k != "年代不详"][:2]
        if top:
            pct = round(sum(eras[k] for k in top) / known * 100)
            out.append(f"主要集中在 {'、'.join(top)}（占已知年代歌曲的 {pct}%）")

    slots = Counter(_slot(datetime.fromtimestamp(r[0]).hour) for r in rows)
    if slots:
        k, v = slots.most_common(1)[0]
        out.append(f"最常在「{k}」听歌（占 {round(v / len(rows) * 100)}%）")

    repeat = 1 - len(set(titles)) / len(titles)
    if len(rows) >= 8:
        mood = "更愿意回听熟悉的歌，偏怀旧与稳定" if repeat > 0.35 else "更愿意尝新，探索欲较强"
        out.append(f"重复播放率 {round(repeat * 100)}%，{mood}")

    recent, prev = _window(rows, now_ts, 0, 7), _window(rows, now_ts, 7, 30)
    if len(recent) >= 4 and len(prev) >= 4:
        ry, py = _avg_year(recent), _avg_year(prev)
        if ry and py and abs(ry - py) >= 4:
            out.append(f"近 7 天平均发行年 {ry}，比上一周期{'更早' if ry < py else '更新'}"
                       f" {abs(ry - py)} 年，口味{'转向更经典的老歌' if ry < py else '转向年代更近的作品'}")
        rn, pn = _night_ratio(recent), _night_ratio(prev)
        if abs(rn - pn) >= 0.18:
            out.append(f"深夜听歌占比从 {round(pn * 100)}% 变到 {round(rn * 100)}%，"
                       f"{'夜晚陪伴型听歌变多' if rn > pn else '白天场景变多'}")
        new_artists = set(_primary(r[2]) for r in recent if r[2]) - set(_primary(r[2]) for r in prev if r[2])
        if new_artists:
            out.append(f"近 7 天新出现 {len(new_artists)} 位此前没听过的歌手："
                       + "、".join(list(new_artists)[:4]))
    return out


def _build(force=False):
    cfg = _cfg()
    empty = {"available": False, "text": "", "recent": [], "top_artists": [],
             "longterm_artists": [], "eras": [], "slots": [], "insights": [],
             "favorites": [], "library_playlists": [], "lbz_playlists": [],
             "stats": {}, "sources": [], "updated_at": "", "reason": ""}
    if not cfg["enabled"]:
        empty["reason"] = "听歌画像已在配置中关闭"
        return empty

    rows, sources = _timeline(cfg, force)
    playlists = _library_playlists(cfg)
    # _timeline 里已经按需刷新过 ListenBrainz，这里直接吃缓存，避免重复发 3 个请求
    _, lbz_playlists, lbz_artists = (_lbz_sources(cfg) if cfg["lbz_enabled"] else ([], [], []))
    if not rows and not playlists:
        empty["reason"] = "还没有播放记录，多听几首歌就会生成画像"
        if sources:
            empty["sources"] = sources
        return empty

    now_ts = int(time.time())
    artists = Counter(_primary(r[2]) for r in rows if r[2])
    eras = Counter(_era(r[4]) for r in rows)
    slots = Counter(_slot(datetime.fromtimestamp(r[0]).hour) for r in rows)
    insights = _insights(rows, now_ts)
    favorites = _favorites(cfg)
    recent = [{"title": r[1], "artist": r[2], "year": r[4],
               "when": datetime.fromtimestamp(r[0]).strftime("%m-%d %H:%M")}
              for r in rows[:cfg["recent_limit"]]]

    lines = ["【用户最近真实听过的歌（来自听歌记录，不是模型猜的）】"]
    if recent:
        lines.append("最近播放（新→旧）：" + "、".join(f"{x['title']}-{x['artist']}" for x in recent[:24]))
    if artists:
        lines.append("常听歌手：" + "、".join(f"{k}({v})" for k, v in artists.most_common(6)))
    if lbz_artists:
        lines.append("ListenBrainz 长期最爱：" + "、".join(k for k, _ in lbz_artists[:6]))
    if favorites:
        lines.append("收藏与高播放：" + "、".join(favorites[:6]))
    if insights:
        lines.append("【听歌习惯与近期变化】")
        lines += [f"- {s}" for s in insights]
    if playlists:
        lines.append("【曲库里已经存在的歌单（不要生成主题雷同的新歌单）】")
        lines.append("、".join(p["name"] for p in playlists[:16]))
    if lbz_playlists:
        lines.append("【ListenBrainz 已经给你推荐过的歌单】" + "、".join(lbz_playlists[:8]))
    lines.append("【使用要求】上面是用户真的听过的曲目、真实习惯与已有歌单："
                 "不要把其中的歌当成新发现反复推荐，不要每次都推最热门最有代表性的那几首，"
                 "也不要再生成与已有歌单主题高度相似的名字；顺着近期口味变化做延展，"
                 "优先给出同气质但更冷门、更长尾的选择。")
    text = "\n".join(lines)[:2200]

    return {
        "available": True, "text": text, "recent": recent,
        "top_artists": [{"name": k, "count": v} for k, v in artists.most_common(8)],
        "longterm_artists": [{"name": k, "count": v} for k, v in lbz_artists[:8]],
        "eras": [{"name": k, "count": v} for k, v in eras.most_common()],
        "slots": [{"name": k, "count": v} for k, v in slots.most_common()],
        "insights": insights, "favorites": favorites,
        "library_playlists": playlists, "lbz_playlists": lbz_playlists,
        "stats": {"plays": len(rows), "tracks": len(set(r[1] for r in rows)),
                  "artists": len(artists), "lookback_days": cfg["lookback_days"],
                  "night_ratio": round(_night_ratio(rows) * 100),
                  "playlists": len(playlists)},
        "sources": sources + (["ListenBrainz 推荐历史"] if lbz_playlists else []),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"), "reason": "",
    }


def profile(force=False):
    if not force and _CACHE["data"] is not None and time.time() - _CACHE["at"] < _TTL:
        return _CACHE["data"]
    try:
        data = _build(force)
    except Exception as e:
        log_api.warning("生成听歌画像失败：%s", e)
        data = {"available": False, "text": "", "recent": [], "top_artists": [],
                "longterm_artists": [], "eras": [], "slots": [], "insights": [],
                "favorites": [], "library_playlists": [], "lbz_playlists": [],
                "stats": {}, "sources": [], "updated_at": "",
                "reason": f"画像生成失败：{str(e)[:100]}"}
    _CACHE["at"], _CACHE["data"] = time.time(), data
    return data


def start_warmer():
    """后台每 4 分钟刷新一次画像，让每次对话都命中缓存、不被外部 API 拖慢。"""
    import threading

    def loop():
        while True:
            try:
                profile(force=True)
            except Exception as e:
                log_api.debug("听歌画像预热失败：%s", e)
            time.sleep(240)

    threading.Thread(target=loop, name="taste-warmer", daemon=True).start()


def one_line():
    """给「存为长期偏好」按钮用的一句话摘要。"""
    p = profile()
    if not p.get("available"):
        return ""
    bits = [p["insights"][0]] if p.get("insights") else []
    if p.get("top_artists"):
        bits.append("常听 " + "、".join(x["name"] for x in p["top_artists"][:3]))
    if p.get("eras") and p["eras"][0]["name"] != "年代不详":
        bits.append("偏好 " + p["eras"][0]["name"])
    return "；".join(bits)[:120]
