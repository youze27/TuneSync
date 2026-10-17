"""core：归一化、匹配键、JSON 路径、配置合并、事件总线。"""
import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from tunesync.app import core, scheduler


class TestNormalize:
    def test_strips_bracket_annotations(self):
        assert core.norm("晴天（Live）") == "晴天"
        assert core.norm("晴天【官方版】") == "晴天"
        assert core.norm("晴天 [Remastered]") == "晴天"

    def test_strips_feat_clause(self):
        assert core.norm("Lucky feat. Billie Eilish") == "lucky"

    def test_fullwidth_and_case(self):
        assert core.norm("ＡＢＣ Ｔｅｓｔ") == "abctest"

    def test_whitespace_and_punctuation(self):
        assert core.norm("  Hey,   You  ") == "heyyou"

    def test_empty_and_none(self):
        assert core.norm(None) == ""
        assert core.norm("") == ""


class TestMainArtist:
    @pytest.mark.parametrize("raw,expected", [
        ("周杰伦", "周杰伦"),
        ("周杰伦/其他歌手", "周杰伦"),
        ("A, B", "A"),
        ("A、B", "A"),
        ("A & B", "A"),
        ("", ""),
    ])
    def test_picks_lead_artist(self, raw, expected):
        assert core.main_artist(raw) == expected


class TestTrackKey:
    def test_key_is_title_pipe_artist(self):
        assert core.tkey("晴天", "周杰伦") == "晴天|周杰伦"

    def test_ignores_co_artist(self):
        assert core.tkey("晴天", "周杰伦/林俊杰") == core.tkey("晴天", "周杰伦")

    def test_ignores_annotation(self):
        assert core.tkey("晴天 (Live)", "周杰伦") == core.tkey("晴天", "周杰伦")


class TestJget:
    def test_dotted_path(self):
        assert core.jget({"data": {"list": [1, 2]}}, "data.list") == [1, 2]

    def test_array_index(self):
        assert core.jget({"a": [{"b": 7}]}, "a.0.b") == 7

    def test_missing_returns_none(self):
        assert core.jget({"a": 1}, "b.c") is None
        assert core.jget({"a": 1}, "") is None

    def test_bad_index_does_not_raise(self):
        assert core.jget({"a": [1]}, "a.9") is None


class TestSafeName:
    def test_strips_path_separators(self):
        assert core.safe_name("AC/DC") == "AC_DC"
        assert core.safe_name("a:b*c?d") == "a_b_c_d"

    def test_never_empty(self):
        assert core.safe_name("") == "unknown"
        assert core.safe_name("///") == "unknown"   # 全是分隔符，归一化后为空
        assert core.safe_name("...  ") == "unknown"

    def test_truncates(self):
        assert len(core.safe_name("x" * 300)) == 80


class TestConfigMerge:
    def test_settings_override_file(self, env):
        core.kv_set("settings", {"download": {"concurrency": 9}})
        assert core.merged_config()["download"]["concurrency"] == 9
        # 未覆盖的键仍在
        assert "min_duration_sec" in core.merged_config()["download"]

    def test_targets_have_own_endpoint(self, env):
        core.kv_set("targets", {"navidrome": {"url": "http://x:4533"}})
        assert core.merged_targets()["navidrome"]["url"] == "http://x:4533"

    def test_deep_merge_keeps_siblings(self, env):
        core.kv_set("settings", {"library": {"root": "/other"}})
        merged = core.merged_config()["library"]
        assert merged["root"] == "/other"
        assert "rescan_interval_min" in merged


class TestKv:
    def test_roundtrip(self, env):
        core.kv_set("k", {"中文": [1, 2]})
        assert core.kv_get("k") == {"中文": [1, 2]}

    def test_missing_returns_default(self, env):
        assert core.kv_get("nope", "fallback") == "fallback"


