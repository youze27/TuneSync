"""歌单写入：Navidrome（Subsonic API）与飞牛音乐（直写 music.db）。

飞牛只有 db 一种写入方式：经实测其二进制不含任何 m3u 歌单解析逻辑，
网关 API（:5666）的登录鉴权也无法打通，因此不再保留这两个伪实现。
"""
import difflib
import glob
import hashlib
import os
import random
import shutil
import sqlite3
import time
import uuid
from datetime import datetime

import requests

from .core import DATA_DIR, log_writer, main_artist, norm

UA = {"User-Agent": "TuneSync/1.0"}
TIMEOUT = 30
# search3 一次拿多少候选：同名不同版本（Live/伴奏/现场版）挤在前面，
# 给少了真正那首会被挤出结果集，歌单就凭空少歌。
SEARCH_LIMIT = 100
# 单次请求最多带多少个 songId，超出部分走 addPlaylistEntry 追加，
# 免得 URL 长到几千字符被中间层截断。
BATCH = 100

# 飞牛 music.db 的预期列集合（子集校验即可，防止版本升级后写坏库）
FEINIU_SCHEMA = {
    "playlist": {"required": ("id", "name"),
                 "optional": ("cover_guid", "cover", "created_at", "user_id")},
    "playlist_track": {"required": ("playlist_id", "track_id"),
                       "optional": ("position", "sort", "sort_order", "add_time")},
    "audio_file": {"required": (), "optional": ("path", "file_path", "track_id", "filename")},
    "track": {"required": (), "optional": ("id", "title", "artist")},
}

# 飞牛封面实体文件的存放根目录（容器内挂载点，对应 NAS 的
# /vol1/@appmeta/trim.music/cover）。playlist.cover_guid 只是引用，
# 飞牛 App 按 cover/<kind>/<guid 前两位>/<guid> 找图——歌单封面必须落在
# playlist 子目录，借 track/album 的 guid 而不复制文件的话 App 显示空白。
COVER_DIR = os.environ.get("FEINIU_COVER_DIR", "/feiniu-cover")


