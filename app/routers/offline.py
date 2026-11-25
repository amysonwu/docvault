# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 DocVault Contributors
# Author: Amyson Wu
"""离线编辑回传（离线回传链路核心）

闭环：下载（绑定基线版本）→ 本地修改 → 回传上传（自动比对）
→ 冲突三方 diff→ 确认后作为新版本提交（带来源标注）。
"""
import datetime as dt
import pathlib
import secrets

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import audit
from ..config import MAX_UPLOAD_MB, OFFLINE_TASK_EXPIRE_DAYS
from ..db import get_db
from ..deps import require_doc_perm
from ..diffing import diff_files
from ..models import DocumentVersion, OfflineTask
from ..storage import storage
from ..version_service import commit_version, version_download_name

router = APIRouter(prefix="/api/documents/{document_id}/offline", tags=["offline"])


@router.post("/task")
def create_task(doc_user=Depends(require_doc_perm("write")), db: Session = Depends(get_db),
                request: Request = None):
    """创建离线编辑任务，绑定下载人与基线版本，返回任务文件（docx 写入任务标识元数据）"""
    doc, user = doc_user
    base = db.query(DocumentVersion).filter_by(document_id=doc.id)\
        .order_by(DocumentVersion.seq.desc()).first()
    if not base:
        raise HTTPException(404, "文档无版本")
    task = OfflineTask(document_id=doc.id, user_id=user.id, base_version_id=base.id,
                       token=secrets.token_hex(12))
    db.add(task)
    db.commit()
    db.refresh(task)
    audit.log(db, user.id, "offline_download", request, "document", doc.id,
              {"base_seq": base.seq, "token": task.token})

    # 下载基线版本文件；docx 场景把任务标识写入文档元数据（供回传自动识别）
    import shutil
    tmp = pathlib.Path(storage.root.parent / "tmp")
    tmp.mkdir(exist_ok=True)
    out = tmp / f"offline_{task.token}{base.ext}"
    if base.ext == ".docx":
        from docx import Document as Docx
        d = Docx(str(storage.path(base.blob_hash)))
        d.core_properties.comments = f"DocVault-OfflineTask:{task.token}"
        d.save(str(out))
    else:
        shutil.copyfile(storage.path(base.blob_hash), out)
    return FileResponse(out, filename=version_download_name(doc, base),
                        media_type="application/octet-stream")


def _detect_token(path: pathlib.Path) -> str | None:
    """从 docx 元数据自动识别任务标识"""
    try:
        from docx import Document as Docx
        comments = Docx(str(path)).core_properties.comments or ""
        if comments.startswith("DocVault-OfflineTask:"):
            return comments.split(":", 1)[1]
    except Exception:
        pass
    return None


def _save_upload(file: UploadFile, tag: str) -> pathlib.Path:
    tmp = pathlib.Path(storage.root.parent / "tmp")
    tmp.mkdir(exist_ok=True)
    p = tmp / f"{tag}_{dt.datetime.utcnow().timestamp()}_{secrets.token_hex(4)}{pathlib.Path(file.filename).suffix.lower()}"
    size = 0
    with open(p, "wb") as f:
        while chunk := file.file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_MB * 1024 * 1024:
                f.close()
                p.unlink(missing_ok=True)
                raise HTTPException(413, f"超过 {MAX_UPLOAD_MB}MB 上限")
            f.write(chunk)
    return p


