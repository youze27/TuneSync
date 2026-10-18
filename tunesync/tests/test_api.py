"""REST API：契约、校验、错误码、状态流转。"""
import json
import os
import time

import pytest
from fastapi.testclient import TestClient
from unittest import mock

from tunesync.app import api, auth, core, pipeline, platforms, recommender, resolver, writers
from tunesync.app.main import app


@pytest.fixture
def no_auth(env, settings):
    """默认关掉登录校验，让其余用例直接打业务接口。

    auth.enabled 的出厂默认是开（口令部署后首次设置），TestAuth 里用 locked
    夹具显式打开、并单独验证默认行为。
    """
    settings(auth={"enabled": False, "proxy_header": ""})


@pytest.fixture
def client(env, no_auth):
    with TestClient(app) as c:
        yield c


def _task_payload(**kw):
    base = {"name": "每日推送", "categories": ["study"],
            "count_per": 3, "cron_expr": "0 8 * * *", "enabled": True}
    base.update(kw)
    return base


class TestMeta:
    def test_health(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200 and r.json()["status"] == "ok"

    def test_openapi_available(self, client):
        assert client.get("/openapi.json").status_code == 200

    def test_unknown_api_path_is_404(self, client):
        r = client.get("/api/不存在的接口")
        assert r.status_code == 404
        assert "detail" in r.json()

    def test_api_path_is_not_swallowed_by_spa_fallback(self, client):
        """前端已构建时 SPA 回退会接管一切路径，接口写错不能被返回 index.html。"""
        import tunesync.app.main as m
        if not os.path.isdir(m.STATIC_DIR):
            pytest.skip("前端未构建，跳过 SPA 回退相关断言")
        r = client.get("/api/不存在的接口")
        assert r.status_code == 404
        assert "text/html" not in r.headers.get("content-type", "")
        assert "未知接口 /api/不存在的接口" in r.json()["detail"]


class TestCategories:
    def test_categories_shape(self, client):
        cats = client.get("/api/categories").json()
        assert len(cats) == 49                     # 48 个模式 + 抖音热歌
        codes = {c["code"] for c in cats}
        assert "douyin" in codes and "guofeng" in codes
        assert all({"code", "name", "group"} <= set(c) for c in cats)

    def test_categories_cover_three_groups(self, client):
        groups = {c["group"] for c in client.get("/api/categories").json()}
        assert groups == {"special", "activity", "mood", "style"}

    def test_platforms_endpoint_is_gone(self, client):
        """平台抓取已删，路由不存在——不能被 SPA 回退吞成 200。"""
        assert client.get("/api/platforms").status_code == 404


class TestTasks:
    def test_create_and_list(self, client):
        r = client.post("/api/tasks", json=_task_payload())
        assert r.status_code == 201
        tasks = client.get("/api/tasks").json()
        assert len(tasks) == 1
        t = tasks[0]
        assert t["platforms"] == [] and t["categories"] == ["study"]
        assert t["category_names"] == ["学习"] and t["running"] is False

    def test_update(self, client):
        tid = client.post("/api/tasks", json=_task_payload()).json()["id"]
        r = client.put(f"/api/tasks/{tid}", json=_task_payload(name="改名", count_per=7))
        assert r.status_code == 200
        t = client.get("/api/tasks").json()[0]
        assert t["name"] == "改名" and t["count_per"] == 7

    def test_delete(self, client):
        tid = client.post("/api/tasks", json=_task_payload()).json()["id"]
        assert client.delete(f"/api/tasks/{tid}").status_code == 200
        assert client.get("/api/tasks").json() == []

    def test_push_mode_roundtrip(self, client):
        """推送方式与来源平台必须真的落库，不能在 TaskIn 里收下就丢。"""
        tid = client.post("/api/tasks", json=_task_payload(
            push_mode="source_llm", source_platforms=["wy", "kw"])).json()["id"]
        t = client.get("/api/tasks").json()[0]
        assert t["push_mode"] == "source_llm" and t["source_platforms"] == ["wy", "kw"]

        client.put(f"/api/tasks/{tid}", json=_task_payload(
            push_mode="source_raw", source_platforms=["tx"]))
        t = client.get("/api/tasks").json()[0]
        assert t["push_mode"] == "source_raw" and t["source_platforms"] == ["tx"]

    def test_empty_platform_list_means_all(self, client):
        """平台留空等价于聚合全平台，不是坏值。"""
        client.post("/api/tasks", json=_task_payload(push_mode="source_llm", source_platforms=[]))
        t = client.get("/api/tasks").json()[0]
        assert t["push_mode"] == "source_llm" and t["source_platforms"] == ["all"]

    @pytest.mark.parametrize("body,msg", [
        ({"push_mode": "瞎写的"}, "未知推送方式"),
        ({"source_platforms": ["不存在的平台"]}, "未知音源平台"),
        ({"scene_mode": "瞎写的"}, "未知场景推送方式"),
    ])
    def test_push_mode_validation_rejects(self, client, body, msg):
        r = client.post("/api/tasks", json=_task_payload(**body))
        assert r.status_code == 400 and msg in r.json()["detail"]

    def test_scene_mode_and_tracks_per_roundtrip(self, client):
        """场景合并方式与每歌单曲目数必须真的落库，不能在 TaskIn 里收下就丢。"""
        tid = client.post("/api/tasks", json=_task_payload(
            scene_mode="merge", tracks_per=50)).json()["id"]
        t = client.get("/api/tasks").json()[0]
        assert t["scene_mode"] == "merge" and t["tracks_per"] == 50

        client.put(f"/api/tasks/{tid}", json=_task_payload(
            scene_mode="separate", tracks_per=0))
        t = client.get("/api/tasks").json()[0]
        assert t["scene_mode"] == "separate" and t["tracks_per"] == 0

    def test_scene_mode_defaults_to_separate(self, client):
        """老客户端不传 scene_mode 时按单独推送处理，与历史行为一致。"""
        client.post("/api/tasks", json=_task_payload())
        t = client.get("/api/tasks").json()[0]
        assert t["scene_mode"] == "separate" and t["tracks_per"] == 0

    @pytest.mark.parametrize("body,msg", [
        ({"categories": []}, "至少选择一个场景"),
        ({"categories": ["不存在的场景"]}, "未知场景"),
        ({"cron_expr": "不是cron"}, "cron"),
        ({"count_per": 0}, None),          # pydantic 字段约束 → 422
    ])
    def test_validation_rejects(self, client, body, msg):
        r = client.post("/api/tasks", json=_task_payload(**body))
        assert r.status_code in (400, 422), r.text
        if msg:
            assert msg in r.json()["detail"]

    def test_missing_task_404(self, client):
        assert client.put("/api/tasks/999", json=_task_payload()).status_code == 404
        assert client.delete("/api/tasks/999").status_code == 404
        assert client.post("/api/tasks/999/run").status_code == 404

    @pytest.mark.parametrize("expr", ["0 8 * * 7", "0 8 * * 0", "0 8 * * 1-5",
                                      "30 0 8 * * *", "*/15 * * * *"])
    def test_accepted_cron_forms(self, client, expr):
        """校验必须走 scheduler.parse_cron，不能直接调 from_crontab——
        否则 7=周日 这类写法会出现「UI 放行、调度时才炸」。"""
        r = client.post("/api/tasks", json=_task_payload(cron_expr=expr))
        assert r.status_code == 201, r.text

    def test_run_triggers_pipeline(self, client, monkeypatch):
        tid = client.post("/api/tasks", json=_task_payload()).json()["id"]
        called = []
        monkeypatch.setattr(pipeline, "run_async", lambda t: called.append(t) or True)
        r = client.post(f"/api/tasks/{tid}/run")
        assert r.status_code == 200 and called == [tid]

    def test_run_conflicts_while_running(self, client, monkeypatch):
        tid = client.post("/api/tasks", json=_task_payload()).json()["id"]
        monkeypatch.setattr(pipeline, "is_running", lambda t: True)
        r = client.post(f"/api/tasks/{tid}/run")
        assert r.status_code == 409 and "正在运行" in r.json()["detail"]


class TestFetchNow:
    """立刻推送：用当前偏好跑一次，不必等 cron 也不必先建任务。"""

    def _prefs(self, **kw):
        p = {"categories": ["study"], "count_per": 3}
        p.update(kw)
        return p

    def test_uses_saved_preferences_when_body_empty(self, client, monkeypatch):
        client.put("/api/settings", json={"preferences": self._prefs()})
        got = {}
        monkeypatch.setattr(pipeline, "run_now_async", lambda p: got.setdefault("p", p) or True)
        r = client.post("/api/fetch-now")
        assert r.status_code == 200 and r.json()["ok"] is True
        assert got["p"] is None      # None = 用服务端存的偏好

    def test_body_overrides_without_persisting(self, client, monkeypatch):
        """草稿试跑不该污染已保存的偏好。"""
        client.put("/api/settings", json={"preferences": self._prefs()})
        got = {}
        monkeypatch.setattr(pipeline, "run_now_async", lambda p: got.setdefault("p", p) or True)
        draft = self._prefs(count_per=7, prompt="深夜emo")
        assert client.post("/api/fetch-now", json=draft).status_code == 200
        assert got["p"] == draft
        saved = client.get("/api/settings").json()["preferences"]
        assert saved["count_per"] == 3 and saved["custom_prompt"] == ""

    def test_empty_preferences_is_400(self, client, monkeypatch):
        # 不 mock run_now_async：校验就在它里面，跑真逻辑才能验到 400。
        # 参数缺失时它在起线程之前就抛 ValueError，不会真跑起来。
        client.put("/api/settings", json={"preferences": self._prefs(categories=[])})
        called = []
        monkeypatch.setattr(pipeline, "run_async", lambda t: called.append(t) or True)
        r = client.post("/api/fetch-now")
        assert r.status_code == 400
        assert "场景" in r.json()["detail"]
        assert called == []

    def test_concurrent_is_409(self, client, monkeypatch):
        monkeypatch.setattr(pipeline, "now_running", lambda: True)
        r = client.post("/api/fetch-now")
        assert r.status_code == 409 and "正在运行" in r.json()["detail"]

    def test_status_exposes_fetching_now(self, client, monkeypatch):
        monkeypatch.setattr(pipeline, "now_running", lambda: False)
        assert client.get("/api/status").json()["fetching_now"] is False
        monkeypatch.setattr(pipeline, "now_running", lambda: True)
        assert client.get("/api/status").json()["fetching_now"] is True


class TestRuns:
    def _mk_run(self, status="success", summary=None):
        conn = core.db()
        conn.execute("INSERT INTO fetch_run(task_id,started_at,status,summary) VALUES(?,?,?,?)",
                     (1, "2026-01-01 08:00:00", status,
                      json.dumps(summary or {"playlists": 1}, ensure_ascii=False)))
        conn.commit()
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def test_empty_latest_is_null(self, client):
        assert client.get("/api/runs/latest").json() is None

    def test_latest_decodes_json_columns(self, client):
        rid = self._mk_run(summary={"playlists": 3, "errors": ["x"]})
        out = client.get("/api/runs/latest").json()
        assert out["id"] == rid
        assert out["summary"]["playlists"] == 3
        assert out["playlists"] == []

    def test_detail_includes_jobs(self, client):
        rid = self._mk_run()
        conn = core.db()
        conn.execute("INSERT INTO run_playlist(run_id,platform,category,name) VALUES(?,?,?,?)",
                     (rid, "netease", "学习", "歌单A"))
        rpid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("INSERT INTO download_job(run_playlist_id,title,artist,status)"
                     " VALUES(?,?,?,?)", (rpid, "晴天", "周杰伦", "local"))
        conn.commit()
        detail = client.get(f"/api/runs/{rid}").json()
        assert detail["playlists"][0]["jobs"][0]["title"] == "晴天"

    def test_missing_run_404(self, client):
        assert client.get("/api/runs/999").status_code == 404

    def test_list_respects_limit(self, client):
        for _ in range(5):
            self._mk_run()
        assert len(client.get("/api/runs?limit=2").json()) == 2


class TestSources:
    def test_crud(self, client):
        cfg = {"search": "http://s/search?kw={kw}", "url": "http://s/u?id={id}",
               "list_path": "data", "url_paths": ["data.url"]}
        sid = client.post("/api/sources", json={"name": "源A", "priority": 2,
                                                "cfg": cfg}).json()["id"]
        got = client.get("/api/sources").json()[0]
        assert got["name"] == "源A" and got["cfg"]["list_path"] == "data"
        assert client.put(f"/api/sources/{sid}",
                          json={"name": "源B", "priority": 1, "cfg": cfg}).status_code == 200
        assert client.get("/api/sources").json()[0]["name"] == "源B"
        assert client.delete(f"/api/sources/{sid}").status_code == 200
        assert client.get("/api/sources").json() == []

    def test_requires_templates(self, client):
        r = client.post("/api/sources", json={"name": "缺模板", "cfg": {"search": ""}})
        assert r.status_code == 400

    def test_duplicate_name_conflicts(self, client):
        cfg = {"search": "http://s/s?kw={kw}", "url": "http://s/u?id={id}"}
        client.post("/api/sources", json={"name": "同名", "cfg": cfg})
        r = client.post("/api/sources", json={"name": "同名", "cfg": cfg})
        assert r.status_code == 409

    def test_check_reports_failure(self, client, monkeypatch):
        from tunesync.app import resolver
        cfg = {"search": "http://s/s?kw={kw}", "url": "http://s/u?id={id}"}
        sid = client.post("/api/sources", json={"name": "源", "cfg": cfg}).json()["id"]
        monkeypatch.setattr(resolver, "probe_source", lambda s: (False, "连不上"))
        r = client.post(f"/api/sources/{sid}/check").json()
        assert r == {"ok": False, "error": "连不上"}
        row = core.db().execute("SELECT * FROM source WHERE id=?", (sid,)).fetchone()
        assert row["health"] == "degraded"      # 默认阈值 3，首次失败只是 degraded
        assert row["last_error"] == "连不上"

    def test_check_missing_source_404(self, client):
        assert client.post("/api/sources/999/check").status_code == 404

    def test_health_endpoint_shape(self, client):
        health = client.get("/api/health/sources").json()
        assert health == [] or {"health", "name", "last_error"} <= set(health[0])

    def test_lx_source_create_ok_without_url(self, client):
        """kind=lx：search + script 即可，不要求 url 模板。"""
        cfg = {"kind": "lx", "search": "http://s/s?kw={kw}", "script": "/* lx */",
               "platform": "wy"}
        r = client.post("/api/sources", json={"name": "LX源", "priority": 3, "cfg": cfg})
        assert r.status_code == 201
        got = client.get("/api/sources").json()[0]
        assert got["cfg"]["kind"] == "lx" and got["cfg"]["platform"] == "wy"

    def test_lx_source_missing_search_rejected(self, client):
        r = client.post("/api/sources", json={
            "name": "LX缺搜索", "cfg": {"kind": "lx", "script": "/* lx */"}})
        assert r.status_code == 400 and "search" in r.json()["detail"]

    def test_lx_source_missing_script_rejected(self, client):
        r = client.post("/api/sources", json={
            "name": "LX缺脚本", "cfg": {"kind": "lx", "search": "http://s/s?kw={kw}"}})
        assert r.status_code == 400 and "script" in r.json()["detail"]

    def test_http_source_missing_url_still_rejected(self, client):
        """默认 kind 视为 http，只有 search 没有 url 依旧 400。"""
        r = client.post("/api/sources", json={
            "name": "缺取链", "cfg": {"search": "http://s/s?kw={kw}"}})
        assert r.status_code == 400


class TestPlaylistImport:
    """歌单广场 / 一键导入路由：只验证契约与转发，平台客户端细节在 test_platforms.py。"""

    def test_platforms(self, client):
        assert client.get("/api/playlist/platforms").json() == [
            {"key": "all", "name": "聚合"}, {"key": "wy", "name": "网易云"},
            {"key": "tx", "name": "QQ 音乐"}, {"key": "kg", "name": "酷狗"},
            {"key": "kw", "name": "酷我"}, {"key": "mg", "name": "咪咕"}]

    def test_categories_wy_static(self, client):
        cats = client.get("/api/playlist/categories", params={"platform": "wy"}).json()
        assert cats[0] == {"value": "华语", "label": "华语"}
        assert all({"value", "label"} == set(c) for c in cats)

    def test_categories_unknown_platform_502(self, client):
        r = client.get("/api/playlist/categories", params={"platform": "xx"})
        assert r.status_code == 502 and "不支持的平台" in r.json()["detail"]

    def test_square_proxies_with_paging(self, client, monkeypatch):
        got = {}
        def _square(platform, **kw):
            got.update(platform=platform, **kw)
            return {"total": 1, "lists": [{"id": "42", "name": "歌单"}]}
        monkeypatch.setattr(platforms, "square", _square)
        r = client.get("/api/playlist/square",
                       params={"platform": "wy", "cat": "华语", "page": 2, "limit": 30})
        assert r.status_code == 200 and r.json()["lists"][0]["id"] == "42"
        assert got == {"platform": "wy", "cat": "华语", "offset": 30, "limit": 30}

    def test_square_limit_clamped_and_empty_cat(self, client, monkeypatch):
        got = {}
        def _square(platform, **kw):
            got.update(platform=platform, **kw)
            return {"total": 0, "lists": []}
        monkeypatch.setattr(platforms, "square", _square)
        client.get("/api/playlist/square", params={"platform": "tx", "cat": "", "limit": 100})
        assert got["limit"] == 60 and got["cat"] is None

    def test_square_platform_error_502(self, client, monkeypatch):
        def _boom(*a, **kw):
            raise platforms.PlatformError("网易云歌单广场失败（code=-460，游客限流）")
        monkeypatch.setattr(platforms, "square", _boom)
        r = client.get("/api/playlist/square", params={"platform": "wy"})
        assert r.status_code == 502 and "-460" in r.json()["detail"]

    def test_detail_resolves_share_link(self, client, monkeypatch):
        got = {}
        def _detail(platform, pid):
            got["ref"] = (platform, pid)
            return {"platform": platform, "id": pid, "name": "x", "cover": "",
                    "count": 0, "tracks": []}
        monkeypatch.setattr(platforms, "detail", _detail)
        r = client.post("/api/playlist/detail",
                        json={"link": "https://music.163.com/#/playlist?id=42"})
        assert r.status_code == 200 and got["ref"] == ("wy", "42")
        assert r.json()["platform"] == "wy"

    def test_detail_digits_need_platform(self, client):
        r = client.post("/api/playlist/detail", json={"link": "42"})
        assert r.status_code == 400 and "平台" in r.json()["detail"]

    def test_detail_bad_link_400(self, client):
        r = client.post("/api/playlist/detail", json={"link": "https://example.com/x"})
        assert r.status_code == 400 and "无法识别" in r.json()["detail"]

    def test_import_triggers_pipeline(self, client, monkeypatch):
        got = {}
        monkeypatch.setattr(pipeline, "import_playlist_async",
                            lambda p, i: got.setdefault("ref", (p, i)))
        r = client.post("/api/playlist/import", json={"platform": "wy", "link": "42"})
        assert r.status_code == 202
        assert r.json() == {"ok": True, "platform": "wy", "id": "42"}
        assert got["ref"] == ("wy", "42")

    def test_import_conflict_409(self, client, monkeypatch):
        def _busy(p, i):
            raise RuntimeError("该歌单已有一次导入正在运行")
        monkeypatch.setattr(pipeline, "import_playlist_async", _busy)
        r = client.post("/api/playlist/import", json={"platform": "wy", "link": "42"})
        assert r.status_code == 409 and "正在运行" in r.json()["detail"]

    def test_import_running_flag(self, client, monkeypatch):
        monkeypatch.setattr(pipeline, "import_running", lambda p, i: True)
        r = client.get("/api/playlist/import/running",
                       params={"platform": "wy", "pid": "42"})
        assert r.json() == {"running": True}


class TestQuickLxImport:
    """落雪式导入：粘贴脚本文本/URL → 沙箱预检 → 自动取名配平台建源。"""

    @pytest.fixture
    def sandbox_ok(self, monkeypatch):
        """替身沙箱：加载即成功，自报支持 wy/kg 两平台。"""
        from tunesync.app import lxbridge
        return mock.patch.object(
            lxbridge.BRIDGE, "ensure",
            create=True, return_value={
                "wy": {"qualitys": ["128k", "320k"]},
                "kg": {"qualitys": ["128k"]}})

    def test_import_text_script_auto_name(self, client, sandbox_ok):
        with sandbox_ok:
            r = client.post("/api/sources/quick-lx",
                            json={"script": "/*! @name 测试音源 v1 */ const x = 1"})
        assert r.status_code == 201, r.text
        out = r.json()
        assert out["name"] == "LX·测试音源 v1"
        assert out["platform"] == "wy" and "wy" in out["platforms"]
        src = core.db().execute("SELECT * FROM source WHERE id=?", (out["id"],)).fetchone()
        cfg = json.loads(src["cfg"])
        assert cfg["kind"] == "lx" and cfg["platform"] == "wy"
        assert cfg["search"].startswith("https://music-api.gdstudio.xyz")
        assert "source=netease" in cfg["search"] and cfg["list_path"] == "$"
        assert cfg["script"] == "/*! @name 测试音源 v1 */ const x = 1"
        assert src["priority"] == 1       # 空表里第一个

    def test_import_url_is_stored_verbatim(self, client, sandbox_ok, monkeypatch):
        """链接输入：预检用拉下来的文本，但 cfg.script 原样存 URL（跟随上游更新）。"""
        from tunesync.app import resolver
        url = "https://ghproxy.net/raw.githubusercontent.com/x/y/latest.js"
        monkeypatch.setattr(resolver, "_lx_script",
                            lambda cfg: "/* pulled */ /*! @name 拉取源 */")
        with sandbox_ok:
            r = client.post("/api/sources/quick-lx", json={"script": url})
        assert r.status_code == 201
        cfg = json.loads(core.db().execute(
            "SELECT cfg FROM source WHERE id=?", (r.json()["id"],)).fetchone()["cfg"])
        assert cfg["script"] == url

    def test_auto_picks_declared_platform(self, client, monkeypatch):
        """脚本自报不含 wy 时，自动改选它声明的第一个平台并配对应搜索源。

        kw 特例：GD 音乐台不支持 source=kuwo，改配酷我官方搜索（abslist + 大写字段）。
        """
        from tunesync.app import lxbridge
        patch = mock.patch.object(
            lxbridge.BRIDGE, "ensure", create=True,
            return_value={"kw": {"qualitys": ["128k"]}, "tx": {}})
        with patch:
            r = client.post("/api/sources/quick-lx", json={"script": "/* lx */"})
        assert r.status_code == 201 and r.json()["platform"] == "kw"
        cfg = json.loads(core.db().execute(
            "SELECT cfg FROM source WHERE id=?", (r.json()["id"],)).fetchone()["cfg"])
        assert cfg["search"].startswith("http://search.kuwo.cn/r.s")
        assert cfg["list_path"] == "abslist" and cfg["id_field"] == "MUSICRID"
        assert cfg["title_field"] == "NAME" and cfg["artist_field"] == "ARTIST"

    def test_sandbox_failure_is_400(self, client, monkeypatch):
        from tunesync.app import lxbridge
        def _boom(script, name="lx-source"):
            raise RuntimeError("语法错误")
        monkeypatch.setattr(lxbridge.BRIDGE, "ensure", _boom, raising=False)
        r = client.post("/api/sources/quick-lx", json={"script": "/* bad */"})
        assert r.status_code == 400 and "无法加载" in r.json()["detail"]
        assert core.db().execute("SELECT COUNT(*) c FROM source").fetchone()["c"] == 0

    def test_duplicate_name_gets_suffix(self, client, sandbox_ok):
        with sandbox_ok:
            first = client.post("/api/sources/quick-lx",
                                json={"script": "/*! @name 同名 */ a"}).json()
            second = client.post("/api/sources/quick-lx",
                                 json={"script": "/*! @name 同名 */ b"}).json()
        assert first["name"] == "LX·同名" and second["name"] == "LX·同名·2"
        def _prio(sid):
            return core.db().execute(
                "SELECT priority FROM source WHERE id=?", (sid,)).fetchone()["priority"]
        assert _prio(second["id"]) == _prio(first["id"]) + 1

    def test_empty_script_and_bad_platform(self, client, sandbox_ok):
        assert client.post("/api/sources/quick-lx", json={"script": "  "}).status_code == 400
        with sandbox_ok:
            r = client.post("/api/sources/quick-lx",
                            json={"script": "/* lx */", "platform": "xm"})
        assert r.status_code == 400 and "平台" in r.json()["detail"]


class TestTargets:
    def test_defaults_mask_password(self, client):
        core.kv_set("targets", {"navidrome": {"url": "http://nd:4533", "user": "u",
                                              "password": "secret"}})
        out = client.get("/api/targets").json()
        assert out["navidrome"]["password"] == "***"

    def test_nested_password_is_masked(self, client):
        """嵌套子对象里的口令同样要掩码，只掩码顶层等于把密钥发回浏览器。"""
        core.kv_set("targets", {"feiniu": {"nested": {"base_url": "http://gw:5666",
                                                     "user": "u", "password": "secret"}}})
        out = client.get("/api/targets").json()
        assert out["feiniu"]["nested"]["password"] == "***"
        assert out["feiniu"]["nested"]["base_url"] == "http://gw:5666"

    @pytest.mark.parametrize("sent", ["***", ""])
    def test_password_placeholder_means_unchanged(self, client, sent):
        """占位值和空串都按「不修改」处理——否则会静默清空密码把人踢下线。"""
        client.put("/api/targets", json={"navidrome": {"url": "http://nd:4533",
                                                     "user": "u", "password": "secret"}})
        client.put("/api/targets", json={"navidrome": {"user": "u2", "password": sent}})
        stored = core.kv_get("targets")["navidrome"]
        assert stored["user"] == "u2"
        assert stored["password"] == "secret"

    def test_new_password_is_stored(self, client):
        client.put("/api/targets", json={"navidrome": {"url": "http://nd:4533",
                                                     "password": "新密码"}})
        assert core.kv_get("targets")["navidrome"]["password"] == "新密码"

    def test_nested_cfg_is_deep_merged(self, client):
        """只改子对象的一个键，不能把整块替换掉。"""
        client.put("/api/targets", json={"feiniu": {"nested": {"base_url": "http://gw:5666",
                                                               "user": "u", "password": "p"}}})
        client.put("/api/targets", json={"feiniu": {"nested": {"user": "u2"}}})
        nested = core.kv_get("targets")["feiniu"]["nested"]
        assert nested == {"base_url": "http://gw:5666", "user": "u2", "password": "p"}

    @pytest.mark.parametrize("sent", ["***", ""])
    def test_nested_password_placeholder_means_unchanged(self, client, sent):
        """嵌套口令的掩码/空串必须递归处理。

        否则前端把 *** 回填进密码框再保存，会把字面量 "***" 存成真口令，
        直接打死认证，而且从界面上完全看不出来。
        """
        client.put("/api/targets", json={"feiniu": {"nested": {"base_url": "http://gw:5666",
                                                               "user": "u", "password": "secret"}}})
        client.put("/api/targets", json={"feiniu": {"nested": {"user": "u2",
                                                              "password": sent}}})
        nested = core.kv_get("targets")["feiniu"]["nested"]
        assert nested["password"] == "secret"
        assert nested["user"] == "u2"

    def test_nested_password_omitted_keeps_old(self, client):
        client.put("/api/targets", json={"feiniu": {"nested": {"base_url": "http://gw:5666",
                                                               "user": "u", "password": "secret"}}})
        client.put("/api/targets", json={"feiniu": {"nested": {"user": "u2"}}})
        assert core.kv_get("targets")["feiniu"]["nested"]["password"] == "secret"

    def test_new_nested_password_is_stored(self, client):
        client.put("/api/targets", json={"feiniu": {"nested": {"password": "新口令"}}})
        assert core.kv_get("targets")["feiniu"]["nested"]["password"] == "新口令"

    def test_unknown_target_rejected(self, client):
        assert client.put("/api/targets", json={"spotify": {}}).status_code == 400

    def test_test_endpoint_reports_error_not_500(self, client):
        core.kv_set("targets", {"navidrome": {"url": "http://127.0.0.1:1", "user": "u"}})
        r = client.post("/api/targets/navidrome/test")
        assert r.status_code == 200 and r.json()["ok"] is False

    def test_test_endpoint_works_even_when_disabled(self, client):
        """enabled=false 只是不参与同步，「测试连接」仍应可用。"""
        core.kv_set("targets", {"navidrome": {"url": "http://127.0.0.1:1", "user": "u",
                                              "password": "p", "enabled": False}})
        assert client.post("/api/targets/navidrome/test").json()["ok"] is False

    def test_feiniu_schema_endpoint(self, client):
        core.kv_set("targets", {"feiniu": {"db_path": "/feiniu-db/music.db"}})
        r = client.get("/api/targets/feiniu/schema").json()
        assert r["ok"] is False and "不存在" in (r["error"] or "")

    def test_feiniu_fix_covers_endpoint(self, client, monkeypatch):
        core.kv_set("targets", {"feiniu": {"db_path": "/feiniu-db/music.db"}})
        called = {}
        def _fake(writer):
            called["db_path"] = writer.db_path
            return {"total": 3, "ok": 1, "copied": 1, "reassigned": 1, "missing": 0}
        monkeypatch.setattr(writers, "fix_feiniu_playlist_covers", _fake)
        r = client.post("/api/targets/feiniu/fix-covers")
        assert r.status_code == 200
        assert r.json()["copied"] == 1
        assert called["db_path"] == "/feiniu-db/music.db"

    def test_feiniu_fix_covers_404_without_target(self, client, monkeypatch):
        # config.yaml 里可能带 feiniu 节，deep_merge 清不掉，直接换掉合并函数
        monkeypatch.setattr(api, "merged_targets", lambda: {"feiniu": {}})
        assert client.post("/api/targets/feiniu/fix-covers").status_code == 404

    def test_feiniu_fix_covers_400_on_runtime_error(self, client, monkeypatch):
        core.kv_set("targets", {"feiniu": {"db_path": "/x.db"}})
        def _boom(writer):
            raise RuntimeError("飞牛 music.db 不可用")
        monkeypatch.setattr(writers, "fix_feiniu_playlist_covers", _boom)
        r = client.post("/api/targets/feiniu/fix-covers")
        assert r.status_code == 400 and "不可用" in r.json()["detail"]


class TestTestConnectionDraft:
    """测试连接必须测「界面上填的地址」。

    原来 /targets/{kind}/test 不收请求体，只测已保存的值。用户改完地址直接点测试
    （还没保存），报错说的是旧地址——真实踩过：填了 IPv6 地址，报错却在连 127.0.0.1，
    报错信息跟屏幕上填的完全对不上，排查绕了很大一圈。
    """

    def _seen(self, monkeypatch):
        """让 build_writers 记录下真正传进去的配置，然后返回成功的测试结果。"""
        got = {}

        class _W:
            def __init__(self, cfg, **kw):
                got["cfg"] = cfg

            def test(self):
                return "连接正常"

        def _fake(cfg, **kw):
            got["cfg"] = cfg[next(iter(cfg))]
            return [_W(got["cfg"])]

        monkeypatch.setattr(writers, "build_writers", _fake)
        return got

    def test_body_is_used_instead_of_stored(self, client, monkeypatch):
        core.kv_set("targets", {"navidrome": {"url": "http://127.0.0.1:4533",
                                               "user": "admin", "password": "真口令"}})
        got = self._seen(monkeypatch)
        r = client.post("/api/targets/navidrome/test",
                        json={"url": "http://10.0.0.9:4533", "user": "youze27"})
        assert r.status_code == 200 and r.json()["ok"] is True
        assert got["cfg"]["url"] == "http://10.0.0.9:4533"
        assert got["cfg"]["user"] == "youze27"

    def test_draft_does_not_persist(self, client, monkeypatch):
        core.kv_set("targets", {"navidrome": {"url": "http://127.0.0.1:4533", "user": "admin"}})
        self._seen(monkeypatch)
        client.post("/api/targets/navidrome/test", json={"url": "http://10.0.0.9:4533"})
        assert core.kv_get("targets")["navidrome"]["url"] == "http://127.0.0.1:4533"

    def test_masked_password_falls_back_to_stored(self, client, monkeypatch):
        """密码框回填的是 ***，测试时必须用已存的真口令，否则必然认证失败。"""
        core.kv_set("targets", {"navidrome": {"url": "http://127.0.0.1:4533",
                                               "user": "u", "password": "真口令"}})
        got = self._seen(monkeypatch)
        client.post("/api/targets/navidrome/test",
                    json={"url": "http://10.0.0.9:4533", "password": "***"})
        assert got["cfg"]["password"] == "真口令"

    def test_empty_body_keeps_stored_behaviour(self, client, monkeypatch):
        core.kv_set("targets", {"navidrome": {"url": "http://127.0.0.1:4533", "user": "u"}})
        got = self._seen(monkeypatch)
        client.post("/api/targets/navidrome/test", json={})
        assert got["cfg"]["url"].endswith(":4533")

    def test_ipv6_url_in_draft_is_not_rewritten(self, client, monkeypatch):
        """用户填公网 IPv6，必须原样使用，不能被换成宿主地址。"""
        core.kv_set("targets", {"navidrome": {"url": "http://127.0.0.1:4533", "user": "u"}})
        got = self._seen(monkeypatch)
        v6 = "https://[2001:db8::1]:4533"   # RFC 3849 文档前缀，别写真地址
        client.post("/api/targets/navidrome/test", json={"url": v6})
        assert got["cfg"]["url"] == v6

    def test_loopback_in_draft_still_gets_host_rewrite(self, client, monkeypatch):
        """草稿里填 127.0.0.1 也照样要改写成宿主地址，否则容器里必然连不上。"""
        core.kv_set("targets", {"navidrome": {"url": "", "user": "u"}})
        monkeypatch.setattr(core, "_in_container", True)
        monkeypatch.setattr(core, "_detect_host", lambda: "nas.local")
        core._host_alias = None
        got = self._seen(monkeypatch)
        client.post("/api/targets/navidrome/test", json={"url": "http://127.0.0.1:4533"})
        assert got["cfg"]["url"] == "http://nas.local:4533"

    def test_unknown_target_rejected(self, client):
        assert client.post("/api/targets/spotify/test", json={}).status_code == 400


class TestSettings:
    def test_defaults_and_override(self, client):
        base = client.get("/api/settings").json()
        assert base["defaults"].get("naming", {}) == {}
        client.put("/api/settings", json={"naming": {"playlist": "{name}"}})
        out = client.get("/api/settings").json()
        assert out["naming"]["playlist"] == "{name}"
        assert out["defaults"]["naming"]["playlist"] == "{name}"

    def test_cron_convention_is_published(self, client):
        """星期编号是 APScheduler 的 0=周一，不是 Unix cron 的 0=周日——
        前端必须按服务端这套生成，否则「周日」会变成「周一」。"""
        cron = client.get("/api/settings").json()["cron"]
        assert "0=周一" in cron["day_of_week"]

    def test_settings_publishes_resolved_host(self, client, monkeypatch):
        """「音源一直连不上」时得能看出 127.0.0.1 被换成了什么。"""
        monkeypatch.setattr(core, "_in_container", True)
        monkeypatch.setattr(core, "_detect_host", lambda: "host.docker.internal")
        core._host_alias = None
        h = client.get("/api/settings").json()["host"]
        assert h == {"alias": "host.docker.internal", "in_container": True, "override": ""}

    def test_llm_key_is_masked(self, client, monkeypatch):
        """api_key 只在部署环境配置，绝不能原样发回浏览器。"""
        monkeypatch.setitem(core.CONFIG, "llm", {
            "base_url": "https://api.deepseek.com/v1", "api_key": "sk-secret",
            "model": "deepseek-chat"})
        out = client.get("/api/settings").json()["llm"]
        assert out["api_key"] == "***"
        assert out["base_url"] == "https://api.deepseek.com/v1" and out["model"] == "deepseek-chat"

    def test_llm_section_can_be_updated(self, client):
        r = client.put("/api/settings", json={"llm": {
            "base_url": "https://api.deepseek.com/v1", "api_key": "sk-abc",
            "model": "deepseek-chat", "temperature": 0.5, "tracks_per_playlist": 20}})
        assert r.status_code == 200
        out = client.get("/api/settings").json()["llm"]
        assert out["base_url"] == "https://api.deepseek.com/v1"
        assert out["model"] == "deepseek-chat"
        assert out["api_key"] == "***" and out["temperature"] == 0.5
        assert core.kv_get("settings")["llm"]["api_key"] == "sk-abc"

    @pytest.mark.parametrize("sent,expect", [
        ("***", "sk-abc"),      # 掩码回填 = 不修改
        ("", "sk-abc"),         # 清空输入框 = 不修改
        ("sk-new", "sk-new"),   # 新 Key 照常保存
    ])
    def test_llm_key_placeholder_semantics(self, client, sent, expect):
        client.put("/api/settings", json={"llm": {"base_url": "http://x", "model": "m",
                                                  "api_key": "sk-abc"}})
        client.put("/api/settings", json={"llm": {"model": "m2", "api_key": sent}})
        stored = core.kv_get("settings")["llm"]
        assert stored["api_key"] == expect and stored["model"] == "m2"

    @pytest.mark.parametrize("body,msg", [
        ({"temperature": "x"}, "temperature"),
        ({"tracks_per_playlist": "abc"}, "tracks_per_playlist"),
        ({"base_url": 123}, "base_url"),
        ({"unknown_field": 1}, "不支持的字段"),
    ])
    def test_llm_section_validated(self, client, body, msg):
        r = client.put("/api/settings", json={"llm": body})
        assert r.status_code == 400 and msg in r.json()["detail"]


class TestTestLlm:
    """LLM 连通测试：先测后存，覆盖值透传给 recommender，不落库。"""

    def _stub_ok(self, monkeypatch):
        seen = {}

        def fake_chat(content, system=None, override=None):
            seen["override"] = override
            return {"choices": [{"message": {"content": "连通"}}]}

        monkeypatch.setattr(recommender, "_chat", fake_chat)
        return seen

    def test_not_configured_400(self, client, monkeypatch):
        monkeypatch.setitem(core.CONFIG, "llm", {})
        r = client.post("/api/settings/test-llm", json={})
        assert r.status_code == 400 and "未配置" in r.json()["detail"]

    def test_ok_returns_latency_model_reply(self, client, monkeypatch):
        monkeypatch.setitem(core.CONFIG, "llm", {
            "base_url": "http://x/v1", "model": "m1", "api_key": "sk-abc"})
        self._stub_ok(monkeypatch)
        r = client.post("/api/settings/test-llm", json={})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["model"] == "m1" and body["reply"] == "连通"
        assert isinstance(body["latency_ms"], int)

    def test_override_reaches_chat_and_skips_placeholder_key(self, client, monkeypatch):
        monkeypatch.setitem(core.CONFIG, "llm", {
            "base_url": "http://saved/v1", "model": "saved-m", "api_key": "sk-saved"})
        seen = self._stub_ok(monkeypatch)
        r = client.post("/api/settings/test-llm", json={
            "base_url": "http://new/v1", "model": "new-m", "api_key": "***"})
        assert r.status_code == 200
        ov = seen["override"]
        assert ov["base_url"] == "http://new/v1" and ov["model"] == "new-m"
        # "***" 掩码不会被当成真 key 发出去
        assert recommender._llm_cfg(ov)["api_key"] == "sk-saved"
        assert r.json()["model"] == "new-m"

    def test_override_never_persisted(self, client, monkeypatch):
        monkeypatch.setitem(core.CONFIG, "llm", {
            "base_url": "http://saved/v1", "model": "saved-m", "api_key": "sk-saved"})
        self._stub_ok(monkeypatch)
        client.post("/api/settings/test-llm", json={"base_url": "http://tmp/v1",
                                                    "model": "tmp-m", "api_key": "sk-tmp"})
        # 连通测试只读配置，绝不能把表单草稿写进库
        assert not (core.kv_get("settings") or {}).get("llm")
        cfg = core.merged_config()["llm"]
        assert cfg["base_url"] == "http://saved/v1" and cfg["api_key"] == "sk-saved"

    def test_recommender_error_becomes_502(self, client, monkeypatch):
        monkeypatch.setitem(core.CONFIG, "llm", {
            "base_url": "http://x/v1", "model": "m1"})

        def boom(content, system=None, override=None):
            raise recommender.RecommenderError("LLM 请求被拒绝（HTTP 401）")

        monkeypatch.setattr(recommender, "_chat", boom)
        r = client.post("/api/settings/test-llm", json={})
        assert r.status_code == 502 and "401" in r.json()["detail"]

    def test_targets_show_rewritten_urls(self, client, monkeypatch):
        monkeypatch.setattr(core, "_in_container", True)
        monkeypatch.setattr(core, "_detect_host", lambda: "nas.local")
        core._host_alias = None
        t = client.get("/api/targets").json()
        assert t["navidrome"]["url"] == "http://nas.local:4533"
        # 飞牛只剩 db 模式，没有网关地址可改写；db_path 是路径也不能被当 URL 换掉
        assert t["feiniu"]["db_path"] == "/feiniu-db/music.db"

    def test_container_does_not_break_library_paths(self, client, monkeypatch):
        """目录是路径不是 URL，改写宿主地址时不能碰。"""
        monkeypatch.setattr(core, "_in_container", True)
        monkeypatch.setattr(core, "_detect_host", lambda: "nas.local")
        core._host_alias = None
        lib = client.get("/api/settings").json()["library"]
        assert lib["root"].startswith("/")

    def test_preferences_are_persisted(self, client):
        """推送偏好要存到服务端，不能只留在浏览器 localStorage。"""
        client.put("/api/settings", json={"preferences": {
            "categories": ["study"], "count_per": 5}})
        pref = client.get("/api/settings").json()["preferences"]
        # PUT 是局部覆盖：没传的键沿用 config.yaml 的默认值
        assert pref == {"categories": ["study"], "custom_prompt": "", "count_per": 5,
                        "push_mode": "source_llm", "source_platforms": ["all"]}
        assert core.kv_get("settings")["preferences"]["count_per"] == 5

    @pytest.mark.parametrize("pref", [
        {"categories": ["不存在的"]},
        {"count_per": 0},
        {"count_per": "x"},
    ])
    def test_preferences_are_validated(self, client, pref):
        assert client.put("/api/settings", json={"preferences": pref}).status_code == 400

    def test_unknown_section_rejected(self, client):
        assert client.put("/api/settings", json={"nope": {}}).status_code == 400


class TestLibraryAndStatus:
    def test_library_status(self, client):
        assert client.get("/api/library").json()["files"] == 0

    def test_rescan_returns_ok(self, client):
        r = client.post("/api/library/rescan")
        assert r.status_code == 200 and r.json()["ok"] is True

    def test_rescan_alias(self, client):
        assert client.post("/api/library/scan").status_code == 200

    def test_status_shape(self, client):
        s = client.get("/api/status").json()
        assert set(s) == {"sources", "last_run", "running", "fetching_now",
                          "library_files", "active_jobs", "queue", "playlists_total",
                          "local_hit_total"}
        assert s["last_run"] is None and s["running"] == []
        assert s["playlists_total"] == 0
        # 本地复用累计：没有任何运行时为 0
        assert s["local_hit_total"] == 0
        # 推送队列是串行的，空闲时谁也没在跑、也没人排队
        assert s["queue"] == {"active": None, "waiting": [], "depth": 0}

    def test_run_queue_endpoint(self, client):
        assert client.get("/api/runs/queue").json() == {"active": None, "waiting": [], "depth": 0}


class _FakeWriter:
    """记录调用参数的假 writer，用于验证自建歌单的写入契约。"""

    def __init__(self, name, err=None):
        self.name = name
        self.err = err
        self.calls = []

    def upsert(self, name, tracks, files):
        self.calls.append((name, list(tracks), list(files)))
        if self.err:
            raise self.err
        return f"{self.name}: {len(tracks)} 首"


def _seed_local_files(library):
    """直接写三行索引，覆盖歌手 / 流派 / 风格 / 年份 / 格式五个筛选维度。

    不走 scan_library：这里要验的是查询与筛选，读标签那步换成造好的行更快也更稳。
    """
    rows = [("晴天", "周杰伦", "流行", "流行", 2003, "mp3", 5 * 1024 * 1024),
            ("七里香", "周杰伦", "流行", "R&B", 2004, "flac", 30 * 1024 * 1024),
            ("遇见", "孙燕姿", "流行", "抒情", 2000, "wav", 8 * 1024 * 1024)]
    conn = core.db()
    paths = []
    for title, artist, genre, style, year, fmt, size in rows:
        p = os.path.join(str(library), "Downloaded", artist, f"{artist} - {title}.{fmt}")
        conn.execute(
            "INSERT OR REPLACE INTO local_file"
            "(path,title,artist,norm_key,mtime,size,format,genre,style,year)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (p, title, artist, core.tkey(title, artist), 1.0, size, fmt,
             genre, style, year))
        paths.append(p)
    conn.commit()
    return paths


