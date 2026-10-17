"""songmeta 多源合并刮削的单元测试（全部 mock 网络，不打真接口）。"""
import struct

import requests
from mutagen.flac import FLAC

from tunesync.app import scraper, songmeta


def _tiny_flac():
    """手工拼一个最小合法 FLAC（fLaC magic + 单个 STREAMINFO 块）。

    mutagen 只解析元数据块，不校验音频帧，够 _write_tags 用了。
    """
    # STREAMINFO：blocksize(2+2) framesize(3+3) + 8B(rate/ch/bits/total) + 16B md5
    packed = (44100 << 44) | (1 << 41) | (15 << 36)      # 44100Hz / 2ch / 16bit
    streaminfo = (struct.pack(">HH", 4096, 4096) + b"\x00" * 6
                  + struct.pack(">Q", packed) + b"\x00" * 16)
    # 块头：1 字节 last-flag+type + 3 字节大端长度（不是 4 字节 int！）
    return b"fLaC" + b"\x80" + len(streaminfo).to_bytes(3, "big") + streaminfo


class TestWriteTagsFlac:
    def test_flac_cover_add_picture(self, tmp_path):
        """封面必须走 FLAC 原生图片块；塞进 VorbisComment 会报
        '<Picture ...> needs to be str for key'，整首歌标失败。"""
        cover = b"\xff\xd8fakejpeg" * 10
        p = tmp_path / "a - b.flac"
        p.write_bytes(_tiny_flac())
        ok = scraper._write_tags(str(p), {"title": "a", "artist": "b", "album": "C",
                                          "genre": "Pop", "year": "2003",
                                          "cover_base64": cover})
        assert ok
        f = FLAC(str(p))
        assert f["title"] == ["a"] and f["album"] == ["C"] and f["date"] == ["2003"]
        assert f.pictures and f.pictures[0].data == cover
        assert f.pictures[0].type == 3 and f.pictures[0].mime == "image/jpeg"
        assert "metadata_block_picture" not in f.tags

    def test_flac_without_cover(self, tmp_path):
        p = tmp_path / "x - y.flac"
        p.write_bytes(_tiny_flac())
        assert scraper._write_tags(str(p), {"title": "x", "artist": "y"})
        f = FLAC(str(p))
        assert f["title"] == ["x"] and not f.pictures


class FakeResp:
    def __init__(self, payload=None, status=200, text=""):
        self._payload = payload
        self.status_code = status
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)


# ───────────────────────────── 基础工具

def test_year_of_variants():
    assert songmeta.year_of("2003-07-31") == "2003"
    assert songmeta.year_of("1993-09-01T07:00:00Z") == "1993"
    assert songmeta.year_of("") == ""
    assert songmeta.year_of("no-date-here") == ""
    # 毫秒时间戳：1120000000000ms ≈ 2005-06
    assert songmeta.year_from_ms(1120000000000) == "2005"
    assert songmeta.year_from_ms(None) == ""


def test_ap_cover_upscale():
    assert songmeta._ap_cover("https://a/1.jpg/100x100bb.jpg").endswith("/600x600bb.jpg")
    assert songmeta._ap_cover("https://a/1.png/200x200bb.png").endswith("/600x600bb.png")
    assert songmeta._ap_cover("https://a/no-suffix.jpg") == "https://a/no-suffix.jpg"
    assert songmeta._ap_cover("") == ""
    assert songmeta._ap_cover(None) == ""


def test_loose_score():
    assert songmeta._loose_score("晴天", "晴天") == 1.2
    assert songmeta._loose_score("小幸运", "小") == 1.0          # 互相包含
    assert songmeta._loose_score("浮誇", "浮夸") == 1.0          # 两字简繁差一字
    assert songmeta._loose_score("海闊天空", "海阔天空") == 1.0   # 四字重合 0.75
    assert songmeta._loose_score(" совершенно", "晴天") == 0.0
    assert songmeta._loose_score("", "晴天") == 0.0


def test_artist_conflict():
    # 拉丁查询撞中文候选：必是翻唱
    assert songmeta._artist_conflict("嗷呜一口仙贝", "Beyond") is True
    # 中文查询撞拉丁候选：放行（原版罗马字名）
    assert songmeta._artist_conflict("Eason Chan", "陈奕迅") is False
    # 完全相等
    assert songmeta._artist_conflict("Beyond", "Beyond") is False
    # 同为中文但对不上：按翻唱
    assert songmeta._artist_conflict("苏念一", "周杰伦") is True
    # 合唱版互相包含：不算冲突
    assert songmeta._artist_conflict("周杰伦/费玉清", "周杰伦") is False
    # 同为拉丁但对不上：按翻唱
    assert songmeta._artist_conflict("Boyz II Men", "The Beatles") is True
    # 空值不判冲突
    assert songmeta._artist_conflict("", "周杰伦") is False


