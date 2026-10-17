"""resolver：本地索引、匹配、下载校验、音质降级、熔断与替补。"""
import json
import os
import time
from unittest import mock

import pytest

from tunesync.app import core, resolver, lxbridge


def _mp3():
    """带 ID3 头、体积 > 1MB 的假 mp3（只过魔数校验，不做真实解码）。"""
    return b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 1_100_000


def _resp(payload, stream=False):
    """构造一个够用的假响应对象。"""
    if stream:
        ctx = mock.MagicMock()
        ctx.__enter__.return_value.iter_content.return_value = [_mp3()]
        return ctx
    r = mock.Mock()
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


class TestScanLibrary:
    def test_indexes_files_and_fills_tags(self, library, wav):
        wav(library, "周杰伦 - 晴天.wav", seconds=2)
        wav(library, "live/未知.wav", seconds=2)
        resolver.scan_library()
        rows = list(core.db().execute("SELECT * FROM local_file"))
        assert len(rows) == 2
        assert all(r["norm_key"] for r in rows)

    def test_incremental_scan_skips_unchanged(self, library, wav):
        wav(library, "a - b.wav", seconds=1)
        assert resolver.scan_library()["new"] == 1
        assert resolver.scan_library()["new"] == 0

    def test_removed_files_are_dropped(self, library, wav):
        f = wav(library, "x - y.wav", seconds=1)
        resolver.scan_library()
        os.remove(f)
        assert resolver.scan_library()["removed"] == 1
        assert core.db().execute("SELECT COUNT(*) c FROM local_file").fetchone()["c"] == 0

    def test_missing_root_is_reported_not_raised(self, env, settings):
        settings(library={"root": "Z:/绝对不存在的目录"})
        result = resolver.scan_library()
        assert "error" in result

    def test_scanned_at_recorded(self, library, wav):
        wav(library, "c - d.wav", seconds=1)
        resolver.scan_library()
        assert core.db().execute("SELECT scanned_at FROM local_file").fetchone()["scanned_at"]

    def test_ignores_non_audio_and_hidden_dirs(self, library, wav):
        wav(library, "keep - me.wav", seconds=1)
        wav(library, ".hidden/skip - me.wav", seconds=1)
        (library / "cover.jpg").write_bytes(b"x")
        resolver.scan_library()
        paths = [r["path"] for r in core.db().execute("SELECT path FROM local_file")]
        assert len(paths) == 1 and "keep - me.wav" in paths[0]


class TestLocalMatch:
    def test_exact_hit(self, library, wav):
        wav(library, "周杰伦 - 晴天.wav", seconds=1)
        resolver.scan_library()
        hit = resolver.local_match("晴天", "周杰伦")
        assert hit and hit.endswith("晴天.wav")

    def test_annotation_insensitive(self, library, wav):
        wav(library, "周杰伦 - 晴天.wav", seconds=1)
        resolver.scan_library()
        assert resolver.local_match("晴天 (Live)", "周杰伦")

    def test_feat_clause_insensitive(self, library, wav):
        wav(library, "A - 歌.wav", seconds=1)
        resolver.scan_library()
        assert resolver.local_match("歌 feat. B", "A")

    def test_miss_returns_none(self, library, wav):
        wav(library, "周杰伦 - 晴天.wav", seconds=1)
        resolver.scan_library()
        assert resolver.local_match("完全不同的歌", "别人") is None