class TestLocalFiles:
    def test_empty_library(self, client, library):
        d = client.get("/api/localfiles").json()
        assert d == {"items": [], "total": 0, "page": 1, "limit": 50}

    def test_list_shape(self, client, library):
        _seed_local_files(library)
        d = client.get("/api/localfiles").json()
        assert d["total"] == 3 and len(d["items"]) == 3
        it = d["items"][0]
        assert {"id", "path", "title", "artist", "album", "genre", "style",
                "year", "format", "size"} <= set(it)

    def test_filter_by_artist(self, client, library):
        _seed_local_files(library)
        d = client.get("/api/localfiles", params={"artist": "周杰伦"}).json()
        assert d["total"] == 2 and {i["artist"] for i in d["items"]} == {"周杰伦"}

    def test_filter_by_keyword(self, client, library):
        _seed_local_files(library)
        assert client.get("/api/localfiles", params={"q": "遇见"}).json()["total"] == 1
        # 路径也算搜索范围：Downloaded 是目录名
        assert client.get("/api/localfiles", params={"q": "Downloaded"}).json()["total"] == 3
        assert client.get("/api/localfiles", params={"q": "根本没有"}).json()["total"] == 0

    def test_filter_by_format_normalises_dot_and_case(self, client, library):
        _seed_local_files(library)
        assert client.get("/api/localfiles", params={"fmt": "flac"}).json()["total"] == 1
        assert client.get("/api/localfiles", params={"fmt": ".MP3"}).json()["total"] == 1

    def test_filter_by_genre(self, client, library):
        _seed_local_files(library)
        d = client.get("/api/localfiles", params={"genre": "流行"}).json()
        assert d["total"] == 3
        assert client.get("/api/localfiles", params={"genre": "摇滚"}).json()["total"] == 0

    def test_filter_by_style(self, client, library):
        """风格是多值的（'R&B;Funk'），按其中一段命中即可。"""
        _seed_local_files(library)
        core.db().execute("UPDATE local_file SET style='R&B;Funk' WHERE artist='周杰伦'")
        core.db().commit()
        d = client.get("/api/localfiles", params={"style": "Funk"}).json()
        assert d["total"] == 2
        assert client.get("/api/localfiles", params={"style": "抒情"}).json()["total"] == 1

    def test_filter_by_year(self, client, library):
        _seed_local_files(library)
        assert client.get("/api/localfiles", params={"year": "2003"}).json()["total"] == 1
        assert client.get("/api/localfiles", params={"year": "2004"}).json()["total"] == 1
        assert client.get("/api/localfiles", params={"year": "1999"}).json()["total"] == 0

    def test_filter_combines_genre_and_year(self, client, library):
        _seed_local_files(library)
        d = client.get("/api/localfiles",
                       params={"genre": "流行", "year": "2004"}).json()
        assert d["total"] == 1 and d["items"][0]["title"] == "七里香"

    def test_pagination(self, client, library):
        _seed_local_files(library)
        p1 = client.get("/api/localfiles", params={"limit": 2, "page": 1}).json()
        p2 = client.get("/api/localfiles", params={"limit": 2, "page": 2}).json()
        assert len(p1["items"]) == 2 and len(p2["items"]) == 1
        assert {i["id"] for i in p1["items"]} & {i["id"] for i in p2["items"]} == set()

    def test_sort_rejects_unknown_column(self, client, library):
        """sort 直接拼进 ORDER BY，非法列名必须被白名单挡掉而不是变成 SQL 注入面。"""
        _seed_local_files(library)
        assert client.get("/api/localfiles", params={"sort": "path; DROP TABLE local_file"}).status_code == 200
        d = client.get("/api/localfiles", params={"sort": "size", "order": "desc"}).json()
        assert d["items"][0]["size"] == 30 * 1024 * 1024
        assert core.db().execute("SELECT COUNT(*) c FROM local_file").fetchone()["c"] == 3

    def test_facets(self, client, library):
        _seed_local_files(library)
        d = client.get("/api/localfiles/facets").json()
        assert d["total"] == 3 and d["scraped"] == 3
        assert {a["name"] for a in d["artists"]} == {"周杰伦", "孙燕姿"}
        assert {f["name"] for f in d["formats"]} == {"mp3", "flac", "wav"}
        assert [g["name"] for g in d["genres"]] == ["流行"]
        assert {s["name"] for s in d["styles"]} == {"流行", "R&B", "抒情"}
        # 年份倒序，方便前端直接铺成滑块
        assert [y["name"] for y in d["years"]] == ["2004", "2003", "2000"]

    def test_facets_cache_hits_then_refreshes(self, client, library):
        """缓存命中时 cached=True；refresh=true 必须真的重算（数据已变时结果也要变）。"""
        # 缓存是模块级的，别的用例可能已经用同一份签名填过，先清干净
        api._facets_cache.update(key=None, at=0, data=None)
        _seed_local_files(library)
        first = client.get("/api/localfiles/facets").json()
        assert first["cached"] is False
        assert client.get("/api/localfiles/facets").json()["cached"] is True
        core.db().execute("UPDATE local_file SET genre='金属' WHERE title='晴天'")
        core.db().commit()
        assert client.get("/api/localfiles/facets").json()["cached"] is True
        again = client.get("/api/localfiles/facets", params={"refresh": True}).json()
        assert again["cached"] is False
        assert {g["name"] for g in again["genres"]} == {"流行", "金属"}


