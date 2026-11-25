# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 DocVault Contributors
# Author: Amyson Wu
"""统一权限内核：管人 / 管事分离 + 就近覆盖 + 写含读

设计要点（对应权限规划）：
- 对象层级：空间 → 目录（可嵌套）→ 文档，每个节点都可单独授权
- 判定顺序：文档 → 目录链（自底向上）→ 空间根，命中最近一条显式授权即生效（就近覆盖）
- 写 ⊇ 读：content=write 自动具备读；不存在"能写不能读"
- 管理权独立：is_admin 只管成员（邀请/移除/授权），不隐含内容写权
- 默认无权限：未授权即 none，私有目录对未受邀者彻底不可见（不继承空间权限）
- 创建者自动赋权：空间/目录/文档创建者 = 该对象的成员管理员 + 内容写权
- 系统管理员：内容只读兜底（穿透读），成员与结构操作兜底；内容写需临时提权
"""
import datetime as dt
from dataclasses import dataclass

from sqlalchemy.orm import Session

from .models import (
    Document, DocumentAcl, DocumentGroupAcl, Folder, FolderAcl, FolderGroupAcl,
    GroupMember, Space, SpaceMember, User, UserGroup,
)

NONE, READ, WRITE = "none", "read", "write"
RANK = {NONE: 0, READ: 1, WRITE: 2}


@dataclass
class Access:
    content: str = NONE
    is_admin: bool = False
    is_system_admin: bool = False
    source: str = "none"          # document / folder:N / space / creator / system / private
    elevated: bool = False        # 是否来自临时提权
    expires_at: dt.datetime | None = None

    @property
    def can_read(self) -> bool:
        return RANK[self.content] >= RANK[READ]

    @property
    def can_write(self) -> bool:
        return RANK[self.content] >= RANK[WRITE]

    @property
    def can_manage(self) -> bool:
        return self.is_admin or self.is_system_admin


def _active(expires_at) -> bool:
    return expires_at is None or expires_at > dt.datetime.utcnow()


def _doc_entry(db: Session, user_id: int, doc_id: int):
    a = db.query(DocumentAcl).filter_by(document_id=doc_id, user_id=user_id).first()
    if a is None or not _active(a.expires_at):
        return None
    return (a.content, a.is_admin, a.expires_at)


def _folder_entry(db: Session, user_id: int, folder_id: int):
    a = db.query(FolderAcl).filter_by(folder_id=folder_id, user_id=user_id).first()
    if a is None or not _active(a.expires_at):
        return None
    return (a.content, a.is_admin, a.expires_at)


def _space_entry(db: Session, user_id: int, space_id: int):
    m = db.query(SpaceMember).filter_by(space_id=space_id, user_id=user_id).first()
    if m is None:
        return None
    return (m.content, m.is_admin)


# ---------------- 用户组 ----------------

def group_scope_ids(db: Session, user_id: int) -> set[int]:
    """该用户「有效所属」的组 = 直接所在组 + 它们的全部祖先组。
    授权给上级组时，下级组成员同样生效（组织树向下继承）。
    结果按 session 缓存，避免列表页每条文档都重复查询。"""
    cache = db.info.setdefault("_group_scope", {})
    if user_id in cache:
        return cache[user_id]
    direct = {r[0] for r in db.query(GroupMember.group_id).filter_by(user_id=user_id).all()}
    if not direct:
        cache[user_id] = set()
        return cache[user_id]
    parents = {gid: pid for gid, pid in db.query(UserGroup.id, UserGroup.parent_id).all()}
    out: set[int] = set()
    for gid in direct:
        cur, guard = gid, 0
        while cur and cur in parents and guard < 64:
            out.add(cur)
            cur, guard = parents[cur], guard + 1
    cache[user_id] = out
    return out


def _best_of(rows, content_of):
    """多条组授权取最强（同强度时管理员标记取并集）"""
    best = None
    for r in rows:
        if not _active(r.expires_at):
            continue
        c = content_of(r)
        rank = RANK.get(c, 0)
        if best is None or rank > best[0]:
            best = (rank, c, bool(r.is_admin), r.expires_at)
        elif rank == best[0] and bool(r.is_admin) and not best[2]:
            best = (rank, c, True, best[3] or r.expires_at)
    return None if best is None else (best[1], best[2], best[3])


def _group_folder_entry(db: Session, user_id: int, folder_id: int):
    gids = group_scope_ids(db, user_id)
    if not gids:
        return None
    rows = db.query(FolderGroupAcl).filter(
        FolderGroupAcl.folder_id == folder_id,
        FolderGroupAcl.group_id.in_(gids)).all()
    return _best_of(rows, lambda r: r.content)