class TestVerifyFile:
    def test_rejects_too_small(self, library, settings):
        settings(download={"min_bytes": 100})
        p = library / "t.bin"
        p.write_bytes(b"ID3" + b"\x00" * 10)
        ok, why = resolver.verify_file(str(p))
        assert not ok and "过小" in why

    def test_rejects_magic_mismatch(self, library, settings):
        settings(download={"min_bytes": 1})
        p = library / "fake.mp3"
        p.write_bytes(b"NOPE" + b"\x00" * 100)
        ok, why = resolver.verify_file(str(p))
        assert not ok and "魔数" in why

    def test_rejects_unknown_extension(self, library, settings):
        settings(download={"min_bytes": 1})
        p = library / "t.xyz"
        p.write_bytes(b"\x00" * 100)
        ok, why = resolver.verify_file(str(p))
        assert not ok and "格式" in why

    def test_accepts_valid_mp3(self, library, settings):
        settings(download={"min_bytes": 100, "min_duration_sec": 0})
        p = library / "ok.mp3"
        p.write_bytes(_mp3())
        ok, why = resolver.verify_file(str(p))
        assert ok, why

    def test_rejects_too_short(self, library, wav, settings):
        settings(download={"min_bytes": 1, "min_duration_sec": 30})
        p = wav(library, "short.wav", seconds=2)
        ok, why = resolver.verify_file(str(p))
        assert not ok and "时长" in why


class TestSearchPick:
    def test_picks_best_match(self):
        results = [{"id": "1", "title": "完全不同的歌", "artist": "别人"},
                   {"id": "2", "title": "晴天", "artist": "周杰伦"}]
        best, score = resolver._pick(results, "晴天", "周杰伦")
        assert best["id"] == "2" and score > 0.6

    def test_artist_boost_applied(self):
        results = [{"id": "1", "title": "晴天", "artist": "完全不同的歌手"}]
        best, _ = resolver._pick(results, "晴天", "周杰伦")
        assert best is None, "歌手完全不符时不该选中"

    def test_low_similarity_rejected(self):
        assert resolver._pick([{"id": "1", "title": "zzz", "artist": "yyy"}], "晴天", "周杰伦")[0] is None


class TestQualityFallback:
    """flac 拿不到链接时应自动降级重试 128 并最终成功。"""

    def _dispatch(self, requested):
        """按 URL 形态返回假的 HTTP 响应；requested 记录调用顺序。"""
        def dispatch(url, **kw):
            if kw.get("stream"):
                return _resp({}, stream=True)
            requested.append(url)
            if "search" in url:
                return _resp({"data": {"songs": [
                    {"id": 7, "name": "晴天", "artist": "周杰伦"}]}})
            if "flac" in url:
                return _resp({"data": {"url": None}})      # 高音质无链接
            return _resp({"data": {"url": "http://cdn/song.mp3"}})
        return dispatch

    def test_falls_back_when_first_quality_fails(self, env, library, settings, add_source):
        settings(download={"quality": "flac", "quality_fallback": ["128"],
                                               "min_bytes": 100, "min_duration_sec": 0})
        add_source(name="主源")
        requested = []
        with mock.patch.object(resolver.requests, "get", side_effect=self._dispatch(requested)):
            result = resolver.resolve_track("晴天", "周杰伦")
        assert result["status"] == "downloaded", result.get("error")
        assert result["meta"]["quality"] == "128"
        assert any("quality=flac" in u for u in requested)
        assert any("quality=128" in u for u in requested)

    def test_all_qualities_fail_marks_source(self, env, library, settings, add_source):
        settings(download={"quality": "flac", "quality_fallback": ["128"],
                                               "min_bytes": 1})
        row = add_source(name="坏源")
        def dispatch(url, **kw):
            if kw.get("stream"):
                return _resp({}, stream=True)
            if "search" in url:
                return _resp({"data": {"songs": [{"id": 7, "name": "晴天", "artist": "周杰伦"}]}})
            return _resp({"data": {"url": None}})     # 任何音质都拿不到链接
        with mock.patch.object(resolver.requests, "get", side_effect=dispatch):
            result = resolver.resolve_track("晴天", "周杰伦")
        assert result["status"] == "skipped"
        assert "坏源" in result["error"]
        after = core.db().execute("SELECT * FROM source WHERE id=?", (row["id"],)).fetchone()
        assert after["fail_count"] >= 1

    def test_falls_through_to_next_source(self, env, library, settings, add_source):
        settings(download={"quality": "flac", "quality_fallback": ["128"],
                                               "min_bytes": 100, "min_duration_sec": 0})
        add_source(name="第一源", priority=1)
        add_source(name="第二源", priority=2)

        def dispatch(url, **kw):
            if kw.get("stream"):
                return _resp({}, stream=True)
            if "first" in url and "search" in url:
                return _resp({"data": {"songs": []}})          # 搜不到 → 直接换源
            if "search" in url:
                return _resp({"data": {"songs": [
                    {"id": 9, "name": "晴天", "artist": "周杰伦"}]}})
            return _resp({"data": {"url": "http://cdn/song.mp3"}})

        conn = core.db()
        cfg = json.loads(conn.execute("SELECT cfg FROM source WHERE name='第一源'").fetchone()["cfg"])
        cfg["search"] = "http://first/search?keywords={kw}"
        conn.execute("UPDATE source SET cfg=? WHERE name='第一源'", (json.dumps(cfg),))
        conn.commit()

        with mock.patch.object(resolver.requests, "get", side_effect=dispatch):
            result = resolver.resolve_track("晴天", "周杰伦")
        assert result["status"] == "downloaded"
        assert result["source"] == "第二源"