class TestLocalPlaylist:
    def _body(self, paths, name="通勤路上"):
        return {"name": name, "paths": list(paths)}

    def test_writes_all_enabled_targets(self, client, library):
        paths = _seed_local_files(library)
        w = _FakeWriter("feiniu")
        with mock.patch.object(writers, "build_writers", return_value=[w]):
            r = client.post("/api/localfiles/playlist", json=self._body(paths))
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["ok"] is True and d["tracks"] == 3
        assert d["results"] == [{"target": "feiniu", "ok": True, "detail": "feiniu: 3 首"}]
        name, tracks, files = w.calls[0]
        assert name == "通勤路上" and files == paths
        assert ("晴天", "周杰伦") in tracks

    def test_one_target_failing_keeps_others(self, client, library):
        paths = _seed_local_files(library)
        bad, good = _FakeWriter("feiniu", RuntimeError("music.db 被锁")), _FakeWriter("navidrome")
        with mock.patch.object(writers, "build_writers", return_value=[bad, good]):
            d = client.post("/api/localfiles/playlist", json=self._body(paths)).json()
        assert d["ok"] is True
        assert [r["ok"] for r in d["results"]] == [False, True]
        assert "music.db 被锁" in d["results"][0]["error"]

    def test_all_targets_failing_returns_502(self, client, library):
        paths = _seed_local_files(library)
        w = _FakeWriter("feiniu", RuntimeError("未定位到任何曲目"))
        with mock.patch.object(writers, "build_writers", return_value=[w]):
            r = client.post("/api/localfiles/playlist", json=self._body(paths))
        assert r.status_code == 502 and "未定位到任何曲目" in r.json()["detail"]

    def test_no_target_configured(self, client, library):
        paths = _seed_local_files(library)
        core.kv_set("targets", {"navidrome": {"enabled": False}, "feiniu": {"enabled": False}})
        r = client.post("/api/localfiles/playlist", json=self._body(paths))
        assert r.status_code == 400 and "推送平台" in r.json()["detail"]

    def test_unknown_path_rejected(self, client, library):
        paths = _seed_local_files(library)
        with mock.patch.object(writers, "build_writers", return_value=[_FakeWriter("feiniu")]):
            r = client.post("/api/localfiles/playlist",
                            json=self._body(paths + [os.path.join(str(library), "x.mp3")]))
        assert r.status_code == 400 and "不在本地索引" in r.json()["detail"]

    @pytest.mark.parametrize("body", [
        {"name": "", "paths": ["/a.mp3"]},
        {"name": "歌单", "paths": []},
    ])
    def test_payload_validation(self, client, library, body):
        assert client.post("/api/localfiles/playlist", json=body).status_code == 422


