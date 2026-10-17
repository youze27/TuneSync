"""recommender：LLM 歌单生成。HTTP 全部打桩，验证请求形状、容错解析与错误语义。"""
import json
from unittest import mock

import pytest
import requests

from tunesync.app import recommender


def _llm_content(payload):
    """把 generate_playlists 期望的返回打包成 OpenAI 兼容响应体。"""
    return {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]}


def _resp(status=200, data=None):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = data if data is not None else {}
    if status >= 400:
        err = requests.HTTPError(f"{status}")
        err.response = r
        r.raise_for_status.side_effect = err
    return r


@pytest.fixture
def llm(settings):
    """已配置好的 LLM（端点指向本机假服务）+ 睡眠打桩。"""
    settings(llm={"base_url": "http://127.0.0.1:8000/v1", "api_key": "sk-test",
                  "model": "test-model", "tracks_per_playlist": 30})
    with mock.patch.object(recommender.time, "sleep"):
        yield settings


GOOD = {"playlists": [
    {"name": "抖音热歌榜TOP50丨全网都在循环",
     "tracks": [{"title": "晴天", "artist": "周杰伦"}, {"title": "孤勇者", "artist": "陈奕迅"}]},
    {"name": "深夜自习室·白噪音钢琴",
     "tracks": [{"title": "River Flows in You", "artist": "李闰珉"}]},
]}


def _post_stub(content=GOOD, status=200):
    """返回 (post 函数, calls)：calls 记录每次请求的 (url, json, headers)。"""
    calls = []

    def _post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "body": json, "headers": headers})
        data = _llm_content(content) if status == 200 else {"error": {"message": "boom"}}
        return _resp(status, data)

    return _post, calls


class TestRequestShape:
    def test_posts_to_chat_completions_with_auth(self, llm):
        _post, calls = _post_stub(GOOD)
        with mock.patch.object(recommender.requests, "post", side_effect=_post):
            recommender.generate_playlists("抖音热歌", 2)
        assert len(calls) == 1
        c = calls[0]
        assert c["url"] == "http://127.0.0.1:8000/v1/chat/completions"
        assert c["headers"]["Authorization"] == "Bearer sk-test"
        assert c["body"]["model"] == "test-model"
        assert c["body"]["response_format"] == {"type": "json_object"}
        # 主题进 user 消息，系统提示词固定
        assert "抖音热歌" in c["body"]["messages"][-1]["content"]
        assert c["body"]["messages"][0]["role"] == "system"
        assert "歌单数量：2" in c["body"]["messages"][-1]["content"]

    def test_custom_prompt_replaces_scene_as_topic(self, llm):
        _post, calls = _post_stub(GOOD)
        with mock.patch.object(recommender.requests, "post", side_effect=_post):
            recommender.generate_playlists("抖音热歌", 1, prompt="深夜emo快歌")
        assert "深夜emo快歌" in calls[0]["body"]["messages"][-1]["content"]

    def test_no_api_key_sends_no_auth_header(self, env):
        from tunesync.app import core
        core.kv_set("settings", {"llm": {"base_url": "http://127.0.0.1:8000/v1",
                                         "model": "m"}})
        _post, calls = _post_stub(GOOD)
        with mock.patch.object(recommender.requests, "post", side_effect=_post), \
             mock.patch.object(recommender.time, "sleep"):
            recommender.generate_playlists("抖音热歌", 1)
        assert "Authorization" not in calls[0]["headers"]


