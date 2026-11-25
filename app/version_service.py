# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 DocVault Contributors
# Author: Amyson Wu
"""版本服务：提交快照、下载命名"""
import datetime as dt
import pathlib
import re

from fastapi import HTTPException
from sqlalchemy.orm import Session

from . import audit
from .storage import storage
from .extract import extract_preview, extract_text
from .diffing import diff_files, summarize_for_version
from .models import Document, DocumentVersion

SAFE_NAME_RE = re.compile(r'[\\/:*?"<>|\r\n\t]')


def sanitize_name(name: str) -> str:
    return SAFE_NAME_RE.sub("_", name).strip() or "未命名"


def commit_version(
    db: Session, doc: Document, src_path, ext: str, note: str,
    user_id: int, source: str, source_ref: str | None = None,
    original_path=None, original_ext: str | None = None,
    request=None, force: bool = False,
) -> DocumentVersion:
    """提交新版本：CAS 落盘 → 幂等检查 → 生成变更摘要 → 更新文档当前态"""
    blob_hash = storage.put(src_path, ext=ext)
    size = storage.size(blob_hash)

    latest = (
        db.query(DocumentVersion)
        .filter_by(document_id=doc.id)
        .order_by(DocumentVersion.seq.desc())
        .first()
    )
    if latest and latest.blob_hash == blob_hash and not force:
        raise HTTPException(409, f"内容与 v{latest.seq} 完全相同，拒绝提交空版本（提交幂等）")

    seq = (latest.seq + 1) if latest else 1

    # 变更摘要：与上一版本做格式内 diff
    change_summary = None
    if latest and latest.blob_hash and source != "restore":
        try:
            old_p = storage.path(latest.blob_hash)
            diff_res = diff_files(old_p, latest.ext, pathlib.Path(src_path), ext)
            change_summary = summarize_for_version(diff_res)
            doc._last_diff = diff_res  # 供回传流程复用，避免重复计算
        except Exception:
            change_summary = None

    # 无实质变更不入链：编辑器保存后的文件字节常有内部差异（时间戳等），
    # 正文与样式都没变时不生成新版本；正文没变但样式变了（加粗/标题/缩进等）要保留并标注
    style_changed = False
    if (latest and change_summary and not force
            and not change_summary.get("added_chars")
            and not change_summary.get("removed_chars")
            and not change_summary.get("changed_units")):
        try:
            old_pv = extract_preview(storage.path(latest.blob_hash), latest.ext)
            new_pv = extract_preview(pathlib.Path(src_path), ext)
            if old_pv and new_pv and old_pv.get("kind") == "html" and new_pv.get("kind") == "html":
                if old_pv["content"] != new_pv["content"]:
                    style_changed = True          # 结构化呈现有差异 → 属于格式/样式变动
                    change_summary["style_changed"] = True
                else:
                    raise HTTPException(409, f"内容与 v{latest.seq} 无实质差异，未生成新版本")
            else:
                raise HTTPException(409, f"内容与 v{latest.seq} 无实质差异，未生成新版本")
        except HTTPException:
            raise
        except Exception:
            pass      # 预览提取失败时放行，避免误拦正常提交

    original_hash = storage.put(original_path, ext=original_ext) if original_path else None
    if original_ext:
        doc.original_ext = original_ext

    ver = DocumentVersion(
        document_id=doc.id, seq=seq, blob_hash=blob_hash, original_blob_hash=original_hash,
        ext=ext, size=size, note=note, source=source, source_ref=source_ref,
        change_summary=change_summary, created_by=user_id,
    )
    db.add(ver)
    doc.ext = ext
    doc.content_text = extract_text(pathlib.Path(src_path), ext) or doc.content_text
    doc.updated_at = dt.datetime.utcnow()
    db.commit()
    db.refresh(ver)
    audit.log(db, user_id, f"version_{source}", request, "document", doc.id,
              {"seq": seq, "note": note, "size": size})
    return ver


def version_download_name(doc: Document, ver: DocumentVersion) -> str:
    """历史版本下载命名含版本号 → 文档名_vN.docx"""
    return f"{sanitize_name(doc.name)}_v{ver.seq}{ver.ext}"
