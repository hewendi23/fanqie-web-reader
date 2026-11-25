import hashlib
import re
import time
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from shared import (
    FQAPI_API,
    FQAPI_TOKEN,
    PARA_COMMENT_MOCK,
    UNIDBG_API,
    _fanqie_client,
    client,
    community_get,
    logger,
)

# 楼中楼按需拉取（点击展开）。前端 fetchWithTimeout=15s，
# fqapi + unidbg 串行兜底合计须明显低于该值。
_REPLY_FETCH_COUNT = 20
_REPLY_FETCH_TIMEOUT_S = 6.0
_RESOURCE_ID_RE = re.compile(r"^\d{1,32}$")

router = APIRouter()


class ParaCountPayload(BaseModel):
    chapter_id: str = Field(..., min_length=1)
    book_id: str = ""


class ParaCommentsPayload(BaseModel):
    chapter_id: str = Field(..., min_length=1)
    book_id: str = ""
    paragraph_idx: int = 0
    count: int = 20


class ParaCommentRepliesPayload(BaseModel):
    comment_id: str = Field(..., min_length=1)
    chapter_id: str = Field(..., min_length=1)
    book_id: str = Field(..., min_length=1)
    count: int = 20


def _require_resource_id(value: str, name: str) -> str:
    """Fanqie ids are numeric. Reject anything else before it reaches a URL path."""
    v = (value or "").strip()
    if not _RESOURCE_ID_RE.fullmatch(v):
        raise HTTPException(status_code=400, detail=f"invalid {name}")
    return v


def _fqapi_reply_url(comment_id: str) -> str | None:
    """Build the public fqapi replies URL, or None if the base/id is unsafe."""
    if not FQAPI_API:
        return None
    if not _RESOURCE_ID_RE.fullmatch(comment_id):
        return None
    path = f"/api/v1/comments/{comment_id}/replies"
    base = httpx.URL(FQAPI_API)
    url = base.join(path)
    if (url.scheme, url.host, url.port) != (base.scheme, base.host, base.port):
        return None
    if url.path != path:
        return None
    return str(url)


async def fqapi_get(path: str, params: dict | None = None, timeout: float = _REPLY_FETCH_TIMEOUT_S) -> dict | None:
    """GET one already-public fqapi path. This fork only allows comment replies."""
    if not isinstance(path, str) or not path.startswith("/api/v1/comments/") or not path.endswith("/replies"):
        logger.warning("fqapi rejected path: %r", path)
        return None
    comment_id = path[len("/api/v1/comments/"):-len("/replies")]
    url = _fqapi_reply_url(comment_id)
    if not url or url.rstrip("/").rsplit("/", 1)[-1] != "replies":
        logger.warning("fqapi rejected path: %r", path)
        return None
    # _fqapi_reply_url rebuilds the path from the id; ignore any other path shape.
    if httpx.URL(url).path != f"/api/v1/comments/{comment_id}/replies":
        return None
    headers = {"Authorization": f"Bearer {FQAPI_TOKEN}"} if FQAPI_TOKEN else {}
    try:
        r = await client.get(url, params=params, headers=headers, timeout=timeout)
        if r.status_code != 200:
            logger.info("fqapi %s HTTP %s", path, r.status_code)
            return None
        data = r.json()
        if not isinstance(data, dict):
            return None
        if data.get("success") is False:
            logger.info("fqapi %s 错误信封: %s", path, data.get("error"))
            return None
        if "code" in data and data.get("code") not in (0, 200, None):
            logger.info("fqapi %s 业务失败: code=%s", path, data.get("code"))
            return None
        return data
    except Exception as e:
        logger.info("fqapi %s 不可用: %s: %s", path, type(e).__name__, e)
    return None


