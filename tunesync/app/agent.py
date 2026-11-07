"""AI 音乐智能体：记忆 + 流式编排 + 平台检索 / 模型生成。

一次请求产出三类事件（经 SSE 推给首页智能体）：
* token      —— 模型实时输出（思考过程与推荐理由）
* playlists  —— 结构化歌单卡片（含封面、名称、来源平台）
* memory     —— 本轮结束自动压缩出的简短记忆

两种生成模式：fetch=从平台歌单广场检索真实歌单；model=模型结构化生成歌单+曲目。
两种推荐模式：user=把歌单交给用户选；smart=自动创建并推送到已启用的推送平台。
"""
from __future__ import annotations

import hashlib
import json
import re

import requests

from . import platforms, pipeline
from .core import db, log_api, merged_config, now

SEP = "---RESULT---"
MEMORY_LIMIT = 24
CANDIDATE_LIMIT = 5

SYSTEM = """你是 TuneSync 的智能音乐助手，像音乐平台的 AI 歌单策划一样和用户对话。

你会收到「长期记忆」，其中包含用户偏好与最近推荐过的歌单/歌曲。请务必：
1. 尊重用户长期偏好；
2. 主动避开记忆里已经反复出现过的场景、歌单主题与歌曲，不要每次都推荐最流行、最有代表性的那几首，
   要有新鲜感和长尾品味；
3. 用户说「记住/我喜欢/以后别推」这类话时，在结果 JSON 里带一个 preference 字段，
   用一句简短中文写下应长期记住的偏好。

先用 2–4 句自然中文说明你如何理解需求、打算怎么选曲（这段会实时展示给用户），
然后另起一行只写 ---RESULT--- ，之后只输出 JSON，不要 markdown 代码块、不要多余解释。

{mode_rule}

记忆：
{memories}
"""

MODE_FETCH = """JSON 格式：{"keywords":["搜索词1","搜索词2"],"preference":"可选"}
keywords 是给音乐平台歌单广场用的 1–3 个中文检索词，要具体、能搜到对应主题，不要写整句指令。"""
MODE_MODEL = """JSON 格式：{"playlists":[{"name":"歌单名","description":"一句话简介","tracks":[{"title":"歌名","artist":"歌手"}]}],"preference":"可选"}
生成 1 张歌单、20–30 首真实存在的歌曲；歌单名口语化有画面感，不要与记忆里的歌单雷同。"""


def _llm():
    cfg = merged_config().get("llm") or {}
    return {
        "base_url": str(cfg.get("base_url") or "").strip().rstrip("/"),
        "api_key": str(cfg.get("api_key") or "").strip(),
        "model": str(cfg.get("model") or "").strip(),
        "temperature": float(cfg.get("temperature", 0.8)),
    }


def configured():
    c = _llm()
    return bool(c["base_url"] and c["model"])


def _headers(c):
    h = {"Content-Type": "application/json"}
    if c["api_key"]:
        h["Authorization"] = f"Bearer {c['api_key']}"
    return h


def _messages(user_content, system):
    return [{"role": "system", "content": system}, {"role": "user", "content": user_content}]


def _stream_chat(user_content, system):
    """逐段 yield 模型输出的文本增量。"""
    c = _llm()
    body = {"model": c["model"], "messages": _messages(user_content, system),
            "temperature": c["temperature"], "stream": True}
    r = requests.post(f"{c['base_url']}/chat/completions", json=body,
                      headers=_headers(c), stream=True, timeout=180)
    r.raise_for_status()
    for line in r.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            delta = (json.loads(data)["choices"][0]["delta"] or {}).get("content") or ""
        except Exception:
            continue
        if delta:
            yield delta


def _chat_once(user_content, system="你是记忆压缩器，只输出一句话，不要解释。"):
    c = _llm()
    body = {"model": c["model"], "messages": _messages(user_content, system),
            "temperature": 0.3}
    r = requests.post(f"{c['base_url']}/chat/completions", json=body,
                      headers=_headers(c), timeout=90)
    r.raise_for_status()
    return (r.json()["choices"][0]["message"].get("content") or "").strip()


# ---------------------------------------------------------------- 记忆

def memories(limit=MEMORY_LIMIT):
    rows = [dict(r) for r in db().execute(
        "SELECT * FROM agent_memory ORDER BY id DESC LIMIT ?", (limit,))]
    return rows[::-1]


