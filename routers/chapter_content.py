"""把章节 HTML 拆成阅读器段落，并保留插图。

番茄正文不是纯文本。unidbg 解密后的 XHTML 和社区 API 的 raw HTML 常见形态是：

- ``<p><blk>段落</blk></p>`` 或单独的 ``<blk>段落</blk>``
- 插图不在 ``<p>`` 里，而是并列的 ``<div class="image"><img src="..."><blk>图注</blk></div>``
- 出版页还会在段落之间直接放 ``<img>``，src 可能是单引号、``data-src``、带 ``&amp;`` 的签名参数，
  以及 ``http://*.byteimg.com``（需要升到 https 再走图片代理）
- 漫画章节把图片清单放在 ``<article>`` 的 JSON 里（``materials`` / ``picInfos``），而不是 ``<img>``

只按 ``<p>`` 抽文本会把这些插图整段丢掉。这里按文档顺序输出字符串段落：
普通段落是纯文本；插图段落是我们自己生成的 ``<img src="绝对地址">``，图注放在同一段的
``<figcaption>`` 里。阅读器据此渲染图片，并且不把插图算进段评序号。

段评接口只给正文 ``<p>`` 编号，从 0 开始。章节标题是 ``<h1 class="chapterTitle1" idx="10000">``，
不是第 0 段；把它收进段落数组会让后面每一段的段评都错到下一段。同一个 ``<p>`` 里的
``<br>`` 或换行也不拆开，否则标题之后的段落还会再错一位。
"""

from __future__ import annotations

import html as html_lib
import json
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup, NavigableString, Tag

