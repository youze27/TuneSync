"""平台歌单拉取 —— 网易云/QQ/酷狗/酷我/咪咕 的游客直连客户端。

全部为公开网页接口、游客身份（无 cookie），2026-10 逐条实测：

- 网易云（wy）
  广场  GET /api/playlist/list?cat=华语&limit&offset&total=true&order=hot
  详情  GET /api/v6/playlist/detail?id=&n=1000 → playlist.trackIds[]
  曲目  POST /api/v3/song/detail（form c=[{"id":x},...]，≤200/批）
- QQ（tx）
  广场  GET /splcloud/fcgi-bin/fcg_get_diss_by_tag.fcg?categoryId=&sortId=5&sin=&ein=
  标签  GET /splcloud/fcgi-bin/fcg_get_diss_tag_conf.fcg（动态分类表，缓存 10 分钟）
  详情  GET /v8/fcg-bin/fcg_v8_playlist_cp.fcg?id= → data.cdlist[0].songlist
- 酷狗（kg）—— 广场为「关键词搜歌单」（端点参考 music-hub / lx-music-desktop）
  搜歌单 GET http://msearchretry.kugou.com/api/v3/search/special?keyword=&page=&pagesize=
  详情   GET http://www2.kugou.kugou.com/yueku/v9/special/single/{sid}-5-9999.html
         （HTML 里 global.data = [...] 提取 hash）+ POST gateway.kugou.com/v2/album_audio/audio
         （固定 key，100 hash/批）补齐曲目
- 酷我（kw）—— 同为关键词搜歌单
  搜歌单 GET http://search.kuwo.cn/r.s?all={kw}&ft=playlist（响应是单引号 JS 字面量，需容错解析）
  详情   GET http://nplserver.kuwo.cn/pl.svc?op=getlistinfo&pid=（无签名，rn=1000/页）
- 咪咕（mg）—— 同为关键词搜歌单
  搜歌单 GET https://jadeite.migu.cn/music_search/v3/search/searchAll（headers 带 md5 sign，
         固定 deviceId，参考 music-hub mg/musicSearch.ts）
  详情   GET https://c.musicapp.migu.cn/MIGUM3.0/resource/playlist/v2.0（歌单信息）
         + GET https://app.c.nf.migu.cn/MIGUM3.0/resource/playlist/song/v2.0（50 首/页翻页）

注意：
- musicu.fcg 的 PlaylistInfoServer 已被风控（code 500003），不要用；
- 网易游客有客户端限流（偶发 460/502/0 结果），报错要提示「稍后再试」；
- wy/tx 是目录式广场（分类→歌单列表），kg/kw/mg/all 是搜索式广场（预设关键词当分类）；
- category 统一用 {"value","label"}：wy 的 value 是分类名，tx 的是标签 ID 字符串，
  kg/kw/mg/all 的 value 是搜索关键词；
- 卡片统一 {id,name,cover,count,play,creator[,platform]}，聚合(all)的列表项带 platform。
"""
import hashlib
import html
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests

# dict 顺序 = 前端 tab 顺序；all 为跨 kg/kw/mg 的关键词聚合
PLATFORM_NAMES = {"all": "聚合", "wy": "网易云", "tx": "QQ 音乐",
                  "kg": "酷狗", "kw": "酷我", "mg": "咪咕"}

SEARCH_PLATFORMS = {"kg", "kw", "mg"}   # 歌单靠关键词搜索的平台（all 聚合它们）
PRESET_KEYWORDS = ["经典", "流行", "华语", "怀旧", "民谣", "摇滚", "轻音乐", "粤语", "老歌", "抖音热歌"]

TIMEOUT = 20
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
WY_HEADERS = {"User-Agent": UA, "Referer": "https://music.163.com/"}
TX_HEADERS = {"User-Agent": UA, "Referer": "https://y.qq.com/", "Origin": "https://y.qq.com"}
MG_WEB_HEADERS = {"User-Agent": UA, "Referer": "https://m.music.migu.cn/"}

# 咪咕搜索签名（music-hub mg/musicSearch.ts 同款固定常量，2026-10 实测有效）
_MG_DEVICE_ID = "963B7AA0D21511ED807EE5846EC87D20"
_MG_SIGN_SALT = "6cdc72a439cef99a3418d2a78aa28c73yyapp2d16148780a1dcc7408e06336b98cfd50"
_MG_APP_UA = ("Mozilla/5.0 (Linux; U; Android 11.0.0; zh-cn; MI 11 Build/OPR1.170623.032) "
              "AppleWebKit/534.30 (KHTML, like Gecko) Version/4.0 Mobile Safari/534.30")