def add_memory(kind, content, payload=None):
    content = str(content or "").strip()
    if not content:
        return None
    db().execute(
        "INSERT INTO agent_memory(kind,content,payload,created_at) VALUES(?,?,?,?)",
        (kind, content[:500], json.dumps(payload or {}, ensure_ascii=False), now()))
    db().commit()
    return db().execute("SELECT last_insert_rowid()").fetchone()[0]


def delete_memory(mid):
    db().execute("DELETE FROM agent_memory WHERE id=?", (mid,))
    db().commit()


def history(limit=30):
    out = []
    for r in memories(200):
        if r["kind"] != "summary":
            continue
        try:
            p = json.loads(r["payload"] or "{}")
        except (TypeError, ValueError):
            p = {}
        out.append({"id": r["id"], "content": r["content"], "created_at": r["created_at"],
                    "playlists": p.get("playlists") or [], "tracks": p.get("tracks") or [],
                    "message": p.get("message") or ""})
    return out[::-1][:limit]


def _memory_block():
    rows = memories(60)
    prefs = [r for r in rows if r["kind"] == "preference"]
    sums = [r for r in rows if r["kind"] == "summary"]
    lines = []
    if prefs:
        lines.append("【用户长期偏好】")
        lines += [f"- {r['content']}" for r in prefs[-10:]]
    if sums:
        lines.append("【最近推荐历史（务必避开雷同）】")
        for r in sums[-12:]:
            lines.append(f"- {r['content']}")
    songs = []
    for r in sums[-8:]:
        try:
            songs += (json.loads(r["payload"] or "{}").get("tracks") or [])
        except (TypeError, ValueError):
            pass
    if songs:
        lines.append("【近期出现过的歌曲，尽量少重复】")
        lines.append("、".join(dict.fromkeys(songs))[:600])
    return "\n".join(lines) if lines else "（暂无记忆，这是第一次对话）"


def summarize(message, items, generate):
    names = [i.get("name") or "" for i in items]
    tracks = []
    for i in items:
        tracks += [t.get("title") or "" for t in (i.get("tracks") or [])][:8]
    prompt = (f"把这次音乐推荐压缩成一句不超过 40 字的中文记忆，包含用户意图与本次主题，"
              f"便于以后避开雷同。只输出这一句。\n"
              f"用户指令：{message}\n歌单：{'、'.join(names)}\n代表曲目：{'、'.join(tracks[:8])}")
    try:
        text = _chat_once(prompt).strip()
    except Exception as e:
        log_api.warning("记忆总结失败，改用规则摘要：%s", e)
        text = f"用户想要「{message[:16]}」，推荐了 {'、'.join(names[:2]) or '歌单'}"
    payload = {"playlists": names, "tracks": [t for t in tracks if t][:30],
               "message": message, "generate": generate}
    add_memory("summary", text[:120], payload)
    return text[:120]


# ---------------------------------------------------------------- 歌单检索 / 生成

def _extract_json(text):
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip(), flags=re.M).strip()
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("模型没有输出结构化结果")
    return json.loads(t[a:b + 1])


def _card(c):
    plat = c.get("platform") or "kg"
    return {"id": str(c.get("id") or ""), "platform": plat,
            "platform_name": platforms.PLATFORM_NAMES.get(plat, plat),
            "name": c.get("name") or "", "cover": c.get("cover") or "",
            "count": int(c.get("count") or 0), "play": int(c.get("play") or 0),
            "creator": c.get("creator") or "", "kind": "platform", "tracks": []}


def fetch_candidates(keywords, limit=CANDIDATE_LIMIT):
    cands, errors = [], []
    for kw in [k for k in (keywords or []) if str(k).strip()][:3]:
        try:
            data = platforms.square("all", cat=str(kw).strip()[:20], offset=0, limit=30)
            for it in data.get("lists") or []:
                if it.get("id"):
                    cands.append(dict(it, platform=it.get("platform") or "kg"))
        except Exception as e:
            errors.append(f"{kw}: {str(e)[:80]}")
    seen, out = set(), []
    for c in cands:
        key = (c["platform"], str(c["id"]))
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    # 复用上一轮的全局硬排除：已推过的歌单排到最后，尽量给新东西
    try:
        out = pipeline._rotate_candidates(out, f"agent|{'|'.join(map(str, keywords or []))}")
    except Exception:
        pass
    return [_card(c) for c in out[:limit]], errors