def _group_doc_entry(db: Session, user_id: int, doc_id: int):
    gids = group_scope_ids(db, user_id)
    if not gids:
        return None
    rows = db.query(DocumentGroupAcl).filter(
        DocumentGroupAcl.document_id == doc_id,
        DocumentGroupAcl.group_id.in_(gids)).all()
    return _best_of(rows, lambda r: r.content or READ)


# ---------------- 解析入口 ----------------

def access_space(db: Session, user: User, space_id: int) -> Access:
    system = user.role == "admin"
    hit = _space_entry(db, user.id, space_id)
    if hit:
        return Access(hit[0], hit[1], system, "space")
    sp = db.get(Space, space_id)
    if sp and sp.owner_id == user.id:
        return Access(WRITE, True, system, "creator")
    if system:
        return Access(READ, True, system, "system")   # 兜底穿透读，不默认写内容
    # 被直接邀请到空间内某个目录/文档（非空间成员）：空间需在列表中可见才能进入，
    # 但仅此而已——不因此获得空间根的读写权限（根目录写权仍只属于空间成员）
    f_grant = (db.query(FolderAcl)
               .join(Folder, Folder.id == FolderAcl.folder_id)
               .filter(Folder.space_id == space_id, FolderAcl.user_id == user.id)
               .first())
    if f_grant and _active(f_grant.expires_at):
        return Access(READ, f_grant.is_admin, system, "grant")
    d_grant = (db.query(DocumentAcl)
               .join(Document, Document.id == DocumentAcl.document_id)
               .filter(Document.space_id == space_id, DocumentAcl.user_id == user.id)
               .first())
    if d_grant and _active(d_grant.expires_at):
        return Access(READ, d_grant.is_admin, system, "grant")
    # 同上，但授权对象是「用户组」：组员同样需要能看到该空间才能进到目录
    gids = group_scope_ids(db, user.id)
    if gids:
        fg = (db.query(FolderGroupAcl)
              .join(Folder, Folder.id == FolderGroupAcl.folder_id)
              .filter(Folder.space_id == space_id, FolderGroupAcl.group_id.in_(gids)).first())
        if fg and _active(fg.expires_at):
            return Access(READ, fg.is_admin, system, "group_grant")
        dg = (db.query(DocumentGroupAcl)
              .join(Document, Document.id == DocumentGroupAcl.document_id)
              .filter(Document.space_id == space_id, DocumentGroupAcl.group_id.in_(gids)).first())
        if dg and _active(dg.expires_at):
            return Access(READ, dg.is_admin, system, "group_grant")
    return Access(NONE, False, system, "none")


def access_folder(db: Session, user: User, folder: Folder | None,
                  space_id: int | None = None) -> Access:
    """目录权限：仅本目录的显式授权生效（不向子目录传递，避免"授父得子"的权限扩散）；
    未授权则回落到空间成员权限。私有目录对未受邀者彻底不可见。
    目录所有者（owner/创建者）在本目录内始终可写——"我的目录我做主"，
    且优先级高于空间成员身份，否则所有者会被空间层的继承覆盖掉。"""
    system = user.role == "admin"
    if folder is not None:
        hit = _folder_entry(db, user.id, folder.id)
        if hit:
            return Access(hit[0], hit[1], system, f"folder:{folder.id}",
                          elevated=bool(hit[2]), expires_at=hit[2])
        is_owner = user.id in (folder.owner_id, folder.created_by)
        if folder.is_private:
            if is_owner:
                return Access(WRITE, True, system, "folder_owner")
        elif is_owner:
            return Access(WRITE, True, system, "folder_owner")
        # 组授权：组内成员（含子组成员）等效于被逐个授权
        ghit = _group_folder_entry(db, user.id, folder.id)
        if ghit:
            return Access(ghit[0], ghit[1], system, f"group_folder:{folder.id}",
                          expires_at=ghit[2])
        if folder.is_private:
            if system:
                return Access(READ, True, system, "system")   # 管理员兜底穿透读，内容仍需提权
            return Access(NONE, False, system, "private")      # 私有目录：不继承空间权限

    sid = folder.space_id if folder is not None else space_id
    if sid:
        hit_s = _space_entry(db, user.id, sid)
        if hit_s:
            return Access(hit_s[0], hit_s[1], system, "space")
    if system:
        return Access(READ, True, system, "system")
    return Access(NONE, False, system, "none")


