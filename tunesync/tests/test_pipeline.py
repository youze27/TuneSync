"""pipeline：端到端编排。LLM 生成与解析全部打桩，只验证流水线自身的编排逻辑。"""
import hashlib
import json
import sqlite3
import threading
import time
from unittest import mock

import pytest

from tunesync.app import core, pipeline, recommender, resolver, scraper

TRACKS = [
    {"title": "本地有的歌", "artist": "甲", "album": "A"},
    {"title": "要下的歌", "artist": "乙", "album": "B"},
    {"title": "拿不到的歌", "artist": "丙", "album": "C"},
]
PLAYLISTS = [{"id": "P1", "name": "学习歌单", "cover": "", "tracks": TRACKS}]


@pytest.fixture
def task(env):
    conn = core.db()
    conn.execute(
        "INSERT INTO fetch_task(name,platforms,categories,count_per,cron_expr,enabled)"
        " VALUES(?,?,?,?,?,1)",
        ("每日推送", json.dumps([]), json.dumps(["study"]), 3, "0 8 * * *"))
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _resolve_stub(results):
    """按歌名返回预设解析结果。"""
    table = {t["title"]: t for t in TRACKS}
    def _f(title, artist="", album="", sid=None):
        r = dict(results[title])
        r["meta"] = {"title": title, "artist": artist, "album": album}
        r["track"] = table.get(title, {"title": title, "artist": artist})
        return r
    return _f


BASE_RESULTS = {
    "本地有的歌": {"status": "local", "path": "/music/甲 - 本地有的歌.mp3",
                  "source": "本地库", "error": None},
    "要下的歌": {"status": "downloaded", "path": "/music/乙 - 要下的歌.mp3",
                 "source": "主音源", "error": None},
    "拿不到的歌": {"status": "skipped", "path": None, "source": None, "error": "全部音源失败"},
}


@pytest.fixture
def run_pipeline(env, task):
    def _run(results=None, playlists=None, tracks=None, writers_cfg=None):
        # 显式关掉 config.yaml 里的默认目标库，避免测试连到真实地址
        cfg = {"navidrome": {"url": "", "enabled": False}, "feiniu": {"enabled": False}}
        cfg.update(writers_cfg or {})
        core.kv_set("targets", cfg)
        pls = playlists if playlists is not None else PLAYLISTS
        if tracks is not None:
            pls = [dict(p, tracks=tracks) for p in pls]
        with mock.patch.object(recommender, "generate_playlists", return_value=pls), \
             mock.patch.object(resolver, "resolve_track",
                               side_effect=_resolve_stub(results or BASE_RESULTS)), \
             mock.patch.object(scraper, "scrape",
                               return_value={"tags": True, "cover": True, "lrc": True}):
            return pipeline.run_task(task)
    return _run


class TestHappyPath:
    def test_records_every_stage(self, run_pipeline):
        run_id = run_pipeline()
        conn = core.db()
        run = conn.execute("SELECT * FROM fetch_run WHERE id=?", (run_id,)).fetchone()
        # 有 1 首 skipped → partial（降级继续），而不是 success
        assert run["status"] == "partial"
        s = json.loads(run["summary"])
        assert s["playlists"] == 1 and s["local"] == 1 and s["downloaded"] == 1 and s["skipped"] == 1

    def test_success_when_everything_resolved(self, run_pipeline):
        results = {k: dict(v) for k, v in BASE_RESULTS.items() if k != "拿不到的歌"}
        tracks = [t for t in TRACKS if t["title"] in results]
        run_id = run_pipeline(results=results, tracks=tracks)
        run = core.db().execute("SELECT * FROM fetch_run WHERE id=?", (run_id,)).fetchone()
        assert run["status"] == "success"

    def test_playlist_row_counted(self, run_pipeline):
        run_id = run_pipeline()
        rp = core.db().execute("SELECT * FROM run_playlist WHERE run_id=?", (run_id,)).fetchone()
        assert (rp["name"], rp["platform"], rp["category"]) == ("学习歌单", "llm", "学习")
        assert (rp["track_total"], rp["local_hit"], rp["downloaded"], rp["skipped"]) == (3, 1, 1, 1)
        assert rp["status"] == "resolved"

    def test_one_job_per_track_in_playlist_order(self, run_pipeline):
        run_id = run_pipeline()
        rp = core.db().execute("SELECT * FROM run_playlist WHERE run_id=?", (run_id,)).fetchone()
        jobs = core.db().execute(
            "SELECT * FROM download_job WHERE run_playlist_id=? ORDER BY id", (rp["id"],)).fetchall()
        assert [j["title"] for j in jobs] == [t["title"] for t in TRACKS]
        assert [j["status"] for j in jobs] == ["local", "done", "skipped"]
        # 失败重试（默认 retry=2）会在错误前加「重试 N 次仍失败」前缀
        assert jobs[2]["error"].endswith("全部音源失败")

    def test_downloaded_track_is_scraped(self, run_pipeline):
        run_pipeline()
        conn = core.db()
        job = conn.execute("SELECT * FROM download_job WHERE status='done'").fetchone()
        assert job["source"] == "主音源" and job["file_path"].endswith(".mp3")

    def test_playlist_dedup_recorded(self, run_pipeline):
        run_pipeline()
        row = core.db().execute("SELECT * FROM playlist").fetchone()
        assert row["platform"] == "llm"
        assert row["source_id"] == hashlib.md5("学习|学习歌单".encode("utf-8")).hexdigest()[:12]
        assert row["name"] == "学习歌单"      # LLM 歌单名已是平台风格，默认不再加前缀

    def test_custom_prompt_marks_category_as_custom(self, env):
        """带自定义提示词时，歌单归属记「自定义」，主题以提示词为准。"""
        core.kv_set("targets", {"navidrome": {"url": "", "enabled": False},
                                "feiniu": {"enabled": False}})
        core.kv_set("settings", {"preferences": {"categories": ["study"], "count_per": 3,
                                                 "custom_prompt": "深夜emo",
                                                 "push_mode": "llm"}})
        with mock.patch.object(recommender, "generate_playlists", return_value=PLAYLISTS) as gen, \
             mock.patch.object(resolver, "resolve_track",
                               side_effect=_resolve_stub(BASE_RESULTS)), \
             mock.patch.object(scraper, "scrape", return_value={}):
            pipeline.run_now(pipeline.now_prefs())
        gen.assert_called_once_with("学习", 3, prompt="深夜emo", tracks_per=None)
        rp = core.db().execute("SELECT * FROM run_playlist").fetchone()
        assert (rp["platform"], rp["category"]) == ("llm", "自定义")

    def test_running_flag_cleared_after(self, run_pipeline):
        run_pipeline()
        assert pipeline.is_running(1) is False