def generated_items(data):
    items = []
    for i, p in enumerate((data.get("playlists") or [])[:CANDIDATE_LIMIT]):
        if not isinstance(p, dict) or not (p.get("name") or "").strip():
            continue
        tracks = [{"title": str(t.get("title") or "").strip(),
                   "artist": str(t.get("artist") or "").strip()}
                  for t in (p.get("tracks") or []) if isinstance(t, dict)][:60]
        name = str(p["name"]).strip()
        items.append({
            "id": "gen:" + hashlib.md5(f"{name}|{i}".encode("utf-8")).hexdigest()[:10],
            "platform": "model", "platform_name": "AI 生成",
            "name": name, "description": str(p.get("description") or "").strip(),
            "cover": "", "count": len(tracks), "play": 0, "creator": "TuneSync AI",
            "kind": "generated", "tracks": tracks,
        })
    return items


def create_items(items, indexes=None):
    """把选中的歌单写入已启用的推送平台（后台排队执行）。"""
    picked = items if indexes is None else [items[i] for i in indexes
                                            if isinstance(i, int) and 0 <= i < len(items)]
    created, errors = 0, []
    for it in picked:
        try:
            if it.get("kind") == "generated":
                pipeline.import_generated_async(it["name"], it.get("tracks") or [])
            else:
                pipeline.import_playlist_async(it["platform"], it["id"])
            created += 1
        except Exception as e:
            errors.append(f"{it.get('name')}: {str(e)[:100]}")
    return {"created": created, "errors": errors}


# ---------------------------------------------------------------- 编排（SSE 事件生成器）

def run(payload):
    message = str(payload.get("message") or "").strip()
    recommend = payload.get("recommend") if payload.get("recommend") in ("smart", "user") else "user"
    generate = payload.get("generate") if payload.get("generate") in ("fetch", "model") else "fetch"
    if not message:
        yield {"type": "error", "message": "先说说你想要什么歌单"}
        return
    if not configured():
        yield {"type": "error", "message": "LLM 未配置：请到「全局设置 → LLM 歌单生成」填接口地址与模型"}
        return

    system = SYSTEM.format(mode_rule=MODE_FETCH if generate == "fetch" else MODE_MODEL,
                           memories=_memory_block())
    yield {"type": "thinking", "text": "正在读取长期记忆与最近推荐…"}
    yield {"type": "thinking", "text": "模型正在理解需求并规划选曲…"}

    buf = []
    try:
        for delta in _stream_chat(message, system):
            buf.append(delta)
            yield {"type": "token", "text": delta}
    except Exception as e:
        yield {"type": "error", "message": f"模型调用失败：{str(e)[:180]}"}
        return

    raw = "".join(buf)
    thought, sep, result = raw.partition(SEP)
    try:
        data = _extract_json(result if sep else raw)
    except Exception as e:
        yield {"type": "error", "message": f"模型输出解析失败：{str(e)[:120]}"}
        return

    if data.get("preference"):
        add_memory("preference", data["preference"])
        yield {"type": "memory", "content": f"记住了：{data['preference']}"}

    if generate == "fetch":
        yield {"type": "thinking", "text": "正在到音乐平台检索歌单…"}
        items, errs = fetch_candidates(data.get("keywords") or [message[:12]])
        for e in errs[:2]:
            yield {"type": "thinking", "text": f"部分平台检索失败：{e}"}
    else:
        items = generated_items(data)
    if not items:
        yield {"type": "error", "message": "这次没有找到合适的歌单，换个说法再试试"}
        return

    yield {"type": "playlists", "items": items, "thought": thought.strip()[:400],
           "recommend": recommend, "generate": generate}

    if recommend == "smart":
        yield {"type": "creating", "text": f"智能模式：自动创建 {len(items)} 张歌单并推送到平台…"}
        res = create_items(items)
        yield {"type": "created", **res}

    try:
        memo = summarize(message, items, generate)
        yield {"type": "memory", "content": memo}
    except Exception as e:
        log_api.warning("写入记忆失败：%s", e)
    yield {"type": "done"}
