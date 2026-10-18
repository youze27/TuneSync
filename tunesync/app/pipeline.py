"""五阶段流水线编排。

Scheduler/Cron 或「立即执行」→ 按推送方式取歌单 → 解析下载 → 刮削 → 等入库 → 写歌单 → 出报告。

推送方式三选一（fetch_task.push_mode）：
  llm          LLM 直接生成歌单（歌名+歌手），完全不使用任何平台歌单
  source_llm   从音源平台拉候选歌单，再让 LLM 按场景与提示词筛掉不合适的
  source_raw   从音源平台拉候选歌单，按平台热度直接取前 N 张，全程不过 LLM

原则（开发文档 2.8）：任何一步失败只降级不中断——单曲失败记 skipped，
单场景失败只记该场景 error，全部失败才把 run 置为 failed。
"""
import hashlib
import json
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from . import platforms, recommender, resolver, scraper, writers
from .core import CATMAP, db, emit, log_pipeline, merged_config, merged_targets, now

PUSH_MODES = ("llm", "source_llm", "source_raw")

# 场景怎么推：separate = 勾了几个场景就跑几轮；merge = 所有场景合成一个主题只跑一轮
SCENE_MODES = ("separate", "merge")

# 两层并发保护是分开的：
#   _running —— 同一件事（同一任务 / 同一次立即推送 / 同一张歌单）不重复触发
#   _slot    —— 全局单工位，不同的推送任务排队串行执行
# 以前只有第一层，两个不同任务可以同时跑，而收尾阶段 _sync_targets 会并发写同一个
# 飞牛 music.db / 同一个 Navidrome 歌单，结果互相覆盖、歌单条目对不上。
_running = set()
_lock = threading.Lock()

_slot = threading.Lock()
_q_lock = threading.Lock()
_waiting = []      # [{guard, label, since}]，按到达顺序
_active = None     # {guard, label, since}，当前正在跑的那一个


# ── 运行日志留档 ──────────────────────────────────────────────
# WS 事件只推给「当时在线」的页面：定时任务半夜跑完，早上打开仪表盘阶段日志
# 就是一片空白。这里给每个 run 留一份内存副本（每类封顶 300 条，总量封顶 50 个
# run，超出按 run_id 从小到大淘汰），前端收到 run_start 或刷新页面时用
# GET /runs/logs 一次性补齐，补完之后 WS 继续增量推。
_run_logs = {}
_run_logs_lock = threading.Lock()
_RUN_LOG_CAP = 300
_RUN_KEEP = 50


def _remember(run_id, kind, payload):
    if not run_id:
        return
    with _run_logs_lock:
        slot = _run_logs.setdefault(int(run_id), {"progress": [], "tracks": []})
        slot[kind].append(payload)
        if len(slot[kind]) > _RUN_LOG_CAP:
            del slot[kind][:len(slot[kind]) - _RUN_LOG_CAP]
        while len(_run_logs) > _RUN_KEEP:      # 老历史淘汰，防长期运行内存涨
            oldest = min(_run_logs)
            if oldest == int(run_id):
                break
            del _run_logs[oldest]


def run_logs(run_ids):
    """按 run_id 批量取留档日志。返回 {run_id: {progress:[], tracks:[]}}。"""
    out = {}
    with _run_logs_lock:
        for rid in run_ids:
            s = _run_logs.get(int(rid))
            if s:
                out[int(rid)] = {k: list(v) for k, v in s.items()}
    return out


def _emit_run(run_id, payload):
    """pipeline 里 progress/track 事件的统一出口：广播 WS + 落一份留档。"""
    emit(payload)
    if payload.get("type") == "progress":
        _remember(run_id, "progress",
                  {"stage": payload.get("stage"), "detail": payload.get("detail")})
    elif payload.get("type") == "track":
        _remember(run_id, "tracks",
                  {k: payload.get(k) for k in
                   ("title", "artist", "status", "source", "file_path", "error")})


def _q_snapshot():
    """取队列快照。调用方必须已持有 _q_lock。"""
    return {"active": dict(_active) if _active else None,
            "waiting": [dict(w) for w in _waiting], "depth": len(_waiting)}


def queue_state():
    """推送队列快照：正在跑的那一个 + 排队的若干个。"""
    with _q_lock:
        return _q_snapshot()