class TestFetchNow:
    """立即推送：不落 fetch_task、不依赖 cron，走的却是同一套流水线。"""

    @pytest.fixture
    def run_now(self, env):
        core.kv_set("targets", {"navidrome": {"url": "", "enabled": False},
                                 "feiniu": {"enabled": False}})
        core.kv_set("settings", {"preferences": {"categories": ["study"], "count_per": 3,
                                                 "push_mode": "llm"}})

        def _run(prefs=None):
            with mock.patch.object(recommender, "generate_playlists", return_value=PLAYLISTS), \
                 mock.patch.object(resolver, "resolve_track",
                                   side_effect=_resolve_stub(BASE_RESULTS)), \
                 mock.patch.object(scraper, "scrape",
                                   return_value={"tags": True, "cover": True, "lrc": True}):
                return pipeline.run_now(pipeline.now_prefs(prefs))
        return _run

    def test_runs_and_is_recorded(self, run_now):
        run_id = run_now()
        conn = core.db()
        run = conn.execute("SELECT * FROM fetch_run WHERE id=?", (run_id,)).fetchone()
        assert run["task_id"] is None          # 不挂在任何计划任务下
        assert run["task_name"] == "立即推送"
        assert run["status"] in ("success", "partial")
        assert run["finished_at"]

    def test_does_not_create_a_task_row(self, run_now):
        """塞进 fetch_task 会在计划任务列表里留垃圾。"""
        before = core.db().execute("SELECT COUNT(*) c FROM fetch_task").fetchone()["c"]
        run_now()
        after = core.db().execute("SELECT COUNT(*) c FROM fetch_task").fetchone()["c"]
        assert after == before == 0

    def test_uses_saved_preferences(self, run_now):
        run_now()
        rows = core.db().execute("SELECT DISTINCT platform, category FROM run_playlist").fetchall()
        assert [(r["platform"], r["category"]) for r in rows] == [("llm", "学习")]

    def test_body_prefs_take_effect(self, run_now):
        core.kv_set("settings", {"preferences": {"categories": ["douyin"], "count_per": 1}})
        run_now({"categories": ["study"], "count_per": 3})
        cats = {r["category"] for r in core.db().execute("SELECT category FROM run_playlist")}
        assert cats == {"学习"}     # 草稿覆盖了已保存的「抖音热歌」

    def test_guard_cleared_after_run(self, run_now):
        run_now()
        assert pipeline.now_running() is False

    def test_second_now_skipped_while_running(self, run_now, monkeypatch):
        """手动连点两次：第二次必须被挡下，不能并发起两轮。"""
        real = pipeline._run
        calls = []

        def _wrapped(task, run_id):
            calls.append(run_id)
            return real(task, run_id)

        with mock.patch.object(pipeline, "_run", side_effect=_wrapped):
            run_now()
        assert len(calls) == 1
        assert pipeline.now_running() is False

    def test_now_prefs_requires_categories(self, env):
        with pytest.raises(ValueError, match="场景"):
            pipeline.now_prefs({"categories": []})

    def test_now_prefs_shape_matches_task_row(self, env):
        """_run() 只认 fetch_task 行的字段，dict 必须同形。"""
        t = pipeline.now_prefs({"categories": ["douyin", "study"], "count_per": 7})
        assert json.loads(t["platforms"]) == []   # 历史遗留列，恒为空
        assert json.loads(t["categories"]) == ["douyin", "study"]
        assert t["count_per"] == 7 and t["id"] is None
        assert t["prompt"] == ""

    def test_now_prefs_prompt_precedence(self, env):
        """请求体的 prompt > 已保存的 custom_prompt > 场景。"""
        t = pipeline.now_prefs({"categories": ["douyin"], "prompt": "深夜emo"})
        assert t["prompt"] == "深夜emo"
        t = pipeline.now_prefs({"categories": ["douyin"], "custom_prompt": "夜跑"})
        assert t["prompt"] == "夜跑"
        t = pipeline.now_prefs({"categories": ["douyin"], "prompt": "A", "custom_prompt": "B"})
        assert t["prompt"] == "A"

    def test_task_and_now_guards_are_independent(self, env, task):
        """一个任务在跑时，仍应允许手动推送——它们本来就是两件事。"""
        pipeline._running.add(f"task-{task}")
        try:
            assert pipeline.is_running(task) is True
            assert pipeline.now_running() is False
        finally:
            pipeline._running.discard(f"task-{task}")

    def test_guard_is_held_before_the_thread_starts(self, env, monkeypatch):
        """占位必须同步完成。放到线程里的话，接口已经返回 200 而运行位还没占上，
        这中间的空窗里连点一次就能起第二轮——而且 fetch-now 的 409 守卫会漏掉。"""
        started = []
        monkeypatch.setattr(pipeline.threading, "Thread",
                            lambda **kw: type("T", (), {"start": lambda s: started.append(1)})())
        core.kv_set("settings", {"preferences": {"categories": ["douyin"], "count_per": 1}})
        assert pipeline.run_now_async() is True
        assert pipeline.now_running() is True     # 线程还没起就已经占上了
        with pytest.raises(RuntimeError):
            pipeline.run_now_async()
        assert started == [1]                     # 第二次没再起线程
        pipeline._release(pipeline.NOW_GUARD)

    def test_failed_validation_leaves_no_guard(self, env):
        """参数不合法时不能留下运行位，否则会永久卡在「正在运行」。"""
        core.kv_set("settings", {"preferences": {"categories": []}})
        with pytest.raises(ValueError):
            pipeline.run_now_async()
        assert pipeline.now_running() is False

    def test_run_async_refuses_when_already_running(self, env, task, monkeypatch):
        started = []
        monkeypatch.setattr(pipeline.threading, "Thread",
                            lambda **kw: type("T", (), {"start": lambda s: started.append(1)})())
        assert pipeline.run_async(task) is True
        assert pipeline.is_running(task) is True
        assert pipeline.run_async(task) is False    # 第二次不排队、不起线程
        assert started == [1]
        pipeline._release(f"task-{task}")

    def test_missing_task_releases_guard(self, env, monkeypatch):
        """任务行不存在时不能占住运行位，否则这个 id 会永久触发不了。"""
        started = []
        monkeypatch.setattr(pipeline.threading, "Thread",
                            lambda **kw: type("T", (), {"start": lambda s: started.append(1)})())
        assert pipeline.run_async(999999) is False
        assert pipeline.is_running(999999) is False
        assert started == []

    def test_db_failure_does_not_leak_guard(self, env, monkeypatch):
        """db() 抛异常时必须照样释放运行位。

        db() 曾经在 try 块外面，它一抛异常 finally 就不执行，运行位永久留在
        _running 里——界面表现是「有一轮一直在跑」，之后每次点立即推送都是 409。
        真实踩过：Windows 绑定挂载下 SQLite 打不开 WAL，后台线程每次都死在这行。
        """
        task = pipeline.now_prefs({"categories": ["douyin"]})

        def boom():
            raise sqlite3.OperationalError("unable to open database file")

        monkeypatch.setattr(pipeline, "db", boom)
        assert pipeline._acquire(pipeline.NOW_GUARD) is True
        with pytest.raises(sqlite3.OperationalError):
            pipeline.run_now(task, pre_acquired=True)
        assert pipeline.now_running() is False

    def test_db_failure_via_async_leaves_nothing_running(self, env, monkeypatch):
        """同上一条，但走真实的后台线程路径——接口当时已经返回 200 了。"""
        core.kv_set("settings", {"preferences": {"categories": ["douyin"], "count_per": 1}})
        done = threading.Event()

        def boom():
            raise sqlite3.OperationalError("unable to open database file")

        monkeypatch.setattr(pipeline, "db", boom)

        # done 由「线程释放运行位」置位：_release 是后台线程收尾的最后一步，
        # 它被调用即证明 db() 炸掉后 finally 照样执行、运行位已归还。
        real_release = pipeline._release

        def release_and_signal(guard):
            real_release(guard)
            done.set()

        monkeypatch.setattr(pipeline, "_release", release_and_signal)

        real_start = threading.Thread.start

        def start_and_wait(self):
            real_start(self)
            done.wait(5)      # 等线程收尾完才让 run_now_async 返回，实际变成同步

        monkeypatch.setattr(pipeline.threading.Thread, "start", start_and_wait)
        pipeline.run_now_async()
        assert done.wait(10)
        assert pipeline.now_running() is False

    def test_run_now_reentrant_is_blocked(self, env):
        """重入必须被挡：否则一次推送里再触发一次，会无限套娃。"""
        core.kv_set("targets", {"navidrome": {"url": "", "enabled": False},
                                 "feiniu": {"enabled": False}})
        core.kv_set("settings", {"preferences": {"categories": ["study"], "count_per": 1,
                                                 "push_mode": "llm"}})
        inner = []

        def reenter(*a, **kw):
            inner.append(pipeline.run_now(pipeline.now_prefs()))
            return PLAYLISTS

        with mock.patch.object(recommender, "generate_playlists", side_effect=reenter), \
             mock.patch.object(resolver, "resolve_track",
                               side_effect=_resolve_stub(BASE_RESULTS)), \
             mock.patch.object(scraper, "scrape", return_value={}):
            pipeline.run_now(pipeline.now_prefs())
        assert inner == [None]                  # 重入被挡，返回 None
        assert core.db().execute("SELECT COUNT(*) c FROM fetch_run").fetchone()["c"] == 1

    def test_scheduled_task_run_still_records_task_id(self, run_pipeline):
        run_id = run_pipeline()
        run = core.db().execute("SELECT task_id FROM fetch_run WHERE id=?", (run_id,)).fetchone()
        assert run["task_id"] is not None


