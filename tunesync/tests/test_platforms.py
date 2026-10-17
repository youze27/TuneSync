"""platforms：广场/分类/详情解析、批量曲目、链接识别。"""
import json
from unittest import mock

import pytest

from tunesync.app import platforms


def _resp(payload):
    r = mock.Mock()
    r.json.return_value = payload
    r.text = json.dumps(payload)
    r.raise_for_status.return_value = None
    r.status_code = 200
    return r


class TestCategories:
    def test_wy_static_list(self):
        cats = platforms.categories("wy")
        assert cats[0] == {"value": "华语", "label": "华语"}
        assert all(c["value"] and c["label"] for c in cats)

    def test_tx_from_tag_conf_with_unescape(self, monkeypatch):
        platforms._tx_tag_cache.clear()
        payload = {"data": {"categories": [
            {"items": [{"categoryId": 10000000, "categoryName": "全部"},
                       {"categoryId": 8, "categoryName": "R&#38;B"}]}]}}
        monkeypatch.setattr(platforms.requests, "get", lambda *a, **k: _resp(payload))
        cats = platforms.categories("tx")
        assert cats[0] == {"value": "10000000", "label": "全部"}
        assert cats[1]["label"] == "R&B"          # HTML 实体要还原

    def test_tx_cached_within_ttl(self, monkeypatch):
        platforms._tx_tag_cache.clear()
        n = []

        def fake_get(*a, **k):
            n.append(1)
            return _resp({"data": {"categories": [
                {"items": [{"categoryId": 1, "categoryName": "x"}]}]}})

        monkeypatch.setattr(platforms.requests, "get", fake_get)
        platforms.categories("tx")
        platforms.categories("tx")
        assert len(n) == 1                         # 第二次命中缓存

    def test_search_platforms_return_presets(self):
        """kg/kw/mg/all 是搜索式广场，分类即预设关键词。"""
        for p in ("kg", "kw", "mg", "all"):
            cats = platforms.categories(p)
            assert cats[0] == {"value": "经典", "label": "经典"}
            assert all(c["value"] and c["label"] for c in cats)

    def test_unknown_platform(self):
        with pytest.raises(platforms.PlatformError):
            platforms.categories("xx")


