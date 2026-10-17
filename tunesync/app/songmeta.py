"""单曲元数据刮削 —— 按歌名/歌手搜索，多源合并出 专辑/流派/年份/封面/歌词。

设计参考 music-meta-web：每源一个独立搜索函数产出一份候选，由 merge_sources 按
优先级做字段级合并（先到先用，缺失才补），任何单源失败都不影响其他源。

各源接口均为游客直连（无需 cookie），2026-10 在容器内逐个实测确认可用字段：

- 网易云  GET /api/search/get/web?s=&type=1        → result.songs[]
            字段名与已下线的 /cloudsearch 不同：artists[]、album.name、album.picId
            流派 genres 已全网下线（song/detail 只剩 tns 翻译名）→ 不可用
            封面 album.picId 拼 https://p1.music.126.net/{picId}.jpg 亦可用，
            但 search 结果不含 picUrl，故统一走 song/detail 拿 picUrl
            年份 album.publishTime（毫秒）
            歌词 GET /api/song/lyric?id=&lv=-1&kv=-1&tv=-1
  POST /api/v3/song/detail  data={"id":sid,"c":'[{"id":sid}]'}（GET 会 400）

- QQ     GET /soso/fcgi-bin/client_search_cp       → data.song.list[]
          搜索结果无流派/封面，但带 albummid，可再打
          GET /v8/fcg-bin/fcg_v8_album_info_cp.fcg?albummid=
          → data.genre（"Pop 流行"）+ data.aDate（"2003-07-31"，年份来源）
            + data.list[] 曲目清单
          封面 https://y.gtimg.cn/music/photo_new/T002R300x300M000{albummid}.jpg
          年份 search 结果的 pubtime（秒级时间戳）
          歌词 GET /lyric/fcgi-bin/fcg_query_lyric_new.fcg?nobase64=1

- 酷狗   GET http://mobilecdn.kugou.com/api/v3/search/song → data.info[]
          字段小写：songname / singername / album_name / hash / album_audio_id
          流派 FileTag 已下线；专辑 info 接口 SSL 失败 → 流派靠 QQ 兜底
          封面 data.info[].trans_param.union_cover（{size} → 400）
          歌词 https://krcs.kugou.com/search?hash= → candidates[0]
                → https://lyrics.kugou.com/download?id=&accesskey=&fmt=lrc

- 酷我   GET http://search.kuwo.cn/r.s?all=&ft=music&pn=0&rn=
          必须带 rformat=json&encoding=utf8 才有结果，否则返回纯文本头
          返回的是 Python dict 字面量（单引号），需 ast.literal_eval 解析
          abslist[] 字段全大写：SONGNAME/ARTIST/ALBUM/DC_TARGETID/DURATION
          搜索结果无流派/年份/专辑图；歌词域名 krcs.kuwo.cn 已 DNS 失败
          → 仅作标题歌手校准 + 兜底定位，字段贡献最小

- 咪咕   jadeite.migu.cn 403、musicmobilepd.migu.cn DNS 失败 → 线上不可用，保留函数但默认不启用

- 苹果   GET https://itunes.apple.com/search?entity=song&media=music
          ⚠️ 不能带 country=CN：Apple 中国区接口对搜索请求恒返回 resultCount=0
          （版权分区差异），省略 country 走默认区才搜得到
          专辑 collectionName / 流派 primaryGenreName / 年份 releaseDate
          封面 artworkUrl100，把结尾的 100x100bb 换成 600x600bb 可拿大图（实测有效）
          时长 trackTimeMillis（毫秒）；无歌词接口
          ⚠️ 华语歌返回的是繁体歌名与罗马字歌手（浮誇 / Eason Chan），
          与简体的「浮夸 / 陈奕迅」对不上，故苹果源走宽松匹配 _pick(rows, loose=True)

- 开放库 MusicBrainz GET https://musicbrainz.org/ws/2/recording?query=&fmt=json
          ⚠️ 必须带合规 UA（含项目名与联系 URL），否则一律 503
          ⚠️ 限速 1 req/s，刮削并发高时要有意错开，否则会被 503 拒绝
          流派 tags[].name（小写英文标签，如 pop / jazz）；专辑与年份 releases[0]
          封面走 coverartarchive.org/release/{id}，但覆盖率很低（实测多 404），
          取不到就算了，不影响其他字段
          中文歌收录差、tags 多为空 → 只作为欧美日韩的兜底

歌词优先级：平台 lrc 接口（网易云 → QQ → 酷狗）→ LRC Lib → 音源自带。
"""
import ast
import html
import re
import time