def _seed_duplicates(library):
    """造一组真文件：手工整理区一份 + 下载目录两份，同名同格式同大小。

    两份下载副本必须落在不同目录，否则 INSERT OR REPLACE 会互相覆盖，
    组里就只剩两份，重复数对不上。
    """
    files = []
    spec = [("整理", os.path.join("手工整理", "周杰伦"), 100.0),
            ("下载A", os.path.join("Downloaded", "周杰伦"), 200.0),
            ("下载B", os.path.join("Downloaded", "周杰伦 合辑"), 300.0)]
    for _tag, sub, mtime in spec:
        p = os.path.join(str(library), sub, "周杰伦 - 晴天.mp3")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(b"x" * 4096)
        os.utime(p, (mtime, mtime))
        core.db().execute(
            "INSERT OR REPLACE INTO local_file(path,title,artist,norm_key,mtime,size,format)"
            " VALUES(?,?,?,?,?,?,?)",
            (p, "晴天", "周杰伦", core.tkey("晴天", "周杰伦"), mtime, 4096, "mp3"))
        files.append(p)
    # 同名但大小不同（另一码率），exact 口径不该把它算成重复
    other = os.path.join(str(library), "Downloaded", "周杰伦", "周杰伦 - 晴天.flac")
    os.makedirs(os.path.dirname(other), exist_ok=True)
    with open(other, "wb") as f:
        f.write(b"y" * 9999)
    core.db().execute(
        "INSERT OR REPLACE INTO local_file(path,title,artist,norm_key,mtime,size,format)"
        " VALUES(?,?,?,?,?,?,?)",
        (other, "晴天", "周杰伦", core.tkey("晴天", "周杰伦"), 400.0, 9999, "flac"))
    # 唯一一首歌，用来验证不会被误判
    solo = os.path.join(str(library), "手工整理", "周杰伦", "周杰伦 - 稻香.mp3")
    with open(solo, "wb") as f:
        f.write(b"z" * 2048)
    core.db().execute(
        "INSERT OR REPLACE INTO local_file(path,title,artist,norm_key,mtime,size,format)"
        " VALUES(?,?,?,?,?,?,?)",
        (solo, "稻香", "周杰伦", core.tkey("稻香", "周杰伦"), 500.0, 2048, "mp3"))
    core.db().commit()
    return files, other, solo