def difflib_ratio(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def epoch_of(value):
    """把各平台五花八门的时间戳归一成 epoch 秒；认不出来返回 None。

    Navidrome 给的是 ISO8601 带 Z，飞牛 created_at 可能是 epoch 秒/毫秒/字符串，
    「按生成时间删老歌单」要跨两个目标统一比较，必须先落到同一个量纲上。
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        v = float(value)
    else:
        s = str(value).strip()
        if s.isdigit():
            v = float(s)
        else:
            t = s.replace("Z", "+00:00").replace(" ", "T", 1) if " " in s else s.replace("Z", "+00:00")
            try:
                d = datetime.fromisoformat(t)
            except ValueError:
                return None
            if d.tzinfo is None:
                d = d.astimezone()
            return int(d.timestamp())
    # 大于 2001 年 9 月的「秒」不可能，说明给的是毫秒
    return int(v / 1000 if v > 1e11 else v)


class PlaylistWriter:
    name = "base"

    def test(self):
        """连接自检，返回给 UI 展示的说明字符串。"""
        raise NotImplementedError

    def upsert(self, name, tracks, files):
        """tracks: [(title, artist)]，files: [path]。成功返回描述字符串。"""
        raise NotImplementedError

    # 可选能力
    def ensure_visible(self, files):
        return files

    def list_playlists(self):
        """列出目标端已有歌单，供「歌单管理」页展示。

        返回 [{id, name, tracks, created, changed, cover}]，created/changed 是 epoch 秒。
        """
        raise NotImplementedError

    def delete_playlist(self, pid):
        raise NotImplementedError


# ---------------------------------------------------------------- Navidrome

class NavidromeWriter(PlaylistWriter):
    name = "navidrome"

    def __init__(self, cfg):
        cfg = cfg or {}
        self.base = (cfg.get("url") or "").rstrip("/")
        self.user = cfg.get("user", "")
        self.pw = cfg.get("password", "")
        if not self.base:
            raise ValueError("未配置 Navidrome 地址")

    def _api(self, endpoint, params=None, method="GET"):
        salt = f"{random.getrandbits(64):016x}"
        token = hashlib.md5((self.pw + salt).encode()).hexdigest()
        # c（客户端名）是必填：Navidrome 缺它会返回 code 10 missing parameter: 'c'，
        # 报错信息完全看不出是少了参数，很容易被误判成地址或口令不对。
        base = [("u", self.user), ("t", token), ("s", salt),
                ("v", "1.16.1"), ("c", "TuneSync"), ("f", "json")]
        if isinstance(params, dict):
            params = base + list(params.items())
        else:
            params = base + list(params or [])
        r = requests.request(method, f"{self.base}/rest/{endpoint}",
                             params=params, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        j = r.json().get("subsonic-response", {})
        if j.get("status") != "ok":
            raise RuntimeError(f"Subsonic 错误：{j.get('error') or j}")
        return j

    def test(self):
        j = self._api("ping")
        return f"已连接 {self.base}（Subsonic {j.get('version', '?')}）"

    def start_scan(self):
        try:
            self._api("startScan")
            return True
        except Exception as e:
            log_writer.warning("Navidrome 触发扫描失败：%s", e)
            return False

    def wait_scan(self, timeout=600):
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                j = self._api("getScanStatus")
                if not (j.get("scanStatus") or {}).get("scanning"):
                    return True
            except Exception:
                return True   # 拿不到状态就不再干等
            time.sleep(5)
        return False

    def _songs(self, query):
        j = self._api("search3", {"query": query, "songCount": SEARCH_LIMIT,
                                  "artistCount": 0, "albumCount": 0})
        return (j.get("searchResult3") or {}).get("song") or []

    @staticmethod
    def _pick(songs, title, artist):
        """在候选里挑最贴的一首，拿不准就返回 None。

        歌名完全相同但歌手对不上（Live、伴奏、翻唱满地都是）一律不认，
        宁可漏一首也不能把歌单写成串版本。
        """
        nt, na = norm(title), norm(main_artist(artist))
        best, best_score = None, 0.0
        for s in songs:
            artist_hit = bool(na and norm(s.get("artist", "")) and
                              (na in norm(s.get("artist", "")) or norm(s.get("artist", "")) in na))
            score = 0.5 * difflib_ratio(nt, norm(s.get("title", "")))
            if artist_hit:
                score += 0.5
            if score > best_score:
                best_score, best = score, s
        return (best.get("id"), best_score) if best and best_score >= 0.6 else (None, best_score)

    def find_song(self, title, artist):
        # songCount 只给 20 的话，同名不同版本一多，真正那首就被挤出结果集了，
        # 表现为歌单凭空少歌——所以一次要够宽。
        sid, _ = self._pick(self._songs(f"{title} {artist}"), title, artist)
        if sid:
            return sid
        # 首轮没命中再放宽一次：只按歌名搜，且仅当同名候选唯一时才认。
        loose = [s for s in self._songs(title) if norm(s.get("title", "")) == norm(title)]
        return loose[0].get("id") if len(loose) == 1 else None

    def _playlist_id(self, name):
        pls = (self._api("getPlaylists", {}).get("playlists") or {}).get("playlist") or []
        return next((p["id"] for p in pls if p.get("name") == name), None)

    def upsert(self, name, tracks, files):
        ids, missing = [], []
        for title, artist in tracks:
            sid = self.find_song(title, artist)
            (ids.append(sid) if sid else missing.append(f"{title} - {artist}"))
        if not ids:
            raise RuntimeError("未在 Navidrome 中定位到歌曲（可能尚未扫描完成）")
        pid = self._playlist_id(name)
        # songId 全塞进一次请求，URL 会涨到几千字符；反向代理或 Navidrome
        # 自身一截断，表现出来就是「歌单少了半截」而且不报错。
        # 所以只把首批交给 createPlaylist，剩下的分批追加。
        params = [("name", name)] + [("songId", i) for i in ids[:BATCH]]
        if pid:
            params.append(("playlistId", pid))
        j = self._api("createPlaylist", params, "POST")
        pid = pid or (j.get("playlist") or {}).get("id")
        rest = ids[BATCH:]
        for i in range(0, len(rest), BATCH):
            self._api("addPlaylistEntry", [("playlistId", pid)]
                      + [("songId", s) for s in rest[i:i + BATCH]], "POST")
        log_writer.info("Navidrome 歌单「%s」写入 %s/%s 首%s", name, len(ids), len(tracks),
                        f"，未匹配 {len(missing)} 首" if missing else "")
        for item in missing[:10]:
            log_writer.info("  Navidrome 未匹配到：%s", item)
        return f"navidrome: {len(ids)}/{len(tracks)} 首"

    def list_playlists(self):
        pls = (self._api("getPlaylists", {}).get("playlists") or {}).get("playlist") or []
        out = []
        for p in pls:
            out.append({
                "id": str(p.get("id") or ""),
                "name": p.get("name") or "",
                "tracks": int(p.get("songCount") or 0),
                "duration": int(p.get("duration") or 0),
                "created": epoch_of(p.get("created")),
                "changed": epoch_of(p.get("changed")),
                "cover": p.get("coverArt") or "",
            })
        # 新的在前；没有时间戳的沉到末尾，避免它们被当成最老的一批清掉
        out.sort(key=lambda x: x["changed"] or x["created"] or 0, reverse=True)
        return out

    def delete_playlist(self, pid):
        self._api("deletePlaylist", {"id": pid})
        log_writer.info("Navidrome 歌单已删除：%s", pid)
        return f"navidrome: 已删除歌单 {pid}"


# ---------------------------------------------------------------- 飞牛音乐

class FeiniuWriter(PlaylistWriter):
    name = "feiniu"

    def __init__(self, cfg):
        cfg = cfg or {}
        self.db_path = cfg.get("db_path", "")
        self.backup = cfg.get("backup_db", cfg.get("backup", True))
        try:
            self.wait_timeout = max(0, int(cfg.get("wait_timeout_sec", 600)))
        except (TypeError, ValueError):
            self.wait_timeout = 600
        try:
            self.wait_interval = max(1, int(cfg.get("wait_interval_sec", 10)))
        except (TypeError, ValueError):
            self.wait_interval = 10
        self.cover_dir = cfg.get("cover_dir") or COVER_DIR
        self._backed_up = False

    # ---- schema 指纹 ----
    @staticmethod
    def _columns(conn, table):
        try:
            return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
        except sqlite3.Error:
            return []

    @staticmethod
    def _pick(cols, names):
        low = [c.lower() for c in cols]
        for n in names:
            if n in low:
                return cols[low.index(n)]
        return None

    def inspect_schema(self):
        """检查 music.db 的表结构指纹。

        error   致命问题：库打不开、表/列缺失——必须拒绝写入
        warning 可继续但建议处理：例如非 WAL 模式，写入可能与飞牛锁冲突
        """
        report = {"db_path": self.db_path, "ok": False, "exists": False,
                  "tables": {}, "missing": [], "error": None, "warning": None}
        if not self.db_path or not os.path.exists(self.db_path):
            report["error"] = f"music.db 不存在：{self.db_path}"
            return report
        report["exists"] = True
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=10)
        except sqlite3.Error as e:
            report["error"] = f"无法打开数据库：{e}"
            return report
        conn.row_factory = sqlite3.Row
        try:
            for table, spec in FEINIU_SCHEMA.items():
                cols = self._columns(conn, table)
                report["tables"][table] = cols
                if not cols:
                    report["missing"].append(table)
                    continue
                low = {c.lower() for c in cols}
                for req in spec["required"]:
                    if req not in low:
                        report["missing"].append(f"{table}.{req}")
            journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
            report["journal_mode"] = journal
            if journal.lower() != "wal":
                report["warning"] = (f"music.db 当前为 {journal} 模式，"
                                     "与飞牛音乐并发写入可能锁冲突，建议先暂停飞牛音乐")
        except sqlite3.Error as e:
            report["error"] = f"读取表结构失败：{e}"
        finally:
            conn.close()
        report["ok"] = not report["missing"] and not report["error"]
        return report

    def schema_report(self):
        """UI 展示用。ok 恒表示「db 写入是否安全」。"""
        return self.inspect_schema()

    def test(self):
        r = self.inspect_schema()
        if not r["ok"]:
            raise RuntimeError(f"schema 不符，缺：{r['missing']}"
                               + (f"（{r['error']}）" if r.get("error") else ""))
        note = f"，{r['warning']}" if r.get("warning") else ""
        return f"music.db 就绪（{r.get('journal_mode', '?')} 模式，表结构匹配{note}）"

    def _conn(self):
        if not self.db_path or not os.path.exists(self.db_path):
            raise RuntimeError(f"music.db 不存在：{self.db_path}")
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=8000")
        return conn

    def _backup(self, conn):
        """写入前备份一次，保留最近 5 份。

        用 SQLite 自己的 backup API 而不是拷贝文件：WAL 模式下最近的事务
        还在 -wal 里没合并进主库，直接 copy 会把这段数据丢掉，
        真要回滚时才发现就晚了。
        """
        if self._backed_up or not self.backup:
            return None
        bdir = os.path.join(DATA_DIR, "backups", "feiniu")
        os.makedirs(bdir, exist_ok=True)
        dst_path = os.path.join(bdir, f"music_{time.strftime('%Y%m%d_%H%M%S')}.db")
        dst = sqlite3.connect(dst_path)
        try:
            with dst:                   # 事务性快照，一致读
                conn.backup(dst)
        finally:
            dst.close()
        for old in sorted(glob.glob(os.path.join(bdir, "music_*.db")))[:-5]:
            os.remove(old)
        self._backed_up = True
        log_writer.info("已备份 music.db → %s", dst_path)
        return dst_path

    def _resolve_sql(self, conn):
        """拼出「文件路径 → track.id」的查询骨架，返回 (SELECT 片段, 路径列) 或 None。

        飞牛有两种 schema：
          - audio_file 带 track_id 列（旧版），直接取；
          - audio_file 没有 track_id，关联在 track.audio_file_id（新版本），
            得先按路径找到 audio_file.id 再转 track.id。
        缺了后一条会把所有文件都判成「未入库」，一路空等到超时才报
        「未定位到任何曲目」。
        """
        cols = self._columns(conn, "audio_file")
        pathc = self._pick(cols, ("path", "file_path", "url", "location", "filename"))
        if not pathc:
            return None
        linkc = self._pick(cols, ("track_id", "trackid"))
        if linkc:
            return f'SELECT "{linkc}", "{pathc}" FROM audio_file', f'"{pathc}"'
        tcols = self._columns(conn, "track")
        afc = self._pick(tcols, ("audio_file_id", "audiofileid", "file_id"))
        if not afc:
            return None
        return (f'SELECT t.id, a."{pathc}" FROM track t JOIN audio_file a ON a.id = t."{afc}"',
                f'a."{pathc}"')

    @staticmethod
    def _pick_path_row(rows, want):
        """模糊匹配多条命中时选最可信的那条。

        rows = [(track_id, db_path)]。飞牛重扫/换库后同一 basename 会留下多行
        audio_file，旧行的 id 已失效——歌单挂上它 App 就显示「文件不存在」。
        优选序：父目录名与期望路径一致 > 文件在容器内仍存在 > 路径最短。
        """
        if not rows:
            return None
        wparent = os.path.basename(os.path.dirname(want)) if want else ""

        def rank(r):
            dbp = r[1] or ""
            score = 0
            if wparent and os.path.basename(os.path.dirname(dbp)) == wparent:
                score -= 100                      # 父目录一致：几乎肯定同一文件
            try:
                if os.path.isfile(dbp):           # 容器看得到的路径才校验，
                    score -= 10                   # 宿主机路径 /vol1/... 自然跳过
            except OSError:
                pass
            return (score, len(dbp))

        return min(rows, key=rank)

    def _track_id(self, conn, path, res=None):
        """按文件路径反查 track.id。

        匹配必须从精确往模糊退：NAS 上同名文件有 800 多组（同名不同版本），
        只用 `LIKE '%basename%'` 会把 A 目录的《晴天》解析成 B 目录那条 track，
        于是同一首歌在 playlist_track 里重复出现，撞上
        UNIQUE(playlist_id, track_id) 直接让整批歌单失败。
        """
        r = res or self._resolve_sql(conn)
        if not r:
            return None
        sel, col = r
        base = os.path.basename(path)
        # 1) 全路径精确；2) 路径结尾精确（飞牛可能把根目录换成别的挂载点）；
        # 3) 退到 basename 模糊，多条命中时按 _pick_path_row 选最可信的。
        for cond, arg in ((f"{col}=?", path),
                          (f"{col} LIKE ?", f"%/{base}")):
            row = conn.execute(f"{sel} WHERE {cond} LIMIT 1", (arg,)).fetchone()
            if row:
                return row[0]
        rows = conn.execute(f"{sel} WHERE {col} LIKE ? LIMIT 50", (f"%{base}%",)).fetchall()
        best = self._pick_path_row([(x[0], x[1]) for x in rows], path)
        return best[0] if best else None

    def _resolve_many(self, conn, paths, res=None):
        """批量反查 {路径: track_id}。整批只跑 3 条 SQL，而不是每文件一次。

        一张歌单 200 首、轮询 60 轮，逐个查就是 12000 次 SQL，等入库这一步
        自己就能吃掉几分钟。
        """
        r = res or self._resolve_sql(conn)
        if not r:
            return {}
        sel, col = r
        found = {}

        def batch(params, sql_of):
            """分批跑一条批量查询，返回结果行列表。"""
            rows = []
            for i in range(0, len(params), 200):   # 避开 SQLite 变量数上限
                chunk = params[i:i + 200]
                rows.extend(conn.execute(sql_of(len(chunk)), chunk))
            return rows

        # 1) 全路径精确，整批一条 IN 查完（绝大多数文件都走这一步）
        for tid, p in batch(todo := list(dict.fromkeys(p for p in paths if p)),
                            lambda n: f"{sel} WHERE {col} IN ({','.join('?' * n)})"):
            found.setdefault(p, tid)

        # 2) 退到 basename 模糊：路径结尾精确 → 任意位置包含。
        #    同 basename 的多条命中先全部收集，再用 _pick_path_row 统一选优
        #    （父目录一致 > 文件仍存在 > 路径最短），避免选中飞牛重扫后
        #    已失效的旧 audio_file 行——那正是歌单里「文件不存在」的来源。
        rest = [p for p in todo if p not in found]
        for tmpl in ("%/{base}", "%{base}%"):
            if not rest:
                break
            bases = list(dict.fromkeys(os.path.basename(p) for p in rest))
            rows = batch([tmpl.format(base=b) for b in bases],
                         lambda n: (f"{sel} WHERE " + " OR ".join([f"{col} LIKE ?"] * n)
                                    + " LIMIT 2000"))
            cands = {}
            for tid, dbp in rows:
                b = (dbp.rsplit("/", 1)[-1] if tmpl == "%/{base}"
                     else max((x for x in bases if x in dbp), key=len, default=None))
                if b:
                    cands.setdefault(b, []).append((tid, dbp))
            by_base = {}
            for p in rest:                        # 同名不同路径时，用各自的期望路径选优
                by_base.setdefault(os.path.basename(p), []).append(p)
            for b, wants in by_base.items():
                if b not in cands:
                    continue
                for p in wants:
                    best = self._pick_path_row(cands[b], p)
                    if best:
                        found.setdefault(p, best[0])
            rest = [p for p in rest if p not in found]
        return found

    def _cover_of(self, conn, tids):
        """取歌单封面：按顺序用前几首歌曲的封面，第一首命中即返回。

        飞牛的封面存在 track.cover_guid（新库）或 album.cover_guid，两张表
        的填充率都不高（实测 track 24%、album 67%），所以逐首往后找而不是
        只看第一首。
        """
        if not tids:
            return None
        tcc = self._pick(self._columns(conn, "track"), ("cover_guid", "cover", "cover_id"))
        acc = self._pick(self._columns(conn, "album"), ("cover_guid", "cover", "cover_id"))
        albumc = self._pick(self._columns(conn, "track"), ("album_id", "albumid"))
        if not tcc and not (acc and albumc):
            return None
        for tid in tids[:20]:
            if tcc:
                row = conn.execute(f'SELECT "{tcc}" FROM track WHERE id=?', (tid,)).fetchone()
                if row and row[0]:
                    return row[0]
            if acc and albumc:
                row = conn.execute(
                    f'SELECT a."{acc}" FROM track t JOIN album a ON a.id=t."{albumc}" '
                    f'WHERE t.id=?', (tid,)).fetchone()
                if row and row[0]:
                    return row[0]
        return None

    # ---- 歌单封面实体文件 ----
    @staticmethod
    def _cover_path(base, kind, guid):
        return os.path.join(base, kind, guid[:2], guid)

    def _cover_src(self, guid):
        """找 guid 对应的封面实体文件，返回 (kind, path)；找不到返回 (None, None)。"""
        if not guid or not os.path.isdir(self.cover_dir):
            return None, None
        for kind in ("playlist", "track", "album", "artist"):
            p = self._cover_path(self.cover_dir, kind, guid)
            if os.path.isfile(p):
                return kind, p
        return None, None

    def _materialize_cover(self, guid):
        """确保歌单封面文件落在 cover/playlist/ 下。

        我们写进 playlist.cover_guid 的引用借自 track/album，guid 有效但实体文件
        在别的子目录，飞牛 App 只在 playlist 目录找歌单封面，因此显示空白。
        把原图复制过来即可，_wXXX 缩略图飞牛会按需自生成，不用管。
        """
        kind, src = self._cover_src(guid)
        if not src:
            return False
        if kind == "playlist":
            return True
        dst = self._cover_path(self.cover_dir, "playlist", guid)
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if not os.path.exists(dst):
                shutil.copyfile(src, dst)
            return True
        except OSError as e:
            log_writer.warning("复制歌单封面失败 %s -> %s：%s", src, dst, e)
            return False

    def _user_id(self, conn):
        """playlist / playlist_track 的 user_id 是 NOT NULL 且没有默认值。

        先复用库里已有歌单的 user_id，再退到 user 表挑一个活跃管理员。
        两条路都取不到时返回 None（此时该列不存在或库是空的，旧 schema 不受影响）。
        """
        for table in ("playlist", "playlist_track"):
            col = self._pick(self._columns(conn, table), ("user_id", "userid"))
            if not col:
                continue
            row = conn.execute(f'SELECT "{col}" FROM {table} LIMIT 1').fetchone()
            if row and row[0] is not None:
                return row[0]
        ucols = self._columns(conn, "user")
        idc = self._pick(ucols, ("id",))
        if not idc:
            return None
        statusc = self._pick(ucols, ("status",))
        rolec = self._pick(ucols, ("role",))
        order = []
        if statusc:
            order.append(f"CASE WHEN \"{statusc}\"='active' THEN 0 ELSE 1 END")
        if rolec:
            order.append(f"CASE WHEN \"{rolec}\"='admin' THEN 0 ELSE 1 END")
        order.append(f'"{idc}"')
        row = conn.execute(f'SELECT "{idc}" FROM user ORDER BY {",".join(order)} LIMIT 1').fetchone()
        return row[0] if row else None

    def _guid(self):
        return uuid.uuid4().hex

    def wait_visible(self, files, timeout=None, interval=None):
        """等飞牛自身扫描把新文件入库（飞牛没有可调用的扫描 API，只能轮询 DB）。

        超时后未入库的文件由调用方降级处理，不会阻塞整条流水线。
        """
        timeout = self.wait_timeout if timeout is None else timeout
        interval = self.wait_interval if interval is None else interval
        try:
            conn = self._conn()
        except Exception as e:
            log_writer.warning("打开 music.db 等待入库失败：%s", e)
            return []
        t0 = time.time()
        pending = list(dict.fromkeys(f for f in files if f))
        ready = []
        try:
            res = self._resolve_sql(conn)
            while pending and time.time() - t0 < timeout:
                hit = self._resolve_many(conn, pending, res)
                ready.extend(p for p in pending if p in hit)
                pending = [p for p in pending if p not in hit]
                if pending and time.time() - t0 < timeout:
                    time.sleep(interval)
        finally:
            conn.close()
        if pending:
            log_writer.warning("等待飞牛入库超时，%s 个文件未入库", len(pending))
        return ready

    def ensure_visible(self, files):
        return self.wait_visible(files)

    # ---- db 模式 ----
    def _db_upsert(self, name, tracks, files):
        report = self.inspect_schema()
        if not report["ok"]:
            raise RuntimeError(f"飞牛音乐版本变更，需适配：缺 {report['missing'] or report['error']}")

        conn = self._conn()
        try:
            pcols = report["tables"]["playlist"]
            ptcols = report["tables"]["playlist_track"]
            pcol = self._pick(pcols, ("name", "title"))
            pl_link = self._pick(ptcols, ("playlist_id", "playlistid"))
            tr_link = self._pick(ptcols, ("track_id", "trackid"))
            poscol = self._pick(ptcols, ("position", "sort", "sort_order", "index"))

            # 按 track_id 去重并保持首次出现顺序：
            # 反查走的是路径匹配，同名不同版本的文件仍可能被解析到同一个
            # track.id；不去重的话 playlist_track 的
            # UNIQUE(playlist_id, track_id) 会直接炸掉整批写入。
            files = list(dict.fromkeys(f for f in files if f))
            res = self._resolve_sql(conn)
            hit = self._resolve_many(conn, files, res)
            tids, seen, missing = [], set(), []
            for f in files:
                tid = hit.get(f)
                if tid and tid not in seen:
                    seen.add(tid)
                    tids.append(tid)
                elif not tid:
                    missing.append(f)
            if missing:
                log_writer.info("等待飞牛扫描入库：%s 个文件", len(missing))
                self.wait_visible(missing)
                for tid in self._resolve_many(conn, missing).values():
                    if tid not in seen:
                        seen.add(tid)
                        tids.append(tid)
            if not tids:
                raise RuntimeError("飞牛库中未定位到任何曲目（新文件尚未被飞牛扫描入库）")
            if len(tids) < len(files):
                log_writer.warning("「%s」%s 个文件解析到同一首歌，已按 track_id 去重",
                                   name, len(files) - len(tids))

            self._backup(conn)
            plid, err = self._write_playlist(conn, name, pcols, ptcols, pcol,
                                             pl_link, tr_link, poscol, tids,
                                             self._cover_of(conn, tids))
            if err:
                raise err
            # 只在少于写入数时报错：歌单里可能有上一轮遗留的同名曲目，
            # 多出来不影响播放，等于则说明写入被静默丢弃了。
            count = conn.execute(
                f'SELECT COUNT(*) FROM playlist_track WHERE "{pl_link}"=?', (plid,)).fetchone()[0]
            if count < len(tids):
                raise RuntimeError(f"写后验证失败：写入 {len(tids)} 条，回读 {count} 条")
            log_writer.info("飞牛(db) 歌单「%s」写入 %s 首（playlist_id=%s）", name, len(tids), plid)
            return f"feiniu(db): {len(tids)}/{len(tracks)} 首"
        finally:
            conn.close()

    def _write_playlist(self, conn, name, pcols, ptcols, pcol, pl_link, tr_link,
                        poscol, tids, cover=None):
        """单事务写入；SQLITE_BUSY 自动重试 3 次。

        新版 music.db 的 playlist.guid / user_id、playlist_track.user_id 都是
        NOT NULL 且没有默认值，光插 name 会直接 IntegrityError——所以按实际
        列把必填值补齐，列不存在时才跳过。

        封面取歌单第一首歌曲的 cover_guid，INSERT OR IGNORE 让
        UNIQUE(playlist_id, track_id) 冲突退化成跳过而不是整批回滚。
        """
        uid = self._user_id(conn)
        gcol = self._pick(pcols, ("guid",))
        ucol_p = self._pick(pcols, ("user_id", "userid"))
        ucol_t = self._pick(ptcols, ("user_id", "userid"))
        ccol = self._pick(pcols, ("cover_guid", "cover", "cover_id"))
        p_extra = []
        if gcol:
            p_extra.append((gcol, self._guid()))
        if ucol_p and uid is not None:
            p_extra.append((ucol_p, uid))
        if ccol and cover:
            p_extra.append((ccol, cover))
        # 新建歌单时落一次生成时间，「歌单管理」的定时清理靠它判断老旧。
        # 统一写 epoch 秒：INTEGER 列存 int，TEXT 列会被 SQLite 转成数字字符串，
        # 两种 epoch_of() 都读得回来。更新已有歌单时不覆盖，保留首次生成时间。
        tcol = self._pick(pcols, ("created_at", "create_time", "createtime"))
        pt_extra = [ucol_t] if (ucol_t and uid is not None) else []

        last_err = None
        for attempt in range(3):
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(f'SELECT id FROM playlist WHERE "{pcol}"=? LIMIT 1',
                                   (name,)).fetchone()
                if row:
                    plid = row["id"]
                    conn.execute(f'DELETE FROM playlist_track WHERE "{pl_link}"=?', (plid,))
                    if ccol and cover:
                        conn.execute(f'UPDATE playlist SET "{ccol}"=? WHERE id=?', (cover, plid))
                else:
                    cols = [pcol] + [c for c, _ in p_extra] + ([tcol] if tcol else [])
                    vals = [name] + [v for _, v in p_extra] + ([int(time.time())] if tcol else [])
                    q = ",".join(f'"{c}"' for c in cols)
                    conn.execute(f'INSERT INTO playlist({q}) VALUES({",".join("?" * len(cols))})', vals)
                    plid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

                cols = [pl_link, tr_link] + pt_extra + ([poscol] if poscol else [])
                if poscol:
                    rows = [(plid, tid, *([uid] if pt_extra else []), i)
                            for i, tid in enumerate(tids)]
                else:
                    rows = [(plid, tid, *([uid] if pt_extra else [])) for tid in tids]
                q = ",".join(f'"{c}"' for c in cols)
                conn.executemany(
                    f'INSERT OR IGNORE INTO playlist_track({q}) VALUES({",".join("?" * len(cols))})',
                    rows)
                conn.commit()
                if cover:
                    # 借来的 guid 必须把实体文件复制到 playlist 目录，否则 App 无封面
                    self._materialize_cover(cover)
                return plid, None
            except sqlite3.Error as e:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                last_err = e
                if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                    raise
                log_writer.warning("写入 music.db 被锁，第 %s 次重试", attempt + 1)
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"music.db 锁冲突，重试 3 次仍失败：{last_err}")

    def upsert(self, name, tracks, files):
        if not files:
            raise RuntimeError("没有可写入的歌曲")
        return self._db_upsert(name, tracks, files)

    def _playlist_schema(self):
        report = self.inspect_schema()
        if not report["ok"]:
            raise RuntimeError(f"飞牛音乐版本变更，需适配：缺 {report['missing'] or report['error']}")
        return report

    def list_playlists(self):
        self._playlist_schema()
        # 只读打开：歌单管理是查看动作，绝不能因为手滑把 music.db 写坏
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            pcols = self._columns(conn, "playlist")
            ptcols = self._columns(conn, "playlist_track")
            name_col = self._pick(pcols, ("name", "title")) or "name"
            pl_link = self._pick(ptcols, ("playlist_id", "playlistid")) or "playlist_id"
            ccol = self._pick(pcols, ("created_at", "create_time", "created",
                                      "createtime", "add_time", "update_time"))
            ucol = self._pick(pcols, ("update_time", "updated_at", "modified_at"))
            cover = self._pick(pcols, ("cover_guid", "cover", "cover_id"))
            time_expr = (f'COALESCE("{ucol}", "{ccol}")' if (ucol and ccol)
                         else (f'"{ucol}"' if ucol else (f'"{ccol}"' if ccol else "NULL")))
            cov_expr = f'"{cover}"' if cover else "NULL"
            rows = conn.execute(
                f'SELECT p.id AS id, p."{name_col}" AS name, {time_expr} AS t, {cov_expr} AS cover,'
                f' (SELECT COUNT(*) FROM playlist_track pt WHERE pt."{pl_link}"=p.id) AS n'
                f' FROM playlist p ORDER BY {time_expr} IS NULL, {time_expr} DESC'
            ).fetchall()
        finally:
            conn.close()
        return [{
            "id": str(r["id"]),
            "name": r["name"] or "",
            "tracks": int(r["n"] or 0),
            "created": epoch_of(r["t"]),
            "changed": epoch_of(r["t"]),
            "cover": r["cover"] or "",
        } for r in rows]

    def delete_playlist(self, pid):
        self._playlist_schema()
        conn = self._conn()
        try:
            ptcols = self._columns(conn, "playlist_track")
            pl_link = self._pick(ptcols, ("playlist_id", "playlistid")) or "playlist_id"
            self._backup(conn)
            last_err = None
            for attempt in range(3):
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(f'DELETE FROM playlist_track WHERE "{pl_link}"=?', (pid,))
                    cur = conn.execute("DELETE FROM playlist WHERE id=?", (pid,))
                    conn.commit()
                    if not cur.rowcount:
                        raise RuntimeError(f"歌单 {pid} 不存在")
                    log_writer.info("飞牛歌单已删除：playlist_id=%s", pid)
                    return f"feiniu: 已删除歌单 {pid}"
                except sqlite3.Error as e:
                    try:
                        conn.rollback()
                    except sqlite3.Error:
                        pass
                    last_err = e
                    if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                        raise
                    log_writer.warning("删除 music.db 被锁，第 %s 次重试", attempt + 1)
                    time.sleep(2 * (attempt + 1))
            raise RuntimeError(f"music.db 锁冲突，重试 3 次仍失败：{last_err}")
        finally:
            conn.close()


def fix_feiniu_playlist_covers(writer):
    """回填飞牛歌单封面：实体文件物化 + cover_guid 引用修正。

    对每张歌单：
      1. cover_guid 的实体已在 playlist 目录 → ok，不动
      2. guid 有效但实体在 track/album → 复制到 playlist 目录（copied）
      3. guid 悬空或为 NULL → 从歌单曲目重选封面并 UPDATE（reassigned）
      4. 曲目也没封面 → missing（只能等飞牛自己扫出封面后再跑）
    """
    stats = {"total": 0, "ok": 0, "copied": 0, "reassigned": 0, "missing": 0,
             "cover_dir": writer.cover_dir,
             "cover_dir_exists": os.path.isdir(writer.cover_dir)}
    report = writer.inspect_schema()
    if not report["ok"]:
        raise RuntimeError(f"飞牛 music.db 不可用：{report['missing'] or report['error']}")
    conn = writer._conn()
    try:
        pcols = writer._columns(conn, "playlist")
        ccol = writer._pick(pcols, ("cover_guid", "cover", "cover_id"))
        if not ccol:
            raise RuntimeError("playlist 表没有封面列，无法回填")
        ptcols = writer._columns(conn, "playlist_track")
        pl_link = writer._pick(ptcols, ("playlist_id", "playlistid"))
        tr_link = writer._pick(ptcols, ("track_id", "trackid"))
        rows = conn.execute(f'SELECT id, "{ccol}" AS cg FROM playlist').fetchall()
        stats["total"] = len(rows)
        for r in rows:
            pid, guid = r["id"], r["cg"]
            if guid:
                if os.path.isfile(writer._cover_path(writer.cover_dir, "playlist", guid)):
                    stats["ok"] += 1
                    continue
                if writer._materialize_cover(guid):
                    stats["copied"] += 1
                    continue
            tids = [row[0] for row in conn.execute(
                f'SELECT "{tr_link}" FROM playlist_track WHERE "{pl_link}"=?', (pid,))]
            new_guid = writer._cover_of(conn, tids)
            if new_guid and writer._materialize_cover(new_guid):
                conn.execute(f'UPDATE playlist SET "{ccol}"=? WHERE id=?', (new_guid, pid))
                stats["reassigned"] += 1
            else:
                stats["missing"] += 1
        conn.commit()
    finally:
        conn.close()
    log_writer.info("飞牛歌单封面回填：%s", stats)
    return stats


def build_writers(targets_cfg, wanted=None, respect_enabled=True):
    """按配置构建启用的 writer 列表。

    respect_enabled=False 用于「测试连接」这类显式指定目标的场景：
    即使 enabled=false 也要能构造出来做一次探测。
    """
    out = []
    nav = (targets_cfg or {}).get("navidrome") or {}
    if nav.get("url") and (nav.get("enabled", True) or not respect_enabled):
        w = NavidromeWriter(nav)
        if not wanted or w.name in wanted:
            out.append(w)
    fei = (targets_cfg or {}).get("feiniu") or {}
    if fei and (fei.get("enabled", True) or not respect_enabled):
        w = FeiniuWriter(fei)
        if not wanted or w.name in wanted:
            out.append(w)
    return out


def build_writer(targets_cfg, target):
    """只构造一个目标的 writer，用于歌单管理这种指定目标的操作。"""
    nav = (targets_cfg or {}).get("navidrome") or {}
    if target == "navidrome" and nav.get("url"):
        return NavidromeWriter(nav)
    fei = (targets_cfg or {}).get("feiniu") or {}
    if target == "feiniu" and fei:
        return FeiniuWriter(fei)
    raise RuntimeError(f"目标 {target} 未配置或不可用")


def cleanup_old_playlists(targets_cfg, keep_days, dry_run=False, only_names=None):
    """按歌单生成时间删除老旧歌单，返回每个目标的处理明细。

    只删「认得出生成时间且早于 keep_days」的歌单；时间认不出的（如飞牛
    playlist 表没有时间列）一律跳过，宁可留着也不误删。
    """
    now_ts = time.time()
    cutoff = now_ts - max(1, int(keep_days)) * 86400
    results, total = [], 0
    for target in ("navidrome", "feiniu"):
        try:
            w = build_writer(targets_cfg, target)
        except Exception as e:
            results.append({"target": target, "ok": False, "error": f"未配置：{e}", "removed": []})
            continue
        removed, failed = [], []
        try:
            pls = w.list_playlists()
        except Exception as e:
            results.append({"target": target, "ok": False,
                            "error": f"读取歌单列表失败：{str(e)[:200]}", "removed": []})
            continue
        for p in pls:
            if only_names and p["name"] not in only_names:
                continue
            ts = p.get("created") or p.get("changed")
            if not ts or ts >= cutoff:
                continue
            if dry_run:
                removed.append({"id": p["id"], "name": p["name"], "created": ts})
                continue
            try:
                w.delete_playlist(p["id"])
                removed.append({"id": p["id"], "name": p["name"], "created": ts})
            except Exception as e:
                failed.append({"id": p["id"], "name": p["name"], "error": str(e)[:160]})
        total += len(removed)
        results.append({"target": target, "ok": True, "scanned": len(pls),
                        "removed": removed, "failed": failed,
                        "cutoff": cutoff, "keep_days": keep_days, "dry_run": dry_run})
        log_writer.info("歌单清理[%s]：扫描 %s 张，命中 %s 张，失败 %s 张",
                        target, len(pls), len(removed), len(failed))
    return {"total": total, "results": results}
