# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 DocVault Contributors
# Author: Amyson Wu
"""管理端：审计查询/ 存储统计/ 临时提权"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..deps import get_current_user
from ..models import AuditLog, Document, DocumentVersion, User

router = APIRouter(prefix="/api/admin", tags=["admin"])


def require_admin(user: User = Depends(get_current_user)):
    if user.role != "admin":
        raise HTTPException(403, "需要系统管理员权限")
    return user


@router.get("/audit")
def query_audit(action: str | None = None, user_id: int | None = None, limit: int = 200,
                db: Session = Depends(get_db), _: User = Depends(require_admin)):
    q = db.query(AuditLog)
    if action:
        q = q.filter(AuditLog.action == action)
    if user_id:
        q = q.filter(AuditLog.user_id == user_id)
    logs = q.order_by(AuditLog.created_at.desc()).limit(min(limit, 1000)).all()
    users = {u.id: u.display_name for u in db.query(User).all()}
    return [{"id": l.id, "user": users.get(l.user_id), "action": l.action,
             "target_type": l.target_type, "target_id": l.target_id,
             "detail": l.detail, "ip": l.ip, "created_at": l.created_at} for l in logs]


@router.post("/elevate/{document_id}")
def elevate(document_id: int, hours: int = 2, content: str = "read",
            db: Session = Depends(get_db), user: User = Depends(require_admin),
            request: Request = None):
    """系统管理员临时提权（JIT）：按需获得该文档的内容读/写权限，默认 2 小时自动失效并留痕"""
    from .. import authz
    doc = db.get(Document, document_id)
    if not doc:
        raise HTTPException(404, "文档不存在")
    if content not in ("read", "write"):
        raise HTTPException(400, "提权类型只能是 read 或 write")
    res = authz.grant(db, user, "document", document_id, user.id,
                      content=content, hours=max(1, min(hours, 24)),
                      reason=f"管理员临时提权（{content}）")
    audit.log(db, user.id, "admin_elevate", request, "document", document_id,
              {"hours": hours, "content": content, "expires_at": str(res.get("expires_at"))})
    return res


@router.get("/users")
def list_users(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """用户清单（用户组成员维护用）"""
    from ..models import GroupMember, UserGroup

    groups_of: dict[int, list[str]] = {}
    for gm, g in (db.query(GroupMember, UserGroup)
                  .join(UserGroup, UserGroup.id == GroupMember.group_id).all()):
        groups_of.setdefault(gm.user_id, []).append(g.name)
    rows = db.query(User).filter(User.is_active.is_(True)).order_by(User.username).all()
    return [{"user_id": u.id, "username": u.username,
             "display_name": u.display_name or u.username,
             "role": u.role, "groups": groups_of.get(u.id, [])} for u in rows]


@router.delete("/users/{user_id}")
def delete_user(user_id: int, db: Session = Depends(get_db),
                user: User = Depends(require_admin), request: Request = None):
    """删除账号：仅系统管理员，且只允许删除「无内容归属」的账号。
    有归属（空间/目录/文档/版本/标签）的账号请先转移或改用停用，避免内容失去责任人。
    审计日志保留（只留 user_id，用于追溯）。"""
    from ..models import (
        DocumentAcl, DocumentGroupAcl, DocumentVersion, Folder, FolderAcl,
        FolderGroupAcl, GroupMember, OfflineTask, ShareLink, Space, SpaceMember, VersionTag,
    )

    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404, "用户不存在")
    if target.id == user.id:
        raise HTTPException(400, "不能删除当前登录的账号")

    owned = {
        "空间": db.query(Space).filter_by(owner_id=user_id).count(),
        "目录": db.query(Folder).filter((Folder.owner_id == user_id) | (Folder.created_by == user_id)).count(),
        "文档": db.query(Document).filter(Document.created_by == user_id).count(),
        "版本": db.query(DocumentVersion).filter(DocumentVersion.created_by == user_id).count(),
    }
    blocked = {k: v for k, v in owned.items() if v}
    if blocked:
        raise HTTPException(
            409, "该账号仍有内容归属：" + "、".join(f"{k} {v} 个" for k, v in blocked.items())
                 + "；请先转移或停用账号")
    if target.role == "admin":
        left = db.query(User).filter(User.role == "admin", User.is_active.is_(True),
                                     User.id != user_id).count()
        if not left:
            raise HTTPException(400, "至少保留一个可用的系统管理员账号")

    # 清理该账号的成员关系与授权，避免留下悬空引用
    db.query(GroupMember).filter_by(user_id=user_id).delete(synchronize_session=False)
    db.query(SpaceMember).filter_by(user_id=user_id).delete(synchronize_session=False)
    db.query(FolderAcl).filter_by(user_id=user_id).delete(synchronize_session=False)
    db.query(DocumentAcl).filter_by(user_id=user_id).delete(synchronize_session=False)
    db.query(ShareLink).filter_by(created_by=user_id).delete(synchronize_session=False)
    db.query(VersionTag).filter_by(created_by=user_id).delete(synchronize_session=False)
    db.query(OfflineTask).filter_by(user_id=user_id).delete(synchronize_session=False)
    # 引用型字段置空（锁、删除人、授权人）
    db.query(Document).filter_by(lock_owner_id=user_id).update(
        {Document.lock_owner_id: None}, synchronize_session=False)
    db.query(Document).filter_by(deleted_by=user_id).update(
        {Document.deleted_by: None}, synchronize_session=False)
    db.query(FolderAcl).filter_by(granted_by=user_id).update(
        {FolderAcl.granted_by: None}, synchronize_session=False)
    db.query(DocumentAcl).filter_by(granted_by=user_id).update(
        {DocumentAcl.granted_by: None}, synchronize_session=False)
    db.query(FolderGroupAcl).filter_by(granted_by=user_id).update(
        {FolderGroupAcl.granted_by: None}, synchronize_session=False)
    db.query(DocumentGroupAcl).filter_by(granted_by=user_id).update(
        {DocumentGroupAcl.granted_by: None}, synchronize_session=False)

    name = target.display_name or target.username
    username = target.username
    db.delete(target)
    db.commit()
    audit.log(db, user.id, "user_delete", request, "user", user_id,
              {"username": username, "display_name": name})
    return {"ok": True, "user_id": user_id, "username": username, "name": name}


@router.get("/stats")
def stats(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """存储统计：逻辑总量 vs 真实磁盘占用（块级去重 + zstd 压缩后的落盘效果）"""
    from ..models import StorageChunk
    from ..storage import storage

    n_docs = db.query(func.count(Document.id)).filter(Document.status == "active").scalar() or 0
    # 逻辑总量按全部版本计（含回收站）：块库仍持有回收站文档的块引用，口径一致才可比
    q = db.query(DocumentVersion).join(Document, Document.id == DocumentVersion.document_id)
    n_versions = q.count()
    logical_bytes = db.query(func.coalesce(func.sum(DocumentVersion.size), 0)).scalar() or 0

    # 块库：引用数 vs 唯一块数 → 去重率
    chunk_refs = db.query(func.coalesce(func.sum(StorageChunk.refcount), 0)).scalar() or 0
    unique_chunks = db.query(func.count(StorageChunk.hash)).scalar() or 0
    chunk_logical_bytes = db.query(func.coalesce(func.sum(StorageChunk.size), 0)).scalar() or 0

    # 实际磁盘占用 = blobs 目录所有文件实测大小（zstd 压缩块 + raw 整件）
    actual_bytes = sum(e.stat().st_size for e in storage.root.rglob("*") if e.is_file())

    saved = max(logical_bytes - actual_bytes, 0)
    return {"documents": n_docs, "versions": n_versions,
            "logical_bytes": logical_bytes, "actual_bytes": actual_bytes,
            "saved_bytes": saved,
            "save_pct": round(saved * 100 / logical_bytes, 1) if logical_bytes else None,
            "chunk_refs": chunk_refs, "unique_chunks": unique_chunks,
            "dedup_pct": round((1 - unique_chunks / chunk_refs) * 100, 1) if chunk_refs else None,
            "compress_ratio": round(chunk_logical_bytes / actual_bytes, 1) if actual_bytes else None,
            "note": "逻辑总量 = 各版本完整文件大小之和（按版本各存一份整件的口径）；"
                    "实际占用 = data/blobs 真实落盘（块级去重 + zstd 压缩后）。"}
