# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 DocVault Contributors
# Author: Amyson Wu
"""编辑内核适配层（预留接口）

预留接口：OnlyOffice / WPS WebOffice 可插拔替换。
其他在线编辑内核（如 WPS WebOffice）可按同一协议接入。
"""
from abc import ABC, abstractmethod


class EditorAdapter(ABC):
    """在线编辑内核适配器协议"""

    @abstractmethod
    def build_config(self, doc, version, user, base_url: str) -> dict:
        """生成编辑器初始化配置（document.url 指向 /api/documents/{id}/versions/{seq}/download）"""

    @abstractmethod
    def parse_callback(self, payload: dict) -> dict:
        """解析编辑器保存回调 → (临时文件, 用户ID, 是否强制) 供 commit_version 使用"""

    @abstractmethod
    def capabilities(self) -> dict:
        """返回内核能力：co_editing / track_changes / pdf_annotate 等"""


class OnlyOfficeAdapter(EditorAdapter):
    """OnlyOffice Document Server 适配器（对接外部 OnlyOffice 服务）"""

    def __init__(self, server_url: str = "http://127.0.0.1:13082", jwt_secret: str = ""):
        self.server_url = server_url
        self.jwt_secret = jwt_secret

    def build_config(self, doc, version, user, base_url: str) -> dict:
        return {
            "document": {
                "fileType": version.ext.lstrip("."),
                "key": f"doc{doc.id}-v{version.seq}",
                "title": f"{doc.name}{doc.ext}",
                "url": f"{base_url}/api/documents/{doc.id}/versions/{version.seq}/download",
            },
            "editorConfig": {"callbackUrl": f"{base_url}/api/documents/{doc.id}/onlyoffice/callback",
                             "user": {"id": user.id, "name": user.display_name}},
        }

    def parse_callback(self, payload: dict) -> dict:
        return {"status": payload.get("status"), "url": payload.get("url")}

    def capabilities(self) -> dict:
        return {"co_editing": True, "track_changes": True, "pdf_annotate": False}


_REGISTRY = {"onlyoffice": None}  # 延迟注册，避免未实现时 import 失败


def get_adapter(name: str) -> EditorAdapter | None:
    if name == "onlyoffice":
        return OnlyOfficeAdapter()
    return None
