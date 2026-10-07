"""离线归档下载（GET /archive）端到端与映射/改写单测。

覆盖：同一快照打包当前修订页面与现行跳转；站内 href（绝对/相对/目录形式）
改写为归档内相对地址且保留查询串与片段；旧址离线入口直达真实页并保留查询串
与片段；只改真正的锚点 href；路径无法安全且唯一映射时返回明确错误、不输出
部分 ZIP；下载不推进修订号、不影响 /pages、历史快照与 /site；未发布时明确
错误。运行：pytest tests/test_archive.py
"""

from __future__ import annotations

import io
import os
import posixpath
import tempfile
import zipfile

import pytest

# 必须在导入 app 之前指定数据库文件
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
os.environ["DOCS_DB_PATH"] = _TMP.name

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.archive import _archive_href, _archive_name, _redirect_entry_html  # noqa: E402
from app.linkcheck import analyze, is_external  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    # 与其他测试模块隔离：直接改用本模块专属的数据库文件
    db.DB_PATH = _TMP.name
    db.init_db()
    with TestClient(app) as c:
        yield c


def publish(client, rev, changes):
    resp = client.post("/publish", json={"expected_revision": rev, "changes": changes})
    assert resp.status_code == 200, resp.text
    return resp.json()["revision"]


def unzip(resp) -> dict[str, str]:
    assert resp.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        assert zf.testzip() is None  # ZIP 完整无损
        return {name: zf.read(name).decode("utf-8") for name in zf.namelist()}


def resolve_entry(base_entry: str, href: str) -> str | None:
    """模拟浏览器在解压目录里点击链接：返回目标归档条目名（站外/纯片段查询另行处理）。"""
    h = href.strip()
    if not h or is_external(h):
        return None
    path_part = h.split("#", 1)[0].split("?", 1)[0]
    if not path_part:
        return base_entry  # 纯片段 / 纯查询：当前文件
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(base_entry), path_part))
    if path_part.endswith("/"):
        resolved = posixpath.join(resolved, "index.html")
    return resolved


# ---------- 路径映射 / href 改写 / 入口页 纯函数 ----------

def test_unit_archive_name_safety():
    assert _archive_name("/index.html") == "index.html"
    assert _archive_name("/docs/guide.html") == "docs/guide.html"
    assert _archive_name("/a b/页面.html") == "a b/页面.html"  # 空格与非 ASCII 可用
    # 无法安全映射：越界、归一化冲突、目录形式、反斜杠
    assert _archive_name("/../evil.html") is None
    assert _archive_name("/a/../b.html") is None
    assert _archive_name("/a/./b.html") is None
    assert _archive_name("/a//b.html") is None
    assert _archive_name("/docs/") is None
    assert _archive_name("/") is None
    assert _archive_name("/a\\b.html") is None


def test_unit_archive_href_rewrite():
    pages = {"/index.html": "", "/docs/guide.html": "", "/docs/index.html": ""}
    # 绝对 / 相对 / 目录形式 -> 归档内相对地址，查询串与片段保留
    assert _archive_href("/index.html", pages, "/docs/guide.html?lang=zh#intro") == \
        "docs/guide.html?lang=zh#intro"
    assert _archive_href("/index.html", pages, "/docs/") == "docs/index.html"
    assert _archive_href("/docs/guide.html", pages, "/index.html#top") == "../index.html#top"
    assert _archive_href("/docs/guide.html", pages, "../index.html") == "../index.html"
    assert _archive_href("/docs/index.html", pages, "guide.html#intro") == "guide.html#intro"
    assert _archive_href("/index.html", pages, "/index.html") == "index.html"  # 绝对自指
    # 纯片段 / 纯查询 / 外部链接 / 查无目标：不改
    assert _archive_href("/index.html", pages, "#top") is None
    assert _archive_href("/index.html", pages, "?v=1") is None
    assert _archive_href("/index.html", pages, "https://example.com/x") is None
    assert _archive_href("/index.html", pages, "//cdn.example.com/l.js") is None
    assert _archive_href("/index.html", pages, "/ghost.html") is None


def test_unit_redirect_entry_html():
    html = _redirect_entry_html("../manual/guide.html")
    assert '<meta http-equiv="refresh" content="0; url=../manual/guide.html">' in html
    # 打开时直达真实页并保留查询串与片段
    assert 'location.replace("../manual/guide.html" + location.search + location.hash)' in html
    assert '<a href="../manual/guide.html">../manual/guide.html</a>' in html
    # 特殊字符按 HTML 属性 / JS 字符串分别转义
    esc = _redirect_entry_html('../a"b.html')
    assert "../a&quot;b.html" in esc and '../a\\"b.html' in esc


# ---------- 建景 ----------

