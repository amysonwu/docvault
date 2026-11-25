# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 DocVault Contributors
# Author: Amyson Wu
"""文档路由：上传 / 列表 / 检索 / 下载 / 回收站 / 编辑锁"""
import datetime as dt
import pathlib

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, or_, text
from sqlalchemy.orm import Session

from .. import audit
from ..config import LOCK_TTL_SECONDS, MAX_UPLOAD_MB, PREVIEW_EXTS
from ..db import get_db
from ..deps import (doc_perm_checker, get_current_user, require_doc_perm,
                    require_space_perm, system_admin_only)
from ..extract import extract_preview
from ..models import Document, DocumentVersion, Folder, User, VersionTag
from ..schemas import CreateDocIn, SaveTextIn
from ..storage import storage
from ..version_service import commit_version, sanitize_name, version_download_name

router = APIRouter(prefix="/api/documents", tags=["documents"])

CREATE_EXTS = {".md", ".txt", ".docx"}


def doc_out(db: Session, d: Document, user: User | None = None,
            tags: list[str] | None = None) -> dict:
    latest = db.query(DocumentVersion).filter_by(document_id=d.id)\
        .order_by(DocumentVersion.seq.desc()).first()
    locker_name = None
    if d.lock_owner_id:
        u = db.get(User, d.lock_owner_id)
        locker_name = (u.display_name or u.username) if u else None
    # 最后更新人 = 最新版本的提交者（没有版本时回落为创建者）
    updater = None
    if latest and latest.created_by:
        updater = db.get(User, latest.created_by)
    elif d.created_by:
        updater = db.get(User, d.created_by)
    updater_name = (updater.display_name or updater.username) if updater else None
    out = {"id": d.id, "name": d.name, "ext": d.ext, "original_ext": d.original_ext,
           "folder_id": d.folder_id, "status": d.status,
           "size": latest.size if latest else 0, "latest_seq": latest.seq if latest else None,
           "lock_owner_id": d.lock_owner_id, "lock_owner_name": locker_name,
           "updated_at": d.updated_at, "created_by": d.created_by,
           "updater_id": updater.id if updater else None, "updater_name": updater_name,
           "tags": tags if tags is not None else _doc_tags(db, d.id)}
    if user is not None:
        from .. import authz
        acc = authz.access_doc(db, user, d)
        out["my_content"] = acc.content
        out["my_admin"] = acc.is_admin
        out["my_source"] = acc.source
    return out


def _tag_map(db: Session, doc_ids: list[int]) -> dict[int, list[str]]:
    """一次查出多份文档的标签（按版本聚合、去重保序）"""
    if not doc_ids:
        return {}
    out: dict[int, list[str]] = {}
    for t in (db.query(VersionTag)
              .filter(VersionTag.document_id.in_(doc_ids))
              .order_by(VersionTag.created_at.desc()).all()):
        names = out.setdefault(t.document_id, [])
        if t.name not in names:
            names.append(t.name)
    return out


def _doc_tags(db: Session, doc_id: int) -> list[str]:
    return _tag_map(db, [doc_id]).get(doc_id, [])


def _docs_having_tag(db: Session, tag: str, doc_ids: list[int]) -> set[int]:
    if not doc_ids:
        return set()
    want = tag.strip().lower()
    rows = (db.query(VersionTag.document_id, VersionTag.name)
            .filter(VersionTag.document_id.in_(doc_ids)).all())
    return {did for did, name in rows if (name or "").lower() == want}


