# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 DocVault Contributors
# Author: Amyson Wu
"""认证路由：注册 / 登录 / 当前用户"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..audit import log
from ..db import get_db
from ..models import User
from ..schemas import LoginIn, RegisterIn, UserOut
from ..security import create_token, hash_password, verify_password
from ..deps import get_current_user

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", response_model=UserOut)
def register(data: RegisterIn, request: Request, db: Session = Depends(get_db)):
    if not data.username.strip() or len(data.password) < 6:
        raise HTTPException(400, "用户名不能为空，密码至少 6 位")
    if db.query(User).filter_by(username=data.username).first():
        raise HTTPException(409, "用户名已存在")
    first_user = db.query(User).count() == 0  # 首个用户即系统管理员
    user = User(
        username=data.username.strip(),
        display_name=data.display_name or data.username,
        password_hash=hash_password(data.password),
        role="admin" if first_user else "member",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    log(db, user.id, "register", request, "user", user.id)
    return user


@router.post("/login")
def login(data: LoginIn, request: Request, db: Session = Depends(get_db)):
    user = db.query(User).filter_by(username=data.username).first()
    if not user or not verify_password(data.password, user.password_hash):
        raise HTTPException(401, "用户名或密码错误")
    if not user.is_active:
        raise HTTPException(403, "账号已停用")
    log(db, user.id, "login", request, "user", user.id)
    return {"token": create_token(user.id, user.username), "user": UserOut.model_validate(user)}


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user


@router.get("/users", response_model=list[UserOut])
def list_users(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return db.query(User).filter(User.is_active == True).all()  # noqa: E712
