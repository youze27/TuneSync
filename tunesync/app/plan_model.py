"""推送计划模型：Trigger / Recipe / Delivery 三段式配置的唯一事实来源。

设计见《推送计划重构-需求与设计方案》。要点：

* ``fetch_task.plan_config`` 存结构化 JSON，是唯一事实来源；旧列保留仅供回滚与兼容读取。
* 计划配置经 ``plan_to_legacy()`` 投影成与旧 fetch_task 行同形的 dict 交给
  pipeline，因此流水线内核（下载 / 刮削 / writer）完全不用改。
* 按用户决策，**平台拉取不再有 AI 筛选，也不再有搜索关键词**（搜索词由场景决定），
  **不再有「每日独立歌单」**（歌单名保持源歌单原名）。
"""
from __future__ import annotations

import copy
import json
import re
from datetime import date, datetime, timedelta

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .core import CATMAP, CONFIG, db, log_api

SCHEMA_VERSION = 2

TRIGGER_TYPES = ("daily", "weekly", "monthly", "interval_days", "hourly", "cron")
SOURCE_KINDS = ("ai", "platform")
WEEKDAY_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
PLATFORM_KEYS = ("all", "wy", "tx", "kg", "kw", "mg")


class PlanError(ValueError):
    """计划配置非法，消息可直接展示给用户。"""


# ---------------------------------------------------------------- 配置模板

def default_plan():
    return {
        "schema_version": SCHEMA_VERSION,
        "trigger": {
            "type": "daily",
            "time": "07:00",
            "weekdays": [0, 1, 2, 3, 4],
            "monthday": 1,
            "interval": 3,
            "anchor_date": date.today().isoformat(),
            "cron": "",
            "misfire": "skip",          # skip | run_once
            "grace_minutes": 60,
            "enabled": True,
        },
        "recipe": {
            "source": "platform",       # ai | platform
            "topic": {
                "scenes": ["douyin"],
                "combine": "separate",  # separate | merge
            },
            "platforms": ["all"],
            "instruction": "",          # 仅 ai：给模型的主题补充；平台模式恒为空
            "playlists_per_run": 1,     # 每次执行产出几张（统一语义）
            "tracks_per_playlist": 0,   # 0 = 跟随全局；仅 ai
            "candidate_pool": 120,
            "rotation": "daily",        # 每日轮换 + 全局硬排除（不重样）
        },
        "delivery": {
            "name_template": None,      # None = 用全局 naming；保持源歌单原名
            "targets": [],              # 空 = 全部启用的推送目标
        },
    }


def normalize(raw):
    """补齐缺省字段、清掉已废弃概念，返回干净的配置对象。"""
    base = default_plan()
    if not isinstance(raw, dict):
        return base
    out = copy.deepcopy(base)
    for section in ("trigger", "recipe", "delivery"):
        if isinstance(raw.get(section), dict):
            out[section].update({k: v for k, v in raw[section].items() if k in out[section]})
    if isinstance(raw.get("recipe", {}).get("topic"), dict):
        out["recipe"]["topic"].update(
            {k: v for k, v in raw["recipe"]["topic"].items() if k in out["recipe"]["topic"]})
    out["schema_version"] = SCHEMA_VERSION
    return out


# ---------------------------------------------------------------- 校验

