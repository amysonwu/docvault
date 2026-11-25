# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 DocVault Contributors
# Author: Amyson Wu
"""OnlyOffice Document Server 在线编辑集成
参照 OnlyOffice 官方集成协议：config 签名下发 → DS 回访取文件 → 编辑完成回调提交新版本。

"""
import pathlib
import time
import urllib.parse
import urllib.request

import jwt as pyjwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import audit
from ..config import OO_DS_INTERNAL_URL, OO_DS_JWT_SECRET, OO_DS_URL
from ..db import get_db
from ..deps import doc_perm_checker, get_current_user
from ..models import Document, DocumentAcl, DocumentVersion, User
from ..storage import storage
from ..version_service import commit_version

OO_EXTS = {".docx", ".xlsx", ".pptx"}
DOC_TYPE = {".docx": "word", ".xlsx": "cell", ".pptx": "slide"}

router = APIRouter(prefix="/api/editors", tags=["editors"])


def _latest(db: Session, doc: Document) -> DocumentVersion:
    ver = db.query(DocumentVersion).filter_by(document_id=doc.id)\
        .order_by(DocumentVersion.seq.desc()).first()
    if not ver:
        raise HTTPException(404, "文档暂无版本")
    return ver


@router.get("/file/{document_id}")
def editor_file(document_id: int, request: Request, db: Session = Depends(get_db)):
    """DS 专用取文件端点：URL 按文档+版本稳定，不含用户会话 token。

    鉴权方式：校验 DS 外发请求携带的 Authorization Bearer（DS 用自身
    JWT_SECRET 签名）。URL 与登录态无关，同一 document key 始终对应同一
    文件版本，DS 不会因用户重新登录误判文件变化而中断编辑会话。"""
    auth = request.headers.get("Authorization", "")
    bearer = auth[7:] if auth.startswith("Bearer ") else ""
    oo_ok = False
    if bearer:
        try:
            pyjwt.decode(bearer, OO_DS_JWT_SECRET, algorithms=["HS256"])
            oo_ok = True
        except Exception:
            oo_ok = False
    if not oo_ok:   # 兼容：人工/测试用 DocVault 自身 token 访问
        get_current_user(request, db)
    doc = db.get(Document, document_id)
    if not doc or doc.status != "active":
        raise HTTPException(404, "文档不存在")
    ver = _latest(db, doc)
    return FileResponse(storage.path(ver.blob_hash),
                        filename=f"{doc.name}{doc.ext}",
                        media_type="application/octet-stream")


@router.get("/blob/{blob_hash}")
def editor_blob(blob_hash: str, request: Request, db: Session = Depends(get_db)):
    """DS 专用取制品端点（内容寻址）：供 ConvertService 转换预览 PDF 时回源。

    鉴权与 /file 一致：验证 DS 自带 JWT；hash 即内容指纹，天然稳定可缓存。"""
    auth = request.headers.get("Authorization", "")
    bearer = auth[7:] if auth.startswith("Bearer ") else ""
    oo_ok = False
    if bearer:
        try:
            pyjwt.decode(bearer, OO_DS_JWT_SECRET, algorithms=["HS256"])
            oo_ok = True
        except Exception:
            oo_ok = False
    if not oo_ok:
        get_current_user(request, db)
    if not blob_hash.replace("-", "").isalnum() or len(blob_hash) < 16:
        raise HTTPException(400, "非法制品标识")
    path = storage.path(blob_hash)
    if not path.exists():
        raise HTTPException(404, "制品不存在")
    return FileResponse(path, media_type="application/octet-stream")


def _can_edit(db: Session, user: User, doc: Document) -> bool:
    """是否有内容写权限（统一内核判定；管理员默认只读，需临时提权）"""
    from .. import authz
    return authz.access_doc(db, user, doc).can_write


def _browser_ds_url(request: Request | None) -> str:
    """浏览器侧加载 api.js 用的 DS 地址。
    OO_DS_URL 配置为 127.0.0.1 时，其它机器访问会解析到访问者自己的机器，故按请求 Host 动态替换主机名。"""
    if request is None:
        return OO_DS_URL
    p = urllib.parse.urlsplit(OO_DS_URL)
    if p.hostname not in ("127.0.0.1", "localhost"):
        return OO_DS_URL
    host = (request.headers.get("host") or "").split(":")[0]
    if not host or host in ("127.0.0.1", "localhost"):
        return OO_DS_URL
    return urllib.parse.urlunsplit((p.scheme, f"{host}:{p.port or 80}", p.path, "", ""))


