"""段评序号必须跟官方正文 <p idx> 对齐。

章节标题（h1.chapterTitle1，idx=10000）不是第 0 段。同一个 <p> 里的 <br> 或换行
也不能拆成多段，否则阅读器按数组顺序请求段评时，会显示下一段的评论。
"""

import json
import os
import subprocess
from pathlib import Path

from routers.chapter_content import html_paragraphs

ROOT = Path(__file__).resolve().parents[1]

# 第34章这种章节：标题 h1 在前，正文 <p idx="0"> 起，插图夹在段落之间。
OFFICIAL_XHTML = """
<h1 class="chapterTitle1" idx="10000">第34章 风起</h1>
<p idx="0"><blk>　　甲段。</blk></p>
<p idx="1"><blk>乙段。</blk></p>
<div class="image"><img src="http://p6-novel.byteimg.com/origin/novel-pic-r/abc.jpg"><blk>图注</blk></div>
<p idx="2">丙段<br>丙续</p>
<p idx="3">丁段</p>
"""


def test_chapter_title_h1_is_not_paragraph_zero():
    paras = html_paragraphs(OFFICIAL_XHTML)
    assert paras[0] == "甲段。"
    assert paras[1] == "乙段。"
    assert paras[2].startswith("<img ")
    assert 'src="https://p6-novel.byteimg.com/origin/novel-pic-r/abc.jpg"' in paras[2]
    assert "<figcaption>图注</figcaption>" in paras[2]
    assert paras[3] == "丙段丙续"
    assert paras[4] == "丁段"
    assert len(paras) == 5
    assert all("第34章" not in p for p in paras)


def test_br_or_newline_inside_one_p_does_not_shift_later_indexes():
    html = (
        '<h1 class="chapterTitle1" idx="10000">第34章</h1>'
        "<p idx=\"0\">甲<br/>甲续</p>"
        "<p idx=\"1\">乙\n乙续</p>"
        "<p idx=\"2\">丙</p>"
    )
    assert html_paragraphs(html) == ["甲甲续", "乙\n乙续", "丙"]


def test_title_paragraph_with_sentinel_idx_is_not_body():
    html = (
        '<p class="chapterTitle1" idx="10000">第34章 标题</p>'
        '<p idx="0">甲</p><p idx="1">乙</p>'
    )
    assert html_paragraphs(html) == ["甲", "乙"]


def test_negative_title_idx_is_not_body():
    html = '<p idx="-1">第34章</p><p idx="0">甲</p><p idx="1">乙</p>'
    assert html_paragraphs(html) == ["甲", "乙"]


def test_heading_between_paragraphs_does_not_consume_an_index():
    html = '<p idx="0">甲</p><h2>小节</h2><p idx="1">乙</p>'
    assert html_paragraphs(html) == ["甲", "乙"]


def test_body_paragraph_may_repeat_the_title_text():
    """第一段正文即使和标题同文，也仍是官方 idx 0，不能被当成标题删掉。"""
    html = (
        '<h1 class="chapterTitle1" idx="10000">第34章</h1>'
        '<p idx="0">第34章</p><p idx="1">正文</p>'
    )
    assert html_paragraphs(html) == ["第34章", "正文"]


def test_blk_chapter_skips_title_and_keeps_body_order():
    html = '<h1 class="chapterTitle1" idx="10000">第34章</h1><blk>甲</blk><blk>乙</blk>'
    assert html_paragraphs(html) == ["甲", "乙"]


def test_image_inside_title_is_kept_and_does_not_take_a_text_slot():
    html = (
        '<h1 class="chapterTitle1" idx="10000">第34章'
        '<img src="https://p3-novel.byteimg.com/a.png"></h1>'
        '<p idx="0">甲</p>'
    )
    paras = html_paragraphs(html)
    assert paras[0].startswith("<img ")
    assert "https://p3-novel.byteimg.com/a.png" in paras[0]
    assert paras[1] == "甲"


def test_comic_article_after_title_stays_in_reading_order():
    html = (
        '<h1 class="chapterTitle1" idx="10000">标题</h1>'
        '<article>{"picInfos":[{"md5":"deadbeef"}]}</article>'
        '<p idx="0">说明</p>'
    )
    paras = html_paragraphs(html)
    assert "https://p3-novel.byteimg.com/origin/novel-pic/deadbeef" in paras[0]
    assert paras[1] == "说明"