class TestSourceHealth:
    def test_failure_counter_and_circuit_break(self, env, settings, add_source):
        settings(source_health={"fail_threshold": 3})
        row = add_source(name="坏源", fail_count=2)
        assert resolver.record_probe(row["id"], False, "boom") == "down"
        row = core.db().execute("SELECT * FROM source WHERE id=?", (row["id"],)).fetchone()
        assert row["health"] == "down" and row["fail_count"] == 3

    def test_success_resets_counter(self, env, settings, add_source):
        settings(source_health={"fail_threshold": 3})
        row = add_source(name="源", fail_count=2)
        assert resolver.record_probe(row["id"], True) == "healthy"
        row = core.db().execute("SELECT * FROM source WHERE id=?", (row["id"],)).fetchone()
        assert row["fail_count"] == 0 and row["last_error"] is None

    def test_down_source_is_skipped(self, env, library, settings, add_source):
        add_source(name="熔断源", health="down")
        result = resolver.resolve_track("晴天", "周杰伦")
        assert result["status"] == "skipped"
        assert "没有可用音源" in result["error"]

    def test_no_source_configured(self, env, library):
        result = resolver.resolve_track("晴天", "周杰伦")
        assert result["status"] == "skipped"


class TestPlaceFile:
    def test_creates_artist_folder(self, env, library):
        tmp = library / "tmp.mp3"
        tmp.write_bytes(_mp3())
        dest, existed = resolver.place_file(str(tmp), {"title": "晴天", "artist": "周杰伦"})
        assert existed is False
        assert os.path.basename(dest) == "周杰伦 - 晴天.mp3"
        assert os.path.basename(os.path.dirname(dest)) == "周杰伦"

    def test_same_file_is_reused(self, env, library):
        a = library / "a.mp3"
        a.write_bytes(_mp3())
        dest, _ = resolver.place_file(str(a), {"title": "晴天", "artist": "周杰伦"})
        b = library / "b.mp3"
        b.write_bytes(_mp3())
        dest2, existed = resolver.place_file(str(b), {"title": "晴天", "artist": "周杰伦"})
        assert existed is True and dest2 == dest

    def test_indexed_immediately(self, env, library):
        tmp = library / "c.mp3"
        tmp.write_bytes(_mp3())
        resolver.place_file(str(tmp), {"title": "晴天", "artist": "周杰伦"})
        assert resolver.local_match("晴天", "周杰伦")


