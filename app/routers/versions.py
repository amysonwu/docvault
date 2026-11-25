# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 DocVault Contributors
# Author: Amyson Wu
"""版本路由：提交 / 历史 / 对比 / 回滚 / 标签 / 版本下载"""
import datetime as dt
import pathlib
import re

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import audit
from ..config import MAX_UPLOAD_MB
from ..db import get_db
from ..deps import get_current_user, require_doc_perm
from ..diffing import diff_files
from ..models import Document, DocumentVersion, User, VersionTag
from ..storage import storage
from ..version_service import commit_version, version_download_name

router = APIRouter(prefix="/api/documents/{document_id}/versions", tags=["versions"])


def _version_out(db: Session, v: DocumentVersion) -> dict:
    creator = db.get(User, v.created_by)
    tags = [t.name for t in db.query(VersionTag).filter_by(version_id=v.id).all()]
    return {"id": v.id, "seq": v.seq, "size": v.size, "note": v.note, "source": v.source,
            "source_ref": v.source_ref, "change_summary": v.change_summary, "ext": v.ext,
            "created_by": v.created_by, "creator_name": creator.display_name if creator else "",
            "created_at": v.created_at, "tags": tags}


def _get_version(db: Session, document_id: int, seq: int) -> DocumentVersion:
    v = db.query(DocumentVersion).filter_by(document_id=document_id, seq=seq).first()
    if not v:
        raise HTTPException(404, f"版本 v{seq} 不存在")
    return v


@router.get("")
def history(doc_user=Depends(require_doc_perm("read")), db: Session = Depends(get_db),
            by_user: int | None = None):
    """版本历史时间线：版本号/提交人/时间/说明/变更摘要，支持按人筛选"""
    doc, _ = doc_user
    q = db.query(DocumentVersion).filter_by(document_id=doc.id)
    if by_user:
        q = q.filter_by(created_by=by_user)
    vers = q.order_by(DocumentVersion.seq.desc()).all()
    return [_version_out(db, v) for v in vers]


@router.post("/commit")
async def commit_new_version(
    document_id: int,
    file: UploadFile = File(...),
    note: str = "",
    doc_user=Depends(require_doc_perm("write")),
    request: Request = None,
    db: Session = Depends(get_db),
):
    """保存并提交版本：必填版本说明；需持有编辑锁"""
    doc, user = doc_user
    if not note.strip():
        raise HTTPException(400, "版本说明必填")
    suffix = pathlib.Path(file.filename).suffix.lower() or doc.ext
    tmp = pathlib.Path(storage.root.parent / "tmp")
    tmp.mkdir(exist_ok=True)
    tmp_file = tmp / f"commit_{document_id}_{dt.datetime.utcnow().timestamp()}{suffix}"
    size = 0
    with open(tmp_file, "wb") as f:
        while chunk := file.file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_MB * 1024 * 1024:
                f.close()
                tmp_file.unlink(missing_ok=True)
                raise HTTPException(413, f"超过 {MAX_UPLOAD_MB}MB 上限")
            f.write(chunk)
    ver = commit_version(db, doc, tmp_file, suffix, note.strip(), user.id,
                         source="edit", request=request)
    # 提交成功后释放编辑锁
    doc.lock_owner_id = None
    doc.lock_expires_at = None
    db.commit()
    return _version_out(db, ver)


@router.get("/diff")
def diff_versions(
    document_id: int,
    old: int,
    new: int,
    doc_user=Depends(require_doc_perm("read")),
    db: Session = Depends(get_db),
):
    """版本对比：任意两版本格式内结构化 diff"""
    doc, _ = doc_user
    v_old = _get_version(db, document_id, old)
    v_new = _get_version(db, document_id, new)
    result = diff_files(storage.path(v_old.blob_hash), v_old.ext,
                        storage.path(v_new.blob_hash), v_new.ext)
    result["old_seq"], result["new_seq"] = old, new
    return result


