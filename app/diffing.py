# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 DocVault Contributors
# Author: Amyson Wu
"""结构化 diff 引擎

统一输出结构：
{
  "kind": "docx|text|xlsx|pptx|pdf|binary",
  "summary": {"added": int, "removed": int, "changed_units": int, "unit": "段落|行|单元格|页"},
  "segments": [ {"op": "equal|insert|delete|replace", "old": [...], "new": [...], "label": "..."} ]
}
"""
import difflib
from pathlib import Path


def diff_files(old_path: Path, old_ext: str, new_path: Path, new_ext: str) -> dict:
    if old_ext != new_ext:
        return {
            "kind": "binary",
            "summary": {"added": 0, "removed": 0, "changed_units": 1, "unit": "文件", "note": f"格式变化 {old_ext}→{new_ext}"},
            "segments": [{"op": "replace", "old": [f"<{old_ext} 原内容>"], "new": [f"<{new_ext} 新内容>"], "label": "整文件"}],
        }
    try:
        if old_ext == ".docx":
            return _docx_diff(old_path, new_path)
        if old_ext == ".xlsx":
            return _xlsx_diff(old_path, new_path)
        if old_ext == ".pptx":
            return _pptx_diff(old_path, new_path)
        if old_ext == ".pdf":
            return _page_diff(old_path, new_path, extractor=_pdf_units, unit="页", kind="pdf")
    except Exception as e:
        return {"kind": "binary",
                "summary": {"added": 0, "removed": 0, "changed_units": 1, "unit": "文件", "note": f"比对失败兜底整文件替换：{e}"},
                "segments": [{"op": "replace", "old": ["<原文件>"], "new": ["<新文件>"], "label": "整文件"}]}
    return _line_diff(old_path, new_path)


# ---- docx：段落级 diff ----
def _docx_units(path: Path) -> list[str]:
    from docx import Document
    doc = Document(str(path))
    units = [p.text for p in doc.paragraphs]
    for t in doc.tables:
        units.append("<表格>")
        for row in t.rows:
            units.append("  | " + " | ".join(c.text.strip() for c in row.cells))
    return units


def _docx_diff(old: Path, new: Path) -> dict:
    return _sequence_diff(_docx_units(old), _docx_units(new), unit="段落", kind="docx")


def _pptx_units(path: Path) -> list[str]:
    """按「形状内段落」拆单元：一句话的改动只影响一个段落，而非整页"""
    from pptx import Presentation
    prs = Presentation(str(path))
    units, labels = [], []
    for i, slide in enumerate(prs.slides, 1):
        got = False
        for shape in slide.shapes:
            if not shape.has_text_frame:
                continue
            for para in shape.text_frame.paragraphs:
                t = "".join(r.text for r in para.runs).strip()
                if t:
                    units.append(t)
                    labels.append(f"第 {i} 页")
                    got = True
        if not got:
            units.append("<空页>")
            labels.append(f"第 {i} 页")
    return units, labels


def _pptx_diff(old: Path, new: Path) -> dict:
    old_u, old_l = _pptx_units(old)
    new_u, new_l = _pptx_units(new)
    return _sequence_diff(old_u, new_u, unit="段落", kind="pptx", labels=old_l)


def _pdf_units(path: Path) -> list[str]:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    return [(p.extract_text() or "").strip() or "<空页>" for p in reader.pages]


def _page_diff(old: Path, new: Path, extractor, unit: str, kind: str) -> dict:
    return _sequence_diff(extractor(old), extractor(new), unit=unit, kind=kind)


# ---- xlsx：单元格级 diff ----
def _xlsx_cells(path: Path) -> dict:
    from openpyxl import load_workbook
    wb = load_workbook(str(path), read_only=True, data_only=True)
    cells = {}
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                if c.value is not None:
                    cells[f"{ws.title}!{c.coordinate}"] = str(c.value)
    return cells