def access_doc(db: Session, user: User, doc: Document) -> Access:
    system = user.role == "admin"
    # 1) 文档级显式授权（含临时提权）优先
    hit = _doc_entry(db, user.id, doc.id)
    if hit:
        return Access(hit[0], hit[1], system, "document",
                      elevated=bool(hit[2]), expires_at=hit[2])
    # 1.5) 文档级「用户组」显式授权
    ghit = _group_doc_entry(db, user.id, doc.id)
    if ghit:
        return Access(ghit[0], ghit[1], system, "group_document", expires_at=ghit[2])
    # 2) 目录链 / 空间层继承
    folder = db.get(Folder, doc.folder_id) if doc.folder_id else None
    fa = access_folder(db, user, folder, space_id=doc.space_id)
    if fa.source != "none":
        content = fa.content
        # 写权降级只看"组织级"来源（空间成员/空间所有者/系统管理员兜底）：
        # 这两类管理员的职责是管人（邀请/移除/授权），不默认写他人内容。
        # 目录范围内的授权（目录所有者 / 目录 ACL / 目录的组授权）不降级——
        # "我的目录我做主"，否则连给目录内文档授写权都会被防提权拦住。
        folder_scoped = (fa.source.startswith("folder:")
                         or fa.source.startswith("group_folder:")
                         or fa.source == "folder_owner")
        if (content == WRITE and fa.is_admin and not folder_scoped
                and doc.created_by != user.id):
            content = READ
        return Access(content, fa.is_admin, system, fa.source,
                      elevated=fa.elevated, expires_at=fa.expires_at)
    # 3) 文档创建者
    if doc.created_by == user.id:
        return Access(WRITE, True, system, "creator")
    if system:
        return Access(READ, True, system, "system")
    return Access(NONE, False, system, "none")


# ---------------- 常用判定 ----------------

def can_read_doc(db, user, doc) -> bool:
    return access_doc(db, user, doc).can_read


def can_write_doc(db, user, doc) -> bool:
    return access_doc(db, user, doc).can_write


def can_manage_doc(db, user, doc) -> bool:
    return access_doc(db, user, doc).can_manage


def visible_folders(db: Session, user: User, space_id: int) -> list[Folder]:
    folders = db.query(Folder).filter_by(space_id=space_id).order_by(Folder.name).all()
    return [f for f in folders if access_folder(db, user, f).can_read]


def hidden_doc_ids(db: Session, user: User, space_id: int) -> set[int]:
    """落在「当前用户不可见目录」中的文档 id（列表/检索/回收站需剔除）"""
    folders = db.query(Folder).filter_by(space_id=space_id).all()
    hidden = [f.id for f in folders if not access_folder(db, user, f).can_read]
    if not hidden:
        return set()
    rows = db.query(Document.id).filter(
        Document.space_id == space_id, Document.folder_id.in_(hidden)).all()
    return {r[0] for r in rows}


# ---------------- 授权（不放大原则） ----------------