import requests

from .core import log_scraper, norm

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
TIMEOUT = 15

YEAR_RE = re.compile(r"(1[89]\d{2}|20\d{2})")
# Live/翻唱/伴奏类标题：宽松匹配时对这类候选降权，别拿现场版顶替录音室原版
_LIVEISH = re.compile(r"live|remix|karaoke|cover|伴奏|翻唱|演唱会", re.I)


def year_of(value):
    """从日期/时间戳/文件名里取 4 位年份，取不到返回 ''。"""
    if isinstance(value, (int, float)) and value > 1_000_000_000:
        ts = time.localtime(value)
        return str(ts.tm_year) if 2000 <= ts.tm_year <= 2099 else ""
    m = YEAR_RE.search(str(value or ""))
    return m.group(1) if m else ""


def year_from_ms(value):
    """毫秒级时间戳 → 年份。"""
    try:
        ts = int(value) / 1000
    except (TypeError, ValueError):
        return ""
    return year_of(ts)


def _clean(v):
    """去 HTML 标签与实体。酷我/QQ 的高亮字段会带 <em> 和 &nbsp;。"""
    v = re.sub(r"<[^>]+>", "", str(v or ""))
    return html.unescape(v).replace("\xa0", " ").strip()


def _score(cand_title, cand_artist, title, artist):
    """候选打分：歌名完全命中权重最高，歌手命中次之，两者都命中才满分。

    参考 music-meta-web 的「文件名反推校验」：不预设谁是歌名谁是歌手，
    要求结果的歌名与歌手都能在搜索词里对上，顺序无关。
    """
    t, a = norm(title), norm(artist)
    ct, ca = norm(cand_title), norm(cand_artist)
    if not ct:
        return 0.0
    s = 0.0
    if ct == t:
        s += 1.0
    elif ct in t or t in ct:
        s += 0.6
    elif _seq_ratio(ct, t) > 0.75:
        s += 0.4
    if a:
        if ca == a:
            s += 1.0
        elif ca and (ca in a or a in ca):
            s += 0.6
        elif _seq_ratio(ca, a) > 0.75:
            s += 0.4
        else:
            s -= 0.3                       # 歌手明显不符，降权
    return s


def _seq_ratio(a, b):
    """粗略相似度：短字符串用最长公共子序列比例。"""
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    prev = [0] * (len(b) + 1)
    for ca in a:
        cur = [0]
        for j, cb in enumerate(b):
            cur.append(prev[j] + 1 if ca == cb else max(cur[j], prev[j + 1]))
        prev = cur
    return prev[-1] / len(a)


def _has_cjk(s):
    return any("\u4e00" <= ch <= "\u9fff" for ch in str(s or ""))


def _artist_conflict(cand, artist):
    """宽松匹配用的歌手冲突判定：对不上就当翻唱处理。

    实测美区 iTunes 的规律：华语原版的歌手多是罗马字（陈奕迅→Eason Chan、
    周杰伦→Jay Chou），而同名翻唱顶上来的多为中文名（晴天→苏念一）或
    与查询歌手完全无关的拉丁名。所以：
      拉丁查询撞中文候选   → 必是翻唱（Beyond→嗷呜一口仙贝）
      中文查询撞拉丁候选   → 放行（大概率是原版罗马字名）
      同为中文/拉丁但对不上 → 按翻唱处理（简繁异名会被误伤，宁可放弃）
    """
    ca, a = norm(cand), norm(artist)
    if not ca or not a:
        return False
    lat_c, lat_a = not _has_cjk(ca), not _has_cjk(a)
    if lat_a and not lat_c:
        return True
    if lat_c and not lat_a:
        return False
    return not (ca == a or ca in a or a in ca)