def _xlsx_diff(old: Path, new: Path) -> dict:
    old_c, new_c = _xlsx_cells(old), _xlsx_cells(new)
    changed, added, removed = [], [], []
    for k in old_c:
        if k not in new_c:
            removed.append({"cell": k, "old": old_c[k]})
        elif old_c[k] != new_c[k]:
            changed.append({"cell": k, "old": old_c[k], "new": new_c[k]})
    for k in new_c:
        if k not in old_c:
            added.append({"cell": k, "new": new_c[k]})
    summary = {"added": len(added), "removed": len(removed), "changed_units": len(changed), "unit": "单元格"}
    segments = [{"op": "replace", "old": [f"{c['cell']}: {c['old']}"], "new": [f"{c['cell']}: {c['new']}"],
                 "label": f"单元格 {c['cell']} 修改"} for c in changed[:500]]
    segments += [{"op": "insert", "old": [], "new": [f"{c['cell']}: {c['new']}"], "label": f"新增 {c['cell']}"} for c in added[:500]]
    segments += [{"op": "delete", "old": [f"{c['cell']}: {c['old']}"], "new": [], "label": f"删除 {c['cell']}"} for c in removed[:500]]
    return {"kind": "xlsx", "summary": summary, "segments": segments}


# ---- 通用序列 diff ----
def _inline_runs(a: str, b: str, limit: int = 4000) -> tuple[list, list] | tuple[None, None]:
    """字符级差异：只标出真正变动的片段，避免整段被判为"全删全加"

    返回 (old_runs, new_runs)，每个 run 是 {"t": "same|del|ins", "s": "文本"}。
    文本过长时放弃细粒度（防止 O(n²) 卡顿），由前端按整行展示。
    """
    if not a or not b or len(a) + len(b) > limit:
        return None, None
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)

    def push(runs: list, kind: str, text: str):
        if not text:
            return
        if runs and runs[-1]["t"] == kind:
            runs[-1]["s"] += text      # 合并相邻同类，减少节点数
        else:
            runs.append({"t": kind, "s": text})

    old_runs: list[dict] = []
    new_runs: list[dict] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            push(old_runs, "same", a[i1:i2])
            push(new_runs, "same", b[j1:j2])
        else:
            push(old_runs, "del", a[i1:i2])
            push(new_runs, "ins", b[j1:j2])
    return old_runs, new_runs


def _sequence_diff(old_units: list[str], new_units: list[str], unit: str, kind: str,
                   labels: list[str] | None = None) -> dict:
    sm = difflib.SequenceMatcher(a=old_units, b=new_units, autojunk=False)
    segments, added_chars, removed_chars, changed_units = [], 0, 0, 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        old_seg, new_seg = old_units[i1:i2], new_units[j1:j2]
        if tag == "equal":
            if old_seg and len(segments) < 2000:
                # 相邻片段也保留上下文，但只保留首尾各1条以控制体积
                segments.append({"op": "equal", "old": old_seg[:2], "new": [], "label": ""})
            continue
        changed_units += 1
        added_chars += sum(len(s) for s in new_seg)
        removed_chars += sum(len(s) for s in old_seg)
        if len(segments) >= 2000:
            continue
        seg = {"op": tag, "old": old_seg, "new": new_seg,
               "label": (labels[i1] if labels and i1 < len(labels) else f"{unit} {i1 + 1}")}
        # 成对替换：附带字级标红/标绿，前端据此只高亮真正变化的片段
        if tag == "replace" and old_seg and new_seg:
            pairs = min(len(old_seg), len(new_seg))
            o_runs, n_runs, ok = [], [], True
            for k in range(pairs):
                ra, rb = _inline_runs(old_seg[k], new_seg[k])
                if ra is None:
                    ok = False
                    break
                o_runs.append(ra)
                n_runs.append(rb)
            if ok and o_runs:
                seg["old_runs"] = o_runs     # 与 old/new 前 pairs 行一一对应
                seg["new_runs"] = n_runs
        segments.append(seg)
    return {"kind": kind,
            "summary": {"added": added_chars, "removed": removed_chars,
                        "changed_units": changed_units, "unit": unit},
            "segments": segments}


def _line_diff(old: Path, new: Path) -> dict:
    o = old.read_text("utf-8", errors="replace").splitlines()
    n = new.read_text("utf-8", errors="replace").splitlines()
    return _sequence_diff(o, n, unit="行", kind="text")


def summarize_for_version(diff_result: dict) -> dict:
    """生成版本变更摘要（+N/-M、涉及单元数）"""
    s = diff_result.get("summary", {})
    return {"kind": diff_result.get("kind"),
            "added_chars": s.get("added", 0), "removed_chars": s.get("removed", 0),
            "changed_units": s.get("changed_units", 0), "unit": s.get("unit", "")}
