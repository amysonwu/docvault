# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 DocVault Contributors
# Author: Amyson Wu
"""站外分享链接：密码 / 有效期 / 仅预览"""
import datetime as dt
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..deps import get_current_user, require_doc_perm
from ..models import DocumentVersion, ShareLink, User
from ..security import hash_password, verify_password
from ..storage import storage

router = APIRouter(prefix="/api/shares", tags=["shares"])


def _require_share_right(doc_user, db: Session):
    """分享 = 把内容发到站外，属于有数据外流风险的操作：
    要求「内容写权限」或「对象管理权限」，只读用户不得对外分享。
    （系统管理员默认只读，需先 JIT 提权，且提权会留痕）"""
    from .. import authz
    doc, user = doc_user
    acc = authz.access_doc(db, user, doc)
    if not (acc.can_write or acc.can_manage):
        raise HTTPException(403, "无权限生成分享链接：需要该文档的写权限或管理权限（只读用户不可对外分享）")
    return doc, user


@router.post("/{document_id}")
def create_share(document_id: int, password: str | None = None, expires_hours: int | None = None,
                 view_only: bool = True,
                 doc_user=Depends(require_doc_perm("read", content_required=True)),
                 db: Session = Depends(get_db), request: Request = None):
    doc, user = _require_share_right(doc_user, db)

    # 去重：同文档、同权限/密码/有效期口径、未撤销且未过期 → 复用现有链接，避免每次点击都新增记录
    now = dt.datetime.utcnow()
    for old in db.query(ShareLink).filter_by(
            document_id=document_id, revoked=False, view_only=view_only).all():
        if old.expires_at and old.expires_at < now:
            continue
        if bool(old.password_hash) != bool(password):
            continue
        if password and not verify_password(password, old.password_hash):
            continue
        # 有效期口径一致：永久对永久，限时对限时
        if bool(old.expires_at) != bool(expires_hours):
            continue
        return {"token": old.token, "url": f"/share/{old.token}",
                "expires_at": old.expires_at, "view_only": old.view_only, "reused": True}

    link = ShareLink(
        document_id=document_id,
        token=secrets.token_urlsafe(24),
        password_hash=hash_password(password) if password else None,
        expires_at=dt.datetime.utcnow() + dt.timedelta(hours=expires_hours) if expires_hours else None,
        view_only=view_only,
        created_by=user.id,
    )
    db.add(link)
    db.commit()
    db.refresh(link)
    audit.log(db, user.id, "share_create", request, "document", document_id,
              {"expires_hours": expires_hours, "has_password": bool(password)})
    return {"token": link.token, "url": f"/share/{link.token}",
            "expires_at": link.expires_at, "view_only": link.view_only, "reused": False}


@router.get("/{document_id}/list")
def list_shares(document_id: int, doc_user=Depends(require_doc_perm("read")),
                db: Session = Depends(get_db)):
    doc, _ = doc_user
    links = db.query(ShareLink).filter_by(document_id=document_id, revoked=False).all()
    return [{"token": l.token, "expires_at": l.expires_at, "view_only": l.view_only,
             "has_password": bool(l.password_hash)} for l in links]


@router.delete("/{document_id}/{token}")
def revoke_share(document_id: int, token: str,
                 doc_user=Depends(require_doc_perm("read", content_required=False)),
                 db: Session = Depends(get_db)):
    doc, _ = _require_share_right(doc_user, db)
    link = db.query(ShareLink).filter_by(token=token, document_id=document_id).first()
    if not link:
        raise HTTPException(404, "分享不存在")
    link.revoked = True
    db.commit()
    return {"ok": True}


# ---- 访客访问（无需登录） ----
guest = APIRouter(prefix="/api/guest", tags=["guest"])


def _valid_link(db: Session, token: str) -> ShareLink:
    link = db.query(ShareLink).filter_by(token=token, revoked=False).first()
    if not link:
        raise HTTPException(404, "分享不存在或已撤销")
    if link.expires_at and link.expires_at < dt.datetime.utcnow():
        raise HTTPException(410, "分享链接已过期失效")
    return link


