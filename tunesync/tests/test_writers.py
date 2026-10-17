"""writers：飞牛 db 直写、schema 指纹校验、备份与幂等重写、Navidrome Subsonic。"""
import hashlib
import json
import os
import sqlite3
from unittest import mock

import pytest

from tunesync.app import writers

# 模拟飞牛 music.db 的最小结构
FEINIU_DDL = """
CREATE TABLE playlist (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                       cover TEXT, created_at TEXT);
CREATE TABLE playlist_track (id INTEGER PRIMARY KEY AUTOINCREMENT, playlist_id INTEGER,
                             track_id INTEGER, position INTEGER);
CREATE TABLE audio_file (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT, track_id INTEGER);
CREATE TABLE track (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, artist TEXT);
"""

# 新版飞牛 music.db：audio_file 不再有 track_id，关联改走 track.audio_file_id；
# playlist/playlist_track 的 guid、user_id 是 NOT NULL 且无默认值，playlist_track 也没有 position。
FEINIU_DDL_V2 = """
CREATE TABLE playlist (id INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT NOT NULL,
                       name TEXT NOT NULL, cover_guid TEXT, user_id INTEGER NOT NULL,
                       created_at TEXT, updated_at TEXT);
CREATE TABLE playlist_track (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                             playlist_id INTEGER NOT NULL, track_id INTEGER NOT NULL,
                             added_at TEXT, created_at TEXT, updated_at TEXT,
                             UNIQUE(playlist_id, track_id));
CREATE TABLE audio_file (id INTEGER PRIMARY KEY AUTOINCREMENT, shared_library_id INTEGER,
                         path TEXT, name TEXT, suffix TEXT, size INTEGER, hash TEXT);
CREATE TABLE track (id INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT, audio_file_id INTEGER,
                    shared_library_id INTEGER, title TEXT, artist TEXT,
                    album_id INTEGER, cover_guid TEXT);
CREATE TABLE album (id INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT, name TEXT,
                    cover_guid TEXT);
CREATE TABLE user (id INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT, name TEXT, role TEXT,
                   status TEXT);
"""


@pytest.fixture
def feiniu_db(data_dir):
    """造一个结构合规的 music.db，并登记两条已入库曲目。"""
    path = data_dir / "music.db"
    conn = sqlite3.connect(path)
    conn.executescript(FEINIU_DDL)
    for tid, fname in ((101, "周杰伦 - 晴天.mp3"), (102, "周杰伦 - 稻香.mp3")):
        conn.execute("INSERT INTO track(id,title) VALUES(?,?)", (tid, fname))
        conn.execute("INSERT INTO audio_file(path,track_id) VALUES(?,?)",
                     (f"/music/Downloaded/周杰伦/{fname}", tid))
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def feiniu_db_v2(data_dir):
    """新版 schema 的 music.db，并预置一条 user_id=1 的歌单（真实库里 user_id 来源）。

    101 走 track.cover_guid、102 走 album.cover_guid、103 两者都没有，
    用来验证封面逐首往后找的兜底顺序。
    """
    path = data_dir / "music_v2.db"
    conn = sqlite3.connect(path)
    conn.executescript(FEINIU_DDL_V2)
    conn.execute("INSERT INTO album(id,guid,name,cover_guid) VALUES(5,'al5','叶惠美','cover-album')")
    for tid, fname, cover, album_id in ((101, "周杰伦 - 晴天.mp3", "cover-track", None),
                                        (102, "周杰伦 - 稻香.mp3", None, 5),
                                        (103, "周杰伦 - 稻香2.mp3", None, None)):
        conn.execute("INSERT INTO audio_file(id,path,name,suffix) VALUES(?,?,?,'mp3')",
                     (tid, f"/vol1/1000/音乐/音乐库/周杰伦/{fname}", fname))
        conn.execute("INSERT INTO track(id,audio_file_id,title,cover_guid,album_id)"
                     " VALUES(?,?,?,?,?)", (tid, tid, fname, cover, album_id))
    conn.execute("INSERT INTO user(id,guid,name,role,status) VALUES(1,'u1','youze27','admin','active')")
    conn.execute("INSERT INTO playlist(guid,name,user_id) VALUES('g0','我喜欢的',1)")
    conn.commit()
    conn.close()
    return path


def _writer(db_path, **kw):
    cfg = {"db_path": str(db_path), "backup_db": False,
           "wait_timeout_sec": 0, "wait_interval_sec": 1}
    cfg.update(kw)
    return writers.FeiniuWriter(cfg)


