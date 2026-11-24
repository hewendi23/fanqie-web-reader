"""书城：代理 unidbg 已暴露的分类接口，不依赖用户 Cookie，也不回退到不存在的社区分类 API。

unidbg v0.0.6（mtongle/fqnovel-unidbg）契约，来自控制器与 DTO，不是 README 概览：

- GET /api/fqnovel/category/front
  可选 categoryTab / needHot / source。
  data 为 FQCategoryFrontResponse，字段经 @JsonProperty 保持 snake_case：
  hot_category、down_category、category_tab_config、category_tab_data.cell_data。
  cell_data 在 Java 侧是 List<Object>，原样透传，结构随频道变化。
- GET /api/fqnovel/category/landing
  必填 categoryId；offset / limit；可选 subCategoryId、sortBy(hot|new|overall)、
  bookStatus(serial|finish)、wordNumber(0-30w|30w-50w|50w+)。
  书籍在 data.book_info，条目是 FQSearchResponse.BookItem（camelCase）。
  服务端把原始 status 填进 BookItem.status，并不复制 creation_status。
- GET /api/fqnovel/category/cell
  newCategoryTab / reqType / distinctStyle。data.cells 同样是原始 JSON。

书卡映射兼容 camelCase（落地页）和 snake_case（cell 原始书）。
"""

import re
import time
from collections import OrderedDict
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from routers.books import _unidbg_get
from shared import logger

router = APIRouter()

_FRONT_TTL = 600
_LANDING_TTL = 180
_CELL_TTL = 600
_CACHE_MAX = 200
_MAX_OFFSET = 1000
_PAGE_LIMIT = 20
_cache: OrderedDict[str, tuple[float, Any]] = OrderedDict()

_ID_RE = re.compile(r"^[0-9A-Za-z_-]{1,32}$")
_SORTS = {"hot", "new", "overall"}
_STATUSES = {"serial", "finish"}
_WORD_NUMBERS = {"0-30w", "30w-50w", "50w+"}
_STATUS_MAP = {"1": "连载中", "0": "已完结"}

_UNAVAILABLE = "书城暂不可用，请确认已启动 unidbg 签名服务"


def _cache_get(key: str, ttl: float) -> Any | None:
    entry = _cache.get(key)
    if entry and time.time() - entry[0] < ttl:
        _cache.move_to_end(key)
        return entry[1]
    if entry:
        _cache.pop(key, None)
    return None


def _cache_set(key: str, value: Any) -> None:
    _cache[key] = (time.time(), value)
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)


