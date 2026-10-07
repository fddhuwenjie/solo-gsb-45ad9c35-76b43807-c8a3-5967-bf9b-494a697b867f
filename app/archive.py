"""离线归档下载：把当前修订的页面与现行旧址跳转打包成可离线浏览的 ZIP。

归档语义：

- **同一数据库快照**：在单个只读事务内读取当前修订号、该修订的完整页面集合
  与现行跳转表，三者互相一致；下载是只读操作——不推进修订号，不改写数据库
  正文，``GET /pages``、历史修订快照与 ``/site`` 响应都不受影响。
- **可离线浏览**：每个页面 ``/a/b.html`` 存为归档文件 ``a/b.html``，解压后
  直接打开 ``index.html`` 即可循站内链接浏览，无需运行服务。页面内**真正的
  锚点 href**（绝对 ``/x.html``、相对 ``../x.html``、目录形式 ``/docs/``）
  一律改写为归档内真实文件的相对地址，查询串与 ``#片段`` 原文保留；纯片段
  （``#x``）、纯查询（``?x``）、外部链接与非链接 HTML 一律不改
  （沿用 :mod:`app.htmlrewrite` 的字节级改写与 :mod:`app.linkcheck` 的解析规则）。
- **旧址离线入口**：每条现行跳转（旧地址 -> 真实页面）在旧地址的归档位置生成
  一个入口页，打开时通过 ``location.replace(目标 + location.search +
  location.hash)`` 直达对应真实页的归档文件并**保留查询串与片段**；
  另附 ``meta refresh`` 与可点击链接作为无脚本回退。
- **安全且唯一**：任一页面/跳转路径无法安全映射为归档文件（含 ``..``/``.``/
  空段、目录形式、反斜杠等），或不同路径映射到同一归档文件名（含大小写不敏感
  文件系统上的冲突）时，抛出 :class:`ArchiveMappingError`——**不输出部分 ZIP**。
- 尚未首次发布（revision 0）时抛 :class:`ArchiveNotPublished`。
"""

from __future__ import annotations

import io
import json
import posixpath
import sqlite3
import zipfile
from html import escape, unescape

from .htmlrewrite import rewrite_anchor_hrefs
from .linkcheck import is_external, resolve_target
from .publish import _load_current_pages
from .redirects import load_redirects
from .relocate import _split_href

# 归档条目使用固定时间戳：同一快照重复下载得到字节一致的 ZIP。
_ZIP_DATE = (1980, 1, 1, 0, 0, 0)


class ArchiveNotPublished(Exception):
    """尚未首次发布（revision 0）：没有可打包的页面。"""

    def __init__(self) -> None:
        super().__init__("not published yet: archive is available after the first publish")


class ArchiveMappingError(Exception):
    """页面/跳转路径无法安全且唯一地映射为归档文件；不输出部分 ZIP。"""

    def __init__(self, errors: list[dict[str, str]]) -> None:
        self.errors = errors
        super().__init__("archive path mapping failed")


def _archive_name(site_path: str) -> str | None:
    """把站点路径（``/a/b.html``）映射为归档文件名（``a/b.html``）。

    无法安全映射时返回 ``None``：``..``（越出解压目录）、``.`` 与空段
    （``//``，归一化后会与其他路径冲突）、目录形式（以 ``/`` 结尾，不是文件）、
    反斜杠（Windows 上是目录分隔符）。
    """
    if not site_path.startswith("/") or "?" in site_path or "#" in site_path:
        return None
    if "\\" in site_path:
        return None
    segments = site_path.split("/")[1:]
    if not segments or segments[-1] == "":
        return None  # 目录形式，不是文件
    for seg in segments:
        if seg in ("", ".", ".."):
            return None
    return "/".join(segments)


def _mapping_errors(pages: dict[str, str], redirects: dict[str, str]) -> tuple[dict[str, str], list[dict[str, str]]]:
    """计算 路径 -> 归档文件名 映射；无法安全且唯一映射时返回错误列表。"""
    errors: list[dict[str, str]] = []
    names: dict[str, str] = {}
    for site_path in sorted([*pages, *redirects]):
        name = _archive_name(site_path)
        if name is None:
            errors.append({
                "source": "",
                "target": site_path,
                "reason": "path cannot be safely mapped to an archive file",
            })
        else:
            names[site_path] = name

    # 唯一性：不同路径不得映射到同一归档文件（按大小写不敏感口径核对，
    # 覆盖 Windows/macOS 的大小写不敏感文件系统）；任一归档文件名也不得
    # 是另一个归档文件的目录前缀（同名文件与目录无法在磁盘上共存）。
    owner: dict[str, str] = {}  # 归档文件名（casefold）-> 首个站点路径
    for site_path in sorted(names):
        key = names[site_path].casefold()
        if key in owner:
            errors.append({
                "source": "",
                "target": site_path,
                "reason": f"archive file name collides with {owner[key]}",
            })
        else:
            owner[key] = site_path
    for site_path in sorted(names):
        parts = names[site_path].casefold().split("/")
        for i in range(1, len(parts)):
            blocker = owner.get("/".join(parts[:i]))
            if blocker is not None:
                errors.append({
                    "source": "",
                    "target": blocker,
                    "reason": f"archive file name is a directory prefix of {site_path}",
                })
    return names, errors