@router.get("/api/comments")
async def comments(
    book_id: str = Query(...),
    chapter_id: str = Query(default=""),
    offset: int = 0,
    count: int = 20,
):
    try:
        params = {"book_id": book_id, "offset": offset, "count": count}
        if chapter_id:
            params["chapter_id"] = chapter_id
        data = await community_get("/api/comment", params)
        if data is not None:
            inner = data.get("data") or {}
            if isinstance(inner, dict):
                d2 = inner.get("data") or inner
                if isinstance(d2, dict):
                    clist = d2.get("comment")
                    if isinstance(clist, list):
                        d2["comment"] = [_normalize_book_comment(c) for c in clist]
            return data
    except Exception as e:
        logger.warning("comments 上游请求失败 book_id=%s: %s", book_id, e, exc_info=True)
        return {"code": 502, "data": [], "msg": f"upstream error: {type(e).__name__}"}
    return {"code": 200, "data": [], "msg": "no comments"}


def _normalize_book_comment(c: dict) -> dict:
    """Flatten Fanqie book-level comment into user_name/avatar_url/content/reply_list shape."""
    if not isinstance(c, dict):
        return c
    ui = c.get("user_info") or {}
    user_name = ""
    avatar_url = ""
    if isinstance(ui, dict):
        user_name = ui.get("user_name") or ui.get("nick_name") or ""
        avatar_url = ui.get("user_avatar") or ui.get("avatar_url") or ""
    content = c.get("text") or c.get("content") or ""
    ts = c.get("create_timestamp") or c.get("create_time") or 0
    if isinstance(ts, (int, float)) and ts > 1_000_000_000_000:
        ts = ts // 1000
    digg = c.get("digg_count") or 0
    out = dict(c)
    out["user_name"] = user_name or c.get("user_name") or "匿名"
    out["avatar_url"] = _fix_img_url(avatar_url or c.get("avatar_url") or "")
    out["content"] = content
    out["create_time"] = ts
    out["digg_count"] = digg
    replies = c.get("reply_list") or c.get("reply_comment") or c.get("child_comments") or []
    if isinstance(replies, list) and replies:
        out["reply_list"] = [_normalize_book_comment(rc) for rc in replies if isinstance(rc, dict)]
    return out


@router.post("/api/paragraph_comment_counts")
async def paragraph_comment_counts(body: ParaCountPayload):
    """Get paragraph comment counts. Mock mode for local dev, unidbg proxy for production."""
    if PARA_COMMENT_MOCK:
        return {"code": 200, "data": _mock_para_counts(body.chapter_id), "msg": "success (mock)"}

    try:
        req_body = {"chapterId": body.chapter_id, "commentSource": 2, "serverChannel": 17, "groupType": 15}
        if body.book_id:
            req_body["bookId"] = body.book_id
        r = await _fanqie_client.post(
            f"{UNIDBG_API}/api/fqcomment/idea",
            json=req_body,
            timeout=20.0,
        )
        data = r.json()
        counts = _extract_para_counts(data)
        return {"code": 200, "data": counts, "msg": "success"}
    except Exception as e:
        logger.warning("paragraph_comment_counts 失败 chapter_id=%s: %s: %s", body.chapter_id, type(e).__name__, e)
    return {"code": 200, "data": {}, "msg": "no data"}


def _mock_para_counts(chapter_id: str) -> dict:
    """Generate mock paragraph comment counts for local development."""
    seed = int(hashlib.md5(chapter_id.encode()).hexdigest()[:8], 16)
    counts = {}
    positions = [0, 3, 7, 12]
    for i, pos in enumerate(positions):
        cnt = (seed >> (i * 4) & 0xF) % 8 + 1
        counts[str(pos)] = cnt
    return counts


