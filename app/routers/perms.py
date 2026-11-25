# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 DocVault Contributors
# Author: Amyson Wu
"""统一权限接口：空间 / 目录 / 文档三级通用

GET    /api/perms/{node_type}/{node_id}                     查看对象授权（含我的权限与继承来源）
POST   /api/perms/{node_type}/{node_id}                      授权（用户 / 用户组；内容 read/write、管理员、时限）
DELETE /api/perms/{node_type}/{node_id}/{user_id}            撤销用户授权（兼容旧路径）
DELETE /api/perms/{node_type}/{node_id}/{subject}/{sid}       撤销授权：subject=user|group
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import audit, authz
from ..db import get_db
from ..deps import get_current_user
from ..models import (
    Document, DocumentAcl, DocumentGroupAcl, Folder, FolderAcl, FolderGroupAcl,
    GroupMember, SpaceMember, User, UserGroup,
)

router = APIRouter(prefix="/api/perms", tags=["perms"])


class GrantIn(BaseModel):
    user_id: int | None = None      # 兼容旧调用：等价 subject_type=user
    subject_type: str = "user"      # user / group
    subject_id: int | None = None
    content: str | None = None      # none / read / write（write 自动含读）
    is_admin: bool | None = None    # 对象管理员（管人，不隐含写权）
    hours: int | None = None        # 临时授权时长（小时），空=永久
    reason: str | None = None


def _resolve(db: Session, user: User, node_type: str, node_id: int) -> authz.Access:
    if node_type == "space":
        return authz.access_space(db, user, node_id)
    if node_type == "folder":
        f = db.get(Folder, node_id)
        if not f:
            raise HTTPException(404, "目录不存在")
        return authz.access_folder(db, user, f)
    if node_type == "document":
        d = db.get(Document, node_id)
        if not d:
            raise HTTPException(404, "文档不存在")
        return authz.access_doc(db, user, d)
    raise HTTPException(400, "非法对象类型")


def _group_rows(db: Session, node_type: str, node_id: int):
    if node_type == "folder":
        return db.query(FolderGroupAcl).filter_by(folder_id=node_id).all()
    if node_type == "document":
        return db.query(DocumentGroupAcl).filter_by(document_id=node_id).all()
    return []


@router.get("/{node_type}/{node_id}")
def get_perms(node_type: str, node_id: int, db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    acc = _resolve(db, user, node_type, node_id)
    if not acc.can_read:
        raise HTTPException(404, "对象不存在或无权查看")

    entries: list[dict] = []
    if node_type == "space":
        rows = db.query(SpaceMember).filter_by(space_id=node_id).all()
        ids = [r.user_id for r in rows]
        users = {u.id: u for u in db.query(User).filter(User.id.in_(ids or [0])).all()}
        for r in rows:
            u = users.get(r.user_id)
            entries.append({"subject_type": "user", "subject_id": r.user_id,
                            "user_id": r.user_id,
                            "username": u.username if u else "",
                            "display_name": (u.display_name or u.username) if u else "",
                            "content": r.content,
                            "is_admin": bool(r.is_admin), "expires_at": None})
    else:
        model, key = ((FolderAcl, "folder_id") if node_type == "folder"
                      else (DocumentAcl, "document_id"))
        rows = db.query(model).filter_by(**{key: node_id}).all()
        ids = [r.user_id for r in rows]
        users = {u.id: u for u in db.query(User).filter(User.id.in_(ids or [0])).all()}
        for r in rows:
            u = users.get(r.user_id)
            content = r.content
            entries.append({"subject_type": "user", "subject_id": r.user_id,
                            "user_id": r.user_id,
                            "username": u.username if u else "",
                            "display_name": (u.display_name or u.username) if u else "",
                            "content": content, "is_admin": bool(r.is_admin),
                            "expires_at": r.expires_at})
        # 用户组授权
        grows = _group_rows(db, node_type, node_id)
        gids = [r.group_id for r in grows]
        groups = {g.id: g for g in db.query(UserGroup).filter(UserGroup.id.in_(gids or [0])).all()}
        for r in grows:
            g = groups.get(r.group_id)
            content = r.content
            entries.append({"subject_type": "group", "subject_id": r.group_id,
                            "user_id": None,
                            "username": "", "display_name": g.name if g else "（已删除的组）",
                            "group_id": r.group_id,
                            "content": content, "is_admin": bool(r.is_admin),
                            "expires_at": r.expires_at})

    present = {e["subject_id"] for e in entries if e["subject_type"] == "user"}
    candidates = db.query(User).filter(User.is_active.is_(True)).order_by(User.username).all()

    # 可选的用户组（供授权选择器用）：按树的深度优先顺序 + 层级深度
    gtree: list[dict] = []
    if node_type != "space":
        from .groups import ordered_groups
        all_groups = db.query(UserGroup).all()
        counts: dict[int, int] = {}
        for m in db.query(GroupMember).all():
            counts[m.group_id] = counts.get(m.group_id, 0) + 1
        gtree = [{"id": g.id, "name": g.name, "parent_id": g.parent_id,
                  "depth": depth, "member_count": counts.get(g.id, 0)}
                 for g, depth in ordered_groups(all_groups)]

    return {
        "node_type": node_type, "node_id": node_id,
        "my": {"content": acc.content, "is_admin": acc.is_admin,
               "can_read": acc.can_read, "can_write": acc.can_write,
               "can_manage": acc.can_manage, "is_system_admin": acc.is_system_admin,
               "source": acc.source, "elevated": acc.elevated,
               "expires_at": acc.expires_at},
        "entries": entries,
        "candidates": [{"user_id": u.id, "username": u.username,
                        "display_name": u.display_name or u.username}
                       for u in candidates if u.id not in present],
        "groups": gtree,
    }


@router.post("/{node_type}/{node_id}")
def grant_perm(node_type: str, node_id: int, data: GrantIn,
               db: Session = Depends(get_db), user: User = Depends(get_current_user),
               request: Request = None):
    subject_type = data.subject_type if data.subject_type in ("user", "group") else "user"
    subject_id = data.subject_id if data.subject_id is not None else data.user_id
    if subject_id is None:
        raise HTTPException(400, "缺少被授权对象")
    target_user_id = None if subject_type == "group" else subject_id
    res = authz.grant(db, user, node_type, node_id, target_user_id,
                      content=data.content, is_admin=data.is_admin,
                      hours=data.hours, reason=data.reason,
                      subject_type=subject_type, subject_id=subject_id)
    audit.log(db, user.id, "perm_grant", request, node_type, node_id,
              {"subject_type": subject_type, "subject_id": subject_id,
               "content": data.content, "is_admin": data.is_admin,
               "hours": data.hours, "reason": data.reason})
    return res


@router.delete("/{node_type}/{node_id}/{subject}/{subject_id}")
def revoke_perm_v2(node_type: str, node_id: int, subject: str, subject_id: int,
                   db: Session = Depends(get_db), user: User = Depends(get_current_user),
                   request: Request = None):
    if subject not in ("user", "group"):
        raise HTTPException(400, "主体只能是 user / group")
    res = authz.revoke(db, user, node_type, node_id,
                       subject_id if subject == "user" else None,
                       subject_type=subject, subject_id=subject_id)
    audit.log(db, user.id, "perm_revoke", request, node_type, node_id,
              {"subject_type": subject, "subject_id": subject_id})
    return res


@router.delete("/{node_type}/{node_id}/{user_id}")
def revoke_perm(node_type: str, node_id: int, user_id: int,
                db: Session = Depends(get_db), user: User = Depends(get_current_user),
                request: Request = None):
    """兼容旧路径：撤销用户授权"""
    res = authz.revoke(db, user, node_type, node_id, user_id)
    audit.log(db, user.id, "perm_revoke", request, node_type, node_id, {"user_id": user_id})
    return res