INDEX_HTML = (
    '<html><body>\n'
    '<h1 id="top">首页</h1>\n'
    '<a href="/docs/guide.html?lang=zh#intro">指南（绝对）</a>\n'
    '<a href="about.html">关于（相对）</a>\n'
    '<a href="/docs/">文档目录（目录形式）</a>\n'
    '<a href="#top">纯片段</a>\n'
    '<a href="?v=1">纯查询</a>\n'
    '<a href="https://example.com/x">外链</a>\n'
    '<a href="//cdn.example.com/lib.js">协议相对</a>\n'
    '<a href="mailto:a@b.com">邮件</a>\n'
    '<a title="hint href=\'/docs/bait.html\'" href="/about.html#me">伪href在title里</a>\n'
    '<img src="/docs/guide.html">\n'
    '<!-- <a href="/docs/guide.html">注释里的链接</a> -->\n'
    '<p>正文提到 /docs/guide.html 不改</p>\n'
    '</body></html>'
)

ABOUT_HTML = (
    '<h2 id="me">About</h2>'
    '<a href="/">站点根（目录形式）</a>'
    '<a href="index.html#top">回首页（相对）</a>'
)

GUIDE_HTML = (
    '<h2 id="intro">Guide</h2>'
    '<a href="../index.html#top">home rel</a>'
    '<a href="/about.html">about abs</a>'
)

DOCS_INDEX_HTML = (
    '<a href="../index.html">up</a>'
    '<a href="/docs/guide.html#intro">guide</a>'
)


def test_01_archive_before_first_publish_is_a_clear_error(client):
    resp = client.get("/archive")
    assert resp.status_code == 409
    assert "not published yet" in resp.json()["detail"]
    assert client.get("/revision").json() == {"revision": 0}  # 不推进修订号


def test_02_first_publish(client):
    rev = publish(client, 0, [
        {"op": "upsert", "path": "/index.html", "content": INDEX_HTML},
        {"op": "upsert", "path": "/about.html", "content": ABOUT_HTML},
        {"op": "upsert", "path": "/docs/guide.html", "content": GUIDE_HTML},
        {"op": "upsert", "path": "/docs/index.html", "content": DOCS_INDEX_HTML},
    ])
    assert rev == 1


def test_03_download_zip_entries_and_headers(client):
    resp = client.get("/archive")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/zip"
    disposition = resp.headers["content-disposition"]
    assert "attachment" in disposition and "docs-archive-r1.zip" in disposition
    files = unzip(resp)
    assert set(files) == {
        "index.html", "about.html", "docs/guide.html", "docs/index.html",
    }


def test_04_index_links_rewritten_to_relative_archive_files(client):
    files = unzip(client.get("/archive"))
    index = files["index.html"]
    # 绝对 / 目录形式 -> 归档内真实文件的相对地址，查询串与片段保留
    assert '<a href="docs/guide.html?lang=zh#intro">指南（绝对）</a>' in index
    assert '<a href="docs/index.html">文档目录（目录形式）</a>' in index
    # 相对链接本就是归档内相对地址
    assert '<a href="about.html">关于（相对）</a>' in index
    # 真正的 href 才改：title 里的伪 href= 原样，真 href 改写
    assert "title=\"hint href='/docs/bait.html'\"" in index
    assert '<a title="hint href=\'/docs/bait.html\'" href="about.html#me">' in index
    # 纯片段 / 纯查询 / 外部链接不改
    for kept in (
        '<a href="#top">纯片段</a>',
        '<a href="?v=1">纯查询</a>',
        '<a href="https://example.com/x">外链</a>',
        '<a href="//cdn.example.com/lib.js">协议相对</a>',
        '<a href="mailto:a@b.com">邮件</a>',
    ):
        assert kept in index
    # 非链接 HTML 一律原样
    assert '<img src="/docs/guide.html">' in index
    assert '<!-- <a href="/docs/guide.html">注释里的链接</a> -->' in index
    assert '<p>正文提到 /docs/guide.html 不改</p>' in index


def test_05_subpage_links_rewritten_relative_to_their_location(client):
    files = unzip(client.get("/archive"))
    guide = files["docs/guide.html"]
    assert '<a href="../index.html#top">home rel</a>' in guide   # 相对保持可达
    assert '<a href="../about.html">about abs</a>' in guide      # 绝对 -> 相对
    docs_index = files["docs/index.html"]
    assert '<a href="../index.html">up</a>' in docs_index
    assert '<a href="guide.html#intro">guide</a>' in docs_index  # 同目录绝对 -> 文件名
    about = files["about.html"]
    assert '<a href="index.html">站点根（目录形式）</a>' in about
    assert '<a href="index.html#top">回首页（相对）</a>' in about


def test_06_archive_is_offline_browsable_from_index(client):
    # 解压后打开 index.html，循站内链接（含子页回链）都能落到归档内真实文件
    files = unzip(client.get("/archive"))
    for entry, html in files.items():
        _ids, hrefs = analyze(html)
        for href in hrefs:
            target = resolve_entry(entry, href)
            if target is None:
                continue  # 外部链接不需要归档内文件
            assert target in files, f"{entry} 的链接 {href!r} 指向归档外: {target}"


# ---------- 旧址离线入口 ----------