def _enter_slot(guard, label):
    """挂起直到轮到本任务，然后独占全局工位。

    排队而不是并行，是因为推送的收尾阶段会直接写飞牛的 music.db，两个任务同时写
    会互相踩。等前面的跑完自然继续，中间不用用户再点一次。
    """
    global _active   # 赋值即建局部名，漏掉这行 _active 会变成本模块的局部变量
    with _q_lock:
        _waiting.append({"guard": guard, "label": label, "since": now()})
        snap = _q_snapshot()
    emit({"type": "queue", "action": "wait", "guard": guard, "label": label,
          "position": snap["depth"], **snap})
    _slot.acquire()
    try:
        with _q_lock:
            _waiting[:] = [w for w in _waiting if w["guard"] != guard]
            _active = {"guard": guard, "label": label, "since": now()}
            snap = _q_snapshot()
        emit({"type": "queue", "action": "start", "guard": guard, "label": label,
              "position": 0, **snap})
    except BaseException:
        # 记账失败必须把工位还回去，否则调用方的 finally 里的 _leave_slot
        # 根本不会执行（它包在 _enter_slot 之后），整条队列就此卡死。
        # _active 也要一并抹掉：_leave_slot 会拿 guard 去比对 _active，
        # 留着这条残影的话前端会一直显示有个任务在跑，实际没人跑。
        with _q_lock:
            _waiting[:] = [w for w in _waiting if w["guard"] != guard]
            if _active and _active["guard"] == guard:
                _active = None
        _slot.release()
        raise


def _leave_slot(guard):
    global _active
    # 先放行再改状态：_slot 一旦漏放，后面所有推送都堵在这儿，整条队列死锁。
    _slot.release()
    with _q_lock:
        if _active and _active["guard"] == guard:
            _active = None
        snap = _q_snapshot()
    emit({"type": "queue", "action": "end", "guard": guard, **snap})


def is_running(task_id):
    with _lock:
        if task_id == NOW_KEY:
            return NOW_GUARD in _running
        return f"task-{task_id}" in _running


def now_running():
    with _lock:
        return NOW_GUARD in _running


def _acquire(guard):
    """占用运行位。返回 False 表示已有同名的在跑。"""
    with _lock:
        if guard in _running:
            return False
        _running.add(guard)
    return True


def _release(guard):
    with _lock:
        _running.discard(guard)


def run_async(task_id):
    # 占位必须同步完成：放到线程里的话，接口已经返回 200 而运行位还没占上，
    # 这中间的空窗里再点一次就能起第二轮。
    guard = f"task-{task_id}"
    conn = db()
    if not conn.execute("SELECT id FROM fetch_task WHERE id=?", (task_id,)).fetchone():
        return False
    if not _acquire(guard):
        return False
    threading.Thread(target=run_task, args=(task_id, True), name=f"run-{task_id}",
                     daemon=True).start()
    return True


def run_task(task_id, pre_acquired=False):
    """计划任务入口。调度器和 run_async 都走这里。

    pre_acquired=True 表示调用方已同步占好位。不传的话这里自己占，
    因此重入会撞上已占好的位置被挡掉——cron 触发与手动触发撞车时只真跑一次。
    """
    conn = db()
    task = conn.execute("SELECT * FROM fetch_task WHERE id=?", (task_id,)).fetchone()
    if not task:
        log_pipeline.error("任务 #%s 不存在", task_id)
        if pre_acquired:
            _release(f"task-{task_id}")
        return None
    return _execute(task, task_id, task["name"], f"task-{task_id}", pre_acquired=pre_acquired)


# 「立即拉取」用的固定占位名。它不是 fetch_task 里的 id，但共用同一套并发保护，
# 所以手动连点两次、或手动和定时撞车，都只会有一次真正在跑。
NOW_KEY = "now"
NOW_GUARD = f"now:{NOW_KEY}"


def now_prefs(prefs=None):
    """把一次「立即推送」的参数整理成与 fetch_task 行同形的 dict。

    不落库：手动推送用完即弃，塞进 fetch_task 会在计划任务列表里留垃圾。
    fetch_run.task_id 允许为 NULL，正好用来区分「定时跑的」和「手动跑的」。
    """
    prefs = prefs or merged_config().get("preferences", {}) or {}
    categories = prefs.get("categories") or []
    if not categories:
        raise ValueError("这条推送计划没有勾选任何场景")
    # fetch-now 请求体里叫 prompt（草稿即来即用）；已保存偏好里叫 custom_prompt
    prompt = prefs.get("prompt") or prefs.get("custom_prompt") or ""
    return {
        "id": None,
        "name": "立即推送",
        # fetch_task 的历史遗留列，平台抓取已删，恒为空列表
        "platforms": "[]",
        "categories": json.dumps(categories, ensure_ascii=False),
        "count_per": max(1, int(prefs.get("count_per") or 3)),
        "prompt": str(prompt),
        "push_mode": normalise_push_mode(prefs.get("push_mode")),
        "source_platforms": json.dumps(normalise_source_platforms(
            prefs.get("source_platforms")), ensure_ascii=False),
        "scene_mode": normalise_scene_mode(prefs.get("scene_mode")),
        "tracks_per": max(0, int(prefs.get("tracks_per") or 0)),
    }


def normalise_push_mode(mode):
    mode = str(mode or "").strip()
    return mode if mode in PUSH_MODES else "llm"


def normalise_scene_mode(mode):
    mode = str(mode or "").strip()
    return mode if mode in SCENE_MODES else "separate"