def _pick(rows, title, artist, min_score=1.5, loose=False):
    """从候选列表里挑最佳项，低于 min_score 视为未命中。

    loose=True 放宽标准给苹果源用：它对华语歌返回繁体歌名 + 罗马字歌手
    （浮誇 / Eason Chan），跟简体的「浮夸 / 陈奕迅」按 norm 完全对不上，
    这时宁可只凭歌名字符重合度高就认下来——反正 Apple 搜索本身已按
    「歌名+歌手」做过一次筛选，候选集很小，误命中风险远低于国内容源。
    """
    if loose:
        # 宽松分满分只有 1.2，门槛必须跟着降，否则永远过不了严格路径的 1.5
        min_score = min(min_score, 0.9)
    best, best_s, best_t = None, min_score, 0.0
    for r in rows:
        t = _score(r.get("title", ""), r.get("artist", ""), title, artist)
        s = t
        if loose:
            ls = _loose_score(r.get("title", ""), title)
            if ls and _LIVEISH.search(r.get("title", "")):
                # Live/翻唱/伴奏版最常见于「原版不在库里、同名翻唱顶上」的场景，
                # 宁可这源拿不到字段也别把好声音现场版当原版写进标签
                ls -= 0.5
            if ls and artist and _artist_conflict(r.get("artist", ""), artist):
                ls -= 0.5          # 歌手对不上按翻唱处理，压到门槛之下
            s = max(s, ls)
        if s > best_s or (best is not None and s == best_s and t > best_t):
            best, best_s, best_t = r, s, t
    return best


def _loose_score(cand_title, title):
    """苹果源专用宽松分：只看歌名的字符重合度，满分 1.2（低于严格命中）。"""
    ct, t = norm(cand_title), norm(title)
    if not ct or not t:
        return 0.0
    if ct == t:
        return 1.2
    shorter, longer = (ct, t) if len(ct) <= len(t) else (t, ct)
    if shorter in longer:
        return 1.0
    # 简繁不同字形时逐字算重合比例，≥0.75 才认；两字歌名简繁常差一字
    # （夸/誇、爱/愛），放宽到 ≥0.5
    hit = sum(1 for ch in shorter if ch in longer) / len(shorter)
    if hit >= 0.75 or (len(shorter) <= 2 and hit >= 0.5):
        return 1.0
    return 0.0


# ---------------------------------------------------------------- 网易云

_WY_H = {"User-Agent": UA, "Referer": "https://music.163.com/", "Cookie": "appver=2.0.2; os=pc"}


def search_wy(title, artist):
    r = requests.get("https://music.163.com/api/search/get/web",
                     params={"s": f"{title} {artist}".strip(), "type": 1,
                             "offset": 0, "total": True, "limit": 10},
                     headers=_WY_H, timeout=TIMEOUT)
    r.raise_for_status()
    songs = ((r.json() or {}).get("result") or {}).get("songs") or []
    rows = []
    for s in songs:
        al = s.get("album") or {}
        rows.append({
            "title": _clean(s.get("name")),
            "artist": _clean("/".join(a.get("name", "") for a in (s.get("artists") or []))),
            "album": _clean(al.get("name")),
            "year": year_from_ms(al.get("publishTime")),
            "sid": str(s.get("id") or ""),
            "duration": (s.get("duration") or 0) / 1000,
            "platform": "wy",
        })
    return _pick(rows, title, artist)


def wy_detail(sid):
    """歌曲详情：封面 picUrl 与发布时间只能从这里取；genres 字段已下线。"""
    if not sid:
        return {}
    try:
        r = requests.post("https://music.163.com/api/v3/song/detail",
                          data={"id": str(sid), "c": f'[{{"id":{sid}}}]'},
                          headers=_WY_H, timeout=TIMEOUT)
        r.raise_for_status()
        songs = (r.json() or {}).get("songs") or []
    except (requests.RequestException, ValueError) as e:
        log_scraper.debug("网易云 song/detail 失败：%s", e)
        return {}
    if not songs:
        return {}
    s = songs[0]
    al = s.get("al") or {}
    return {
        "album": _clean(al.get("name")),
        "cover": al.get("picUrl") or "",
        "year": year_from_ms(s.get("publishTime") or al.get("publishTime")),
        "duration": round((s.get("duration") or 0) / 1000),
        "artist": _clean("/".join(a.get("name", "") for a in (s.get("ar") or []))),
        "alias": [_clean(a.get("name")) for a in (s.get("alia") or [])],
    }


