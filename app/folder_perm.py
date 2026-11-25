# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 DocVault Contributors
# Author: Amyson Wu
"""目录级权限（兼容层，实际判定统一在 authz.py）

- 私有目录：仅创建者与受邀者可见，且不向上继承空间权限
- 就近覆盖：目录显式授权优先于祖先与空间
- 成员管理员：is_admin 只代表"管人"，不隐含内容写权
"""
from sqlalchemy.orm import Session

from . import authz
from .models import Document, Folder, User

READ, WRITE = authz.READ, authz.WRITE


def folder_perm(db: Session, user: User, folder: Folder | None) -> str | None:
    """返回 read / write / None（不可见）"""
    acc = authz.access_folder(db, user, folder)
    return acc.content if acc.can_read else None


def can_read(db: Session, user: User, folder: Folder | None) -> bool:
    return authz.access_folder(db, user, folder).can_read


def can_write(db: Session, user: User, folder: Folder | None) -> bool:
    return authz.access_folder(db, user, folder).can_write


def visible_folders(db: Session, user: User, space_id: int) -> list[Folder]:
    return authz.visible_folders(db, user, space_id)


def doc_visible(db: Session, user: User, doc: Document) -> bool:
    return authz.access_doc(db, user, doc).can_read


def hidden_doc_ids(db: Session, user: User, space_id: int) -> set[int]:
    return authz.hidden_doc_ids(db, user, space_id)