def test_pick_strict_vs_loose():
    rows = [{"title": "晴天", "artist": "苏念一", "album": "封面版"}]
    # 严格模式：歌名对但歌手不符 → 拒
    assert songmeta._pick(rows, "晴天", "周杰伦") is None
    # 宽松模式：同为中文名但对不上 → 仍拒（判定为翻唱）
    assert songmeta._pick(rows, "晴天", "周杰伦", loose=True) is None
    # 宽松模式：罗马字歌手放行
    rows2 = [{"title": "浮誇", "artist": "Eason Chan"}]
    hit = songmeta._pick(rows2, "浮夸", "陈奕迅", loose=True)
    assert hit and hit["title"] == "浮誇"
    # Live 版降权到门槛之下
    rows3 = [{"title": "晴天 (Live)", "artist": "伍珂玥"}]
    assert songmeta._pick(rows3, "晴天", "周杰伦", loose=True) is None
    # 真原版（歌手严格匹配）不受 Live 标记影响
    rows4 = [{"title": "海阔天空 (Live)", "artist": "Beyond"}]
    assert songmeta._pick(rows4, "海阔天空", "Beyond") is not None
    # 同分时优先严格分更高的候选
    rows5 = [{"title": "晴天", "artist": "苏念一"}, {"title": "晴天", "artist": "Jay Chou"}]
    hit5 = songmeta._pick(rows5, "晴天", "周杰伦", loose=True)
    assert hit5 and hit5["artist"] == "Jay Chou"


# ───────────────────────────── search_ap（Apple，全部 mock）

def test_search_ap_mock(monkeypatch):
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        payload = {"results": [
            {   # 中文歌手名候选：判定为翻唱，应被跳过
                "trackName": "海阔天空", "artistName": "伍珂玥",
                "collectionName": "翻唱集", "primaryGenreName": "Mandopop",
                "releaseDate": "2021-01-01", "artworkUrl100": "https://a/x/100x100bb.jpg",
                "trackTimeMillis": 300000, "trackId": 1},
            {   # 繁体歌名 + 拉丁歌手：原版，应被选中
                "trackName": "海闊天空", "artistName": "Beyond",
                "collectionName": "樂與怒", "primaryGenreName": "Cantopop/HK-Pop",
                "releaseDate": "1993-09-01T07:00:00Z",
                "artworkUrl100": "https://a/y/100x100bb.jpg",
                "trackTimeMillis": 326000, "trackId": 2},
        ]}
        return FakeResp(payload)

    monkeypatch.setattr(songmeta.requests, "get", fake_get)
    hit = songmeta.search_ap("海阔天空", "Beyond")
    assert hit["platform"] == "ap"
    assert hit["title"] == "海闊天空"
    assert hit["artist"] == "Beyond"
    assert hit["album"] == "樂與怒"
    assert hit["genre"] == "Cantopop/HK-Pop"
    assert hit["year"] == "1993"
    assert hit["cover"].endswith("/600x600bb.jpg")
    assert hit["duration"] == 326
    assert hit["track_id"] == "2"
    # country 参数绝不能出现（Apple 中国区搜索恒返回空）
    assert "country" not in (captured["params"] or {})
    assert captured["params"]["entity"] == "song"


# ───────────────────────────── search_mb / mb_cover（全部 mock）

_MB_REC = {
    "title": "Yesterday",
    "artist-credit": [{"artist": {"name": "The Beatles"}}, " feat. ', "],
    "releases": [
        {"id": "rel-2023", "date": "2023-01-01", "title": "Compilation"},
        {"id": "rel-1965", "date": "1965-08-06", "title": "Help!"},
    ],
    "tags": [{"name": "rock"}, {"name": "britpop"}],
    "length": 125000,
    "id": "rec-1",
}


def test_search_mb_mock(monkeypatch):
    def fake_get(url, params=None, headers=None, timeout=None):
        if "musicbrainz.org" in url:
            assert "TuneSync" in headers["User-Agent"]   # 合规 UA 是硬要求
            return FakeResp({"recordings": [_MB_REC]})
        return FakeResp(None, status=404)                # coverartarchive 404

    monkeypatch.setattr(songmeta.requests, "get", fake_get)
    hit = songmeta.search_mb("Yesterday", "The Beatles")
    assert hit["platform"] == "mb"
    assert hit["artist"] == "The Beatles feat. ',"        # str 与 dict 混排都兼容
    # 取发行日期最早的 release，而不是任意顺序的 releases[0]
    assert hit["album"] == "Help!"
    assert hit["year"] == "1965"
    assert hit["genre"] == "rock; britpop"
    assert hit["duration"] == 125                          # 毫秒 → 秒
    assert hit["cover"] == ""                              # 404 → 空串不致命


