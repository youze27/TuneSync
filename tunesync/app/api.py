"""REST 路由。前缀 /api，自动文档见 /docs。

契约以本文件为准，前端只依赖这里列出的端点。
"""
import json
import os
import re
import threading
import time

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import auth, lxbridge, pipeline, platforms, recommender, resolver, scheduler, writers
from .core import (CATEGORIES, CATMAP, CONFIG, _apply_host, db, emit, host_alias,
                   kv_get, kv_set, log_api, merged_config, merged_targets, now,
                   running_in_container, source_cfg)

router = APIRouter(prefix="/api")


def current_user(request):
    """取出当前登录用户。走 SSO 反代头优先，其次会话 cookie；未启用则恒为 None。"""
    if not auth.auth_enabled():
        return None
    name = auth.trusted_identity(request.headers)
    if name:
        row = auth.get_user(name)
        return {"id": row["id"], "username": row["username"]} if row else {"id": 0, "username": name}
    return auth.user_of_token(request.cookies.get(auth.COOKIE, ""))

# 星期字段用的是 APScheduler 的编号，不是 Unix cron 的 0=周日。
# 前端生成 cron 必须按这套编号，否则「周日」会被当成「周一」。
CRON_HELP = {
    "fields": "分 时 日 月 周",
    "day_of_week": "0=周一 1=周二 2=周三 3=周四 4=周五 5=周六 6=周日（7 也按周日处理）",
    "example": "0 8 * * 1-5 表示周一到周五早八点",
}


def _run_playlists(run_id):
    return [dict(p) for p in db().execute(
        "SELECT * FROM run_playlist WHERE run_id=? ORDER BY id", (run_id,))]


def _decode(row):
    """把 JSON 文本列还原成对象，供前端直接消费。"""
    out = dict(row)
    for k in ("platforms", "categories", "source_platforms"):
        if k in out and isinstance(out[k], str):
            try:
                out[k] = json.loads(out[k] or "[]")
            except ValueError:
                out[k] = []
    if "summary" in out and isinstance(out["summary"], str):
        try:
            out["summary"] = json.loads(out["summary"] or "{}")
        except ValueError:
            out["summary"] = {}
    return out


# ------------------------------------------------------------------ 分类

@router.get("/categories", tags=["推送场景"])
def categories():
    """预设推送场景（抖音热歌置顶）+ 活动/心情/风格分组。"""
    return CATEGORIES


# ------------------------------------------------------------------ 全局设置

@router.get("/settings", tags=["设置"])
def get_settings():
    """全局配置（config.yaml + UI 覆盖的合并视图）：命名、下载、刮削、LLM、
    新建计划默认值、访问安全。
    """
    cfg = merged_config()
    llm = dict(cfg.get("llm", {}) or {})
    if llm.get("api_key"):
        llm["api_key"] = "***"
    return {
        "naming": cfg.get("naming", {"playlist": "{name}"}),
        "library": cfg.get("library", {}),
        "download": cfg.get("download", {}),
        "scraper": cfg.get("scraper", {}),
        "source_health": cfg.get("source_health", {}),
        "playlist_cleanup": cfg.get("playlist_cleanup", {}),
        "server": cfg.get("server", {}),
        "llm": llm,
        "preferences": merged_config().get("preferences", {}),
        "auth": {
            "enabled": auth.auth_enabled(),
            "proxy_header": auth.auth_proxy_header(),
            "users": auth.count_users(),
        },
        "cron": CRON_HELP,
        "defaults": kv_get("settings") or {},
        # 容器里 127.0.0.1 会被换成宿主地址，这里把结果摊开，
        # 免得「音源一直连不上」时还要去猜地址被换成了什么
        "host": {
            "alias": host_alias(),
            "in_container": running_in_container(),
            "override": os.environ.get("TUNESYNC_HOST", ""),
        },
    }


@router.put("/settings", tags=["设置"])
def put_settings(body: dict):
    """局部覆盖全局配置；未出现的键保持不变。

    preferences 是新建推送计划时的表单默认值（场景 / 自定义提示词 / 每场景歌单数），
    llm 是歌单生成用的模型配置（base_url / api_key / model 等），
    都存库而不是留在浏览器里，换设备/换人也能用，且改完立即生效、无需重启。
    """
    allowed = {"naming", "library", "download", "scraper", "source_health",
               "preferences", "llm", "auth", "playlist_cleanup"}
    bad = set(body) - allowed
    if bad:
        raise HTTPException(400, f"不支持覆盖的配置节：{sorted(bad)}")
    if "preferences" in body:
        _check_preferences(body["preferences"])
    if "llm" in body:
        body["llm"] = _normalize_llm(body["llm"])
    if "auth" in body:
        body["auth"] = _normalize_auth(body["auth"])
    cur = kv_get("settings") or {}
    for k, v in body.items():
        cur[k] = v
    kv_set("settings", cur)
    scheduler.reload_config()
    return get_settings()


def _normalize_llm(llm):
    """llm 节：字段白名单 + 数值归一 + api_key 占位还原。

    api_key 与推送平台口令同语义："***" 或 "" 都表示「不修改」——
    否则前端把掩码 *** 回填进表单再保存，会把字面量 "***" 存成真 key。
    """
    if llm is None:
        llm = {}
    if not isinstance(llm, dict):
        raise HTTPException(400, "llm 配置必须是对象")
    llm = dict(llm)
    allowed = {"base_url", "api_key", "model", "temperature", "tracks_per_playlist",
               "select_pool"}
    bad = set(llm) - allowed
    if bad:
        raise HTTPException(400, f"llm 节不支持的字段：{sorted(bad)}")
    for k in ("base_url", "api_key", "model"):
        if llm.get(k) is not None and not isinstance(llm[k], str):
            raise HTTPException(400, f"llm.{k} 必须是字符串")
    if "temperature" in llm:
        try:
            llm["temperature"] = float(llm["temperature"])
        except (TypeError, ValueError):
            raise HTTPException(400, "llm.temperature 必须是数字")
    for k in ("tracks_per_playlist", "select_pool"):
        if k in llm:
            try:
                llm[k] = max(1, int(llm[k]))
            except (TypeError, ValueError):
                raise HTTPException(400, f"llm.{k} 必须是正整数")
    if llm.get("api_key") in ("***", "", None):
        llm["api_key"] = (merged_config().get("llm") or {}).get("api_key") or ""
    return llm


def _normalize_auth(cfg):
    """auth 节：只认 enabled / proxy_header，并校验请求头名是否合法。"""
    if cfg is None:
        cfg = {}
    if not isinstance(cfg, dict):
        raise HTTPException(400, "auth 配置必须是对象")
    out = {}
    bad = set(cfg) - {"enabled", "proxy_header"}
    if bad:
        raise HTTPException(400, f"auth 节不支持的字段：{sorted(bad)}")
    out["enabled"] = bool(cfg.get("enabled", True))
    header = str(cfg.get("proxy_header") or "").strip()
    if header and not re.fullmatch(r"[A-Za-z0-9-]{1,64}", header):
        raise HTTPException(400, "SSO 身份请求头名只能包含字母、数字和连字符")
    out["proxy_header"] = header
    return out