def grant(db: Session, actor: User, node_type: str, node_id: int,
          target_user_id: int | None = None, content: str | None = None,
          is_admin: bool | None = None, hours: int | None = None,
          reason: str | None = None, *, subject_type: str = "user",
          subject_id: int | None = None) -> dict:
    """给对象授权（被授权主体可以是用户或用户组）。约束：只有该对象的管理员（或系统管理员）
    能授权，且授出的内容权不得超过自己拥有的内容权；is_admin 只能由管理员授予。"""
    from fastapi import HTTPException

    if subject_type not in ("user", "group"):
        raise HTTPException(400, "被授权主体只能是 user / group")
    if node_type not in ("space", "folder", "document"):
        raise HTTPException(400, "非法对象类型")
    if content is not None and content not in (NONE, READ, WRITE):
        raise HTTPException(400, "内容权只能是 none / read / write")

    if node_type == "space":
        acc = access_space(db, actor, node_id)
    elif node_type == "folder":
        f = db.get(Folder, node_id)
        if not f:
            raise HTTPException(404, "目录不存在")
        acc = access_folder(db, actor, f)
    else:
        d = db.get(Document, node_id)
        if not d:
            raise HTTPException(404, "文档不存在")
        acc = access_doc(db, actor, d)

    if not acc.can_manage:
        raise HTTPException(403, "你不是该对象的管理员，无权授权")
    # 权限不放大：非系统管理员只能授出自己拥有的内容级别
    if content is not None and not acc.is_system_admin:
        if RANK[content] > RANK[acc.content]:
            raise HTTPException(403, "不能授出高于自身的内容权限")
    # 禁止成员管理员转授"管理员"身份：只有系统管理员或该空间的空间管理员可以
    if is_admin and not acc.is_system_admin:
        sid = None
        if node_type == "space":
            sid = node_id
        elif node_type == "folder":
            f = db.get(Folder, node_id)
            sid = f.space_id if f else None
        else:
            d = db.get(Document, node_id)
            sid = d.space_id if d else None
        space_acc = access_space(db, actor, sid) if sid else None
        if not (space_acc and space_acc.is_admin):
            raise HTTPException(403, "成员管理员不能把他人设为管理员，需由空间管理员授予")

    if subject_type == "group":
        if node_type == "space":
            raise HTTPException(400, "空间成员请按用户授权；组授权适用于目录与文档")
        if not subject_id:
            raise HTTPException(400, "缺少用户组")
        if not db.get(UserGroup, subject_id):
            raise HTTPException(404, "用户组不存在")
        target_user_id = None
    else:
        target = db.get(User, target_user_id) if target_user_id else None
        if not target:
            raise HTTPException(404, "目标用户不存在")
    expires = dt.datetime.utcnow() + dt.timedelta(hours=hours) if hours else None

    if node_type == "space":
        m = db.query(SpaceMember).filter_by(space_id=node_id, user_id=target_user_id).first()
        if not m:
            m = SpaceMember(space_id=node_id, user_id=target_user_id, content=READ)
            db.add(m)
        if content is not None:
            m.content = content
        if is_admin is not None:
            m.is_admin = bool(is_admin)
    elif node_type == "folder":
        if content == NONE:
            raise HTTPException(400, "目录内容权只支持 read / write（收回权限请用撤销接口）")
        if subject_type == "group":
            a = db.query(FolderGroupAcl).filter_by(folder_id=node_id, group_id=subject_id).first()
            if not a:
                a = FolderGroupAcl(folder_id=node_id, group_id=subject_id, content=READ)
                db.add(a)
            if content is not None:
                a.content = content
            if is_admin is not None:
                a.is_admin = bool(is_admin)
            a.expires_at = expires
            a.granted_by = actor.id
        else:
            a = db.query(FolderAcl).filter_by(folder_id=node_id, user_id=target_user_id).first()
            if not a:
                a = FolderAcl(folder_id=node_id, user_id=target_user_id, content=READ)
                db.add(a)
            if content is not None:
                a.content = content
            if is_admin is not None:
                a.is_admin = bool(is_admin)
            a.expires_at = expires
            a.granted_by = actor.id
    else:
        if subject_type == "group":
            a = db.query(DocumentGroupAcl).filter_by(document_id=node_id, group_id=subject_id).first()
            if not a:
                a = DocumentGroupAcl(document_id=node_id, group_id=subject_id, content=READ)
                db.add(a)
            if content is not None:
                a.content = content
            if is_admin is not None:
                a.is_admin = bool(is_admin)
            a.expires_at = expires
            a.granted_by = actor.id
        else:
            a = db.query(DocumentAcl).filter_by(document_id=node_id, user_id=target_user_id).first()
            if not a:
                a = DocumentAcl(document_id=node_id, user_id=target_user_id, content=READ)
                db.add(a)
            if content is not None:
                a.content = content
            if is_admin is not None:
                a.is_admin = bool(is_admin)
            a.expires_at = expires
            a.granted_by = actor.id

    db.commit()
    return {"ok": True, "node_type": node_type, "node_id": node_id,
            "subject_type": subject_type,
            "subject_id": subject_id if subject_type == "group" else target_user_id,
            "user_id": target_user_id, "content": content, "is_admin": is_admin,
            "expires_at": expires, "reason": reason}


def revoke(db: Session, actor: User, node_type: str, node_id: int,
           target_user_id: int | None = None, *, subject_type: str = "user",
           subject_id: int | None = None) -> dict:
    from fastapi import HTTPException

    if node_type == "space":
        acc = access_space(db, actor, node_id)
    elif node_type == "folder":
        f = db.get(Folder, node_id)
        acc = access_folder(db, actor, f) if f else Access()
    else:
        d = db.get(Document, node_id)
        acc = access_doc(db, actor, d) if d else Access()
    if not acc.can_manage:
        raise HTTPException(403, "无权撤销该对象的授权")

    if node_type == "space":
        m = db.query(SpaceMember).filter_by(space_id=node_id, user_id=target_user_id).first()
        if m and m.space_id:
            # 空间成员移除 = 删除成员关系
            db.delete(m)
        db.commit()
        return {"ok": True}

    if subject_type == "group":
        if node_type == "folder":
            row = db.query(FolderGroupAcl).filter_by(folder_id=node_id, group_id=subject_id).first()
        else:
            row = db.query(DocumentGroupAcl).filter_by(document_id=node_id, group_id=subject_id).first()
    else:
        model = FolderAcl if node_type == "folder" else DocumentAcl
        key = "folder_id" if node_type == "folder" else "document_id"
        row = db.query(model).filter_by(**{key: node_id}, user_id=target_user_id).first()
    if row:
        db.delete(row)
        db.commit()
    return {"ok": True}