def _extract_para_counts(raw) -> dict:
    """Extract {para_index: count} mapping from various API response formats."""
    def _from_obj(obj):
        out = {}
        for k, v in obj.items():
            try:
                idx = int(k)
            except (ValueError, TypeError):
                continue
            if isinstance(v, dict):
                cnt = v.get("count") or v.get("comment_count") or 0
                if isinstance(cnt, (int, float)) and cnt > 0:
                    out[str(idx)] = cnt
            elif isinstance(v, (int, float)) and v > 0:
                out[str(idx)] = v
        return out

    def _from_array(arr):
        out = {}
        for item in arr:
            if not isinstance(item, dict):
                continue
            idx = item.get("para_index") or item.get("para_idx") or item.get("index") or 0
            cnt = item.get("count") or item.get("comment_count") or 0
            try:
                idx = int(idx)
                cnt = int(cnt)
            except (ValueError, TypeError):
                continue
            if idx >= 0 and cnt > 0:
                out[str(idx)] = cnt
        return out

    candidates_obj = [raw]
    if isinstance(raw, dict):
        candidates_obj.append(raw.get("data"))
        candidates_obj.append(raw.get("paras"))
        d1 = raw.get("data")
        if isinstance(d1, dict):
            candidates_obj.append(d1.get("data"))
            candidates_obj.append(d1.get("paras"))
            d2 = d1.get("data")
            if isinstance(d2, dict):
                candidates_obj.append(d2.get("data"))
                candidates_obj.append(d2)

    for obj in candidates_obj:
        if isinstance(obj, dict) and obj:
            result = _from_obj(obj)
            if result:
                return result

    candidates_arr = []
    if isinstance(raw, dict):
        for key in ["data_list", "list", "idea_list", "ideas"]:
            candidates_arr.append(raw.get(key))
        if isinstance(raw.get("data"), dict):
            for key in ["data_list", "list"]:
                candidates_arr.append(raw["data"].get(key))
        if isinstance(raw.get("detail"), dict):
            for key in ["data_list", "list"]:
                candidates_arr.append(raw["detail"].get(key))

    for arr in candidates_arr:
        if isinstance(arr, list) and arr:
            result = _from_array(arr)
            if result:
                return result

    return {}


@router.post("/api/paragraph_comments")
async def paragraph_comments(body: ParaCommentsPayload):
    """Get paragraph comments. Mock mode for local dev, unidbg proxy for production.

    列表只返回主评 + reply_count。楼中楼不在这里预拉，点「展开」再走
    /api/paragraph_comment_replies，避免拖慢现有段评加载。
    """
    if PARA_COMMENT_MOCK:
        return {"code": 200, "data": _mock_para_comments(body.chapter_id, body.paragraph_idx), "msg": "success (mock)"}

    try:
        r = await _fanqie_client.post(
            f"{UNIDBG_API}/api/fqcomment/list",
            json={
                "chapterId": body.chapter_id,
                "bookId": body.book_id,
                "paraIndex": body.paragraph_idx,
                "commentSource": 2,
                "commentType": 1,
                "serverChannel": 18,
                "groupType": 15,
                "count": body.count,
            },
            timeout=20.0,
        )
        data = r.json()
        comments = _normalize_comments(data)
        for c in comments:
            # Drop embedded previews when we can reload them on expand.
            # Without a comment_id the reader still shows any reply_list inline.
            if isinstance(c, dict) and c.get("comment_id"):
                c.pop("reply_list", None)
        return {"code": 200, "data": comments, "msg": "success"}
    except Exception as e:
        logger.warning("paragraph_comments 失败 chapter_id=%s idx=%d: %s", body.chapter_id, body.paragraph_idx, e, exc_info=True)
    return {"code": 200, "data": [], "msg": "no paragraph comments"}


@router.post("/api/paragraph_comment_replies")
async def paragraph_comment_replies(body: ParaCommentRepliesPayload):
    """Lazy-load nested replies for one paragraph comment (click to expand)."""
    comment_id = _require_resource_id(body.comment_id, "comment_id")
    chapter_id = _require_resource_id(body.chapter_id, "chapter_id")
    book_id = _require_resource_id(body.book_id, "book_id")
    count = max(1, min(int(body.count or _REPLY_FETCH_COUNT), 50))

    if PARA_COMMENT_MOCK:
        return {
            "code": 200,
            "data": _mock_para_replies(comment_id),
            "msg": "success (mock)",
        }

    replies = await _fetch_comment_replies(
        comment_id=comment_id,
        book_id=book_id,
        chapter_id=chapter_id,
        count=count,
    )
    return {"code": 200, "data": replies, "msg": "success" if replies else "no replies"}


