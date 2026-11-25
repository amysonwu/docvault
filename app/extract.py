# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 DocVault Contributors
# Author: Amyson Wu
"""各格式正文提取（docx/pdf 等正文进全文索引）+ 内容预览"""
import html as _html
from pathlib import Path


def _esc(s: str) -> str:
    return _html.escape(str(s), quote=False)


def extract_preview(path: Path, ext: str) -> dict | None:
    """内容预览：docx/xlsx/pptx/pdf → 结构化 HTML；md → 渲染后的 HTML；txt → 纯文本"""
    try:
        if ext == ".md":
            return {"kind": "html", "content": _md_html(path)}
        if ext == ".txt":
            return {"kind": "text", "content": path.read_text("utf-8", errors="replace")[:200_000]}
        if ext == ".docx":
            return {"kind": "html", "content": _docx_html(path)}
        if ext == ".xlsx":
            return {"kind": "html", "content": _xlsx_html(path)}
        if ext == ".pptx":
            return {"kind": "html", "content": _pptx_html(path)}
        if ext == ".pdf":
            return {"kind": "html", "content": _pdf_html(path)}
    except Exception:
        return None
    return None


def md_to_html(text: str) -> str:
    """Markdown 文本 → 渲染后的 HTML（含表格/代码块/引用），并做基础安全清洗"""
    import re as _re

    text = (text or "")[:400_000]
    try:
        import markdown as _md
        html = _md.markdown(text, extensions=["extra", "sane_lists", "nl2br"])
    except Exception:
        return "<p>" + _esc(text).replace("\n", "<br>") + "</p>"
    # 安全清洗：移除脚本类标签、事件属性与 javascript: 协议
    html = _re.sub(r"(?is)<(script|style|iframe|object|embed|link|meta)\b[^>]*>.*?</\1>", "", html)
    html = _re.sub(r"(?is)<(script|style|iframe|object|embed|link|meta)\b[^>]*/?>", "", html)
    html = _re.sub(r"(?i)\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", html)
    html = _re.sub(r"(?i)javascript:", "", html)
    return html or "<p class=\"muted\">(空文档)</p>"


def _md_html(path: Path) -> str:
    return md_to_html(path.read_text("utf-8", errors="replace"))


def _docx_html(path: Path) -> str:
    """docx → 结构化 HTML：按正文原顺序保留标题层级、行内格式、列表与表格"""
    import re as _re

    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(str(path))
    out: list[str] = []
    list_tag: str | None = None

    def close_list():
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    def style_name(p) -> str:
        try:
            return (p.style.name or "").strip()
        except Exception:
            return ""

    def heading_level(p) -> int:
        st = style_name(p).lower()
        m = _re.match(r"^(heading|标题)\s*(\d)", st)
        if m:
            return min(int(m.group(2)), 6)
        if st in ("title", "标题"):
            return 1
        if st in ("subtitle", "副标题"):
            return 2
        try:
            lvl = p.paragraph_format.outline_level
            if lvl is not None:
                return min(int(lvl) + 1, 6)
        except Exception:
            pass
        return 0

    def list_kind(p) -> str | None:
        """依据编号属性 + 样式名判断有序/无序列表"""
        try:
            if p._p.pPr is not None and p._p.pPr.numPr is not None:
                st = style_name(p).lower()
                return "ol" if ("number" in st or "编号" in st) else "ul"
        except Exception:
            pass
        return None

    def runs_html(p) -> str:
        parts = []
        for r in p.runs:
            t = _esc(r.text)
            if not t:
                continue
            if r.bold:
                t = f"<strong>{t}</strong>"
            if r.italic:
                t = f"<em>{t}</em>"
            if r.underline:
                t = f"<u>{t}</u>"
            parts.append(t)
        return "".join(parts)

    def p_attrs(p) -> str:
        """把 Word 的段落缩进/对齐映射成内联样式（首行缩进、两端对齐等）"""
        css: list[str] = []
        try:
            pPr = p._p.pPr
            ind = pPr.find(qn("w:ind")) if pPr is not None else None
            if ind is not None:
                chars = ind.get(qn("w:firstLineChars"))
                first = ind.get(qn("w:firstLine"))
                em = None
                if chars:
                    em = int(chars) / 100            # firstLineChars=200 → 2 字符
                elif first:
                    em = int(first) / 20 / 10.875    # twips → pt → em（正文 14.5px）
                if em and em > 0.1:
                    css.append(f"text-indent:{round(min(em, 4), 2)}em")
            jc = pPr.find(qn("w:jc")) if pPr is not None else None
            if jc is not None:
                val = jc.get(qn("w:val"))
                mapping = {"center": "center", "right": "right",
                           "both": "justify", "distribute": "justify"}
                if val in mapping:
                    css.append(f"text-align:{mapping[val]}")
        except Exception:
            pass
        return f' style="{";".join(css)}"' if css else ""

    def table_html(tbl) -> str:
        rows = []
        for i, row in enumerate(tbl.rows):
            tag = "th" if i == 0 else "td"
            cells = "".join(f"<{tag}>{_esc(c.text.strip())}</{tag}>" for c in row.cells)
            rows.append(f"<tr>{cells}</tr>")
        return '<table class="pv-table">' + "".join(rows) + "</table>"

    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(child, doc)
            txt = p.text.strip()
            lvl = heading_level(p)
            if lvl:
                close_list()
                if txt:
                    out.append(f"<h{lvl}{p_attrs(p)}>{runs_html(p) or _esc(txt)}</h{lvl}>")
                continue
            kind = list_kind(p)
            if kind:
                if list_tag != kind:
                    close_list()
                    out.append(f"<{kind}>")
                    list_tag = kind
                out.append(f"<li>{runs_html(p) or _esc(txt)}</li>")
                continue
            close_list()
            if txt:
                out.append(f"<p{p_attrs(p)}>{runs_html(p) or _esc(txt)}</p>")
        elif tag == "tbl":
            close_list()
            out.append(table_html(Table(child, doc)))
    close_list()
    return "".join(out) or '<p class="muted">(空文档)</p>'


