"""APScheduler 封装。

任务不落 APScheduler 的持久化 job store，而是每次启动都从 fetch_task 表重建——
单容器重启后不丢任务，且数据库是唯一事实来源（UI 改完立即生效）。
"""
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import pipeline, resolver, writers
from .core import CONFIG, db, log_api, merged_config, merged_targets

_sched = None
_rescan_lock = threading.Lock()
_cleanup_lock = threading.Lock()


import re

# 星期字段：Unix cron 里 7 和 0 都表示周日，但 APScheduler 的上限是 6，直接传 7 会报错。
# 这里把 7 归一成 0，其余编号沿用 APScheduler 的 0=周一。
_DOW7 = re.compile(r"(^|[\/,])7(?=$|[\/,])")


def parse_cron(expr):
    """接受 5 段（分 时 日 月 周）或 6 段（含秒）两种写法。

    注意星期字段沿用 APScheduler 编号：0=周一 … 6=周日。
    常见的「7=周日」写法会被自动归一，Unix cron 的「0=周日」则是周一的含义。
    """
    expr = (expr or "").strip()
    parts = expr.split()
    if len(parts) == 6:
        expr = " ".join(parts[1:])
    parts = expr.split()
    if len(parts) == 5:
        parts[4] = _DOW7.sub(r"\g<1>0", parts[4])
        expr = " ".join(parts)
    return CronTrigger.from_crontab(expr)


def _run_task(task_id):
    pipeline.run_task(task_id)


def _rescan():
    if _rescan_lock.acquire(blocking=False):
        try:
            resolver.scan_library()
        except Exception as e:
            log_api.warning("增量扫描失败：%s", e)
        finally:
            _rescan_lock.release()


def _probe_sources():
    try:
        resolver.check_all_sources()
    except Exception as e:
        log_api.warning("音源探活失败：%s", e)


def _cleanup_playlists():
    """按生成时间清掉老旧歌单。开关关着就直接返回，不做任何事。"""
    cfg = merged_config().get("playlist_cleanup") or {}
    if not cfg.get("enabled", True):
        return
    keep = max(1, int(cfg.get("keep_days", 30) or 30))
    if not _cleanup_lock.acquire(blocking=False):
        return
    try:
        res = writers.cleanup_old_playlists(merged_targets(), keep)
        if res["total"]:
            log_api.info("定时清理老旧歌单：共删除 %s 张（保留 %s 天）", res["total"], keep)
    except Exception as e:
        log_api.warning("老旧歌单清理失败：%s", e)
    finally:
        _cleanup_lock.release()


_scrape_lock = threading.Lock()


def _auto_scrape():
    """定时刮削：定期补刮缺失字段（scope=missing）。

    正在手动刮削时跳过本轮（scrape_async 自己会拒，这里先查状态避免无意义触发）；
    锁保证调度器重入时不会叠出两轮。
    """
    auto = (merged_config().get("scraper") or {}).get("auto_scrape") or {}
    limit = int(auto.get("limit", 0) or 0)
    if resolver.scrape_state()["running"]:
        log_api.info("定时刮削：已有刮削在跑，本轮跳过")
        return
    if not _scrape_lock.acquire(blocking=False):
        return
    try:
        if not resolver.scrape_candidates(scope="missing", limit=1):
            log_api.info("定时刮削：没有缺失字段的文件，本轮跳过")
            return
        resolver.scrape_async(scope="missing", limit=limit)
        log_api.info("定时刮削已触发（scope=missing, limit=%s）", limit or "不限")
    except Exception as e:
        log_api.warning("定时刮削触发失败：%s", e)
    finally:
        _scrape_lock.release()


def _auto_scrape_cfg():
    """读 scraper.auto_scrape：{enabled, cron, limit}。返回 (enabled, cron_expr)。"""
    auto = (merged_config().get("scraper") or {}).get("auto_scrape") or {}
    enabled = bool(auto.get("enabled", False))
    cron = str(auto.get("cron") or "0 4 * * *").strip()   # 默认每天凌晨 4 点
    return enabled, cron


