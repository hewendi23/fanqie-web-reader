"""段评楼中楼：列表只带 reply_count，回复正文按需另拉。"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from routers.comments import _fqapi_reply_url, _normalize_comments, _normalize_replies
from server import app

BOOK = "7444482186822044734"
CHAPTER = "7444575111484539454"


@pytest.fixture
def client():
    test_client = TestClient(app)
    yield test_client
    test_client.close()


def _comment_item(*, comment_id, text, reply_count, reply_show_count=1, reply_list=None, image_url=""):
    content = {"text": text}
    if image_url:
        content["image_data_list"] = {"0": {"url": image_url}}
    item = {
        "comment": {
            "comment_id": comment_id,
            "common": {
                "content": content,
                "create_timestamp": 1700000000,
                "user_info": {"base_info": {"user_name": "读者甲", "user_avatar": ""}},
            },
            "stat": {"digg_count": 3, "reply_count": reply_count},
            "expand": {"reply_show_count": reply_show_count, "reply_show_row": 3},
        }
    }
    if reply_list is not None:
        item["comment"]["reply_list"] = reply_list
    return item


def _list_payload(*items):
    return {"code": 0, "data": {"data_list": list(items)}}


class _JsonResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_normalize_comments_extracts_comment_id_and_reply_count():
    comments = _normalize_comments(_list_payload(_comment_item(
        comment_id="991122", text="主评", reply_count=4,
    )))
    assert len(comments) == 1
    assert comments[0]["comment_id"] == "991122"
    assert comments[0]["reply_count"] == 4
    assert comments[0]["content"] == "主评"
    assert comments[0]["user_name"] == "读者甲"
    assert comments[0]["digg_count"] == 3


def test_normalize_comments_ignores_expand_reply_show_count():
    comments = _normalize_comments(_list_payload(
        _comment_item(comment_id="7671158225588863806", text="[哭]", reply_count=0, reply_show_count=1),
        _comment_item(comment_id="7688743694229947198", text="小哥小哥", reply_count=1, reply_show_count=1),
    ))
    assert [c["reply_count"] for c in comments] == [0, 1]
    assert "expand" not in comments[0]


def test_normalize_comments_keeps_paragraph_comment_images():
    url = "https://p3-novel.byteimg.com/origin/novel-pic/a.jpg"
    comments = _normalize_comments(_list_payload(_comment_item(
        comment_id="42", text="带图", reply_count=0, image_url=url,
    )))
    assert comments[0]["images"] == [url]
    assert comments[0]["reply_count"] == 0


def test_normalize_replies_handles_capital_common_and_reply_to():
    raw = {
        "code": 0,
        "data": {
            "reply_list": [
                {
                    "reply_id": "1",
                    "Common": {
                        "content": {"text": "楼中楼"},
                        "create_timestamp": 1700000100,
                        "user_info": {"base_info": {"user_name": "读者乙", "user_avatar": ""}},
                    },
                    "stat": {"digg_count": 7},
                    "reply_to_user_info": {"base_info": {"user_name": "读者甲"}},
                }
            ]
        },
    }
    replies = _normalize_replies(raw)
    assert len(replies) == 1
    assert replies[0]["content"] == "楼中楼"
    assert replies[0]["user_name"] == "读者乙"
    assert replies[0]["digg_count"] == 7
    assert replies[0]["reply_to_user_name"] == "读者甲"


def test_paragraph_comments_list_does_not_prefetch_replies(client):
    preview = {
        "common": {
            "content": {"text": "不应出现在列表"},
            "user_info": {"base_info": {"user_name": "路人"}},
        }
    }
    legacy = {
        "user_name": "旧数据",
        "content": "没有 comment_id 的内嵌回复",
        "reply_list": [{"user_name": "路人", "content": "仍应内嵌显示"}],
    }
    payload = _list_payload(
        _comment_item(comment_id="555666777", text="渝川好评", reply_count=2, reply_list=[preview]),
        _comment_item(comment_id="7671158225588863806", text="无回复", reply_count=0, reply_list=[preview]),
        legacy,
    )
    calls = []

    async def fake_post(url, json=None, timeout=None):
        calls.append(url)
        return _JsonResp(payload)

    async def prefetch(*_args, **_kwargs):
        raise AssertionError("paragraph list must not fetch replies")

    with (
        patch("routers.comments._fanqie_client.post", fake_post),
        patch("routers.comments.fqapi_get", prefetch),
        patch("routers.comments._fetch_comment_replies", prefetch),
    ):
        response = client.post("/api/paragraph_comments", json={
            "chapter_id": CHAPTER,
            "book_id": BOOK,
            "paragraph_idx": 58,
            "count": 20,
        })

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 200
    by_id = {c["comment_id"]: c for c in body["data"] if c.get("comment_id")}
    assert by_id["555666777"]["content"] == "渝川好评"
    assert by_id["555666777"]["reply_count"] == 2
    assert "reply_list" not in by_id["555666777"]
    assert by_id["7671158225588863806"]["reply_count"] == 0
    assert "reply_list" not in by_id["7671158225588863806"]
    legacy_row = next(c for c in body["data"] if c["content"] == "没有 comment_id 的内嵌回复")
    assert "comment_id" not in legacy_row
    assert legacy_row["reply_list"][0]["content"] == "仍应内嵌显示"
    assert calls and all(url.endswith("/api/fqcomment/list") for url in calls)
    assert not any("/reply/list" in url for url in calls)


def test_paragraph_comment_replies_via_fqapi(client):
    reply_payload = {
        "code": 0,
        "data": {
            "reply_list": [
                {
                    "reply_id": "88",
                    "common": {
                        "content": {"text": "附议"},
                        "create_timestamp": 1689425000,
                        "user_info": {"base_info": {"user_name": "路人丙", "user_avatar": ""}},
                    },
                    "stat": {"digg_count": 0},
                    "reply_to_user_info": {"base_info": {"user_name": "读者甲"}},
                }
            ]
        },
    }
    seen = {}

    async def fake_fqapi(path, params=None, timeout=6.0):
        seen["path"] = path
        seen["params"] = params
        seen["timeout"] = timeout
        return reply_payload

    async def fake_post(*_args, **_kwargs):
        raise AssertionError("fqapi already returned replies")

    with (
        patch("routers.comments.FQAPI_API", "http://fqapi.test"),
        patch("routers.comments.fqapi_get", fake_fqapi),
        patch("routers.comments._fanqie_client.post", fake_post),
    ):
        response = client.post("/api/paragraph_comment_replies", json={
            "comment_id": "555666777",
            "chapter_id": CHAPTER,
            "book_id": BOOK,
            "count": 10,
        })

    assert response.status_code == 200
    body = response.json()
    assert body["msg"] == "success"
    assert body["data"][0]["content"] == "附议"
    assert body["data"][0]["user_name"] == "路人丙"
    assert body["data"][0]["reply_to_user_name"] == "读者甲"
    assert seen["path"] == "/api/v1/comments/555666777/replies"
    assert seen["params"]["book_id"] == BOOK
    assert seen["params"]["group_id"] == CHAPTER
    assert seen["params"]["count"] == 10


def test_paragraph_comment_replies_fall_back_to_unidbg_when_fqapi_empty(client):
    unidbg_payload = {
        "code": 0,
        "data": {
            "reply_list": [
                {
                    "Common": {
                        "content": {"text": "unidbg 回复"},
                        "user_info": {"base_info": {"user_name": "读者乙"}},
                    },
                    "reply_to_user_info": {"base_info": {"user_name": "读者甲"}},
                }
            ]
        },
    }
    posted = {}

    async def fake_fqapi(path, params=None, timeout=6.0):
        return {"code": 0, "data": {"reply_list": []}}

    async def fake_post(url, json=None, timeout=None):
        posted["url"] = url
        posted["json"] = json
        posted["timeout"] = timeout
        return _JsonResp(unidbg_payload)

    with (
        patch("routers.comments.FQAPI_API", "http://fqapi.test"),
        patch("routers.comments.fqapi_get", fake_fqapi),
        patch("routers.comments._fanqie_client.post", fake_post),
        patch("routers.comments.UNIDBG_API", "http://unidbg.test"),
    ):
        response = client.post("/api/paragraph_comment_replies", json={
            "comment_id": "555666777",
            "chapter_id": CHAPTER,
            "book_id": BOOK,
        })

    assert response.status_code == 200
    body = response.json()
    assert body["data"][0]["content"] == "unidbg 回复"
    assert body["data"][0]["reply_to_user_name"] == "读者甲"
    assert posted["url"] == "http://unidbg.test/api/fqcomment/reply/list"
    assert posted["json"]["commentId"] == "555666777"
    assert posted["json"]["bookId"] == BOOK
    assert posted["json"]["chapterId"] == CHAPTER
    assert posted["timeout"] == 6.0


def test_paragraph_comment_replies_reject_unsafe_ids(client):
    async def should_not_run(*_args, **_kwargs):
        raise AssertionError("unsafe id must not reach upstream")

    with (
        patch("routers.comments.fqapi_get", should_not_run),
        patch("routers.comments._fanqie_client.post", should_not_run),
    ):
        response = client.post("/api/paragraph_comment_replies", json={
            "comment_id": "../555",
            "chapter_id": CHAPTER,
            "book_id": BOOK,
        })

    assert response.status_code == 400


def test_empty_unidbg_reply_list_is_retried_with_comment_type(client):
    """Stock unidbg reply/list is HTTP 200 with reply_list=[] and total=0.

    The same path returns the thread once comment_type=1 is present.
    """
    empty = {
        "code": 0,
        "message": "success",
        "data": {
            "code": 0,
            "message": "请求成功",
            "data": {"reply_list": [], "comment_list_info": {"total": 0, "has_more": False}},
        },
    }
    filled = {
        "code": 0,
        "message": "请求成功",
        "data": {
            "reply_list": [
                {
                    "Common": {
                        "content": {"text": "这个是土木堡生还者"},
                        "user_info": {"base_info": {"user_name": "读者乙"}},
                    },
                    "reply_to_user_info": {"base_info": {"user_name": "读者甲"}},
                }
            ],
            "comment_list_info": {"total": 1, "has_more": False},
        },
    }
    posted = {}

    class _Resp:
        def __init__(self, payload, status_code=200):
            self._payload = payload
            self.status_code = status_code

        def json(self):
            return self._payload

    async def fake_unidbg_post(url, json=None, timeout=None):
        if url.endswith("/api/fqcomment/reply/list"):
            return _Resp(empty)
        if url.endswith("/api/fq-signature/generateSignatureWithMap"):
            assert "/novel/commentapi/reply/list/7470880687937749785/v1" in json["url"]
            return _Resp({"X-Argus": "sig", "X-Gorgon": "g"})
        raise AssertionError(url)

    async def fake_upstream(url, json=None, headers=None, timeout=None):
        posted["url"] = url
        posted["json"] = json
        posted["comment_source"] = headers.get("comment-source")
        return _Resp(filled)

    async def fake_pool(url, timeout=None):
        assert url.endswith("/api/device/pool/status")
        return _Resp({
            "devices": [{
                "deviceId": "100",
                "installId": "200",
                "aid": "1967",
                "versionCode": "68132",
                "versionName": "6.8.1.32",
                "deviceType": "phone",
                "deviceBrand": "Xiaomi",
                "osApi": 33,
                "osVersion": "13",
                "resolution": "3200*1440",
                "dpi": "640",
                "updateVersionCode": "68132",
                "hostAbi": "armeabi-v7a",
                "romVersion": "V433IR+release-keys",
                "cdid": "cdid",
                "cookie": "store-region=cn-zj",
                "userAgent": "com.dragon.read/68132",
            }]
        })

    called = {"fqapi": False}

    async def fake_fqapi(*_args, **_kwargs):
        called["fqapi"] = True
        return None

    with (
        patch("routers.comments.FQAPI_API", ""),
        patch("routers.comments.UNIDBG_API", "http://unidbg.test"),
        patch("routers.comments.fqapi_get", fake_fqapi),
        patch("routers.comments._fanqie_client.post", fake_unidbg_post),
        patch("routers.comments._fanqie_client.get", fake_pool),
        patch("routers.comments.client.post", fake_upstream),
    ):
        response = client.post("/api/paragraph_comment_replies", json={
            "comment_id": "7470880687937749785",
            "chapter_id": CHAPTER,
            "book_id": BOOK,
        })

    assert response.status_code == 200
    body = response.json()
    assert body["data"][0]["content"] == "这个是土木堡生还者"
    assert body["data"][0]["reply_to_user_name"] == "读者甲"
    assert posted["json"]["comment_type"] == 1
    assert posted["json"]["comment_source"] == 502
    assert posted["json"]["group_id"] == CHAPTER
    assert "3200*1440" in posted["url"]
    assert "V433IR+release-keys" in posted["url"]
    assert called["fqapi"] is False


def test_empty_reply_body_stays_empty_and_retryable(client):
    """Both the stock reply/list and the comment_type retry can come back empty.

    The HTTP response stays 200 with no rows so the reader can tap expand again.
    """
    empty = {
        "code": 0,
        "data": {"data": {"reply_list": [], "comment_list_info": {"total": 0}}},
    }

    class _Resp:
        def __init__(self, payload, status_code=200):
            self._payload = payload
            self.status_code = status_code

        def json(self):
            return self._payload

    async def fake_unidbg_post(url, json=None, timeout=None):
        if url.endswith("/generateSignatureWithMap"):
            return _Resp({"X-Argus": "sig"})
        return _Resp(empty)

    async def fake_pool(url, timeout=None):
        return _Resp({"devices": [{"deviceId": "100", "installId": "200", "cookie": "c", "userAgent": "ua"}]})

    async def fake_upstream(url, json=None, headers=None, timeout=None):
        return _Resp(empty)

    with (
        patch("routers.comments.FQAPI_API", ""),
        patch("routers.comments.UNIDBG_API", "http://unidbg.test"),
        patch("routers.comments._fanqie_client.post", fake_unidbg_post),
        patch("routers.comments._fanqie_client.get", fake_pool),
        patch("routers.comments.client.post", fake_upstream),
    ):
        response = client.post("/api/paragraph_comment_replies", json={
            "comment_id": "7470880687937749785",
            "chapter_id": CHAPTER,
            "book_id": BOOK,
        })

    assert response.status_code == 200
    body = response.json()
    assert body["data"] == []
    assert body["msg"] == "no replies"


def test_default_reply_fetch_does_not_call_fqapi(client):
    async def boom(*_args, **_kwargs):
        raise AssertionError("default startup must not call fqapi")

    async def fake_unidbg_post(url, json=None, timeout=None):
        class _Resp:
            def json(self):
                return {
                    "code": 0,
                    "data": {"reply_list": [{"common": {"content": {"text": "来自 unidbg"}, "user_info": {"base_info": {"user_name": "甲"}}}}]},
                }
        return _Resp()

    with (
        patch("routers.comments.FQAPI_API", ""),
        patch("routers.comments.UNIDBG_API", "http://unidbg.test"),
        patch("routers.comments.fqapi_get", boom),
        patch("routers.comments._fanqie_client.post", fake_unidbg_post),
        patch("routers.comments.client.get", boom),
    ):
        response = client.post("/api/paragraph_comment_replies", json={
            "comment_id": "555666777",
            "chapter_id": CHAPTER,
            "book_id": BOOK,
        })

    assert response.status_code == 200
    assert response.json()["data"][0]["content"] == "来自 unidbg"


def test_fqapi_reply_url_stays_on_comment_replies(monkeypatch):
    monkeypatch.setattr("routers.comments.FQAPI_API", "http://127.0.0.1:18921")
    url = _fqapi_reply_url("555666777")
    assert url == "http://127.0.0.1:18921/api/v1/comments/555666777/replies"
    assert _fqapi_reply_url("../555") is None
    assert _fqapi_reply_url("555/admin") is None
    monkeypatch.setattr("routers.comments.FQAPI_API", "")
    assert _fqapi_reply_url("555666777") is None


def test_mock_comments_advertise_replies_without_bodies(client):
    with patch("routers.comments.PARA_COMMENT_MOCK", True):
        listed = client.post("/api/paragraph_comments", json={
            "chapter_id": CHAPTER,
            "book_id": BOOK,
            "paragraph_idx": 0,
        })
        assert listed.status_code == 200
        comments = listed.json()["data"]
        assert comments
        first = comments[0]
        assert first["reply_count"] == 2
        assert first["comment_id"].isdigit()
        assert "reply_list" not in first

        replies = client.post("/api/paragraph_comment_replies", json={
            "comment_id": first["comment_id"],
            "chapter_id": CHAPTER,
            "book_id": BOOK,
        })

    assert replies.status_code == 200
    rows = replies.json()["data"]
    assert len(rows) == 2
    assert rows[0]["content"]
    assert rows[1]["reply_to_user_name"] == "书虫小王"