@router.get("/config/{document_id}")
def editor_config(request: Request,
                  doc_user=Depends(doc_perm_checker("read", content_required=True)), token: str = "",
                  db: Session = Depends(get_db)):
    """下发 OnlyOffice 编辑器配置（config 整体用 DS 的 JWT_SECRET 签名）"""
    doc, user = doc_user
    if doc.ext not in OO_EXTS:
        raise HTTPException(415, f"{doc.ext} 不支持在线编辑（仅 docx / xlsx / pptx）")
    latest = _latest(db, doc)

    # DS 必须能从容器内回访 DocVault；取文件走 /file/{id} 端点（URL 按版本稳定，不带用户 token）
    base = OO_DS_INTERNAL_URL.rstrip("/")
    doc_url = f"{base}/api/editors/file/{doc.id}?v={latest.seq}"
    cb_url = f"{base}/api/editors/callback/{doc.id}"
    key = f"dv{doc.id}_v{latest.seq}_{latest.blob_hash[:10]}"

    # 编辑模式只看「是否有编辑权限」：多人可同时进入同一会话协同编辑；
    # 锁只用于状态提示，保存统一走版本链，不因他人持锁降级为只读。
    mode = "edit" if _can_edit(db, user, doc) else "view"

    cfg = {
        "documentType": DOC_TYPE[doc.ext],
        "document": {
            "title": f"{doc.name}{doc.ext}",
            "url": doc_url,
            "fileType": doc.ext.lstrip("."),
            "key": key,
            "permissions": {"edit": mode == "edit"},
        },
        "editorConfig": {
            "callbackUrl": cb_url,
            "lang": "zh-CN",
            "mode": mode,
            "user": {"id": str(user.id), "name": user.display_name or user.username},
            "customization": {
                "compactHeader": True,
                "hideRightMenu": True,
                "autosave": True,
                "forcesave": True,
                # 去除 OnlyOffice 品牌露出（左上角 logo、关于、反馈入口）
                "logo": {"image": "", "imageEmbedded": "", "url": ""},
                "about": False,
                "feedback": False,
                "customer": {"name": "", "info": "", "logo": ""},
            },
        },
    }
    cfg["token"] = pyjwt.encode(cfg, OO_DS_JWT_SECRET, algorithm="HS256")
    return {"config": cfg, "ds_url": _browser_ds_url(request), "mode": mode}


_DISCARD_UNTIL: dict[int, float] = {}
# 在线状态：doc_id -> {user_id: 最后心跳时间}，用于阻止"多人协同时一人放弃导致他人改动丢失"
_PRESENCE: dict[int, dict[int, float]] = {}
PRESENCE_TTL = 90


def _active_users(document_id: int, exclude_id: int | None = None) -> list[int]:
    now = time.time()
    users = {uid: ts for uid, ts in _PRESENCE.get(document_id, {}).items() if now - ts < PRESENCE_TTL}
    _PRESENCE[document_id] = users
    return [uid for uid in users if uid != exclude_id]


@router.post("/presence/{document_id}")
def touch_presence(document_id: int, doc_user=Depends(doc_perm_checker("read"))):
    """编辑器心跳：登记当前用户正在该文档的编辑会话中"""
    doc, user = doc_user
    _PRESENCE.setdefault(document_id, {})[user.id] = time.time()
    return {"ok": True}


@router.get("/presence/{document_id}")
def get_presence(document_id: int, doc_user=Depends(doc_perm_checker("read")),
                 db: Session = Depends(get_db)):
    """当前同一文档的其他在线成员（协同时禁止"放弃改动"）"""
    doc, user = doc_user
    others = _active_users(document_id, exclude_id=user.id)
    names = []
    for uid in others:
        u = db.get(User, uid)
        names.append(u.display_name or u.username if u else str(uid))
    return {"others": len(others), "names": names}


@router.post("/render-md")
def render_md(data: dict, user: User = Depends(get_current_user)):
    """Markdown 实时预览：编辑区右侧即时渲染（不落库，仅渲染）"""
    from ..extract import md_to_html
    return {"html": md_to_html(str(data.get("content") or ""))}