class TestEventHub:
    def test_publish_without_loop_reaches_subscriber(self):
        hub = core.EventHub()
        q = hub.subscribe()
        assert hub.publish({"type": "x"}) == 1
        assert q.get_nowait() == {"type": "x"}

    def test_unsubscribe_stops_delivery(self):
        hub = core.EventHub()
        q = hub.subscribe()
        hub.unsubscribe(q)
        assert hub.publish({"type": "x"}) == 0
        assert hub.listeners == 0

    def test_slow_consumer_drops_instead_of_blocking(self):
        hub = core.EventHub(maxsize=2)
        q = hub.subscribe()
        for i in range(5):
            hub.publish({"i": i})
        assert q.qsize() == 2
        assert q.get_nowait()["i"] == 3   # 丢最旧的，保留最新的

    def test_thread_safe_publish(self):
        import threading
        hub = core.EventHub(maxsize=1000)
        for _ in range(3):
            hub.subscribe()
        threads = [threading.Thread(target=lambda: [hub.publish({"n": i}) for i in range(50)])
                   for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert hub.listeners == 3


class TestCronParsing:
    def test_five_and_six_fields(self, env):
        assert scheduler.parse_cron("0 8 * * *") is not None
        assert scheduler.parse_cron("30 0 8 * * *") is not None

    def test_seven_normalizes_to_sunday(self, env):
        """Unix cron 里 7=周日，直接喂给 APScheduler 会 ValueError。"""
        assert str(scheduler.parse_cron("0 8 * * 7")) == str(scheduler.parse_cron("0 8 * * 0"))

    def test_seven_inside_list(self, env):
        assert scheduler.parse_cron("0 8 * * 7,1") is not None
        assert str(scheduler.parse_cron("0 8 * * 7,1")) == str(scheduler.parse_cron("0 8 * * 0,1"))

    def test_full_week_uses_six_not_seven(self, env):
        """APScheduler 上限是 6，整周要写 0-6 或 *，写 1-7 会报错。"""
        with pytest.raises(Exception):
            scheduler.parse_cron("0 8 * * 1-7")
        assert scheduler.parse_cron("0 8 * * *") is not None

    def test_zero_is_monday_not_sunday(self, env):
        """0=周一 是 APScheduler 的约定，不是 Unix cron 的 0=周日。"""
        import datetime
        monday = scheduler.parse_cron("0 8 * * 0").get_next_fire_time(None, datetime.datetime.now())
        assert monday.weekday() == 0
        sunday = scheduler.parse_cron("0 8 * * 6").get_next_fire_time(None, datetime.datetime.now())
        assert sunday.weekday() == 6

    @pytest.mark.parametrize("bad", ["不是cron", "0 8 * *", "0 8 * * 8", "99 8 * * *"])
    def test_invalid_rejected(self, env, bad):
        with pytest.raises(Exception):
            scheduler.parse_cron(bad)


class TestSeedSources:
    def _reseed(self):
        """清掉「已播种」标记，模拟一个全新的库。"""
        core.db().execute("DELETE FROM kv WHERE k='seeded_sources'")
        core.db().commit()

    def test_config_sources_seeded_once(self, env):
        self._reseed()
        expected = len(core.CONFIG.get("sources") or [])
        assert core.seed_sources() == expected
        assert core.db().execute("SELECT COUNT(*) n FROM source").fetchone()["n"] == expected
        assert core.seed_sources() == 0        # 再调一次不重复
        assert core.db().execute("SELECT COUNT(*) n FROM source").fetchone()["n"] == expected

    def test_seeded_source_has_usable_cfg(self, env):
        self._reseed()
        core.seed_sources()
        row = core.db().execute("SELECT cfg FROM source").fetchone()
        cfg = json.loads(row["cfg"])
        assert "{kw}" in cfg["search"] and "{id}" in cfg["url"]

    def test_does_not_resurrect_after_user_deletes_all(self, env):
        """用户删光音源后重启，示例音源不该自己回来。"""
        self._reseed()
        core.seed_sources()
        core.db().execute("DELETE FROM source")
        core.db().commit()
        core.init_db()                        # 相当于重启
        assert core.db().execute("SELECT COUNT(*) n FROM source").fetchone()["n"] == 0


class TestLegacyUpgrade:
    """老库升级。索引依赖 MIGRATIONS 新增的列，顺序错了容器直接起不来。"""

    def _make_legacy_db(self, data_dir):
        """造一个「本地音乐筛选改造之前」的库：local_file 没有 genre/style/year。"""
        raw = sqlite3.connect(str(Path(data_dir) / "legacy.db"))
        raw.executescript("""
        CREATE TABLE local_file(
          id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT UNIQUE, title TEXT,
          artist TEXT, norm_key TEXT, mtime REAL);
        CREATE TABLE fetch_task(
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, platforms TEXT,
          categories TEXT, count_per INTEGER DEFAULT 3, cron_expr TEXT,
          enabled INTEGER DEFAULT 1);
        """)
        raw.execute("INSERT INTO local_file(path,title,artist,norm_key,mtime)"
                    " VALUES('/m/a.mp3','晴天','周杰伦','k',1.0)")
        raw.commit()
        raw.close()

    def test_init_db_upgrades_legacy_library(self, env, data_dir):
        self._make_legacy_db(data_dir)
        core.DB_PATH = str(Path(data_dir) / "legacy.db")
        if hasattr(core._local, "conn"):
            core._local.conn.close()
            del core._local.conn

        core.init_db()          # 老版本这一步会报 no such column: genre

        cols = {r["name"] for r in core.db().execute("PRAGMA table_info(local_file)")}
        assert {"genre", "style", "year", "album", "cover", "scraped_at"} <= cols
        tcols = {r["name"] for r in core.db().execute("PRAGMA table_info(fetch_task)")}
        assert {"push_mode", "source_platforms", "custom_prompt",
                "scene_mode", "tracks_per"} <= tcols
        idx = {r["name"] for r in core.db().execute("PRAGMA index_list(local_file)")}
        assert {"idx_lf_genre", "idx_lf_year"} <= idx
        # 原有数据一行不少
        assert core.db().execute("SELECT COUNT(*) n FROM local_file").fetchone()["n"] == 1

    def test_init_db_is_idempotent_on_legacy(self, env, data_dir):
        self._make_legacy_db(data_dir)
        core.DB_PATH = str(Path(data_dir) / "legacy.db")
        if hasattr(core._local, "conn"):
            core._local.conn.close()
            del core._local.conn
        core.init_db()
        core.init_db()          # 第二次不能再报 duplicate column


class TestTestHarness:
    """夹具自身的行为——夹具写错地方，测试照样全绿，最难发现。"""

    def test_work_dir_stays_under_tests(self, data_dir, library, work_root):
        for d in (data_dir, library):
            assert work_root in Path(d).parents or Path(d).parent == work_root, \
                f"临时目录跑到了仓库里：{d}"

    def test_work_dir_is_not_relative(self, data_dir):
        assert Path(data_dir).is_absolute(), f"临时目录不是绝对路径：{data_dir}"


class TestHostAlias:
    """容器里 127.0.0.1 指向容器自己，不改写的话同机服务全都连不通。"""

    def test_bare_machine_keeps_loopback(self, monkeypatch):
        """裸机/开发机不能改写：那里的 127.0.0.1 本来就是对的。"""
        monkeypatch.setattr(core, "_in_container", False)
        core._host_alias = None
        assert core.host_alias() == ""
        assert core.localise_url("http://127.0.0.1:3000/search") == "http://127.0.0.1:3000/search"

    def test_container_rewrites_loopback(self, monkeypatch):
        monkeypatch.setattr(core, "_in_container", True)
        monkeypatch.setattr(core, "_detect_host", lambda: "host.docker.internal")
        core._host_alias = None
        assert core.localise_url("http://127.0.0.1:3000/search") == \
            "http://host.docker.internal:3000/search"

    def test_explicit_env_wins_even_on_bare_machine(self, monkeypatch):
        monkeypatch.setenv("TUNESYNC_HOST", "192.168.1.50")
        core._host_alias = None
        assert core.host_alias() == "192.168.1.50"
        assert core.localise_url("http://127.0.0.1:4533") == "http://192.168.1.50:4533"

    def test_remote_address_untouched(self, monkeypatch):
        """远端服务不能被改写——LLM 端点可能在另一台机器上。"""
        monkeypatch.setattr(core, "_in_container", True)
        monkeypatch.setattr(core, "_detect_host", lambda: "host.docker.internal")
        core._host_alias = None
        for u in ("http://10.0.0.7:3000/x", "https://music.example.com/api",
                  "http://192.168.1.100:4533"):
            assert core.localise_url(u) == u

    def test_port_and_path_preserved(self, monkeypatch):
        monkeypatch.setattr(core, "_in_container", True)
        monkeypatch.setattr(core, "_detect_host", lambda: "nas.local")
        core._host_alias = None
        assert core.localise_url("http://localhost:4533/rest/getMusicFolders") == \
            "http://nas.local:4533/rest/getMusicFolders"
        assert core.localise_url("http://127.0.0.1:3000/search?kw=a%20b") == \
            "http://nas.local:3000/search?kw=a%20b"

    def test_non_url_strings_untouched(self, monkeypatch):
        monkeypatch.setattr(core, "_in_container", True)
        monkeypatch.setattr(core, "_detect_host", lambda: "nas.local")
        core._host_alias = None
        for v in ("/music/Downloaded", "", "127.0.0.1", "[{category}] {name}", None, 123):
            assert core.localise_url(v) == v

    def test_config_tree_is_rewritten_not_just_top_level(self, monkeypatch):
        """LLM base_url、音源 cfg 模板、目标库 url 都要被改写，
        否则「音源一直失败」会一直查不出原因。"""
        monkeypatch.setattr(core, "_in_container", True)
        monkeypatch.setattr(core, "_detect_host", lambda: "nas.local")
        core._host_alias = None
        tree = {
            "llm": {"base_url": "http://127.0.0.1:8000"},
            "sources": [{"cfg": {"search": "http://127.0.0.1:3001/s?kw={kw}",
                                 "url": "http://127.0.0.1:3001/u?id={id}"}}],
            "targets": {"navidrome": {"url": "http://127.0.0.1:4533"},
                        "feiniu": {"api": {"base_url": "http://127.0.0.1:5666"}}},
            "library": {"root": "/music", "download_dir": "/music/Downloaded"},
        }
        out = core._apply_host(tree)
        assert out["llm"]["base_url"] == "http://nas.local:8000"
        assert out["sources"][0]["cfg"]["search"] == "http://nas.local:3001/s?kw={kw}"
        assert out["sources"][0]["cfg"]["url"] == "http://nas.local:3001/u?id={id}"
        assert out["targets"]["navidrome"]["url"] == "http://nas.local:4533"
        assert out["targets"]["feiniu"]["api"]["base_url"] == "http://nas.local:5666"
        # 目录是路径不是 URL，不能动
        assert out["library"]["root"] == "/music"

    def test_merged_config_reflects_rewrite(self, env, monkeypatch):
        monkeypatch.setattr(core, "_in_container", True)
        monkeypatch.setattr(core, "_detect_host", lambda: "nas.local")
        core._host_alias = None
        core.kv_set("settings", {"llm": {"base_url": "http://127.0.0.1:8000"}})
        assert core.merged_config()["llm"]["base_url"] == \
            "http://nas.local:8000"

    def test_already_rewritten_url_is_idempotent(self, monkeypatch):
        monkeypatch.setattr(core, "_in_container", True)
        monkeypatch.setattr(core, "_detect_host", lambda: "nas.local")
        core._host_alias = None
        once = core.localise_url("http://127.0.0.1:3000/a")
        assert core.localise_url(once) == once

    def test_merged_config_does_not_mutate_module_level_config(self, env, monkeypatch):
        """merged_config() 的顶层浅拷贝让 cfg["targets"] 就是 CONFIG["targets"]。
        若就地改写，第一个开改写的用例会永久污染模块级 CONFIG，
        后面所有用例都拿到宿主地址——这类污染不报错，只会悄悄错。"""
        before = core.CONFIG["targets"]["navidrome"]["url"]
        assert before.startswith("http://127.0.0.1")
        monkeypatch.setattr(core, "_in_container", True)
        monkeypatch.setattr(core, "_detect_host", lambda: "nas.local")
        core._host_alias = None
        got = core.merged_config()
        assert got["targets"]["navidrome"]["url"] == "http://nas.local:4533"
        # 关键断言：模块级配置没被动过
        assert core.CONFIG["targets"]["navidrome"]["url"] == before
        # 关掉改写后必须还是回环地址（_detect_host 的补丁也要一并撤掉）
        core._in_container = False
        monkeypatch.setattr(core, "_detect_host", lambda: "")
        core._host_alias = None
        assert core.merged_config()["targets"]["navidrome"]["url"] == before

    def test_llm_config_is_not_mutated(self, env, monkeypatch):
        before = core.CONFIG["llm"]["base_url"]
        monkeypatch.setattr(core, "_in_container", True)
        monkeypatch.setattr(core, "_detect_host", lambda: "nas.local")
        core._host_alias = None
        core.kv_set("settings", {"llm": {"base_url": "http://127.0.0.1:8000"}})
        assert core.merged_config()["llm"]["base_url"] == "http://nas.local:8000"
        assert core.CONFIG["llm"]["base_url"] == before

    def test_source_cfg_from_sqlite_is_rewritten(self, env, monkeypatch):
        """音源存在 SQLite 里，不在配置树中，改写必须发生在解析 cfg 时。"""
        monkeypatch.setattr(core, "_in_container", True)
        monkeypatch.setattr(core, "_detect_host", lambda: "nas.local")
        core._host_alias = None
        raw = json.dumps({
            "search": "http://127.0.0.1:3001/s?kw={kw}",
            "url": "http://127.0.0.1:3001/u?id={id}",
            "url_paths": ["data.url", "url"],
        }, ensure_ascii=False)
        cfg = core.source_cfg(raw)
        assert cfg["search"] == "http://nas.local:3001/s?kw={kw}"
        assert cfg["url"] == "http://nas.local:3001/u?id={id}"
        # 模板占位符与非 URL 字段不能被破坏
        assert "{kw}" in cfg["search"] and "{id}" in cfg["url"]
        assert cfg["url_paths"] == ["data.url", "url"]

    def test_source_cfg_survives_bad_json(self, env):
        for bad in (None, "", "{不是 json", "[]"):
            assert core.source_cfg(bad) == {} or core.source_cfg(bad) == []

    def test_stored_source_cfg_is_not_mutated(self, env, monkeypatch):
        """改写只发生在读取时，库里存的必须是用户原样输入的字符串——
        否则「测试连接」看到的地址和实际抓取用的地址会对不上。"""
        monkeypatch.setattr(core, "_in_container", True)
        monkeypatch.setattr(core, "_detect_host", lambda: "nas.local")
        core._host_alias = None
        raw = '{"search": "http://127.0.0.1:3001/s?kw={kw}"}'
        core.db().execute(
            "INSERT INTO source(name,priority,enabled,cfg) VALUES(?,?,?,?)",
            ("改写测试源", 1, 1, raw))
        core.db().commit()
        # 读的时候被改写
        row = core.db().execute("SELECT cfg FROM source WHERE name=?", ("改写测试源",)).fetchone()
        assert core.source_cfg(row["cfg"])["search"] == "http://nas.local:3001/s?kw={kw}"
        # 库里原样未动
        assert row["cfg"] == raw


class TestStaleRunRecovery:
    def test_running_runs_are_closed_on_startup(self, env):
        """容器被杀会让 run 永远卡在 running，UI 上像还在跑。"""
        conn = core.db()
        conn.execute("INSERT INTO fetch_run(task_id,started_at,status) VALUES(1,'x','running')")
        conn.execute("INSERT INTO run_playlist(run_id,platform,category,name,status)"
                     " VALUES(1,'llm','学习','歌单','processing')")
        conn.commit()
        assert core.recover_stale_runs() == 1
        run = conn.execute("SELECT * FROM fetch_run WHERE id=1").fetchone()
        assert run["status"] == "failed" and run["finished_at"]
        assert "异常退出" in json.loads(run["summary"])["fatal"]
        rp = conn.execute("SELECT * FROM run_playlist WHERE id=1").fetchone()
        assert rp["status"] == "failed" and "异常退出" in rp["error"]

    def test_finished_runs_untouched(self, env):
        conn = core.db()
        conn.execute("INSERT INTO fetch_run(task_id,started_at,status) VALUES(1,'x','success')")
        conn.commit()
        assert core.recover_stale_runs() == 0
        assert conn.execute("SELECT status FROM fetch_run").fetchone()["status"] == "success"


class TestSchemaMigration:
    def test_added_columns_present(self, env):
        cols = {r["name"] for r in core.db().execute("PRAGMA table_info(local_file)")}
        assert {"scanned_at", "size", "format"} <= cols

    def test_init_is_idempotent(self, env):
        core.init_db()
        core.init_db()
        n = core.db().execute("SELECT COUNT(*) n FROM source").fetchone()["n"]
        assert n == 0                            # env 已清空且打过标记，不该被重新灌入


class TestDbRetry:
    """SQLite 锁冲突重试：刮削/下载并发写时 database is locked 不再直接炸整轮。"""

    def test_success_passthrough(self):
        assert core.db_retry(lambda: 42) == 42

    def test_retries_on_locked_then_succeeds(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise sqlite3.OperationalError("database is locked")
            return "ok"

        assert core.db_retry(flaky, base=0) == "ok"
        assert calls["n"] == 3

    def test_non_locked_error_raises_immediately(self):
        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            raise sqlite3.OperationalError("no such table: x")

        with pytest.raises(sqlite3.OperationalError):
            core.db_retry(boom, base=0)
        assert calls["n"] == 1

    def test_gives_up_after_max_tries(self):
        def always_locked():
            raise sqlite3.OperationalError("database is locked")

        with pytest.raises(sqlite3.OperationalError):
            core.db_retry(always_locked, tries=3, base=0)
