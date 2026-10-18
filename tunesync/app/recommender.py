"""LLM 歌单推荐器。

取代原平台抓取适配器：按推送场景（或用户自定义提示词）让大模型直接生成
仿音乐平台风格的结构化歌单（歌单名 + 歌名/歌手），交由流水线去做
本地匹配 → 在线下载 → 刮削。

走 OpenAI 兼容的 /chat/completions 接口，base_url / api_key / model 全部在
config.yaml 的 llm 节配置。
"""
import json
import re
import time
import uuid

import requests

from .core import log_fetcher, merged_config
from .platforms import PLATFORM_NAMES

TIMEOUT = 120
RETRY = 1          # 5xx / 网络错误额外重试次数
BACKOFF = 3        # 指数退避基数（秒）

SYSTEM_PROMPT = (
    "你是一位资深音乐编辑，精通 QQ音乐 / 网易云音乐 / 酷狗 / 抖音热榜等平台的"
    "歌单命名与选曲风格。根据给定的推送场景生成歌单：\n"
    "1. 歌单名必须像真实平台上的热门歌单——口语化、有画面感、常带符号或数字，"
    "例如「温柔到骨子里的华语民谣」「抖音热歌榜TOP50丨全网都在循环」「深夜自习室·白噪音钢琴」；"
    "多张歌单之间的名字与选曲不要雷同。\n"
    "2. 只推荐真实存在的歌曲，歌名与歌手必须一一对应（不要张冠李戴），"
    "优先选流传度高、在各平台容易搜到的正式版本（避免 Live / Remix，除非切题）。\n"
    "3. 只输出 JSON，格式："
    '{"playlists":[{"name":"歌单名","tracks":[{"title":"歌名","artist":"歌手"}]}]}，'
    "不要输出任何解释文字或 markdown 代码块。"
)

SELECT_PROMPT = (
    "你是一位资深音乐编辑，正在为用户挑选值得下载到本地乐库的歌单。\n"
    "我会给你一批候选歌单（来自各音乐平台的真实歌单），请按推送场景与提示词筛选：\n"
    "1. 只保留与场景/提示词真正贴合的歌单；完全无关的一律丢弃，宁缺毋滥。\n"
    "2. 优先选曲目数适中（10–100 首）、口碑与播放量靠前的歌单。\n"
    "3. 不要改名、不要新造歌单，只做「选 / 不选」的判断；\n"
    "   返回的 id 必须是候选列表里原样出现的 id，不得编造或改写。\n"
    "4. 只输出 JSON，格式："
    '{"playlists":[{"id":"候选歌单id","name":"原歌单名","reason":"一句话理由"}]}，'
    "不要输出任何解释文字或 markdown 代码块。"
)


class RecommenderError(Exception):
    pass


def _cfg(key, default=None):
    return (merged_config().get("llm") or {}).get(key, default)


def _llm_cfg(override=None):
    """生效的 llm 配置：已保存值 + 可选的临时覆盖（连通测试「先测后存」用）。

    覆盖值里 api_key 为 "***" / 空 / 缺省都表示「用已保存的 key」，
    与 PUT /settings 的掩码语义一致——前端表单回显的就是 ***。
    """
    cfg = dict(merged_config().get("llm") or {})
    if override:
        ov = {k: v for k, v in override.items()
              if k in ("base_url", "api_key", "model", "temperature") and v is not None}
        if ov.get("api_key") in ("***", ""):
            ov.pop("api_key", None)
        cfg.update(ov)
    return cfg


def is_configured(override=None):
    """LLM 是否已经配置到可用（base_url 与 model 都要有）。"""
    cfg = _llm_cfg(override)
    return bool((cfg.get("base_url") or "").strip() and (cfg.get("model") or "").strip())