# 酷狗详情：HTML 里的 hash 列表 + gateway 批量接口（固定 key，非计算签名）
_KG_DATA_RE = re.compile(r"global\.data = (\[.+\]);")
_KG_INFO_RE = re.compile(r"global = \{[\s\S]*?name: \"(.+)\"[\s\S]*?pic: \"(.+)\"[\s\S]*?\};")
_KG_GATEWAY = "http://gateway.kugou.com/v2/album_audio/audio"
_KG_GATEWAY_KEY = "OIlwieks28dk2k092lksi2UIkp"
_KG_GATEWAY_HEADERS = {"KG-THash": "13a3164", "KG-RC": "1", "KG-Fake": "0", "KG-RF": "00869891",
                       "User-Agent": "Android712-AndroidPhone-11451-376-0-FeeCacheUpdate-wifi",
                       "x-router": "kmr.service.kugou.com"}
_KG_GATEWAY_BATCH = 100
# 单曲分享短链：t1.kugou.com/song.html?id=xxx → www.kugou.com/share/xxx.html
# 该页不是歌单页，但内嵌 var dataFromSmarty = [...]，是页面给出的完整曲目集合
_KG_SHARE_RE = re.compile(r"var\s+dataFromSmarty\s*=\s*\[")
_KG_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)

_MG_SONG_PAGE = 50      # 咪咕曲目单页上限
_MG_MAX_PAGES = 40      # 防呆上限
_WY_SONG_BATCH = 200    # /api/v3/song/detail 每批条数上限

_tx_tag_cache: dict = {}       # "tags" -> (fetched_at, [{"value","label"}])
_TX_TAG_TTL = 600

# 网易官方分类名直接可用（order=hot 热门歌单），挑常用的一组
WY_CATEGORIES = ["华语", "流行", "民谣", "说唱", "电子", "古风", "摇滚", "轻音乐",
                 "爵士", "ACG", "欧美", "日语", "韩语", "粤语", "清晨", "夜晚", "学习", "工作"]


class PlatformError(RuntimeError):
    """平台接口失败（限流/无结果/结构变化），消息直接展示给用户。"""