class TestDegradation:
    def test_one_scene_failing_keeps_others(self, env, task):
        """单场景生成失败只影响该场景，其余场景继续 → partial。"""
        conn = core.db()
        conn.execute("UPDATE fetch_task SET categories=? WHERE id=?",
                     (json.dumps(["douyin", "study"]), task))
        conn.commit()
        core.kv_set("targets", {"navidrome": {"url": "", "enabled": False},
                                "feiniu": {"enabled": False}})

        def flaky(scene, count, prompt=None, tracks_per=None):
            if scene == "抖音热歌":
                raise RuntimeError("LLM 挂了")
            return PLAYLISTS

        results = {"要下的歌": {"status": "local", "path": "/music/乙 - 要下的歌.mp3",
                                "source": "本地库", "error": None}}
        with mock.patch.object(recommender, "generate_playlists", side_effect=flaky), \
             mock.patch.object(resolver, "resolve_track",
                               side_effect=_resolve_stub(results)), \
             mock.patch.object(scraper, "scrape", return_value={}):
            run_id = pipeline.run_task(task)

        run = core.db().execute("SELECT * FROM fetch_run WHERE id=?", (run_id,)).fetchone()
        assert run["status"] == "partial"
        assert json.loads(run["summary"])["playlists"] == 1
        assert any("LLM 挂了" in e for e in json.loads(run["summary"])["errors"])

    def test_all_scenes_failing_is_failed(self, env, task):
        core.kv_set("targets", {"navidrome": {"url": "", "enabled": False},
                                "feiniu": {"enabled": False}})
        with mock.patch.object(recommender, "generate_playlists",
                               side_effect=RuntimeError("挂了")):
            run_id = pipeline.run_task(task)
        run = core.db().execute("SELECT * FROM fetch_run WHERE id=?", (run_id,)).fetchone()
        assert run["status"] == "failed"
        assert json.loads(run["summary"])["attempted"] == 0

    def test_empty_tracks_marks_playlist_failed(self, env, task):
        """歌单拿到了但一首歌都没有：该歌单记 failed，run 降级为 partial。"""
        core.kv_set("targets", {"navidrome": {"url": "", "enabled": False},
                                "feiniu": {"enabled": False}})
        with mock.patch.object(recommender, "generate_playlists",
                               return_value=[{"name": "学习歌单", "tracks": [], "cover": ""}]):
            run_id = pipeline.run_task(task)
        rp = core.db().execute("SELECT * FROM run_playlist WHERE run_id=?", (run_id,)).fetchone()
        assert rp["status"] == "failed" and "该歌单没有曲目" in rp["error"]
        # 歌单拿到了，只是曲目为空：算跑过了一部分，不是完全失败
        run = core.db().execute("SELECT * FROM fetch_run WHERE id=?", (run_id,)).fetchone()
        assert run["status"] == "partial"

    def test_resolve_exception_marks_track_skipped(self, run_pipeline):
        results = dict(BASE_RESULTS)
        results["要下的歌"] = {"status": "skipped", "path": None, "source": None,
                               "error": "解析异常：boom"}
        run_pipeline(results=results)
        job = core.db().execute("SELECT * FROM download_job WHERE title='要下的歌'").fetchone()
        assert job["status"] == "skipped" and "boom" in job["error"]

    def test_queue_wait_not_counted_as_timeout(self, env, task):
        """排队等待不计入单首超时：并发 1 时第 2 首排队 1.5s 不能被 1s 超时线误杀。

        回归推送下载「大批歌被判下载超时」的 bug——超时起点曾是提交时刻，
        一张歌单几十首排队时，后面的歌还没轮到执行就被批量记超时跳过。
        """
        core.kv_set("targets", {"navidrome": {"url": "", "enabled": False},
                                "feiniu": {"enabled": False}})
        core.kv_set("download", {"concurrency": 1, "track_timeout": 1, "stall_timeout": 600,
                                 "retry": 0})

        tracks = [{"title": f"排队歌{i}", "artist": "甲", "album": "A"} for i in range(2)]
        pls = [{"id": "P1", "name": "排队歌单", "cover": "", "tracks": tracks}]

        def slow_resolve(title, artist="", album="", sid=None):
            time.sleep(1.5)
            return {"status": "downloaded", "path": f"/music/{title}.mp3",
                    "source": "主音源", "error": None,
                    "meta": {"title": title, "artist": artist, "album": album}}

        with mock.patch.object(recommender, "generate_playlists", return_value=pls), \
             mock.patch.object(resolver, "resolve_track", side_effect=slow_resolve), \
             mock.patch.object(scraper, "scrape",
                               return_value={"tags": True, "cover": True, "lrc": True}):
            run_id = pipeline.run_task(task)

        rp = core.db().execute("SELECT * FROM run_playlist WHERE run_id=?", (run_id,)).fetchone()
        assert rp["downloaded"] == 2 and rp["skipped"] == 0

    def test_missing_task_produces_no_run(self, env):
        assert pipeline.run_task(999) is None
        assert core.db().execute("SELECT COUNT(*) c FROM fetch_run").fetchone()["c"] == 0