@router.post("/upload")
def upload(
    space_id: int,
    file: UploadFile = File(...),
    folder_id: int | None = None,
    note: str = "初始版本（上传）",
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    request: Request = None,
):
    """上传新文档 → 生成 v1（上传新文件默认生成 v1）"""
    from .. import authz
    suffix = pathlib.Path(file.filename).suffix.lower()
    stem = sanitize_name(pathlib.Path(file.filename).stem)
    if suffix not in PREVIEW_EXTS:
        raise HTTPException(415, f"不支持的格式：{suffix}（首发支持 docx/pptx/xlsx/pdf/doc/ppt/txt/图片）")

    # 容器写权限：上传到目录需目录可写；上传到空间根需空间可写
    if folder_id:
        folder = db.get(Folder, folder_id)
        if folder is None or folder.space_id != space_id:
            raise HTTPException(404, "目录不存在")
        acc = authz.access_folder(db, user, folder)
        if acc.source == "private":
            raise HTTPException(404, "目录不存在")
        if not acc.can_write:
            raise HTTPException(403, "你对该目录没有写权限")
    else:
        if not authz.access_space(db, user, space_id).can_write:
            raise HTTPException(403, "你在该空间没有写权限")

    tmp = pathlib.Path(storage.root.parent / "tmp")
    tmp.mkdir(exist_ok=True)
    tmp_file = tmp / f"up_{dt.datetime.utcnow().timestamp()}_{file.filename}"
    size = 0
    with open(tmp_file, "wb") as f:
        while chunk := file.file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_MB * 1024 * 1024:
                f.close()
                tmp_file.unlink(missing_ok=True)
                raise HTTPException(413, f"单文件超过 {MAX_UPLOAD_MB}MB 上限")
            f.write(chunk)

    doc = Document(space_id=space_id, folder_id=folder_id, name=stem, ext=suffix,
                   created_by=user.id)
    db.add(doc)
    db.flush()
    commit_version(db, doc, tmp_file, suffix, note, user.id, source="upload", request=request)
    out = doc_out(db, doc, user)
    out["download_name"] = f"{stem}{suffix}"
    return out


@router.post("/create")
def create_document(
    space_id: int,
    data: CreateDocIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    request: Request = None,
):
    """页面直接新建文档（.md/.txt/.docx）→ 生成含标题的 v1"""
    from .. import authz
    ext = data.ext.lower()
    if not ext.startswith("."):
        ext = "." + ext
    if ext not in CREATE_EXTS:
        raise HTTPException(415, f"在线新建仅支持 {' / '.join(sorted(CREATE_EXTS))}")
    name = sanitize_name(data.name)

    # 容器写权限：目录内新建需目录可写；空间根新建需空间可写
    if data.folder_id:
        folder = db.get(Folder, data.folder_id)
        if folder is None or folder.space_id != space_id:
            raise HTTPException(404, "目录不存在")
        acc = authz.access_folder(db, user, folder)
        if acc.source == "private":
            raise HTTPException(404, "目录不存在")
        if not acc.can_write:
            raise HTTPException(403, "你对该目录没有写权限")
    else:
        if not authz.access_space(db, user, space_id).can_write:
            raise HTTPException(403, "你在该空间没有写权限")

    tmp = pathlib.Path(storage.root.parent / "tmp")
    tmp.mkdir(exist_ok=True)
    tmp_file = tmp / f"new_{dt.datetime.utcnow().timestamp()}_{name}{ext}"
    if ext == ".docx":
        from docx import Document as DocxDocument
        d = DocxDocument()
        d.add_heading(name, level=1)
        d.add_paragraph("")
        d.save(tmp_file)
    elif ext == ".md":
        tmp_file.write_text(f"# {name}\n\n", encoding="utf-8")
    else:
        tmp_file.write_text(f"{name}\n", encoding="utf-8")

    doc = Document(space_id=space_id, folder_id=data.folder_id, name=name, ext=ext,
                   created_by=user.id)
    db.add(doc)
    db.flush()
    commit_version(db, doc, tmp_file, ext, "初始版本（在线新建）", user.id, source="create", request=request)
    out = doc_out(db, doc, user)
    out["download_name"] = f"{name}{ext}"
    return out


@router.get("/{document_id}/raw_text")
def raw_text(doc_user=Depends(doc_perm_checker("read", content_required=True)),
             db: Session = Depends(get_db)):
    """读取 md/txt 正文供页面在线编辑"""
    doc, _ = doc_user
    if doc.ext not in (".md", ".txt"):
        raise HTTPException(415, "仅 md / txt 支持页面编辑")
    latest = db.query(DocumentVersion).filter_by(document_id=doc.id)\
        .order_by(DocumentVersion.seq.desc()).first()
    if not latest:
        raise HTTPException(404, "文档暂无版本")
    p = storage.path(latest.blob_hash)
    return {"name": doc.name, "ext": doc.ext, "content": p.read_text(encoding="utf-8", errors="replace")}