def _xlsx_html(path: Path) -> str:
    from openpyxl import load_workbook
    wb = load_workbook(str(path), read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        out.append(f"<h3>{_esc(ws.title)}</h3>")
        rows = []
        for row in ws.iter_rows(max_row=500, max_col=30, values_only=True):
            cells = ["" if v is None else _esc(v) for v in row]
            if any(c != "" for c in cells):
                rows.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
        out.append("<table class=\"pv-table\">" + "".join(rows) + "</table>")
    return "".join(out)


def _pptx_html(path: Path) -> str:
    from pptx import Presentation
    prs = Presentation(str(path))
    out = []
    for i, slide in enumerate(prs.slides, 1):
        out.append(f"<h3>第 {i} 页</h3>")
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    t = "".join(run.text for run in para.runs).strip()
                    if t:
                        out.append(f"<p>{_esc(t)}</p>")
    return "".join(out)


def _pdf_html(path: Path) -> str:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    out = []
    for i, page in enumerate(reader.pages, 1):
        t = (page.extract_text() or "").strip()
        if t:
            out.append(f"<h3>第 {i} 页</h3>")
            for line in t.splitlines():
                if line.strip():
                    out.append(f"<p>{_esc(line.strip())}</p>")
    return "".join(out)


def extract_text(path: Path, ext: str) -> str | None:
    try:
        if ext == ".docx":
            return _docx(path)
        if ext == ".pptx":
            return _pptx(path)
        if ext == ".xlsx":
            return _xlsx(path)
        if ext == ".pdf":
            return _pdf(path)
        if ext in (".txt", ".md"):
            return path.read_text("utf-8", errors="replace")[:200_000]
        # 老格式（doc/ppt/xls/rtf/odt…）：先转 PDF 再抽正文，保证能进全文索引
        if ext in (".doc", ".ppt", ".xls", ".rtf", ".odt", ".odp", ".ods"):
            from .office_convert import ensure_pdf_for_file, pdf_text
            pdf = ensure_pdf_for_file(path, ext)
            if pdf is not None:
                txt = pdf_text(pdf)
                if txt.strip():
                    return txt
    except Exception:
        return None  # 提取失败不阻断上传
    return None


def _docx(path: Path) -> str:
    from docx import Document
    doc = Document(str(path))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            parts.append(" | ".join(c.text.strip() for c in row.cells))
    return "\n".join(parts)[:500_000]


def _pptx(path: Path) -> str:
    from pptx import Presentation
    prs = Presentation(str(path))
    parts = []
    for i, slide in enumerate(prs.slides, 1):
        texts = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                texts.append(shape.text_frame.text)
        parts.append(f"[第{i}页] " + "\n".join(t for t in texts if t.strip()))
    return "\n".join(parts)[:500_000]


def _xlsx(path: Path) -> str:
    from openpyxl import load_workbook
    wb = load_workbook(str(path), read_only=True, data_only=True)
    parts = []
    for ws in wb.worksheets:
        parts.append(f"[{ws.title}]")
        for row in ws.iter_rows(max_row=2000, max_col=50, values_only=True):
            cells = [str(v) for v in row if v is not None]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)[:500_000]


def _pdf(path: Path) -> str:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    parts = []
    for i, page in enumerate(reader.pages, 1):
        t = page.extract_text() or ""
        if t.strip():
            parts.append(f"[第{i}页] {t}")
    return "\n".join(parts)[:500_000]