def normalise_source_platforms(platforms_in):
    """来源平台列表，非法项丢弃；空列表等价于聚合。"""
    if isinstance(platforms_in, str):
        platforms_in = [p.strip() for p in platforms_in.split(",") if p.strip()]
    valid = [p for p in (platforms_in or []) if p in platforms.PLATFORM_NAMES]
    return valid or ["all"]


def push_mode_of(task):
    """从 fetch_task 行 / now_prefs dict 里取推送方式，非法值退回 llm。"""
    return normalise_push_mode((task or {}).get("push_mode"))


def scene_mode_of(task):
    """从 fetch_task 行 / now_prefs dict 里取场景推送方式，非法值退回 separate。"""
    return normalise_scene_mode((task or {}).get("scene_mode"))


def tracks_per_of(task):
    """每张歌单的曲目数；0 或非法值表示沿用 llm.tracks_per_playlist 全局配置。"""
    try:
        n = int((task or {}).get("tracks_per") or 0)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def source_platforms_of(task):
    raw = (task or {}).get("source_platforms")
    if isinstance(raw, list):
        return normalise_source_platforms(raw)
    try:
        parsed = json.loads(raw or "[]")
    except (TypeError, ValueError):
        parsed = []
    return normalise_source_platforms(parsed)


def run_now_async(prefs=None):
    """异步触发一次立即拉取，立即返回；进度照常走 WebSocket。"""
    task = now_prefs(prefs)        # 参数校验放在占位之前，失败时不留运行位
    if not _acquire(NOW_GUARD):
        raise RuntimeError("已有一轮拉取正在运行")
    threading.Thread(target=run_now, args=(task, True), name="run-now",
                     daemon=True).start()
    return True


def run_now(task, pre_acquired=False):
    return _execute(task, None, task["name"], NOW_GUARD, pre_acquired=pre_acquired)


def _execute(task, task_id, task_name, guard, pre_acquired=False):
    """所有运行的统一实现：排队 → 记 fetch_run → 跑 → 写状态 → 释放。

    guard 是 _running 里的键，与 task_id 分开：立即拉取没有 task_id，
    但仍然需要和别的运行互斥。pre_acquired 用于调用方已经同步占好位的情形。

    入队等待放在 db() 之前、finally 里统一释放：db() 一抛异常，槽位和运行位
    都必须照常归还，否则后面所有推送都只能干等着。
    """
    if not pre_acquired and not _acquire(guard):
        log_pipeline.info("%s 已在运行中，跳过本次触发", task_name)
        return None
    try:
        _enter_slot(guard, task_name)
        try:
            return _run_body(task, task_id, task_name)
        finally:
            _leave_slot(guard)
    finally:
        _release(guard)


def _run_body(task, task_id, task_name):
    conn = db()
    conn.execute(
        "INSERT INTO fetch_run(task_id, task_name, started_at, status) VALUES(?,?,?, 'running')",
        (task_id, task_name, now()))
    conn.commit()
    run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    emit({"type": "run_start", "run_id": run_id, "task_id": task_id, "task": task_name})
    _remember(run_id, "progress", {"stage": "run", "detail": f"任务「{task_name}」开始运行"})
    try:
        summary = _run(task, run_id)
        # failed = 一个歌单都没打开（平台挂了 / 无结果）；否则有错就是 partial。
        # 「歌单打开了但取详情失败」属于跑过了一部分，要和「完全没跑成」区分开。
        nothing_usable = summary.get("playlists", 0) == 0 and summary.get("attempted", 0) == 0
        status = ("failed" if nothing_usable and summary.get("errors")
                  else ("partial" if summary.get("errors") else "success"))
        conn.execute("UPDATE fetch_run SET status=?, finished_at=?, summary=? WHERE id=?",
                     (status, now(), json.dumps(summary, ensure_ascii=False), run_id))
    except Exception as e:
        log_pipeline.error("流水线异常：%s", e)
        log_pipeline.debug(traceback.format_exc())
        summary = {"fatal": str(e)[:300]}
        conn.execute("UPDATE fetch_run SET status='failed', finished_at=?, summary=? WHERE id=?",
                     (now(), json.dumps(summary, ensure_ascii=False), run_id))
    try:
        conn.commit()
    except Exception:
        pass
    emit({"type": "run_end", "run_id": run_id, "summary": summary})
    _remember(run_id, "progress", {"stage": "end", "detail": f"运行结束：{json.dumps(summary, ensure_ascii=False)[:160]}"})
    log_pipeline.info("运行 #%s 结束：%s", run_id, summary)
    return run_id


def _concurrency():
    return max(1, int(merged_config().get("download", {}).get("concurrency", 5) or 5))


def _retry_max():
    """下载失败重试次数（不含首次），默认 2 = 最多尝试 3 次。"""
    return max(0, int(merged_config().get("download", {}).get("retry", 2) or 0))