class TestParsing:
    def test_generates_structured_playlists(self, llm):
        _post, _ = _post_stub(GOOD)
        with mock.patch.object(recommender.requests, "post", side_effect=_post):
            out = recommender.generate_playlists("抖音热歌", 2)
        assert len(out) == 2
        assert out[0]["name"] == "抖音热歌榜TOP50丨全网都在循环"
        assert out[0]["tracks"][0] == {"title": "晴天", "artist": "周杰伦", "album": ""}

    def test_markdown_fence_is_stripped(self, llm):
        fenced = "```json\n" + json.dumps(GOOD, ensure_ascii=False) + "\n```"
        with mock.patch.object(recommender.requests, "post",
                               side_effect=lambda *a, **kw: _resp(200, {
                                   "choices": [{"message": {"content": fenced}}]})):
            out = recommender.generate_playlists("抖音热歌", 1)
        assert out[0]["name"] == GOOD["playlists"][0]["name"]

    def test_prose_around_json_is_tolerated(self, llm):
        text = "好的，这是你要的歌单：\n" + json.dumps(GOOD, ensure_ascii=False) + "\n请查收。"
        with mock.patch.object(recommender.requests, "post",
                               side_effect=lambda *a, **kw: _resp(200, {
                                   "choices": [{"message": {"content": text}}]})):
            out = recommender.generate_playlists("抖音热歌", 1)
        assert len(out) == 1

    def test_invalid_json_raises(self, llm):
        with mock.patch.object(recommender.requests, "post",
                               side_effect=lambda *a, **kw: _resp(200, {
                                   "choices": [{"message": {"content": "这不是 JSON"}}]})):
            with pytest.raises(recommender.RecommenderError, match="不是有效 JSON"):
                recommender.generate_playlists("抖音热歌", 1)

    def test_missing_playlists_array_raises(self, llm):
        with mock.patch.object(recommender.requests, "post",
                               side_effect=lambda *a, **kw: _resp(200, {
                                   "choices": [{"message": {"content": '{"foo": 1}'}}]})):
            with pytest.raises(recommender.RecommenderError, match="playlists"):
                recommender.generate_playlists("抖音热歌", 1)

    def test_empty_playlists_raises(self, llm):
        with mock.patch.object(recommender.requests, "post",
                               side_effect=lambda *a, **kw:
                                   _resp(200, _llm_content({"playlists": []}))):
            with pytest.raises(recommender.RecommenderError, match="未生成任何可用歌单"):
                recommender.generate_playlists("抖音热歌", 1)

    def test_entries_without_name_or_tracks_are_skipped(self, llm):
        mixed = {"playlists": [
            {"name": "", "tracks": [{"title": "x", "artist": "y"}]},   # 没名字
            {"name": "只有名字"},                                       # 没曲目
            {"name": "正常歌单", "tracks": [{"title": "晴天", "artist": "周杰伦"}]},
            "不是字典",
        ]}
        with mock.patch.object(recommender.requests, "post",
                               side_effect=lambda *a, **kw:
                                   _resp(200, _llm_content(mixed))):
            out = recommender.generate_playlists("抖音热歌", 5)
        assert [p["name"] for p in out] == ["正常歌单"]

    def test_tracks_without_title_are_dropped(self, llm):
        payload = {"playlists": [{"name": "歌单", "tracks": [
            {"title": "", "artist": "x"}, {"title": "晴天", "artist": "周杰伦"}]}]}
        with mock.patch.object(recommender.requests, "post",
                               side_effect=lambda *a, **kw:
                                   _resp(200, _llm_content(payload))):
            out = recommender.generate_playlists("抖音热歌", 1)
        assert [t["title"] for t in out[0]["tracks"]] == ["晴天"]

    def test_tracks_per_playlist_caps_output(self, llm):
        five = {"playlists": [{"name": "歌单",
                               "tracks": [{"title": f"歌{i}", "artist": "甲"}
                                          for i in range(5)]}]}
        llm(llm={"tracks_per_playlist": 2})
        with mock.patch.object(recommender.requests, "post",
                               side_effect=lambda *a, **kw:
                                   _resp(200, _llm_content(five))):
            out = recommender.generate_playlists("抖音热歌", 1)
        assert len(out[0]["tracks"]) == 2

    def test_count_caps_number_of_playlists(self, llm):
        with mock.patch.object(recommender.requests, "post",
                               side_effect=lambda *a, **kw:
                                   _resp(200, _llm_content({"playlists": [
                                       {"name": f"歌单{i}",
                                        "tracks": [{"title": "晴天", "artist": "周杰伦"}]}
                                       for i in range(5)]}))):
            out = recommender.generate_playlists("抖音热歌", 2)
        assert len(out) == 2

    def test_malformed_response_shape_raises(self, llm):
        with mock.patch.object(recommender.requests, "post",
                               side_effect=lambda *a, **kw: _resp(200, {"nope": 1})):
            with pytest.raises(recommender.RecommenderError, match="响应格式异常"):
                recommender.generate_playlists("抖音热歌", 1)


class TestConfigAndErrors:
    def test_not_configured_raises_before_any_http(self, env):
        """config.yaml 的 llm 节是空的，必须给出可操作的报错。"""
        with mock.patch.object(recommender.requests, "post") as post:
            with pytest.raises(recommender.RecommenderError, match="LLM 未配置"):
                recommender.generate_playlists("抖音热歌", 1)
        assert post.call_count == 0

    def test_is_configured(self, llm):
        from tunesync.app import core
        assert recommender.is_configured() is True
        core.kv_set("settings", {"llm": {"base_url": "", "model": "m"}})
        assert recommender.is_configured() is False

    def test_5xx_retries_then_succeeds(self, llm):
        responses = [_resp(500), _resp(200, _llm_content(GOOD))]
        with mock.patch.object(recommender.requests, "post", side_effect=responses):
            out = recommender.generate_playlists("抖音热歌", 1)
        assert len(out) == 1

    def test_5xx_exhausting_retries_raises(self, llm):
        with mock.patch.object(recommender.requests, "post",
                               side_effect=[_resp(500), _resp(500)]):
            with pytest.raises(recommender.RecommenderError, match="服务不可用"):
                recommender.generate_playlists("抖音热歌", 1)

    def test_4xx_raises_immediately_without_retry(self, llm):
        post = mock.Mock(return_value=_resp(401))
        with mock.patch.object(recommender.requests, "post", post):
            with pytest.raises(recommender.RecommenderError, match="401"):
                recommender.generate_playlists("抖音热歌", 1)
        assert post.call_count == 1

    def test_network_error_retries_then_raises(self, llm):
        post = mock.Mock(side_effect=requests.ConnectionError("refused"))
        with mock.patch.object(recommender.requests, "post", post):
            with pytest.raises(recommender.RecommenderError, match="服务不可用"):
                recommender.generate_playlists("抖音热歌", 1)
        assert post.call_count == recommender.RETRY + 1

    def test_empty_topic_raises(self, llm):
        with pytest.raises(recommender.RecommenderError, match="主题为空"):
            recommender.generate_playlists("", 1, prompt="  ")
