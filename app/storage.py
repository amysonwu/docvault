# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 DocVault Contributors
# Author: Amyson Wu
"""文件存储层：抽象接口 + 磁盘内容寻址实现

两层内容寻址：
- 整文件 CAS（raw）：blob 路径 = data/blobs/<h[:2]>/<h[2:4]>/<h>，h 为 SHA-256 hex
- 块级 CAS（office / chunks）：文件内容 = 有序块清单（storage_manifests / items），
  块全局共享（storage_chunks，引用计数），真实编辑过的版本只存变化块；
  office 部件解出内容后一律再过 CDC（小部件自然退化为单块）
- office/chunks 的 file_hash 为清单哈希（跨机器确定性）；raw 保持整文件 SHA-256 原值
- 块文件统一 zstd 帧落盘（仅磁盘编码；哈希与校验均针对解压内容，对上层透明）

对外契约不变：put 返回内容哈希、path 返回可读 Path（块级内容按需物化到缓存），
业务层 18 处调用零感知。未来切换 MinIO/S3：实现 StorageBackend 新子类即可。
"""
import hashlib
import os
import shutil
import tempfile
import time
import zipfile
from abc import ABC, abstractmethod
from pathlib import Path

import zstandard
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .chunking import (OFFICE_EXTS, assemble_office, cdc_chunks, manifest_hash,
                       sha256_bytes, split_office)
from .config import BLOB_DIR, CACHE_DIR, CACHE_MAX_GB, CDC_MAX_MB, DEDUP_MODE, ZSTD_LEVEL
from .db import SessionLocal
from .models import StorageChunk, StorageManifest, StorageManifestItem


def _zstd_compress(data: bytes) -> bytes:
    """块落盘压缩。哈希/清单/校验均针对解压内容，压缩只是磁盘编码，对上层透明。"""
    return zstandard.ZstdCompressor(level=ZSTD_LEVEL).compress(data)


def _zstd_decompress(data: bytes) -> bytes:
    return zstandard.ZstdDecompressor().decompress(data)


def sha256_file(fp) -> str:
    h = hashlib.sha256()
    for chunk in iter(lambda: fp.read(1024 * 1024), b""):
        h.update(chunk)
    return h.hexdigest()


class BlobMissingError(Exception):
    """内容寻址对象在磁盘上缺失（存储被清空/迁移丢失等）。

    上层应将其转为明确的 4xx 提示（见 main.py 全局 handler），
    而不是让 FileResponse 以 500 + 崩栈收场。"""
    def __init__(self, blob_hash: str, why: str = ""):
        self.blob_hash = blob_hash
        super().__init__(f"内容对象缺失: {blob_hash}" + (f"（{why}）" if why else ""))


class StorageBackend(ABC):
    @abstractmethod
    def put(self, src_path: Path, ext: str | None = None) -> str:
        """落盘一个本地文件，返回内容哈希（内容相同返回既有哈希）"""

    @abstractmethod
    def path(self, blob_hash: str) -> Path:
        """返回可读路径（磁盘实现直接给 Path；对象存储实现时换临时下载）"""

    @abstractmethod
    def exists(self, blob_hash: str) -> bool: ...

    @abstractmethod
    def size(self, blob_hash: str) -> int: ...

    @abstractmethod
    def delete(self, blob_hash: str) -> None: ...