async def _fetch_comment_replies(
    comment_id: str,
    book_id: str,
    chapter_id: str,
    count: int = _REPLY_FETCH_COUNT,
) -> list:
    """Fetch nested replies. Try public fqapi first, then unidbg reply/list.

    Live unidbg often returns HTTP 200 with an empty reply_list even when
    stat.reply_count > 0, so an empty fqapi result still falls through.
    """
    comment_id = _require_resource_id(comment_id, "comment_id")
    book_id = _require_resource_id(book_id, "book_id")
    chapter_id = _require_resource_id(chapter_id, "chapter_id")

    raw = await fqapi_get(
        f"/api/v1/comments/{comment_id}/replies",
        params={
            "book_id": book_id,
            "group_id": chapter_id,
            "count": count,
        },
        timeout=_REPLY_FETCH_TIMEOUT_S,
    )
    if raw is not None:
        replies = _normalize_replies(raw)
        if replies:
            return replies

    if UNIDBG_API:
        try:
            r = await _fanqie_client.post(
                f"{UNIDBG_API}/api/fqcomment/reply/list",
                json={
                    "commentId": comment_id,
                    "bookId": book_id,
                    "chapterId": chapter_id,
                    "count": count,
                },
                timeout=_REPLY_FETCH_TIMEOUT_S,
            )
            replies = _normalize_replies(r.json())
            if replies:
                return replies
        except Exception as e:
            logger.warning(
                "paragraph reply list unidbg 失败 comment_id=%s: %s",
                comment_id, e, exc_info=True,
            )
    return []


def _mock_para_comments(chapter_id: str, paragraph_idx: int) -> list:
    """Generate mock paragraph comments for local development."""
    counts = _mock_para_counts(chapter_id)
    if str(paragraph_idx) not in counts:
        return []

    now = int(time.time())
    users = [
        {"name": "书虫小王", "avatar": "https://i.pravatar.cc/80?img=1"},
        {"name": "夜读人", "avatar": "https://i.pravatar.cc/80?img=2"},
        {"name": "墨染书香", "avatar": "https://i.pravatar.cc/80?img=3"},
        {"name": "浮生若梦", "avatar": "https://i.pravatar.cc/80?img=4"},
        {"name": "阅尽千帆", "avatar": "https://i.pravatar.cc/80?img=5"},
    ]
    texts = [
        "这段写得太好了，画面感十足！",
        "作者文笔真的不错，细腻入微",
        "看到这里忍不住笑出了声",
        "剧情转折好突然，完全没想到",
        "这段描写很真实，感同身受",
        "伏笔埋得好深，前面就有暗示了",
        "这角色塑造得很有层次感",
        "节奏把握得恰到好处，不拖沓",
    ]
    seed = int(hashlib.md5(f"{chapter_id}_{paragraph_idx}".encode()).hexdigest()[:8], 16)
    num_comments = (seed % 5) + 1
    result = []
    for i in range(num_comments):
        user_idx = (seed + i) % len(users)
        text_idx = (seed + i * 3) % len(texts)
        comment = {
            "comment_id": str(10_000_000_000 + (seed % 1_000_000) * 10 + i),
            "user_name": users[user_idx]["name"],
            "avatar_url": users[user_idx]["avatar"],
            "content": texts[text_idx],
            "create_time": now - (i + 1) * 3600 * ((seed % 24) + 1),
            "digg_count": (seed + i) % 50,
            "reply_count": 2 if i == 0 else 0,
        }
        if i == 0 and seed % 4 == 0:
            comment["images"] = ["https://picsum.photos/seed/para{}_{}/300/200".format(paragraph_idx, i)]
        result.append(comment)
    return result