def _chat(user_content, system=SYSTEM_PROMPT, override=None):
    cfg = _llm_cfg(override)
    base_url = (cfg.get("base_url") or "").strip().rstrip("/")
    api_key = (cfg.get("api_key") or "").strip()
    model = (cfg.get("model") or "").strip()
    if not base_url or not model:
        raise RecommenderError(
            "LLM 未配置：请在「全局设置」页的「LLM 歌单生成」卡片填写接口地址与模型")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        "temperature": float(cfg.get("temperature", 0.8)),
        # response_format 只是尽力而为：不支持的兼容端点会忽略或 4xx，
        # 4xx 属确定性错误直接报出来，便于用户发现端点不兼容。
        "response_format": {"type": "json_object"},
    }
    last = None
    for i in range(RETRY + 1):
        try:
            r = requests.post(f"{base_url}/chat/completions", json=body,
                              headers=headers, timeout=TIMEOUT)
            if r.status_code >= 500:
                raise RuntimeError(f"HTTP {r.status_code}")   # 上游抖动，可重试
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:      # 4xx 是确定性错误（key 无效/端点不兼容），立即报
            detail = ""
            try:
                detail = json.dumps(e.response.json(), ensure_ascii=False)[:150]
            except Exception:
                detail = str(e)[:150]
            raise RecommenderError(
                f"LLM 请求被拒绝（HTTP {e.response.status_code}）：{detail}") from e
        except Exception as e:               # 网络错误 / 5xx：退避后重试
            last = e
        if i < RETRY:
            time.sleep(BACKOFF * 2 ** i)
    raise RecommenderError(f"LLM 服务不可用：{str(last)[:150]}")


def _content(data):
    """从 OpenAI 兼容响应里取首条回复文本。"""
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RecommenderError(f"LLM 响应格式异常：{json.dumps(data, ensure_ascii=False)[:200]}")
    if not isinstance(content, str) or not content.strip():
        raise RecommenderError("LLM 返回了空内容")
    return content


def _extract_json(text):
    """容错解析：剥 ```json 围栏；混在散文里时取首个 JSON 对象。"""
    s = text.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", s, re.S)
    if fence:
        s = fence.group(1).strip()
    try:
        return json.loads(s)
    except ValueError:
        pass
    dec = json.JSONDecoder()
    for i, ch in enumerate(s):
        if ch == "{":
            try:
                return dec.raw_decode(s, i)[0]
            except ValueError:
                continue
    raise RecommenderError("LLM 返回内容不是有效 JSON")


# ── AI 元数据补全（刮削页）───────────────────────────────────

AI_META_SYSTEM = (
    "你是音乐元数据助手。输入一批歌曲（title/artist/album），返回每首的发行年份、流派与风格。"
    '只输出 JSON：{"items":[{"i":编号,"year":1998,"genre":"流行","style":"国语流行"}]}。'
    "year 必须是 4 位数字；genre 用主流派别（流行/摇滚/民谣/电子/Hip-Hop/R&B/古典/爵士 等）；"
    "style 是更细的子风格（如 粤语流行/Synthwave/城市流行）。"
    "拿不准的字段留空字符串，绝不编造；不认识的歌直接跳过该项。"
)


def enrich_tracks(items, override=None):
    """批量补元数据：items=[{"i":1,"title","artist","album"?}]。

    返回 {"1": {"year":1998,"genre":"流行","style":"国语流行"}, ...}。
    模型漏答/答错编号的那首自然不出现在结果里，由调用方按单首失败处理——
    一首歌出错绝不放大到整批。
    """
    data = _chat(json.dumps({"tracks": items}, ensure_ascii=False),
                 system=AI_META_SYSTEM, override=override)
    obj = _extract_json(_content(data))
    out = {}
    for it in (obj or {}).get("items") or []:
        try:
            i = str(int(it.get("i")))
        except (TypeError, ValueError):
            continue
        try:
            year = int(str(it.get("year"))[:4])
        except (TypeError, ValueError):
            year = 0
        if not 1900 <= year <= 2100:
            year = 0
        out[i] = {"year": year,
                  "genre": str(it.get("genre") or "").strip()[:40],
                  "style": str(it.get("style") or "").strip()[:40]}
    return out


