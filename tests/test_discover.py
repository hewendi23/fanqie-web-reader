import pytest
from fastapi.testclient import TestClient

from routers.discover import _cache
from server import app


@pytest.fixture
def client():
    test_client = TestClient(app)
    yield test_client
    test_client.close()


@pytest.fixture(autouse=True)
def _clear_cache():
    _cache.clear()
    yield
    _cache.clear()


def _front_payload():
    return {
        "hot_category": [
            {
                "id": "7",
                "name": "玄幻",
                "book_num": "1200",
                "cover": "https://example.com/a.jpg",
                "top": [{"id": "70", "name": "东方玄幻"}],
            }
        ],
        "down_category": [
            {"id": "1", "name": "都市", "top": [{"category_id": "11", "name": "都市生活"}]},
            {"name": "没有编号"},
        ],
        "category_tab_config": {
            "default_tab": 1,
            "tab_list": [
                {"tab_type": 1, "tab_name": "男生"},
                {"tab_type": 0, "tab_name": "女生"},
            ],
        },
        "category_tab_data": {
            "category_tab": 1,
            "tab_name": "男生",
            "cell_data": [
                {
                    "cell_id": 100,
                    "cell_name": "热门题材",
                    "atom_data": [
                        {"category_data": {"category_id": "8", "name": "现代言情"}},
                        {"category_data": {"name": "缺编号"}},
                    ],
                },
                {
                    "cell_id": 101,
                    "cell_name": "",
                    "book_data": [
                        {
                            "book_id": "123",
                            "book_name": "测试书",
                            "author": "作者",
                            "abstract": "简介",
                            "thumb_url": "https://p1.example.com/c.jpg",
                            "word_number": "300000",
                            "serial_count": "12",
                            "creation_status": "1",
                            "score": "9.1",
                            "tags": ["热血", "升级"],
                            "category": "玄幻",
                            "read_count": "1000",
                        }
                    ],
                },
            ],
        },
    }


def test_front_maps_categories_tabs_and_cells(client, monkeypatch):
    calls = {"n": 0}

    async def fake_get(path, params=None):
        calls["n"] += 1
        assert path == "/api/fqnovel/category/front"
        assert params["needHot"] == "true"
        assert "categoryTab" not in params
        return _front_payload()

    monkeypatch.setattr("routers.discover._unidbg_get", fake_get)
    response = client.get("/api/discover/front")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["hot_categories"][0]["id"] == "7"
    assert data["hot_categories"][0]["children"][0]["name"] == "东方玄幻"
    assert data["categories"][0]["children"][0]["id"] == "11"
    assert len(data["categories"]) == 1
    assert data["tabs"][0]["tab_name"] == "男生"
    assert data["active_tab"] == 1
    assert data["sections"][0]["categories"][0] == {
        "id": "8",
        "name": "现代言情",
        "cover": "",
        "book_count": "",
        "description": "",
        "children": [],
    }
    book = data["sections"][1]["books"][0]
    assert book["BookID"] == "123"
    assert book["Name"] == "测试书"
    assert book["Status"] == "连载中"
    assert book["WordCount"] == 300000
    assert book["Tags"] == "热血,升级"
    assert data["sections"][1]["title"] == "书单"

    again = client.get("/api/discover/front")
    assert again.status_code == 200
    assert calls["n"] == 1


def test_front_unavailable_without_inventing_categories(client, monkeypatch):
    async def fake_get(path, params=None):
        return None

    monkeypatch.setattr("routers.discover._unidbg_get", fake_get)
    response = client.get("/api/discover/front")
    assert response.status_code == 503
    body = response.json()
    assert body["data"] is None
    assert "unidbg" in body["msg"]


def test_category_maps_landing_books_and_rejects_bad_filters(client, monkeypatch):
    async def fake_get(path, params=None):
        assert path == "/api/fqnovel/category/landing"
        assert params["categoryId"] == "7"
        assert params["sortBy"] == "new"
        assert params["bookStatus"] == "finish"
        assert params["wordNumber"] == "50w+"
        assert params["offset"] == 0
        assert params["limit"] == 20
        return {
            "book_info": [
                {
                    "bookId": "999",
                    "bookName": "落地书",
                    "author": "甲",
                    "description": "desc",
                    "coverUrl": "https://x/y.jpg",
                    "wordCount": 120000,
                    "totalChapters": 30,
                    "category": "玄幻",
                    "tags": ["升级"],
                    "rating": 8.6,
                    "readCount": "20",
                    "status": "0",
                }
            ],
            "has_more": True,
            "offset": 20,
        }

    monkeypatch.setattr("routers.discover._unidbg_get", fake_get)
    response = client.get(
        "/api/discover/category",
        params={"categoryId": "7", "sortBy": "new", "bookStatus": "finish", "wordNumber": "50w+"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["has_more"] is True
    assert data["offset"] == 20
    assert data["books"][0]["Name"] == "落地书"
    assert data["books"][0]["Status"] == "已完结"
    assert data["books"][0]["Score"] == 8.6

    rejected = client.get("/api/discover/category", params={"categoryId": "7", "sortBy": "recommend"})
    assert rejected.status_code == 400
    missing = client.get("/api/discover/category")
    assert missing.status_code == 400
    crawled = client.get("/api/discover/category", params={"categoryId": "../etc", "offset": 5000})
    assert crawled.status_code == 400


def test_cell_maps_raw_sections(client, monkeypatch):
    async def fake_get(path, params=None):
        assert path == "/api/fqnovel/category/cell"
        assert params == {"newCategoryTab": 1, "reqType": 0, "distinctStyle": 0}
        return {
            "cells": [
                {
                    "cell_name": "本周书单",
                    "book_data": [{"bookId": "55", "bookName": "另一本", "creationStatus": "1"}],
                }
            ],
            "has_more": False,
        }

    monkeypatch.setattr("routers.discover._unidbg_get", fake_get)
    response = client.get("/api/discover/cell", params={"newCategoryTab": 1})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["has_more"] is False
    assert data["sections"][0]["title"] == "本周书单"
    assert data["sections"][0]["books"][0]["BookID"] == "55"
    assert data["sections"][0]["books"][0]["Status"] == "连载中"


def test_index_points_discover_at_bookstore(client):
    html = client.get("/").text
    views = client.get("/static/js/views.js").text
    main = client.get("/static/js/main.js").text
    assert 'data-route="discover"' in html
    assert "navigate('discover')" in html
    assert "去书城逛逛" in views
    assert "navigate('discover')" in views
    assert "search?q=${encodeURIComponent(t.name)}" not in views
    assert "热门分类" in views
    assert "let lastTopTab = 'discover'" in main
    assert "unidbg" in views