class TestSourceCfgShapes:
    """真实公开音源（GD 音乐台等）的两种响应形态。"""

    def test_search_accepts_top_level_array(self, env, add_source):
        """list_path="$"：搜索结果就是顶层数组，artist 是数组。"""
        row = add_source(name="GD源", search="http://gd/search?name={kw}&count={limit}",
                         list_path="$", artist_field="artist")
        src = core.db().execute("SELECT * FROM source WHERE id=?", (row["id"],)).fetchone()
        with mock.patch.object(resolver.requests, "get", return_value=_resp([
                {"id": 42, "name": "晴天", "artist": ["周杰伦"], "album": "叶惠美"}])):
            out = resolver._search(src, "晴天 周杰伦")
        assert out[0]["id"] == "42" and out[0]["artist"] == "周杰伦"

    def test_empty_list_path_means_top_level(self, env, add_source):
        row = add_source(name="裸数组源", list_path="")
        src = core.db().execute("SELECT * FROM source WHERE id=?", (row["id"],)).fetchone()
        with mock.patch.object(resolver.requests, "get", return_value=_resp([
                {"id": 7, "name": "晴天", "artist": "周杰伦"}])):
            assert resolver._search(src, "kw")[0]["id"] == "7"

    def test_quality_map_rewrites_placeholder(self, env, add_source):
        """音质参数名换算：GD 音乐台的 flac 写作 br=999。"""
        row = add_source(name="映射源", url="http://gd/api?id={id}&br={quality}",
                         url_paths=["url"], quality_map={"flac": "999"})
        src = core.db().execute("SELECT * FROM source WHERE id=?", (row["id"],)).fetchone()
        seen = []

        def fake_get(url, **kw):
            seen.append(url)
            return _resp({"url": "http://cdn/a.flac"})

        with mock.patch.object(resolver.requests, "get", side_effect=fake_get):
            assert resolver._audio_url(src, "42", "flac") == "http://cdn/a.flac"
            assert resolver._audio_url(src, "42", "320") == "http://cdn/a.flac"
        assert "br=999" in seen[0] and "br=320" in seen[1]


class _FakeBridge:
    """LxBridge 替身：记录调用，按音质名返回预设链接。"""

    def __init__(self, urls=None, fail=()):
        self.calls = []
        self.ensured = []
        self.urls = urls or {}
        self.fail = set(fail)

    def ensure(self, script, name="lx-source"):
        self.ensured.append((script, name))
        return {"wy": {"type": "music", "actions": ["musicUrl"],
                       "qualitys": ["128k", "320k", "flac"]}}

    def call(self, source, action, info, timeout=45):
        self.calls.append((source, action, info))
        if info.get("type") in self.fail:
            raise RuntimeError(f"{info.get('type')} 不可用")
        return self.urls.get(info.get("type"))


