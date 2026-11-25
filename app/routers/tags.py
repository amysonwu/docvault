# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 DocVault Contributors
# Author: Amyson Wu
"""版本标签检索：把「标签 → 某文档的某个版本」按 git tag 的方式列出来

GET /api/tags                     标签清单（含版本数/文档数/最近使用，供筛选与联想）
GET /api/tags/versions?tag=xx     命中列表：所有打了该标签的（文档, 版本）

权限：只返回当前用户可读的文档；不可读文档的标签不出现，避免泄露存在性。
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import authz
from ..db import get_db
from ..deps import get_current_user
from ..models import Document, DocumentVersion, Space, User, VersionTag

router = APIRouter(prefix="/api/tags", tags=["tags"])


def _visible_rows(db: Session, user: User, space_id: int | None = None):
    """取回标签行（标签 + 文档 + 版本 + 空间），按文档级权限过滤。
    同一文档只判一次权限（多标签时复用）。"""
    q = (db.query(VersionTag, Document, DocumentVersion, Space)
         .join(Document, Document.id == VersionTag.document_id)
         .join(DocumentVersion, DocumentVersion.id == VersionTag.version_id)
         .join(Space, Space.id == Document.space_id)
         .filter(Document.status == "active")
         .order_by(VersionTag.created_at.desc()))
    if space_id:
        q = q.filter(Document.space_id == space_id)
    cache: dict[int, bool] = {}
    out = []
    for tag, doc, ver, sp in q.all():
        if doc.id not in cache:
            cache[doc.id] = authz.access_doc(db, user, doc).can_read
        if cache[doc.id]:
            out.append((tag, doc, ver, sp))
    return out


@router.get("")
def list_tags(q: str | None = None, space_id: int | None = None,
              db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """标签清单：按「最近使用」倒序；q 用于输入联想（包含匹配）"""
    rows = _visible_rows(db, user, space_id)
    kw = (q or "").strip().lower()
    agg: dict[str, dict] = {}
    for tag, doc, ver, sp in rows:
        key = tag.name.lower()
        if kw and kw not in key:
            continue
        item = agg.get(key)
        if item is None:
            item = agg[key] = {"name": tag.name, "version_count": 0,
                               "doc_ids": set(), "last_used": tag.created_at}
        elif tag.created_at > item["last_used"]:
            item["last_used"] = tag.created_at
        item["version_count"] += 1
        item["doc_ids"].add(doc.id)
    out = [{"name": v["name"], "version_count": v["version_count"],
            "doc_count": len(v["doc_ids"]), "last_used": v["last_used"]}
           for v in agg.values()]
    out.sort(key=lambda x: x["last_used"], reverse=True)
    return out


@router.get("/versions")
def tag_versions(tag: str = Query(..., min_length=1), space_id: int | None = None,
                 limit: int = 200,
                 db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """按标签列出命中的（文档, 版本）——即「打了该标签的所有特定历史版本」"""
    want = tag.strip().lower()
    rows = [r for r in _visible_rows(db, user, space_id) if r[0].name.lower() == want]
    doc_ids = {doc.id for _, doc, _, _ in rows}
    latest: dict[int, int] = {}
    if doc_ids:
        for did, seq in (db.query(DocumentVersion.document_id, DocumentVersion.seq)
                         .filter(DocumentVersion.document_id.in_(doc_ids)).all()):
            latest[did] = max(latest.get(did, 0), seq)
    author_ids = {t.created_by for t, _, _, _ in rows} or {0}
    users = {u.id: (u.display_name or u.username)
             for u in db.query(User).filter(User.id.in_(author_ids)).all()}

    out = []
    for tag_row, doc, ver, sp in rows[:max(1, min(limit, 500))]:
        out.append({
            "tag": tag_row.name,
            "tagged_at": tag_row.created_at,
            "tagged_by": users.get(tag_row.created_by, ""),
            "is_latest": latest.get(doc.id, 0) == ver.seq,
            "document": {"id": doc.id, "name": doc.name, "ext": doc.ext,
                         "space_id": sp.id, "space_name": sp.name,
                         "folder_id": doc.folder_id},
            "version": {"seq": ver.seq, "size": ver.size, "note": ver.note,
                        "created_at": ver.created_at, "source": ver.source},
        })
    return out