def _compare_task(db: Session, doc, user, task: OfflineTask, tmp: pathlib.Path) -> dict:
    """离线比对核心：回传文件 vs 基线 diff + 冲突三方检查"""
    if dt.datetime.utcnow() - task.created_at > dt.timedelta(days=OFFLINE_TASK_EXPIRE_DAYS):
        task.status = "expired"
        db.commit()
        raise HTTPException(409, f"离线任务已超期 {OFFLINE_TASK_EXPIRE_DAYS} 天，请重新下载后再提交")

    base = db.get(DocumentVersion, task.base_version_id)
    latest = db.query(DocumentVersion).filter_by(document_id=doc.id)\
        .order_by(DocumentVersion.seq.desc()).first()

    diff_vs_base = diff_files(storage.path(base.blob_hash), base.ext, tmp,
                              pathlib.Path(tmp).suffix.lower() or base.ext)
    if not diff_vs_base["summary"].get("changed_units"):
        raise HTTPException(409, "文件与基线版本内容一致，无需提交")

    result = {
        "task_token": task.token,
        "base_seq": base.seq,
        "latest_seq": latest.seq,
        "is_conflict": False,
        "diff_vs_base": diff_vs_base,
        "diff_vs_latest": None,
        "note_prefill": f"离线回传自 v{base.seq}",
    }
    if latest.seq != base.seq:
        diff_latest_vs_base = diff_files(storage.path(base.blob_hash), base.ext,
                                         storage.path(latest.blob_hash), latest.ext)
        result["diff_vs_latest"] = diff_latest_vs_base
        if diff_latest_vs_base["summary"].get("changed_units"):
            result["is_conflict"] = True
    result["_tmp_file"] = str(tmp)
    return result


def _find_task(db: Session, doc, user, tmp: pathlib.Path) -> OfflineTask | None:
    """任务识别：文件元数据 → 该用户该文档最近未回传任务（兜底）"""
    token = _detect_token(tmp)
    task = None
    if token:
        task = db.query(OfflineTask).filter_by(token=token, document_id=doc.id).first()
    if not task or task.status != "pending":
        task = db.query(OfflineTask).filter_by(document_id=doc.id, user_id=user.id, status="pending")\
            .order_by(OfflineTask.created_at.desc()).first()
    return task if (task and task.status == "pending") else None


@router.post("/smart")
async def offline_smart(
    document_id: int,
    file: UploadFile = File(...),
    note: str = "",
    doc_user=Depends(require_doc_perm("write")),
    request: Request = None,
    db: Session = Depends(get_db),
):
    """统一提交入口（UI 简化）：自动识别文件是否携带离线任务元数据——
    有 → 走离线比对/冲突流程，返回比对结果等用户确认；无 → 直接提交新版本。"""
    doc, user = doc_user
    tmp = _save_upload(file, "smart_commit")

    task = _find_task(db, doc, user, tmp)
    if not task:
        # 普通提交路径：直接入版本链（自动生成变更摘要）
        try:
            ver = commit_version(db, doc, tmp, doc.ext,
                                 note.strip() or "上传更新", user.id, source="upload", request=request)
        finally:
            tmp.unlink(missing_ok=True)
        return {"mode": "committed", "seq": ver.seq, "note": ver.note,
                "change_summary": ver.change_summary}

    result = _compare_task(db, doc, user, task, tmp)
    result["mode"] = "offline_compare"
    return result


@router.post("/return/confirm")
def offline_confirm(
    document_id: int,
    tmp_file: str,
    task_token: str,
    note: str = "",
    doc_user=Depends(require_doc_perm("write")),
    request: Request = None,
    db: Session = Depends(get_db),
):
    """用户确认差异后正式提交新版本，来源标注『离线回传自 vX.Y』"""
    doc, user = doc_user
    task = db.query(OfflineTask).filter_by(token=task_token, document_id=doc.id).first()
    if not task or task.status != "pending":
        raise HTTPException(404, "离线任务不存在或已回传")
    p = pathlib.Path(tmp_file)
    if not p.exists():
        raise HTTPException(410, "临时文件已过期，请重新上传回传")
    suffix = p.suffix.lower()
    base = db.get(DocumentVersion, task.base_version_id)
    ver = commit_version(
        db, doc, p, suffix, note.strip() or f"离线回传自 v{base.seq}", user.id,
        source="offline_upload", source_ref=f"离线回传自 v{base.seq}", request=request,
    )
    task.status = "returned"
    task.returned_version_id = ver.id
    task.returned_at = dt.datetime.utcnow()
    p.unlink(missing_ok=True)
    db.commit()
    audit.log(db, user.id, "offline_return", request, "document", doc.id,
              {"base_seq": base.seq, "new_seq": ver.seq})
    return {"seq": ver.seq, "note": ver.note, "change_summary": ver.change_summary}