class TestLxSource:
    """kind=lx 音源：search 仍走 http 模板，取链接交给沙箱桥。"""

    def _src(self, add_source, **cfg):
        base = {"kind": "lx", "platform": "wy", "script": "/* fake script */"}
        base.update(cfg)
        row = add_source(name="LX源", **base)
        return core.db().execute("SELECT * FROM source WHERE id=?", (row["id"],)).fetchone()

    def test_routes_to_bridge_with_raw_musicinfo(self, env, add_source, monkeypatch):
        src = self._src(add_source)
        fake = _FakeBridge(urls={"flac24bit": "http://cdn/song.flac"})
        monkeypatch.setattr(lxbridge, "BRIDGE", fake)
        out = resolver._audio_url(src, "42", "flac", raw={"id": 42, "name": "晴天"})
        assert out == "http://cdn/song.flac"
        # 第一个可用音质命中即停；info 里带上原始搜索条目当 musicInfo
        assert [c[2]["type"] for c in fake.calls] == ["flac24bit"]
        assert fake.calls[0][0] == "wy" and fake.calls[0][1] == "musicUrl"
        mi = fake.calls[0][2]["musicInfo"]
        assert mi["id"] == 42 and mi["name"] == "晴天"
        assert fake.ensured[0][1] == "LX源"          # 脚本以音源名加载

    def test_music_info_gets_platform_aliases(self, env, add_source, monkeypatch):
        """社区脚本按平台读 songmid/hash/rid，GD 搜索条目只有 id——补齐别名不覆盖已有值。"""
        src = self._src(add_source)
        fake = _FakeBridge(urls={"320k": "http://cdn/s.mp3"})
        monkeypatch.setattr(lxbridge, "BRIDGE", fake)
        resolver._audio_url(src, "42", "320",
                            raw={"id": 42, "songmid": "orig", "name": "晴天"})
        mi = fake.calls[0][2]["musicInfo"]
        assert mi["songmid"] == "orig"              # 已有值不覆盖
        assert mi["rid"] == "42" and mi["hash"] == "42"

    def test_music_info_strips_kuwo_musicrid(self, env, add_source, monkeypatch):
        """酷我官方搜索条目只有 MUSICRID="MUSIC_xxx"——剥前缀补齐全部 id 别名。"""
        src = self._src(add_source)
        fake = _FakeBridge(urls={"320k": "http://cdn/s.mp3"})
        monkeypatch.setattr(lxbridge, "BRIDGE", fake)
        resolver._audio_url(src, "MUSIC_228908", "320",
                            raw={"MUSICRID": "MUSIC_228908", "NAME": "晴天"})
        mi = fake.calls[0][2]["musicInfo"]
        assert mi["rid"] == "228908" and mi["hash"] == "228908"
        assert mi["songmid"] == "228908" and mi["MUSICRID"] == "MUSIC_228908"

    def test_quality_chain_degrades(self, env, add_source, monkeypatch):
        """flac → flac24bit/flac/320k 逐级降级，flac24bit 失败后用 flac。"""
        src = self._src(add_source)
        fake = _FakeBridge(urls={"flac": "http://cdn/song.flac"})
        monkeypatch.setattr(lxbridge, "BRIDGE", fake)
        assert resolver._audio_url(src, "42", "flac", raw={}) == "http://cdn/song.flac"
        assert [c[2]["type"] for c in fake.calls] == ["flac24bit", "flac"]

    def test_all_qualities_fail_raises(self, env, add_source, monkeypatch):
        src = self._src(add_source)
        fake = _FakeBridge(fail={"flac24bit", "flac", "320k"})
        monkeypatch.setattr(lxbridge, "BRIDGE", fake)
        with pytest.raises(RuntimeError, match="320k"):
            resolver._audio_url(src, "42", "flac", raw={})
        assert len(fake.calls) == 3                  # 整条链都试过

    def test_unknown_quality_gets_k_suffix(self, env, add_source, monkeypatch):
        src = self._src(add_source)
        fake = _FakeBridge(urls={"500k": "http://cdn/song"})
        monkeypatch.setattr(lxbridge, "BRIDGE", fake)
        assert resolver._audio_url(src, "42", "500", raw={}) == "http://cdn/song"
        assert [c[2]["type"] for c in fake.calls] == ["500k"]

    def test_missing_script_raises(self, env, add_source):
        src = self._src(add_source, script="")
        with pytest.raises(RuntimeError, match="脚本"):
            resolver._audio_url(src, "42", "flac", raw=None)

    def test_script_url_fetched_and_cached(self, env, add_source, monkeypatch):
        """script 填 URL 时自动拉取，TTL 内复用缓存不再发请求。"""
        resolver._lx_script_cache.clear()
        seen = []

        def fake_get(url, **kw):
            seen.append(url)
            r = mock.Mock()
            r.text = "/* remote script */"
            r.raise_for_status.return_value = None
            return r

        cfg = {"script": "http://ex/script.js"}
        with mock.patch.object(resolver.requests, "get", side_effect=fake_get):
            assert resolver._lx_script(cfg) == "/* remote script */"
            assert resolver._lx_script(cfg) == "/* remote script */"
        assert seen == ["http://ex/script.js"]

    def test_script_text_passthrough(self, env, add_source):
        """内联脚本文本不触发任何网络请求，原样返回。"""
        text = "/*! lx-source v1 */ lx.on(lx.EVENT_NAMES.request, () => {})"
        assert resolver._lx_script({"script": text}) == text