def _mock_para_replies(comment_id: str) -> list:
    """Mock nested replies returned when the expand control is clicked."""
    now = int(time.time())
    seed = int(hashlib.md5(str(comment_id).encode()).hexdigest()[:8], 16)
    users = ["夜读人", "墨染书香", "浮生若梦"]
    texts = ["同感！", "这段确实绝了", "作者伏笔埋得好"]
    out = []
    for i in range(2):
        out.append({
            "user_name": users[(seed + i) % len(users)],
            "avatar_url": f"https://i.pravatar.cc/80?img={(seed + i) % 10 + 1}",
            "content": texts[(seed + i) % len(texts)],
            "create_time": now - (i + 1) * 900,
            "digg_count": (seed + i) % 12,
            "reply_to_user_name": "书虫小王" if i == 1 else "",
        })
    return out


def _fix_img_url(url: str) -> str:
    """Return URL unchanged — HEIC→JPEG conversion is handled by img_proxy.

    Previously this changed .heic to .jpeg in the path, but that breaks the
    CDN signature (x-signature is computed over the full path including extension).
    """
    return url or ""


def _common_block(obj: dict) -> dict:
    """Fanqie replies often use capital ``Common``; comments use ``common``."""
    if not isinstance(obj, dict):
        return {}
    c = obj.get("common") or obj.get("Common") or {}
    return c if isinstance(c, dict) else {}


def _as_int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_reply_item(rc: dict) -> dict | None:
    """Flatten one reply/list item (or an embedded reply preview)."""
    if not isinstance(rc, dict):
        return None
    rc_cmt = rc.get("comment") or rc.get("comment_info") or rc
    if not isinstance(rc_cmt, dict):
        return None
    rc_common = _common_block(rc_cmt) or _common_block(rc)

    rc_user = ""
    rc_avatar = ""

    def _ru(d):
        nonlocal rc_user, rc_avatar
        if not isinstance(d, dict):
            return
        if not rc_user:
            rc_user = d.get("user_name") or d.get("name") or d.get("nick_name") or ""
        if not rc_avatar:
            rc_avatar = d.get("user_avatar") or d.get("avatar_url") or d.get("avatar") or ""

    _ru(rc.get("user"))
    _ru(rc_common.get("user_info"))
    ui = rc_common.get("user_info")
    if isinstance(ui, dict):
        _ru(ui.get("base_info"))
    if not rc_user:
        rc_user = rc.get("user_name") or rc.get("nick_name") or "匿名"

    rc_text = ""
    rc_content = rc_common.get("content") or {}
    if isinstance(rc_content, dict):
        rc_text = rc_content.get("text") or rc_content.get("content") or ""
    if not rc_text:
        rc_text = rc.get("text") or rc.get("content") or ""
    if isinstance(rc_text, dict):
        rc_text = rc_text.get("text") or ""
    if not str(rc_text or "").strip():
        return None

    rc_ts = 0
    for rts in [
        rc_common.get("create_timestamp"),
        rc.get("create_timestamp"),
        rc.get("create_time"),
        rc.get("created_ts"),
    ]:
        if isinstance(rts, (int, float)) and rts > 0:
            rc_ts = rts
            break
    if isinstance(rc_ts, (int, float)) and rc_ts > 1_000_000_000_000:
        rc_ts = rc_ts // 1000

    rc_stat = rc.get("stat") if isinstance(rc.get("stat"), dict) else {}
    cmt_stat = rc_cmt.get("stat") if isinstance(rc_cmt.get("stat"), dict) else {}
    rc_digg = (
        rc_stat.get("digg_count")
        or cmt_stat.get("digg_count")
        or rc_common.get("digg_count")
        or rc.get("digg_count")
        or rc.get("like_count")
        or 0
    )

    out = {
        "user_name": rc_user,
        "avatar_url": _fix_img_url(rc_avatar),
        "content": rc_text,
        "create_time": rc_ts,
        "digg_count": _as_int(rc_digg),
    }
    reply_to = ""
    for src in (
        rc.get("reply_to_user_info"),
        rc_common.get("reply_to_user_info"),
        rc_cmt.get("reply_to_user_info") if isinstance(rc_cmt, dict) else None,
    ):
        if not isinstance(src, dict):
            continue
        base = src.get("base_info") if isinstance(src.get("base_info"), dict) else src
        reply_to = base.get("user_name") or base.get("nick_name") or ""
        if reply_to:
            break
    if reply_to:
        out["reply_to_user_name"] = reply_to
    return out


