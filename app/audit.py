# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 DocVault Contributors
# Author: Amyson Wu
"""审计日志"""
from fastapi import Request
from sqlalchemy.orm import Session

from .models import AuditLog


def log(db: Session, user_id: int | None, action: str, request: Request | None = None,
        target_type: str | None = None, target_id: int | None = None, detail: dict | None = None):
    ip = None
    if request is not None:
        ip = request.client.host if request.client else request.headers.get("x-forwarded-for")
    db.add(AuditLog(user_id=user_id, action=action, target_type=target_type,
                    target_id=target_id, detail=detail, ip=ip))
    db.commit()