@router.post("/settings/test-llm", tags=["设置"])
def test_llm(body: dict | None = None):
    """大模型连通性测试：发一个最小对话请求。

    歌单生成 / 智能筛选都走 recommender._chat 同一条路，这里直接复用它，
    测通了那两个功能就能用。返回延迟与模型回复片段，失败给可读原因。

    body 可带 {base_url, api_key, model, temperature} 临时覆盖已保存值——
    前端把表单当前内容传上来，新 Key 不用先保存就能测（先测后存）。
    api_key 传 "***"/空/缺省都表示用已保存的 key。
    """
    override = body if isinstance(body, dict) and body else None
    if not recommender.is_configured(override):
        raise HTTPException(400, "LLM 未配置：请先填写接口地址与模型")
    t0 = time.time()
    try:
        data = recommender._chat('只回复两个字：连通', system="你是连通性测试探针。",
                                 override=override)
        reply = recommender._content(data)
    except recommender.RecommenderError as e:
        raise HTTPException(502, str(e))
    except Exception as e:
        raise HTTPException(502, f"LLM 测试失败：{str(e)[:200]}")
    return {"ok": True, "latency_ms": int((time.time() - t0) * 1000),
            "model": recommender._llm_cfg(override).get("model", ""),
            "reply": reply[:50]}


# ------------------------------------------------------------------ 配置导入导出

_EXPORT_SECTIONS = ("scraper", "download", "naming", "library", "source_health",
                    "playlist_cleanup")
_TASK_COLS = ("name", "platforms", "categories", "count_per", "cron_expr", "enabled",
              "push_mode", "source_platforms", "custom_prompt", "scene_mode", "tracks_per")


@router.get("/config/export", tags=["设置"])
def export_config():
    """导出计划任务 + 关键配置为 JSON 文件内容（前端触发下载）。

    不含 LLM 密钥与推送平台口令——配置文件会在设备间传来传去，
    密钥泄露面太大；导入后需要重新填一次密钥。
    """
    tasks = []
    for r in db().execute("SELECT * FROM fetch_task ORDER BY id"):
        t = _decode(r)
        tasks.append({k: t.get(k) for k in _TASK_COLS})
    cfg = merged_config()
    settings = {k: cfg.get(k) for k in _EXPORT_SECTIONS if cfg.get(k)}
    return {"kind": "tunesync-config", "version": 1, "exported_at": now(),
            "tasks": tasks, "settings": settings}


@router.post("/config/import", tags=["设置"])
def import_config(body: dict):
    """导入 export 出的 JSON：任务按名字存在则更新、不存在则新建；配置节合并覆盖。"""
    if not isinstance(body, dict) or body.get("kind") != "tunesync-config":
        raise HTTPException(400, "不是 TuneSync 导出的配置文件")
    n_new, n_upd = 0, 0
    for t in body.get("tasks") or []:
        if not isinstance(t, dict) or not (t.get("name") or "").strip():
            continue
        try:
            scheduler.parse_cron(str(t.get("cron_expr") or ""))
        except Exception:
            raise HTTPException(400, f"任务「{t.get('name')}」的 cron 表达式无效：{t.get('cron_expr')}")
        vals = [str(t.get("name")).strip(),
                json.dumps(t.get("platforms") or [], ensure_ascii=False),
                json.dumps(t.get("categories") or [], ensure_ascii=False),
                max(1, int(t.get("count_per") or 3)),
                str(t.get("cron_expr") or "").strip(),
                1 if t.get("enabled", True) else 0,
                pipeline.normalise_push_mode(t.get("push_mode") or "llm"),
                json.dumps(pipeline.normalise_source_platforms(t.get("source_platforms") or ["all"]),
                           ensure_ascii=False),
                str(t.get("custom_prompt") or "").strip(),
                pipeline.normalise_scene_mode(t.get("scene_mode") or "separate"),
                int(t.get("tracks_per") or 0)]
        row = db().execute("SELECT id FROM fetch_task WHERE name=?", (vals[0],)).fetchone()
        if row:
            db().execute(
                "UPDATE fetch_task SET name=?,platforms=?,categories=?,count_per=?,cron_expr=?,"
                "enabled=?,push_mode=?,source_platforms=?,custom_prompt=?,scene_mode=?,"
                "tracks_per=? WHERE id=?", (*vals, row["id"]))
            n_upd += 1
        else:
            db().execute(
                "INSERT INTO fetch_task(name,platforms,categories,count_per,cron_expr,enabled,"
                "push_mode,source_platforms,custom_prompt,scene_mode,tracks_per)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)", vals)
            n_new += 1
    db().commit()
    settings = body.get("settings") or {}
    applied = []
    if isinstance(settings, dict):
        cur = kv_get("settings") or {}
        for k in _EXPORT_SECTIONS:
            if isinstance(settings.get(k), dict):
                cur[k] = {**(cur.get(k) or {}), **settings[k]}
                applied.append(k)
        if applied:
            kv_set("settings", cur)
    scheduler.reload_config()   # 内含 rebuild_jobs + 定时刮削重建
    log_api.info("配置导入：任务 新建 %s / 更新 %s，配置节 %s", n_new, n_upd, applied)
    return {"ok": True, "tasks_created": n_new, "tasks_updated": n_upd,
            "settings_applied": applied}


def _check_preferences(pref):
    pref = pref or {}
    for code in pref.get("categories") or []:
        if code not in CATMAP:
            raise HTTPException(400, f"未知场景：{code}")
    n = pref.get("count_per")
    if n is not None and (not isinstance(n, int) or n < 1):
        raise HTTPException(400, "每个场景的歌单数必须是正整数")
    mode = pref.get("push_mode")
    if mode is not None and mode not in pipeline.PUSH_MODES:
        raise HTTPException(400, f"未知推送方式：{mode}")
    plats = pref.get("source_platforms")
    if plats is not None:
        if not isinstance(plats, list):
            raise HTTPException(400, "source_platforms 必须是数组")
        unknown = [p for p in plats if p not in platforms.PLATFORM_NAMES]
        if unknown:
            raise HTTPException(400, f"未知音源平台：{unknown}")


# ------------------------------------------------------------------ 计划任务

class TaskIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    # 历史遗留列：平台抓取已删，恒为空列表，仅为不破坏旧表结构而保留
    platforms: list[str] = Field(default_factory=list)
    categories: list[str]
    count_per: int = Field(default=3, ge=1, le=50)
    # 每个任务自己的自定义提示词（非空时歌单归到「自定义」场景）
    custom_prompt: str = Field(default="", max_length=2000)
    cron_expr: str
    enabled: bool = True
    # llm | source_llm | source_raw
    push_mode: str = "llm"
    # 来源平台列表，仅 push_mode != llm 时有意义
    source_platforms: list[str] = Field(default_factory=lambda: ["all"])
    # separate = 每个场景各推一轮；merge = 所有场景合成一个主题只推一轮
    scene_mode: str = "separate"
    # 每张歌单的曲目数，0 = 沿用 llm.tracks_per_playlist
    tracks_per: int = Field(default=0, ge=0, le=200)