class TestSyncTargets:
    def test_navidrome_receives_ordered_tracks(self, run_pipeline):
        got = {}

        class FakeNavidrome:
            def __init__(self, cfg):
                self.name = "navidrome"

            def start_scan(self):
                got["scan"] = True

            def wait_scan(self):
                got["waited"] = True

            def upsert(self, name, tracks, files):
                got["name"] = name
                got["tracks"] = tracks
                got["files"] = files
                return "navidrome: ok"

        with mock.patch("tunesync.app.writers.NavidromeWriter", FakeNavidrome):
            run_pipeline(writers_cfg={"navidrome": {"url": "http://nd:4533", "enabled": True}})
        assert got["scan"] is True and got["waited"] is True
        assert got["tracks"] == [("本地有的歌", "甲"), ("要下的歌", "乙")]   # skipped 被剔除
        assert got["files"] == ["/music/甲 - 本地有的歌.mp3", "/music/乙 - 要下的歌.mp3"]

    def test_writer_failure_recorded_per_playlist(self, run_pipeline):
        class Boom:
            name = "feiniu"

            def __init__(self, cfg):
                self.name = "feiniu"

            def upsert(self, *a, **kw):
                raise RuntimeError("写入失败")

        with mock.patch("tunesync.app.writers.FeiniuWriter", Boom):
            run_pipeline(writers_cfg={"feiniu": {"enabled": True,
                                                 "db_path": "/feiniu-db/music.db"}})
        rp = core.db().execute("SELECT * FROM run_playlist").fetchone()
        assert rp["status"].startswith("sync-failed")

    def test_no_targets_configured_still_succeeds(self, run_pipeline):
        results = {k: dict(v) for k, v in BASE_RESULTS.items() if k != "拿不到的歌"}
        tracks = [t for t in TRACKS if t["title"] in results]
        run_id = run_pipeline(results=results, tracks=tracks, writers_cfg={})
        assert core.db().execute("SELECT status FROM fetch_run WHERE id=?",
                                 (run_id,)).fetchone()["status"] == "success"