_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def validate(plan):
    """校验配置；有问题直接抛 PlanError（消息给用户看）。"""
    p = normalize(plan)
    tr, rc, dl = p["trigger"], p["recipe"], p["delivery"]

    if tr["type"] not in TRIGGER_TYPES:
        raise PlanError(f"未知触发类型：{tr['type']}")
    if not _TIME.match(str(tr["time"] or "")):
        raise PlanError("执行时间需为 HH:MM（24 小时制）")
    if tr["type"] == "weekly":
        wd = tr["weekdays"] or []
        if not wd or any(not isinstance(x, int) or x < 0 or x > 6 for x in wd):
            raise PlanError("每周执行至少要选一天（0=周一 … 6=周日）")
    if tr["type"] == "monthly":
        md = tr["monthday"]
        if md != "last" and (not isinstance(md, int) or md < 1 or md > 31):
            raise PlanError("每月日期需为 1–31 或 last")
    if tr["type"] in ("interval_days", "hourly"):
        n = int(tr["interval"] or 0)
        if n < 1 or n > (365 if tr["type"] == "interval_days" else 23):
            raise PlanError("间隔数值超出允许范围")
    if tr["type"] == "cron":
        if not str(tr["cron"] or "").strip():
            raise PlanError("自定义模式需要填写 cron 表达式")
        try:
            CronTrigger.from_crontab(str(tr["cron"]).strip())
        except Exception as e:
            raise PlanError(f"cron 表达式非法：{e}")
    if tr["misfire"] not in ("skip", "run_once"):
        raise PlanError("未知错过策略")

    if rc["source"] not in SOURCE_KINDS:
        raise PlanError(f"未知歌单来源：{rc['source']}")
    scenes = rc["topic"]["scenes"] or []
    if not scenes:
        raise PlanError("至少选择一个推送场景")
    unknown = [c for c in scenes if c not in CATMAP]
    if unknown:
        raise PlanError(f"未知场景：{unknown}")
    if rc["topic"]["combine"] not in ("separate", "merge"):
        raise PlanError("未知主题组合方式")
    plats = [x for x in (rc["platforms"] or []) if x in PLATFORM_KEYS]
    if rc["source"] == "platform" and not plats:
        raise PlanError("平台歌单拉取至少要选一个来源平台")
    # all 聚合已含 kg/kw/mg，和具体平台同时出现只会重复请求；
    # 保留更明确的具体平台列表（否则 wy/tx 反而被丢掉）
    if "all" in plats and len(plats) > 1:
        plats = [x for x in plats if x != "all"]
    try:
        n = int(rc["playlists_per_run"] or 0)
    except (TypeError, ValueError):
        n = 0
    if n < 1 or n > 50:
        raise PlanError("每次产出歌单数需为 1–50")
    try:
        t = int(rc["tracks_per_playlist"] or 0)
    except (TypeError, ValueError):
        t = 0
    if t < 0 or t > 200:
        raise PlanError("每歌单曲目数需为 0–200（0=跟随全局）")
    if dl["name_template"] is not None and not str(dl["name_template"]).strip():
        raise PlanError("命名模板不能是空字符串（留空表示用全局设置）")

    p["trigger"]["weekdays"] = sorted(set(tr["weekdays"] or []))
    p["recipe"]["platforms"] = plats or ["all"]
    p["recipe"]["playlists_per_run"] = n
    p["recipe"]["tracks_per_playlist"] = t
    p["recipe"]["instruction"] = str(rc["instruction"] or "").strip() if rc["source"] == "ai" else ""
    return p


# ---------------------------------------------------------------- 触发引擎

def build_trigger(cfg):
    """把结构化触发配置变成 APScheduler trigger。"""
    t = normalize({"trigger": cfg})["trigger"]
    hh, mm = (int(x) for x in t["time"].split(":"))
    kind = t["type"]
    if kind == "cron":
        return CronTrigger.from_crontab(str(t["cron"]).strip())
    if kind == "daily":
        return CronTrigger(hour=hh, minute=mm)
    if kind == "weekly":
        return CronTrigger(day_of_week=",".join(str(x) for x in sorted(t["weekdays"])),
                           hour=hh, minute=mm)
    if kind == "monthly":
        return CronTrigger(day=t["monthday"] if t["monthday"] == "last" else int(t["monthday"]),
                           hour=hh, minute=mm)
    if kind == "hourly":
        return CronTrigger(hour=f"*/{int(t['interval'])}", minute=mm)
    if kind == "interval_days":
        anchor = t["anchor_date"] or date.today().isoformat()
        try:
            start = datetime.strptime(f"{anchor} {t['time']}", "%Y-%m-%d %H:%M")
        except ValueError:
            start = datetime.combine(date.today(), datetime.min.time()).replace(hour=hh, minute=mm)
        return IntervalTrigger(days=max(1, int(t["interval"])), start_date=start)
    raise PlanError(f"未知触发类型：{kind}")


def describe_trigger(cfg):
    """触发配置 → 人话。前端不再各自实现 describeCron。"""
    t = normalize({"trigger": cfg})["trigger"]
    hm = t["time"]
    if t["type"] == "daily":
        return f"每天 {hm}"
    if t["type"] == "weekly":
        return f"每{('、'.join(WEEKDAY_LABELS[x] for x in sorted(t['weekdays'])))} {hm}"
    if t["type"] == "monthly":
        return f"每月 {t['monthday'] if t['monthday'] == 'last' else str(t['monthday']) + ' 日'} {hm}"
    if t["type"] == "interval_days":
        return f"每 {t['interval']} 天 {hm}（自 {t['anchor_date']} 起）"
    if t["type"] == "hourly":
        return f"每 {t['interval']} 小时整点后 {hm.split(':')[1]} 分"
    return f"自定义 cron：{t['cron']}"