class DiskCASStorage(StorageBackend):
    def __init__(self, root: Path = BLOB_DIR, cache_dir: Path = CACHE_DIR):
        self.root = root
        self.cache_dir = cache_dir
        self.root.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._last_prune = 0.0

    # ---- 路径 ----
    def _blob_path(self, h: str) -> Path:
        return self.root / h[:2] / h[2:4] / h

    def _cache_path(self, h: str) -> Path:
        return self.cache_dir / h

    def _write_blob(self, src: Path, dst: Path):
        """临时文件 + 原子改名，避免半写文件"""
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.NamedTemporaryFile(delete=False, dir=dst.parent)
        with open(src, "rb") as f:
            shutil.copyfileobj(f, tmp.file, 1024 * 1024)
        tmp.file.close()
        Path(tmp.name).replace(dst)

    def _read_chunk_verified(self, item_hash: str, expect_size: int) -> bytes:
        """读块（zstd 解压）并逐块校验 SHA-256（防磁盘腐坏静默扩散的唯一防线）"""
        p = self._blob_path(item_hash)
        if not p.exists():
            raise BlobMissingError(item_hash, "存储块不在磁盘上")
        data = _zstd_decompress(p.read_bytes())
        if len(data) != expect_size or sha256_bytes(data) != item_hash:
            raise IOError(f"存储块校验失败: {item_hash}（文件可能损坏，请运行存储巡检）")
        return data

    # ---- put ----
    def put(self, src_path: Path, ext: str | None = None) -> str:
        src = Path(src_path)
        fsize = src.stat().st_size
        kind, items, file_hash = self._plan(src, ext, fsize)

        sess: Session = SessionLocal()
        try:
            if sess.get(StorageManifest, file_hash):
                # 幂等：清单已存在。raw 主块文件兜底补写（理论上已存在）
                if kind == "raw" and not self._blob_path(file_hash).exists():
                    self._write_blob(src, self._blob_path(file_hash))
                sess.commit()
                return file_hash

            # 1) 缺失块先落盘（临时 + 原子改名），统一 zstd 帧编码
            for it in items:
                dst = self._blob_path(it["item_hash"])
                if dst.exists():
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                if it.get("data") is not None:
                    tmp = tempfile.NamedTemporaryFile(delete=False, dir=dst.parent)
                    tmp.file.write(_zstd_compress(it["data"]))
                    tmp.file.close()
                    Path(tmp.name).replace(dst)
                elif kind == "raw":
                    self._write_blob(src, dst)
                else:  # pragma: no cover
                    raise IOError(f"清单项缺数据: {it['item_hash']}")

            # 2) DB 事务：块 upsert（refcount+1）+ 清单登记（幂等）
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            for it in items:
                stmt = mysql_insert(StorageChunk).values(
                    hash=it["item_hash"], size=it["item_size"], refcount=1, created_at=now)
                stmt = stmt.on_duplicate_key_update(refcount=StorageChunk.refcount + 1)
                sess.execute(stmt)
            sess.add(StorageManifest(file_hash=file_hash, kind=kind,
                                     n_items=len(items), bytes_total=fsize))
            for seq, it in enumerate(items):
                sess.add(StorageManifestItem(file_hash=file_hash, seq=seq,
                                             name=it.get("name") or None,
                                             item_hash=it["item_hash"],
                                             item_size=it["item_size"]))
            sess.commit()
        except IntegrityError:
            # 并发同内容：另一请求已建立清单，其事务含 refcount 计数，本次回滚即可
            sess.rollback()
        finally:
            sess.close()
        return file_hash

    def _plan(self, src: Path, ext: str | None, fsize: int):
        """选择切分器：office（L1）→ chunks（L2）→ raw。任何异常都降级 raw。"""
        if DEDUP_MODE != "off" and ext and ext.lower() in OFFICE_EXTS \
                and zipfile.is_zipfile(src):
            try:
                items = split_office(src)
                if items:
                    return "office", items, manifest_hash(items)
            except Exception:
                pass
        if DEDUP_MODE == "all" and fsize <= CDC_MAX_MB * 1024 * 1024:
            try:
                buf = src.read_bytes()
                parts = cdc_chunks(buf)
                if parts:
                    items = [{"name": None, "item_hash": sha256_bytes(p),
                              "item_size": len(p), "data": p} for p in parts]
                    return "chunks", items, manifest_hash(items)
            except Exception:
                pass
        with open(src, "rb") as f:
            h = sha256_file(f)
        return "raw", [{"name": "", "item_hash": h, "item_size": fsize, "data": None}], h

    # ---- path：块级内容物化到缓存 ----
    def path(self, blob_hash: str) -> Path:
        sess: Session = SessionLocal()
        try:
            m = sess.get(StorageManifest, blob_hash)
            kind = m.kind if m else None
        finally:
            sess.close()

        if kind is None or kind == "raw":
            # 无清单（旧数据 / off 模式）或 raw：blob 文件本身就是原文
            p = self._blob_path(blob_hash)
            if not p.exists():
                raise BlobMissingError(blob_hash, "整文件对象不在磁盘上")
            return p

        cache = self._cache_path(blob_hash)
        if cache.exists():
            if kind == "chunks" and m:
                ok = cache.stat().st_size == m.bytes_total
            else:
                ok = True    # office 重组 zip 大小 ≠ 逻辑大小；文件原子落盘即完整
            if ok:
                return cache

        items = self._manifest_items(blob_hash)
        tmp = tempfile.NamedTemporaryFile(delete=False, dir=self.cache_dir)
        tmp.file.close()
        try:
            if kind == "office":
                assemble_office(items, tmp.name,
                                lambda h, s: self._read_chunk_verified(h, s))
            else:
                with open(tmp.name, "wb") as out:
                    for it in items:
                        out.write(self._read_chunk_verified(it["item_hash"], it["item_size"]))
            os.replace(tmp.name, cache)
        finally:
            Path(tmp.name).unlink(missing_ok=True)
        self._prune_cache()
        return cache

    def _manifest_items(self, file_hash: str):
        sess: Session = SessionLocal()
        try:
            rows = (sess.query(StorageManifestItem)
                    .filter_by(file_hash=file_hash).order_by(StorageManifestItem.seq).all())
            return [{"name": r.name, "item_hash": r.item_hash,
                     "item_size": r.item_size} for r in rows]
        finally:
            sess.close()

    # ---- exists / size / delete ----
    def exists(self, blob_hash: str) -> bool:
        sess: Session = SessionLocal()
        try:
            if sess.get(StorageManifest, blob_hash):
                return True
        finally:
            sess.close()
        return self._blob_path(blob_hash).exists()

    def size(self, blob_hash: str) -> int:
        sess: Session = SessionLocal()
        try:
            m = sess.get(StorageManifest, blob_hash)
            if m:
                return m.bytes_total
        finally:
            sess.close()
        p = self._blob_path(blob_hash)
        return p.stat().st_size if p.exists() else 0

    def delete(self, blob_hash: str) -> None:
        """删除一份内容：清单回收 → 块引用计数递减 → 归零块物理删除。

        注意：调用方须确保没有其它版本仍引用该哈希（回收站彻底删除时的前提）；
        scripts/gc_storage.py 是不依赖调用纪律的权威重算通道。
        """
        sess: Session = SessionLocal()
        try:
            m = sess.get(StorageManifest, blob_hash)
            if not m:
                p = self._blob_path(blob_hash)
                if p.exists():
                    p.unlink()
                return
            items = sess.query(StorageManifestItem).filter_by(file_hash=blob_hash).all()
            for it in items:
                c = sess.get(StorageChunk, it.item_hash)
                if c:
                    c.refcount -= 1
                    if c.refcount <= 0:
                        pf = self._blob_path(c.hash)
                        if pf.exists():
                            pf.unlink()
                        sess.delete(c)
            sess.query(StorageManifestItem).filter_by(file_hash=blob_hash).delete()
            sess.delete(m)
            sess.commit()
        finally:
            sess.close()
        self._cache_path(blob_hash).unlink(missing_ok=True)

    # ---- 缓存淘汰 ----
    def _prune_cache(self):
        """每小时最多触发一次：mtime>7 天必删；总量超配额时按最旧优先删。"""
        now = time.time()
        if now - self._last_prune < 3600:
            return
        self._last_prune = now
        if not self.cache_dir.exists():
            return
        infos = sorted(((e.stat().st_mtime, e.stat().st_size, e)
                        for e in self.cache_dir.iterdir() if e.is_file()),
                       key=lambda x: x[0])
        total = sum(s for _, s, _ in infos)
        quota = CACHE_MAX_GB * 1024 ** 3
        for mtime, size, e in infos:
            if mtime < now - 7 * 86400 or total > quota:
                try:
                    e.unlink()
                    total -= size
                except OSError:
                    pass
            else:
                break


storage = DiskCASStorage()