class TestConcurrencyGuard:
    def test_second_trigger_skipped_while_running(self, env, task):
        core.kv_set("targets", {"navidrome": {"url": "", "enabled": False},
                                "feiniu": {"enabled": False}})
        only_two = {"本地有的歌": BASE_RESULTS["本地有的歌"],
                    "要下的歌": BASE_RESULTS["要下的歌"]}
        with mock.patch.object(recommender, "generate_playlists", return_value=PLAYLISTS) as gen, \
             mock.patch.object(resolver, "resolve_track",
                               side_effect=_resolve_stub(only_two)), \
             mock.patch.object(scraper, "scrape", return_value={}):
            def slow(*a, **kw):
                pipeline.run_task(task)      # 重入
                return PLAYLISTS
            gen.side_effect = slow
            pipeline.run_task(task)
        runs = core.db().execute("SELECT COUNT(*) c FROM fetch_run").fetchone()["c"]
        assert runs == 1, "同一任务的并发触发必须被挡掉"


class TestPushQueue:
    """推送队列：不同任务之间必须串行，不能同时占着写入目标库。"""

    def test_two_tasks_run_one_after_another(self, env):
        """两个不同任务同时触发：第二个必须排队，且第一个跑完后自动接上。"""
        core.kv_set("targets", {"navidrome": {"url": "", "enabled": False},
                                "feiniu": {"enabled": False}})
        conn = core.db()
        ids = []
        for n in (1, 2):
            conn.execute(
                "INSERT INTO fetch_task(name,platforms,categories,count_per,cron_expr,enabled)"
                " VALUES(?,?,?,?,?,1)", (f"任务{n}", json.dumps([]), json.dumps(["study"]), 2, "0 8 * * *"))
            conn.commit()
            ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

        entered_a = threading.Event()
        entered_b = threading.Event()
        a_finished = threading.Event()
        # b 进执行体时如果 a 还没跑完，说明两者真的并行了
        overlap = threading.Event()
        hold = threading.Event()

        real_run_body = pipeline._run_body

        def slow_body(task, task_id, task_name):
            if task_id == ids[0]:
                entered_a.set()
                hold.wait(5)
                try:
                    return real_run_body(task, task_id, task_name)
                finally:
                    a_finished.set()
            # b 能走到这里，说明它已经抢到了工位
            if not a_finished.is_set():
                overlap.set()
            entered_b.set()
            return real_run_body(task, task_id, task_name)

        with mock.patch.object(pipeline, "_run_body", slow_body):
            ta = threading.Thread(target=pipeline.run_task, args=(ids[0],), daemon=True)
            tb = threading.Thread(target=pipeline.run_task, args=(ids[1],), daemon=True)
            ta.start()
            assert entered_a.wait(5), "第一个任务没进入执行体"
            tb.start()
            # b 此时只能在队列里等着。等它真的排进队再断言，中间有个调度空窗。
            q = {}
            for _ in range(100):
                q = pipeline.queue_state()
                if q["depth"] == 1:
                    break
                time.sleep(0.02)
            assert q["depth"] == 1, f"第二个任务应当排队，实际队列={q}"
            assert q["waiting"][0]["guard"] == f"task-{ids[1]}"
            assert q["active"]["guard"] == f"task-{ids[0]}"
            assert pipeline.is_running(ids[1]) is True    # 排队中也算「在跑」
            hold.set()
            ta.join(10)
            tb.join(10)

        assert entered_b.is_set(), "第一个跑完后第二个必须自动接上"
        assert not overlap.is_set(), "两个任务并行执行了，写入目标库会互相踩"
        assert pipeline.queue_state() == {"active": None, "waiting": [], "depth": 0}
        # 两轮都真的落库了
        assert core.db().execute("SELECT COUNT(*) c FROM fetch_run").fetchone()["c"] == 2

    def test_queue_slot_released_when_body_raises(self, env):
        """执行体抛异常时工位必须归还，否则后面的推送永远排不上。"""
        with mock.patch.object(pipeline, "_run_body",
                               side_effect=RuntimeError("boom")):
            with mock.patch.object(core, "kv_set"), \
                 mock.patch.object(recommender, "generate_playlists", return_value=PLAYLISTS):
                with pytest.raises(RuntimeError):
                    pipeline.run_now(pipeline.now_prefs({"categories": ["study"], "count_per": 1}))
        assert pipeline.queue_state()["active"] is None
        assert pipeline.now_running() is False

    def test_enter_slot_accounting_failure_releases_slot(self, env, monkeypatch):
        """_enter_slot 拿到工位后记账失败，必须自己还回去，不能指望调用方的 finally。"""
        # 只在 action == "start" 上炸：这条是拿到工位之后才发的，才能命中
        # _enter_slot 里 acquire() 之后的 except 分支。早于 acquire 的那条
        # （action == "wait"）抛了的话，走的是另一条路径，测不到这里要的东西。
        monkeypatch.setattr(pipeline, "emit",
                            lambda msg: (_ for _ in ()).throw(RuntimeError("emit 炸了"))
                            if msg.get("type") == "queue" and msg.get("action") == "start"
                            else None)
        with pytest.raises(RuntimeError):
            pipeline._enter_slot("task-x", "任务 X")
        # 工位已归还：另一个任务能立刻拿到
        assert pipeline._slot.acquire(blocking=False) is True
        pipeline._slot.release()
        with pipeline._q_lock:
            assert pipeline._waiting == []
        assert pipeline.queue_state()["active"] is None   # 残影也不能留