def _resolve_one(tr, ctx, job_id, index=0):
    """解析 + 刮削单首歌。下载成功后立刻刮削，不等整批下载完。

    失败重试：skipped（所有音源都没下成）时整链重试，最多 retry+1 次。
    音源限流/网络抖动大多是瞬时的，下一轮大概率就通了；每次重试间隔
    递增（2s/4s/…封顶 8s），避免贴着限流打。
    """
    title, artist = tr.get("title", ""), tr.get("artist", "")
    attempts = 1 + _retry_max()
    result = {"status": "skipped", "path": None, "source": None,
              "error": "未开始", "meta": {}}
    for attempt in range(1, attempts + 1):
        try:
            result = resolver.resolve_track(title, artist, tr.get("album", ""), tr.get("sid"))
        except Exception as e:
            result = {"status": "skipped", "path": None, "source": None,
                      "error": f"解析异常：{str(e)[:120]}", "meta": {}}
        if result["status"] != "skipped":
            break
        if attempt < attempts:
            _emit_run(ctx["run_id"], {"type": "progress", "stage": "retry", "run_id": ctx["run_id"],
                  "run_playlist_id": ctx["rpid"], "job_id": job_id,
                  "detail": f"{title} - {artist} 第 {attempt} 次下载失败，{min(2 * attempt, 8)}s 后重试"})
            time.sleep(min(2 * attempt, 8))
    if attempts > 1 and result["status"] == "skipped":
        result["error"] = f"重试 {attempts} 次仍失败：" + (result.get("error") or "")
    result["track"] = tr                      # as_completed 会乱序，必须带上原曲
    result["_i"] = index
    _update_job(job_id, result["status"], result.get("source"), result.get("path"),
                result.get("error"))
    if result["status"] == "downloaded":
        _emit_run(ctx["run_id"], {"type": "progress", "stage": "scrape", "run_id": ctx["run_id"],
                                  "run_playlist_id": ctx["rpid"], "job_id": job_id,
                                  "detail": f"{title} - {artist}"})
        detail = scraper.scrape(result["path"], result["meta"], tr.get("sid"))
        result["scrape"] = detail
        # detail["meta"] 里的 lrc="1" 就是「有歌词」标记，write_scrape_tags 会存进索引
        resolver.write_scrape_tags(result["path"], detail.get("meta") or {})
        got = scraper.label_hits(detail.get("hits") or {}) or ["无新增字段"]
        _emit_run(ctx["run_id"], {"type": "progress", "stage": "scrape", "run_id": ctx["run_id"],
                                  "run_playlist_id": ctx["rpid"], "job_id": job_id,
                                  "detail": f"{title} · " + "、".join(got)})
        _update_job(job_id, "done", result["source"], result["path"], None)
    _emit_run(ctx["run_id"], {"type": "track", "run_id": ctx["run_id"], "run_playlist_id": ctx["rpid"],
                              "job_id": job_id, "title": title, "artist": artist, "status": result["status"],
                              "source": result["source"], "file_path": result["path"],
                              "error": result.get("error")})
    return result


def _create_jobs(rpid, tracks):
    """按歌单顺序先建好审计行。

    worker 是并发完成的，若让每个 worker 自己 INSERT，运行历史里曲目顺序就是随机的；
    先占位再 UPDATE 才能保证与歌单顺序一致（运行历史页面直接按 id 排序展示）。
    """
    conn = db()
    job_ids = []
    for tr in tracks:
        conn.execute(
            "INSERT INTO download_job(run_playlist_id,title,artist,status,created_at)"
            " VALUES(?,?,?,'pending',?)",
            (rpid, tr.get("title", ""), tr.get("artist", ""), now()))
        job_ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()
    return job_ids


def _update_job(job_id, status, source=None, path=None, error=None):
    if not job_id:
        return
    conn = db()
    conn.execute("UPDATE download_job SET status=?, source=COALESCE(?,source), "
                 "file_path=COALESCE(?,file_path), error=? WHERE id=?",
                 (status, source, path, error, job_id))
    conn.commit()