def _normalize_replies(raw) -> list:
    """Normalize reply/list payloads into the same shape as comment.reply_list."""
    candidates = []

    def _collect(node, depth=0):
        if depth > 6:
            return
        if isinstance(node, list):
            candidates.append(node)
            return
        if not isinstance(node, dict):
            return
        for key in ["reply_list", "replies", "sub_comments", "data_list", "list"]:
            v = node.get(key)
            if isinstance(v, list):
                candidates.append(v)
        for key in ["data", "detail", "response", "result"]:
            v = node.get(key)
            if isinstance(v, (dict, list)):
                _collect(v, depth + 1)

    _collect(raw)
    replies = []
    for cand in candidates:
        if cand and len(cand) > len(replies):
            replies = cand
    out = []
    for rc in replies[:_REPLY_FETCH_COUNT]:
        flat = _normalize_reply_item(rc)
        if flat:
            out.append(flat)
    return out


def _normalize_comments(raw) -> list:
    """Normalize comment data from various API response formats."""
    review_candidates = []

    def _collect_from(node, depth=0):
        if depth > 6:
            return
        if isinstance(node, list):
            review_candidates.append(node)
            return
        if not isinstance(node, dict):
            return
        for key in ["reviews", "list", "data_list", "comment_list", "comments", "comment", "ideas"]:
            v = node.get(key)
            if isinstance(v, list):
                review_candidates.append(v)
            elif isinstance(v, dict):
                _collect_from(v, depth + 1)
        for key in ["data", "detail", "response", "result", "comment_data", "common_comments"]:
            v = node.get(key)
            if isinstance(v, (dict, list)):
                _collect_from(v, depth + 1)

    _collect_from(raw)

    reviews = []
    for cand in review_candidates:
        if cand and len(cand) > len(reviews):
            reviews = cand

    if not reviews:
        return []

    result = []
    for item in reviews:
        if not isinstance(item, dict):
            continue

        comment_obj = item.get("comment") or item.get("comment_info") or item
        if not isinstance(comment_obj, dict):
            continue
        common = _common_block(comment_obj)

        user_name = ""
        avatar_url = ""
        def _u(d):
            nonlocal user_name, avatar_url
            if not isinstance(d, dict):
                return
            if not user_name:
                user_name = d.get("user_name") or d.get("name") or d.get("nick_name") or ""
            if not avatar_url:
                avatar_url = d.get("user_avatar") or d.get("avatar_url") or d.get("avatar") or d.get("head_url") or ""
        _u(item.get("user"))
        _u(item.get("user_info"))
        item_ui = item.get("user_info")
        _u(item_ui.get("base_info") if isinstance(item_ui, dict) else None)
        _u(common.get("user_info"))
        common_ui = common.get("user_info")
        _u(common_ui.get("base_info") if isinstance(common_ui, dict) else None)
        if not user_name:
            user_name = item.get("user_name") or item.get("nick_name") or item.get("name") or "匿名"
        if not avatar_url:
            avatar_url = item.get("avatar_url") or item.get("avatar") or item.get("head_url") or ""

        text = ""
        content = common.get("content") or {}
        if isinstance(content, dict):
            text = content.get("text") or content.get("content") or ""
        if not text and isinstance(comment_obj, dict):
            text = comment_obj.get("text") or comment_obj.get("content") or ""
        if not text:
            text = item.get("text") or item.get("content") or item.get("comment_text") or item.get("reply_text") or ""

        digg_count = (
            item.get("digg_count") or item.get("like_count") or item.get("digg") or 0
        )
        stat_digg = common.get("digg_count") or common.get("like_count")
        if stat_digg:
            digg_count = stat_digg
        if not digg_count:
            for stat_src in (
                comment_obj.get("stat") if isinstance(comment_obj.get("stat"), dict) else None,
                item.get("stat") if isinstance(item.get("stat"), dict) else None,
            ):
                if not isinstance(stat_src, dict):
                    continue
                found = stat_src.get("digg_count") or stat_src.get("like_count")
                if isinstance(found, (int, float)) and found > 0:
                    digg_count = int(found)
                    break

        # Prefer real reply totals. Do NOT use expand.reply_show_count —
        # that is a UI slot hint (often 1 even when reply_count is 0).
        reply_count = 0
        saw_reply_count = False
        for src in (
            (comment_obj.get("stat") or {}).get("reply_count")
            if isinstance(comment_obj.get("stat"), dict)
            else None,
            (item.get("stat") or {}).get("reply_count")
            if isinstance(item.get("stat"), dict)
            else None,
            item.get("reply_count"),
            comment_obj.get("reply_count"),
            common.get("reply_count"),
        ):
            if isinstance(src, (int, float)):
                reply_count = max(0, int(src))
                saw_reply_count = True
                break

        comment_id = (
            comment_obj.get("comment_id")
            or item.get("comment_id")
            or common.get("comment_id")
            or ""
        )
        if comment_id is not None:
            comment_id = str(comment_id).strip()

        ts = 0
        for tsrc in [
            common.get("create_timestamp"),
            comment_obj.get("create_timestamp") if isinstance(comment_obj, dict) else None,
            item.get("created_ts"),
            item.get("create_timestamp"),
            item.get("create_time"),
            item.get("ctime"),
        ]:
            if isinstance(tsrc, (int, float)) and tsrc > 0:
                ts = tsrc
                break
        if isinstance(ts, (int, float)) and ts > 1_000_000_000_000:
            ts = ts // 1000

        comment = {
            "user_name": user_name,
            "avatar_url": _fix_img_url(avatar_url),
            "content": text,
            "create_time": ts,
            "digg_count": digg_count,
            "reply_count": reply_count,
        }
        if comment_id:
            comment["comment_id"] = comment_id

        images = item.get("image_list") or item.get("pic_list") or item.get("images") or []
        if not images and isinstance(common.get("content"), dict):
            img_data = common["content"].get("image_data_list") or {}
            if isinstance(img_data, dict) and img_data:
                images = list(img_data.values())
        if isinstance(images, dict):
            images = list(images.values())
        if isinstance(images, str):
            images = [images]
        if images and isinstance(images, list):
            img_urls = []
            for img in images:
                if isinstance(img, str) and img:
                    img_urls.append(_fix_img_url(img))
                elif isinstance(img, dict):
                    url = img.get("url") or img.get("origin_url") or img.get("thumb_url") or img.get("web_url") or ""
                    if url:
                        img_urls.append(_fix_img_url(url))
            if img_urls:
                comment["images"] = img_urls

        replies = (
            item.get("reply_list")
            or item.get("replies")
            or item.get("sub_comments")
            or comment_obj.get("reply_list")
            or []
        )
        if not replies and isinstance(common.get("reply_list"), (list, dict)):
            replies = common["reply_list"]
        if isinstance(replies, dict):
            replies = list(replies.values())
        if replies and isinstance(replies, list):
            flat_replies = []
            for rc in replies[:5]:
                flat = _normalize_reply_item(rc)
                if flat:
                    flat_replies.append(flat)
            if flat_replies:
                # Kept only so a payload that already embeds bodies can fill
                # reply_count. The list endpoint strips reply_list before
                # responding; the reader loads bodies on expand.
                comment["reply_list"] = flat_replies
                if not saw_reply_count and not comment.get("reply_count"):
                    comment["reply_count"] = len(flat_replies)

        result.append(comment)
    return result