@router.post("/discard/{document_id}")
def mark_discard(document_id: int, doc_user=Depends(doc_perm_checker("write"))):
    """标记「放弃本次编辑」：随后的保存回调将被丢弃，不产生新版本。
    多人协同时拒绝——放弃会连同其他人未保存的改动一起丢弃。"""
    doc, user = doc_user
    others = _active_users(document_id, exclude_id=user.id)
    if others:
        raise HTTPException(
            409,
            f"当前还有 {len(others)} 位成员在同一文档编辑，放弃会一并丢弃他们的改动，已阻止该操作")
    _DISCARD_UNTIL[document_id] = time.time() + 180
    return {"ok": True, "ttl": 180}


@router.post("/callback/{document_id}")
def editor_callback(document_id: int, request: Request, db: Session = Depends(get_db)):
    """DS 编辑保存回调：取回编辑后的文件，提交为新版本（来源=在线编辑）
    回调体由 DS 以 JWT_SECRET 签名放在 Authorization: Bearer 里，先验签再处理。"""
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    if not token:
        raise HTTPException(401, "缺少 OnlyOffice 回调签名")
    try:
        decoded = pyjwt.decode(token, OO_DS_JWT_SECRET, algorithms=["HS256"])
    except Exception:
        raise HTTPException(401, "回调签名无效")
    # OO 的 JWT 是包装格式：真正的回调体嵌在 payload 字段里（顶层只有 payload）
    body = decoded.get("payload") if isinstance(decoded.get("payload"), dict) else decoded

    status = body.get("status")
    # 全状态留痕（0=无改动关闭 1=构建中 2=保存并关闭 4=异常关闭 6=强制保存），便于排查
    audit.log(db, 0, "editor_callback", request, "document", document_id,
              {"status": status, "key": body.get("key", "")})
    if status not in (2, 6):        # 2=保存完成并关闭 6=强制保存；其余状态直接确认
        return {"error": 0}

    doc = db.get(Document, document_id)
    if not doc or doc.status != "active":
        return {"error": 0}
    url = body.get("url", "")
    if not url:
        return {"error": 0}

    # DS 生成的文件 URL 的 host 可能是容器内部名（不可达）——先按原 URL 下载，失败再换宿主映射端口重试
    parts = urllib.parse.urlparse(url)
    ds_netloc = OO_DS_URL.split("//", 1)[1]
    candidates = [url]
    if parts.netloc != ds_netloc:
        candidates.append(urllib.parse.urlunparse(parts._replace(netloc=ds_netloc)))
    tmp = pathlib.Path(storage.root.parent / "tmp")
    tmp.mkdir(exist_ok=True)
    tmp_file = tmp / f"oo_{document_id}_{body.get('key', 'x')}{doc.ext}"
    ok = False
    for u in candidates:
        try:
            urllib.request.urlretrieve(u, tmp_file)
            ok = True
            break
        except Exception:
            continue
    if not ok:
        audit.log(db, doc.created_by, "editor_callback_error", request, "document",
                  doc.id, {"err": "下载编辑结果失败", "url": url})
        return {"error": 1}

    uid = doc.lock_owner_id or doc.created_by
    # 用户选择了"放弃改动"：丢弃这次保存，不产生版本
    discard_at = _DISCARD_UNTIL.get(document_id)
    if discard_at and time.time() < discard_at:
        _DISCARD_UNTIL.pop(document_id, None)
        audit.log(db, uid, "editor_discarded", request, "document", doc.id,
                  {"key": body.get("key", "")})
        if doc.lock_owner_id:
            doc.lock_owner_id = None
            doc.lock_expires_at = None
            db.commit()
        try:
            tmp_file.unlink(missing_ok=True)
        except Exception:
            pass
        return {"error": 0}
    _DISCARD_UNTIL.pop(document_id, None)

    try:
        ver = commit_version(db, doc, tmp_file, doc.ext, "在线编辑",
                             uid, source="edit", request=request)
        audit.log(db, uid, "editor_saved", request, "document", doc.id, {"seq": ver.seq})
    except Exception as e:
        # 内容无实质变化（幂等）等场景：告知 DS 无错误即可
        audit.log(db, doc.created_by, "editor_commit_skipped", request, "document",
                  doc.id, {"reason": str(e)[:200]})
    finally:
        # 锁随保存自动释放（编辑闭环），无论是否入链
        if doc.lock_owner_id:
            doc.lock_owner_id = None
            doc.lock_expires_at = None
            db.commit()
    return {"error": 0}