def lrc_wy(sid):
    if not sid:
        return ""
    try:
        r = requests.get("https://music.163.com/api/song/lyric",
                         params={"id": sid, "lv": -1, "kv": -1, "tv": -1},
                         headers=_WY_H, timeout=TIMEOUT)
        r.raise_for_status()
        j = r.json() or {}
    except (requests.RequestException, ValueError):
        return ""
    lrc = (j.get("lrc") or {}).get("lyric") or ""
    return _clean(lrc) if lrc.strip() else ""


# ---------------------------------------------------------------- QQ 音乐

_TX_H = {"User-Agent": UA, "Referer": "https://y.qq.com/"}


def search_tx(title, artist):
    r = requests.get("https://c.y.qq.com/soso/fcgi-bin/client_search_cp",
                     params={"w": f"{title} {artist}".strip(), "format": "json",
                             "p": 1, "n": 10, "t": 0},
                     headers=_TX_H, timeout=TIMEOUT)
    r.raise_for_status()
    lst = (((r.json() or {}).get("data") or {}).get("song") or {}).get("list") or []
    rows = []
    for s in lst:
        rows.append({
            "title": _clean(s.get("songname")),
            "artist": _clean("/".join(x.get("name", "") for x in (s.get("singer") or []))),
            "album": _clean(s.get("albumname")),
            "year": year_of(s.get("pubtime")),
            "cover": f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{s.get('albummid')}.jpg"
                     if s.get("albummid") else "",
            "songmid": s.get("songmid") or "",
            "albummid": s.get("albummid") or "",
            "duration": s.get("interval") or 0,
            "platform": "tx",
        })
    return _pick(rows, title, artist)


def tx_album(albummid):
    """专辑详情：QQ 唯一稳定的流派来源（data.genre = "Pop 流行"），年份在 aDate。"""
    if not albummid:
        return {}
    try:
        r = requests.get("https://c.y.qq.com/v8/fcg-bin/fcg_v8_album_info_cp.fcg",
                         params={"albummid": albummid, "format": "json"},
                         headers=_TX_H, timeout=TIMEOUT)
        r.raise_for_status()
        d = (r.json() or {}).get("data") or {}
    except (requests.RequestException, ValueError) as e:
        log_scraper.debug("QQ 专辑详情失败：%s", e)
        return {}
    if not d:
        return {}
    genre = _clean(d.get("genre")).split(" ")
    return {
        "genre": "; ".join(g for g in genre if g),
        "year": year_of(d.get("aDate")),
        "album": _clean(d.get("name")),
        "publisher": _clean(d.get("company")),
        "songs": len(d.get("list") or []),
    }


def lrc_tx(songmid):
    if not songmid:
        return ""
    try:
        r = requests.get("https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg",
                         params={"songmid": songmid, "format": "json", "nobase64": 1, "g_tk": 5381},
                         headers=_TX_H, timeout=TIMEOUT)
        r.raise_for_status()
        j = r.json() or {}
    except (requests.RequestException, ValueError):
        return ""
    if j.get("retcode") not in (0, None):
        return ""
    lyric = _clean(j.get("lyric"))
    return lyric if len(lyric) > 40 else ""     # 太短的多半是占位


# ---------------------------------------------------------------- 酷狗

_KG_H = {"User-Agent": UA}


def search_kg(title, artist):
    r = requests.get("http://mobilecdn.kugou.com/api/v3/search/song",
                     params={"keyword": f"{title} {artist}".strip(), "page": 1, "pagesize": 10,
                             "showtype": 1, "filter": 0, "utf8": 1, "clientver": 11451,
                             "clienttype": 1, "appid": 1005, "platform": "pc", "noencode": 1},
                     headers=_KG_H, timeout=TIMEOUT)
    r.raise_for_status()
    info = ((r.json() or {}).get("data") or {}).get("info") or []
    rows = []
    for s in info:
        cover = ((s.get("trans_param") or {}).get("union_cover") or "").replace("{size}", "400")
        rows.append({
            "title": _clean(s.get("songname") or s.get("filename")),
            "artist": _clean(s.get("singername")),
            "album": _clean(s.get("album_name")),
            "cover": cover,
            "hash": s.get("hash") or "",
            "album_id": str(s.get("album_id") or ""),
            "duration": s.get("duration") or 0,
            "platform": "kg",
        })
    return _pick(rows, title, artist)


