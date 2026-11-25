# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 DocVault Contributors
# Author: Amyson Wu
"""环境配置（可配置项）"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DOCVAULT_DATA_DIR", BASE_DIR / "data"))
BLOB_DIR = DATA_DIR / "blobs"
CACHE_DIR = DATA_DIR / "cache"       # 版本重组缓存（可随时清空）

# ---- 存储去重（块级 CAS）----
DEDUP_MODE = os.environ.get("DOCVAULT_DEDUP", "office")   # off / office / all
CACHE_MAX_GB = float(os.environ.get("DOCVAULT_CACHE_GB", "4"))   # 重组缓存配额
CDC_MAX_MB = int(os.environ.get("DOCVAULT_CDC_MAX_MB", "64"))    # 超过此大小不切块（整文件）
ZSTD_LEVEL = max(1, int(os.environ.get("DOCVAULT_ZSTD_LEVEL", "3")))  # 块落盘压缩等级（1 最快，越大压缩比越高）

# ---- 数据库（MySQL 8 兼容库）----
DB_HOST = os.environ.get("DOCVault_DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("DOCVault_DB_PORT", "3306"))
DB_USER = os.environ.get("DOCVault_DB_USER", "docvault")
DB_PASS = os.environ.get("DOCVault_DB_PASS", "DocVault_2026")
DB_NAME = os.environ.get("DOCVault_DB_NAME", "docvault")
DATABASE_URL = f"mysql+pymysql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}?charset=utf8mb4"

# ---- 安全 ----
JWT_SECRET = os.environ.get("DOCVault_JWT_SECRET", "dev-only-change-me-docvault-jwt-secret-2026")
JWT_ALG = "HS256"
JWT_EXPIRE_HOURS = int(os.environ.get("DOCVault_JWT_EXPIRE_HOURS", "24"))

# ---- 上传限制（单文件 ≤500MB 可配置）----
MAX_UPLOAD_MB = int(os.environ.get("DOCVault_MAX_UPLOAD_MB", "500"))

# ---- 编辑锁（乐观锁：心跳超时自动释放）----
LOCK_TTL_SECONDS = int(os.environ.get("DOCVault_LOCK_TTL", "300"))

# ---- 回收站（30 天可恢复）/ 离线任务超期----
TRASH_RETENTION_DAYS = 30
OFFLINE_TASK_EXPIRE_DAYS = 30

# ---- 支持的格式----
NATIVE_EXTS = {".docx", ".pptx", ".xlsx", ".pdf"}          # 直接入库
CONVERTIBLE_EXTS = {".doc", ".ppt"}                        # 上传后转 OOXML（当前保留原件并标记待转换）
PREVIEW_EXTS = {".docx", ".pptx", ".xlsx", ".pdf", ".doc", ".ppt", ".txt", ".md", ".png", ".jpg", ".jpeg", ".gif"}
TEXT_INDEX_EXTS = {".docx", ".pdf", ".txt", ".md", ".pptx", ".xlsx",
                   ".doc", ".ppt", ".xls", ".rtf", ".odt", ".odp", ".ods"}

# ---- OnlyOffice 在线编辑 ----
OO_DS_URL = os.environ.get("DOCVault_OO_URL", "http://127.0.0.1:13082")          # 浏览器侧加载 api.js（推荐走同源反代）
# 服务端（后端进程）调用 DS 的地址：容器内 127.0.0.1 指向容器自己，必须用宿主可达地址
OO_DS_SERVER_URL = os.environ.get("DOCVault_OO_SERVER", OO_DS_URL)
OO_DS_INTERNAL_URL = os.environ.get("DOCVault_OO_INTERNAL", "http://host.docker.internal:8000")  # OO 容器回访 DocVault 用（须与 WEB 端口一致）
OO_DS_JWT_SECRET = os.environ.get("DOCVault_OO_SECRET",
                                  "change-me-oo-secret")  # 必须与 OnlyOffice 容器的 JWT_SECRET 一致，生产环境务必通过环境变量覆盖
