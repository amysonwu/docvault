# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 DocVault Contributors
# Author: Amyson Wu
"""DocVault 应用入口"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from .db import SessionLocal, init_db
from .routers import admin, auth, documents, groups, offline, perms, shares, spaces, tags, versions
from .storage import BlobMissingError

app = FastAPI(title="DocVault 智档", version="0.1.0",
              description="私有部署的文档管理与协作平台：版本溯源 · 在线协同 · 全程审计")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(BlobMissingError)
async def _blob_missing(request, exc: "BlobMissingError"):
    """存储对象缺失（存储被清空/迁移丢失）：410 + 可读提示，而非 500 崩栈"""
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=410, content={
        "detail": "该版本的内容文件在存储中缺失（可能因存储重置/迁移丢失）。"
                  "无法打开或下载，请删除该文档后重新上传。"})


app.include_router(auth.router)
app.include_router(spaces.router)
app.include_router(spaces.folders_router)
app.include_router(documents.router)
app.include_router(versions.router)
app.include_router(offline.router)
app.include_router(shares.router)
app.include_router(shares.guest)
app.include_router(perms.router)
app.include_router(groups.router)
app.include_router(tags.router)
app.include_router(admin.router)
from .routers import editors
app.include_router(editors.router)


@app.on_event("startup")
def startup():
    init_db()


@app.get("/api/health")
def health():
    # 顺手验证 DB 连通
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ok", "db": "ok", "build": APP_BUILD}
    except Exception as e:
        return {"status": "ok", "db": f"error: {e}", "build": APP_BUILD}
    finally:
        db.close()


# SPA 前端
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
APP_BUILD = "2026-11-24.43"     # 前端构建标记：排查时确认浏览器加载的是哪一版
_NO_STORE = {"Cache-Control": "no-store, max-age=0, must-revalidate"}


@app.get("/share/{token}", include_in_schema=False)
def guest_share_page(token: str):
    """站外分享访客页面（无需登录）"""
    from fastapi.responses import FileResponse
    return FileResponse(WEB_DIR / "guest.html", headers=_NO_STORE)


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
def index_page():
    """首页显式下发并禁用缓存：避免前端更新后浏览器仍跑旧脚本"""
    from fastapi.responses import FileResponse
    return FileResponse(WEB_DIR / "index.html", headers=_NO_STORE)


@app.get("/guest.html", include_in_schema=False)
def guest_page_file():
    from fastapi.responses import FileResponse
    return FileResponse(WEB_DIR / "guest.html", headers=_NO_STORE)


@app.get("/pdf-viewer.html", include_in_schema=False)
def pdf_viewer_page():
    """自建 PDF 阅读器（本地 pdf.js）：可控缩略图宽度、去浏览器默认工具栏"""
    from fastapi.responses import FileResponse
    return FileResponse(WEB_DIR / "pdf-viewer.html", headers=_NO_STORE)


if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