def _bad(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": status, "msg": msg, "data": None})


def _ok(data: Any) -> dict:
    return {"code": 200, "msg": "success", "data": data}


def _first(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _clean_params(params: dict) -> dict:
    return {key: value for key, value in params.items() if value is not None and value != ""}


def map_discover_book(item: dict) -> dict | None:
    """把 unidbg 落地页（camelCase）或 cell 原始书（snake_case）收成现有书卡字段。"""
    if not isinstance(item, dict):
        return None
    nested = item.get("book_data")
    if isinstance(nested, list) and nested and isinstance(nested[0], dict) and not item.get("book_id") and not item.get("bookId"):
        item = nested[0]
    book_id = _first(item.get("bookId"), item.get("book_id"))
    if book_id is None:
        return None
    book_id = str(book_id)
    if not _ID_RE.fullmatch(book_id):
        return None

    tags = item.get("tagsStr") or item.get("tags") or ""
    if isinstance(tags, list):
        tags = ",".join(str(tag) for tag in tags if tag)
    elif not isinstance(tags, str):
        tags = ""

    creation = _first(item.get("creationStatus"), item.get("creation_status"))
    status_raw = str(creation if creation is not None else (item.get("status") or ""))
    status = _STATUS_MAP.get(status_raw, status_raw if status_raw in ("连载中", "已完结") else "")

    word = _first(item.get("wordCount"), item.get("word_number")) or 0
    try:
        word = int(float(word))
    except (TypeError, ValueError):
        word = 0

    chapters = _first(
        item.get("totalChapters"),
        item.get("serial_count"),
        item.get("chapter_number"),
        item.get("content_chapter_number"),
    )
    chapters = "" if chapters is None else chapters
    score = _first(item.get("rating"), item.get("score")) or ""
    read_count = _first(item.get("readCount"), item.get("read_count"), item.get("readCntText"), item.get("read_cnt_text")) or 0

    return {
        "BookID": book_id,
        "Name": str(_first(item.get("bookName"), item.get("book_name"), item.get("bookShortName")) or ""),
        "Author": str(item.get("author") or ""),
        "Desc": str(_first(item.get("description"), item.get("abstract"), item.get("bookAbstractV2"), item.get("book_abstract_v2")) or ""),
        "ThumbUrl": str(_first(item.get("coverUrl"), item.get("thumb_url"), item.get("thumbUrl"), item.get("detailPageThumbUrl"), item.get("expandThumbUrl")) or ""),
        "ChapterCount": chapters,
        "Category": str(_first(item.get("category"), item.get("categoryV2"), item.get("category_v2")) or ""),
        "Score": score,
        "WordCount": word,
        "Status": status,
        "Tags": tags,
        "ReadCount": read_count,
    }


def _category_from(obj: dict) -> dict | None:
    if not isinstance(obj, dict):
        return None
    for key in ("category_data", "categoryData"):
        inner = obj.get(key)
        if isinstance(inner, dict):
            found = _category_from(inner)
            if found:
                return found
    if obj.get("book_id") or obj.get("bookId") or obj.get("book_name") or obj.get("bookName"):
        return None

    raw_id = _first(obj.get("id"), obj.get("category_id"), obj.get("categoryId"))
    name = _first(obj.get("name"), obj.get("category_name"), obj.get("categoryName"), obj.get("title"))
    children: list[dict] = []
    seen: set[str] = set()
    for key in ("top", "sub_category", "subCategory", "children"):
        for child in _as_list(obj.get(key)):
            parsed = _category_from(child)
            if not parsed or not parsed["id"] or parsed["id"] in seen:
                continue
            seen.add(parsed["id"])
            children.append(parsed)

    category_id = "" if raw_id is None else str(raw_id)
    if category_id in ("0", "null", "None") or not _ID_RE.fullmatch(category_id or ""):
        category_id = ""
    if not name or (not category_id and not children):
        return None
    return {
        "id": category_id,
        "name": str(name),
        "cover": str(_first(obj.get("cover"), obj.get("pic_url"), obj.get("thumb_url")) or ""),
        "book_count": str(_first(obj.get("book_num"), obj.get("book_cnt"), obj.get("bookNum")) or ""),
        "description": str(obj.get("description") or ""),
        "children": children,
    }


def _categories_from(items: Any) -> list[dict]:
    found: list[dict] = []
    seen: set[str] = set()
    for item in _as_list(items):
        category = _category_from(item) if isinstance(item, dict) else None
        if not category:
            continue
        key = category["id"] or category["name"]
        if key in seen:
            continue
        seen.add(key)
        found.append(category)
    return found


def _section_from_cell(cell: Any, index: int) -> dict | None:
    if not isinstance(cell, dict):
        return None
    title = str(_first(cell.get("cell_name"), cell.get("cellName"), cell.get("title")) or "")
    books: list[dict] = []
    categories: list[dict] = []

    for key in ("book_data", "bookData", "books", "book_list", "bookList", "book_info"):
        for item in _as_list(cell.get(key)):
            book = map_discover_book(item) if isinstance(item, dict) else None
            if book:
                books.append(book)
    for key in ("atom_data", "atomData", "category_list", "categories"):
        for item in _as_list(cell.get(key)):
            if not isinstance(item, dict):
                continue
            category = _category_from(item)
            if category:
                categories.append(category)
            elif (book := map_discover_book(item)):
                books.append(book)

    nested = _as_list(cell.get("cell_data") or cell.get("cellData") or cell.get("cells"))
    if nested and not books and not categories:
        for sub in nested:
            section = _section_from_cell(sub, 0)
            if not section:
                continue
            books.extend(section["books"])
            categories.extend(section["categories"])
            if not title:
                title = section["title"]

    books = _dedupe_books(books)
    categories = _dedupe_categories(categories)
    if not books and not categories:
        return None
    if not title:
        title = "书单" if books else "分类"
    return {
        "id": str(_first(cell.get("cell_id"), cell.get("cellId"), index)),
        "title": title,
        "show_type": cell.get("show_type") if cell.get("show_type") is not None else cell.get("showType") or "",
        "categories": categories,
        "books": books,
    }


def _dedupe_books(books: list[dict]) -> list[dict]:
    found: list[dict] = []
    seen: set[str] = set()
    for book in books:
        if book["BookID"] in seen:
            continue
        seen.add(book["BookID"])
        found.append(book)
    return found


def _dedupe_categories(categories: list[dict]) -> list[dict]:
    found: list[dict] = []
    seen: set[str] = set()
    for category in categories:
        key = category["id"] or category["name"]
        if key in seen:
            continue
        seen.add(key)
        found.append(category)
    return found


def _tabs_from(config: Any) -> tuple[list[dict], Any]:
    if not isinstance(config, dict):
        return [], None
    tabs: list[dict] = []
    tab_list = config.get("tab_list") or config.get("tabList") or []
    if isinstance(tab_list, list) and tab_list:
        for tab in tab_list:
            if not isinstance(tab, dict):
                continue
            name = tab.get("tab_name") or tab.get("tabName") or ""
            tab_type = tab.get("tab_type", tab.get("tabType"))
            if name == "" or tab_type is None:
                continue
            tabs.append({"tab_type": tab_type, "tab_name": str(name)})
    else:
        names = config.get("tab_name_list") or config.get("tabNameList") or []
        types = config.get("tab_type_list") or config.get("tabTypeList") or []
        if isinstance(names, list):
            for index, name in enumerate(names):
                tab_type = types[index] if isinstance(types, list) and index < len(types) else index
                if name:
                    tabs.append({"tab_type": tab_type, "tab_name": str(name)})
    default_tab = config.get("default_tab", config.get("defaultTab"))
    return tabs, default_tab


def normalize_front(data: dict | None) -> dict:
    data = data or {}
    tab_data = data.get("category_tab_data") or data.get("categoryTabData") or {}
    if not isinstance(tab_data, dict):
        tab_data = {}
    sections = []
    for index, cell in enumerate(_as_list(tab_data.get("cell_data") or tab_data.get("cellData"))):
        section = _section_from_cell(cell, index)
        if section:
            sections.append(section)
    tabs, default_tab = _tabs_from(data.get("category_tab_config") or data.get("categoryTabConfig"))
    sub_tabs = []
    for item in _as_list(tab_data.get("sub_tabs") or tab_data.get("subTabs")):
        if not isinstance(item, dict):
            continue
        sub_id = item.get("sub_tab_id") or item.get("subTabId")
        sub_name = item.get("sub_tab_name") or item.get("subTabName")
        if sub_id and sub_name and _ID_RE.fullmatch(str(sub_id)):
            sub_tabs.append({"id": str(sub_id), "name": str(sub_name)})
    return {
        "hot_categories": _categories_from(data.get("hot_category") or data.get("hotCategory")),
        "categories": _categories_from(data.get("down_category") or data.get("downCategory")),
        "tabs": tabs,
        "default_tab": default_tab,
        "active_tab": tab_data.get("category_tab", tab_data.get("categoryTab")),
        "tab_name": str(tab_data.get("tab_name") or tab_data.get("tabName") or ""),
        "sections": sections,
        "sub_tabs": sub_tabs,
    }


def normalize_cells(data: dict | None) -> dict:
    data = data or {}
    sections = []
    for index, cell in enumerate(_as_list(data.get("cells") or data.get("cell_data") or data.get("cellData"))):
        section = _section_from_cell(cell, index)
        if section:
            sections.append(section)
    has_more = data.get("has_more", data.get("hasMore"))
    return {"sections": sections, "has_more": bool(has_more) if has_more is not None else False}


def normalize_landing(data: dict | None, offset: int, limit: int) -> dict:
    data = data or {}
    raw_books = data.get("book_info") or data.get("books") or []
    books = _dedupe_books([book for item in _as_list(raw_books) if (book := map_discover_book(item))])
    has_more = data.get("has_more", data.get("hasMore"))
    if has_more is None:
        has_more = len(books) >= limit
    next_offset = data.get("offset")
    try:
        next_offset = int(next_offset)
    except (TypeError, ValueError):
        next_offset = offset + len(books)
    if next_offset <= offset:
        next_offset = offset + len(books)
    return {"books": books, "has_more": bool(has_more), "offset": next_offset}


def _parse_tab(value: str, name: str) -> int | JSONResponse:
    if value == "":
        return 0
    if not re.fullmatch(r"-?\d{1,3}", value):
        return _bad(f"{name}无效")
    number = int(value)
    if number < 0 or number > 32:
        return _bad(f"{name}无效")
    return number


@router.get("/api/discover/front")
async def discover_front(
    category_tab: str = Query("", alias="categoryTab"),
    need_hot: str = Query("1", alias="needHot"),
):
    """书城首页：热门分类、频道、下拉分类和当前频道的运营块。"""
    tab = _parse_tab(category_tab, "频道")
    if isinstance(tab, JSONResponse):
        return tab
    hot_flag = need_hot.strip().lower() not in ("0", "false", "no", "off")
    params = _clean_params({
        "categoryTab": tab if category_tab != "" else None,
        "needHot": "true" if hot_flag else "false",
    })
    cache_key = "front:" + "&".join(f"{key}={params[key]}" for key in sorted(params))
    cached = _cache_get(cache_key, _FRONT_TTL)
    if cached is not None:
        return _ok(cached)

    data = await _unidbg_get("/api/fqnovel/category/front", params)
    if data is None:
        logger.info("书城首页不可用：unidbg category/front 无响应")
        return _bad(_UNAVAILABLE, 503)
    payload = normalize_front(data)
    _cache_set(cache_key, payload)
    return _ok(payload)


@router.get("/api/discover/category")
async def discover_category(
    category_id: str = Query("", alias="categoryId"),
    offset: int = Query(0),
    limit: int = Query(_PAGE_LIMIT),
    sub_category_id: str = Query("", alias="subCategoryId"),
    sort_by: str = Query("hot", alias="sortBy"),
    book_status: str = Query("", alias="bookStatus"),
    word_number: str = Query("", alias="wordNumber"),
):
    """分类落地页。只按分类接口筛选排序，不用搜索结果冒充推荐。"""
    category_id = category_id.strip()
    sub_category_id = sub_category_id.strip()
    if not _ID_RE.fullmatch(category_id):
        return _bad("请选择分类")
    if sub_category_id and not _ID_RE.fullmatch(sub_category_id):
        return _bad("子分类无效")
    if sort_by not in _SORTS:
        return _bad("排序方式无效")
    if book_status and book_status not in _STATUSES:
        return _bad("连载状态无效")
    if word_number and word_number not in _WORD_NUMBERS:
        return _bad("字数筛选无效")
    if offset < 0 or offset > _MAX_OFFSET:
        return _bad("页码超出范围")
    if limit < 1 or limit > _PAGE_LIMIT:
        return _bad("每页数量无效")

    params = _clean_params({
        "categoryId": category_id,
        "offset": offset,
        "limit": limit,
        "subCategoryId": sub_category_id,
        "sortBy": sort_by,
        "bookStatus": book_status,
        "wordNumber": word_number,
    })
    cache_key = "category:" + "&".join(f"{key}={params[key]}" for key in sorted(params))
    cached = _cache_get(cache_key, _LANDING_TTL)
    if cached is not None:
        return _ok(cached)

    data = await _unidbg_get("/api/fqnovel/category/landing", params)
    if data is None:
        logger.info("分类页不可用：unidbg category/landing 无响应 categoryId=%s", category_id)
        return _bad(_UNAVAILABLE, 503)
    payload = normalize_landing(data, offset, limit)
    _cache_set(cache_key, payload)
    return _ok(payload)


@router.get("/api/discover/cell")
async def discover_cell(
    new_category_tab: str = Query("0", alias="newCategoryTab"),
    req_type: str = Query("0", alias="reqType"),
    distinct_style: str = Query("0", alias="distinctStyle"),
):
    """某个频道的运营块。cell 形状不固定，只抽出其中的分类和书。"""
    tab = _parse_tab(new_category_tab, "频道")
    req = _parse_tab(req_type, "请求类型")
    style = _parse_tab(distinct_style, "样式")
    for parsed in (tab, req, style):
        if isinstance(parsed, JSONResponse):
            return parsed
    params = {"newCategoryTab": tab, "reqType": req, "distinctStyle": style}
    cache_key = "cell:" + "&".join(f"{key}={params[key]}" for key in sorted(params))
    cached = _cache_get(cache_key, _CELL_TTL)
    if cached is not None:
        return _ok(cached)

    data = await _unidbg_get("/api/fqnovel/category/cell", params)
    if data is None:
        logger.info("书城运营块不可用：unidbg category/cell 无响应 tab=%s", tab)
        return _bad(_UNAVAILABLE, 503)
    payload = normalize_cells(data)
    _cache_set(cache_key, payload)
    return _ok(payload)