def lrc_kg(song_hash):
    """酷狗歌词：krcs search 返回 candidates[0] 的 id/accesskey，再换 lrc 正文。"""
    if not song_hash:
        return ""
    try:
        r = requests.get("https://krcs.kugou.com/search",
                         params={"ver": 1, "man": "yes", "client": "mobi", "keyword": "",
                                 "duration": "", "hash": song_hash, "album_audio_id": ""},
                         headers=_KG_H, timeout=TIMEOUT)
        r.raise_for_status()
        cands = (r.json() or {}).get("candidates") or []
        if not cands:
            return ""
        r2 = requests.get("https://lyrics.kugou.com/download",
                          params={"ver": 1, "client": "pc", "id": cands[0].get("id"),
                                  "accesskey": cands[0].get("accesskey"),
                                  "fmt": "lrc", "charset": "utf8"},
                          headers=_KG_H, timeout=TIMEOUT)
        r2.raise_for_status()
        return _clean((r2.json() or {}).get("content"))
    except (requests.RequestException, ValueError):
        return ""


# ---------------------------------------------------------------- 酷我

_KW_H = {"User-Agent": UA}


def _kw_object(text):
    """r.s 返回的是 Python dict 字面量（单引号 / True / None），不是合法 JSON。"""
    i = text.find("'abslist':[")
    if i < 0:
        return {}
    raw = text[i + len("'abslist':"):]
    depth, instr, esc, end = 0, False, False, None
    for idx, ch in enumerate(raw):
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == "'":
                instr = False
            continue
        if ch == "'":
            instr = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                end = idx + 1
                break
    if end is None:
        return {}
    try:
        return {"abslist": ast.literal_eval(raw[:end])}
    except (ValueError, SyntaxError):
        return {}


def search_kw(title, artist):
    # rformat=json + encoding=utf8 缺一不可，否则只有纯文本统计头没有结果
    r = requests.get("http://search.kuwo.cn/r.s",
                     params={"all": f"{title} {artist}".strip(), "ft": "music", "pn": 0,
                             "rn": 10, "rformat": "json", "encoding": "utf8"},
                     headers=_KW_H, timeout=TIMEOUT)
    r.raise_for_status()
    rows = []
    for s in _kw_object(r.text).get("abslist") or []:
        rows.append({
            "title": _clean(s.get("SONGNAME")),
            "artist": _clean(s.get("ARTIST")),
            "album": _clean(s.get("ALBUM")),
            "duration": int(s.get("DURATION") or 0),
            "rid": str(s.get("DC_TARGETID") or ""),
            "platform": "kw",
        })
    return _pick(rows, title, artist)


# ---------------------------------------------------------------- 苹果音乐

_AP_H = {"User-Agent": UA}


def _ap_cover(url):
    """artworkUrl100 结尾是 100x100bb.jpg，换成 600x600bb 拿大图（实测有效）。"""
    u = str(url or "")
    return re.sub(r"/\d+x\d+bb\.(jpg|png)$", "/600x600bb.\\1", u) if u else ""


def search_ap(title, artist):
    # country 参数千万不能给 CN：Apple 中国区对搜索请求恒返回 resultCount=0，
    # 省略它走默认区才搜得到华语歌
    r = requests.get("https://itunes.apple.com/search",
                     params={"term": f"{title} {artist}".strip(), "entity": "song",
                             "media": "music", "limit": 15},
                     headers=_AP_H, timeout=TIMEOUT)
    r.raise_for_status()
    rows = []
    for s in (r.json() or {}).get("results") or []:
        rows.append({
            "title": _clean(s.get("trackName")),
            "artist": _clean(s.get("artistName")),
            "album": _clean(s.get("collectionName")),
            "genre": _clean(s.get("primaryGenreName")),
            "year": year_of(s.get("releaseDate")),
            "cover": _ap_cover(s.get("artworkUrl100")),
            "duration": round((s.get("trackTimeMillis") or 0) / 1000),
            "track_id": str(s.get("trackId") or ""),
            "platform": "ap",
        })
    return _pick(rows, title, artist, loose=True)


# ---------------------------------------------------------------- 开放库 MusicBrainz

# MusicBrainz 强制要求 UA 里带项目名与联系方式，否则一律 503
_MB_H = {"User-Agent": "TuneSync/1.0 ( https://github.com/tianxiaodudou/music-meta-web )",
         "Accept": "application/json"}