def _process_playlist(run_id, rpid, platform, category, playlist, summary, pool):
    conn = db()
    conn.execute("UPDATE run_playlist SET status='processing' WHERE id=?", (rpid,))
    conn.commit()

    # 三种推送方式在这一步之后完全一致：曲目在取歌单时就带齐了（LLM 生成
    # 或平台详情），无需二次取详情。
    tracks = playlist.get("tracks") or []
    if not tracks:
        err = "该歌单没有曲目"
        conn.execute("UPDATE run_playlist SET status='failed', error=? WHERE id=?",
                     (err, rpid))
        conn.commit()
        summary["errors"].append(f"[{platform}] {playlist['name']}：{err}")
        _emit_run(run_id, {"type": "progress", "stage": "fetch-error", "run_id": run_id,
                           "detail": f"{platform}/{playlist['name']}: {err}"})
        return None

    # 总进度期望值：每个歌单开跑时把它的曲目数累加进「已知的总量」。
    # 前端进度条 = 已处理首数 / 期望总量，新歌单开跑时百分比会回跳一点，
    # 这是诚实进度（总曲数要等场景全部拉完才能定），比 0→100 直跳可读得多。
    emit({"type": "run_expected", "run_id": run_id, "add": len(tracks)})

    ctx = {"run_id": run_id, "rpid": rpid}
    job_ids = _create_jobs(rpid, tracks)
    results = []
    indexed = list(enumerate(tracks))
    futures = {pool.submit(_resolve_one, tr, ctx, job_ids[i], i): i
               for i, tr in indexed}

    def _skipped_result(i, err):
        return {"status": "skipped", "path": None, "source": None,
                "error": err, "track": tracks[i], "_i": i}

    # 超时机制：as_completed 遇上一首永远不返回的歌会整张歌单卡死（进度再也不动）。
    # 改成手动巡检：单首超过 track_timeout 记超时失败；全部停滞超过 stall_timeout
    # 看门狗收尾，剩余一律记失败。被杀不掉的线程靠请求自身的 TIMEOUT 自然消亡。
    track_timeout = max(60, int(merged_config().get("download", {}).get("track_timeout", 300) or 300))
    stall_timeout = max(track_timeout, int(merged_config().get("download", {}).get("stall_timeout", 900) or 900))
    started = {f: time.time() for f in futures}
    pending = set(futures)
    last_beat = time.time()
    while pending:
        progressed = False
        for fut in list(pending):
            i = futures[fut]
            if fut.done():
                pending.discard(fut)
                progressed = True
                try:
                    results.append(fut.result())
                except Exception as e:   # 理论上 _resolve_one 已兜底，这里是最后一道保险
                    results.append(_skipped_result(i, f"任务异常：{str(e)[:120]}"))
            elif time.time() - started[fut] > track_timeout:
                pending.discard(fut)
                progressed = True
                err = f"下载超时（单首超过 {track_timeout}s）"
                results.append(_skipped_result(i, err))
                _update_job(job_ids[i], "skipped", None, None, err)
                _emit_run(run_id, {"type": "track", "run_id": run_id, "run_playlist_id": rpid,
                                   "job_id": job_ids[i], "title": tracks[i].get("title", ""),
                                   "artist": tracks[i].get("artist", ""), "status": "skipped",
                                   "source": None, "file_path": None, "error": err})
        if progressed:
            last_beat = time.time()
        elif time.time() - last_beat > stall_timeout:
            log_pipeline.error("歌单 #%s 停滞超过 %ss，看门狗终止剩余 %s 首",
                               rpid, stall_timeout, len(pending))
            for fut in pending:
                i = futures[fut]
                err = f"推送停滞超过 {stall_timeout}s，任务被看门狗终止"
                results.append(_skipped_result(i, err))
                _update_job(job_ids[i], "skipped", None, None, err)
            pending.clear()
        else:
            time.sleep(0.5)
    results.sort(key=lambda r: r["_i"])   # 恢复成歌单原始顺序

    local_hit = sum(1 for r in results if r["status"] == "local")
    downloaded = sum(1 for r in results if r["status"] == "downloaded")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    summary["playlists"] += 1
    summary["local"] += local_hit
    summary["downloaded"] += downloaded
    summary["skipped"] += skipped
    if skipped:
        summary["errors"].append(
            f"[{platform}] {playlist['name']}：{skipped} 首未匹配到可用音源")

    conn.execute(
        "UPDATE run_playlist SET track_total=?, local_hit=?, downloaded=?, skipped=?,"
        " status='resolved' WHERE id=?",
        (len(tracks), local_hit, downloaded, skipped, rpid))
    conn.commit()

    ordered = [r for r in results if r["status"] != "skipped"]
    pairs = [(r["track"].get("title", ""), r["track"].get("artist", "")) for r in ordered]
    # 同一个文件可能匹配到多首歌（模糊匹配兜底），重复写进 files 会让推送平台
    # 的 playlist_track 撞 UNIQUE(playlist_id, track_id)。
    files = list(dict.fromkeys(r["path"] for r in ordered if r.get("path")))
    return {"name": None, "pairs": pairs, "files": files, "rpid": rpid,
            "platform": platform, "playlist": playlist, "category": category}


