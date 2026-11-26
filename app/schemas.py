# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 DocVault Contributors
# Author: Amyson Wu
"""Pydantic DTO"""
import datetime as dt

from pydantic import BaseModel


class RegisterIn(BaseModel):
    username: str
    password: str
    display_name: str = ""


class LoginIn(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    id: int
    username: str
    display_name: str
    role: str

    class Config:
        from_attributes = True


class SpaceIn(BaseModel):
    name: str
    description: str = ""


class SpaceUpdateIn(BaseModel):
    name: str | None = None
    description: str | None = None


class FolderIn(BaseModel):
    name: str
    parent_id: int | None = None


class MemberIn(BaseModel):
    user_id: int
    content: str | None = None    # none / read / write（write 自动含读）
    is_admin: bool | None = None  # 对象管理员（管人，不隐含写权）


class CommitIn(BaseModel):
    note: str


class TagIn(BaseModel):
    name: str
    version_id: int


class ShareIn(BaseModel):
    password: str | None = None
    expires_hours: int | None = None   # None = 永久；N 小时有效
    view_only: bool = True


class OfflineReturnIn(BaseModel):
    task_token: str | None = None      # 自动识别失败时手动关联


class ConfirmOfflineIn(BaseModel):
    note: str | None = None
    force_replace: bool = False        # 比对失败兜底：整文件替换提交


class CreateDocIn(BaseModel):
    name: str
    ext: str = ".docx"                 # 在线新建仅支持 .md/.txt/.docx
    folder_id: int | None = None


class RenameDocIn(BaseModel):
    name: str                                  # 仅主名，不含扩展名


class SaveTextIn(BaseModel):
    content: str
    note: str = "在线编辑"


class VersionOut(BaseModel):
    id: int
    seq: int
    size: int
    note: str
    source: str
    source_ref: str | None
    change_summary: dict | None
    created_by: int
    creator_name: str = ""
    created_at: dt.datetime
    tags: list[str] = []


class DocumentOut(BaseModel):
    id: int
    name: str
    ext: str
    original_ext: str | None
    folder_id: int | None
    status: str
    size: int
    latest_seq: int | None = None
    lock_owner_id: int | None = None
    lock_owner_name: str | None = None
    updated_at: dt.datetime
    created_by: int


class FolderAclIn(BaseModel):
    user_id: int
    content: str = "read"       # read / write
    is_admin: bool | None = None  # 设为该目录的成员管理员


class FolderUpdateIn(BaseModel):
    is_private: bool | None = None
    name: str | None = None
    parent_id: int | None = None   # 移动到指定父目录
    move_to_root: bool | None = None  # 移到空间根（与顶级目录同级）