class TestDuplicates:
    def test_preview_groups_and_totals(self, client, library):
        _seed_duplicates(library)
        d = client.get("/api/library/duplicates").json()
        assert d["scope"] == "exact"
        assert d["groups_count"] == 1 and d["redundant"] == 2
        assert d["freed_bytes"] == 8192
        g = d["groups"][0]
        assert g["title"] == "晴天"
        # 手工整理的那份优先保留
        assert "手工整理" in g["keep"]["path"]
        assert all("Downloaded" in x["path"] for x in g["duplicates"])

    def test_song_scope_includes_other_bitrate(self, client, library):
        _seed_duplicates(library)
        d = client.get("/api/library/duplicates", params={"scope": "song"}).json()
        assert d["scope"] == "song"
        # 同名的 flac 版本也会进组，共 3 份 mp3 之外的分组仍然按曲名归并
        assert d["groups_count"] == 1
        assert d["redundant"] == 3

    def test_no_duplicates(self, client, library):
        d = client.get("/api/library/duplicates").json()
        assert d["groups_count"] == 0 and d["redundant"] == 0 and d["freed_bytes"] == 0

    def test_dry_run_does_not_touch_disk_or_index(self, client, library):
        files, _other, _solo = _seed_duplicates(library)
        d = client.post("/api/library/dedupe", json={"dry_run": True}).json()
        assert d["dry_run"] is True and d["removed"] == 2 and d["freed_bytes"] == 8192
        assert all(os.path.exists(p) for p in files)
        assert client.get("/api/library").json()["files"] == 5

    def test_dedupe_keeps_one_and_updates_index(self, client, library):
        files, _other, _solo = _seed_duplicates(library)
        d = client.post("/api/library/dedupe", json={"dry_run": False}).json()
        assert d["dry_run"] is False and d["removed"] == 2
        assert not d["failed"]
        assert sum(os.path.exists(p) for p in files) == 1
        assert client.get("/api/library").json()["files"] == 3

    def test_explicit_paths_are_honoured(self, client, library):
        files, _other, _solo = _seed_duplicates(library)
        d = client.post("/api/library/dedupe",
                        json={"paths": [files[1]], "dry_run": False}).json()
        assert d["removed"] == 1
        assert not os.path.exists(files[1]) and os.path.exists(files[0]) and os.path.exists(files[2])

    def test_keep_path_can_never_be_deleted(self, client, library):
        files, _other, _solo = _seed_duplicates(library)
        keep = client.get("/api/library/duplicates").json()["groups"][0]["keep"]["path"]
        d = client.post("/api/library/dedupe", json={"paths": [keep], "dry_run": False}).json()
        assert d["removed"] == 0 and os.path.exists(keep)

    def test_file_outside_library_root_is_skipped(self, client, library, tmp_path):
        _seed_duplicates(library)
        # 与组内大小格式完全相同，只是落在音乐库根目录之外 —— 必须被拦下
        outsider = tmp_path / "outside.mp3"
        outsider.write_bytes(b"q" * 4096)
        core.db().execute(
            "INSERT OR REPLACE INTO local_file(path,title,artist,norm_key,mtime,size,format)"
            " VALUES(?,?,?,?,?,?,?)",
            (str(outsider), "晴天", "周杰伦", core.tkey("晴天", "周杰伦"), 50.0, 4096, "mp3"))
        core.db().commit()
        d = client.post("/api/library/dedupe", json={"dry_run": False}).json()
        assert outsider.exists()
        assert d["removed"] == 2
        assert len(d["failed"]) == 1
        assert "不在音乐库目录内" in d["failed"][0]["error"]

    def test_missing_file_does_not_abort_the_run(self, client, library):
        files, _other, _solo = _seed_duplicates(library)
        os.remove(files[1])                       # 索引还在，文件已被人为删掉
        d = client.post("/api/library/dedupe", json={"dry_run": False}).json()
        assert d["removed"] == 1                  # 另一份仍然删掉了
        assert len(d["failed"]) == 1
        # 删成功的那一行出索引，删失败的还留着
        assert client.get("/api/library").json()["files"] == 4

    def test_async_dedupe_reports_progress_and_finishes(self, client, library):
        files, _other, _solo = _seed_duplicates(library)
        r = client.post("/api/library/dedupe",
                        json={"dry_run": False, "async_run": True})
        assert r.status_code == 200
        for _ in range(200):                      # 后台线程，最多等 20 秒
            st = client.get("/api/library/dedupe/status").json()
            if not st["running"]:
                break
            time.sleep(0.1)
        assert st["running"] is False
        assert st["total"] == 2 and st["removed"] == 2 and st["freed_bytes"] == 8192
        assert sum(os.path.exists(p) for p in files) == 1
        assert client.get("/api/library").json()["files"] == 3

    def test_second_async_dedupe_is_rejected_while_running(self, client, library, monkeypatch):
        _seed_duplicates(library)
        monkeypatch.setattr(resolver, "dedupe",
                            lambda **kw: time.sleep(0.5))
        assert client.post("/api/library/dedupe",
                           json={"dry_run": True, "async_run": True}).status_code == 200
        assert client.post("/api/library/dedupe",
                           json={"dry_run": True, "async_run": True}).status_code == 409
        time.sleep(0.8)