def _run(task, run_id):
    conn = db()
    task = dict(task)
    categories = json.loads(task["categories"] or "[]")
    count = max(1, task["count_per"] or 3)
    prompt = str(task.get("custom_prompt") or task.get("prompt") or "").strip()
    naming = merged_config().get("naming", {}).get("playlist", "{name}")
    push_mode = push_mode_of(task)
    source_plats = source_platforms_of(task)
    scene_mode = scene_mode_of(task)
    tracks_per = tracks_per_of(task)
    summary = {"playlists": 0, "local": 0, "downloaded": 0, "skipped": 0,
               "attempted": 0, "errors": []}

    write_jobs = []
    with ThreadPoolExecutor(max_workers=_concurrency(), thread_name_prefix="resolve") as pool:
        for category, scene, cat_name in _scene_rounds(categories, prompt, scene_mode):
            _emit_run(run_id, {"type": "progress", "stage": "fetch", "run_id": run_id,
                               "detail": prompt or scene})
            try:
                if push_mode == "llm":
                    playlists = _fetch_llm_playlists(scene, count, prompt, tracks_per)
                else:
                    playlists = _fetch_source_playlists(push_mode, source_plats,
                                                        scene, count, prompt)
            except Exception as e:
                summary["errors"].append(f"取歌单失败 [{scene}]：{str(e)[:120]}")
                _emit_run(run_id, {"type": "progress", "stage": "fetch-error", "run_id": run_id,
                                   "detail": f"{scene}: {str(e)[:120]}"})
                continue

            for pl in playlists:
                # LLM 生成的歌单没有平台 id，用 场景+歌单名 的摘要当稳定 source_id；
                # 平台歌单直接用 平台+pid。重跑同名歌单是幂等更新而不是无限膨胀。
                if pl["platform"] == "llm":
                    source_id = hashlib.md5(
                        f"{scene}|{pl.get('name', '')}".encode("utf-8")).hexdigest()[:12]
                else:
                    source_id = hashlib.md5(
                        f"{pl['platform']}|{pl['id']}".encode("utf-8")).hexdigest()[:12]
                conn.execute(
                    "INSERT INTO run_playlist(run_id,platform,category,name,cover_url,source_id,"
                    "status) VALUES(?,?,?,?,?,?, 'pending')",
                    (run_id, pl["platform"], cat_name, pl.get("name", ""),
                     pl.get("cover", ""), source_id))
                conn.commit()
                rpid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                summary["attempted"] += 1
                job = _process_playlist(run_id, rpid, pl["platform"], category, pl, summary, pool)
                if not job:
                    continue
                job["name"] = naming.format(category=cat_name, name=pl.get("name", ""),
                                            platform=_platform_label(pl["platform"]))
                conn.execute(
                    "INSERT OR IGNORE INTO playlist(platform,source_id,name,category) VALUES(?,?,?,?)",
                    (pl["platform"], source_id, job["name"], cat_name))
                conn.commit()
                if job["pairs"]:
                    write_jobs.append(job)

    _sync_targets(run_id, write_jobs, summary)
    return summary


def _platform_label(platform):
    return "LLM" if platform == "llm" else platforms.PLATFORM_NAMES.get(platform, platform)


def _scene_rounds(categories, prompt, scene_mode):
    """把勾选的场景摊成若干轮 [(category, scene, cat_name)]。

    separate  每个场景一轮，主题就是场景名（跟老行为一致）。
    merge     所有场景合成一轮，主题是场景名顿号拼接；模型拿到的是一个
              综合主题而不是碎片化的单个场景，歌单之间也更连贯。
    自定义提示词是一种特殊场景：歌单归属记「自定义」，主题以提示词为准。
    """
    cats = [CATMAP.get(code) or {"code": code, "name": code} for code in categories]
    if not cats:
        return []
    if scene_mode == "merge" and len(cats) > 1:
        joined = "、".join(c["name"] for c in cats)
        return [(cats[0], joined, "自定义" if prompt else "综合")]
    return [(c, c["name"], "自定义" if prompt else c["name"]) for c in cats]


def _fetch_llm_playlists(scene, count, prompt, tracks_per=0):
    """方式一：歌单完全由 LLM 生成，曲目也是模型给的歌名+歌手。

    tracks_per 为 0 时沿用 llm.tracks_per_playlist 全局配置。
    """
    playlists = recommender.generate_playlists(
        scene, count, prompt=prompt, tracks_per=tracks_per or None)
    return [dict(p, platform="llm") for p in playlists]