@guest.get("/{token}/info")
def guest_info(token: str, db: Session = Depends(get_db)):
    from ..models import Document
    link = _valid_link(db, token)
    doc = db.get(Document, link.document_id)
    return {"name": f"{doc.name}{doc.ext}", "has_password": bool(link.password_hash),
            "view_only": link.view_only}


@guest.post("/{token}/open")
def guest_open(token: str, password: str | None = None, db: Session = Depends(get_db)):
    """校验密码后返回文档列表数据（仅预览）"""
    link = _valid_link(db, token)
    if link.password_hash and not verify_password(password or "", link.password_hash):
        raise HTTPException(401, "分享密码错误")
    from ..models import Document
    d = db.get(Document, link.document_id)
    vers = db.query(DocumentVersion).filter_by(document_id=d.id)\
        .order_by(DocumentVersion.seq.desc()).first()
    return {"name": f"{d.name}{d.ext}", "latest_seq": vers.seq if vers else None,
            "view_only": link.view_only}


@guest.get("/{token}/content")
def guest_content(token: str, password: str | None = None, db: Session = Depends(get_db)):
    """访客内容预览：返回结构化 HTML（docx/xlsx/pptx/pdf）或纯文本（md/txt）"""
    from ..extract import extract_preview
    from ..models import Document
    link = _valid_link(db, token)
    if link.password_hash and not verify_password(password or "", link.password_hash):
        raise HTTPException(401, "分享密码错误")
    doc = db.get(Document, link.document_id)
    ver = db.query(DocumentVersion).filter_by(document_id=doc.id)\
        .order_by(DocumentVersion.seq.desc()).first()
    if not ver:
        raise HTTPException(404, "文档暂无内容版本")
    res = extract_preview(storage.path(ver.blob_hash), doc.ext)
    if res is None:
        raise HTTPException(415, f"{doc.ext} 格式暂不支持在线预览")
    return {"name": f"{doc.name}{doc.ext}", "seq": ver.seq,
            "view_only": link.view_only, **res}


@guest.get("/{token}/render")
def guest_render(token: str, password: str | None = None, db: Session = Depends(get_db)):
    """访客保真预览：Office/PDF → PDF 直出（浏览器原生渲染），图片 → 原图"""
    from ..models import Document
    link = _valid_link(db, token)
    if link.password_hash and not verify_password(password or "", link.password_hash):
        raise HTTPException(401, "分享密码错误")
    doc = db.get(Document, link.document_id)
    ver = db.query(DocumentVersion).filter_by(document_id=doc.id)\
        .order_by(DocumentVersion.seq.desc()).first()
    if not ver:
        raise HTTPException(404, "文档暂无内容版本")
    ext = (doc.ext or "").lower()
    path = storage.path(ver.blob_hash)
    if ext in (".png", ".jpg", ".jpeg", ".gif"):
        mime = {"png": "image/png", "jpg": "image/jpeg",
                "jpeg": "image/jpeg", "gif": "image/gif"}[ext.lstrip(".")]
        return FileResponse(path, media_type=mime)
    from ..office_convert import ensure_pdf
    pdf = ensure_pdf(ver.blob_hash, ext)
    if pdf is None:
        raise HTTPException(415, f"{ext} 暂时无法生成保真预览")
    return FileResponse(pdf, media_type="application/pdf",
                        headers={"Content-Disposition": "inline",
                                 "Cache-Control": "private, max-age=120"})


@guest.get("/{token}/download")
def guest_download(token: str, password: str | None = None, db: Session = Depends(get_db),
                   request: Request = None):
    link = _valid_link(db, token)
    if link.password_hash and not verify_password(password or "", link.password_hash):
        raise HTTPException(401, "分享密码错误")
    if link.view_only:
        raise HTTPException(403, "该分享为仅预览模式，禁止下载")
    from ..models import Document
    doc = db.get(Document, link.document_id)
    ver = db.query(DocumentVersion).filter_by(document_id=doc.id)\
        .order_by(DocumentVersion.seq.desc()).first()
    audit.log(db, None, "guest_download", request, "document", doc.id, {"token": token})
    return FileResponse(storage.path(ver.blob_hash),
                        filename=f"{doc.name}{doc.ext}", media_type="application/octet-stream")