class TestAuth:
    """登录校验。auth.enabled 出厂默认 true，用 locked 夹具显式打开后所有 /api 都要会话（登录相关除外）。"""

    @pytest.fixture
    def locked(self, env, settings):
        # 显式依赖 env：settings 是写 kv 的，得排在建库之后
        settings(auth={"enabled": True, "proxy_header": ""})
        return True

    def _setup(self, client, user="admin", pwd="secret123"):
        return client.post("/api/auth/setup", json={"username": user, "password": pwd})

    # ── 未启用

    def test_default_is_enabled(self, env):
        """出厂配置里没写 auth 节，必须仍然是「要登录」，否则升级即裸奔。"""
        assert auth.auth_enabled() is True

    def test_disabled_keeps_everything_open(self, client):
        assert client.get("/api/status").status_code == 200
        s = client.get("/api/auth/state").json()
        assert s["enabled"] is False and s["authenticated"] is False

    # ── 启用后的拦截

    def test_api_requires_login(self, client, locked):
        r = client.get("/api/status")
        assert r.status_code == 401 and "未登录" in r.json()["detail"]

    def test_login_endpoints_stay_open(self, client, locked):
        assert client.get("/api/auth/state").status_code == 200
        assert client.post("/api/auth/login",
                           json={"username": "x", "password": "y"}).status_code == 401

    def test_health_stays_open(self, client, locked):
        assert client.get("/api/health").status_code == 200

    def test_unauthenticated_reply_is_401_not_302(self, client, locked):
        """XHR 收到 302 会把登录页 HTML 当 JSON 解析，必须是 401。"""
        r = client.get("/api/tasks")
        assert r.status_code == 401

    def test_401_carries_cors_headers(self, client, locked):
        """中间件注册在 CORS 之后（后进先出），401 才带得上 CORS 头。"""
        r = client.get("/api/status", headers={"Origin": "http://example.com"})
        assert r.status_code == 401 and "access-control-allow-origin" in r.headers

    # ── 初始化与登录

    def test_need_setup_when_no_user(self, client, locked):
        s = client.get("/api/auth/state").json()
        assert s["enabled"] is True and s["need_setup"] is True and s["user"] is None

    def test_setup_creates_user_and_session(self, client, locked):
        r = self._setup(client)
        assert r.status_code == 200 and r.json()["user"]["username"] == "admin"
        assert auth.COOKIE in r.cookies
        # 拿到 cookie 后业务接口直接可用
        assert client.get("/api/status").status_code == 200

    def test_setup_cannot_be_replayed(self, client, locked):
        self._setup(client)
        r = self._setup(client, user="hacker")
        assert r.status_code == 400 and "账号已存在" in r.json()["detail"]
        assert auth.get_user("hacker") is None

    def test_setup_rejects_short_password(self, client, locked):
        assert self._setup(client, pwd="123").status_code == 422

    def test_setup_rejected_when_auth_disabled(self, client):
        assert self._setup(client).status_code == 400

    def test_login_with_wrong_password(self, client, locked):
        self._setup(client)
        client.cookies.clear()
        r = client.post("/api/auth/login", json={"username": "admin", "password": "错的口令"})
        assert r.status_code == 401 and "用户名或口令不正确" in r.json()["detail"]

    def test_unknown_user_gives_the_same_message(self, client, locked):
        """不能让攻击者用报错文案区分「用户不存在」与「口令错」。"""
        self._setup(client)
        client.cookies.clear()
        r = client.post("/api/auth/login", json={"username": "查无此人", "password": "whatever"})
        assert r.status_code == 401
        assert r.json()["detail"] == "用户名或口令不正确"

    def test_login_succeeds_after_setup(self, client, locked):
        self._setup(client)
        client.cookies.clear()
        assert client.get("/api/status").status_code == 401
        r = client.post("/api/auth/login", json={"username": "admin", "password": "secret123"})
        assert r.status_code == 200 and client.get("/api/status").status_code == 200

    def test_state_reports_current_user(self, client, locked):
        self._setup(client, user="youze")
        s = client.get("/api/auth/state").json()
        assert s["authenticated"] is True and s["user"]["username"] == "youze"
        assert s["need_setup"] is False

    def test_logout_invalidates_session(self, client, locked):
        self._setup(client)
        token = client.cookies.get(auth.COOKIE)
        assert client.post("/api/auth/logout").status_code == 200
        assert auth.user_of_token(token) is None
        assert client.get("/api/status").status_code == 401

    def test_password_is_not_stored_in_plaintext(self, client, locked):
        self._setup(client, pwd="secret123")
        row = auth.get_user("admin")
        assert "secret123" not in row["password_hash"]
        assert row["password_hash"].startswith("pbkdf2_sha256$")

    def test_salted_hash_differs_per_user(self, client, locked):
        auth.create_user("a", "same-password")
        auth.create_user("b", "same-password")
        assert auth.get_user("a")["password_hash"] != auth.get_user("b")["password_hash"]

    # ── 改口令

    def test_change_password_needs_old_one(self, client, locked):
        self._setup(client)
        r = client.post("/api/auth/password",
                        json={"old_password": "错的", "new_password": "newsecret"})
        assert r.status_code == 400 and "原口令不正确" in r.json()["detail"]

    def test_change_password_kills_all_sessions(self, client, locked):
        self._setup(client)
        r = client.post("/api/auth/password",
                        json={"old_password": "secret123", "new_password": "newsecret"})
        assert r.status_code == 200
        # 当前会话也一并失效
        assert client.get("/api/status").status_code == 401
        client.cookies.clear()
        assert client.post("/api/auth/login",
                           json={"username": "admin", "password": "secret123"}).status_code == 401
        assert client.post("/api/auth/login",
                           json={"username": "admin", "password": "newsecret"}).status_code == 200

    def test_change_password_rejects_short(self, client, locked):
        self._setup(client)
        assert client.post("/api/auth/password",
                           json={"old_password": "secret123",
                                 "new_password": "12345"}).status_code == 422

    # ── 会话过期

    def test_expired_session_is_rejected(self, client, locked):
        self._setup(client)
        core.db().execute("UPDATE auth_session SET expires_at='2000-01-01 00:00:00'")
        core.db().commit()
        assert client.get("/api/status").status_code == 401

    # ── SSO 预留

    def test_proxy_header_is_trusted_when_configured(self, client, env, settings):
        settings(auth={"enabled": True, "proxy_header": "X-Remote-User"})
        r = client.get("/api/status", headers={"X-Remote-User": "alice"})
        assert r.status_code == 200
        s = client.get("/api/auth/state", headers={"X-Remote-User": "alice"}).json()
        assert s["sso_ready"] is True and s["user"]["username"] == "alice"

    def test_proxy_header_ignored_when_not_configured(self, client, locked):
        """没配 proxy_header 时任何人自造这个头都必须无效，否则就是后门。"""
        r = client.get("/api/status", headers={"X-Remote-User": "attacker"})
        assert r.status_code == 401

    # ── 进度通道（WebSocket）

    def test_progress_socket_closed_when_anonymous(self, client, locked):
        """HTTP 中间件管不到 WebSocket，/ws/progress 必须自己校验，否则匿名连接
        能持续收到歌名与文件路径。"""
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect) as ei:
            with client.websocket_connect("/ws/progress"):
                pass
        assert ei.value.code == 4401

    def test_progress_socket_opens_after_login(self, client, locked):
        self._setup(client)
        with client.websocket_connect("/ws/progress") as ws:
            # 连上后服务端先推 hello
            assert ws.receive_json()["type"] == "hello"