_IMAGE_CLASS_TOKENS = {"image", "illus", "illustration", "picture", "pic"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_BLOCK_TAGS = {"p", "blk", "li"}
_SKIP_TAGS = {"script", "style", "tt_keyword_ad", "head", "title", "meta", "link"}
# 官方章节标题的 idx。正文 <p> 从 0 连续编号，标题固定用这个哨兵值。
_TITLE_IDX_MIN = 10000
_CAPTION_TAGS = {"blk", "figcaption", "span", "p"}
_CDN_SUFFIXES = (
    "byteimg.com",
    "fqnovelpic.com",
    "bytecdn.cn",
    "toutiao.com",
    "bytedance.com",
    "ixigua.com",
)
_ORIGIN_CDN = "https://p3-novel.byteimg.com/origin/"
_ARTICLE_RE = re.compile(r"<article\b[^>]*>(.*?)</article>", re.I | re.S)
_RELATIVE_PREFIXES = ("novel-pic/", "novel-pic-r/", "tos-cn-")


def html_paragraphs(content_html: str) -> list[str]:
    """按阅读顺序返回段落。插图段落以 ``<img `` 开头。"""
    raw = content_html or ""
    stripped = raw.strip()
    if not stripped:
        return []

    if stripped.startswith("{") and stripped.endswith("}"):
        urls = _comic_image_urls(stripped)
        if urls:
            return [_img_markup(url, "") for url in urls]

    expanded = _expand_comic_articles(raw)
    soup = _parse_html(expanded)
    root = soup.body or soup
    parts: list[tuple] = []
    _walk(root, parts)
    return _emit(parts)


def _parse_html(content: str) -> BeautifulSoup:
    # html.parser 能保留 <blk> 这类非标准标签，也不依赖 lxml 对 HTML 的改写。
    return BeautifulSoup(content, "html.parser")


def _expand_comic_articles(content: str) -> str:
    def repl(match: re.Match[str]) -> str:
        urls = _comic_image_urls(match.group(1))
        if not urls:
            return match.group(0)
        return "".join(_img_markup(url, "") for url in urls)

    return _ARTICLE_RE.sub(repl, content)


def _comic_image_urls(raw: str) -> list[str]:
    text = html_lib.unescape(raw or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []

    urls: list[str] = []
    materials = data.get("materials") if isinstance(data.get("materials"), dict) else {}
    skeleton = data.get("skeleton") if isinstance(data.get("skeleton"), dict) else {}
    items = skeleton.get("data") if isinstance(skeleton.get("data"), list) else []
    if materials and items:
        for item in items:
            if not isinstance(item, dict):
                continue
            key = (
                item.get("element_name")
                or item.get("elementName")
                or item.get("element_info")
                or item.get("elementInfo")
                or ""
            )
            url = _material_url(materials.get(str(key)))
            if url:
                urls.append(url)
    elif materials:
        for key in sorted(materials):
            url = _material_url(materials.get(key))
            if url:
                urls.append(url)

    if not urls:
        pics = data.get("picInfos") or data.get("pic_infos") or []
        if isinstance(pics, list):
            for pic in pics:
                url = _pic_url(pic)
                if url:
                    urls.append(url)
    return _dedupe(urls)


def _material_url(mat) -> str | None:
    if not isinstance(mat, dict):
        return None
    kind = str(mat.get("type") or "img").lower()
    if kind not in ("img", "image"):
        return None
    data = mat.get("data") if isinstance(mat.get("data"), dict) else {}
    uri = data.get("web_uri") or data.get("url") or data.get("path") or mat.get("web_uri") or ""
    return _normalize_img_url(str(uri))


def _pic_url(pic) -> str | None:
    if isinstance(pic, str):
        uri = pic if pic.startswith(("http", "novel-pic", "//")) else "novel-pic/" + pic
        return _normalize_img_url(uri)
    if not isinstance(pic, dict):
        return None
    uri = pic.get("url") or pic.get("web_uri") or ""
    if uri:
        return _normalize_img_url(str(uri))
    md5 = pic.get("md5") or ""
    if md5:
        return _normalize_img_url("novel-pic/" + str(md5))
    return None


def _official_idx(tag: Tag) -> int | None:
    raw = tag.get("idx")
    if raw is None or isinstance(raw, list):
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _has_chapter_title_class(tag: Tag) -> bool:
    for token in _class_tokens(tag):
        compact = token.replace("-", "").replace("_", "")
        if "chaptertitle" in compact:
            return True
    return False


def _is_chapter_title(tag: Tag) -> bool:
    """标题不占段评序号。官方标题是 h1.chapterTitle1，idx 通常为 10000。

    只跳过标题块本身。内部还包着正文 ``<p>`` 的容器不能整棵丢掉。
    """
    name = (tag.name or "").lower()
    if name in _HEADING_TAGS:
        return True
    if tag.find("p") is not None:
        return False
    if _has_chapter_title_class(tag):
        return True
    if name in _BLOCK_TAGS:
        idx = _official_idx(tag)
        if idx is not None and (idx < 0 or idx >= _TITLE_IDX_MIN):
            return True
    return False


def _collect_images(node, parts: list[tuple]) -> None:
    """标题块本身不进正文，但里面的插图仍按阅读顺序保留。"""
    children = getattr(node, "children", None)
    if children is None:
        return
    for child in children:
        if not isinstance(child, Tag):
            continue
        name = (child.name or "").lower()
        if name in _SKIP_TAGS:
            continue
        image = _image_block(child)
        if image is not None:
            url, caption = image
            if url:
                parts.append(("img", url, caption))
            continue
        if name in ("img", "source"):
            url = _img_url_from_tag(child)
            if url:
                parts.append(("img", url, ""))
            continue
        _collect_images(child, parts)


def _walk(node, parts: list[tuple]) -> None:
    children = getattr(node, "children", None)
    if children is None:
        return
    for child in children:
        if isinstance(child, NavigableString):
            text = str(child).replace("\xa0", " ")
            if text.strip():
                parts.append(("text", text, False))
            continue
        if not isinstance(child, Tag):
            continue
        name = (child.name or "").lower()
        if name in _SKIP_TAGS:
            continue
        if _is_chapter_title(child):
            _collect_images(child, parts)
            continue
        image = _image_block(child)
        if image is not None:
            url, caption = image
            if url:
                parts.append(("img", url, caption))
            continue
        if name in _BLOCK_TAGS:
            if child.find("img") or child.find("source"):
                _walk(child, parts)
            else:
                text = _text_of(child)
                if text:
                    # 一个官方块只对应一个段评序号，不按 <br> / 换行再切开。
                    parts.append(("text", text, True))
            continue
        if name in ("img", "source"):
            url = _img_url_from_tag(child)
            if url:
                parts.append(("img", url, ""))
            continue
        _walk(child, parts)


def _image_block(tag: Tag) -> tuple[str, str] | None:
    """一个只包含单张图（和可选图注）的容器，返回 (url, caption)。"""
    if tag.name == "img":
        url = _img_url_from_tag(tag)
        return (url, "") if url else None

    strict = _strict_image_block(tag)
    if strict is not None:
        return strict
    return _classed_image_block(tag)


def _strict_image_block(tag: Tag) -> tuple[str, str] | None:
    if tag.name not in ("div", "figure", "picture"):
        return None
    img = None
    captions: list[str] = []
    for child in tag.children:
        if isinstance(child, NavigableString):
            if str(child).strip():
                return None
            continue
        if not isinstance(child, Tag):
            continue
        if child.name == "img":
            if img is not None:
                return None
            img = child
            continue
        if child.name in ("br", "source"):
            continue
        if child.name in _CAPTION_TAGS and child.find("img") is None:
            text = _text_of(child)
            if text:
                captions.append(text)
            continue
        return None
    if img is None:
        return None
    url = _img_url_from_tag(img)
    if not url:
        source = tag.find("source")
        url = _img_url_from_tag(source) if source else None
    if not url:
        return None
    return url, " ".join(captions)


def _classed_image_block(tag: Tag) -> tuple[str, str] | None:
    if tag.name not in ("div", "figure"):
        return None
    if not (_class_tokens(tag) & _IMAGE_CLASS_TOKENS):
        return None
    imgs = tag.find_all("img")
    if len(imgs) != 1:
        return None
    url = _img_url_from_tag(imgs[0])
    if not url:
        return None
    captions: list[str] = []
    for node in tag.find_all(["blk", "figcaption"]):
        if node.find("img"):
            continue
        text = _text_of(node)
        if text:
            captions.append(text)
    for string in tag.find_all(string=True):
        parent = string.parent
        if parent is None:
            continue
        if parent.name in ("blk", "figcaption") or parent.find_parent(["blk", "figcaption"]):
            continue
        if parent.name == "img" or parent.find_parent("img"):
            continue
        if str(string).strip():
            return None
    return url, " ".join(captions)


def _class_tokens(tag: Tag) -> set[str]:
    raw = tag.get("class") or []
    if isinstance(raw, str):
        raw = raw.split()
    return {str(token).lower() for token in raw}


def _text_of(tag: Tag) -> str:
    # 去掉 <br> 本身，保留它两侧的文字。不要换成换行，否则同一段会被拆成两段。
    for br in tag.find_all("br"):
        br.replace_with("")
    return tag.get_text("", strip=False).replace("\xa0", " ").strip()


def _img_url_from_tag(tag: Tag | None) -> str | None:
    if tag is None:
        return None
    for key in ("src", "data-src", "data-original", "data-url"):
        url = _normalize_img_url(tag.get(key) or "")
        if url:
            return url
    srcset = tag.get("srcset") or ""
    if srcset.strip():
        first = srcset.split(",")[0].strip().split()[0]
        return _normalize_img_url(first)
    return None


def _normalize_img_url(url: str) -> str | None:
    """把章节插图地址规范成可请求的 http(s) URL。危险协议和相对垃圾地址返回 None。"""
    if url is None:
        return None
    url = html_lib.unescape(str(url)).strip().strip("\"'")
    if not url:
        return None
    lowered = url.lower()
    if lowered.startswith(("javascript:", "data:", "vbscript:", "blob:", "file:")):
        return None
    if url.startswith("//"):
        url = "https:" + url
    elif lowered.startswith(_RELATIVE_PREFIXES) or (
        not lowered.startswith("http") and "novel-pic" in lowered
    ):
        url = _ORIGIN_CDN + url.lstrip("/")
    elif not lowered.startswith("http"):
        return None

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    host = parsed.hostname.lower()
    if parsed.scheme == "http" and _cdn_host(host):
        url = "https://" + url[len("http://") :]
    return url


def _cdn_host(host: str) -> bool:
    return any(host == suffix or host.endswith("." + suffix) for suffix in _CDN_SUFFIXES)


def _img_markup(url: str, caption: str) -> str:
    markup = f'<img src="{html_lib.escape(url, quote=True)}">'
    caption = " ".join(caption.split())
    if caption:
        markup += f"<figcaption>{html_lib.escape(caption)}</figcaption>"
    return markup


def _emit(parts: list[tuple]) -> list[str]:
    out: list[str] = []
    for part in parts:
        if part[0] == "img":
            out.append(_img_markup(part[1], part[2]))
            continue
        text = str(part[1]).replace("\xa0", " ").strip()
        if not text:
            continue
        # 来自同一个 <p>/<blk> 的文本保持一段。没有标签的纯文本仍按空行分段。
        atomic = bool(part[2]) if len(part) > 2 else False
        if atomic:
            out.append(text)
            continue
        for line in re.split(r"\n+", text):
            line = line.strip()
            if line:
                out.append(line)
    return out


def _dedupe(urls: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out
