"""章节插图解析与阅读器渲染。

上游正文里的插图不在 <p> 内（<div class="image">、段落之间的 <img>、漫画 article JSON）。
这些用例锁定：插图被保留、正文顺序不变、危险地址被丢掉。
"""

import json
import subprocess
from pathlib import Path

from routers.chapter_content import html_paragraphs
from routers.comments import _img_host_allowed, _is_heic_payload

ROOT = Path(__file__).resolve().parents[1]
APP_XHTML = """
<chapter>
  <blk>第一段正文。</blk>
  <div class="image"><img src="http://p6-novel.byteimg.com/origin/novel-pic-r/abc.jpg"><blk>图注一</blk></div>
  <blk>第二段。</blk>
</chapter>
"""


def test_blk_paragraphs_stay_plain_text():
    html = "<p><blk>　　第一段。</blk></p><p><blk>第二段，带标点。</blk></p>"
    assert html_paragraphs(html) == ["第一段。", "第二段，带标点。"]


def test_image_div_between_blocks_is_kept_in_order():
    paras = html_paragraphs(APP_XHTML)
    assert paras[0] == "第一段正文。"
    assert paras[1].startswith("<img ")
    assert 'src="https://p6-novel.byteimg.com/origin/novel-pic-r/abc.jpg"' in paras[1]
    assert "<figcaption>图注一</figcaption>" in paras[1]
    assert paras[2] == "第二段。"
    assert len(paras) == 3


def test_image_outside_p_is_not_dropped_when_paragraphs_exist():
    html = '<p>甲</p><img src="https://p3-novel.byteimg.com/a.png"><p>乙</p>'
    paras = html_paragraphs(html)
    assert [p for p in paras if not p.startswith("<img")] == ["甲", "乙"]
    assert any(p.startswith("<img") and "https://p3-novel.byteimg.com/a.png" in p for p in paras)


def test_single_quotes_entities_and_data_src():
    html = (
        "<p>前文</p>"
        "<img src='http://p9-novel-sign.byteimg.com/a.image?x=1&amp;y=2'>"
        '<img data-src="https://p3-novel.byteimg.com/origin/novel-pic/z.webp">'
    )
    paras = html_paragraphs(html)
    assert paras[0] == "前文"
    assert "https://p9-novel-sign.byteimg.com/a.image?x=1&amp;y=2" in paras[1]
    assert "https://p3-novel.byteimg.com/origin/novel-pic/z.webp" in paras[2]


def test_relative_novel_pic_and_protocol_relative():
    html = '<img src="novel-pic/abc"><img src="//p3-novel.byteimg.com/origin/novel-pic/d.jpg">'
    paras = html_paragraphs(html)
    assert "https://p3-novel.byteimg.com/origin/novel-pic/abc" in paras[0]
    assert "https://p3-novel.byteimg.com/origin/novel-pic/d.jpg" in paras[1]


def test_dangerous_image_urls_are_dropped():
    html = '<p>正文</p><img src="javascript:alert(1)"><img src="data:text/html,hi">'
    assert html_paragraphs(html) == ["正文"]


def test_text_around_inline_image_is_kept():
    html = '<p>见图<img src="https://p3-novel.byteimg.com/a.png">如下</p>'
    assert html_paragraphs(html) == [
        "见图",
        '<img src="https://p3-novel.byteimg.com/a.png">',
        "如下",
    ]


def test_entities_in_text_are_decoded():
    assert html_paragraphs("<p>第二段 &amp; 符号</p>") == ["第二段 & 符号"]


def test_comic_article_json_becomes_image_paragraphs():
    payload = {
        "materials": {
            "e1": {"type": "img", "data": {"web_uri": "novel-pic/abc"}},
            "e2": {"type": "img", "data": {"web_uri": "https://p6-novel.byteimg.com/origin/novel-pic/def"}},
        },
        "skeleton": {"data": [{"element_name": "e1"}, {"elementInfo": "e2"}]},
    }
    encoded = json.dumps(payload, ensure_ascii=False).replace('"', "&#34;")
    html = f"<article>{encoded}</article><p>说明</p>"
    paras = html_paragraphs(html)
    assert "https://p3-novel.byteimg.com/origin/novel-pic/abc" in paras[0]
    assert "https://p6-novel.byteimg.com/origin/novel-pic/def" in paras[1]
    assert paras[2] == "说明"


def test_pic_infos_md5():
    raw = '{"picInfos":[{"md5":"deadbeef"}]}'
    paras = html_paragraphs(raw)
    assert paras == ['<img src="https://p3-novel.byteimg.com/origin/novel-pic/deadbeef">']


def test_plain_multiline_text_still_splits():
    assert html_paragraphs("第一段\n\n第二段") == ["第一段", "第二段"]


def test_img_host_allowlist_requires_a_real_suffix():
    assert _img_host_allowed("p3-novel.byteimg.com")
    assert _img_host_allowed("byteimg.com")
    assert not _img_host_allowed("evilbyteimg.com")
    assert not _img_host_allowed("byteimg.com.evil.example")


def test_heic_sniff_uses_ftyp_brand():
    payload = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 8
    assert _is_heic_payload(payload, "application/octet-stream", "https://cdn.example/a.bin")
    assert not _is_heic_payload(b"\xff\xd8\xff", "image/jpeg", "https://cdn.example/a.jpg")


def test_reader_renders_illustration_without_shifting_paragraph_index():
    """阅读器要把插图画成 img，且前后正文的段评序号不被插图挤开。"""
    script = r"""
const fs = require('fs');
const vm = require('vm');
const document = {
  createElement() {
    let text = '';
    return {
      set textContent(v) { text = String(v ?? ''); },
      get innerHTML() {
        return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      },
    };
  },
  addEventListener() {},
  getElementById() { return null; },
};
const window = { addEventListener() {}, matchMedia() { return { matches: false }; } };
const context = { document, window, console, URL, API: '' };
context.global = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync('static/js/app.js', 'utf8'), context);
vm.runInContext(fs.readFileSync('static/js/reader.js', 'utf8'), context);
const paras = [
  '第一段正文。',
  '<img src="https://p6-novel.byteimg.com/origin/novel-pic-r/abc.jpg"><figcaption>图注一</figcaption>',
  '第二段。',
];
const html = context.renderParas(paras, '');
const chunks = context.renderParagraphChunks(paras, '');
if (!html.includes('class="content-img"')) throw new Error('missing content image');
if (!html.includes('/api/img_proxy?url=')) throw new Error('image was not proxied');
if (!html.includes('alt="插图"')) throw new Error('missing alt');
if (!html.includes('class="img-caption"')) throw new Error('missing caption');
if (!html.includes('图注一')) throw new Error('caption text missing');
if (html.includes('<figcaption')) throw new Error('raw figcaption leaked');
const idxs = [...html.matchAll(/data-para-idx="(\d+)"/g)].map(m => m[1]);
if (idxs.join(',') !== '0,0,1,1') throw new Error('para index shifted: ' + idxs.join(','));
if (context.paragraphQuote(paras, 1) !== '第二段。') throw new Error('quote mapped to the wrong paragraph');
if (chunks.length !== 3) throw new Error('expected 3 chunks, got ' + chunks.length);
if (chunks[1].includes('para-wrap')) throw new Error('illustration became a comment paragraph');
console.log('ok');
"""
    proc = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