def _check_task(t: TaskIn):
    if not t.categories:
        raise HTTPException(400, "至少选择一个场景")
    unknown = [c for c in t.categories if c not in CATMAP]
    if unknown:
        raise HTTPException(400, f"未知场景：{unknown}")
    if t.push_mode not in pipeline.PUSH_MODES:
        raise HTTPException(400, f"未知推送方式：{t.push_mode}")
    if t.scene_mode not in pipeline.SCENE_MODES:
        raise HTTPException(400, f"未知场景推送方式：{t.scene_mode}")
    bad_plats = [p for p in t.source_platforms if p not in platforms.PLATFORM_NAMES]
    if bad_plats:
        raise HTTPException(400, f"未知音源平台：{bad_plats}")
    try:
        # 必须走 scheduler.parse_cron 而不是 CronTrigger.from_crontab：
        # parse_cron 才是调度器真正使用的解析入口（含 7→周日 的归一），
        # 直接调 from_crontab 会出现「UI 放行、调度时才炸」的不一致。
        scheduler.parse_cron(t.cron_expr)
    except ValueError as e:
        raise HTTPException(400, f"cron 表达式非法（需 5 段）：{e}")


@router.get("/tasks", tags=["计划任务"])
def list_tasks():
    rows = []
    for r in db().execute("SELECT * FROM fetch_task ORDER BY id"):
        t = _decode(r)
        t["enabled"] = bool(t["enabled"])
        t["running"] = pipeline.is_running(t["id"])
        last = db().execute("SELECT * FROM fetch_run WHERE task_id=? ORDER BY id DESC LIMIT 1",
                            (t["id"],)).fetchone()
        t["last_run"] = _decode(last) if last else None
        t["next_run"] = scheduler.next_run_time(t["id"])
        t["category_names"] = [CATMAP.get(c, {}).get("name", c) for c in t["categories"]]
        rows.append(t)
    return rows


@router.post("/tasks", tags=["计划任务"], status_code=201)
def create_task(t: TaskIn):
    _check_task(t)
    db().execute(
        "INSERT INTO fetch_task(name,platforms,categories,count_per,cron_expr,enabled,"
        "push_mode,source_platforms,custom_prompt,scene_mode,tracks_per)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (t.name, json.dumps(t.platforms), json.dumps(t.categories),
         t.count_per, t.cron_expr, int(t.enabled),
         pipeline.normalise_push_mode(t.push_mode),
         json.dumps(pipeline.normalise_source_platforms(t.source_platforms),
                    ensure_ascii=False),
         t.custom_prompt.strip(),
         pipeline.normalise_scene_mode(t.scene_mode),
         int(t.tracks_per or 0)))
    db().commit()
    scheduler.rebuild_jobs()
    log_api.info("新建任务 #%s %s", db().execute("SELECT last_insert_rowid()").fetchone()[0], t.name)
    return {"ok": True, "id": db().execute("SELECT last_insert_rowid()").fetchone()[0]}


@router.put("/tasks/{tid}", tags=["计划任务"])
def update_task(tid: int, t: TaskIn):
    if not db().execute("SELECT id FROM fetch_task WHERE id=?", (tid,)).fetchone():
        raise HTTPException(404, "任务不存在")
    _check_task(t)
    db().execute(
        "UPDATE fetch_task SET name=?,platforms=?,categories=?,count_per=?,cron_expr=?,enabled=?,"
        "push_mode=?,source_platforms=?,custom_prompt=?,scene_mode=?,tracks_per=? WHERE id=?",
        (t.name, json.dumps(t.platforms), json.dumps(t.categories),
         t.count_per, t.cron_expr, int(t.enabled),
         pipeline.normalise_push_mode(t.push_mode),
         json.dumps(pipeline.normalise_source_platforms(t.source_platforms),
                    ensure_ascii=False), t.custom_prompt.strip(),
         pipeline.normalise_scene_mode(t.scene_mode),
         int(t.tracks_per or 0), tid))
    db().commit()
    scheduler.rebuild_jobs()
    return {"ok": True}


@router.delete("/tasks/{tid}", tags=["计划任务"])
def delete_task(tid: int):
    if not db().execute("SELECT id FROM fetch_task WHERE id=?", (tid,)).fetchone():
        raise HTTPException(404, "任务不存在")
    db().execute("DELETE FROM fetch_task WHERE id=?", (tid,))
    db().commit()
    scheduler.rebuild_jobs()
    return {"ok": True}


@router.post("/tasks/{tid}/run", tags=["计划任务"])
def run_task(tid: int):
    """手动「立即执行」，与 Cron 走同一入口。

    推送是全局串行的：别的任务在跑时，这里不再报 409，而是排进队列等它跑完。
    只有同一个任务自己已经在队列里/在跑，才算重复触发。
    """
    if not db().execute("SELECT id FROM fetch_task WHERE id=?", (tid,)).fetchone():
        raise HTTPException(404, "任务不存在")
    if pipeline.is_running(tid):
        raise HTTPException(409, "该任务已在队列中或正在运行")
    pipeline.run_async(tid)
    q = pipeline.queue_state()
    return {"ok": True,
            "detail": "已排入推送队列" if q["depth"] else "已触发后台执行",
            "queue_depth": q["depth"]}