class TestSquare:
    def test_wy_maps_fields(self, monkeypatch):
        payload = {"code": 200, "total": 1, "playlists": [
            {"id": 42, "name": "热歌", "coverImgUrl": "http://c/1.jpg",
             "trackCount": 10, "playCount": 12345, "creator": {"nickname": "u"}}]}
        seen = {}

        def fake_get(url, **kw):
            seen["url"], seen["params"] = url, kw.get("params")
            return _resp(payload)

        monkeypatch.setattr(platforms.requests, "get", fake_get)
        out = platforms.square("wy", cat="华语", offset=30, limit=20)
        assert out["total"] == 1
        assert out["lists"][0] == {"id": "42", "name": "热歌", "cover": "http://c/1.jpg",
                                   "count": 10, "play": 12345, "creator": "u"}
        assert seen["params"]["cat"] == "华语" and seen["params"]["offset"] == 30

    def test_wy_rate_limited_raises(self, monkeypatch):
        monkeypatch.setattr(platforms.requests, "get",
                            lambda *a, **k: _resp({"code": -460}))
        with pytest.raises(platforms.PlatformError, match="限流"):
            platforms.square("wy")

    def test_tx_maps_fields_and_window(self, monkeypatch):
        payload = {"data": {"sum": 500, "list": [
            {"dissid": 7707261125, "dissname": "说唱", "imgurl": "http://i/2.jpg",
             "song_count": 66, "listennum": 8888, "nickname": "up"}]}}
        seen = {}

        def fake_get(url, **kw):
            seen["params"] = kw.get("params")
            return _resp(payload)

        monkeypatch.setattr(platforms.requests, "get", fake_get)
        out = platforms.square("tx", cat="165", offset=60, limit=30)
        assert out["total"] == 500
        assert out["lists"][0]["id"] == "7707261125" and out["lists"][0]["count"] == 66
        assert seen["params"]["categoryId"] == "165"
        assert seen["params"]["sin"] == 60 and seen["params"]["ein"] == 89

    def test_kg_maps_fields(self, monkeypatch):
        payload = {"status": 1, "errcode": 0, "data": {"total": 480, "info": [
            {"specialid": 8851337, "specialname": "经典",
             "nickname": "u", "imgurl": "http://i/3.jpg",
             "songcount": 300, "playcount": 29908154}]}}
        seen = {}

        def fake_get(url, **kw):
            seen["url"], seen["params"] = url, kw.get("params")
            return _resp(payload)

        monkeypatch.setattr(platforms.requests, "get", fake_get)
        out = platforms.square("kg", cat="经典", offset=60, limit=30)   # offset 60 → 第 3 页
        assert out["total"] == 480
        assert out["lists"][0]["id"] == "8851337" and out["lists"][0]["count"] == 300
        assert seen["params"]["page"] == 3 and seen["params"]["pagesize"] == 30
        assert "search/special" in seen["url"]

    def test_kw_single_quote_json(self, monkeypatch):
        """酷我搜索响应是单引号 JS 字面量，必须容错解析。"""
        raw = ("{'TOTAL':'150','abslist':[{'playlistid':'3690453083','name':'经典老歌',"
               "'nickname':'kw1','pic':'http://i/4.jpg','songnum':'85','playcnt':'1526'}]}")
        r = mock.Mock()
        r.json.side_effect = ValueError("not json")
        r.text = raw
        r.raise_for_status.return_value = None

        def fake_get(url, **kw):
            assert "search.kuwo.cn/r.s" in url and kw["params"]["ft"] == "playlist"
            return r

        monkeypatch.setattr(platforms.requests, "get", fake_get)
        out = platforms.square("kw", cat="经典", offset=0, limit=30)
        assert out["total"] == 150
        assert out["lists"][0] == {"id": "3690453083", "name": "经典老歌",
                                   "cover": "http://i/4.jpg", "count": 85,
                                   "play": 1526, "creator": "kw1"}

    def test_mg_maps_fields(self, monkeypatch):
        payload = {"code": "000000", "songListResultData": {"totalCount": "200", "result": [
            {"id": "234168499", "name": "老歌藏时光", "userName": "mg1",
             "musicListPicUrl": "http://i/5.jpg", "musicNum": 118, "playNum": "9999"}]}}
        seen = {}

        def fake_get(url, **kw):
            seen["url"], seen["params"], seen["headers"] = url, kw.get("params"), kw.get("headers")
            return _resp(payload)

        monkeypatch.setattr(platforms.requests, "get", fake_get)
        out = platforms.square("mg", cat="经典", offset=0, limit=30)
        assert out["total"] == 200
        assert out["lists"][0]["id"] == "234168499" and out["lists"][0]["play"] == 9999
        assert seen["headers"]["sign"] and seen["headers"]["deviceId"]
        assert '"songlist":1' in seen["params"]["searchSwitch"]

    def test_all_aggregates_and_tolerates_failure(self, monkeypatch):
        """聚合：kg 挂、kw/mg 通 → 列表带 platform 且 errors 含 kg。"""
        def fake_get(url, **kw):
            if "kugou" in url:
                raise platforms.requests.ConnectionError("boom")
            if "kuwo" in url:
                return _resp({"TOTAL": "1", "abslist": [
                    {"playlistid": "1", "name": "酷我歌单", "nickname": "k",
                     "pic": "", "songnum": "2", "playcnt": "9"}]})
            return _resp({"code": "000000", "songListResultData": {"totalCount": "1", "result": [
                {"id": "2", "name": "咪咕歌单", "userName": "m", "musicListPicUrl": "",
                 "musicNum": 3, "playNum": 8}]}})

        monkeypatch.setattr(platforms.requests, "get", fake_get)
        out = platforms.square("all", cat="民谣", offset=0, limit=30)
        assert out["total"] == 2
        plats = {i["platform"] for i in out["lists"]}
        assert plats == {"kw", "mg"}
        assert "kg" in out["errors"]

    def test_all_all_fail_raises(self, monkeypatch):
        def fake_get(url, **kw):
            raise platforms.requests.ConnectionError("down")

        monkeypatch.setattr(platforms.requests, "get", fake_get)
        with pytest.raises(platforms.PlatformError, match="聚合搜索全部失败"):
            platforms.square("all", cat="民谣")


