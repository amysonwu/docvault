# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 DocVault Contributors
# Author: Amyson Wu
"""存储 GC：权威重算通道（可重复执行，不依赖调用纪律）

三步，全部安全幂等：
1. 回收无版本引用的清单（blob_hash/original_blob_hash 都不再指向它）
2. 按「剩余清单项」重算每个块的 refcount；归零块删文件 + 删行
3. 清扫孤儿文件：blobs 目录里既不在块表、也不被任何版本引用、
   且 mtime 超过 24 小时的文件（put 崩溃残留）

用法：python scripts/gc_storage.py [--dry-run]
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func  # noqa: E402

from app.config import BLOB_DIR  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import (DocumentVersion, StorageChunk, StorageManifest,  # noqa: E402
                        StorageManifestItem)

DRY = "--dry-run" in sys.argv


def main():
    sess = SessionLocal()
    try:
        # ---- 1) 活跃哈希集合：所有版本引用的内容 ----
        live = set()
        for h, in sess.query(DocumentVersion.blob_hash):
            if h:
                live.add(h)
        for h, in sess.query(DocumentVersion.original_blob_hash):
            if h:
                live.add(h)

        stale_manifests = [m.file_hash for m in sess.query(StorageManifest)
                           if m.file_hash not in live]
        freed_items = 0
        for fh in stale_manifests:
            n = sess.query(StorageManifestItem).filter_by(file_hash=fh).count()
            freed_items += n
            sess.query(StorageManifestItem).filter_by(file_hash=fh).delete()
            m = sess.get(StorageManifest, fh)
            if m:
                sess.delete(m)
        print(f"[1] 回收无版本引用的清单: {len(stale_manifests)} 份（{freed_items} 个清单项）")

        # ---- 2) 重算 refcount；归零块物理删除 ----
        counts = dict(sess.query(StorageManifestItem.item_hash,
                                 func.count(StorageManifestItem.file_hash))
                      .group_by(StorageManifestItem.item_hash).all())
        dead = 0
        for c in sess.query(StorageChunk):
            c.refcount = counts.get(c.hash, 0)
            if c.refcount <= 0:
                p = BLOB_DIR / c.hash[:2] / c.hash[2:4] / c.hash
                if p.exists() and not DRY:
                    p.unlink()
                if not DRY:
                    sess.delete(c)
                dead += 1
        print(f"[2] 归零块: {dead} 个{'（dry-run 未删除）' if DRY else ''}")

        if not DRY:
            sess.commit()

        # ---- 3) 孤儿文件清扫（崩溃残留：无清单登记且无版本引用）----
        known = {c.hash for c in sess.query(StorageChunk)} | live
        orphans, orphan_bytes = 0, 0
        cutoff = time.time() - 86400
        for p in BLOB_DIR.glob("*/*/*"):
            if p.name in known or p.stat().st_mtime > cutoff:
                continue
            orphan_bytes += p.stat().st_size
            orphans += 1
            if not DRY:
                p.unlink()
        print(f"[3] 孤儿文件: {orphans} 个 / {orphan_bytes} 字节"
              f"{'（dry-run 未删除）' if DRY else ''}")
    finally:
        sess.close()
    print("完成" + ("（dry-run）" if DRY else ""))


if __name__ == "__main__":
    main()