@router.post("/fetch-now", tags=["推送计划"])
def fetch_now(body: dict = None):
    """立刻推送歌单：用默认偏好跑一次，不必等 cron，也不必先建任务。

    body 为空时用服务端存的默认值；带 body 时可以用界面上的草稿先试跑，
    不满意就直接丢掉，不会污染已保存的默认值。
    形状：{"categories":["douyin"],"prompt":"自定义提示词","count_per":1}
    """
    if pipeline.now_running():
        raise HTTPException(409, "已有一轮推送在队列中或正在运行")
    try:
        pipeline.run_now_async(body or None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    q = pipeline.queue_state()
    return {"ok": True, "detail": "已排入推送队列" if q["depth"] else "已开始推送",
            "queue_depth": q["depth"]}


@router.get("/runs/queue", tags=["运行历史"])
def run_queue():
    """推送队列：谁在跑、谁在等。等 WS 断了也能看到排队情况。"""
    return pipeline.queue_state()


@router.get("/runs/logs", tags=["运行历史"])
def run_logs(ids: str):
    """按 run_id 批量取留档的进度/曲目日志（逗号分隔）。

    WS 只推给当时在线的页面；定时任务跑完后才打开仪表盘时，
    靠这个接口把阶段日志和歌曲进度补齐。
    """
    want = []
    for x in (ids or "").split(","):
        try:
            want.append(int(x))
        except ValueError:
            continue
    return pipeline.run_logs(want)


# ------------------------------------------------------------------ 运行历史

@router.get("/runs", tags=["运行历史"])
def runs(limit: int = 50):
    return [_decode(r) for r in db().execute(
        "SELECT * FROM fetch_run ORDER BY id DESC LIMIT ?", (min(limit, 500),))]


@router.get("/runs/latest", tags=["运行历史"])
def latest_run():
    r = db().execute("SELECT * FROM fetch_run ORDER BY id DESC LIMIT 1").fetchone()
    if not r:
        return None
    out = _decode(r)
    out["playlists"] = _run_playlists(out["id"])
    return out


@router.get("/runs/{rid}", tags=["运行历史"])
def run_detail(rid: int):
    r = db().execute("SELECT * FROM fetch_run WHERE id=?", (rid,)).fetchone()
    if not r:
        raise HTTPException(404, "运行记录不存在")
    out = _decode(r)
    pls = _run_playlists(rid)
    for p in pls:
        p["jobs"] = [dict(j) for j in db().execute(
            "SELECT * FROM download_job WHERE run_playlist_id=? ORDER BY id", (p["id"],))]
    out["playlists"] = pls
    return out


# ------------------------------------------------------------------ 音源

class SourceIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    priority: int = Field(default=1, ge=1, le=999)
    enabled: bool = True
    # http-api 通用模板：{kw}=搜索词 {id}=歌曲ID {quality}=音质
    cfg: dict = Field(default_factory=dict)


def _source_rows():
    rows = []
    for r in db().execute("SELECT * FROM source ORDER BY priority, id"):
        s = dict(r)
        s["enabled"] = bool(s["enabled"])
        s["cfg"] = source_cfg(s["cfg"])
        rows.append(s)
    return rows


@router.get("/sources", tags=["音源"])
def list_sources():
    return _source_rows()


@router.post("/sources", tags=["音源"], status_code=201)
def create_source(s: SourceIn):
    _validate_source_cfg(s.cfg)
    if db().execute("SELECT id FROM source WHERE name=?", (s.name,)).fetchone():
        raise HTTPException(409, "音源名称已存在")
    db().execute("INSERT INTO source(name,priority,enabled,cfg) VALUES(?,?,?,?)",
                 (s.name, s.priority, int(s.enabled), json.dumps(s.cfg, ensure_ascii=False)))
    db().commit()
    return {"ok": True, "id": db().execute("SELECT last_insert_rowid()").fetchone()[0]}


def _validate_source_cfg(cfg):
    """http 与 lx 两种音源类型的必填项。

    - http：search + url 两个 URL 模板；
    - lx：search（把歌搜成脚本的 musicInfo）+ script（脚本 URL 或粘贴文本）。
    """
    if (cfg.get("kind") or "http") == "lx":
        if not cfg.get("search"):
            raise HTTPException(400, "LX 音源也需要 search 模板（用于产出脚本的 musicInfo）")
        if not (cfg.get("script") or "").strip():
            raise HTTPException(400, "LX 音源需要 script：填脚本 URL 或直接粘贴脚本文本")
        return
    if not cfg.get("search") or not cfg.get("url"):
        raise HTTPException(400, "cfg 至少需要 search 与 url 两个 URL 模板")


@router.put("/sources/{sid}", tags=["音源"])
def update_source(sid: int, s: SourceIn):
    if not db().execute("SELECT id FROM source WHERE id=?", (sid,)).fetchone():
        raise HTTPException(404, "音源不存在")
    _validate_source_cfg(s.cfg)
    db().execute("UPDATE source SET name=?,priority=?,enabled=?,cfg=? WHERE id=?",
                 (s.name, s.priority, int(s.enabled),
                  json.dumps(s.cfg, ensure_ascii=False), sid))
    db().commit()
    return {"ok": True}


@router.delete("/sources/{sid}", tags=["音源"])
def delete_source(sid: int):
    db().execute("DELETE FROM source WHERE id=?", (sid,))
    db().commit()
    return {"ok": True}


@router.post("/sources/quick-lx", tags=["音源"], status_code=201)
def quick_import_lx(body: dict):
    """落雪式导入：粘贴 LX 音源脚本文本或脚本链接，自动识别导入。

    - URL 输入会原样存进 cfg.script（脚本每 10 分钟跟随上游更新）；
    - 沙箱预检加载，失败直接 400 并带脚本错误；
    - 名称取脚本 @name，重名自动加序号；平台默认 wy，脚本自报不支持时
      自动改选它声明的第一个平台，搜索模板按平台配 GD 音乐台对应 source。
    """
    script = (body.get("script") or "").strip()
    if not script:
        raise HTTPException(400, "请粘贴音源脚本文本或脚本链接")
    return _quick_lx_one(script, (body.get("name") or "").strip(),
                         (body.get("platform") or "").strip())


@router.post("/sources/quick-lx-batch", tags=["音源"], status_code=201)
def quick_import_lx_batch(body: dict):
    """批量落雪导入：一次传多个脚本文本/链接（前端多选 .js 文件）。

    单个失败不影响其他——每个脚本独立沙箱预检，结果逐条返回，
    前端按条提示哪几个成了哪几个为什么没成。
    """
    scripts = body.get("scripts") or []
    if not isinstance(scripts, list) or not scripts:
        raise HTTPException(400, "scripts 必须是非空数组（脚本文本或链接）")
    if len(scripts) > 20:
        raise HTTPException(400, "一次最多导入 20 个脚本")
    platform = (body.get("platform") or "").strip()
    results = []
    ok = 0
    for s in scripts:
        s = (s or "").strip()
        if not s:
            results.append({"ok": False, "error": "脚本内容为空"})
            continue
        try:
            r = _quick_lx_one(s, "", platform)
            results.append({"ok": True, **r})
            ok += 1
        except HTTPException as e:
            results.append({"ok": False, "error": str(e.detail)[:200],
                            "script": s[:60]})
        except Exception as e:
            results.append({"ok": False, "error": str(e)[:200], "script": s[:60]})
    return {"ok": ok, "failed": len(results) - ok, "results": results}


def _quick_lx_one(script, name, platform):
    """单个 LX 脚本导入的完整链路（quick-lx / quick-lx-batch 共用）。"""
    # URL → 拉文本预检（复用 resolver 的缓存）；纯文本直接预检
    try:
        text = resolver._lx_script({"script": script})
        meta = lxbridge.BRIDGE.ensure(text, name="preview")
    except Exception as e:
        raise HTTPException(400, f"脚本无法加载：{str(e)[:200]}")
    finally:
        lxbridge.BRIDGE.stop()          # 预检完释放 runner，别让 preview 驻留
    plats = list(meta or {})
    platform = platform or ("wy" if (not plats or "wy" in plats) else plats[0])
    if platform not in resolver.LX_SEARCH_SOURCES:
        raise HTTPException(400, f"脚本不支持的平台：{platform}（支持：wy/kg/kw/tx/mg）")
    if not name:
        # 社区脚本头注释块普遍很长（扫描 8K）；`$` 非 MULTILINE 匹配的是
        # 字符串结尾而非行尾，跨行注释下会匹配失败，所以按行取再剥 `*/`
        m = re.search(r"@name[ \t]+([^\n\r]+)", text[:8000])
        name = f"LX·{m.group(1).split('*/')[0].strip()[:40]}" if m else "LX·未命名音源"
    base, n = name, 1
    while db().execute("SELECT id FROM source WHERE name=?", (name,)).fetchone():
        n += 1
        name = f"{base}·{n}"
    cfg = {"kind": "lx", "platform": platform, "script": script}
    if platform == "kw":
        # GD 音乐台不支持 source=kuwo，kw 脚本改配酷我官方搜索
        cfg.update({"search": resolver.KUWO_LX_SEARCH, **resolver.KUWO_LX_FIELDS})
    else:
        # 模板里 {kw}/{limit} 是 resolver 填歌名用的占位符，不能 str.format，只换 {src}
        cfg["search"] = resolver.DEFAULT_LX_SEARCH.replace(
            "{src}", resolver.LX_SEARCH_SOURCES[platform])
        cfg["list_path"] = "$"
    prio = (db().execute("SELECT MAX(priority) m FROM source").fetchone()["m"] or 0) + 1
    db().execute("INSERT INTO source(name,priority,enabled,cfg) VALUES(?,?,1,?)",
                 (name, prio, json.dumps(cfg, ensure_ascii=False)))
    db().commit()
    sid = db().execute("SELECT last_insert_rowid()").fetchone()[0]
    log_api.info("快速导入 LX 音源 #%s %s（platform=%s）", sid, name, platform)
    return {"id": sid, "name": name, "platform": platform, "platforms": plats,
            "qualitys": (meta or {}).get(platform, {}).get("qualitys") or []}


# ------------------------------------------------------------------ 歌单导入
# 平台歌单浏览与一键导入：游客直连网易云/QQ，下载后推送全部已配置推送平台。

class PlaylistRef(BaseModel):
    """歌单引用：link 支持分享链接或纯数字 ID；platform 给出时优先使用。"""
    platform: str = ""
    link: str = Field(min_length=1)


def _resolve_ref(body: PlaylistRef):
    try:
        plat, pid = platforms.parse_link(body.link, body.platform)
    except platforms.PlatformError as e:
        raise HTTPException(400, str(e))
    if not plat:
        raise HTTPException(400, "纯数字 ID 需要同时选择平台")
    return plat, pid


@router.get("/playlist/platforms", tags=["歌单导入"])
def playlist_platforms():
    return [{"key": k, "name": v} for k, v in platforms.PLATFORM_NAMES.items()]


@router.get("/playlist/categories", tags=["歌单导入"])
def playlist_categories(platform: str):
    try:
        return platforms.categories(platform)
    except platforms.PlatformError as e:
        raise HTTPException(502, str(e))


@router.get("/playlist/square", tags=["歌单导入"])
def playlist_square(platform: str, cat: str = "", page: int = 1, limit: int = 30):
    try:
        return platforms.square(platform, cat=cat or None,
                                offset=(max(1, page) - 1) * limit,
                                limit=min(max(1, limit), 60))
    except platforms.PlatformError as e:
        raise HTTPException(502, str(e))


@router.post("/playlist/detail", tags=["歌单导入"])
def playlist_detail(body: PlaylistRef):
    plat, pid = _resolve_ref(body)
    try:
        return platforms.detail(plat, pid)
    except platforms.PlatformError as e:
        raise HTTPException(502, str(e))


@router.post("/playlist/import", tags=["歌单导入"], status_code=202)
def playlist_import(body: PlaylistRef):
    """一键导入：后台拉整张歌单 → 解析下载 → 推送 fn + Navidrome。"""
    plat, pid = _resolve_ref(body)
    try:
        pipeline.import_playlist_async(plat, pid)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "platform": plat, "id": pid}