class TestDetail:
    def test_wy_tracks_from_batched_song_detail(self, monkeypatch):
        detail_payload = {"code": 200, "playlist": {
            "name": "热歌", "coverImgUrl": "http://c/x.jpg", "trackCount": 3,
            "trackIds": [{"id": i} for i in (1, 2, 3)]}}
        song_payload = {"code": 200, "songs": [
            {"name": f"歌{i}", "ar": [{"name": "A"}, {"name": "B"}],
             "al": {"name": "专辑"}, "id": i} for i in (1, 2, 3)]}
        posts = []

        monkeypatch.setattr(platforms.requests, "get",
                            lambda *a, **k: _resp(detail_payload))

        def fake_post(url, **kw):
            posts.append(kw.get("data"))
            return _resp(song_payload)

        monkeypatch.setattr(platforms.requests, "post", fake_post)
        out = platforms.detail("wy", "42")
        assert out["platform"] == "wy" and out["name"] == "热歌" and out["count"] == 3
        assert out["tracks"][0]["title"] == "歌1"
        assert out["tracks"][0]["artist"] == "A/B"
        assert out["tracks"][0]["album"] == "专辑"
        assert out["tracks"][0]["raw"] == {"id": 1}
        assert len(posts) == 1                     # 3 首 → 单批

    def test_wy_large_playlist_batches(self, monkeypatch):
        tids = list(range(250))
        detail_payload = {"code": 200, "playlist": {
            "name": "大歌单", "trackIds": [{"id": t} for t in tids]}}
        calls = []

        monkeypatch.setattr(platforms.requests, "get",
                            lambda *a, **k: _resp(detail_payload))

        def fake_post(url, **kw):
            calls.append(1)
            return _resp({"code": 200, "songs": []})

        monkeypatch.setattr(platforms.requests, "post", fake_post)
        platforms.detail("wy", "42")
        assert len(calls) == 2                     # 250 首 → 200 + 50 两批

    def test_wy_song_detail_rate_limited_raises(self, monkeypatch):
        detail_payload = {"code": 200, "playlist": {"trackIds": [{"id": 1}]}}
        monkeypatch.setattr(platforms.requests, "get",
                            lambda *a, **k: _resp(detail_payload))
        monkeypatch.setattr(platforms.requests, "post",
                            lambda *a, **k: _resp({"code": -460}))
        with pytest.raises(platforms.PlatformError):
            platforms.detail("wy", "42")

    def test_tx_maps_songlist(self, monkeypatch):
        payload = {"data": {"cdlist": [{
            "dissname": "说唱精选", "logo": "http://i/x.jpg", "cur_song_num": 2,
            "songlist": [
                {"songname": "你的", "songmid": "002xTz", "songid": 127404639,
                 "singer": [{"name": "DouDou"}, {"name": "Viva"}], "albumname": "你的"},
                {"songname": "第二首", "songmid": "m2", "songid": 2,
                 "singer": [{"name": "S"}], "albumname": "A2"}]}]}}
        monkeypatch.setattr(platforms.requests, "get", lambda *a, **k: _resp(payload))
        out = platforms.detail("tx", "7707261125")
        assert out["platform"] == "tx" and out["name"] == "说唱精选"
        assert out["tracks"][0]["title"] == "你的"
        assert out["tracks"][0]["artist"] == "DouDou/Viva"
        assert out["tracks"][0]["raw"] == {"songmid": "002xTz", "songid": 127404639}

    def test_tx_empty_songlist_returns_empty_tracks(self, monkeypatch):
        """隐私歌单空 songlist：不抛错，交由导入侧记 error。"""
        monkeypatch.setattr(platforms.requests, "get",
                            lambda *a, **k: _resp({"data": {"cdlist": [{}]}}))
        out = platforms.detail("tx", "123")
        assert out["tracks"] == []

    def test_kg_detail_html_plus_gateway(self, monkeypatch):
        html_page = ('<script>var global = {name: "经典合集", pic: "http://i/kg.jpg", id: 8851337};\n'
                     'var global.data = [{"hash":"h1"},{"hash":"h2"},{"hash":"h1"}];</script>')
        gateway_payload = {"status": 1, "data": [
            [{"audio_info": {"audio_id": "a1", "hash": "h1"},
              "author_name": "BEYOND", "ori_audio_name": "灰色轨迹",
              "songname": "灰色轨迹 (电影(天若有情)歌曲)",
              "album_info": {"album_id": "1", "album_name": "精选"}}],
            [{"audio_info": {"audio_id": "a1", "hash": "h1"},
              "author_name": "BEYOND", "ori_audio_name": "灰色轨迹",
              "songname": "重复项", "album_info": {"album_id": "1", "album_name": "精选"}}],
        ]}
        posts = []

        monkeypatch.setattr(platforms.requests, "get",
                            lambda *a, **k: mock.Mock(text=html_page,
                                                      raise_for_status=lambda: None))

        def fake_post(url, json=None, **kw):
            posts.append((url, json, kw.get("headers")))
            return _resp(gateway_payload)

        monkeypatch.setattr(platforms.requests, "post", fake_post)
        out = platforms.detail("kg", "8851337")
        assert out["platform"] == "kg" and out["name"] == "经典合集"
        assert out["cover"] == "http://i/kg.jpg"
        assert len(out["tracks"]) == 1             # 重复 audio_id 去重
        assert out["tracks"][0]["title"] == "灰色轨迹"   # 优先 ori_audio_name
        assert out["tracks"][0]["artist"] == "BEYOND"
        assert out["tracks"][0]["raw"] == {"hash": "h1", "audio_id": "a1"}
        assert len(posts) == 1                     # 3 hash（含 1 重复）→ 单批
        url, body, headers = posts[0]
        assert "gateway.kugou.com" in url
        assert body["key"] == "OIlwieks28dk2k092lksi2UIkp"
        assert len(body["data"]) == 3 and {"hash": "h1"} in body["data"]
        assert headers["x-router"] == "kmr.service.kugou.com"

    def test_kw_detail_maps_fields(self, monkeypatch):
        payload = {"result": "ok", "total": 2, "title": "经典老歌", "pic": "http://i/kw.jpg",
                   "uname": "kw1", "playnum": 1526, "musiclist": [
                       {"name": "Slam", "artist": "Pendulum", "album": "Slam/Out Here",
                        "id": "94783438", "duration": "344", "N_MINFO": "level:x,bitrate:1"},
                       {"name": "第二首", "artist": "A&B", "album": "专辑2",
                        "id": "94783439", "duration": "200"}]}
        seen = {}

        def fake_get(url, **kw):
            seen["url"], seen["params"] = url, kw.get("params")
            return _resp(payload)

        monkeypatch.setattr(platforms.requests, "get", fake_get)
        out = platforms.detail("kw", "3690453083")
        assert out["platform"] == "kw" and out["name"] == "经典老歌" and out["count"] == 2
        assert out["tracks"][0]["title"] == "Slam"
        assert out["tracks"][0]["artist"] == "Pendulum"
        assert out["tracks"][1]["artist"] == "A、B"        # 多歌手 & 分隔换顿号
        assert out["tracks"][0]["raw"] == {"id": "94783438"}
        assert "nplserver.kuwo.cn" in seen["url"] and seen["params"]["rn"] == 1000

    def test_mg_detail_pages_until_total(self, monkeypatch):
        info_payload = {"code": "000000", "data": {
            "title": "老歌藏时光", "imgItem": {"img": "http://i/mg.jpg"},
            "summary": "d", "ownerName": "mg1", "opNumItem": {"playNum": 9999}}}
        page1 = [{"songId": str(i), "songName": f"歌{i}", "singerList": [{"name": "S1"}, {"name": "S2"}],
                  "album": "专辑", "copyrightId": f"c{i}"} for i in range(50)]
        page2 = [{"songId": "50", "songName": "歌50", "singerList": [{"name": "S"}],
                  "album": "专辑", "copyrightId": "c50"},
                 {"songId": "51", "songName": "歌51", "singerList": [], "album": "",
                  "copyrightId": "c51"}]
        pages = {1: {"code": "000000", "data": {"totalCount": 52, "songList": page1}},
                 2: {"code": "000000", "data": {"totalCount": 52, "songList": page2}}}
        seen = {}

        def fake_get(url, **kw):
            seen["url"] = url
            if "c.musicapp.migu.cn" in url:
                return _resp(info_payload)
            assert "app.c.nf.migu.cn" in url
            return _resp(pages[kw["params"]["pageNo"]])

        monkeypatch.setattr(platforms.requests, "get", fake_get)
        out = platforms.detail("mg", "234168499")
        assert out["platform"] == "mg" and out["name"] == "老歌藏时光"
        assert out["cover"] == "http://i/mg.jpg" and out["count"] == 52
        assert len(out["tracks"]) == 52            # 第 2 页不满 50 → 停
        assert out["tracks"][0]["artist"] == "S1、S2"
        assert out["tracks"][0]["raw"]["songId"] == "0"
        assert "playlist/song/v2.0" in seen["url"]