_ALLOWED_IMG_HOSTS = ("fqnovelpic.com", "byteimg.com", "bytecdn.cn", "toutiao.com", "bytedance.com", "ixigua.com")
_IMG_FETCH_HEADERS = {
    "Referer": "https://fanqienovel.com/",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}
_HEIC_BRANDS = (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1", b"heim", b"heis")


def _img_host_allowed(host: str) -> bool:
    """CDN 主机名必须完全等于允许后缀，或是其子域。避免 evilbyteimg.com 这种后缀伪造。"""
    name = (host or "").lower().split(":")[0].rstrip(".")
    return any(name == suffix or name.endswith("." + suffix) for suffix in _ALLOWED_IMG_HOSTS)


def _is_heic_payload(content: bytes, content_type: str, url: str) -> bool:
    ctype = (content_type or "").lower()
    if "heic" in ctype or "heif" in ctype:
        return True
    path = urlparse(url).path.lower()
    if path.endswith((".heic", ".heif")):
        return True
    return len(content) >= 12 and content[4:8] == b"ftyp" and content[8:12] in _HEIC_BRANDS

_heif_registered = False


def _ensure_heif_support():
    """Register pillow-heif plugin for HEIC decoding (lazy, one-time)."""
    global _heif_registered
    if _heif_registered:
        return
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
        _heif_registered = True
        logger.info("pillow-heif registered for HEIC decoding")
    except ImportError:
        logger.warning("pillow-heif not installed, HEIC images will not be converted")


@router.get("/api/img_proxy")
async def img_proxy(url: str = Query(..., min_length=1)):
    """Proxy image requests to bypass IP-bound signature restrictions on Fanqie CDN avatars.
    Converts HEIC responses to JPEG for browser compatibility."""
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.hostname:
        return Response(status_code=400, content="invalid url")
    if parsed.scheme not in ("http", "https"):
        return Response(status_code=400, content="invalid scheme")
    if not _img_host_allowed(parsed.hostname):
        return Response(status_code=403, content="host not allowed")

    try:
        r = await client.get(url, timeout=15.0, headers=_IMG_FETCH_HEADERS)
        final = urlparse(str(r.url))
        if not _img_host_allowed(final.hostname or ""):
            logger.warning("img_proxy redirected off allowlist: %s", final.netloc)
            return Response(status_code=403, content="host not allowed")
        if r.status_code != 200:
            logger.warning("img_proxy upstream %d for %s", r.status_code, parsed.netloc)
            return Response(status_code=r.status_code, content=r.content)
        content_type = r.headers.get("content-type", "image/jpeg")

        # Convert HEIC to JPEG for browser compatibility. Some chapter CDNs
        # label HEIC as octet-stream, so also sniff the ftyp brand and the path.
        if _is_heic_payload(r.content, content_type, str(r.url)):
            _ensure_heif_support()
            if _heif_registered:
                try:
                    import io
                    from PIL import Image
                    img = Image.open(io.BytesIO(r.content))
                    buf = io.BytesIO()
                    img.convert("RGB").save(buf, format="JPEG", quality=85)
                    return Response(
                        content=buf.getvalue(),
                        media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"},
                    )
                except Exception as e:
                    logger.warning("img_proxy HEIC conversion failed: %s: %s", type(e).__name__, e)
                    # Fall through to serve original (browser onerror will handle)

        return Response(content=r.content, media_type=content_type, headers={"Cache-Control": "public, max-age=86400"})
    except Exception as e:
        logger.warning("img_proxy error: %s: %s", type(e).__name__, e)
    return Response(status_code=502, content="proxy error")