def search_mb(title, artist):
    """MusicBrainz：开放版权数据库，欧美日韩收录好，中文歌基本没有。"""
    query = f'recording:"{title}"' + (f' AND artist:"{artist}"' if artist else "")
    # 限速 1 req/s：批量刮削多首并发时必撞 503，退避一次再试，仍拒绝就放弃
    for attempt in (1, 2):
        try:
            r = requests.get("https://musicbrainz.org/ws/2/recording",
                             params={"query": query, "fmt": "json", "limit": 10},
                             headers=_MB_H, timeout=TIMEOUT)
            r.raise_for_status()
            break
        except requests.HTTPError as e:
            if attempt == 2 or getattr(e.response, "status_code", 0) != 503:
                raise
            time.sleep(1.5)
    rows = []
    for rec in (r.json() or {}).get("recordings") or []:
        rels = [x for x in (rec.get("releases") or []) if x.get("id")]
        # releases 顺序是任意的，releases[0] 可能是 2023 年的重发合集；
        # 取发行日期最早的才贴近歌曲原版年份
        dated = [x for x in rels if x.get("date")]
        rel = min(dated, key=lambda x: x["date"]) if dated else (rels[0] if rels else {})
        # artist-credit 元素有时是纯字符串有时是 {"artist": {...}}，两种都要兼容
        artist_name = "".join(
            a if isinstance(a, str) else (a.get("artist") or {}).get("name", "")
            for a in (rec.get("artist-credit") or []))
        tags = [_clean(t if isinstance(t, str) else t.get("name"))
                for t in (rec.get("tags") or [])]
        rows.append({
            "title": _clean(rec.get("title")),
            "artist": _clean(artist_name),
            "album": _clean(rel.get("title")),
            "genre": "; ".join(t for t in tags if t),
            "year": year_of(rel.get("date")),
            "cover": mb_cover(rel.get("id")),
            "duration": round((rec.get("length") or 0) / 1000),
            "mbid": rec.get("id") or "",
            "release_id": rel.get("id") or "",
            "platform": "mb",
        })
    return _pick(rows, title, artist)


_MB_COVER_CACHE = {}


def mb_cover(release_id):
    """封面走 Cover Art Archive，实测覆盖率很低（多 404），取不到就返回空。

    带缓存是因为同一张专辑在库里常出现多首歌，不缓存等于反复打同一个慢接口。
    """
    if not release_id:
        return ""
    if release_id in _MB_COVER_CACHE:
        return _MB_COVER_CACHE[release_id]
    url = ""
    try:
        r = requests.get(f"https://coverartarchive.org/release/{release_id}",
                         headers=_MB_H, timeout=TIMEOUT)
        if r.status_code == 200:
            # 响应体是 {"images": [...]} 对象（也可能是裸数组），迭代前先归一
            data = r.json()
            imgs = data.get("images") or [] if isinstance(data, dict) else (data or [])
            for img in imgs:
                if isinstance(img, dict) and img.get("front"):
                    url = ((img.get("thumbnails") or {}).get("1200")
                           or img.get("image") or "")
                    if url:
                        break
    except (requests.RequestException, ValueError):
        url = ""
    _MB_COVER_CACHE[release_id] = url
    return url


# ---------------------------------------------------------------- LRC Lib

def lrc_lrclib(title, artist, album="", duration=0):
    """LRC Lib 免费歌词库（欧美/日韩为主），有时长校验，避免张冠李戴。"""
    kw = {"track_name": title, "artist_name": artist}
    if album:
        kw["album_name"] = album
    if duration:
        kw["duration"] = int(duration)
    try:
        r = requests.get("https://lrclib.net/api/get", params=kw, headers=_KW_H, timeout=TIMEOUT)
        if r.status_code == 200:
            j = r.json() or {}
            for key in ("syncedLyrics", "plainLyrics"):
                if _clean(j.get(key)):
                    return _clean(j[key])
        r = requests.get("https://lrclib.net/api/search", params=kw, headers=_KW_H, timeout=TIMEOUT)
        if r.status_code == 200:
            for item in (r.json() or [])[:3]:
                for key in ("syncedLyrics", "plainLyrics"):
                    if _clean(item.get(key)):
                        return _clean(item[key])
    except (requests.RequestException, ValueError):
        pass
    return ""