@router.get("/playlist/import/running", tags=["歌单导入"])
def playlist_import_running(platform: str, pid: str):
    return {"running": pipeline.import_running(platform, pid)}


@router.post("/sources/{sid}/check", tags=["音源"])
def check_source(sid: int):
    """手动探活：search → 取链接。"""
    s = db().execute("SELECT * FROM source WHERE id=?", (sid,)).fetchone()
    if not s:
        raise HTTPException(404, "音源不存在")
    ok, err = resolver.probe_source(s)
    resolver.record_probe(sid, ok, err)
    return {"ok": ok, "error": err}


@router.post("/sources/check-all", tags=["音源"])
def check_all_sources():
    threading.Thread(target=resolver.check_all_sources, name="probe-all", daemon=True).start()
    return {"ok": True, "detail": "已在后台检测全部音源"}


@router.get("/health/sources", tags=["音源"])
def source_health():
    """仪表盘的音源健康状态卡。"""
    return [{k: s[k] for k in ("id", "name", "priority", "enabled", "health",
                                "fail_count", "last_error", "last_check")}
            for s in _source_rows()]


# ------------------------------------------------------------------ 推送平台

@router.get("/targets", tags=["推送平台"])
def get_targets():
    """Navidrome / 飞牛音乐连接配置。

    密码逐层掩码——飞牛的网关口令在 cfg["api"]["password"] 里，
    只掩码顶层会把真正的密钥原样送回浏览器。
    """
    return _mask_secrets(merged_targets())