def derived_cron(cfg):
    """给旧 cron_expr 列一个可读的派生值（仅供兼容展示，不参与新调度）。"""
    t = normalize({"trigger": cfg})["trigger"]
    if t["type"] == "cron":
        return str(t["cron"]).strip()
    mm, hh = t["time"].split(":")[1], t["time"].split(":")[0]
    if t["type"] == "daily":
        return f"{mm} {hh} * * *"
    if t["type"] == "weekly":
        return f"{mm} {hh} * * {','.join(str(x) for x in sorted(t['weekdays']))}"
    if t["type"] == "monthly":
        return f"{mm} {hh} {t['monthday']} * *"
    if t["type"] == "hourly":
        return f"{mm} */{t['interval']} * * *"
    return f"{mm} {hh} */{t['interval']} * *"


def next_fires(cfg, count=5, tz=None):
    """未来 N 次触发时间（字符串列表）。"""
    import zoneinfo
    try:
        z = zoneinfo.ZoneInfo(tz or CONFIG.get("server", {}).get("timezone", "Asia/Shanghai"))
    except Exception:
        z = None
    trig = build_trigger(cfg)
    now = datetime.now(tz=z) if z else datetime.now()
    out, cur = [], None
    for _ in range(count):
        cur = trig.get_next_fire_time(cur, now)
        if cur is None:
            break
        out.append(cur.strftime("%Y-%m-%d %H:%M"))
    return out


def misfire_options(cfg):
    """(misfire_grace_time, coalesce) —— 错过策略显式化。"""
    t = normalize({"trigger": cfg})["trigger"]
    if t["misfire"] == "run_once":
        return max(60, int(t["grace_minutes"] or 60) * 60), True
    return 1, False


def preview(plan):
    """执行预览：纯计算，不落库、不跑流水线。"""
    p = validate(plan)
    tr, rc = p["trigger"], p["recipe"]
    per_run = 1 if rc["topic"]["combine"] == "merge" else rc["playlists_per_run"]
    tracks = rc["tracks_per_playlist"] or 30
    tpl = p["delivery"]["name_template"] or "{name}"
    return {
        "trigger_text": describe_trigger(tr),
        "next_runs": next_fires(tr, 5),
        "playlists_per_run": per_run,
        "tracks_per_playlist": tracks,
        "max_downloads": per_run * tracks,
        "name_example": tpl.replace("{name}", "示例歌单名")
                           .replace("{category}", "抖音")
                           .replace("{platform}", "酷狗")
                           .replace("{date}", date.today().isoformat()),
        "source_text": "AI 生成" if rc["source"] == "ai" else "平台歌单拉取",
        "search_text": "" if rc["source"] == "ai" else
                       "、".join(CATMAP.get(c, {}).get("name", c) for c in rc["topic"]["scenes"][:3]),
        "warnings": ([f"每次产出 {per_run} 张歌单，上限约 {per_run * tracks} 首下载，建议调小"]
                     if per_run * tracks > 200 else []),
    }


# ---------------------------------------------------------------- 旧数据兼容

def _legacy_trigger(cron_expr, enabled):
    """把旧 cron 尽量翻译成结构化触发；认不出的原样保留为自定义 cron。"""
    t = default_plan()["trigger"]
    t["enabled"] = bool(enabled)
    parts = str(cron_expr or "").strip().split()
    if len(parts) == 6:
        parts = parts[1:]
    if len(parts) != 5:
        t.update({"type": "cron", "cron": str(cron_expr or "0 7 * * *").strip()})
        return t
    mi, hh, dom, mon, dow = parts
    try:
        t["time"] = f"{int(hh):02d}:{int(mi):02d}"
    except ValueError:
        t["time"] = "07:00"
    step = re.fullmatch(r"\*/(\d+)", dom)
    hour_step = re.fullmatch(r"\*/(\d+)", hh)
    if mon == "*" and dom == "*" and dow == "*":
        t["type"] = "daily"
    elif mon == "*" and step and dow == "*":
        t.update({"type": "interval_days", "interval": int(step.group(1)),
                  "anchor_date": date.today().isoformat()})
    elif mon == "*" and dom != "*" and re.fullmatch(r"\d+", dom) and dow == "*":
        t.update({"type": "monthly", "monthday": int(dom)})
    elif mon == "*" and dom == "*" and hour_step and dow == "*":
        t.update({"type": "hourly", "interval": int(hour_step.group(1))})
    elif mon == "*" and dom == "*" and dow != "*" and re.fullmatch(r"[\d,\-]+", dow):
        days = []
        for seg in dow.split(","):
            if "-" in seg:
                a, b = (int(x) for x in seg.split("-")[:2])
                days.extend(range(a, min(b, 6) + 1))
            elif seg.isdigit():
                days.append(int(seg))
        t.update({"type": "weekly", "weekdays": sorted({min(6, d) for d in days}) or [0]})
    else:
        t.update({"type": "cron", "cron": " ".join(parts)})
    return t


