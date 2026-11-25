# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 DocVault Contributors
# Author: Amyson Wu
"""空间 / 目录 / 成员（三级结构：空间→目录→文档）"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user, require_space_perm, system_admin_only
from ..models import Folder, Space, SpaceMember, User
from ..schemas import (FolderAclIn, FolderIn, FolderUpdateIn, MemberIn, SpaceIn,
                       SpaceUpdateIn)

router = APIRouter(prefix="/api/spaces", tags=["spaces"])


def space_out(s: Space, my_content: str | None):
    return {"id": s.id, "name": s.name, "description": s.description,
            "owner_id": s.owner_id, "my_content": my_content}


@router.get("")
def list_spaces(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """只列出当前用户可读的空间（系统管理员全部可见，作为兜底）"""
    from .. import authz
    spaces = db.query(Space).all()
    out = []
    for s in spaces:
        acc = authz.access_space(db, user, s.id)
        if not acc.can_read:
            continue
        role = "space_admin" if acc.is_admin else ("creator" if acc.source == "creator" else "member")
        out.append({**space_out(s, acc.content),
                    "my_admin": acc.is_admin,
                    "my_role": role, "is_system_admin": acc.is_system_admin})
    return out


@router.post("")
def create_space(data: SpaceIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """创建空间：创建者自动成为空间管理员 + 内容写权"""
    sp = Space(name=data.name, description=data.description, owner_id=user.id)
    db.add(sp)
    db.flush()
    db.add(SpaceMember(space_id=sp.id, user_id=user.id,
                       content="write", is_admin=True))
    db.commit()
    return {**space_out(sp, "write"), "my_admin": True,
            "my_role": "creator", "is_system_admin": user.role == "admin"}


@router.post("/{space_id}/folders")
def create_folder(space_id: int, data: FolderIn, db: Session = Depends(get_db),
                  user: User = Depends(require_space_perm("write"))):
    if data.parent_id is not None:
        from ..folder_perm import WRITE, folder_perm
        parent = db.get(Folder, data.parent_id)
        if not parent or parent.space_id != space_id:
            raise HTTPException(404, "父目录不存在")
        fp = folder_perm(db, user, parent)
        if fp is None:
            raise HTTPException(404, "父目录不存在")
        if fp != WRITE:
            raise HTTPException(403, "你在该目录仅有只读权限，无法新建子目录")
    f = Folder(space_id=space_id, parent_id=data.parent_id, name=data.name,
               created_by=user.id, owner_id=user.id)
    db.add(f)
    db.commit()
    return {"id": f.id, "name": f.name, "parent_id": f.parent_id}


@router.get("/{space_id}/folders")
def list_folders(space_id: int, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    """目录树：只返回「有权限的目录」及其「祖先路径」。
    祖先仅为层级完整性而展示（标为无权限、不可进入）；无关的兄弟目录不出现。"""
    from .. import authz
    if not authz.access_space(db, user, space_id).can_read:
        return []
    all_folders = db.query(Folder).filter_by(space_id=space_id).order_by(Folder.name).all()
    by_id = {f.id: f for f in all_folders}
    acc = {}
    for f in all_folders:
        a = authz.access_folder(db, user, f)
        if a.source == "private":
            continue          # 私有目录：未受邀者对元数据也不可见
        acc[f.id] = a

    keep: set[int] = set()
    for f in all_folders:
        a = acc.get(f.id)
        if a is None or not a.can_read:
            continue
        node = f
        guard = set()
        while node is not None and node.id not in guard:
            guard.add(node.id)
            keep.add(node.id)
            node = by_id.get(node.parent_id) if node.parent_id else None

    out = []
    for f in all_folders:
        if f.id not in keep:
            continue
        a = acc.get(f.id)
        out.append({"id": f.id, "name": f.name, "parent_id": f.parent_id,
                    "is_private": bool(f.is_private),
                    "owner_id": f.owner_id or f.created_by,
                    "my_content": a.content if a else "none",
                    "can_read": bool(a and a.can_read),
                    "can_manage": bool(a and a.can_manage)})
    return out


# ---- 目录级权限（私有目录白名单）：所有者或管理员可管理 ----
folders_router = APIRouter(prefix="/api/folders", tags=["folders"])


def _folder_or_404(db: Session, folder_id: int) -> Folder:
    f = db.get(Folder, folder_id)
    if not f:
        raise HTTPException(404, "目录不存在")
    return f


def _require_folder_owner(db: Session, user: User, folder: Folder):
    """目录的成员管理员（含系统管理员）才能管该目录的授权"""
    from .. import authz
    if not authz.access_folder(db, user, folder).can_manage:
        raise HTTPException(403, "你不是该目录的管理员，无权管理其权限")


@folders_router.get("/{folder_id}/acl")
def get_folder_acl(folder_id: int, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    from .. import authz
    from ..models import FolderAcl
    f = _folder_or_404(db, folder_id)
    acc = authz.access_folder(db, user, f)
    if not acc.can_read:
        raise HTTPException(404, "目录不存在")
    _require_folder_owner(db, user, f)
    rows = db.query(FolderAcl).filter_by(folder_id=folder_id).all()
    users = {u.id: u for u in db.query(User).filter(User.id.in_([r.user_id for r in rows] or [0])).all()}
    members = db.query(SpaceMember).filter_by(space_id=f.space_id).all()
    member_uids = {m.user_id for m in members}
    candidates = db.query(User).filter(User.is_active.is_(True)).order_by(User.username).all()
    return {
        "folder": {"id": f.id, "name": f.name, "is_private": bool(f.is_private),
                   "parent_id": f.parent_id, "space_id": f.space_id,
                   "owner_id": f.owner_id or f.created_by},
        "entries": [{"user_id": r.user_id,
                     "username": users[r.user_id].username if r.user_id in users else "",
                     "display_name": users[r.user_id].display_name if r.user_id in users else "",
                     "content": r.content, "is_admin": bool(r.is_admin)} for r in rows],
        "my": {"content": acc.content, "is_admin": acc.is_admin, "can_manage": acc.can_manage},
        "candidates": [{"user_id": u.id, "username": u.username,
                        "display_name": u.display_name or u.username,
                        "in_space": u.id in member_uids}
                       for u in candidates if u.id != (f.owner_id or f.created_by)],
    }


@folders_router.post("/{folder_id}/acl")
def set_folder_acl(folder_id: int, data: FolderAclIn, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user), request: Request = None):
    from .. import audit
    from ..folder_perm import folder_perm
    from ..models import FolderAcl
    f = _folder_or_404(db, folder_id)
    if folder_perm(db, user, f) is None:
        raise HTTPException(404, "目录不存在")
    _require_folder_owner(db, user, f)
    if data.content not in ("read", "write"):
        raise HTTPException(400, "内容权只能是 read 或 write")
    target = db.get(User, data.user_id)
    if not target or not target.is_active:
        raise HTTPException(404, "用户不存在")
    if data.user_id == (f.owner_id or f.created_by):
        raise HTTPException(400, "所有者无需邀请，默认拥有可写权限")
    from .. import authz
    acc = authz.access_folder(db, user, f)
    # 权限不放大：非系统管理员只能授出自己不高于自身的内容权
    if not acc.is_system_admin and authz.RANK[data.content] > authz.RANK[acc.content]:
        raise HTTPException(403, "不能授出高于自身的内容权限")
    row = db.query(FolderAcl).filter_by(folder_id=folder_id, user_id=data.user_id).first()
    if row:
        row.content = data.content
        if data.is_admin is not None:
            row.is_admin = bool(data.is_admin)
        row.granted_by = user.id
    else:
        db.add(FolderAcl(folder_id=folder_id, user_id=data.user_id, content=data.content,
                         is_admin=bool(data.is_admin), granted_by=user.id, created_by=user.id))
    db.commit()
    audit.log(db, user.id, "folder_acl_set", request, "folder", folder_id,
              {"user_id": data.user_id, "content": data.content, "is_admin": data.is_admin})
    return {"ok": True}


@folders_router.delete("/{folder_id}/acl/{user_id}")
def remove_folder_acl(folder_id: int, user_id: int, db: Session = Depends(get_db),
                      user: User = Depends(get_current_user), request: Request = None):
    from .. import audit
    from ..folder_perm import folder_perm
    from ..models import FolderAcl
    f = _folder_or_404(db, folder_id)
    if folder_perm(db, user, f) is None:
        raise HTTPException(404, "目录不存在")
    _require_folder_owner(db, user, f)
    row = db.query(FolderAcl).filter_by(folder_id=folder_id, user_id=user_id).first()
    if not row:
        raise HTTPException(404, "该用户未被授权")
    db.delete(row)
    db.commit()
    audit.log(db, user.id, "folder_acl_remove", request, "folder", folder_id, {"user_id": user_id})
    return {"ok": True}


@folders_router.patch("/{folder_id}")
def update_folder(folder_id: int, data: FolderUpdateIn, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user), request: Request = None):
    """设置/取消私有（仅受邀可见）"""
    from .. import audit
    from ..folder_perm import folder_perm
    from ..models import FolderAcl
    f = _folder_or_404(db, folder_id)
    if folder_perm(db, user, f) is None:
        raise HTTPException(404, "目录不存在")
    _require_folder_owner(db, user, f)
    changed = {}
    if data.is_private is not None:
        f.is_private = bool(data.is_private)
        changed["is_private"] = f.is_private
        if not f.is_private:
            # 转回公开：清空白名单，避免残留授权影响后续判断
            db.query(FolderAcl).filter_by(folder_id=folder_id).delete(synchronize_session=False)
    if data.name is not None and data.name.strip():
        f.name = data.name.strip()
        changed["name"] = f.name
    # 调整层级：移到空间根，或挂到同空间的另一目录下（含防环校验）
    if data.move_to_root:
        f.parent_id = None
        changed["parent_id"] = None
    elif data.parent_id is not None and data.parent_id != f.parent_id:
        parent = db.get(Folder, data.parent_id)
        if not parent or parent.space_id != f.space_id:
            raise HTTPException(404, "目标父目录不存在")
        # 防环：目标不能是自己或自己的后代
        node, guard = parent, set()
        while node is not None and node.id not in guard:
            if node.id == f.id:
                raise HTTPException(400, "不能把目录移动到自己的子目录下")
            guard.add(node.id)
            node = db.get(Folder, node.parent_id) if node.parent_id else None
        f.parent_id = parent.id
        changed["parent_id"] = parent.id
    if not changed:
        raise HTTPException(400, "没有需要更新的字段")
    db.commit()
    audit.log(db, user.id, "folder_update", request, "folder", folder_id, changed)
    return {"ok": True, **changed}


@router.patch("/{space_id}")
def update_space(space_id: int, data: SpaceUpdateIn, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user), request: Request = None):
    """修改空间名称/描述：空间创建者、空间管理员或系统管理员"""
    from .. import audit, authz
    sp = db.get(Space, space_id)
    if not sp:
        raise HTTPException(404, "空间不存在")
    acc = authz.access_space(db, user, space_id)
    # 仅空间创建者、空间管理员或系统管理员可改名
    if not (sp.owner_id == user.id or acc.is_admin):
        raise HTTPException(403, "只有空间创建者或管理员可以修改空间信息")
    changed = {}
    if data.name is not None:
        name = data.name.strip()
        if not name:
            raise HTTPException(400, "空间名称不能为空")
        if len(name) > 64:
            raise HTTPException(400, "空间名称不能超过 64 个字符")
        sp.name = name
        changed["name"] = name
    if data.description is not None:
        sp.description = data.description.strip()[:512]
        changed["description"] = sp.description
    if not changed:
        raise HTTPException(400, "没有需要更新的字段")
    db.commit()
    audit.log(db, user.id, "space_update", request, "space", space_id, changed)
    return {"ok": True, **changed}


@router.delete("/{space_id}")
def delete_space(space_id: int, db: Session = Depends(get_db),
                 user: User = Depends(system_admin_only),
                 request: Request = None):
    """删除空间：仅系统管理员。仍有「在用」文档时拒绝；回收站内文档随空间一并彻底清理"""
    from .. import audit
    from ..models import (Document, DocumentAcl, DocumentGroupAcl, DocumentVersion, FolderAcl,
                          FolderGroupAcl, OfflineTask, ShareLink, VersionTag)
    sp = db.get(Space, space_id)
    if not sp:
        raise HTTPException(404, "空间不存在")
    active = db.query(Document).filter_by(space_id=space_id, status="active").count()
    if active:
        raise HTTPException(409, f"空间内还有 {active} 个文档在用，请先删除（移入回收站）后再删除空间")

    # 回收站文档随空间彻底清理（含版本、标签、分享、授权、离线任务）
    trashed = db.query(Document).filter_by(space_id=space_id).all()
    for d in trashed:
        db.query(VersionTag).filter_by(document_id=d.id).delete(synchronize_session=False)
        db.query(DocumentAcl).filter_by(document_id=d.id).delete(synchronize_session=False)
        db.query(DocumentGroupAcl).filter_by(document_id=d.id).delete(synchronize_session=False)
        db.query(ShareLink).filter_by(document_id=d.id).delete(synchronize_session=False)
        db.query(OfflineTask).filter_by(document_id=d.id).delete(synchronize_session=False)
        db.query(DocumentVersion).filter_by(document_id=d.id).delete(synchronize_session=False)
        db.delete(d)
    purged = len(trashed)
    db.flush()      # 先把文档删除落地，否则删除目录会撞 folder_id 外键

    folders = db.query(Folder).filter_by(space_id=space_id).all()
    parent_ids = [f.id for f in folders]
    if parent_ids:
        db.query(FolderAcl).filter(FolderAcl.folder_id.in_(parent_ids)).delete(synchronize_session=False)
        db.query(FolderGroupAcl).filter(FolderGroupAcl.folder_id.in_(parent_ids)).delete(synchronize_session=False)
        db.query(Folder).filter(Folder.parent_id.in_(parent_ids)).delete(synchronize_session=False)
    db.query(Folder).filter_by(space_id=space_id).delete(synchronize_session=False)
    db.query(SpaceMember).filter_by(space_id=space_id).delete(synchronize_session=False)
    name = sp.name
    db.delete(sp)
    db.commit()
    audit.log(db, user.id, "space_delete", request, "space", space_id,
              {"name": name, "purged_docs": purged})
    return {"ok": True, "name": name, "purged_docs": purged}


@router.post("/{space_id}/members")
def add_member(space_id: int, data: MemberIn, db: Session = Depends(get_db),
               user: User = Depends(get_current_user), request: Request = None):
    """邀请成员 / 调整授权：仅该空间管理员或系统管理员；不得授出高于自身的权限"""
    from .. import audit, authz
    content, is_admin = data.content, data.is_admin
    if content is None and is_admin is None:
        content = "read"
    res = authz.grant(db, user, "space", space_id, data.user_id,
                      content=content, is_admin=is_admin)
    audit.log(db, user.id, "space_member_set", request, "space", space_id,
              {"user_id": data.user_id, "content": content, "is_admin": is_admin})
    return res


@router.get("/{space_id}/members")
def list_members(space_id: int, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    """空间成员与授权列表（权限面板用）"""
    from .. import authz
    acc = authz.access_space(db, user, space_id)
    if not acc.can_read:
        raise HTTPException(404, "空间不存在")
    rows = db.query(SpaceMember).filter_by(space_id=space_id).all()
    users = {u.id: u for u in db.query(User).filter(
        User.id.in_([r.user_id for r in rows] or [0])).all()}
    entries = []
    for r in rows:
        u = users.get(r.user_id)
        entries.append({
            "user_id": r.user_id,
            "username": u.username if u else "",
            "display_name": (u.display_name or u.username) if u else "",
            "content": r.content,
            "is_admin": bool(r.is_admin),
        })
    candidates = db.query(User).filter(User.is_active.is_(True)).order_by(User.username).all()
    in_space = {r.user_id for r in rows}
    return {
        "space": {"id": space_id},
        "my": {"content": acc.content, "is_admin": acc.is_admin,
               "can_manage": acc.can_manage, "is_system_admin": acc.is_system_admin},
        "entries": entries,
        "candidates": [{"user_id": u.id, "username": u.username,
                        "display_name": u.display_name or u.username}
                       for u in candidates if u.id not in in_space],
    }


@router.delete("/{space_id}/members/{user_id}")
def remove_member(space_id: int, user_id: int, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user), request: Request = None):
    from .. import audit, authz
    res = authz.revoke(db, user, "space", space_id, user_id)
    audit.log(db, user.id, "space_member_remove", request, "space", space_id,
              {"user_id": user_id})
    return res