class TestParseLink:
    def test_wy_query_url(self):
        assert platforms.parse_link("https://music.163.com/#/playlist?id=3778678") == ("wy", "3778678")

    def test_wy_path_url(self):
        assert platforms.parse_link("https://music.163.com/playlist/3778678") == ("wy", "3778678")

    def test_tx_playlist_url(self):
        assert platforms.parse_link("https://y.qq.com/n/ryqq/playlist/7267024244") == ("tx", "7267024244")

    def test_tx_taoge_share(self):
        assert platforms.parse_link(
            "https://i.y.qq.com/n2/m/share/details/taoge.html?id=7267024244&host=wechat"
        ) == ("tx", "7267024244")

    def test_kg_special_url(self):
        assert platforms.parse_link("https://www.kugou.com/special/8851337.html") == ("kg", "8851337")

    def test_kg_id_html_url(self):
        assert platforms.parse_link(
            "https://www.kugou.com/speciallist/single/abc-id-8851337.html"
        ) == ("kg", "8851337")

    def test_kw_play_detail_url(self):
        assert platforms.parse_link("https://kuwo.cn/play_detail/3690453083") == ("kw", "3690453083")

    def test_kw_playlist_detail_url(self):
        assert platforms.parse_link("https://kuwo.cn/playlist_detail/123") == ("kw", "123")

    def test_mg_playlist_url(self):
        assert platforms.parse_link("https://music.migu.cn/v3/music/playlist/234168499") == ("mg", "234168499")

    def test_digits_with_platform_hint(self):
        assert platforms.parse_link("3778678", "wy") == ("wy", "3778678")

    def test_digits_without_hint(self):
        assert platforms.parse_link("3778678") == ("", "3778678")

    def test_unrecognized_raises(self):
        with pytest.raises(platforms.PlatformError):
            platforms.parse_link("https://example.com/foo?id=1")

    def test_empty_raises(self):
        with pytest.raises(platforms.PlatformError):
            platforms.parse_link("  ")