def _archive_href(source: str, pages: dict[str, str], href: str) -> str | None:
    """把站内 href 改写为归档内真实文件的相对地址；其余返回 ``None``（不改）。

    绝对、相对与目录形式的站内 href 先按既有规则解析到真实页面，再换算成
    源页面归档位置到目标归档文件的相对路径；查询串与 ``#片段`` 原文保留。
    纯片段/纯查询、外部链接、以及解析不到现存页面的 href（已发布快照必经整包
    校验，不会发生；防御性保留原文）都不改。
    """
    h = href.strip()
    if not h or is_external(h):
        return None

    path_part, query, fragment = _split_href(h)
    if not path_part:
        return None  # #片段 / ?查询：仍指向本页

    target, _frag, reason = resolve_target(source, unescape(h))
    if reason is not None or target not in pages:
        return None

    rel = posixpath.relpath(target, posixpath.dirname(source))
    return rel + query + fragment


def _redirect_entry_html(rel_target: str) -> str:
    """旧址离线入口页：打开时直达真实页的归档文件，并保留查询串与片段。

    ``rel_target`` 是旧地址归档位置到真实页归档文件的相对路径。
    """
    url_attr = escape(rel_target, quote=True)
    url_js = json.dumps(rel_target, ensure_ascii=False)
    url_text = escape(rel_target)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        "<title>页面已搬迁</title>\n"
        f'<meta http-equiv="refresh" content="0; url={url_attr}">\n'
        f"<script>location.replace({url_js} + location.search + location.hash);</script>\n"
        "</head>\n"
        "<body>\n"
        f'<p>此地址已搬迁，正前往 <a href="{url_attr}">{url_text}</a> …</p>\n'
        "</body>\n"
        "</html>\n"
    )


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=_ZIP_DATE)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    return info


def build_archive(conn: sqlite3.Connection) -> tuple[int, bytes]:
    """从同一数据库快照构建离线 ZIP，返回 ``(修订号, ZIP 字节)``。

    尚未首次发布抛 :class:`ArchiveNotPublished`；任一路径无法安全且唯一地
    映射为归档文件时抛 :class:`ArchiveMappingError`，不输出部分 ZIP。
    只读操作：不推进修订号，不改写任何存储内容。
    """
    try:
        conn.execute("BEGIN")  # 只读快照：同一事务内读取修订号、页面与跳转
        row = conn.execute("SELECT current_revision FROM meta WHERE id = 1").fetchone()
        current = row["current_revision"]
        if current == 0:
            raise ArchiveNotPublished()
        pages = _load_current_pages(conn, current)
        redirects = load_redirects(conn)
    finally:
        conn.rollback()  # 只读事务：释放快照，数据已读入内存

    # 1) 路径 -> 归档文件名映射（安全 + 唯一）；失败则不产出任何 ZIP。
    names, errors = _mapping_errors(pages, redirects)
    for site_path in sorted(redirects):
        if redirects[site_path] not in names:
            # 跳转守护保证目标必为当前真实页面，不会发生；防御性兜底。
            errors.append({
                "source": site_path,
                "target": redirects[site_path],
                "reason": "redirect target is not a published page",
            })
    if errors:
        raise ArchiveMappingError(errors)

    # 2) 逐页改写站内 href 为归档内相对地址；旧址生成离线入口页。
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for site_path in sorted(pages):
            content = rewrite_anchor_hrefs(
                pages[site_path], lambda h, s=site_path: _archive_href(s, pages, h)
            )
            zf.writestr(_zip_info(names[site_path]), content)
        for site_path in sorted(redirects):
            rel = posixpath.relpath(
                redirects[site_path], posixpath.dirname(site_path)
            )
            zf.writestr(_zip_info(names[site_path]), _redirect_entry_html(rel))
    return current, buf.getvalue()