class TestScrapeIntegrity:
    """统计准确性与手动刮削可靠性（第十轮）。"""

    def test_scan_preserves_scrape_marks(self, library, wav):
        """刮削会改文件 mtime → 增量扫描必然重扫；旧的 INSERT OR REPLACE 把
        列清单外的 scraped_at/lyrics 重置成 NULL，统计数字随之漂移。
        改成 ON CONFLICT DO UPDATE 后这两个标记必须保住。"""
        f = wav(library, "a - b.wav", seconds=1)
        resolver.scan_library()
        resolver.write_scrape_tags(str(f), {"album": "专辑", "genre": "流行",
                                            "year": 2003, "cover": "1", "lyrics": "x"})
        os.utime(f, (time.time() + 10, time.time() + 10))
        resolver.scan_library()
        row = core.db().execute(
            "SELECT scraped_at, lyrics FROM local_file WHERE path=?", (str(f),)).fetchone()
        assert row["scraped_at"], "重扫后 scraped_at 不应被清掉"
        assert row["lyrics"] == "1"

    def test_candidates_missing_by_field_gap(self, library):
        """scope=missing 按「缺任一字段」挑：统计说缺什么、清单就列什么。"""
        conn = core.db()
        rows = [("p1", "2001-01-01 00:00:00", "流行", 2003, "1", "1"),
                ("p2", "2001-01-01 00:00:00", "", 2003, "1", "1"),
                ("p3", "", "流行", 2003, "1", "1")]
        for path, scraped, genre, year, cover, lyr in rows:
            conn.execute(
                "INSERT INTO local_file(path,title,artist,norm_key,mtime,scraped_at,"
                "genre,year,cover,lyrics) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (path, path, "a", "k", 1.0, scraped, genre, year, cover, lyr))
        conn.commit()
        missing = resolver.scrape_candidates(scope="missing")
        assert {r["path"] for r in missing} == {"p2", "p3"}
        assert all(r["todo"] for r in missing)
        p1 = next(r for r in resolver.scrape_candidates(scope="all")
                  if r["path"] == "p1")
        assert p1["todo"] is False

    def test_scrape_files_empty_sets_state(self, library):
        """空候选不能把状态挂在 running/0 条上，要落回结束并留说明。"""
        out = resolver.scrape_files(scope="missing")
        assert out["total"] == 0
        st = resolver.scrape_state()
        assert not st["running"] and st["ended_at"]
        assert st["logs"] and "没有待刮削" in st["logs"][0]["name"]

    def test_scrape_bg_failure_recorded(self, library, monkeypatch):
        """后台线程崩了也要把原因写进 errors/logs，明细才有的看。"""
        def boom(*a, **k):
            raise RuntimeError("网络炸了")

        monkeypatch.setattr(resolver, "scrape_files", boom)
        resolver.scrape_async(scope="missing")
        deadline = time.time() + 5
        while time.time() < deadline:
            st = resolver.scrape_state()
            if not st["running"] and st["errors"]:
                break
            time.sleep(0.05)
        st = resolver.scrape_state()
        assert not st["running"]
        assert any("网络炸了" in (e.get("error") or "") for e in st["errors"])
        assert st["logs"] and not st["logs"][0]["ok"]


class TestScrapeOneSafety:
    """_scrape_one 绝不抛异常：一首歌刮削炸掉不能带崩整轮（database is locked 教训）。"""

    def test_exception_becomes_error_string(self, monkeypatch):
        from tunesync.app import scraper

        def boom(*a, **k):
            raise RuntimeError("写入失败")

        monkeypatch.setattr(scraper, "scrape", boom)
        err, info = resolver._scrape_one({"path": "/music/x.mp3", "title": "x", "artist": "y"})
        assert "写入失败" in err
        assert info["path"] == "/music/x.mp3"
        assert info["fields"] == []




