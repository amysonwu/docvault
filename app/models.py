# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 DocVault Contributors
# Author: Amyson Wu
"""ORM 模型：全部业务表与存储元数据表"""
import datetime as dt

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Enum, ForeignKey, Index, Integer, String, Text, JSON,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

NOW = dt.datetime.utcnow


def utcnow():
    return NOW()


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(64), default="")
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(Enum("admin", "member", name="user_role"), default="member")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Space(Base):
    __tablename__ = "spaces"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(String(512), default="")
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    quota_bytes: Mapped[int] = mapped_column(BigInteger, default=0)  # 0 = 不限
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class SpaceMember(Base):
    __tablename__ = "space_members"
    __table_args__ = (Index("ux_space_member", "space_id", "user_id", unique=True),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    # 内容权（none/read/write，write⊇read）+ 是否对象管理员（管人，不含内容写权）
    content: Mapped[str] = mapped_column(Enum("none", "read", "write", name="content_perm"),
                                         default="read")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)



class Folder(Base):
    __tablename__ = "folders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("folders.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(255))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    # 目录级权限：默认继承空间权限；设为私有后仅所有者与受邀人可见
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    is_private: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class FolderAcl(Base):
    """目录级授权：read=查看下载，write=上传/编辑；is_admin=该目录的成员管理员（管人不管内容）"""
    __tablename__ = "folder_acls"
    __table_args__ = (Index("ux_folder_acl", "folder_id", "user_id", unique=True),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    folder_id: Mapped[int] = mapped_column(ForeignKey("folders.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    content: Mapped[str] = mapped_column(Enum("read", "write", name="folder_perm"), default="read")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)      # 成员管理员
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)  # 临时提权到期
    granted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class UserGroup(Base):
    """用户组：多级树（parent_id 自引用）。
    授权给某组 = 该组及所有子组成员都获得权限（组织树向下继承），
    一个用户可以同时属于多个组。"""
    __tablename__ = "user_groups"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("user_groups.id"), nullable=True, index=True)
    description: Mapped[str] = mapped_column(String(512), default="")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class GroupMember(Base):
    """组成员（多对多：一个用户可在多个组）"""
    __tablename__ = "group_members"
    __table_args__ = (Index("ux_group_member", "group_id", "user_id", unique=True),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("user_groups.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class FolderGroupAcl(Base):
    """目录授权给用户组：组成员（含子组成员）等效于被逐个授权"""
    __tablename__ = "folder_group_acls"
    __table_args__ = (Index("ux_folder_group_acl", "folder_id", "group_id", unique=True),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    folder_id: Mapped[int] = mapped_column(ForeignKey("folders.id"), index=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("user_groups.id"), index=True)
    content: Mapped[str] = mapped_column(Enum("read", "write", name="folder_group_perm"), default="read")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    granted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class DocumentGroupAcl(Base):
    """文档授权给用户组"""
    __tablename__ = "document_group_acls"
    __table_args__ = (Index("ux_doc_group_acl", "document_id", "group_id", unique=True),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("user_groups.id"), index=True)
    content: Mapped[str] = mapped_column(Enum("none", "read", "write", name="doc_group_perm"),
                                        default="read")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    granted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"), index=True)
    folder_id: Mapped[int | None] = mapped_column(ForeignKey("folders.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(255))                # 不含扩展名
    ext: Mapped[str] = mapped_column(String(16))                  # 库内标准后缀（如 .docx）
    original_ext: Mapped[str | None] = mapped_column(String(16), nullable=True)  # 转换前原件后缀（.doc 等）
    content_text: Mapped[str | None] = mapped_column(Text, nullable=True)        # 正文提取，ngram 全文索引
    status: Mapped[str] = mapped_column(Enum("active", "trashed", name="doc_status"), default="active")
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # 乐观编辑锁：谁在编辑 + 心跳过期自动失效
    lock_owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    lock_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    versions: Mapped[list["DocumentVersion"]] = relationship(
        back_populates="document", order_by="DocumentVersion.seq", cascade="all, delete-orphan"
    )


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (Index("ux_doc_seq", "document_id", "seq", unique=True),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)                      # v1, v2, ...
    blob_hash: Mapped[str] = mapped_column(String(64))             # CAS 内容哈希（当前标准格式内容）
    original_blob_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 转换场景保留原件
    ext: Mapped[str] = mapped_column(String(16))
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    note: Mapped[str] = mapped_column(String(512), default="")     # 版本说明
    # 来源：upload 上传 / edit 在线编辑 / rollback 回滚 / offline_upload 离线回传 / restore 回收站恢复
    source: Mapped[str] = mapped_column(
        Enum("upload", "edit", "rollback", "offline_upload", "restore", "create", "edit_online", name="version_source"), default="upload"
    )
    source_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 如 "回滚自 v3" / "离线回传自 v2"
    change_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)   # 变更摘要 +N/-M
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    document: Mapped[Document] = relationship(back_populates="versions")


class VersionTag(Base):
    """版本标签：标签指向「某文档的某个版本」，用法类似 git tag。
    同一版本可挂多个标签；同一版本内同名标签只允许一条（unique）。"""
    __tablename__ = "version_tags"
    __table_args__ = (Index("ux_tag", "document_id", "name"),
                      Index("ux_ver_tag", "version_id", "name", unique=True),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("document_versions.id"))
    name: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class DocumentAcl(Base):
    """文档级授权：content=read/write，is_admin=该文档的成员管理员；expires_at 用于临时提权"""
    __tablename__ = "document_acls"
    __table_args__ = (Index("ux_doc_acl", "document_id", "user_id", unique=True),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    content: Mapped[str] = mapped_column(Enum("none", "read", "write", name="content_perm"),
                                        default="read")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    granted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class OfflineTask(Base):
    """离线编辑任务：绑定下载人/基线版本，回传时自动关联比对"""
    __tablename__ = "offline_tasks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    base_version_id: Mapped[int] = mapped_column(ForeignKey("document_versions.id"))
    token: Mapped[str] = mapped_column(String(48), unique=True)    # 写入文件元数据 / 手动关联用
    status: Mapped[str] = mapped_column(
        Enum("pending", "returned", "expired", name="offline_status"), default="pending"
    )
    returned_version_id: Mapped[int | None] = mapped_column(ForeignKey("document_versions.id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    returned_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class ShareLink(Base):
    """站外分享：密码 + 有效期 + 仅预览"""
    __tablename__ = "share_links"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    token: Mapped[str] = mapped_column(String(48), unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(256), nullable=True)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    view_only: Mapped[bool] = mapped_column(Boolean, default=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    """操作审计：登录/上传/编辑/下载/分享/权限/回滚等，保留≥1年"""
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)   # login / upload / download / commit / rollback / ...
    target_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)


class StorageManifest(Base):
    """版本内容清单：一份文件内容 = 一行（块级 CAS）"""
    __tablename__ = "storage_manifests"
    file_hash: Mapped[str] = mapped_column(String(64), primary_key=True)  # 清单哈希（raw = 整文件 SHA-256）
    kind: Mapped[str] = mapped_column(String(8))          # raw / office / chunks
    n_items: Mapped[int] = mapped_column(Integer)
    bytes_total: Mapped[int] = mapped_column(BigInteger)  # 逻辑总大小（= version.size）
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class StorageManifestItem(Base):
    """清单项：有序部件（office）或有序块（chunks），拼接/重组即原文"""
    __tablename__ = "storage_manifest_items"
    file_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)   # 重组顺序
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)  # office 部件路径；chunks 为 NULL
    item_hash: Mapped[str] = mapped_column(String(64), index=True)
    item_size: Mapped[int] = mapped_column(BigInteger)


class StorageChunk(Base):
    """块库：全局共享、按内容寻址，refcount=引用块数（GC 依据）"""
    __tablename__ = "storage_chunks"
    hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    size: Mapped[int] = mapped_column(BigInteger)
    refcount: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