class TestImportPlaylist:
    """平台歌单导入：曲目来自 platforms.detail，下载与推送复用主管线。"""

    _NO_WRITERS = {"navidrome": {"url": "", "enabled": False}, "feiniu": {"enabled": False}}

    def _detail(self, tracks=None):
        return {"platform": "wy", "id": "42", "name": "平台热歌", "cover": "http://c/x.jpg",
                "count": 3, "tracks": TRACKS if tracks is None else tracks}

    def _run_import(self, detail=None, results=None, writers_cfg=None):
        cfg = dict(self._NO_WRITERS)
        cfg.update(writers_cfg or {})
        core.kv_set("targets", cfg)
        guard = "import:wy:42"
        pipeline._acquire(guard)
        try:
            with mock.patch.object(pipeline.platforms, "detail",
                                   return_value=detail or self._detail()), \
                 mock.patch.object(resolver, "resolve_track",
                                   side_effect=_resolve_stub(results or BASE_RESULTS)), \
                 mock.patch.object(scraper, "scrape",
                                   return_value={"tags": True, "cover": True, "lrc": True}):
                return pipeline._import_execute("wy", "42", guard)
        finally:
            pipeline._release(guard)

    def test_import_records_playlist_and_tracks(self, env):
        run_id = self._run_import()
        summary = json.loads(core.db().execute(
            "SELECT summary FROM fetch_run WHERE id=?", (run_id,)).fetchone()["summary"])
        assert summary["playlists"] == 1 and summary["attempted"] == 1
        assert summary["local"] == 1 and summary["downloaded"] == 1 and summary["skipped"] == 1
        rp = core.db().execute(
            "SELECT * FROM run_playlist WHERE run_id=?", (run_id,)).fetchone()
        assert rp["platform"] == "wy" and rp["category"] == "平台歌单"
        assert rp["name"] == "平台热歌"
        assert rp["source_id"] == hashlib.md5(b"wy|42").hexdigest()[:12]
        # 下载任务按歌单顺序落库
        jobs = list(core.db().execute(
            "SELECT title FROM download_job WHERE run_playlist_id=? ORDER BY id", (rp["id"],)))
        assert [j["title"] for j in jobs] == [t["title"] for t in TRACKS]

    def test_run_renamed_to_playlist_title(self, env):
        run_id = self._run_import()
        row = core.db().execute(
            "SELECT task_name FROM fetch_run WHERE id=?", (run_id,)).fetchone()
        assert "导入：平台热歌" in row["task_name"]

    def test_reimport_is_idempotent(self, env):
        self._run_import()
        self._run_import()
        n = core.db().execute(
            "SELECT COUNT(*) c FROM playlist WHERE platform='wy'").fetchone()["c"]
        assert n == 1, "同一张平台歌单重导是更新而不是膨胀"

    def test_empty_playlist_marks_run_failed(self, env):
        run_id = self._run_import(detail=self._detail(tracks=[]))
        row = core.db().execute(
            "SELECT status FROM fetch_run WHERE id=?", (run_id,)).fetchone()
        assert row["status"] == "failed"
        n = core.db().execute("SELECT COUNT(*) c FROM run_playlist").fetchone()["c"]
        assert n == 0, "拉不到曲目时不应建歌单占位"

    def test_import_guard_blocks_double_run(self, env):
        guard = "import:wy:1"
        assert pipeline._acquire(guard)
        try:
            with pytest.raises(RuntimeError, match="正在运行"):
                pipeline.import_playlist_async("wy", "1")
        finally:
            pipeline._release(guard)