def _mask_secrets(obj, field="password"):
    """递归掩码所有名为 password 的字段。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == field and v:
                out[k] = "***"
            else:
                out[k] = _mask_secrets(v, field)
        return out
    return obj


def _keep_existing_password(old, new, field="password"):
    """password 为 *** 或空串都表示「不修改」，递归处理。

    必须和 _mask_secrets 一样走完整棵树：飞牛网关口令在 cfg["api"]["password"]，
    只判顶层会让前端回填的 *** 被当成真口令存进去，直接打死网关认证。
    """
    if isinstance(new, dict):
        prev = old if isinstance(old, dict) else {}
        out = _deep_merge_dict(prev, new)
        for k, v in new.items():
            if k == field and v in ("***", "", None):
                if k in prev:
                    out[k] = prev[k]
                else:
                    out.pop(k, None)
            elif isinstance(v, dict):
                out[k] = _keep_existing_password(prev.get(k), v, field)
        return out
    merged = _deep_merge_dict(old if isinstance(old, dict) else {}, new)
    if new.get(field) in ("***", "", None):
        if field in old:
            merged[field] = old[field]
        else:
            merged.pop(field, None)
    return merged


def _deep_merge_dict(old, new):
    out = dict(old or {})
    for k, v in (new or {}).items():
        out[k] = _deep_merge_dict(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


@router.put("/targets", tags=["推送平台"])
def put_targets(body: dict):
    """局部覆盖推送平台配置。密码传 "***" 或 "" 都表示保持原值不变。"""
    cur = kv_get("targets") or {}
    for kind, cfg in body.items():
        if kind not in ("navidrome", "feiniu"):
            raise HTTPException(400, f"未知推送平台 {kind}")
        if not isinstance(cfg, dict):
            raise HTTPException(400, f"{kind} 配置必须是对象")
        old = {**(CONFIG.get("targets", {}).get(kind) or {}), **(cur.get(kind) or {})}
        merged = _keep_existing_password(old, cfg)
        cur[kind] = _deep_merge_dict(cur.get(kind) or {}, merged)
    kv_set("targets", cur)
    return get_targets()


@router.post("/targets/{kind}/test", tags=["推送平台"])
def test_target(kind: str, body: dict = None):
    """测试连接。带 body 时用界面上的当前输入试跑，**不写回**已保存的配置。

    不带 body 的老行为是「只测已存的值」——但用户改完地址直接点测试、
    还没保存的话，报错说的是旧地址，报错信息跟屏幕上填的对不上，
    排查起来极其费劲（真实踩过：填了 IPv6 地址，报错却在连 127.0.0.1）。
    """
    if kind not in ("navidrome", "feiniu"):
        raise HTTPException(400, "未知推送平台")
    cfg = merged_targets().get(kind) or {}
    if isinstance(body, dict) and body:
        # 密码栏回填的是 ***，沿用已存的真值；其余字段以界面输入为准
        draft = _keep_existing_password(cfg, _deep_merge_dict(cfg, body))
        cfg = _apply_host(draft)      # 填 127.0.0.1 也要做宿主地址改写
    if not cfg:
        raise HTTPException(404, f"未配置 {kind}")
    try:
        w = writers.build_writers({kind: cfg}, wanted=[kind], respect_enabled=False)[0]
        detail = w.test()
        return {"ok": True, "detail": detail or "连接正常"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


@router.get("/targets/feiniu/schema", tags=["推送平台"])
def feiniu_schema():
    """飞牛 music.db 的表结构指纹校验结果（db 模式前置检查）。"""
    cfg = merged_targets().get("feiniu") or {}
    return writers.FeiniuWriter(cfg).schema_report()


@router.post("/targets/feiniu/fix-covers", tags=["推送平台"])
def feiniu_fix_covers():
    """回填飞牛歌单封面：把借自 track/album 的封面实体复制到 playlist 目录，
    cover_guid 为空的按歌单曲目重选。飞牛 App 只在 playlist 目录找歌单封面。
    """
    cfg = merged_targets().get("feiniu") or {}
    if not cfg:
        raise HTTPException(404, "未配置飞牛目标库")
    try:
        return writers.fix_feiniu_playlist_covers(writers.FeiniuWriter(cfg))
    except RuntimeError as e:
        raise HTTPException(400, str(e))


# ------------------------------------------------------------------ 歌单管理

@router.get("/playlists/targets", tags=["歌单管理"])
def playlist_targets():
    """哪些推送目标可以读/管歌单。前端用它决定显示几个选项卡。"""
    tg = merged_targets()
    out = []
    for key, label in (("navidrome", "Navidrome"), ("feiniu", "飞牛音乐")):
        item = {"key": key, "name": label, "available": False, "error": ""}
        try:
            writers.build_writer(tg, key)
            item["available"] = True
        except Exception as e:
            item["error"] = str(e)[:160]
        out.append(item)
    return {"targets": out}


@router.get("/playlists/{target}", tags=["歌单管理"])
def playlists_list(target: str, days: int = 0):
    """列出目标端已有歌单。days>0 时只回「生成时间早于 days 天前」的老歌单。"""
    try:
        pls = writers.build_writer(merged_targets(), target).list_playlists()
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    if days > 0:
        cutoff = time.time() - days * 86400
        pls = [p for p in pls
               if (p.get("created") or p.get("changed"))
               and (p.get("created") or p.get("changed")) < cutoff]
    return {"target": target, "playlists": pls, "count": len(pls)}


@router.delete("/playlists/{target}/{pid}", tags=["歌单管理"])
def playlist_delete(target: str, pid: str):
    try:
        w = writers.build_writer(merged_targets(), target)
        return {"target": target, "id": pid, "msg": w.delete_playlist(pid)}
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, str(e)[:300])


class CleanupIn(BaseModel):
    keep_days: int = Field(default=0, ge=0)      # 0 表示沿用全局设置里的保留天数
    dry_run: bool = False
    names: list[str] | None = None


@router.post("/playlists/cleanup", tags=["歌单管理"])
def playlists_cleanup(body: CleanupIn):
    """按生成时间清理老旧歌单。dry_run=true 只返回将被删的清单，不动数据。"""
    keep = body.keep_days or int(merged_config().get("playlist_cleanup", {}).get("keep_days", 180) or 180)
    return writers.cleanup_old_playlists(merged_targets(), keep,
                                         dry_run=body.dry_run, only_names=body.names)


# ------------------------------------------------------------------ 本地库

@router.post("/library/rescan", tags=["本地库"])
def rescan_library():
    """手动重建本地索引（后台线程执行）。"""
    threading.Thread(target=resolver.scan_library, name="rescan", daemon=True).start()
    emit({"type": "progress", "stage": "library", "detail": "已触发本地库扫描"})
    return {"ok": True, "detail": "后台扫描已启动"}


@router.post("/library/scan", tags=["本地库"], include_in_schema=False)
def rescan_library_alias():
    return rescan_library()


@router.get("/library", tags=["本地库"])
def library_status():
    r = db().execute("SELECT COUNT(*) c, MAX(scanned_at) t FROM local_file").fetchone()
    return {"files": r["c"], "scanned_at": r["t"]}


@router.get("/library/duplicates", tags=["本地库"])
def library_duplicates(scope: str = "exact"):
    """重复曲目预览。scope=exact 只认同格式同大小的拷贝，song 则把同名不同版本也算上。"""
    groups = resolver.duplicate_groups(scope)
    return {
        "scope": scope,
        "groups": groups,
        "groups_count": len(groups),
        "redundant": sum(len(g["duplicates"]) for g in groups),
        "freed_bytes": sum(sum(d["size"] or 0 for d in g["duplicates"]) for g in groups),
    }


class DedupeIn(BaseModel):
    """paths 留空表示按默认规则处理全部；dry_run 只统计不删；async 走后台线程。"""
    paths: list[str] = Field(default_factory=list)
    dry_run: bool = True
    async_run: bool = False


@router.post("/library/dedupe", tags=["本地库"])
def library_dedupe(body: DedupeIn):
    """删除重复文件并同步本地索引。每组里按规则选定的那一份永远保留。

    async_run=True 时立刻返回，进度用 GET /library/dedupe/status 查。
    几千份文件在 NAS 上删一遍要几分钟，同步等会让 HTTP 先超时。
    """
    if body.async_run:
        try:
            return resolver.dedupe_async(paths=body.paths or None, dry_run=body.dry_run)
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
    return resolver.dedupe(paths=body.paths or None, dry_run=body.dry_run)


@router.get("/library/dedupe/status", tags=["本地库"])
def library_dedupe_status():
    return resolver.dedupe_state()


# ------------------------------------------------------------------ 登录

class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class PasswordIn(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=6, max_length=128)


class SetupIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=6, max_length=128)


@router.get("/auth/state", tags=["登录"])
def auth_state(request: Request):
    """前端启动时问一次：要不要登录、当前是否已登录、是否需要初始化账号。

    未启用鉴权时 enabled=false，前端直接跳过登录页。
    """
    user = current_user(request)
    return {
        "enabled": auth.auth_enabled(),
        "authenticated": user is not None,
        "user": user,
        "need_setup": auth.auth_enabled() and auth.count_users() == 0,
        "sso_ready": bool(auth.trusted_identity(request.headers)),
    }


@router.post("/auth/setup", tags=["登录"])
def auth_setup(body: SetupIn):
    """首次启用鉴权时创建管理员账号。已有任何账号则拒绝，避免被抢注。"""
    if not auth.auth_enabled():
        raise HTTPException(400, "登录校验已关闭（config.yaml 的 auth.enabled 或环境变量 TUNESYNC_AUTH=off）")
    if auth.count_users():
        raise HTTPException(400, "账号已存在，无法重复初始化")
    auth.create_user(body.username, body.password)
    token = auth.issue_session(auth.get_user(body.username)["id"])
    resp = JSONResponse({"ok": True, "user": {"username": body.username}})
    resp.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_DAYS * 86400,
                    httponly=True, samesite="lax", path="/")
    return resp


@router.post("/auth/login", tags=["登录"])
def auth_login(body: LoginIn, response: Response):
    """账号口令登录。失败一律回同一句「用户名或口令不正确」，不透露哪边错了。"""
    user = auth.verify_login(body.username, body.password)
    if not user:
        log_api.warning("登录失败：%s", body.username)
        raise HTTPException(401, "用户名或口令不正确")
    token = auth.issue_session(user["id"])
    auth.touch_login(user["id"])
    response.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_DAYS * 86400,
                        httponly=True, samesite="lax", path="/")
    log_api.info("登录成功：%s", user["username"])
    return {"ok": True, "user": {"username": user["username"]}}


@router.post("/auth/logout", tags=["登录"])
def auth_logout(request: Request, response: Response):
    auth.revoke_session(request.cookies.get(auth.COOKIE, ""))
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


@router.post("/auth/password", tags=["登录"])
def auth_password(body: PasswordIn, request: Request):
    """改口令要验旧口令；改完该用户所有会话立即失效（含当前这条）。"""
    user = current_user(request)
    if not user:
        raise HTTPException(401, "未登录")
    if not auth.change_password(user["id"], body.old_password, body.new_password):
        raise HTTPException(400, "原口令不正确")
    return {"ok": True, "detail": "口令已更新，请重新登录"}


# ------------------------------------------------------------------ 本地乐库

# 排序白名单：sort 参数直接拼进 ORDER BY，不能让调用方传原字符串
_LOCAL_SORTS = {
    "title": "title COLLATE NOCASE",
    "artist": "artist COLLATE NOCASE",
    "album": "album COLLATE NOCASE",
    "genre": "genre COLLATE NOCASE",
    "style": "style COLLATE NOCASE",
    "year": "year",
    "size": "size",
    "mtime": "mtime",
}

_facets_cache = {"key": None, "at": 0.0, "data": None}


def _tag_match(col, value):
    """流派/风格可能写成 "流行; R&B"，按分号拆分后逐个精确匹配。"""
    parts = [p.strip() for p in str(value).replace("，", ";").split(";") if p.strip()]
    if not parts:
        return None
    return " OR ".join([f"{col} LIKE ?"] * len(parts)), [f"%{p}%" for p in parts]


def _local_where(q="", artist="", genre="", style="", year="", fmt=""):
    where, params = ["1=1"], []
    q = (q or "").strip()
    if q:
        like = f"%{q}%"
        where.append("(title LIKE ? OR artist LIKE ? OR path LIKE ?)")
        params += [like, like, like]
    if artist:
        where.append("artist = ?")
        params.append(artist)
    if genre:
        hit = _tag_match("genre", genre)
        if hit:
            where.append(f"({hit[0]})")
            params += hit[1]
    if style:
        hit = _tag_match("style", style)
        if hit:
            where.append(f"({hit[0]})")
            params += hit[1]
    if year:
        where.append("year = ?")
        params.append(int(year))
    if fmt:
        where.append("format = ?")
        params.append(fmt.lower().lstrip("."))
    return " AND ".join(where), params


@router.get("/localfiles", tags=["本地乐库"])
def local_files(q: str = "", artist: str = "", genre: str = "", style: str = "",
                year: str = "", fmt: str = "", sort: str = "artist",
                order: str = "asc", page: int = 1, limit: int = 50):
    """本地乐库浏览：关键词 + 歌手 + 流派 + 风格 + 发行年份 + 格式，分页返回。"""
    where, params = _local_where(q, artist, genre, style, year, fmt)
    col = _LOCAL_SORTS.get(sort, _LOCAL_SORTS["artist"])
    direction = "DESC" if str(order).lower() == "desc" else "ASC"
    page, limit = max(1, page), min(max(1, limit), 200)
    total = db().execute(f"SELECT COUNT(*) c FROM local_file WHERE {where}",
                         params).fetchone()["c"]
    rows = db().execute(
        f"SELECT id, path, title, artist, album, genre, style, year, format, size, mtime "
        f"FROM local_file WHERE {where} "
        f"ORDER BY {col} {direction}, id {direction} LIMIT ? OFFSET ?",
        (*params, limit, (page - 1) * limit)).fetchall()
    return {"items": [dict(r) for r in rows], "total": total,
            "page": page, "limit": limit}


def _build_facets(limit=200):
    """筛选面板的候选值：歌手 / 流派 / 风格 / 年份 / 格式，各自带数量。"""
    top = min(max(1, limit), 500)

    def group(expr, extra=""):
        return [{"name": r["name"], "count": r["c"]} for r in db().execute(
            f"SELECT {expr} AS name, COUNT(*) c FROM local_file {extra} "
            f"GROUP BY name HAVING name<>'' ORDER BY c DESC, name LIMIT {top}")]
    stats = db().execute(
        "SELECT COUNT(*) total, "
        "SUM(CASE WHEN genre IS NOT NULL AND genre<>'' THEN 1 ELSE 0 END) genred, "
        "SUM(CASE WHEN year IS NOT NULL AND year>0 THEN 1 ELSE 0 END) yeared "
        "FROM local_file").fetchone()
    return {
        "total": stats["total"] or 0,
        "scraped": stats["genred"] or 0,
        "artists": group("artist"),
        "genres": group("genre"),
        "styles": group("style"),
        "formats": group("format"),
        "years": [{"name": str(r["y"]), "count": r["c"]} for r in db().execute(
            "SELECT year y, COUNT(*) c FROM local_file WHERE year IS NOT NULL AND year>0 "
            "GROUP BY year ORDER BY year DESC")],
    }


@router.get("/localfiles/facets", tags=["本地乐库"])
def local_facets(refresh: bool = False):
    """筛选候选值。走内存缓存，刷新/索引变动后失效。"""
    key = _local_index_key()
    if not refresh and _facets_cache["key"] == key and _facets_cache["data"]:
        return {**_facets_cache["data"], "cached": True}
    data = _build_facets()
    _facets_cache.update(key=key, at=time.time(), data=data)
    return {**data, "cached": False}


def _local_index_key():
    """索引一变（扫描/刮削回写）缓存就失效，签名取自库里的聚合值。"""
    r = db().execute(
        "SELECT COUNT(*) c, COALESCE(MAX(scanned_at),'') s, "
        "COALESCE(MAX(mtime),0) m, COALESCE(MAX(scraped_at),'') p FROM local_file"
    ).fetchone()
    return (r["c"], r["s"], r["m"], r["p"])


class LocalPlaylistIn(BaseModel):
    """用选中的本地文件建歌单，不下载任何东西，只做重排与推送。"""
    name: str = Field(min_length=1, max_length=64)
    paths: list[str] = Field(min_length=1)


@router.post("/localfiles/playlist", tags=["本地乐库"])
def create_local_playlist(body: LocalPlaylistIn):
    """把选中的本地歌曲组成歌单，写进所有已启用的推送平台。

    与推送流水线共用 writers：飞牛按文件路径反查 track.id，Navidrome 按
    歌名/歌手定位，所以只在推送平台里重排曲目，不产生任何下载。
    """
    found = {}
    for i in range(0, len(body.paths), 400):        # SQLite 变量上限 999
        chunk = body.paths[i:i + 400]
        marks = ",".join("?" * len(chunk))
        for r in db().execute(
                f"SELECT path, title, artist FROM local_file WHERE path IN ({marks})", chunk):
            found[r["path"]] = (r["title"] or "", r["artist"] or "")
    missing = [p for p in body.paths if p not in found]
    if missing:
        raise HTTPException(400, f"{len(missing)} 首歌曲不在本地索引里，请先重新扫描音乐库")

    targets = writers.build_writers(merged_targets())
    if not targets:
        raise HTTPException(400, "没有启用任何推送平台，请先在「推送平台」页配置")
    tracks = [found[p] for p in body.paths]
    results, ok = [], 0
    for w in targets:
        try:
            results.append({"target": w.name, "ok": True, "detail": w.upsert(body.name, tracks, body.paths)})
            ok += 1
        except Exception as e:
            log_api.warning("本地歌单「%s」写入 %s 失败：%s", body.name, w.name, e)
            results.append({"target": w.name, "ok": False, "error": str(e)[:200]})
    if not ok:
        raise HTTPException(502, "全部推送平台写入失败：" + "；".join(
            f"{r['target']}: {r.get('error')}" for r in results)[:300])
    log_api.info("本地歌单「%s」写入 %s 首 → %s", body.name, len(tracks),
                 "、".join(r["target"] for r in results if r["ok"]))
    return {"ok": True, "tracks": len(tracks), "results": results}


# ------------------------------------------------------------------ 刮削设置

@router.get("/scrape/stats", tags=["刮削"])
def scrape_stats():
    """本地库标签完整度概览。"""
    return {**resolver.scrape_stats(), "running": resolver.scrape_state()["running"]}


@router.get("/scrape/candidates", tags=["刮削"])
def scrape_candidates(scope: str = "missing", limit: int = 200):
    """列出待刮削的文件（预检与统计用；前端已改为点开始直接全量跑）。"""
    limit = min(max(1, limit), 1000)
    return {"items": resolver.scrape_candidates(scope=scope, limit=limit),
            "stats": resolver.scrape_stats()}


class ScrapeRunIn(BaseModel):
    # missing 只补缺字段的（流派/年份/封面/歌词/没刮过）/ all 全库重来
    scope: str = "missing"
    limit: int = Field(default=0, ge=0, le=20000)


@router.post("/scrape/run", tags=["刮削"])
def scrape_run(body: ScrapeRunIn):
    """手动批量刮削，立即返回，进度走 WebSocket 或 /scrape/status 轮询。

    一律按「范围 + 上限」全量跑，全自动不支持挑选子集。
    """
    # 先预检候选：空任务直接报错，而不是后台跑了个 0/0 的空转，
    # 用户侧表现成「点了开始没反应」
    probe = resolver.scrape_candidates(scope=body.scope, limit=1)
    if not probe:
        raise HTTPException(400, "没有待刮削的文件：可把范围切到「全库重来」再试")
    try:
        return resolver.scrape_async(scope=body.scope, limit=body.limit)
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@router.get("/scrape/status", tags=["刮削"])
def scrape_status():
    return resolver.scrape_state()


@router.post("/scrape/ai-run", tags=["刮削"])
def scrape_ai_run(body: ScrapeRunIn):
    """AI 批量刮削：用 LLM 补年份/流派/风格，合并成批请求省调用次数。

    单首失败只记错误不放大到整批；进度走 /scrape/ai/status 轮询。
    """
    if not recommender.is_configured():
        raise HTTPException(400, "LLM 未配置：先在「全局设置」页的「LLM 歌单生成」卡片填好接口地址与模型")
    probe = [r for r in resolver.ai_scrape_candidates(scope=body.scope, limit=1)]
    if not probe:
        raise HTTPException(400, "没有需要 AI 补的文件：年份/流派都齐了，或范围内没有文件")
    try:
        return resolver.ai_scrape_async(scope=body.scope, limit=body.limit)
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@router.get("/scrape/ai/status", tags=["刮削"])
def scrape_ai_status():
    return resolver.ai_scrape_state()


# ------------------------------------------------------------------ 状态总览

@router.get("/status", tags=["系统"])
def status():
    """仪表盘首屏：最近一次抓取 + 正在运行的任务 + 音源健康。"""
    last = db().execute("SELECT * FROM fetch_run ORDER BY id DESC LIMIT 1").fetchone()
    running = [dict(r) for r in db().execute(
        "SELECT r.*, t.name task_name FROM fetch_run r LEFT JOIN fetch_task t ON t.id=r.task_id "
        "WHERE r.status='running' ORDER BY r.id")]
    lib = db().execute("SELECT COUNT(*) c FROM local_file").fetchone()["c"]
    q = pipeline.queue_state()
    # 歌单总数 = 历史推送过的全部歌单（playlist 表按 平台+source_id 去重，重跑不膨胀），
    # 只看最近一次 run 的 run_playlist 会随着新运行来回变，用户要的是累计数
    playlists_total = db().execute("SELECT COUNT(*) c FROM playlist").fetchone()["c"]
    # 本地复用累计：所有历史运行加起来复用了多少首本地文件
    local_hit_total = db().execute(
        "SELECT COALESCE(SUM(local_hit),0) c FROM run_playlist").fetchone()["c"]
    return {
        "sources": [{k: s[k] for k in ("id", "name", "priority", "enabled", "health",
                                        "fail_count", "ok_count", "err_count",
                                        "last_error", "last_check")}
                    for s in _source_rows()],
        "last_run": _decode(last) if last else None,
        "running": running,
        # 推送队列：串行执行，界面上要能看到「排了几个」
        "queue": q,
        # 「立即拉取」不挂在任何 fetch_task 下，界面上要靠这个字段禁用按钮，
        # 否则能连点出多次触发
        "fetching_now": pipeline.now_running(),
        "library_files": lib,
        "playlists_total": playlists_total,
        "local_hit_total": local_hit_total,
        "active_jobs": db().execute(
            "SELECT COUNT(*) c FROM download_job WHERE status IN ('pending','downloading','scraping')"
        ).fetchone()["c"],
    }