@router.post("/{document_id}/save_text")
def save_text(
    data: SaveTextIn,
    doc_user=Depends(doc_perm_checker("write")),
    db: Session = Depends(get_db),
    request: Request = None,
):
    """页面编辑 md/txt → 提交新版本（沿用编辑锁，版本说明必填由前端保证）"""
    doc, user = doc_user
    if doc.ext not in (".md", ".txt"):
        raise HTTPException(415, "仅 md / txt 支持页面编辑")
    tmp = pathlib.Path(storage.root.parent / "tmp")
    tmp.mkdir(exist_ok=True)
    tmp_file = tmp / f"edit_{dt.datetime.utcnow().timestamp()}_{doc.name}{doc.ext}"
    tmp_file.write_text(data.content, encoding="utf-8")
    ver = commit_version(db, doc, tmp_file, doc.ext, data.note, user.id,
                         source="edit_online", request=request)
    return {"seq": ver.seq, "change_summary": ver.change_summary}


@router.get("")
def list_documents(
    space_id: int,
    folder_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """文档列表：按权限内核逐条过滤（未授权即不可见）"""
    from .. import authz
    q = db.query(Document).filter_by(space_id=space_id, status="active")
    if folder_id is None:
        q = q.filter(Document.folder_id.is_(None))
    else:
        folder = db.get(Folder, folder_id)
        if folder is None or folder.space_id != space_id or not authz.access_folder(db, user, folder).can_read:
            return []
        q = q.filter_by(folder_id=folder_id)
    docs = q.order_by(Document.updated_at.desc()).all()
    visible = [d for d in docs if authz.access_doc(db, user, d).can_read]
    tm = _tag_map(db, [d.id for d in visible])
    return [doc_out(db, d, user, tags=tm.get(d.id, [])) for d in visible]


def _escape_like(s: str) -> str:
    """转义 LIKE 通配符，避免用户输入的 % _ 被当成通配符"""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# 带扩展名搜索时，需要回到去掉后缀的词去命中 name（name 列不含扩展名）
_KNOWN_EXTS = (".docx", ".doc", ".pdf", ".txt", ".md", ".xlsx", ".xls",
               ".pptx", ".ppt", ".rtf", ".odt", ".odp", ".ods", ".csv")


def _split_terms(q: str, limit: int = 5) -> tuple[list[str], list[str]]:
    """把查询切成 (长词, 单字词)。

    ngram_token_size=2：单字无法进全文索引，必须走 LIKE 兜底。
    """
    raw = [t for t in (q or "").split() if t][:limit]
    expanded: list[str] = []
    for t in raw:
        stem = None
        low = t.lower()
        for e in _KNOWN_EXTS:
            if low.endswith(e) and len(t) > len(e):
                stem = t[: -len(e)]
                break
        # name 列不含扩展名：带后缀的词替换为词干，而不是额外追加（否则 AND 永不成立）
        expanded.append(stem if stem else t)
    long_terms = [t for t in expanded if len(t) >= 2]
    short_terms = [t for t in expanded if len(t) == 1]
    return long_terms, short_terms


@router.get("/search")
def search(
    q: str,
    space_id: int | None = None,
    ext: str | None = None,
    tag: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """检索：MySQL ngram 全文索引（MATCH AGAINST）+ 单字 LIKE 兜底

    规则：
    - space_id 省略 = 跨全部「当前用户可读」的空间检索；传入则只查该空间
    - 长词（≥2 字）走 FULLTEXT 索引 `ft_documents_ngram(name, content_text)`，
      布尔模式全部用 +（AND）；按相关度打分排序
    - 单字词（ngram_token_size=2 无法索引）走 LIKE 兜底
    - 空格分词；"会议纪要.docx" 自动替换为词干（name 列不含扩展名）
    - 用户输入的 % _ 作为普通字符处理
    - 只检索 active 文档（不含回收站）；目录级不可见文档在 SQL 层排除
    """
    from .. import authz
    from ..models import Space

    if space_id is None:
        space_ids = [s.id for s in db.query(Space).all()
                     if authz.access_space(db, user, s.id).can_read]
        if not space_ids:
            return []
        base = db.query(Document).filter(
            Document.space_id.in_(space_ids), Document.status == "active")
        hidden: set[int] = set()
        for sid in space_ids:
            hidden |= authz.hidden_doc_ids(db, user, sid)
    else:
        base = db.query(Document).filter_by(space_id=space_id, status="active")
        hidden = authz.hidden_doc_ids(db, user, space_id)

    # 目录级不可见的文档直接在 SQL 里排除，避免 LIMIT 之后才被丢弃
    if hidden:
        base = base.filter(~Document.id.in_(hidden))

    long_terms, short_terms = _split_terms(q)

    if long_terms:
        # 布尔模式：每个词加引号做短语匹配（ngram 必须加引号），前缀 + 表示必须命中
        fts_query = " ".join('+"{}"'.format(t.replace('"', " ")) for t in long_terms)
        where_fts = text(
            "MATCH(name, content_text) AGAINST (:fts1 IN BOOLEAN MODE) > 0"
        ).bindparams(fts1=fts_query)
        order_fts = text(
            "MATCH(name, content_text) AGAINST (:fts2 IN BOOLEAN MODE) DESC"
        ).bindparams(fts2=fts_query)
        base = base.filter(where_fts).order_by(order_fts, Document.updated_at.desc())

    if short_terms:
        name_with_ext = func.concat(Document.name, func.coalesce(Document.ext, ""))
        for t in short_terms:
            pat = f"%{_escape_like(t)}%"
            base = base.filter(or_(
                Document.name.like(pat, escape="\\"),
                name_with_ext.like(pat, escape="\\"),
                Document.content_text.like(pat, escape="\\"),
            ))

    if not long_terms and not short_terms:
        base = base.order_by(Document.updated_at.desc())

    if ext:
        base = base.filter_by(ext=ext)

    # 全量取回后做文档级 ACL 过滤（目录级已在 SQL 层排除），上限放宽到 500
    rows = [d for d in base.limit(500).all() if authz.access_doc(db, user, d).can_read]
    if tag:
        keep = _docs_having_tag(db, tag, [d.id for d in rows])
        rows = [d for d in rows if d.id in keep]
    rows = rows[:100]
    tm = _tag_map(db, [d.id for d in rows])
    return [doc_out(db, d, user, tags=tm.get(d.id, [])) for d in rows]


@router.get("/{document_id}")
def get_document(doc_user=Depends(require_doc_perm("read")), db: Session = Depends(get_db)):
    doc, user = doc_user
    return doc_out(db, doc, user)


@router.get("/{document_id}/versions/{seq}/content")
def version_content(document_id: int, seq: int,
                    doc_user=Depends(require_doc_perm("read", content_required=True)),
                    db: Session = Depends(get_db)):
    """版本内容预览：docx/xlsx/pptx/pdf → 结构化 HTML，md/txt → 纯文本"""
    doc, _ = doc_user
    ver = db.query(DocumentVersion).filter_by(document_id=document_id, seq=seq).first()
    if not ver:
        raise HTTPException(404, "版本不存在")
    res = extract_preview(storage.path(ver.blob_hash), doc.ext)
    if res is None:
        raise HTTPException(415, f"{doc.ext} 暂不支持内容预览")
    return {"id": doc.id, "name": doc.name, "ext": doc.ext, "seq": ver.seq, **res}


@router.get("/{document_id}/versions/{seq}/render")
def version_render(document_id: int, seq: int,
                   doc_user=Depends(require_doc_perm("read", content_required=True)),
                   db: Session = Depends(get_db)):
    """保真预览：Office/PDF 统一转 PDF，直接交给浏览器 PDF 阅读器渲染。

    docx/doc/pptx/ppt/xlsx/xls/rtf/odt → DS 转 PDF（按内容哈希缓存）
    pdf → 原件直出；图片 → 原图直出
    转换不可用时返回 415，前端自动回落到 HTML 预览。
    """
    doc, _ = doc_user
    ver = db.query(DocumentVersion).filter_by(document_id=document_id, seq=seq).first()
    if not ver:
        raise HTTPException(404, "版本不存在")

    ext = (doc.ext or "").lower()
    if ext in (".png", ".jpg", ".jpeg", ".gif"):
        path = storage.path(ver.blob_hash)
        if not path.exists():
            raise HTTPException(404, "内容不存在")
        mime = {"png": "image/png", "jpg": "image/jpeg",
                "jpeg": "image/jpeg", "gif": "image/gif"}[ext.lstrip(".")]
        return FileResponse(path, media_type=mime,
                            headers={"Cache-Control": "private, max-age=300"})

    from ..office_convert import ensure_pdf
    pdf = ensure_pdf(ver.blob_hash, ext)
    if pdf is None:
        raise HTTPException(415, f"{ext} 暂时无法生成保真预览")
    return FileResponse(pdf, media_type="application/pdf",
                        filename=f"{doc.name}.pdf",
                        headers={"Content-Disposition": "inline",
                                 "Cache-Control": "private, max-age=300"})


@router.get("/{document_id}/download")
def download_latest(doc_user=Depends(require_doc_perm("read", content_required=True)),
                    request: Request = None,
                    db: Session = Depends(get_db)):
    """标准格式下载：以库内后缀为准，计入审计"""
    doc, user = doc_user
    ver = db.query(DocumentVersion).filter_by(document_id=doc.id)\
        .order_by(DocumentVersion.seq.desc()).first()
    if not ver:
        raise HTTPException(404, "文档无版本")
    audit.log(db, user.id, "download", request, "document", doc.id, {"seq": ver.seq})
    return FileResponse(storage.path(ver.blob_hash),
                        filename=version_download_name(doc, ver),
                        media_type="application/octet-stream")


@router.post("/{document_id}/lock")
def acquire_lock(doc_user=Depends(require_doc_perm("write")), db: Session = Depends(get_db)):
    """编辑锁：乐观锁 + TTL 心跳"""
    doc, user = doc_user
    now = dt.datetime.utcnow()
    if doc.lock_owner_id and doc.lock_owner_id != user.id and doc.lock_expires_at and doc.lock_expires_at > now:
        u = db.get(User, doc.lock_owner_id)
        raise HTTPException(423, f"{u.display_name or u.username} 正在编辑此文档")
    doc.lock_owner_id = user.id
    doc.lock_expires_at = now + dt.timedelta(seconds=LOCK_TTL_SECONDS)
    db.commit()
    return {"locked": True, "ttl": LOCK_TTL_SECONDS}


@router.delete("/{document_id}/lock")
def release_lock(doc_user=Depends(require_doc_perm("write")), db: Session = Depends(get_db)):
    doc, user = doc_user
    if doc.lock_owner_id == user.id:
        doc.lock_owner_id = None
        doc.lock_expires_at = None
        db.commit()
    return {"locked": False}


@router.delete("/{document_id}")
def trash_document(document_id: int, request: Request = None,
                   db: Session = Depends(get_db),
                   user: User = Depends(system_admin_only)):
    """移入回收站：按权限规划，删除属结构操作，仅系统管理员可执行"""
    doc = db.get(Document, document_id)
    if not doc or doc.status != "active":
        raise HTTPException(404, "文档不存在")
    doc.status = "trashed"
    doc.deleted_at = dt.datetime.utcnow()
    doc.deleted_by = user.id
    db.commit()
    audit.log(db, user.id, "trash", request, "document", doc.id, {"name": doc.name})
    return {"ok": True, "retention_days": 30}


@router.get("/trash/list")
def trash_list(space_id: int, db: Session = Depends(get_db),
               user: User = Depends(require_space_perm("read"))):
    from ..folder_perm import hidden_doc_ids
    docs = db.query(Document).filter_by(space_id=space_id, status="trashed").all()
    blocked = hidden_doc_ids(db, user, space_id)
    if blocked:
        docs = [d for d in docs if d.id not in blocked]
    return [doc_out(db, d, user) for d in docs]


@router.post("/{document_id}/restore")
def restore_document(document_id: int, request: Request = None,
                     db: Session = Depends(get_db),
                     user: User = Depends(system_admin_only)):
    doc = db.get(Document, document_id)
    if not doc or doc.status != "trashed":
        raise HTTPException(404, "回收站中无此文档")
    doc.status = "active"
    doc.deleted_at = None
    db.commit()
    audit.log(db, user.id, "restore", request, "document", doc.id)
    return {"ok": True}