@router.post("/rollback")
def rollback(
    document_id: int,
    target_seq: int,
    doc_user=Depends(require_doc_perm("write")),
    request: Request = None,
    db: Session = Depends(get_db),
):
    """一键回滚：以目标版本内容生成新版本，历史不丢弃"""
    doc, user = doc_user
    target = _get_version(db, document_id, target_seq)
    latest = db.query(DocumentVersion).filter_by(document_id=doc.id)\
        .order_by(DocumentVersion.seq.desc()).first()
    if latest and latest.seq == target.seq:
        raise HTTPException(409, f"当前已是 v{target.seq}，无需回滚")
    # 直接以 CAS 里已有的 blob 生成新版本（无需物理复制——CAS 天然共享内容）
    seq = latest.seq + 1 if latest else 1
    ver = DocumentVersion(
        document_id=doc.id, seq=seq, blob_hash=target.blob_hash,
        original_blob_hash=target.original_blob_hash, ext=target.ext, size=target.size,
        note=f"回滚至 v{target.seq}", source="rollback", source_ref=f"回滚自 v{target.seq}",
        created_by=user.id,
    )
    db.add(ver)
    doc.ext = target.ext
    doc.updated_at = dt.datetime.utcnow()
    doc.lock_owner_id = None
    doc.lock_expires_at = None
    db.commit()
    db.refresh(ver)
    audit.log(db, user.id, "version_rollback", request, "document", doc.id,
              {"target_seq": target.seq, "new_seq": ver.seq})
    return _version_out(db, ver)


@router.post("/tags")
def add_tag(document_id: int, version_seq: int,
            name: str | None = None, names: str | None = None,
            doc_user=Depends(require_doc_perm("read")), db: Session = Depends(get_db),
            user: User = Depends(get_current_user)):
    """版本标签：标签指向「某文档的某个版本」，用法类似 git tag。

    支持一次打多个：names="对外发布,评审稿"（逗号/分号/空格分隔）；重名标签静默忽略。
    """
    ver = _get_version(db, document_id, version_seq)
    raw: list[str] = []
    if names:
        raw += re.split(r"[,，;；\s]+", names)
    if name:
        raw.append(name)
    wanted: list[str] = []
    seen: set[str] = set()
    for r in raw:
        t = (r or "").strip()
        if not t or t.lower() in seen:
            continue
        if len(t) > 32:
            raise HTTPException(400, f"标签名过长（最多 32 字）：{t[:10]}…")
        seen.add(t.lower())
        wanted.append(t)
    if not wanted:
        raise HTTPException(400, "标签名不能为空")
    if len(wanted) > 10:
        raise HTTPException(400, "单次最多添加 10 个标签")

    existing = {t.name.lower() for t in db.query(VersionTag).filter_by(version_id=ver.id).all()}
    added, skipped = [], []
    for t in wanted:
        if t.lower() in existing:
            skipped.append(t)
            continue
        db.add(VersionTag(document_id=document_id, version_id=ver.id, name=t, created_by=user.id))
        added.append(t)
    db.commit()
    return {"ok": True, "added": added, "skipped": skipped}


@router.delete("/tags")
def delete_tag(document_id: int, version_seq: int, name: str,
               doc_user=Depends(require_doc_perm("read")), db: Session = Depends(get_db),
               user: User = Depends(get_current_user), request: Request = None):
    """删除版本标签：仅「打标签本人」或该文档管理员可删"""
    doc, _u = doc_user
    ver = _get_version(db, document_id, version_seq)
    t = (db.query(VersionTag)
         .filter_by(version_id=ver.id, name=name.strip()).first())
    if not t:
        raise HTTPException(404, "标签不存在")
    from .. import authz
    acc = authz.access_doc(db, user, doc)
    if t.created_by != user.id and not acc.can_manage:
        raise HTTPException(403, "只有打标签的人或该文档管理员可以删除标签")
    db.delete(t)
    db.commit()
    audit.log(db, user.id, "version_tag_delete", request, "document", document_id,
              {"seq": version_seq, "tag": name})
    return {"ok": True}


@router.get("/{seq}/download")
def download_version(document_id: int, seq: int,
                     doc_user=Depends(require_doc_perm("read")),
                     request: Request = None, db: Session = Depends(get_db)):
    """版本下载：命名含版本号 文档名_vN.docx"""
    doc, user = doc_user
    ver = _get_version(db, document_id, seq)
    return FileResponse(storage.path(ver.blob_hash),
                        filename=version_download_name(doc, ver),
                        media_type="application/octet-stream")