def _int(v, default=0):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def _wy(path, **params):
    try:
        r = requests.get(f"https://music.163.com{path}", params=params,
                         headers=WY_HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        raise PlatformError(f"网易云接口请求失败：{str(e)[:120]}") from e
    except ValueError as e:
        raise PlatformError("网易云返回了非 JSON 内容（可能被限流），请稍后再试") from e


def _tx(path, **params):
    try:
        r = requests.get(f"https://c.y.qq.com{path}", params=params,
                         headers=TX_HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        raise PlatformError(f"QQ 音乐接口请求失败：{str(e)[:120]}") from e
    except ValueError as e:
        raise PlatformError("QQ 音乐返回了非 JSON 内容，请稍后再试") from e


def _kg(path, **params):
    try:
        r = requests.get(f"http://msearchretry.kugou.com{path}", params=params,
                         headers={"User-Agent": UA}, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        raise PlatformError(f"酷狗接口请求失败：{str(e)[:120]}") from e
    except ValueError as e:
        raise PlatformError("酷狗返回了非 JSON 内容，请稍后再试") from e


def _kw_json(text):
    """酷我搜索接口返回单引号 JS 对象字面量，不是严格 JSON。"""
    try:
        return json.loads(text)
    except ValueError:
        pass
    try:
        return json.loads(text.replace("'", '"'))
    except ValueError as e:
        raise PlatformError("酷我返回了非 JSON 内容（可能被限流），请稍后再试") from e


def _kw(url, **params):
    try:
        r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=TIMEOUT)
        r.raise_for_status()
        return _kw_json(r.text)
    except requests.RequestException as e:
        raise PlatformError(f"酷我接口请求失败：{str(e)[:120]}") from e


def _mg_sign(time_str, text):
    raw = f"{text}{_MG_SIGN_SALT}{_MG_DEVICE_ID}{time_str}"
    return hashlib.md5(raw.encode()).hexdigest()


def _mg_get(url, sign_text, **params):
    ts = str(int(time.time() * 1000))
    headers = {"uiVersion": "A_music_3.6.1", "deviceId": _MG_DEVICE_ID, "timestamp": ts,
               "sign": _mg_sign(ts, sign_text), "channel": "0146921", "User-Agent": _MG_APP_UA}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        raise PlatformError(f"咪咕接口请求失败：{str(e)[:120]}") from e
    except ValueError as e:
        raise PlatformError("咪咕返回了非 JSON 内容，请稍后再试") from e


def _check_wy(d, where):
    """网易云的业务码：200 成功；游客限流常见 -460/非 200。"""
    code = d.get("code")
    if code != 200:
        raise PlatformError(f"网易云{where}失败（code={code}，游客限流或接口变更，请稍后再试）")
    return d


# ---------------------------------------------------------------- 分类

def categories(platform):
    """分类列表，[{value,label}]。wy 静态表；tx 动态拉标签配置并缓存；
    kg/kw/mg/all 是搜索式广场，分类即预设关键词。"""
    if platform in SEARCH_PLATFORMS or platform == "all":
        return [{"value": k, "label": k} for k in PRESET_KEYWORDS]
    if platform == "wy":
        return [{"value": c, "label": c} for c in WY_CATEGORIES]
    if platform == "tx":
        cached = _tx_tag_cache.get("tags")
        if cached and time.time() - cached[0] < _TX_TAG_TTL:
            return cached[1]
        d = _tx("/splcloud/fcgi-bin/fcg_get_diss_tag_conf.fcg", picmid=1, g_tk=5381,
                loginUin=0, hostUin=0, format="json", inCharset="utf8", outCharset="utf-8",
                notice=0, platform="yqq.json", needNewCode=0)
        cats = []
        for g in (d.get("data") or {}).get("categories") or []:
            for it in g.get("items") or []:
                if it.get("categoryId") and it.get("categoryName"):
                    cats.append({"value": str(it["categoryId"]),
                                 "label": html.unescape(str(it["categoryName"]))})
        if not cats:
            raise PlatformError("QQ 音乐分类接口无数据")
        _tx_tag_cache["tags"] = (time.time(), cats)
        return cats
    raise PlatformError(f"不支持的平台：{platform}")


# ---------------------------------------------------------------- 歌单广场

def _search_page(offset, limit):
    """offset → 搜索式平台的页码（1 起）。"""
    return offset // max(1, limit) + 1


def _kg_square(keyword, page, limit):
    d = _kg("/api/v3/search/special", keyword=keyword, page=page, pagesize=limit,
            showtype=10, filter=0, version=7910, sver=2)
    if d.get("errcode") != 0:
        raise PlatformError(f"酷狗搜歌单失败（errcode={d.get('errcode')}）")
    data = d.get("data") or {}
    lists = [{
        "id": str(i.get("specialid") or ""),
        "name": html.unescape(i.get("specialname") or ""),
        "cover": i.get("imgurl") or "",
        "count": _int(i.get("songcount")),
        "play": _int(i.get("playcount")),
        "creator": html.unescape(i.get("nickname") or ""),
    } for i in data.get("info") or [] if i.get("specialid")]
    return {"total": _int(data.get("total")) or len(lists), "lists": lists}


def _kw_square(keyword, page, limit):
    d = _kw("http://search.kuwo.cn/r.s", all=keyword, pn=page - 1, rn=limit,
            rformat="json", encoding="utf8", ver="mbox", vipver="MUSIC_8.7.7.0_BCS37",
            plat="pc", devid="28156413", ft="playlist", pay=0, needliveshow=0)
    lists = [{
        "id": str(i.get("playlistid") or ""),
        "name": html.unescape(i.get("name") or ""),
        "cover": i.get("pic") or "",
        "count": _int(i.get("songnum")),
        "play": _int(i.get("playcnt")),
        "creator": html.unescape(i.get("nickname") or ""),
    } for i in d.get("abslist") or [] if i.get("playlistid")]
    return {"total": _int(d.get("TOTAL")) or len(lists), "lists": lists}


_MG_SEARCH_SWITCH = ('{"song":0,"album":0,"singer":0,"tagSong":0,"mvSong":0,'
                     '"bestShow":0,"songlist":1,"lyricSong":0}')


def _mg_square(keyword, page, limit):
    d = _mg_get("https://jadeite.migu.cn/music_search/v3/search/searchAll", keyword,
                isCorrect=1, isCopyright=1, searchSwitch=_MG_SEARCH_SWITCH,
                pageSize=limit, text=keyword, pageNo=page, sort=0, sid="USS")
    res = (d.get("songListResultData") or {})
    lists = [{
        "id": str(i.get("id") or ""),
        "name": i.get("name") or "",
        "cover": i.get("musicListPicUrl") or "",
        "count": _int(i.get("musicNum")),
        "play": _int(i.get("playNum")),
        "creator": i.get("userName") or "",
    } for i in res.get("result") or [] if i.get("id")]
    return {"total": _int(res.get("totalCount")) or len(lists), "lists": lists}


_SEARCH_SQUARES = {"kg": _kg_square, "kw": _kw_square, "mg": _mg_square}


def _square_all(keyword, offset, limit):
    """聚合：并发搜 kg/kw/mg，单平台失败不影响其它（容错聚合）。"""
    page = _search_page(offset, limit)
    lists, errors, total = [], {}, 0
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {key: ex.submit(fn, keyword, page, limit) for key, fn in _SEARCH_SQUARES.items()}
        for key, fut in futs.items():
            try:
                res = fut.result()
            except PlatformError as e:
                errors[key] = str(e)
                continue
            batch = []
            for item in res["lists"]:
                item = dict(item)
                item["platform"] = key
                batch.append(item)
            lists.extend(batch)
            total += _int(res.get("total")) or len(batch)
    if not lists and errors:
        detail = "；".join(f"{PLATFORM_NAMES[k]}：{v}" for k, v in errors.items())
        raise PlatformError(f"聚合搜索全部失败：{detail}")
    out = {"total": total, "lists": lists}
    if errors:
        out["errors"] = errors
    return out


def square(platform, cat=None, offset=0, limit=30):
    if platform == "wy":
        d = _check_wy(_wy("/api/playlist/list", cat=cat or "华语", limit=limit,
                          offset=offset, total="true", order="hot"), "歌单广场")
        lists = [{
            "id": str(p.get("id", "")),
            "name": p.get("name", ""),
            "cover": p.get("coverImgUrl", ""),
            "count": p.get("trackCount", 0) or 0,
            "play": p.get("playCount", 0) or 0,
            "creator": (p.get("creator") or {}).get("nickname", ""),
        } for p in d.get("playlists") or []]
        return {"total": d.get("total", len(lists)), "lists": lists}

    if platform == "tx":
        sin = max(0, offset)
        d = _tx("/splcloud/fcgi-bin/fcg_get_diss_by_tag.fcg", picmid=1, g_tk=5381,
                loginUin=0, hostUin=0, format="json", inCharset="utf8", outCharset="utf-8",
                notice=0, platform="yqq.json", needNewCode=0,
                categoryId=cat or "10000000", sortId=5, sin=sin, ein=sin + max(1, limit) - 1)
        data = d.get("data") or {}
        lists = [{
            "id": str(p.get("dissid", "")),
            "name": p.get("dissname", ""),
            "cover": p.get("imgurl", ""),
            "count": p.get("song_count") or 0,
            "play": p.get("listennum") or 0,
            "creator": p.get("nickname", ""),
        } for p in data.get("list") or []]
        return {"total": data.get("sum", len(lists)), "lists": lists}

    if platform in _SEARCH_SQUARES:
        return _SEARCH_SQUARES[platform](cat or "经典", _search_page(offset, limit), limit)

    if platform == "all":
        return _square_all(cat or "经典", offset, limit)

    raise PlatformError(f"不支持的平台：{platform}")


# ---------------------------------------------------------------- 歌单详情

def _wy_songs(tids):
    """trackIds 批量换曲目信息，200 个一批。"""
    tracks = []
    for i in range(0, len(tids), _WY_SONG_BATCH):
        chunk = tids[i:i + _WY_SONG_BATCH]
        c = json.dumps([{"id": t} for t in chunk], ensure_ascii=False)
        try:
            r = requests.post("https://music.163.com/api/v3/song/detail",
                              data={"c": c, "n": len(chunk)},
                              headers=WY_HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            d = r.json()
        except (requests.RequestException, ValueError) as e:
            raise PlatformError(f"网易云曲目详情请求失败：{str(e)[:120]}") from e
        _check_wy(d, "曲目详情")
        for s in d.get("songs") or []:
            tracks.append({
                "title": s.get("name", "") or "",
                "artist": "/".join(a.get("name", "") for a in s.get("ar") or [] if a.get("name")),
                "album": (s.get("al") or {}).get("name", "") or "",
                "raw": {"id": s.get("id")},
            })
    return tracks


def _kg_js_array(text, start):
    """从 text[start]（必须是 '['）起做括号配平扫描，取出完整 JS 数组字面量。

    不能用非贪婪正则：数组内部的 '];' 会让 .*? 提前截断，json.loads 报 Extra data。
    """
    depth, in_str, esc, quote = 0, False, False, ""
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == quote:
                in_str = False
            continue
        if c in "\"'":
            in_str, quote = True, c
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return ""


def _kg_fetch_share(sid):
    """酷狗单曲分享短链（字母数字 id）→ hash 列表 + (歌单名, 封面)。

    t1.kugou.com/song.html?id=xxx 会 302 到 www.kugou.com/share/xxx.html，
    该页不是歌单页（无任何 specialid），但内嵌 dataFromSmarty 数组，
    是页面给出的完整曲目集合，直接当歌单导入。
    """
    url = f"https://www.kugou.com/share/{sid}.html"
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as e:
        raise PlatformError(f"酷狗分享页请求失败：{str(e)[:120]}") from e
    m = _KG_SHARE_RE.search(r.text)
    if not m:
        raise PlatformError("酷狗分享页解析失败（页面结构变化），请确认链接是否还有效")
    raw = _kg_js_array(r.text, m.end() - 1)
    try:
        items = json.loads(raw)
    except ValueError as e:
        raise PlatformError("酷狗分享页解析失败（dataFromSmarty 非法）") from e
    hashes = [{"hash": it["hash"]} for it in items if isinstance(it, dict) and it.get("hash")]
    if not hashes:
        raise PlatformError("酷狗分享页里没有取到曲目")
    mt = _KG_TITLE_RE.search(r.text)
    # title 形如「歌名_歌手_酷狗音乐」，取首段做歌单名
    name = html.unescape(mt.group(1)).split("_")[0].strip() if mt else ""
    return hashes, name, ""


def _kg_fetch_hashes(sid):
    """抓 special/single HTML → hash 列表 + (歌单名, 封面)。未命中重试最多 2 次。"""
    url = f"http://www2.kugou.kugou.com/yueku/v9/special/single/{sid}-5-9999.html"
    last = None
    for _ in range(3):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
            r.raise_for_status()
        except requests.RequestException as e:
            raise PlatformError(f"酷狗歌单详情请求失败：{str(e)[:120]}") from e
        m = _KG_DATA_RE.search(r.text)
        if m:
            try:
                hashes = [{"hash": it["hash"]}
                          for it in json.loads(m.group(1)) if it.get("hash")]
            except ValueError as e:
                raise PlatformError("酷狗歌单详情解析失败（global.data 非法）") from e
            mi = _KG_INFO_RE.search(r.text)
            name = html.unescape(mi.group(1)) if mi else ""
            cover = mi.group(2) if mi else ""
            return hashes, name, cover
        last = r.text[:120]
    raise PlatformError(f"酷狗歌单详情解析失败（页面结构变化）：{last}")


def _kg_gateway_songs(hashes):
    """gateway 按 100 hash/批补齐曲目信息，多批并发。"""
    base = {"area_code": "1", "show_privilege": 1, "show_album_info": "1", "is_publish": "",
            "appid": 1005, "clientver": 11451, "mid": "1", "dfid": "-",
            "clienttime": int(time.time() * 1000), "key": _KG_GATEWAY_KEY,
            "fields": "album_info,author_name,audio_info,ori_audio_name,base,songname"}

    def run(chunk):
        try:
            r = requests.post(_KG_GATEWAY, json={**base, "data": chunk},
                              headers=_KG_GATEWAY_HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json().get("data") or []
        except requests.RequestException as e:
            raise PlatformError(f"酷狗曲目信息请求失败：{str(e)[:120]}") from e
        except ValueError as e:
            raise PlatformError("酷狗曲目信息返回了非 JSON 内容") from e

    batches = [hashes[i:i + _KG_GATEWAY_BATCH] for i in range(0, len(hashes), _KG_GATEWAY_BATCH)]
    if len(batches) == 1:
        groups = run(batches[0])
    else:
        with ThreadPoolExecutor(max_workers=4) as ex:
            groups = [g for sub in ex.map(run, batches) for g in sub]
    songs = []
    for group in groups:
        for s in group or []:
            if s and s.get("audio_info"):
                songs.append(s)
    return songs


def _kg_detail(pid):
    pid = str(pid)
    # 纯数字是 yueku 歌单；字母数字是分享短链，两套体系互不相通
    if pid.isdigit():
        hashes, name, cover = _kg_fetch_hashes(pid)
    else:
        hashes, name, cover = _kg_fetch_share(pid)
    songs = _kg_gateway_songs(hashes) if hashes else []
    tracks, seen = [], set()
    for s in songs:
        ai = s.get("audio_info") or {}
        aid = str(ai.get("audio_id") or "")
        if not aid or aid in seen:
            continue
        seen.add(aid)
        alb = s.get("album_info") or {}
        tracks.append({
            "title": html.unescape(s.get("ori_audio_name") or s.get("songname") or ""),
            "artist": html.unescape(s.get("author_name") or ""),
            "album": html.unescape(alb.get("album_name") or ""),
            "raw": {"hash": ai.get("hash"), "audio_id": aid},
        })
    return {"platform": "kg", "id": str(pid), "name": name, "cover": cover,
            "count": len(tracks), "tracks": tracks}


def _kw_detail(pid):
    tracks, info, total = [], {}, 0
    for pn in range(10):                       # rn=1000/页，防呆上限 10 页
        d = _kw("http://nplserver.kuwo.cn/pl.svc", op="getlistinfo", pid=pid, pn=pn,
                rn=1000, encode="utf8", keyset="pl2012", identity="kuwo", pcmp4=1,
                vipver="MUSIC_9.0.5.0_W1", newver=1)
        if d.get("result") != "ok":
            if pn == 0:
                raise PlatformError("获取酷我歌单详情失败")
            break
        if pn == 0:
            info = d
        total = _int(d.get("total")) or total
        ml = d.get("musiclist") or []
        for it in ml:
            tracks.append({
                "title": html.unescape(str(it.get("name") or "")),
                "artist": html.unescape(str(it.get("artist") or "").replace("&", "、")),
                "album": html.unescape(str(it.get("album") or "")),
                "raw": {"id": str(it.get("id") or "")},
            })
        if not ml or (total and len(tracks) >= total):
            break
    return {"platform": "kw", "id": str(pid),
            "name": info.get("title", ""), "cover": info.get("pic", ""),
            "count": total or len(tracks), "tracks": tracks}


def _mg_detail(pid):
    info = {}
    try:
        d = requests.get("https://c.musicapp.migu.cn/MIGUM3.0/resource/playlist/v2.0",
                         params={"playlistId": pid}, headers=MG_WEB_HEADERS,
                         timeout=TIMEOUT).json()
        if d.get("code") == "000000":
            data = d.get("data") or {}
            info = {"name": data.get("title") or "",
                    "cover": (data.get("imgItem") or {}).get("img") or "",
                    "play": _int((data.get("opNumItem") or {}).get("playNum"))}
    except (requests.RequestException, ValueError):
        pass                                  # 歌单信息失败不致命，曲目接口再试
    tracks, total = [], 0
    for page_no in range(1, _MG_MAX_PAGES + 1):
        try:
            r = requests.get("https://app.c.nf.migu.cn/MIGUM3.0/resource/playlist/song/v2.0",
                             params={"pageNo": page_no, "pageSize": _MG_SONG_PAGE,
                                     "playlistId": pid},
                             headers=MG_WEB_HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            d = r.json()
        except (requests.RequestException, ValueError) as e:
            if page_no == 1:
                raise PlatformError(f"获取咪咕歌单详情失败：{str(e)[:120]}") from e
            break
        if d.get("code") != "000000":
            if page_no == 1:
                raise PlatformError("获取咪咕歌单详情失败")
            break
        data = d.get("data") or {}
        total = _int(data.get("totalCount")) or total
        songs = data.get("songList") or []
        for s in songs:
            singers = "、".join(x.get("name") for x in s.get("singerList") or [] if x.get("name"))
            tracks.append({
                "title": s.get("songName") or "",
                "artist": singers,
                "album": s.get("album") or "",
                "raw": {"songId": s.get("songId"), "copyrightId": s.get("copyrightId")},
            })
        if len(songs) < _MG_SONG_PAGE or (total and len(tracks) >= total):
            break
    return {"platform": "mg", "id": str(pid),
            "name": info.get("name", ""), "cover": info.get("cover", ""),
            "count": total or len(tracks), "tracks": tracks}


def detail(platform, pid):
    """歌单详情 + 完整曲目列表（title/artist/album/raw）。"""
    if platform == "wy":
        d = _check_wy(_wy("/api/v6/playlist/detail", id=pid, n=1000), "歌单详情")
        p = d.get("playlist") or {}
        tids = [t.get("id") for t in p.get("trackIds") or [] if t.get("id")]
        return {
            "platform": "wy", "id": str(pid),
            "name": p.get("name", ""), "cover": p.get("coverImgUrl", ""),
            "count": p.get("trackCount", len(tids)) or len(tids),
            "tracks": _wy_songs(tids),
        }

    if platform == "tx":
        d = _tx("/v8/fcg-bin/fcg_v8_playlist_cp.fcg", id=pid, format="json", g_tk=5381,
                loginUin=0, hostUin=0, inCharset="utf8", outCharset="utf-8", notice=0,
                platform="yqq.json", needNewCode=0)
        cd = ((d.get("data") or {}).get("cdlist") or [None])[0] or {}
        tracks = [{
            "title": s.get("songname", "") or s.get("name", "") or "",
            "artist": "/".join(a.get("name", "") for a in s.get("singer") or [] if a.get("name")),
            "album": s.get("albumname", "") or "",
            "raw": {"songmid": s.get("songmid"), "songid": s.get("songid")},
        } for s in cd.get("songlist") or []]
        return {
            "platform": "tx", "id": str(pid),
            "name": cd.get("dissname", ""), "cover": cd.get("logo", ""),
            "count": cd.get("cur_song_num", len(tracks)) or len(tracks),
            "tracks": tracks,
        }

    if platform == "kg":
        return _kg_detail(pid)
    if platform == "kw":
        return _kw_detail(pid)
    if platform == "mg":
        return _mg_detail(pid)

    raise PlatformError(f"不支持的平台：{platform}")


# ---------------------------------------------------------------- 链接解析

def parse_link(text, platform=""):
    """歌单链接 / 纯数字 ID → (platform, pid)。识别不出平台时 platform 为空串。

    支持：
      https://music.163.com/#/playlist?id=3778678
      https://music.163.com/playlist/3778678
      https://y.qq.com/n/ryqq/playlist/7267024244
      https://i.y.qq.com/n2/m/share/details/taoge.html?id=xxx
      https://www.kugou.com/special/8851337.html （…-id-8851337.html 亦可）
      https://t1.kugou.com/song.html?id=4dPwVbaG6V2 （单曲分享短链，字母数字 id）
      https://kuwo.cn/play_detail/123 （playlist_detail/123 亦可）
      https://music.migu.cn/v3/music/playlist/123
      3778678 （纯数字，平台由调用方给）
    """
    t = (text or "").strip()
    if not t:
        raise PlatformError("请填歌单链接或 ID")
    if t.isdigit():
        return (platform or "", t)
    m = (re.search(r"music\.163\.com[^ ]*?[?&]id=(\d+)", t)
         or re.search(r"music\.163\.com/playlist/(\d+)", t))
    if m:
        return ("wy", m.group(1))
    m = (re.search(r"y\.qq\.com[^ ]*?playlist/(\d+)", t)
         or re.search(r"[?&](?:disstid|id)=(\d+)", t))
    if m and ("qq.com" in t or "disstid=" in t):
        return ("tx", m.group(1))
    m = (re.search(r"kugou\.com.*?special/?(\d+)", t)
         or re.search(r"kugou\.com.*?-id-(\d+)", t)
         or re.search(r"kugou\.com/(?:song|share)[^#\s]*?[?&]id=([A-Za-z0-9_\-]+)", t)
         or re.search(r"kugou\.com/share/([A-Za-z0-9_\-]+)\.html", t))
    if m:
        return ("kg", m.group(1))
    m = re.search(r"kuwo\.cn.*?detail/?(\d+)", t)
    if m:
        return ("kw", m.group(1))
    m = re.search(r"migu\.cn.*?playlist/?([A-Za-z0-9_\-]+)", t)
    if m:
        return ("mg", m.group(1))
    raise PlatformError("无法识别的歌单链接：支持网易云/QQ/酷狗/酷我/咪咕的歌单分享链接")