def test_title_div_without_paragraphs_is_not_body_text():
    html = '<div class="chapter-title">第34章 风起</div><p idx="0">甲</p><p idx="1">乙</p>'
    assert html_paragraphs(html) == ["甲", "乙"]


def test_content_cache_generation_bumped_for_realigned_paragraphs():
    cache_js = (ROOT / "static/js/cache.js").read_text()
    sw = (ROOT / "static/sw.js").read_text()
    assert "CONTENT_IDB_PREFIX = 'c3:'" in cache_js
    assert "c2:" not in cache_js
    assert "CACHE_NAME = 'fanqie-21'" in sw


def test_reader_aligns_comments_with_official_p_idx_and_keeps_title():
    """标题在章节头，正文段评下标等于官方 p idx，插图不占号，作者说仍在。"""
    paras = html_paragraphs(OFFICIAL_XHTML)
    script = r"""
const fs = require('fs');
const vm = require('vm');
const store = {};
function storage() {
  return {
    getItem(k) { return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
    setItem(k, v) { store[k] = String(v); },
    removeItem(k) { delete store[k]; },
  };
}
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
  querySelector() { return null; },
  querySelectorAll() { return []; },
  documentElement: { style: { setProperty() {} } },
};
const window = {
  addEventListener() {},
  scrollTo() {},
  matchMedia() { return { matches: false }; },
};
const context = {
  document, window, console, URL, API: '',
  localStorage: storage(),
  sessionStorage: storage(),
  _profileUser: null,
};
context.global = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync('static/js/app.js', 'utf8'), context);
vm.runInContext(fs.readFileSync('static/js/reader.js', 'utf8'), context);
const paras = JSON.parse(process.env.PARAS);
context.cache = {
  detail: {
    b1: {
      bookName: '示例书',
      chapters: [{ ChapterID: 'c1', Name: '第34章 风起' }],
    },
  },
};
context.getContentCache = () => ({ paragraphs: paras, authorSpeak: '作者有话说' });
const app = { innerHTML: '' };
let renderError = null;
try { context.renderScrollReader(app, 'b1', 0); }
catch (e) { renderError = e; }
const html = app.innerHTML;
if (!html.includes('class="chapter-name"')) {
  throw renderError || new Error('chapter header was not rendered');
}
if (!html.includes('第34章 风起')) throw new Error('chapter title missing from header');
const header = html.slice(html.indexOf('chapter-name'), html.indexOf('reader-content'));
if (!header.includes('第34章 风起')) throw new Error('title is not in the chapter header');
const body = html.slice(html.indexOf('reader-content'));
const texts = [...body.matchAll(/<div class="para-wrap"[^>]*><p>([\s\S]*?)<span class="para-badge"/g)].map(m => m[1]);
if (texts.join('|') !== '甲段。|乙段。|丙段丙续|丁段') {
  throw new Error('body paragraphs: ' + texts.join('|'));
}
const idxs = [...body.matchAll(/data-para-idx="(\d+)"/g)].map(m => m[1]);
if (idxs.join(',') !== '0,0,1,1,2,2,3,3') throw new Error('para index shifted: ' + idxs.join(','));
if (context.paragraphQuote(paras, 0) !== '甲段。') throw new Error('idx 0 quote ' + context.paragraphQuote(paras, 0));
if (context.paragraphQuote(paras, 2) !== '丙段丙续') throw new Error('idx 2 quote ' + context.paragraphQuote(paras, 2));
if (context.paragraphQuote(paras, 3) !== '丁段') throw new Error('idx 3 quote ' + context.paragraphQuote(paras, 3));
const yi = body.indexOf('乙段。');
const img = body.indexOf('class="content-img"');
const bing = body.indexOf('丙段丙续');
if (!(yi >= 0 && img > yi && bing > img)) throw new Error('image left reading order');
if (!body.includes('/api/img_proxy?url=')) throw new Error('image was not proxied');
if (!body.includes('图注')) throw new Error('caption missing');
if (!body.includes('作者有话说')) throw new Error('author speak missing');
if (body.includes('data-para-idx') && /author-speak[\s\S]*data-para-idx/.test(body)) {
  throw new Error('author speak took a paragraph index');
}
console.log('ok');
"""
    proc = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PARAS": json.dumps(paras, ensure_ascii=False)},
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