def _fetch_source_playlists(push_mode, source_plats, scene, count, prompt):
    """方式二 / 三：从真实平台歌单广场取候选，按需让 LLM 筛一轮，再补齐曲目。

    source_raw   直接取热度靠前的 count 张，不过 LLM。
    source_llm   送一批候选给 LLM 挑，挑完再逐张取详情。
    """
    keyword = prompt or scene
    candidates, errors = _collect_candidates(source_plats, keyword)
    for plat, msg in errors:
        log_pipeline.warning("候选歌单拉取失败 [%s]：%s", plat, msg)
    if not candidates:
        raise RuntimeError("音源平台没有返回任何候选歌单"
                           + (f"（{len(errors)} 个平台失败）" if errors else ""))

    if push_mode == "source_llm":
        picked = recommender.select_playlists(candidates, scene, count, prompt=prompt)
    else:
        picked = candidates[:count]
    if not picked:
        raise RuntimeError("没有选中任何歌单")

    out = []
    for c in picked:
        pl = _load_source_playlist(c)
        if pl:
            out.append(pl)
        if len(out) >= count:
            break
    if not out and picked:
        # 挑中的这几张恰好是隐私歌单/平台空返回，别整个任务判死，
        # 从候选池里继续往后补，凑满 count 张为止。
        chosen = {(c["platform"], c["id"]) for c in picked}
        for c in candidates:
            if (c["platform"], c["id"]) in chosen:
                continue
            pl = _load_source_playlist(c)
            if pl:
                out.append(pl)
            if len(out) >= count:
                break
    if not out:
        raise RuntimeError(f"{len(picked)} 张候选歌单都没取到曲目（隐私歌单或平台空返回）")
    return out


def _collect_candidates(source_plats, keyword):
    """跨平台聚合候选歌单。单平台失败只记录，不影响其他平台。"""
    want = max(10, int(merged_config().get("push", {}).get("candidate_pool", 30) or 30))
    candidates, errors = [], []
    for plat in source_plats:
        try:
            data = platforms.square(plat, cat=keyword, offset=0, limit=want)
            lists = data.get("lists") or []
            # wy / tx 的 cat 必须是官方分类名或标签 ID，填自定义提示词会返回 0 条。
            # 这时退回平台默认分类，至少还能按热度拿到候选。
            if not lists and plat in ("wy", "tx"):
                fallback = platforms.square(plat, cat=None, offset=0, limit=want)
                if fallback.get("lists"):
                    log_pipeline.info("[%s] 关键词「%s」无结果，改用默认分类", plat, keyword)
                    data, lists = fallback, fallback["lists"]
        except Exception as e:
            errors.append((plat, str(e)[:120]))
            continue
        for item in lists:
            if not item.get("id"):
                continue
            # 聚合来源（all）返回的每一项自带真实 platform（kg/kw/mg），
            # 拿外层的 all 去 detail() 会直接报「不支持的平台：all」，
            # 结果就是一张歌单都取不到。优先用候选自带的平台。
            plat_key = item.get("platform") or plat
            item = dict(item, platform=plat_key)
            # 同一张歌单可能被多个平台收录，按 平台+id 去重
            if not any(c["platform"] == plat_key and c["id"] == item["id"] for c in candidates):
                candidates.append(item)
    return candidates, errors


def _load_source_playlist(candidate):
    """取一张平台歌单的详情，补成与 LLM 歌单同形的结构（带 tracks）。"""
    try:
        detail = platforms.detail(candidate["platform"], str(candidate["id"]))
    except Exception as e:
        log_pipeline.warning("歌单详情拉取失败 [%s#%s]：%s",
                             candidate["platform"], candidate["id"], str(e)[:120])
        return None
    tracks = detail.get("tracks") or []
    if not tracks:
        return None
    return {"id": candidate["id"], "platform": candidate["platform"],
            "name": detail.get("name") or candidate.get("name", ""),
            "cover": detail.get("cover") or candidate.get("cover", ""),
            "tracks": tracks}


def _sync_targets(run_id, jobs, summary):
    """先等各推送平台把新文件入库，再建歌单。"""
    if not jobs:
        return
    targets = merged_targets()
    try:
        built = writers.build_writers(targets)
    except Exception as e:
        summary["errors"].append(f"推送平台配置有误：{str(e)[:120]}")
        return

    for w in built:
        if isinstance(w, writers.NavidromeWriter):
            _emit_run(run_id, {"type": "progress", "stage": "scan", "run_id": run_id,
                               "detail": "等待 Navidrome 扫描新文件"})
            w.start_scan()
            w.wait_scan()
        for job in jobs:
            _emit_run(run_id, {"type": "progress", "stage": "write", "run_id": run_id,
                               "detail": f"{w.name}: {job['name']}"})
            try:
                result = w.upsert(job["name"], job["pairs"], job["files"])
                db().execute("UPDATE run_playlist SET status=? WHERE id=?", (result[:300], job["rpid"]))
            except Exception as e:
                summary["errors"].append(f"同步失败 {w.name}/{job['name']}：{str(e)[:120]}")
                db().execute("UPDATE run_playlist SET status=? WHERE id=?",
                             (f"sync-failed: {str(e)[:200]}", job["rpid"]))
            db().commit()


# ---------------------------------------------------------------- 平台歌单导入
# 与「立即推送」的区别只在曲目来源：计划任务按 push_mode 取歌单，
# 这里固定是用户在歌单广场手动选中的一张平台歌单。
# 下载（_process_playlist）与推送（_sync_targets）完全复用，进度照常走 WebSocket。

def import_running(platform, pid):
    with _lock:
        return f"import:{platform}:{pid}" in _running