class TestPushMode:
    """三档推送方式：只有「取歌单」这一步不同，后面的下载刮削写入完全同构。"""

    _NO_WRITERS = {"navidrome": {"url": "", "enabled": False}, "feiniu": {"enabled": False}}

    # platform 是 _collect_candidates 聚合时注入的（dict(item, platform=plat)），
    # 手工打桩 square 返回值时必须带上，否则 _load_source_playlist 取详情会 KeyError。
    CANDIDATES = [
        {"id": "9", "name": "深夜emo", "cover": "", "count": 30, "play": 900,
         "creator": "甲", "platform": "wy"},
        {"id": "8", "name": "学习纯音", "cover": "", "count": 30, "play": 800,
         "creator": "乙", "platform": "wy"},
        {"id": "7", "name": "健身燃脂", "cover": "", "count": 30, "play": 700,
         "creator": "丙", "platform": "wy"},
    ]

    def _set_mode(self, task, mode, plats=("all",)):
        core.db().execute("UPDATE fetch_task SET push_mode=?, source_platforms=? WHERE id=?",
                          (mode, json.dumps(list(plats)), task))
        core.db().commit()

    def _run(self, task, picked=None, square_error=None):
        core.kv_set("targets", self._NO_WRITERS)
        by_id = {c["id"]: c for c in self.CANDIDATES}

        def detail(plat, pid, **kw):
            # 按请求的 id 回歌单名，这样「选中哪张」能在落库结果上看出来
            c = by_id.get(str(pid), {"name": "深夜emo"})
            return {"platform": plat, "id": str(pid), "name": c["name"], "cover": "",
                    "count": 3, "tracks": TRACKS}

        with mock.patch.object(pipeline.platforms, "square",
                               side_effect=square_error or (lambda *a, **k: {"lists": self.CANDIDATES})), \
             mock.patch.object(pipeline.platforms, "detail", side_effect=detail), \
             mock.patch.object(recommender, "select_playlists",
                               return_value=(picked if picked is not None else self.CANDIDATES[:1])), \
             mock.patch.object(resolver, "resolve_track",
                               side_effect=_resolve_stub(BASE_RESULTS)), \
             mock.patch.object(scraper, "scrape",
                               return_value={"tags": True, "cover": True, "lrc": True}):
            return pipeline.run_task(task)

    def test_llm_mode_never_touches_platform_square(self, env, task):
        """方式一：歌单完全由 LLM 生成，一次平台请求都不该发出去。"""
        core.kv_set("targets", self._NO_WRITERS)
        with mock.patch.object(pipeline.platforms, "square") as sq, \
             mock.patch.object(recommender, "generate_playlists",
                               return_value=[{"name": "学习歌单", "cover": "", "tracks": TRACKS}]), \
             mock.patch.object(resolver, "resolve_track",
                               side_effect=_resolve_stub(BASE_RESULTS)), \
             mock.patch.object(scraper, "scrape", return_value={}):
            run_id = pipeline.run_task(task)
        sq.assert_not_called()
        rp = core.db().execute("SELECT * FROM run_playlist WHERE run_id=?", (run_id,)).fetchone()
        assert rp["platform"] == "llm"
        # LLM 歌单没有平台 id，幂等键取 md5(场景|歌单名)
        assert rp["source_id"] == hashlib.md5("学习|学习歌单".encode()).hexdigest()[:12]

    def test_source_raw_takes_hot_list_without_llm(self, env, task):
        """方式三：直接取候选前 N 张，全程不过 LLM。"""
        self._set_mode(task, "source_raw", ["wy"])
        with mock.patch.object(recommender, "select_playlists") as sel:
            run_id = self._run(task)
        sel.assert_not_called()
        rp = core.db().execute("SELECT * FROM run_playlist WHERE run_id=?", (run_id,)).fetchone()
        assert rp["platform"] == "wy"
        assert rp["source_id"] == hashlib.md5("wy|9".encode()).hexdigest()[:12]

    def test_source_llm_lets_model_pick(self, env, task):
        """方式二：候选先过一遍 LLM，落库的是模型选中的那张。"""
        self._set_mode(task, "source_llm", ["wy"])
        run_id = self._run(task, picked=[self.CANDIDATES[1]])
        rp = core.db().execute("SELECT * FROM run_playlist WHERE run_id=?", (run_id,)).fetchone()
        # 落库的是模型选中的那张：名字跟着 detail 走，幂等键是 wy|8
        assert rp["name"] == "学习纯音"
        assert rp["source_id"] == hashlib.md5("wy|8".encode()).hexdigest()[:12]

    def test_one_platform_failing_keeps_others(self, env, task):
        """单平台拉不到候选只记 warning，另一个平台的候选照常推送。"""
        self._set_mode(task, "source_raw", ["wy", "tx"])

        def square(plat, **kw):
            if plat == "wy":
                raise RuntimeError("网易云 503")
            # 单平台 square 的返回项本来不带 platform（只有聚合 all 才带，
            # 由 _collect_candidates 用外层 plat 补），这里跟着请求的平台走
            return {"lists": [dict(c, platform=plat) for c in self.CANDIDATES]}

        core.kv_set("targets", self._NO_WRITERS)
        with mock.patch.object(pipeline.platforms, "square", side_effect=square), \
             mock.patch.object(pipeline.platforms, "detail", return_value={
                 "platform": "tx", "id": "9", "name": "深夜emo", "cover": "",
                 # 只给前两首（全都能解析），第三首在 BASE_RESULTS 里是 skipped，
                 # 会往 summary.errors 里塞一条，把 status 拖成 partial，测的就不是本意了
                 "count": 2, "tracks": TRACKS[:2]}), \
             mock.patch.object(resolver, "resolve_track",
                               side_effect=_resolve_stub(BASE_RESULTS)), \
             mock.patch.object(scraper, "scrape", return_value={}):
            run_id = pipeline.run_task(task)
        run = core.db().execute("SELECT * FROM fetch_run WHERE id=?", (run_id,)).fetchone()
        assert run["status"] == "success"
        rp = core.db().execute("SELECT * FROM run_playlist WHERE run_id=?", (run_id,)).fetchone()
        assert rp["platform"] == "tx"

    def test_all_platforms_failed_marks_run_failed(self, env, task):
        self._set_mode(task, "source_raw", ["wy"])
        run_id = self._run(task, square_error=RuntimeError("全部平台挂了"))
        run = core.db().execute("SELECT * FROM fetch_run WHERE id=?", (run_id,)).fetchone()
        assert run["status"] == "failed"
        assert any("候选歌单" in e for e in json.loads(run["summary"])["errors"])


class TestPushModeNormalisation:
    def test_invalid_mode_falls_back(self):
        assert pipeline.normalise_push_mode("瞎写的") == "llm"
        assert pipeline.normalise_push_mode(None) == "llm"
        assert pipeline.normalise_push_mode(" source_raw ") == "source_raw"

    def test_invalid_scene_mode_falls_back(self):
        assert pipeline.normalise_scene_mode("瞎写的") == "separate"
        assert pipeline.normalise_scene_mode(None) == "separate"
        assert pipeline.normalise_scene_mode(" merge ") == "merge"

    def test_tracks_per_read_from_row(self):
        assert pipeline.tracks_per_of({"tracks_per": 50}) == 50
        assert pipeline.tracks_per_of({"tracks_per": 0}) == 0
        assert pipeline.tracks_per_of({"tracks_per": "坏值"}) == 0
        assert pipeline.tracks_per_of({}) == 0