def _rebuild_auto_scrape():
    """按配置重建定时刮削任务。关闭/表达式非法时只移除不报错。"""
    if _sched is None:
        return
    try:
        _sched.get_job("auto-scrape").remove()
    except Exception:
        pass
    enabled, cron = _auto_scrape_cfg()
    if not enabled:
        return
    try:
        _sched.add_job(_auto_scrape, parse_cron(cron), id="auto-scrape",
                       replace_existing=True, misfire_grace_time=3600,
                       coalesce=True, max_instances=1)
        log_api.info("定时刮削已启用：%s", cron)
    except Exception as e:
        log_api.error("定时刮削的 cron「%s」无效：%s", cron, e)


def rebuild_jobs():
    """按 fetch_task 表重建全部定时任务。"""
    if _sched is None:
        return 0
    for j in list(_sched.get_jobs()):
        if j.id.startswith("task-"):
            j.remove()
    added = 0
    for t in db().execute("SELECT * FROM fetch_task WHERE enabled=1"):
        try:
            _sched.add_job(_run_task, parse_cron(t["cron_expr"]), id=f"task-{t['id']}",
                           args=[t["id"]], replace_existing=True,
                           misfire_grace_time=3600, coalesce=True, max_instances=1)
            added += 1
        except Exception as e:
            log_api.error("任务 #%s 的 cron「%s」无效：%s", t["id"], t["cron_expr"], e)
    return added


def next_run_time(task_id):
    if _sched is None:
        return None
    job = _sched.get_job(f"task-{task_id}")
    if job is None or job.next_run_time is None:
        return None
    return job.next_run_time.strftime("%Y-%m-%d %H:%M:%S")


def reload_config():
    """配置（扫描间隔、探活间隔、老歌单清理）被 UI 改动后重建周期任务。"""
    if _sched is None:
        return
    for job_id, key, default in (("rescan", ("library", "rescan_interval_min"), 10),
                                 ("probe", ("source_health", "probe_interval_min"), 30),
                                 ("playlist-cleanup", ("playlist_cleanup", "interval_min"), 1440)):
        try:
            _sched.get_job(job_id).remove()
        except Exception:
            pass
        minutes = merged_config().get(key[0], {}).get(key[1], default)
        try:
            minutes = max(1, int(minutes))
        except (TypeError, ValueError):
            minutes = default
        _sched.add_job({"rescan": _rescan, "probe": _probe_sources,
                        "playlist-cleanup": _cleanup_playlists}[job_id],
                       "interval", minutes=minutes, id=job_id, max_instances=1)
    _rebuild_auto_scrape()
    rebuild_jobs()


def start():
    global _sched
    if _sched is not None:
        return
    tz = CONFIG.get("server", {}).get("timezone", "Asia/Shanghai")
    _sched = BackgroundScheduler(timezone=tz)
    _sched.start()
    rebuild_jobs()
    cfg = merged_config()
    _sched.add_job(_rescan, "interval",
                   minutes=max(1, cfg.get("library", {}).get("rescan_interval_min", 10)),
                   id="rescan", max_instances=1)
    _sched.add_job(_probe_sources, "interval",
                   minutes=max(1, cfg.get("source_health", {}).get("probe_interval_min", 30)),
                   id="probe", max_instances=1)
    _sched.add_job(_cleanup_playlists, "interval",
                   minutes=max(1, cfg.get("playlist_cleanup", {}).get("interval_min", 1440)),
                   id="playlist-cleanup", max_instances=1)
    _rebuild_auto_scrape()
    log_api.info("调度器已启动，共 %s 个任务", len(_sched.get_jobs()))


def stop():
    global _sched
    if _sched is not None:
        _sched.shutdown(wait=False)
        _sched = None