def test_mb_cover_shape_and_cache(monkeypatch):
    songmeta._MB_COVER_CACHE.clear()
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        return FakeResp({"images": [
            {"front": False, "image": "https://c/back.jpg"},
            {"front": True, "image": "https://c/front.jpg",
             "thumbnails": {"1200": "https://c/front-1200.jpg"}},
        ]})

    monkeypatch.setattr(songmeta.requests, "get", fake_get)
    assert songmeta.mb_cover("rel-x") == "https://c/front-1200.jpg"
    # 缓存生效：同一 release 第二次不打网络
    assert songmeta.mb_cover("rel-x") == "https://c/front-1200.jpg"
    assert len(calls) == 1
    songmeta._MB_COVER_CACHE.clear()


def test_search_mb_retries_503(monkeypatch):
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(url)
        if len(calls) == 1:
            return FakeResp(None, status=503)
        return FakeResp({"recordings": [_MB_REC]})

    monkeypatch.setattr(songmeta.requests, "get", fake_get)
    monkeypatch.setattr(songmeta.time, "sleep", lambda s: None)
    hit = songmeta.search_mb("Yesterday", "The Beatles")
    assert hit["title"] == "Yesterday"
    mb_calls = [u for u in calls if "musicbrainz.org" in u]
    assert len(mb_calls) == 2                              # 503 退避后重试成功


def test_source_registration():
    assert set(songmeta.SOURCE_SEARCH) == {"wy", "tx", "kg", "kw", "ap", "mb"}
    assert songmeta.DEFAULT_ORDER == ["wy", "tx", "kg", "kw", "ap", "mb"]
    # 苹果与开放库没有公开 lrc 接口，不该出现在歌词换源表里
    assert "ap" not in songmeta.SOURCE_LRC and "mb" not in songmeta.SOURCE_LRC


# ───────────────────────────── merge_sources 字段级合并

def test_merge_sources_field_merge(monkeypatch):
    def fake_wy(title, artist):
        return {"title": title, "artist": artist, "album": "叶惠美", "year": "2003",
                "sid": "666", "duration": 269, "platform": "wy"}

    def fake_ap(title, artist):
        return {"title": title, "artist": artist, "album": "", "genre": "Mandopop",
                "year": "", "cover": "https://a/600x600bb.jpg", "duration": 271,
                "platform": "ap"}

    monkeypatch.setitem(songmeta.SOURCE_SEARCH, "wy", fake_wy)
    monkeypatch.setitem(songmeta.SOURCE_SEARCH, "ap", fake_ap)
    monkeypatch.setattr(songmeta, "wy_detail", lambda sid: {})
    monkeypatch.setattr(songmeta, "tx_album", lambda mid: {})
    out = songmeta.merge_sources("晴天", "周杰伦", order=["wy", "ap"], want_lrc=False)
    # 先到先用：专辑/年份归 wy，流派/封面由 ap 补
    assert out["album"] == "叶惠美" and out["hits"]["album"] == "wy"
    assert out["year"] == "2003" and out["hits"]["year"] == "wy"
    assert out["genre"] == "Mandopop" and out["hits"]["genre"] == "ap"
    assert out["cover"] == "https://a/600x600bb.jpg" and out["hits"]["cover"] == "ap"
    assert out["platform"] == "wy" and out["song_id"] == "666" and out["duration"] == 269
    assert len(out["logs"]) == 2


def test_merge_sources_survives_failure(monkeypatch):
    def boom(title, artist):
        raise RuntimeError("503")

    def fake_tx(title, artist):
        return {"title": title, "artist": artist, "album": "A", "genre": "Pop",
                "year": "2001", "cover": "c", "songmid": "m1", "duration": 200,
                "platform": "tx"}

    monkeypatch.setitem(songmeta.SOURCE_SEARCH, "wy", boom)
    monkeypatch.setitem(songmeta.SOURCE_SEARCH, "tx", fake_tx)
    monkeypatch.setattr(songmeta, "wy_detail", lambda sid: {})
    monkeypatch.setattr(songmeta, "tx_album", lambda mid: {})
    out = songmeta.merge_sources("歌", "手", order=["wy", "tx"], want_lrc=False)
    assert out["album"] == "A" and out["hits"]["album"] == "tx"
    assert any("wy 搜索失败" in x for x in out["logs"])


# ───────────────────────────── scraper.source_order 白名单

def test_source_order_whitelist(env, settings):
    settings(scraper={"order": ["ap", "mb", "bogus", "WY"]})
    assert scraper.source_order() == ["ap", "mb", "wy"]     # 未知值过滤、大小写归一
    settings(scraper={"order": "not-a-list"})
    assert scraper.source_order() == list(songmeta.DEFAULT_ORDER)
    settings(scraper={"order": []})
    assert scraper.source_order() == list(songmeta.DEFAULT_ORDER)