class TestSceneRounds:
    """场景怎么摊成几轮，只影响「生成主题」和「跑几遍」。"""

    def test_separate_runs_one_round_per_scene(self):
        rounds = pipeline._scene_rounds(["douyin", "study"], "", "separate")
        assert [(r[1], r[2]) for r in rounds] == [("抖音热歌", "抖音热歌"),
                                                   ("学习", "学习")]

    def test_merge_joins_scenes_into_one_round(self):
        rounds = pipeline._scene_rounds(["douyin", "study"], "", "merge")
        assert len(rounds) == 1
        assert rounds[0][1] == "抖音热歌、学习"      # 模型拿到的是综合主题
        assert rounds[0][2] == "综合"                # 歌单归属记「综合」

    def test_merge_with_single_scene_is_unchanged(self):
        """只勾一个场景时合并与单独没有区别，不该把主题改名。"""
        rounds = pipeline._scene_rounds(["study"], "", "merge")
        assert len(rounds) == 1 and rounds[0][1] == "学习"

    def test_custom_prompt_wins_over_scene_name(self):
        rounds = pipeline._scene_rounds(["study"], "深夜emo", "merge")
        assert rounds[0][2] == "自定义"

    def test_no_categories_yields_no_rounds(self):
        assert pipeline._scene_rounds([], "", "merge") == []

    def test_merge_makes_one_llm_call(self, env, task):
        core.db().execute("UPDATE fetch_task SET categories=?, scene_mode='merge'"
                          " WHERE id=?",
                          (json.dumps(["douyin", "study"]), task))
        core.db().commit()
        core.kv_set("targets", {"navidrome": {"url": "", "enabled": False},
                                "feiniu": {"enabled": False}})
        with mock.patch.object(recommender, "generate_playlists",
                               return_value=PLAYLISTS) as gen, \
             mock.patch.object(resolver, "resolve_track",
                               side_effect=_resolve_stub(BASE_RESULTS)), \
             mock.patch.object(scraper, "scrape", return_value={}):
            pipeline.run_task(task)
        gen.assert_called_once()
        assert gen.call_args.args[0] == "抖音热歌、学习"
        rp = core.db().execute("SELECT * FROM run_playlist").fetchone()
        assert rp["category"] == "综合"

    def test_tracks_per_reaches_generator(self, env, task):
        core.db().execute("UPDATE fetch_task SET tracks_per=50 WHERE id=?", (task,))
        core.db().commit()
        core.kv_set("targets", {"navidrome": {"url": "", "enabled": False},
                                "feiniu": {"enabled": False}})
        with mock.patch.object(recommender, "generate_playlists",
                               return_value=PLAYLISTS) as gen, \
             mock.patch.object(resolver, "resolve_track",
                               side_effect=_resolve_stub(BASE_RESULTS)), \
             mock.patch.object(scraper, "scrape", return_value={}):
            pipeline.run_task(task)
        assert gen.call_args.kwargs["tracks_per"] == 50

    def test_zero_tracks_per_defers_to_global_config(self, env, task):
        """0 = 沿用 llm.tracks_per_playlist，不能把 0 当成「每张 0 首」。"""
        core.kv_set("targets", {"navidrome": {"url": "", "enabled": False},
                                "feiniu": {"enabled": False}})
        with mock.patch.object(recommender, "generate_playlists",
                               return_value=PLAYLISTS) as gen, \
             mock.patch.object(resolver, "resolve_track",
                               side_effect=_resolve_stub(BASE_RESULTS)), \
             mock.patch.object(scraper, "scrape", return_value={}):
            pipeline.run_task(task)
        assert gen.call_args.kwargs["tracks_per"] is None


class TestPushModeSourcePlatforms:
    def test_source_platforms_rules(self):
        assert pipeline.normalise_source_platforms([]) == ["all"]
        assert pipeline.normalise_source_platforms(["wy", "不存在"]) == ["wy"]
        assert pipeline.normalise_source_platforms("wy, kw") == ["wy", "kw"]

    def test_source_platforms_read_from_row(self):
        assert pipeline.source_platforms_of({"source_platforms": '["tx"]'}) == ["tx"]
        assert pipeline.source_platforms_of({"source_platforms": ["mg"]}) == ["mg"]
        assert pipeline.source_platforms_of({"source_platforms": "坏 JSON"}) == ["all"]
        assert pipeline.source_platforms_of({}) == ["all"]


class TestResolveRetry:
    """下载失败自动重试：skipped 时整链重试，最多 retry+1 次（默认共 3 次）。"""

    def _ctx(self):
        return {"run_id": 1, "rpid": 1}

    def _set_retry(self, n):
        cur = core.kv_get("settings") or {}
        cur["download"] = {**cur.get("download", {}), "retry": n}
        core.kv_set("settings", cur)

    def _skip_result(self, err):
        return {"status": "skipped", "path": None, "source": None,
                "error": err, "meta": {}}

    def test_retries_until_exhausted(self, monkeypatch):
        calls = {"n": 0}

        def always_skip(*a, **k):
            calls["n"] += 1
            return self._skip_result("无音源")

        monkeypatch.setattr(pipeline.resolver, "resolve_track", always_skip)
        monkeypatch.setattr(pipeline.time, "sleep", lambda s: None)
        self._set_retry(2)
        r = pipeline._resolve_one({"title": "t", "artist": "a"}, self._ctx(), 0)
        assert calls["n"] == 3                      # 首次 + 重试 2 次
        assert r["status"] == "skipped"
        assert "重试 3 次仍失败" in r["error"]

    def test_success_on_second_attempt_stops(self, monkeypatch):
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                return self._skip_result("限流")
            return {"status": "downloaded", "path": "/music/x.mp3",
                    "source": "gd", "error": None, "meta": {}}

        monkeypatch.setattr(pipeline.resolver, "resolve_track", flaky)
        monkeypatch.setattr(pipeline.time, "sleep", lambda s: None)
        monkeypatch.setattr(pipeline.scraper, "scrape",
                            lambda *a, **k: {"meta": {}, "hits": {}, "logs": []})
        monkeypatch.setattr(pipeline.resolver, "write_scrape_tags", lambda *a, **k: None)
        self._set_retry(2)
        r = pipeline._resolve_one({"title": "t", "artist": "a"}, self._ctx(), 0)
        assert calls["n"] == 2
        assert r["status"] == "downloaded"

    def test_retry_zero_disables(self, monkeypatch):
        calls = {"n": 0}

        def always_skip(*a, **k):
            calls["n"] += 1
            return self._skip_result("无")

        monkeypatch.setattr(pipeline.resolver, "resolve_track", always_skip)
        self._set_retry(0)
        r = pipeline._resolve_one({"title": "t", "artist": "a"}, self._ctx(), 0)
        assert calls["n"] == 1
        assert "重试" not in (r["error"] or "")
