"""立刻拉取的端到端验收：在同一进程里起 uvicorn，外部网络全部打桩。

为什么不用已跑着的服务：桩打在别的进程里对运行中的服务无效。
所以这里自己起一份服务端，再对它发真实 HTTP 请求——真实 socket、
真实路由、真实线程，外部依赖才被替换掉。
"""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["TUNESYNC_SKIP_SCAN"] = "1"
os.environ["TUNESYNC_CONFIG"] = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tunesync", "config", "config.yaml")

import uvicorn  # noqa: E402

from tunesync.app import core, fetchers, main, resolver, scraper  # noqa: E402

PORT = 16789
B = f"http://127.0.0.1:{PORT}"

PLAYLISTS = [{"id": "P1", "name": "学习歌单", "cover": "", "count": 2, "url": "u1"}]
TRACKS = [{"title": "要下的歌", "artist": "乙", "sid": "2", "album": "B", "cover": ""}]

ok = 0
fail = []


def check(name, cond, detail=""):
    global ok
    if cond:
        ok += 1
        print(f"  ok   {name}")
    else:
        fail.append(name)
        print(f"  FAIL {name} {detail}")


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(B + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:200]


def _resolve(title, artist="", album="", sid=None):
    return {"status": "downloaded", "path": "/music/乙 - 要下的歌.mp3",
            "source": "打桩音源", "error": None,
            "meta": {"title": title, "artist": artist, "album": album}}


patches = [
    mock.patch.object(fetchers, "list_playlists", return_value=PLAYLISTS),
    mock.patch.object(fetchers, "playlist_tracks", return_value=TRACKS),
    mock.patch.object(resolver, "resolve_track", side_effect=_resolve),
    mock.patch.object(scraper, "scrape", return_value={"tags": True, "cover": True, "lrc": True}),
]
for p in patches:
    p.start()

cfg = uvicorn.Config(main.app, host="127.0.0.1", port=PORT, log_level="warning")
server = uvicorn.Server(cfg)
threading.Thread(target=server.run, daemon=True).start()
for _ in range(100):
    if server.started:
        break
    time.sleep(0.1)
print(f"服务已起：{B}")

try:
    core.kv_set("settings", {
        "preferences": {"platforms": ["netease"], "categories": ["study"], "count_per": 2},
        "platforms": {"netease": {"api_base": "http://api:3000"}}})
    core.kv_set("targets", {"navidrome": {"url": "", "enabled": False},
                            "feiniu": {"enabled": False}})
    conn = core.db()
    conn.execute("DELETE FROM source")
    conn.execute("INSERT INTO source(name,priority,enabled,cfg) VALUES(?,?,?,?)",
                 ("打桩音源", 1, 1, json.dumps(
                     {"search": "http://127.0.0.1:3001/s?kw={kw}",
                      "url": "http://127.0.0.1:3001/u?id={id}"}, ensure_ascii=False)))
    conn.commit()
    before = conn.execute("SELECT COUNT(*) c FROM fetch_task").fetchone()["c"]

    def wait_idle(timeout=40):
        end = time.time() + timeout
        while time.time() < end:
            _, s = call("GET", "/api/status")
            if not s.get("fetching_now"):
                return s
            time.sleep(0.3)
        return call("GET", "/api/status")[1]

    print("\n[空 body → 用服务端已保存的偏好]")
    st, res = call("POST", "/api/fetch-now")
    check("返回 200", st == 200, f"实际 {st} {res}")
    check("ok=true", isinstance(res, dict) and res.get("ok") is True, str(res)[:100])
    s = wait_idle()
    check("fetching_now 已归位", s.get("fetching_now") is False, str(s.get("fetching_now")))
    check("没有卡住的 running", not s.get("running"), str(s.get("running"))[:120])

    print("\n[运行记录]")
    _, last = call("GET", "/api/runs/latest")
    check("有运行记录", last is not None)
    check("task_id 为空", last.get("task_id") is None, str(last.get("task_id")))
    check("任务名=立即拉取", last.get("task_name") == "立即拉取", str(last.get("task_name")))
    check("状态已终结", last.get("status") in ("success", "partial", "failed"),
          str(last.get("status")))
    print(f"  summary = {json.dumps(last.get('summary'), ensure_ascii=False)[:150]}")
    pls = last.get("playlists") or []
    check("有歌单明细", len(pls) >= 1, f"{len(pls)} 条")
    if pls:
        print(f"  {pls[0].get('name')} / {pls[0].get('platform')} / {pls[0].get('category')}")

    _, tasks = call("GET", "/api/tasks")
    check("没有新建计划任务", len(tasks) == before, f"{len(tasks)} vs {before}")

    print("\n[草稿覆盖：用不同参数试跑，且不写回服务端]")
    st, _ = call("POST", "/api/fetch-now",
                 {"platforms": ["netease"], "categories": ["recent"], "count_per": 5})
    check("返回 200", st == 200)
    wait_idle()
    _, last2 = call("GET", "/api/runs/latest")
    cats = {p.get("category") for p in (last2.get("playlists") or [])}
    check("用草稿的「最近更新」", cats == {"最近更新"}, str(cats))
    _, saved = call("GET", "/api/settings")
    check("草稿没写回偏好", saved["preferences"]["categories"] == ["study"],
          str(saved["preferences"]))

    print("\n[校验]")
    st, res = call("POST", "/api/fetch-now", {"platforms": [], "categories": ["study"]})
    check("空平台被拒 400", st == 400, f"实际 {st} {res}")
    check("提示里提到平台", "平台" in str(res), str(res)[:80])
    st, res = call("POST", "/api/fetch-now", {"platforms": ["netease"], "categories": []})
    check("空分类被拒 400", st == 400, f"实际 {st} {res}")

    print("\n[并发保护]")
    st, _ = call("POST", "/api/fetch-now")
    busy, last_st = False, st
    for _ in range(8):
        last_st, res = call("POST", "/api/fetch-now")
        if last_st == 409:
            busy = True
            break
        time.sleep(0.03)
    check("第二次被挡 409", busy, f"实际 {last_st} {res}")
    wait_idle()
    _, s = call("GET", "/api/status")
    check("结束后归位", s.get("fetching_now") is False)
finally:
    server.should_exit = True
    time.sleep(0.6)
    for p in patches:
        p.stop()

print(f"\n{'=' * 52}\n通过 {ok} 项，失败 {len(fail)} 项")
if fail:
    for f in fail:
        print("  - " + f)
    sys.exit(1)
print("立刻拉取链路可用")