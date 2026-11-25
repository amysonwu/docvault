# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 DocVault Contributors
# Author: Amyson Wu
"""Office 文档 → PDF 转换（保真预览）

思路：把 docx/doc/pptx/ppt/xlsx/xls 等交给 OnlyOffice Document Server 的
ConvertService 转成 PDF，再由浏览器原生 PDF 阅读器渲染。相比自己解析 OOXML
拼 HTML，这条路能 100% 保留版式：字体、字号、行距、缩进、页边距、分页、
表格边框与底纹、图片与文本框、页眉页脚——都是原件的样子。

结果按 blob 的 SHA-256 缓存在 data/preview_pdf/{hash}.pdf，同一内容只转一次。
转换失败（DS 不可用 / 不支持格式）时返回 None，调用方回落到 HTML 渲染。
"""
import pathlib
import threading
import xml.etree.ElementTree as ET

import jwt
import requests

from .config import DATA_DIR, OO_DS_INTERNAL_URL, OO_DS_JWT_SECRET, OO_DS_SERVER_URL

PDF_CACHE = DATA_DIR / "preview_pdf"
# 转换环境版本：字体/渲染引擎变化时递增，避免命中旧的 DS 转换缓存
_CONV_TAG = "f2"
PDF_CACHE.mkdir(parents=True, exist_ok=True)

# 可交给 DS 转 PDF 的格式（ext → DS 认识的 filetype）
CONVERTIBLE = {
    ".docx": "docx", ".doc": "doc", ".rtf": "rtf", ".odt": "odt",
    ".pptx": "pptx", ".ppt": "ppt", ".odp": "odp",
    ".xlsx": "xlsx", ".xls": "xls", ".ods": "ods", ".csv": "csv",
    ".txt": "txt",
}

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(hash_key: str) -> threading.Lock:
    with _locks_guard:
        lk = _locks.get(hash_key)
        if lk is None:
            lk = _locks[hash_key] = threading.Lock()
        return lk


def cache_path(blob_hash: str) -> pathlib.Path:
    return PDF_CACHE / f"{blob_hash}.pdf"


def cached_pdf(blob_hash: str) -> pathlib.Path | None:
    p = cache_path(blob_hash)
    return p if p.exists() and p.stat().st_size > 0 else None


def _ds_convert(filetype: str, blob_hash: str, timeout: int = 180) -> bytes | None:
    """调 DS 的 ConvertService 把制品转成 PDF，返回 PDF 字节"""
    payload = {
        "async": False,
        "filetype": filetype,
        "outputtype": "pdf",
        "key": f"dvpv{_CONV_TAG}_{blob_hash[:26]}",   # 内容寻址 + 转换版本：字体升级后自动重建
        "title": f"{blob_hash[:10]}.{filetype}",
        "url": f"{OO_DS_INTERNAL_URL}/api/editors/blob/{blob_hash}",
    }
    token = jwt.encode(payload, OO_DS_JWT_SECRET, algorithm="HS256")
    try:
        r = requests.post(
            f"{OO_DS_SERVER_URL}/ConvertService.ashx",
            json=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=timeout,
        )
        if not r.ok:
            return None
        root = ET.fromstring(r.text)
        if (root.findtext("EndConvert") or "").lower() != "true":
            return None
        file_url = root.findtext("FileUrl")
        if not file_url:
            return None
    except Exception:
        return None

    # FileUrl 里的主机名是 DS 自己视角的地址，换成我们配置的浏览器/AI 可达地址
    try:
        from urllib.parse import urlsplit, urlunsplit
        parts = urlsplit(file_url)
        base = urlsplit(OO_DS_SERVER_URL)
        file_url = urlunsplit((base.scheme, base.netloc, parts.path, parts.query, ""))
        r2 = requests.get(file_url, timeout=timeout,
                          headers={"Authorization": f"Bearer {token}"})
        if not r2.ok or not r2.content.startswith(b"%PDF"):
            return None
        return r2.content
    except Exception:
        return None


def ensure_pdf(blob_hash: str, ext: str) -> pathlib.Path | None:
    """确保该 blob 有对应的 PDF 预览文件；不支持/失败返回 None。

    PDF 原件直接返回其缓存副本（不重复转换）。
    """
    ext = (ext or "").lower()
    dst = cache_path(blob_hash)
    if dst.exists() and dst.stat().st_size > 0:
        return dst

    from .storage import storage
    src = storage.path(blob_hash)
    if not src.exists():
        return None

    with _lock_for(blob_hash):
        if dst.exists() and dst.stat().st_size > 0:
            return dst

        if ext == ".pdf":
            data = src.read_bytes()
        else:
            filetype = CONVERTIBLE.get(ext)
            if not filetype:
                return None
            data = _ds_convert(filetype, blob_hash)
            if data is None:
                return None

        tmp = dst.with_suffix(".pdf.part")
        tmp.write_bytes(data)
        tmp.replace(dst)
        return dst


def ensure_pdf_for_file(path: pathlib.Path, ext: str) -> pathlib.Path | None:
    """按文件内容（SHA-256）找/生成 PDF，用于制品不在 CAS 就位时的场景"""
    import hashlib
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        blob_hash = h.hexdigest()
    except OSError:
        return None
    return ensure_pdf(blob_hash, ext)


def pdf_text(pdf_path: pathlib.Path, limit: int = 500_000) -> str:
    """从 PDF 抽正文（老格式 doc/ppt/xls 转 PDF 后用于全文检索）"""
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(pdf_path))
        parts = []
        for page in reader.pages:
            t = page.extract_text() or ""
            if t.strip():
                parts.append(t)
            if sum(len(x) for x in parts) > limit:
                break
        return "\n".join(parts)[:limit]
    except Exception:
        return ""


def invalidate(blob_hash: str) -> None:
    """内容变更/清理时移除缓存（同 hash 内容不变，一般不需要）"""
    p = cache_path(blob_hash)
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass
