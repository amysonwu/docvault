# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 DocVault Contributors
# Author: Amyson Wu
"""用户组维护：多级树 + 成员（一个用户可属于多个组）

GET    /api/groups                        组列表（扁平，带 parent_id / 成员数，前端拼树）
POST   /api/groups                        新建组（可指定上级组）
GET    /api/groups/{id}                   组详情 + 成员
PATCH  /api/groups/{id}                   改名 / 描述 / 调整上级组
DELETE /api/groups/{id}                   删除组（连同子组、成员关系与组授权）
POST   /api/groups/{id}/members           加成员
DELETE /api/groups/{id}/members/{user_id} 移除成员
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..deps import get_current_user
from ..models import (
    DocumentGroupAcl, FolderGroupAcl, GroupMember, User, UserGroup,
)

router = APIRouter(prefix="/api/groups", tags=["groups"])


class GroupIn(BaseModel):
    name: str
    parent_id: int | None = None
    description: str | None = None


class GroupPatch(BaseModel):
    name: str | None = None
    parent_id: int | None = None
    description: str | None = None


class MemberIn(BaseModel):
    user_id: int


def _require_admin(user: User):
    if user.role != "admin":
        raise HTTPException(403, "仅系统管理员可维护用户组")


def ordered_groups(rows: list[UserGroup]) -> list[tuple[UserGroup, int]]:
    """按树的深度优先顺序返回 (组, 深度)：父组后面紧跟其子组。
    不能按 (depth, name) 平铺排序——那样同一层级的组会被排到所有顶级组之后，
    前端加缩进后就"看起来"挂在了最后一个顶级组下面。"""
    by_parent: dict[int | None, list[UserGroup]] = {}
    for g in rows:
        by_parent.setdefault(g.parent_id, []).append(g)
    out: list[tuple[UserGroup, int]] = []

    def walk(pid, depth: int):
        for g in sorted(by_parent.get(pid, []), key=lambda x: x.name):
            out.append((g, depth))
            if depth < 32:
                walk(g.id, depth + 1)

    walk(None, 0)
    seen = {g.id for g, _ in out}
    for g in rows:                      # 父节点缺失的孤儿兜底落到根层
        if g.id not in seen:
            out.append((g, 0))
    return out


@router.get("")
def list_groups(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = db.query(UserGroup).all()
    counts = {}
    for m in db.query(GroupMember).all():
        counts[m.group_id] = counts.get(m.group_id, 0) + 1
    groups = [{
        "id": g.id, "name": g.name, "parent_id": g.parent_id,
        "description": g.description or "", "member_count": counts.get(g.id, 0),
        "depth": depth,
    } for g, depth in ordered_groups(rows)]
    return {"groups": groups, "can_manage": user.role == "admin"}


@router.post("")
def create_group(data: GroupIn, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user), request: Request = None):
    _require_admin(user)
    name = (data.name or "").strip()
    if not name:
        raise HTTPException(400, "组名不能为空")
    if data.parent_id:
        if not db.get(UserGroup, data.parent_id):
            raise HTTPException(404, "上级组不存在")
    dup = db.query(UserGroup).filter_by(parent_id=data.parent_id, name=name).first()
    if dup:
        raise HTTPException(400, "同级下已有同名组")
    g = UserGroup(name=name, parent_id=data.parent_id,
                  description=(data.description or "").strip(), created_by=user.id)
    db.add(g)
    db.commit()
    audit.log(db, user.id, "group_create", request, "group", g.id, {"name": name})
    return {"id": g.id, "name": g.name, "parent_id": g.parent_id, "member_count": 0}


@router.get("/{group_id}")
def get_group(group_id: int, db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    g = db.get(UserGroup, group_id)
    if not g:
        raise HTTPException(404, "用户组不存在")
    members = (db.query(GroupMember, User)
               .join(User, User.id == GroupMember.user_id)
               .filter(GroupMember.group_id == group_id)
               .order_by(User.username).all())
    children = db.query(UserGroup).filter_by(parent_id=group_id).order_by(UserGroup.name).all()
    parent = db.get(UserGroup, g.parent_id) if g.parent_id else None
    return {
        "id": g.id, "name": g.name, "parent_id": g.parent_id,
        "description": g.description or "",
        "parent_name": parent.name if parent else "",
        "children": [{"id": c.id, "name": c.name, "member_count":
                      db.query(GroupMember).filter_by(group_id=c.id).count()} for c in children],
        "members": [{"user_id": u.id, "username": u.username,
                     "display_name": u.display_name or u.username} for _, u in members],
    }


@router.patch("/{group_id}")
def update_group(group_id: int, data: GroupPatch, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user), request: Request = None):
    _require_admin(user)
    g = db.get(UserGroup, group_id)
    if not g:
        raise HTTPException(404, "用户组不存在")
    if data.name is not None:
        name = data.name.strip()
        if not name:
            raise HTTPException(400, "组名不能为空")
        dup = db.query(UserGroup).filter(
            UserGroup.parent_id == (data.parent_id if data.parent_id is not None else g.parent_id),
            UserGroup.name == name, UserGroup.id != group_id).first()
        if dup:
            raise HTTPException(400, "同级下已有同名组")
        g.name = name
    if data.description is not None:
        g.description = data.description.strip()
    if data.parent_id is not None:
        if data.parent_id == group_id:
            raise HTTPException(400, "不能把自己设为上级组")
        if data.parent_id:
            if not db.get(UserGroup, data.parent_id):
                raise HTTPException(404, "上级组不存在")
            # 防止形成环：新上级不能是自己的后代
            cur, guard = data.parent_id, 0
            while cur and guard < 64:
                if cur == group_id:
                    raise HTTPException(400, "不能把组移动到自己的子组下")
                p = db.get(UserGroup, cur)
                cur = p.parent_id if p else None
                guard += 1
        g.parent_id = data.parent_id or None
    db.commit()
    audit.log(db, user.id, "group_update", request, "group", group_id,
              {"name": g.name, "parent_id": g.parent_id})
    return {"ok": True, "id": g.id, "name": g.name, "parent_id": g.parent_id}


@router.delete("/{group_id}")
def delete_group(group_id: int, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user), request: Request = None):
    _require_admin(user)
    g = db.get(UserGroup, group_id)
    if not g:
        raise HTTPException(404, "用户组不存在")
    # 收集子树
    ids, frontier = [group_id], [group_id]
    while frontier:
        rows = db.query(UserGroup.id).filter(UserGroup.parent_id.in_(frontier)).all()
        frontier = [r[0] for r in rows if r[0] not in ids]
        ids.extend(frontier)
    db.query(GroupMember).filter(GroupMember.group_id.in_(ids)).delete(synchronize_session=False)
    db.query(FolderGroupAcl).filter(FolderGroupAcl.group_id.in_(ids)).delete(synchronize_session=False)
    db.query(DocumentGroupAcl).filter(DocumentGroupAcl.group_id.in_(ids)).delete(synchronize_session=False)
    # 先断开自引用（parent_id 外键不允许直接删父行），再整棵子树删除
    db.query(UserGroup).filter(UserGroup.id.in_(ids)).update(
        {UserGroup.parent_id: None}, synchronize_session=False)
    db.query(UserGroup).filter(UserGroup.id.in_(ids)).delete(synchronize_session=False)
    db.commit()
    audit.log(db, user.id, "group_delete", request, "group", group_id,
              {"name": g.name, "removed_group_ids": ids})
    return {"ok": True, "removed": ids}


@router.post("/{group_id}/members")
def add_member(group_id: int, data: MemberIn, db: Session = Depends(get_db),
               user: User = Depends(get_current_user), request: Request = None):
    _require_admin(user)
    if not db.get(UserGroup, group_id):
        raise HTTPException(404, "用户组不存在")
    target = db.get(User, data.user_id)
    if not target:
        raise HTTPException(404, "用户不存在")
    dup = db.query(GroupMember).filter_by(group_id=group_id, user_id=data.user_id).first()
    if dup:
        return {"ok": True, "already": True}
    db.add(GroupMember(group_id=group_id, user_id=data.user_id))
    db.commit()
    audit.log(db, user.id, "group_member_add", request, "group", group_id,
              {"user_id": data.user_id})
    return {"ok": True}


@router.delete("/{group_id}/members/{user_id}")
def remove_member(group_id: int, user_id: int, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user), request: Request = None):
    _require_admin(user)
    row = db.query(GroupMember).filter_by(group_id=group_id, user_id=user_id).first()
    if row:
        db.delete(row)
        db.commit()
        audit.log(db, user.id, "group_member_remove", request, "group", group_id,
                  {"user_id": user_id})
    return {"ok": True}
