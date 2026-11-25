# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 DocVault Contributors
# Author: Amyson Wu
"""认证与权限依赖（权限内核见 authz.py）"""
import datetime as dt

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from . import authz
from .db import get_db
from .models import Document, Folder, User
from .security import decode_token

# 校验动作词汇：read（读）/ write（写，含读）/ manage（成员管理）


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    auth = request.headers.get("Authorization", "")
    # 依次尝试：header Bearer token → query token。
    # 注意：OnlyOffice DS 取文件时会携带它自己的 JWT header（不同密钥），
    # 因此 header 验证失败必须回退到 query token，而不是直接 401。
    header_token = auth[7:] if auth.startswith("Bearer ") else ""
    for token in (header_token, request.query_params.get("token", "")):
        payload = decode_token(token) if token else None
        if not payload:
            continue
        user = db.get(User, int(payload["sub"]))
        if user and user.is_active:
            return user
    raise HTTPException(401, "未登录或凭证失效")


def allows(acc: authz.Access, action: str) -> bool:
    if action == "read":
        return acc.can_read
    if action == "write":
        return acc.can_write
    if action == "manage":
        return acc.can_manage
    return False


def space_perm_checker(min_perm: str):
    """空间级权限校验：read / write（内容）/ manage（成员管理）"""

    def checker(space_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
        acc = authz.access_space(db, user, space_id)
        if not allows(acc, min_perm):
            raise HTTPException(403, "无权限执行该操作")
        return user

    return checker


def doc_perm_checker(min_perm: str, allow_trashed: bool = False,
                     content_required: bool = False):
    """文档级权限：沿 文档→目录→空间 就近覆盖判定
    allow_trashed：回收站操作（恢复）放宽 active 校验
    content_required：访问正文/下载等「内容」时，系统管理员必须走临时提权（JIT），防止默默穿透"""

    def checker(
        document_id: int,
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ):
        doc = db.get(Document, document_id)
        if not doc or (doc.status != "active" and not allow_trashed):
            raise HTTPException(404, "文档不存在")
        acc = authz.access_doc(db, user, doc)
        # 私有目录未受邀：按不存在处理，避免泄露存在性
        if acc.source == "private":
            raise HTTPException(404, "文档不存在")
        if not allows(acc, min_perm):
            if min_perm == "write" and acc.content == authz.READ:
                raise HTTPException(403, "你对该文档只有查看权限，需要写权限才能修改")
            raise HTTPException(403, "无权限执行该操作")
        # 系统管理员默认不介入内容：读列表元数据可以，读正文/下载必须临时提权
        if content_required and acc.is_system_admin and not acc.elevated and acc.source == "system":
            raise HTTPException(403, "系统管理员默认不可查看业务内容，请先申请临时访问权限（JIT）")
        # 并发写由版本链 + 基线 diff 收敛，不做写前拒绝
        return doc, user

    return checker


def folder_perm_checker(min_perm: str):
    """目录级权限校验（folder_id 路径参数）"""

    def checker(folder_id: int, user: User = Depends(get_current_user),
                db: Session = Depends(get_db)):
        f = db.get(Folder, folder_id)
        if not f:
            raise HTTPException(404, "目录不存在")
        acc = authz.access_folder(db, user, f)
        if acc.source == "private":
            raise HTTPException(404, "目录不存在")
        if not allows(acc, min_perm):
            raise HTTPException(403, "无权限执行该操作")
        return f, user

    return checker


def system_admin_only(user: User = Depends(get_current_user)) -> User:
    """仅系统管理员：删除 / 改名 / 移动等结构操作"""
    if user.role != "admin":
        raise HTTPException(403, "该操作仅系统管理员可执行")
    return user


# 兼容别名：路由处统一使用 Depends(require_space_perm(...)) / Depends(require_doc_perm(...))
require_space_perm = space_perm_checker
require_doc_perm = doc_perm_checker
require_folder_perm = folder_perm_checker