def import_playlist_async(platform, pid):
    """后台导入一张平台歌单：拉详情 → 逐首解析下载 → 推送全部已配置推送平台。"""
    guard = f"import:{platform}:{pid}"
    if not _acquire(guard):
        raise RuntimeError("该歌单已有一次导入正在运行")
    threading.Thread(target=_import_execute, args=(platform, pid, guard),
                     name=f"import-{platform}-{pid}", daemon=True).start()
    return True


def _import_execute(platform, pid, guard):
    """与 _execute 同构：同样要排队、同样在 finally 里归还槽位与运行位。"""
    label = f"导入歌单 {platform}#{pid}"
    try:
        _enter_slot(guard, label)
        try:
            return _import_body(platform, pid)
        finally:
            _leave_slot(guard)
    finally:
        _release(guard)


def _import_body(platform, pid):
    conn = db()
    conn.execute(
        "INSERT INTO fetch_run(task_id, task_name, started_at, status) VALUES(?,?,?, 'running')",
        (None, f"导入歌单：{platforms.PLATFORM_NAMES.get(platform, platform)} #{pid}", now()))
    conn.commit()
    run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    emit({"type": "run_start", "run_id": run_id, "task_id": None,
          "task": f"导入歌单 {platform}#{pid}"})
    _remember(run_id, "progress",
              {"stage": "run", "detail": f"导入歌单 {platform}#{pid} 开始运行"})
    try:
        summary = _run_import(platform, pid, run_id)
        nothing_usable = summary.get("playlists", 0) == 0 and summary.get("attempted", 0) == 0
        status = ("failed" if nothing_usable and summary.get("errors")
                  else ("partial" if summary.get("errors") else "success"))
        conn.execute("UPDATE fetch_run SET status=?, finished_at=?, summary=? WHERE id=?",
                     (status, now(), json.dumps(summary, ensure_ascii=False), run_id))
    except Exception as e:
        log_pipeline.error("导入异常：%s", e)
        log_pipeline.debug(traceback.format_exc())
        summary = {"fatal": str(e)[:300]}
        conn.execute("UPDATE fetch_run SET status='failed', finished_at=?, summary=? WHERE id=?",
                     (now(), json.dumps(summary, ensure_ascii=False), run_id))
    try:
        conn.commit()
    except Exception:
        pass
    emit({"type": "run_end", "run_id": run_id, "summary": summary})
    log_pipeline.info("导入运行 #%s 结束：%s", run_id, summary)
    return run_id


def _run_import(platform, pid, run_id):
    conn = db()
    summary = {"playlists": 0, "local": 0, "downloaded": 0, "skipped": 0,
               "attempted": 0, "errors": []}
    naming = merged_config().get("naming", {}).get("playlist", "{name}")
    try:
        detail = platforms.detail(platform, pid)
    except Exception as e:
        summary["errors"].append(f"拉取歌单失败：{str(e)[:200]}")
        _emit_run(run_id, {"type": "progress", "stage": "fetch-error", "run_id": run_id,
                           "detail": str(e)[:120]})
        return summary
    if not detail.get("tracks"):
        summary["errors"].append(
            f"歌单「{detail.get('name') or pid}」没有曲目（隐私歌单或平台空返回）")
        return summary

    # 运行记录改用歌单真名展示（插入时只知道 pid）
    try:
        conn.execute("UPDATE fetch_run SET task_name=? WHERE id=?",
                     (f"导入：{detail.get('name', '')[:60]}", run_id))
        conn.commit()
    except Exception:
        pass

    category = {"code": "platform", "name": "平台歌单"}
    # 平台真实歌单按 pid 幂等：重复导入同一张是更新而不是膨胀
    source_id = hashlib.md5(f"{platform}|{pid}".encode("utf-8")).hexdigest()[:12]
    conn.execute(
        "INSERT INTO run_playlist(run_id,platform,category,name,cover_url,source_id,status)"
        " VALUES(?,?,?,?,?,?, 'pending')",
        (run_id, platform, "平台歌单", detail.get("name", ""), detail.get("cover", ""), source_id))
    conn.commit()
    rpid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    summary["attempted"] += 1

    playlist = {"name": detail.get("name", ""), "cover": detail.get("cover", ""),
                "tracks": detail.get("tracks") or []}
    with ThreadPoolExecutor(max_workers=_concurrency(), thread_name_prefix="resolve") as pool:
        job = _process_playlist(run_id, rpid, platform, category, playlist, summary, pool)
    if not job:
        return summary
    name = naming.format(category="平台歌单", name=detail.get("name", ""),
                         platform=platforms.PLATFORM_NAMES.get(platform, platform))
    job["name"] = name
    conn.execute("INSERT OR IGNORE INTO playlist(platform,source_id,name,category) VALUES(?,?,?,?)",
                 (platform, source_id, name, "平台歌单"))
    conn.commit()
    _sync_targets(run_id, [job], summary)
    return summary