def generate_playlists(scene, count, prompt=None, tracks_per=None):
    """生成 count 张主题歌单。

    scene   场景名（categories.yaml 里的中文名），prompt 存在时仅作展示
    prompt  用户自定义推送提示词；存在时替代场景成为生成主题
    返回形状与旧平台适配器一致：[{"id","name","tracks":[{"title","artist",...}],"cover"}]
    """
    count = max(1, int(count or 1))
    if tracks_per is None:
        tracks_per = _cfg("tracks_per_playlist", 30)
    tracks_per = max(1, int(tracks_per or 30))
    topic = str(prompt or "").strip() or str(scene or "").strip()
    if not topic:
        raise RecommenderError("推送主题为空：请选择场景或填写自定义提示词")

    data = _chat(f"生成歌单数量：{count}\n"
                 f"每张歌单的曲目数：{tracks_per}\n"
                 f"推送主题：{topic}")
    parsed = _extract_json(_content(data))
    pls = parsed.get("playlists") if isinstance(parsed, dict) else None
    if not isinstance(pls, list):
        raise RecommenderError("LLM 返回缺少 playlists 数组")

    out = []
    for p in pls:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "").strip()
        tracks = []
        for t in p.get("tracks") or []:
            if not isinstance(t, dict):
                continue
            title = str(t.get("title") or "").strip()
            if not title:
                continue
            tracks.append({"title": title,
                           "artist": str(t.get("artist") or "").strip(),
                           "album": str(t.get("album") or "")})
            if len(tracks) >= tracks_per:
                break
        if name and tracks:
            # 平台已删，id 只用于本地去重（playlist.source_id），伪 id 足够
            out.append({"id": uuid.uuid4().hex[:12], "name": name,
                        "tracks": tracks, "cover": ""})
        if len(out) >= count:
            break
    if not out:
        raise RecommenderError("LLM 未生成任何可用歌单（缺少歌单名或曲目）")
    log_fetcher.info("LLM 生成了 %s 张「%s」歌单", len(out), topic[:40])
    return out[:count]


def select_playlists(candidates, scene, count, prompt=None):
    """按场景与提示词，从真实平台歌单里挑出值得推送的那些。

    candidates  platforms.square 的列表项，需带 id / name / cover / platform
    返回候选的子集（保留 platform、id、name、cover 等原字段），最多 count 张。
    模型编造的 id 会被丢弃——只认候选里真实存在的 id。
    """
    count = max(1, int(count or 1))
    if not candidates:
        raise RecommenderError("没有可供筛选的候选歌单")
    topic = str(prompt or "").strip() or str(scene or "").strip()
    if not topic:
        raise RecommenderError("推送主题为空：请选择场景或填写自定义提示词")

    # 候选很多时一次性塞给模型既超 context 又稀释注意力；只取前若干条送审
    pool = list(candidates)[:int(_cfg("select_pool", 40) or 40)]
    listing = "\n".join(
        f"- id={c.get('id')} | 平台={PLATFORM_NAMES.get(c.get('platform', ''), c.get('platform', ''))}"
        f" | 歌单名={c.get('name', '')}"
        f" | 曲目数={c.get('count', 0)} | 播放量={c.get('play', 0)}"
        f" | 创建者={c.get('creator', '')}"
        for c in pool)

    data = _chat(f"最多挑选：{count} 张\n"
                 f"推送主题：{topic}\n"
                 f"候选歌单：\n{listing}", system=SELECT_PROMPT)
    parsed = _extract_json(_content(data))
    pls = parsed.get("playlists") if isinstance(parsed, dict) else None
    if not isinstance(pls, list):
        raise RecommenderError("LLM 返回缺少 playlists 数组")

    by_id = {str(c.get("id")): c for c in pool}
    picked, seen = [], set()
    for p in pls:
        if not isinstance(p, dict):
            continue
        src = by_id.get(str(p.get("id", "")))
        if src is None or src["id"] in seen:
            continue          # 模型编造或重复的 id 直接跳过
        seen.add(src["id"])
        item = dict(src)
        reason = str(p.get("reason") or "").strip()
        if reason:
            item["reason"] = reason
        picked.append(item)
        if len(picked) >= count:
            break
    if not picked:
        raise RecommenderError(f"LLM 未从 {len(pool)} 个候选里选出任何歌单（场景：{topic[:30]}）")
    log_fetcher.info("LLM 从 %s 个候选里选了 %s 张「%s」歌单",
                     len(pool), len(picked), topic[:40])
    return picked