class TestSchemaReport:
    def test_compatible_schema_is_ok(self, feiniu_db):
        report = _writer(feiniu_db).schema_report()
        assert report["ok"] is True
        assert report["missing"] == []
        assert "playlist" in report["tables"]

    def test_missing_table_flagged(self, feiniu_db):
        conn = sqlite3.connect(feiniu_db)
        conn.execute("DROP TABLE playlist_track")
        conn.commit()
        conn.close()
        report = _writer(feiniu_db).schema_report()
        assert report["ok"] is False and "playlist_track" in report["missing"]

    def test_renamed_column_flagged(self, feiniu_db):
        conn = sqlite3.connect(feiniu_db)
        conn.execute("ALTER TABLE playlist_track RENAME COLUMN track_id TO song_ref")
        conn.commit()
        conn.close()
        report = _writer(feiniu_db).schema_report()
        assert report["ok"] is False
        assert any("track_id" in m for m in report["missing"])

    def test_absent_db_reports_error(self, data_dir):
        report = _writer(data_dir / "不存在.db").schema_report()
        assert report["ok"] is False and "不存在" in (report["error"] or "")

    def test_test_raises_on_bad_schema(self, feiniu_db):
        conn = sqlite3.connect(feiniu_db)
        conn.execute("DROP TABLE playlist")
        conn.commit()
        conn.close()
        with pytest.raises(Exception, match="schema"):
            _writer(feiniu_db).test()

    def test_schema_mismatch_raises(self, feiniu_db):
        conn = sqlite3.connect(feiniu_db)
        conn.execute("DROP TABLE playlist_track")
        conn.commit()
        conn.close()
        with pytest.raises(Exception, match="需适配"):
            _writer(feiniu_db).upsert("歌单", [("晴天", "周杰伦")], ["/music/周杰伦 - 晴天.mp3"])

    def test_no_locatable_tracks_raises(self, feiniu_db):
        with pytest.raises(Exception, match="未定位到"):
            _writer(feiniu_db).upsert("歌单", [("新歌", "未知")], ["/music/Downloaded/新/新.mp3"])

    def test_backup_created_when_enabled(self, feiniu_db, data_dir, monkeypatch):
        w = writers.FeiniuWriter({"db_path": str(feiniu_db),
                                  "backup_db": True,
                                  "wait_timeout_sec": 0, "wait_interval_sec": 1})
        monkeypatch.setattr(writers, "DATA_DIR", str(data_dir))
        w.upsert("歌单", [("晴天", "周杰伦")], ["/music/周杰伦 - 晴天.mp3"])
        backups = list((data_dir / "backups" / "feiniu").glob("music_*.db"))
        assert len(backups) == 1

    def test_backup_captures_wal_content(self, feiniu_db, data_dir, monkeypatch):
        """拷贝文件会把 -wal 里未合并的事务丢掉，backup API 才是一致快照。"""
        monkeypatch.setattr(writers, "DATA_DIR", str(data_dir))
        src = sqlite3.connect(feiniu_db)
        src.execute("PRAGMA journal_mode=WAL")
        src.execute("INSERT INTO track(title) VALUES('未合并的写入')")
        src.commit()                       # 数据在 -wal 里，主库文件里还没有
        try:
            w = writers.FeiniuWriter({"db_path": str(feiniu_db),
                                      "backup_db": True, "wait_timeout_sec": 0,
                                      "wait_interval_sec": 1})
            path = w._backup(w._conn())
        finally:
            src.close()
        bak = sqlite3.connect(path)
        titles = [r[0] for r in bak.execute("SELECT title FROM track")]
        bak.close()
        assert "未合并的写入" in titles