class TestScrapeRun:
    """手动刮削运行入口：空任务拒绝、参数持久化、候选 todo 标志。"""

    def test_empty_candidates_rejected(self, client, library):
        """空任务要 400 明说，不能后台跑个 0/0 的空转让用户以为没执行。"""
        r = client.post("/api/scrape/run", json={"scope": "missing"})
        assert r.status_code == 400
        assert "没有待刮削" in r.json()["detail"]

    def test_run_starts_with_candidates(self, client, library, monkeypatch):
        _seed_local_files(library)
        seen = {}

        def fake_files(paths=None, scope="missing", limit=0):
            seen["scope"] = scope
            resolver._set_scrape(running=False, ended_at=resolver.now(),
                                 total=1, done=1, ok=1)
            return {"total": 1, "ok": 1, "failed": 0, "errors": [], "logs": [], "elapsed": 0}

        monkeypatch.setattr(resolver, "scrape_files", fake_files)
        r = client.post("/api/scrape/run", json={"scope": "all"})
        assert r.status_code == 200
        deadline = time.time() + 5
        while time.time() < deadline and not seen:
            time.sleep(0.05)
        assert seen.get("scope") == "all"

    def test_run_options_persisted(self, client, library):
        """刮削范围/数量上限/预览上限随 scraper 节入库，换浏览器不丢。"""
        r = client.put("/api/settings", json={"scraper": {"manual_scope": "all",
                                                          "manual_limit": 50,
                                                          "preview_limit": 300}})
        assert r.status_code == 200
        sc = client.get("/api/settings").json()["scraper"]
        assert sc["manual_scope"] == "all"
        assert sc["manual_limit"] == 50
        assert sc["preview_limit"] == 300

    def test_candidates_todo_flag(self, client, library):
        _seed_local_files(library)
        items = client.get("/api/scrape/candidates",
                           params={"scope": "all"}).json()["items"]
        assert items
        # 造的数据没有封面/歌词，全部应标 todo=True
        assert all(it["todo"] for it in items)