def test_07_redirects_and_relocate_produce_offline_entries(client):
    # 登记旧地址跳转（不推进修订号）
    resp = client.post("/redirects", json={"path": "/legacy/home.html", "target": "/index.html"})
    assert resp.status_code == 200, resp.text
    # 搬迁真实页：腾出的旧路径自动登记为跳转（revision -> 2）
    resp = client.post("/relocate", json={
        "expected_revision": 1,
        "old_path": "/docs/guide.html",
        "new_path": "/manual/guide.html",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == 2

    files = unzip(client.get("/archive"))
    assert set(files) == {
        "index.html", "about.html", "docs/index.html", "manual/guide.html",
        "legacy/home.html", "docs/guide.html",  # 旧址离线入口
    }
    # 旧址入口：打开时直达真实页归档文件，并保留查询串与片段
    legacy = files["legacy/home.html"]
    assert '<meta http-equiv="refresh" content="0; url=../index.html">' in legacy
    assert 'location.replace("../index.html" + location.search + location.hash)' in legacy
    assert '<a href="../index.html">../index.html</a>' in legacy
    moved = files["docs/guide.html"]
    assert '<meta http-equiv="refresh" content="0; url=../manual/guide.html">' in moved
    assert 'location.replace("../manual/guide.html" + location.search + location.hash)' in moved
    # 真实页随搬迁改指：首页指南链接落到 manual/guide.html（查询串与片段保留）
    assert '<a href="manual/guide.html?lang=zh#intro">指南（绝对）</a>' in files["index.html"]
    # 旧址入口的链接也能在归档内解析
    for entry in ("legacy/home.html", "docs/guide.html"):
        _ids, hrefs = analyze(files[entry])
        for href in hrefs:
            target = resolve_entry(entry, href)
            assert target is None or target in files
    # 同一快照重复下载字节一致（条目时间戳固定）
    assert client.get("/archive").content == client.get("/archive").content


def test_08_download_has_no_side_effects(client):
    before_rev = client.get("/revision").json()
    before_pages = client.get("/pages").json()
    before_site = client.get("/site/index.html").text
    before_rev1 = client.get("/pages", params={"revision": 1}).json()
    before_redirects = client.get("/redirects").json()

    assert client.get("/archive").status_code == 200

    assert client.get("/revision").json() == before_rev            # 不推进修订号
    assert client.get("/pages").json() == before_pages             # /pages 原文不变
    assert client.get("/site/index.html").text == before_site      # /site 响应不变
    assert client.get("/pages", params={"revision": 1}).json() == before_rev1  # 历史快照不变
    assert client.get("/redirects").json() == before_redirects     # 跳转表不变
    # 存储正文仍是发布/搬迁原文（绝对站内 href，不带归档相对化）
    stored_index = before_pages["pages"]["/index.html"]
    assert '<a href="/manual/guide.html?lang=zh#intro">指南（绝对）</a>' in stored_index


# ---------- 无法安全且唯一映射：明确错误，不输出部分 ZIP ----------

def test_09_unmappable_path_fails_without_partial_zip(client):
    # 页面路径本身不经过链接校验：/../evil.html 可以发布成功
    rev = publish(client, 2, [
        {"op": "upsert", "path": "/../evil.html", "content": "<p>evil</p>"},
    ])
    assert rev == 3

    resp = client.get("/archive")
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/json")  # 不是 ZIP
    errors = resp.json()["errors"]
    assert errors[0]["target"] == "/../evil.html"
    assert errors[0]["reason"] == "path cannot be safely mapped to an archive file"
    assert client.get("/revision").json() == {"revision": 3}  # 不推进修订号

    # 删掉问题页面后恢复可下载
    rev = publish(client, 3, [{"op": "delete", "path": "/../evil.html"}])
    assert rev == 4
    assert client.get("/archive").status_code == 200


def test_10_case_insensitive_name_collision_fails(client):
    rev = publish(client, 4, [
        {"op": "upsert", "path": "/UP.html", "content": "<p>UP</p>"},
        {"op": "upsert", "path": "/up.html", "content": "<p>up</p>"},
    ])
    assert rev == 5
    resp = client.get("/archive")
    assert resp.status_code == 422
    reasons = [e["reason"] for e in resp.json()["errors"]]
    assert any("collides" in r for r in reasons)

    rev = publish(client, 5, [
        {"op": "delete", "path": "/UP.html"},
        {"op": "delete", "path": "/up.html"},
    ])
    assert rev == 6
    assert client.get("/archive").status_code == 200


def test_11_file_directory_prefix_conflict_fails(client):
    # /docs（文件）与既有 /docs/index.html（需要 docs/ 目录）无法共存于磁盘
    rev = publish(client, 6, [
        {"op": "upsert", "path": "/docs", "content": "<p>docs as file</p>"},
    ])
    assert rev == 7
    resp = client.get("/archive")
    assert resp.status_code == 422
    errors = resp.json()["errors"]
    assert errors[0]["target"] == "/docs"
    assert "directory prefix" in errors[0]["reason"]

    rev = publish(client, 7, [{"op": "delete", "path": "/docs"}])
    assert rev == 8
    assert client.get("/archive").status_code == 200