class TestDbMode:
    def test_creates_playlist_and_tracks(self, feiniu_db):
        w = _writer(feiniu_db)
        result = w.upsert("[学习] 歌单A", [("晴天", "周杰伦"), ("稻香", "周杰伦")],
                          ["/music/Downloaded/周杰伦/周杰伦 - 晴天.mp3",
                           "/music/Downloaded/周杰伦/周杰伦 - 稻香.mp3"])
        assert "2/2" in result
        conn = sqlite3.connect(feiniu_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM playlist WHERE name=?", ("[学习] 歌单A",)).fetchone()
        tracks = conn.execute("SELECT * FROM playlist_track WHERE playlist_id=? ORDER BY position",
                              (row["id"],)).fetchall()
        conn.close()
        assert [t["track_id"] for t in tracks] == [101, 102]

    def test_second_sync_updates_in_place(self, feiniu_db):
        w = _writer(feiniu_db)
        w.upsert("歌单", [("晴天", "周杰伦")], ["/music/周杰伦 - 晴天.mp3"])
        w.upsert("歌单", [("稻香", "周杰伦")], ["/music/周杰伦 - 稻香.mp3"])
        conn = sqlite3.connect(feiniu_db)
        playlists = conn.execute("SELECT COUNT(*) FROM playlist WHERE name='歌单'").fetchone()[0]
        tracks = conn.execute(
            "SELECT COUNT(*) FROM playlist_track pt JOIN playlist p ON p.id=pt.playlist_id"
            " WHERE p.name='歌单'").fetchone()[0]
        conn.close()
        assert playlists == 1, "同名歌单必须幂等复用"
        assert tracks == 1, "应整体重插而不是追加"

    def test_backup_created_when_enabled(self, feiniu_db, data_dir, monkeypatch):
        w = writers.FeiniuWriter({"db_path": str(feiniu_db),
                                  "backup_db": True,
                                  "wait_timeout_sec": 0, "wait_interval_sec": 1})
        monkeypatch.setattr(writers, "DATA_DIR", str(data_dir))
        w.upsert("歌单", [("晴天", "周杰伦")], ["/music/周杰伦 - 晴天.mp3"])
        backups = list((data_dir / "backups" / "feiniu").glob("music_*.db"))
        assert len(backups) == 1

    def test_backup_captures_wal_content(self, feiniu_db, data_dir, monkeypatch):
        """拷贝文件会把 -wal 里未合并的事务丢掉，backup API 才是一致快照。"""
        monkeypatch.setattr(writers, "DATA_DIR", str(data_dir))
        src = sqlite3.connect(feiniu_db)
        src.execute("PRAGMA journal_mode=WAL")
        src.execute("INSERT INTO track(title) VALUES('未合并的写入')")
        src.commit()                       # 数据在 -wal 里，主库文件里还没有
        try:
            w = writers.FeiniuWriter({"db_path": str(feiniu_db),
                                      "backup_db": True, "wait_timeout_sec": 0,
                                      "wait_interval_sec": 1})
            path = w._backup(w._conn())
        finally:
            src.close()
        bak = sqlite3.connect(path)
        titles = [r[0] for r in bak.execute("SELECT title FROM track")]
        bak.close()
        assert "未合并的写入" in titles

    def test_no_files_raises(self, feiniu_db):
        with pytest.raises(Exception, match="没有可写入"):
            _writer(feiniu_db).upsert("歌单", [], [])


class TestDbModeNewSchema:
    """新版飞牛 music.db：audio_file 无 track_id、guid/user_id NOT NULL、playlist_track 无 position。"""

    def test_schema_report_ok(self, feiniu_db_v2):
        assert _writer(feiniu_db_v2).schema_report()["ok"] is True

    def test_track_id_resolved_via_audio_file_id(self, feiniu_db_v2):
        """audio_file 没有 track_id 时，要走 track.audio_file_id 反查。

        少了这条分支，所有文件都判成未入库，upsert 会一直空等到 600s 超时。
        """
        w = _writer(feiniu_db_v2)
        conn = w._conn()
        assert w._track_id(conn, "/music/周杰伦/周杰伦 - 晴天.mp3") == 101
        assert w._track_id(conn, "/music/周杰伦/不存在.mp3") is None
        conn.close()

    def test_upsert_satisfies_not_null_columns(self, feiniu_db_v2):
        w = _writer(feiniu_db_v2)
        result = w.upsert("歌单B", [("晴天", "周杰伦"), ("稻香", "周杰伦")],
                          ["/music/周杰伦/周杰伦 - 晴天.mp3",
                           "/music/周杰伦/周杰伦 - 稻香.mp3"])
        assert "2/2" in result
        conn = sqlite3.connect(feiniu_db_v2)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM playlist WHERE name='歌单B'").fetchone()
        assert row["guid"], "guid 是 NOT NULL，必须补一个"
        assert row["user_id"] == 1, "user_id 应复用库里已有歌单的用户"
        tracks = conn.execute(
            "SELECT * FROM playlist_track WHERE playlist_id=? ORDER BY track_id",
            (row["id"],)).fetchall()
        conn.close()
        assert [t["track_id"] for t in tracks] == [101, 102]
        assert all(t["user_id"] == 1 for t in tracks)

    def test_second_sync_updates_in_place(self, feiniu_db_v2):
        w = _writer(feiniu_db_v2)
        w.upsert("歌单C", [("晴天", "周杰伦")], ["/music/周杰伦/周杰伦 - 晴天.mp3"])
        w.upsert("歌单C", [("稻香", "周杰伦")], ["/music/周杰伦/周杰伦 - 稻香.mp3"])
        conn = sqlite3.connect(feiniu_db_v2)
        cnt_p = conn.execute("SELECT COUNT(*) FROM playlist WHERE name='歌单C'").fetchone()[0]
        cnt_t = conn.execute(
            "SELECT COUNT(*) FROM playlist_track pt JOIN playlist p ON p.id=pt.playlist_id"
            " WHERE p.name='歌单C'").fetchone()[0]
        conn.close()
        assert cnt_p == 1 and cnt_t == 1

    def test_empty_db_without_user_id_still_writes(self, feiniu_db_v2):
        """一条歌单都没有时从 user 表兜底拿 user_id，不能崩在 IntegrityError 上。"""
        conn = sqlite3.connect(feiniu_db_v2)
        conn.execute("DELETE FROM playlist")
        conn.commit()
        conn.close()
        result = _writer(feiniu_db_v2).upsert("歌单D", [("晴天", "周杰伦")],
                                              ["/music/周杰伦/周杰伦 - 晴天.mp3"])
        assert "1/1" in result
        conn = sqlite3.connect(feiniu_db_v2)
        uid = conn.execute("SELECT user_id FROM playlist WHERE name='歌单D'").fetchone()[0]
        conn.close()
        assert uid == 1

    def test_same_basename_in_two_dirs_maps_to_two_tracks(self, feiniu_db_v2):
        """同名不同版本必须按路径解析到各自的 track，不能被 basename 模糊匹配合并。"""
        conn = sqlite3.connect(feiniu_db_v2)
        conn.execute("INSERT INTO audio_file(id,path,name,suffix) VALUES(104,?,'晴天.mp3','mp3')",
                     ("/vol1/1000/音乐/音乐库/周杰伦/现场版/晴天.mp3",))
        conn.execute("INSERT INTO track(id,audio_file_id,title) VALUES(104,104,'晴天')")
        conn.commit()
        conn.close()
        w = _writer(feiniu_db_v2)
        out = w.upsert("同名", [("晴天", "周杰伦"), ("晴天", "周杰伦")],
                       ["/vol1/1000/音乐/音乐库/周杰伦/周杰伦 - 晴天.mp3",
                        "/vol1/1000/音乐/音乐库/周杰伦/现场版/晴天.mp3"])
        assert "2/2" in out
        conn = sqlite3.connect(feiniu_db_v2)
        tids = [r[0] for r in conn.execute(
            "SELECT pt.track_id FROM playlist_track pt JOIN playlist p ON p.id=pt.playlist_id"
            " WHERE p.name='同名' ORDER BY pt.id")]
        conn.close()
        assert tids == [101, 104]

    def test_repeated_paths_do_not_break_unique_constraint(self, feiniu_db_v2):
        """同一个文件传两次只能写一条，否则撞 UNIQUE(playlist_id, track_id)。"""
        conn = sqlite3.connect(feiniu_db_v2)
        conn.commit()
        conn.close()
        out = _writer(feiniu_db_v2).upsert(
            "重复", [("晴天", "周杰伦"), ("晴天", "周杰伦")],
            ["/music/周杰伦/周杰伦 - 晴天.mp3", "/music/周杰伦/周杰伦 - 晴天.mp3"])
        assert "1/2" in out
        conn = sqlite3.connect(feiniu_db_v2)
        n = conn.execute("SELECT COUNT(*) FROM playlist_track pt JOIN playlist p"
                         " ON p.id=pt.playlist_id WHERE p.name='重复'").fetchone()[0]
        conn.close()
        assert n == 1

    def test_cover_taken_from_first_track(self, feiniu_db_v2):
        _writer(feiniu_db_v2).upsert("带封面", [("晴天", "周杰伦")],
                                     ["/music/周杰伦/周杰伦 - 晴天.mp3"])
        conn = sqlite3.connect(feiniu_db_v2)
        cov = conn.execute("SELECT cover_guid FROM playlist WHERE name='带封面'").fetchone()[0]
        conn.close()
        assert cov == "cover-track"

    def test_cover_falls_back_to_album_then_next_track(self, feiniu_db_v2):
        """第一首没封面就退到专辑封面；整首都没有就往后找下一首。"""
        _writer(feiniu_db_v2).upsert(
            "退专辑", [("稻香", "周杰伦"), ("稻香2", "周杰伦")],
            ["/music/周杰伦/周杰伦 - 稻香.mp3", "/music/周杰伦/周杰伦 - 稻香2.mp3"])
        conn = sqlite3.connect(feiniu_db_v2)
        cov = conn.execute("SELECT cover_guid FROM playlist WHERE name='退专辑'").fetchone()[0]
        conn.close()
        assert cov == "cover-album"

    def test_resolve_many_matches_track_id_lookup(self, feiniu_db_v2):
        w = _writer(feiniu_db_v2)
        conn = w._conn()
        paths = ["/music/周杰伦/周杰伦 - 晴天.mp3",
                 "/music/周杰伦/周杰伦 - 稻香.mp3",
                 "/music/周杰伦/不存在.mp3"]
        assert w._resolve_many(conn, paths) == {
            paths[0]: 101, paths[1]: 102}
        conn.close()

    def test_resolve_many_handles_more_than_400_paths(self, feiniu_db_v2):
        """超过 SQLite 变量数上限时要能分批，否则大歌单一进这里就报错。"""
        w = _writer(feiniu_db_v2)
        conn = w._conn()
        paths = [f"/music/周杰伦/合辑{i}/周杰伦 - 晴天.mp3" for i in range(500)]
        assert w._resolve_many(conn, paths) == {p: 101 for p in paths}
        conn.close()


@pytest.fixture
def cover_dir(data_dir):
    """造一个封面实体目录：cover-track 在 track 下、cover-album 在 album 下。"""
    base = data_dir / "covers"
    for kind, guid in (("track", "cover-track"), ("album", "cover-album")):
        d = base / kind / guid[:2]
        d.mkdir(parents=True)
        (d / guid).write_bytes(b"\xff\xd8jpeg")
    return base


class TestPlaylistCoverMaterialize:
    def test_materialize_copies_track_cover_into_playlist_dir(self, feiniu_db_v2, cover_dir):
        w = _writer(feiniu_db_v2, cover_dir=str(cover_dir))
        assert w._materialize_cover("cover-track") is True
        assert (cover_dir / "playlist" / "co" / "cover-track").is_file()

    def test_materialize_true_when_already_in_playlist(self, feiniu_db_v2, cover_dir):
        w = _writer(feiniu_db_v2, cover_dir=str(cover_dir))
        d = cover_dir / "playlist" / "gh"
        d.mkdir(parents=True)
        (d / "ghost-ok").write_bytes(b"x")
        assert w._materialize_cover("ghost-ok") is True

    def test_materialize_false_without_source_or_dir(self, feiniu_db_v2, cover_dir, data_dir):
        w = _writer(feiniu_db_v2, cover_dir=str(cover_dir))
        assert w._materialize_cover("no-such-guid") is False
        w2 = _writer(feiniu_db_v2, cover_dir=str(data_dir / "不存在目录"))
        assert w2._materialize_cover("cover-track") is False

    def test_upsert_materializes_cover(self, feiniu_db_v2, cover_dir):
        _writer(feiniu_db_v2, cover_dir=str(cover_dir)).upsert(
            "物化", [("晴天", "周杰伦")], ["/music/周杰伦/周杰伦 - 晴天.mp3"])
        assert (cover_dir / "playlist" / "co" / "cover-track").is_file()


class TestFixPlaylistCovers:
    def _seed(self, db_path):
        conn = sqlite3.connect(db_path)
        # 已正常：实体在 playlist 目录（测试中另外造文件）
        conn.execute("INSERT INTO playlist(guid,name,user_id,cover_guid)"
                     " VALUES('g-ok','正常',1,'cover-ok')")
        # 借引用：guid 指向 track 目录的实体 → 应复制（copied）
        conn.execute("INSERT INTO playlist(guid,name,user_id,cover_guid)"
                     " VALUES('g-borrow','借引用',1,'cover-track')")
        # NULL：从曲目重选 → 101 的 cover-track（reassigned）
        conn.execute("INSERT INTO playlist(guid,name,user_id) VALUES('g-null','没封面',1)")
        pid = conn.execute("SELECT id FROM playlist WHERE name='没封面'").fetchone()[0]
        conn.execute("INSERT INTO playlist_track(user_id,playlist_id,track_id)"
                     " VALUES(1,?,101)", (pid,))
        # 悬空且曲目也无封面 → missing
        conn.execute("INSERT INTO playlist(guid,name,user_id,cover_guid)"
                     " VALUES('g-ghost','悬空',1,'ghost-guid')")
        conn.commit()
        conn.close()

    def test_fix_stats_and_db_updates(self, feiniu_db_v2, cover_dir):
        self._seed(feiniu_db_v2)
        d = cover_dir / "playlist" / "co"
        d.mkdir(parents=True)
        (d / "cover-ok").write_bytes(b"x")
        w = _writer(feiniu_db_v2, cover_dir=str(cover_dir))
        stats = writers.fix_feiniu_playlist_covers(w)
        # 「我喜欢的」无曲目 → missing；正常 ok；借引用 copied；没封面 reassigned；悬空 missing
        assert stats["total"] == 5
        assert stats["ok"] == 1
        assert stats["copied"] == 1
        assert stats["reassigned"] == 1
        assert stats["missing"] == 2
        assert (cover_dir / "playlist" / "co" / "cover-track").is_file()
        conn = sqlite3.connect(feiniu_db_v2)
        cov = conn.execute("SELECT cover_guid FROM playlist WHERE name='没封面'").fetchone()[0]
        ghost = conn.execute("SELECT cover_guid FROM playlist WHERE name='悬空'").fetchone()[0]
        conn.close()
        assert cov == "cover-track"
        assert ghost == "ghost-guid"   # 没修好不乱改

    def test_fix_rejects_bad_schema(self, data_dir):
        w = _writer(data_dir / "不存在.db", cover_dir=str(data_dir))
        with pytest.raises(RuntimeError):
            writers.fix_feiniu_playlist_covers(w)


class TestNavidrome:
    def _w(self):
        return writers.NavidromeWriter({"url": "http://nd:4533/", "user": "u", "password": "p"})

    def test_auth_params_are_md5_token_salt(self):
        w = self._w()
        seen = {}
        def fake_request(method, url, params=None, headers=None, timeout=None):
            seen.update(dict(params))
            r = mock.Mock()
            r.json.return_value = {"subsonic-response": {"status": "ok", "version": "1.16.1"}}
            r.raise_for_status.return_value = None
            return r
        with mock.patch.object(writers.requests, "request", side_effect=fake_request):
            w.test()
        assert seen["u"] == "u"
        assert seen["t"] == hashlib.md5(("p" + seen["s"]).encode()).hexdigest()
        assert seen["f"] == "json"

    def test_client_name_is_always_sent(self):
        """Subsonic 的 c（客户端名）是必填。

        缺了它 Navidrome 返回 code 10 missing parameter: 'c'——这条报错完全看不出
        是少传了参数，很容易被误判成地址写错或口令不对，排查绕远路。
        """
        w = self._w()
        seen = {}

        def fake_request(method, url, params=None, headers=None, timeout=None):
            seen.update(dict(params))
            r = mock.Mock()
            r.json.return_value = {"subsonic-response": {"status": "ok", "version": "1.16.1"}}
            r.raise_for_status.return_value = None
            return r

        with mock.patch.object(writers.requests, "request", side_effect=fake_request):
            w.test()
        assert seen.get("c"), "缺少 c 参数：Navidrome 会返回 code 10"

    def test_client_name_present_on_every_call(self):
        """建歌单等写操作也必须带 c，不能只有 ping 带。"""
        w = self._w()
        seen = []

        def fake_request(method, url, params=None, headers=None, timeout=None):
            seen.append(dict(params or []))
            r = mock.Mock()
            r.json.return_value = {"subsonic-response": {"status": "ok"}}
            r.raise_for_status.return_value = None
            return r

        with mock.patch.object(writers.requests, "request", side_effect=fake_request):
            w.test()
            w._api("getPlaylists")
        assert all(p.get("c") for p in seen), f"有请求缺 c：{seen}"

    def test_missing_url_raises_on_init(self):
        with pytest.raises(ValueError, match="Navidrome"):
            writers.NavidromeWriter({})

    def test_create_playlist_posts_song_ids(self):
        w = self._w()
        calls = []
        def fake_request(method, url, params=None, headers=None, timeout=None):
            calls.append((method, url, dict(params or [])))
            ep = url.rsplit("/", 1)[-1]
            if ep == "search3":
                body = {"searchResult3": {"song": [
                    {"id": "s1", "title": "晴天", "artist": "周杰伦"}]}}
            elif ep == "getPlaylists":
                body = {"playlists": {"playlist": []}}
            else:
                body = {}
            r = mock.Mock()
            r.json.return_value = {"subsonic-response": {"status": "ok", **body}}
            r.raise_for_status.return_value = None
            return r
        with mock.patch.object(writers.requests, "request", side_effect=fake_request):
            out = w.upsert("歌单", [("晴天", "周杰伦")], ["/music/x.mp3"])
        create = [c for c in calls if c[1].endswith("createPlaylist")]
        assert create and create[0][2]["songId"] == "s1"
        assert "1/1" in out

    def test_existing_playlist_is_updated(self):
        w = self._w()
        seen = []
        def fake_request(method, url, params=None, headers=None, timeout=None):
            seen.append((url, dict(params or [])))
            ep = url.rsplit("/", 1)[-1]
            if ep == "search3":
                body = {"searchResult3": {"song": [
                    {"id": "s1", "title": "晴天", "artist": "周杰伦"}]}}
            elif ep == "getPlaylists":
                body = {"playlists": {"playlist": [{"id": "P1", "name": "歌单"}]}}
            else:
                body = {}
            r = mock.Mock()
            r.json.return_value = {"subsonic-response": {"status": "ok", **body}}
            r.raise_for_status.return_value = None
            return r
        with mock.patch.object(writers.requests, "request", side_effect=fake_request):
            w.upsert("歌单", [("晴天", "周杰伦")], ["/music/x.mp3"])
        create = [p for u, p in seen if u.endswith("createPlaylist")][0]
        assert create.get("playlistId") == "P1", "已存在的歌单应走更新而不是新建"

    def test_subsonic_error_is_reported(self):
        w = self._w()
        def fake_request(method, url, params=None, headers=None, timeout=None):
            r = mock.Mock()
            r.json.return_value = {"subsonic-response": {"status": "failed",
                                                         "error": {"message": "密码错误"}}}
            r.raise_for_status.return_value = None
            return r
        with mock.patch.object(writers.requests, "request", side_effect=fake_request):
            with pytest.raises(Exception, match="密码错误"):
                w.test()

    def _echo(self, seen):
        """把目标那首放在一堆同名不同版本的干扰项后面。"""
        def fake_request(method, url, params=None, headers=None, timeout=None):
            p = dict(params or [])
            seen.append((url, p))
            ep = url.rsplit("/", 1)[-1]
            if ep == "search3":
                if p.get("query") == "晴天 周杰伦":
                    song = [{"id": f"noise{i}", "title": "晴天",
                             "artist": "群星" if i % 2 else "周杰伦"} for i in range(30)]
                    song.append({"id": "s1", "title": "晴天", "artist": "周杰伦"})
                else:
                    song = [{"id": "s1", "title": "晴天", "artist": "周杰伦"}]
                body = {"searchResult3": {"song": song}}
            elif ep == "getPlaylists":
                body = {"playlists": {"playlist": []}}
            elif ep == "createPlaylist":
                body = {"playlist": {"id": "P9"}}
            else:
                body = {}
            r = mock.Mock()
            r.json.return_value = {"subsonic-response": {"status": "ok", **body}}
            r.raise_for_status.return_value = None
            return r
        return fake_request

    def test_search_asks_wide_enough_result_set(self):
        """候选只取 20 的话，同名版本一多就把真歌挤出结果集了。"""
        w = self._w()
        seen = []
        with mock.patch.object(writers.requests, "request",
                               side_effect=self._echo(seen)):
            out = w.upsert("歌单", [("晴天", "周杰伦")], ["/music/x.mp3"])
        q = [p for u, p in seen if u.endswith("search3")]
        assert q and all(p["songCount"] >= 50 for p in q), f"搜索候选给少了：{q}"
        assert "1/1" in out

    def test_falls_back_to_title_only_search(self):
        """带歌手搜不到时再按歌名搜一遍，好过直接把这首丢掉。"""
        w = self._w()
        seen = []

        def fake_request(method, url, params=None, headers=None, timeout=None):
            p = dict(params or [])
            seen.append(p.get("query"))
            ep = url.rsplit("/", 1)[-1]
            if ep == "search3":
                # 第一轮（带歌手）空结果，第二轮（只按歌名）才命中
                song = [] if "周杰伦" in (p.get("query") or "") else [
                    {"id": "s1", "title": "晴天", "artist": "周杰伦"}]
                body = {"searchResult3": {"song": song}}
            elif ep == "getPlaylists":
                body = {"playlists": {"playlist": []}}
            else:
                body = {}
            r = mock.Mock()
            r.json.return_value = {"subsonic-response": {"status": "ok", **body}}
            r.raise_for_status.return_value = None
            return r

        with mock.patch.object(writers.requests, "request", side_effect=fake_request):
            out = w.upsert("歌单", [("晴天", "周杰伦")], ["/music/x.mp3"])
        assert "晴天" in seen, f"没有按歌名兜底重搜：{seen}"
        assert "1/1" in out, "兜底命中后应当照常写入"

    def test_ambiguous_title_fallback_is_skipped(self):
        """放宽到只按歌名搜时，同名候选不唯一就不能认——会写串版本。"""
        w = self._w()

        def fake_request(method, url, params=None, headers=None, timeout=None):
            p = dict(params or [])
            ep = url.rsplit("/", 1)[-1]
            if ep == "search3":
                body = {"searchResult3": {"song": [
                    {"id": "a", "title": "晴天", "artist": "周杰伦"},
                    {"id": "b", "title": "晴天", "artist": "林俊杰"}]}}
            elif ep == "getPlaylists":
                body = {"playlists": {"playlist": []}}
            else:
                body = {}
            r = mock.Mock()
            r.json.return_value = {"subsonic-response": {"status": "ok", **body}}
            r.raise_for_status.return_value = None
            return r

        with mock.patch.object(writers.requests, "request", side_effect=fake_request):
            assert w.find_song("晴天", "未知歌手") is None

    def test_large_playlist_is_appended_in_batches(self):
        """大歌单不能把 songId 全塞进一次请求，URL 太长会被截断成半截歌单。"""
        w = self._w()
        seen = []

        def fake_request(method, url, params=None, headers=None, timeout=None):
            p = list(params or [])
            seen.append((url.rsplit("/", 1)[-1], p))
            ep = url.rsplit("/", 1)[-1]
            if ep == "search3":
                body = {"searchResult3": {"song": [
                    {"id": "s1", "title": "晴天", "artist": "周杰伦"}]}}
            elif ep == "getPlaylists":
                body = {"playlists": {"playlist": []}}
            elif ep == "createPlaylist":
                body = {"playlist": {"id": "P9"}}
            else:
                body = {}
            r = mock.Mock()
            r.json.return_value = {"subsonic-response": {"status": "ok", **body}}
            r.raise_for_status.return_value = None
            return r

        n = writers.BATCH * 2 + 5
        with mock.patch.object(writers.requests, "request", side_effect=fake_request):
            out = w.upsert("歌单", [("晴天", "周杰伦")] * n, ["/music/x.mp3"])
        creates = [p for ep, p in seen if ep == "createPlaylist"]
        adds = [p for ep, p in seen if ep == "addPlaylistEntry"]
        rest = n - writers.BATCH
        assert len(creates) == 1, "只应建/更新一次歌单，剩下的追加"
        assert len([x for x in creates[0] if x[0] == "songId"]) <= writers.BATCH
        assert len(adds) == -(-rest // writers.BATCH), f"剩余 {rest} 首的分批数不对：{len(adds)}"
        assert sum(len([x for x in p if x[0] == "songId"]) for p in adds) == rest
        assert all(dict(p).get("playlistId") == "P9" for p in adds), "追加必须带上新建返回的 playlistId"
        assert f"{n}/{n}" in out


class TestBuildWriters:
    def test_only_configured_targets(self, env):
        out = writers.build_writers({"navidrome": {"url": "http://nd:4533"}})
        assert [w.name for w in out] == ["navidrome"]

    def test_feiniu_included_when_present(self, env):
        out = writers.build_writers({"feiniu": {"db_path": "/feiniu-db/music.db"}})
        assert [w.name for w in out] == ["feiniu"]

    def test_wanted_filter(self, env):
        cfg = {"navidrome": {"url": "http://nd:4533"},
               "feiniu": {"db_path": "/feiniu-db/music.db"}}
        assert [w.name for w in writers.build_writers(cfg, wanted=["feiniu"])] == ["feiniu"]

    def test_empty_config_yields_nothing(self, env):
        assert writers.build_writers({}) == []