# ---------------------------------------------------------------- 合并

SOURCE_SEARCH = {"wy": search_wy, "tx": search_tx, "kg": search_kg, "kw": search_kw,
                  "ap": search_ap, "mb": search_mb}
# 每源搜索命中后拿哪个字段去换歌词（苹果与 MusicBrainz 没有公开 lrc 接口）
SOURCE_LRC_KEY = {"wy": "sid", "tx": "songmid", "kg": "hash"}
SOURCE_LRC = {"wy": lrc_wy, "tx": lrc_tx, "kg": lrc_kg}
# 苹果字段最全（专辑/流派/年份/封面一次到位），但华语要靠宽松匹配，
# 排在国内三源之后当主力补手；MusicBrainz 中文基本没有，放最后兜底欧美日韩。
DEFAULT_ORDER = ["wy", "tx", "kg", "kw", "ap", "mb"]
FIELDS = ("album", "genre", "year", "cover")


def merge_sources(title, artist, order=None, want_lrc=True, log=None):
    """多源搜索并字段级合并，返回 album/genre/year/cover/lrc 及命中溯源。

    order 形如 ["wy","tx","kg","kw"]，按序尝试；先命中的源先占字段，
    后续源只补空缺。任何单源异常都被吞掉记进 log，不影响整体。
    """
    order = [p for p in (order or DEFAULT_ORDER) if p in SOURCE_SEARCH]
    out = {k: "" for k in FIELDS}
    out.update({"lrc": "", "platform": "", "song_id": "", "duration": 0, "hits": {}, "logs": []})
    log = log if log is not None else out["logs"]

    for plat in order:
        fn = SOURCE_SEARCH[plat]
        try:
            cand = fn(title, artist)
        except Exception as e:                 # noqa: BLE001 单源失败不拖累其他源
            log.append(f"{plat} 搜索失败：{str(e)[:80]}")
            continue
        if not cand:
            log.append(f"{plat} 无匹配")
            continue
        if not out["platform"]:
            out["platform"] = plat
            out["song_id"] = cand.get(SOURCE_LRC_KEY.get(plat, ""), "")
            out["duration"] = cand.get("duration") or 0

        # 各源的字段补充接口：wy 要 song/detail 拿封面，tx 要专辑页拿流派与发行日期
        if plat == "wy":
            try:
                extra = wy_detail(cand.get("sid"))
            except Exception as e:             # noqa: BLE001
                log.append(f"wy 详情失败：{str(e)[:80]}")
                extra = {}
            if extra.get("artist"):
                cand["artist"] = extra["artist"]
        elif plat == "tx":
            # QQ 流派只挂在专辑上，搜索结果没有，必须再打一次专辑详情
            try:
                extra = tx_album(cand.get("albummid"))
            except Exception as e:             # noqa: BLE001
                log.append(f"tx 专辑详情失败：{str(e)[:80]}")
                extra = {}
        else:
            extra = {}
        for k, v in extra.items():
            if v and not cand.get(k):
                cand[k] = v

        got = []
        for k in FIELDS:
            if cand.get(k) and not out[k]:
                out[k] = cand[k]
                got.append(k)
                out["hits"][k] = plat
        out["logs"].append(f"{plat} 命中「{cand.get('title')} - {cand.get('artist')}」"
                           f" → {'/'.join(got) if got else '无新字段'}")
        if all(out[k] for k in FIELDS):
            break

    if want_lrc and not out["lrc"]:
        for plat in order:
            fn = SOURCE_LRC.get(plat)
            sid = out["song_id"] if out["platform"] == plat else None
            if not fn or not sid:
                continue
            try:
                out["lrc"] = fn(sid)
            except Exception:                  # noqa: BLE001
                out["lrc"] = ""
            if out["lrc"]:
                out["hits"]["lrc"] = plat
                log.append(f"{plat} 歌词命中 {len(out['lrc'])} 字节")
                break
        if not out["lrc"]:
            out["lrc"] = lrc_lrclib(title, artist, out.get("album", ""), out.get("duration") or 0)
            if out["lrc"]:
                out["hits"]["lrc"] = "lrclib"
                log.append(f"LRC Lib 歌词命中 {len(out['lrc'])} 字节")
            else:
                log.append("所有数据源都没有歌词")
    return out