def legacy_to_plan(row):
    """旧 fetch_task 行 → 新配置（迁移用）。"""
    r = dict(row)
    p = default_plan()
    p["trigger"] = _legacy_trigger(r.get("cron_expr"), r.get("enabled", 1))

    def _arr(key, fallback):
        v = r.get(key)
        if isinstance(v, list):
            return v
        try:
            v = json.loads(v or "[]")
        except (TypeError, ValueError):
            v = []
        return v or fallback

    mode = str(r.get("push_mode") or "llm")
    ai = mode == "llm"
    p["recipe"].update({
        "source": "ai" if ai else "platform",
        "topic": {
            "scenes": _arr("categories", ["douyin"]),
            "combine": "merge" if r.get("scene_mode") == "merge" else "separate",
        },
        "platforms": _arr("source_platforms", ["all"]),
        # 平台模式不再有搜索关键词：旧 custom_prompt 直接丢弃
        "instruction": str(r.get("custom_prompt") or "").strip() if ai else "",
        "playlists_per_run": 1 if r.get("scene_mode") == "merge" else max(1, int(r.get("count_per") or 1)),
        "tracks_per_playlist": int(r.get("tracks_per") or 0),
    })
    return normalize(p)


def plan_to_legacy(plan, row=None):
    """新配置 → 与旧 fetch_task 行同形的 dict，交给 pipeline 原样执行。

    三条用户决策在这里落地：
    * 平台模式恒为 source_raw（无 AI 筛选）
    * 平台模式 custom_prompt 恒为空 → 搜索词由场景自动决定
    * daily_unique 恒为 0 → 歌单名保持源歌单原名
    """
    p = validate(plan)
    r = dict(row) if row is not None else {}
    tr, rc, dl = p["trigger"], p["recipe"], p["delivery"]
    return {
        "id": r.get("id"),
        "name": r.get("name") or "计划",
        "platforms": "[]",
        "categories": json.dumps(rc["topic"]["scenes"], ensure_ascii=False),
        "count_per": rc["playlists_per_run"],
        "custom_prompt": rc["instruction"] if rc["source"] == "ai" else "",
        "prompt": rc["instruction"] if rc["source"] == "ai" else "",
        "cron_expr": derived_cron(tr),
        "enabled": 1 if tr["enabled"] else 0,
        "push_mode": "llm" if rc["source"] == "ai" else "source_raw",
        "source_platforms": json.dumps(rc["platforms"], ensure_ascii=False),
        "scene_mode": rc["topic"]["combine"],
        "tracks_per": rc["tracks_per_playlist"],
        "daily_unique": 0,
        "name_template": dl["name_template"],
    }


def read_plan(row):
    """读一行：有 plan_config 用它，否则实时从旧列投影（老库兼容）。"""
    r = dict(row)
    raw = r.get("plan_config")
    if raw:
        try:
            return normalize(json.loads(raw)), True
        except (TypeError, ValueError):
            log_api.warning("任务 #%s 的 plan_config 损坏，回退旧列", r.get("id"))
    return legacy_to_plan(r), False


def migrate_all():
    """把还没有 plan_config 的旧计划翻译成新配置。返回迁移条数。"""
    c = db()
    rows = c.execute("SELECT * FROM fetch_task WHERE plan_config IS NULL OR plan_config=''").fetchall()
    n = 0
    for row in rows:
        plan = legacy_to_plan(row)
        c.execute("UPDATE fetch_task SET plan_config=?, schema_version=? WHERE id=?",
                  (json.dumps(plan, ensure_ascii=False), SCHEMA_VERSION, row["id"]))
        log_api.info("计划迁移 #%s「%s」：%s → %s / %s",
                     row["id"], row["name"], row["cron_expr"],
                     describe_trigger(plan["trigger"]), plan["recipe"]["source"])
        n += 1
    if n:
        c.commit()
    return n
