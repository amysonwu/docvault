# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 DocVault Contributors
# Author: Amyson Wu
"""存储去重回归测试（L1 部件内 CDC 混合切分 + L2 CDC）

用法: python scripts/dedup_test.py [BASE]   # BASE 默认 http://127.0.0.1:10001

Part A（纯 Python，无需服务）：CDC 边界稳定性（含强制切 min-hash 锚定）、
          清单哈希确定性、Office 切分/重组保真、255K→5M 跨阈值增长共享、
          多版本清单逐字节复现、块落盘 zstd 编码往返
Part B（E2E）：上传 docx → 提交第二版 → 断言只有变化块落盘 → 下载/重组保真
              → 缓存命中 → GC 一致性（需 dv-db / dv-app 容器可 docker exec）
Part C（E2E）：三版本 → 彻底清除中间版本 → 独占块回收、共享块存活
              → GC 幂等 → 其余版本仍逐字节复现
"""
import hashlib
import io
import os
import random
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app.chunking as ck  # noqa: E402
from app.chunking import (assemble_office, cdc_chunks, manifest_hash,  # noqa: E402
                          sha256_bytes, split_office)
from app.config import CACHE_DIR  # noqa: E402

IN_CONTAINER = Path("/.dockerenv").exists()
if IN_CONTAINER:                       # 容器内跑：入口走 compose 网络别名
    BASE = sys.argv[1] if len(sys.argv) > 1 else "http://edge:80"
else:
    BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:10001"


def _has_container(name: str) -> bool:
    """有 docker 命令 ≠ 有目标容器：CI / 裸机直跑时应退回本进程模式"""
    if shutil.which("docker") is None:
        return False
    try:
        r = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                           capture_output=True, text=True, timeout=15)
        return name in r.stdout.split()
    except Exception:
        return False


# 仅当 dv-app / dv-db 确实在跑时才走 docker exec（本机 compose 场景）
HAS_DOCKER = (not IN_CONTAINER) and _has_container("dv-app") and _has_container("dv-db")
MYSQL = ("docker exec dv-db mysql -uroot -p"
         + os.environ.get("DOCVAULT_DB_ROOT_PASS", "DocVault_Root_2026")
         + " docvault -N -B -e")
ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {name}")
    else:
        fail += 1
        print(f"  ❌ {name}  {extra}")


def _sql_db(q):
    """容器内无 docker CLI：直连本进程的 SQLAlchemy"""
    from sqlalchemy import text
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        rows = s.execute(text(q)).fetchall()
        return [[("" if v is None else str(v)) for v in row] for row in rows]
    finally:
        s.close()


def sql(q):
    if HAS_DOCKER:
        r = subprocess.run(MYSQL + f' "{q}"', shell=True, capture_output=True, text=True)
        return [line.split("\t") for line in r.stdout.strip().splitlines() if line]
    return _sql_db(q)


def run_in_app(code: str):
    """在 dv-app 环境执行 python 片段：宿主机走 docker exec；容器内本进程即 app"""
    if HAS_DOCKER:
        return subprocess.run(f"docker exec dv-app python -c {repr(code)}",
                              shell=True, capture_output=True, text=True)
    ns = {"__name__": "__snippet__"}
    exec(compile(code, "<snippet>", "exec"), ns)
    return None


def run_gc():
    """跑一次 GC 脚本（权威 refcount 重算）"""
    if HAS_DOCKER:
        return subprocess.run("docker exec dv-app python scripts/gc_storage.py",
                              shell=True, capture_output=True, text=True)
    root = Path(__file__).resolve().parent.parent
    return subprocess.run([sys.executable, "scripts/gc_storage.py"], cwd=root,
                          capture_output=True, text=True)


def make_docx(paragraphs):
    from docx import Document
    d = Document()
    for p in paragraphs:
        d.add_paragraph(p)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def docx_paragraphs(data: bytes):
    from docx import Document
    d = Document(io.BytesIO(data))
    return [p.text for p in d.paragraphs if p.text.strip()]


def make_zip(parts: dict) -> io.BytesIO:
    """按 {名: 内容} 造一个最小合法 zip（office 容器模拟）"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in parts.items():
            z.writestr(name, data)
    buf.seek(0)
    return buf


def group_items(items):
    """连续同名 item 聚合为部件（与 assemble_office 的重组规则一致）"""
    g, order = {}, []
    for it in items:
        if it["name"] not in g:
            g[it["name"]] = []
            order.append(it["name"])
        g[it["name"]].append(it)
    return g, order


def chunk_items(name: str, blob: bytes):
    return [{"name": name, "item_hash": sha256_bytes(c), "item_size": len(c), "data": c}
            for c in cdc_chunks(blob)]


# ==================== Part A：算法自检 ====================
def part_a():
    print("== Part A. 算法自检 ==")

    # CDC：拼接还原 + 块尺寸分布
    rng = random.Random(42)
    buf = rng.randbytes(1 << 20)
    chunks = cdc_chunks(buf)
    check("CDC 拼接还原原文", b"".join(chunks) == buf)
    sizes = [len(c) for c in chunks]
    check("CDC 块尺寸分布合理", all(len(c) <= 256 * 1024 for c in chunks)
          and 40 * 1024 <= sum(sizes) / len(sizes) <= 130 * 1024,
          f"avg={sum(sizes)//len(sizes)}, n={len(chunks)}")

    # CDC：中间插入 1KB，仅影响局部块（去重的灵魂）
    mid = len(buf) // 2
    buf2 = buf[:mid] + random.Random(7).randbytes(1024) + buf[mid:]
    h1 = [sha256_bytes(c) for c in cdc_chunks(buf)]
    h2 = [sha256_bytes(c) for c in cdc_chunks(buf2)]
    changed = len(set(h2) - set(h1))
    check("CDC 边界稳定性：插入 1KB 仅 ≤3 块变化", changed <= 3, f"changed={changed}")

    # 强制切内容锚定：把 LATE 掩码改成"永不命中"→ 每个块都走强制切（模拟
    # 低熵长尾），此时插入 1 字节仍只影响局部块——min-hash 锚定生效，
    # 不会复现固定分块的"尾部全错位"病症
    real_late = ck.MASK_LATE
    ck.MASK_LATE = ck.MASK64
    try:
        bufF = random.Random(11).randbytes(1 << 20)
        c1 = cdc_chunks(bufF)
        check("强制切场景：块全部 ≤ MAX", all(len(c) <= ck.CDC_MAX for c in c1)
              and all(len(c) > 16 * 1024 for c in c1[:-1]))
        p = len(bufF) // 3
        bufF2 = bufF[:p] + b"\x01" + bufF[p:]
        s1 = {sha256_bytes(c) for c in c1}
        s2 = {sha256_bytes(c) for c in cdc_chunks(bufF2)}
        changedF = len(s2 - s1)
        check("强制切锚定：插入 1 字节仅 ≤3 块变化", changedF <= 3, f"changed={changedF}")
    finally:
        ck.MASK_LATE = real_late

    # 跨阈值增长：255KB 部件被编辑并涨到 5MB+，仍应大量共享未变块
    part_v1 = random.Random(99).randbytes(255 * 1024)
    k = 130 * 1024
    part_v2 = (part_v1[:k] + random.Random(8).randbytes(1024) + part_v1[k:]
               + random.Random(77).randbytes(5 * 1024 * 1024))
    it1 = chunk_items("doc.xml", part_v1)
    it2 = chunk_items("doc.xml", part_v2)
    s1h = {i["item_hash"] for i in it1}
    s2h = {i["item_hash"] for i in it2}
    shared = s1h & s2h
    check("255K→5M：未变区域块共享 ≥2", len(shared) >= 2,
          f"shared={len(shared)}/{len(s1h)}")
    new_bytes = sum(i["item_size"] for i in it2 if i["item_hash"] not in s1h)
    check("255K→5M：新增字节 ≈ 新内容+编辑块（不含未变前缀/后缀）",
          new_bytes < 5 * 1024 * 1024 + 600 * 1024,
          f"new={new_bytes / 1024:.0f}KB / 部件 {len(part_v2) / 1024:.0f}KB")

    # 清单哈希：确定性 + 顺序敏感
    items = [{"name": "a.xml", "item_hash": "x" * 64, "item_size": 10},
             {"name": "b.xml", "item_hash": "y" * 64, "item_size": 20}]
    check("清单哈希确定性", manifest_hash(items) == manifest_hash(list(reversed([items[1], items[0]]))))
    check("清单哈希顺序敏感", manifest_hash(items) != manifest_hash(list(reversed(items))))

    # 块落盘 zstd：压缩可逆 + 帧头标记 + 体积收益
    from app.storage import _zstd_compress, _zstd_decompress
    ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
    sample = ("块级落盘压缩测试。" * 4096).encode("utf-8")   # ~128KB 高重复度内容
    cz = _zstd_compress(sample)
    check("zstd 压缩块带帧头且小于原文", cz[:4] == ZSTD_MAGIC and len(cz) < len(sample),
          f"{len(cz)} vs {len(sample)}")
    check("zstd 解压还原逐字节一致", _zstd_decompress(cz) == sample)

    # Office 切分/重组：部件级无损（小部件每部件 1 块，行为与旧整存一致）
    src = make_docx(["去重测试", "第一段", "第二段"])
    with zipfile.ZipFile(io.BytesIO(src)) as z:
        orig_parts = {i.filename: sha256_bytes(z.read(i.filename)) for i in z.infolist() if not i.is_dir()}
    parts = split_office(io.BytesIO(src))
    g, _ = group_items(parts)
    check("Office 切分部件齐全", set(g) == set(orig_parts),
          f"{len(g)} vs {len(orig_parts)}")
    check("Office 切块哈希自洽", all(i["item_hash"] == sha256_bytes(i["data"]) for i in parts))
    check("Office 部件逐块拼接与原文一致（含多块部件）",
          all(sha256_bytes(b"".join(c["data"] for c in g[n])) == orig_parts[n] for n in g))
    # 重组校验：用切块自身数据当"块库"
    re_zip = Path("_dedup_reassemble.docx")
    assemble_office(parts, re_zip, lambda h, s: next(i["data"] for i in parts if i["item_hash"] == h))
    with zipfile.ZipFile(re_zip) as z2:
        re_parts = {i.filename: sha256_bytes(z2.read(i.filename)) for i in z2.infolist() if not i.is_dir()}
    check("Office 重组逐部件无损", re_parts == orig_parts)
    from docx import Document as _Doc
    check("重组 zip 仍为合法 docx", [p.text for p in _Doc(re_zip).paragraphs][:1] == ["去重测试"])
    re_zip.unlink(missing_ok=True)

    # 多版本复现：v1/v2/v3 清单各自独立，共享块库下逐版本逐字节还原
    small = {"[Content_Types].xml": b"ct", "styles.xml": b"sty" * 100}
    parts_v1 = {**small, "doc.xml": part_v1}
    parts_v2 = {**small, "doc.xml": part_v2}
    part_v3 = part_v2[:600 * 1024] + random.Random(5).randbytes(1024) + part_v2[600 * 1024:]
    parts_v3 = {**small, "doc.xml": part_v3}
    items_by_ver, store = {}, {}
    mhash = {}
    for tag, pz in (("v1", parts_v1), ("v2", parts_v2), ("v3", parts_v3)):
        its = split_office(make_zip(pz))
        items_by_ver[tag] = its
        mhash[tag] = manifest_hash(its)
        store.update({i["item_hash"]: i["data"] for i in its})
    check("三版本清单哈希互不相同", len(set(mhash.values())) == 3)
    check("小部件跨版本共享（每部件 1 块）",
          all(sha256_bytes(parts_v1[n]) in {i["item_hash"] for i in items_by_ver["v2"]}
              for n in small))
    repro_ok = True
    for tag, pz in (("v1", parts_v1), ("v2", parts_v2), ("v3", parts_v3)):
        out = Path(f"_dedup_repro_{tag}.zip")
        assemble_office(items_by_ver[tag], out, lambda h, s: store[h])
        with zipfile.ZipFile(out) as z:
            repro_ok &= all(sha256_bytes(z.read(n)) == sha256_bytes(pz[n]) for n in pz)
        out.unlink(missing_ok=True)
    check("历史复现：三个版本逐字节还原（含 5MB 大部件）", repro_ok)


# ==================== Part B：E2E 全链路 ====================
def part_b():
    print("== Part B. E2E 全链路 ==")
    tag = f"dedup_{int(time.time())}"
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/register",
               json={"username": tag, "password": "dedup123", "display_name": "去重测试"})
    r = s.post(f"{BASE}/api/auth/login", json={"username": tag, "password": "dedup123"})
    s.headers.update({"Authorization": f"Bearer {r.json()['token']}"})

    sp = s.post(f"{BASE}/api/spaces", json={"name": f"_{tag}"}).json()
    stamp = time.strftime("run%H%M%S")     # 内容带运行戳：确保块哈希是本次新块（未压缩的历史块不会重写）
    v1 = make_docx(["去重验证文档", f"运行标识 {stamp}", "第一章 背景说明", "这一段保持不变用于去重校验。", "初始预算 100 万元。"])
    up = s.post(f"{BASE}/api/spaces/{sp['id']}/documents/upload" if False else
                f"{BASE}/api/documents/upload?space_id={sp['id']}",
                files={"file": ("dedup测试.docx", v1,
                                 "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
    check("上传 v1", up.ok, up.text[:150])
    did = up.json()["id"]

    s.post(f"{BASE}/api/documents/{did}/lock")
    v2 = make_docx(["去重验证文档", f"运行标识 {stamp}", "第一章 背景说明", "这一段保持不变用于去重校验。", "预算调整为 120 万元。"])
    c = s.post(f"{BASE}/api/documents/{did}/versions/commit?note=dedup_v2",
               files={"file": ("dedup测试.docx", v2, "application/octet-stream")})
    check("提交 v2", c.ok, c.text[:150])

    # ---- 存储层断言（DB 直查）----
    rows = sql(f"SELECT seq, blob_hash, size FROM document_versions WHERE document_id={did} ORDER BY seq")
    check("两版本 blob_hash 不同（清单哈希）", len(rows) == 2 and rows[0][1] != rows[1][1])
    h1, h2 = rows[0][1], rows[1][1]
    kinds = sql(f"SELECT file_hash, kind, bytes_total FROM storage_manifests WHERE file_hash IN ('{h1}','{h2}')")
    check("两版本均为 office 清单", len(kinds) == 2 and all(k[1] == "office" for k in kinds), str(kinds))
    sizes = {r[1]: int(r[2]) for r in rows}   # blob_hash -> size
    check("bytes_total 与版本 size 一致", all(int(k[2]) == sizes[k[0]] for k in kinds))

    items2 = sql(f"SELECT item_hash FROM storage_manifest_items WHERE file_hash='{h2}'")
    rc = {r[0]: int(r[1]) for r in sql(
        f"SELECT hash, refcount FROM storage_chunks WHERE hash IN ("
        f"{','.join(chr(39) + i[0] + chr(39) for i in items2)})")}
    new_chunks = [h for h, n in rc.items() if n == 1]
    shared = [h for h, n in rc.items() if n >= 2]
    check("v2 新增块 ≤5（只有变化部件落盘）", len(new_chunks) <= 5, f"new={len(new_chunks)}")
    check("v2 复用 v1 部件 ≥5（块级去重生效）", len(shared) >= 5, f"shared={len(shared)}")
    new_bytes = sum(next(int(r[0]) for r in sql(f"SELECT size FROM storage_chunks WHERE hash='{h}'"))
                    for h in new_chunks)
    print(f"      [v1 整件 {sizes[h1]}B ｜ v2 实际新增 {new_bytes}B = {new_bytes / sizes[h1] * 100:.1f}%]")
    check("v2 实际磁盘增量 <50%", new_bytes < sizes[h1] * 0.5)

    # ---- 块落盘编码：全部块文件均为 zstd 帧 ----
    from app.config import BLOB_DIR
    ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
    n_compressed = sum(1 for h in rc
                       if (BLOB_DIR / h[:2] / h[2:4] / h).read_bytes()[:4] == ZSTD_MAGIC)
    check("块文件统一 zstd 帧编码", n_compressed == len(rc),
          f"{n_compressed}/{len(rc)}")

    # ---- 下载/重组保真 ----
    d1 = s.get(f"{BASE}/api/documents/{did}/versions/1/download")
    d2 = s.get(f"{BASE}/api/documents/{did}/versions/2/download")
    check("下载 v1/v2 成功", d1.ok and d2.ok)
    check("v1 重组后段落保真", docx_paragraphs(d1.content)[0] == "去重验证文档")
    check("v2 重组后内容正确", "预算调整为 120 万元。" in docx_paragraphs(d2.content))
    diff = s.get(f"{BASE}/api/documents/{did}/versions/diff?old=1&new=2")
    check("版本 diff 正常（走物化路径）", diff.ok, diff.text[:120])

    # ---- 缓存命中 ----
    if HAS_DOCKER and not IN_CONTAINER:
        cache_ok = subprocess.run(
            f'docker exec dv-app sh -c "test -f /app/data/cache/{h2} && echo yes"',
            shell=True, capture_output=True, text=True).stdout.strip() == "yes"
    else:
        cache_ok = (CACHE_DIR / h2).exists()
    check("重组缓存已落盘", cache_ok)
    d2b = s.get(f"{BASE}/api/documents/{did}/versions/2/download")
    check("二次下载（缓存路径）一致", d2b.ok and d2b.content == d2.content)

    # ---- 清理：文档进回收站 → 删空间 → 存储层回收（容器内执行）----
    adm = requests.Session()
    la = adm.post(f"{BASE}/api/auth/login", json={"username": "admin", "password": "Admin@123"})
    if la.ok:
        adm.headers.update({"Authorization": f"Bearer {la.json()['token']}"})
        adm.delete(f"{BASE}/api/documents/{did}")
    s.delete(f"{BASE}/api/spaces/{sp['id']}")
    cleanup = f"""
import sys; sys.path.insert(0, '/app')
from app.storage import storage
storage.delete({h1!r})
storage.delete({h2!r})
print("cleaned")
"""
    run_in_app(cleanup)
    print("      [测试数据已清理：回收站文档 + 清单 + 块]")


# ==================== Part C：历史复现 + 删中间版本 + GC ====================
def part_c():
    print("== Part C. 历史版本复现 + 删中间版本 + GC ==")
    tag = f"dedupc_{int(time.time())}"
    s = requests.Session()
    s.post(f"{BASE}/api/auth/register",
           json={"username": tag, "password": "dedup123", "display_name": "复现测试"})
    r = s.post(f"{BASE}/api/auth/login", json={"username": tag, "password": "dedup123"})
    s.headers.update({"Authorization": f"Bearer {r.json()['token']}"})
    sp = s.post(f"{BASE}/api/spaces", json={"name": f"_{tag}"}).json()

    def commit(note, paras):
        return s.post(f"{BASE}/api/documents/{did}/versions/commit?note={note}",
                      files={"file": ("复现.docx", make_docx(paras), "application/octet-stream")})

    up = s.post(f"{BASE}/api/documents/upload?space_id={sp['id']}",
                files={"file": ("复现.docx",
                                make_docx(["复现文档", "第一段", "第二段 待修改甲", "第三段"]),
                                "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
    check("上传 v1", up.ok, up.text[:150])
    did = up.json()["id"]
    s.post(f"{BASE}/api/documents/{did}/lock")
    check("提交 v2", commit("v2", ["复现文档", "第一段", "第二段 已修改乙", "第三段"]).ok)
    check("提交 v3", commit("v3", ["复现文档", "第一段", "第二段 已修改乙", "第三段 追加丙"]).ok)

    rows = sql(f"SELECT seq, blob_hash FROM document_versions WHERE document_id={did} ORDER BY seq")
    hashes = {int(r[0]): r[1] for r in rows}
    check("三个版本清单互不相同", len(set(hashes.values())) == 3, str(hashes))

    items = {seq: {r[0] for r in sql(f"SELECT item_hash FROM storage_manifest_items WHERE file_hash='{h}'")}
             for seq, h in hashes.items()}

    # 基线：删除前 v1/v3 可下载（内容即重组结果，逐字节固定）
    d1 = s.get(f"{BASE}/api/documents/{did}/versions/1/download")
    d3 = s.get(f"{BASE}/api/documents/{did}/versions/3/download")
    check("v1/v3 基线下载成功", d1.ok and d3.ok)
    check("v1 内容保真", docx_paragraphs(d1.content)[0] == "复现文档")
    check("v3 内容保真", any("追加丙" in p for p in docx_paragraphs(d3.content)),
          str(docx_paragraphs(d3.content))[:100])

    # 模拟 v2 被彻底清除（回收站彻底删除时 storage.delete 的真实路径）
    purge = f"""
import sys; sys.path.insert(0, '/app')
from app.storage import storage
storage.delete({hashes[2]!r})
print("purged")
"""
    run_in_app(purge)

    # v2 独占块应物理消失；与 v1/v3 共享的块必须存活
    only2 = items[2] - items[1] - items[3]
    q = ",".join(f"'{h}'" for h in only2) or "''"
    gone = sql(f"SELECT hash FROM storage_chunks WHERE hash IN ({q})")
    check("v2 独占块已物理回收", len(gone) == 0, f"残留={len(gone)}/{len(only2)}")
    shared_keep = items[1] & items[3]
    q2 = ",".join(f"'{h}'" for h in shared_keep) or "''"
    alive = sql(f"SELECT hash FROM storage_chunks WHERE hash IN ({q2})")
    check("v1/v3 共享块全部存活", len(alive) == len(shared_keep),
          f"{len(alive)}/{len(shared_keep)}")

    # GC 幂等：连跑两次不误删、不报错
    for i in (1, 2):
        g = run_gc()
        check(f"GC 第 {i} 次跑通", g is None or g.returncode == 0,
              (g.stderr or g.stdout)[-200:] if g else "")

    # 复现能力：删 v2 + GC 后，v1/v3 仍逐字节还原；v2 内容不可取
    d1b = s.get(f"{BASE}/api/documents/{did}/versions/1/download")
    d3b = s.get(f"{BASE}/api/documents/{did}/versions/3/download")
    check("删 v2 + GC 后 v1 仍逐字节复现", d1b.ok and d1b.content == d1.content)
    check("删 v2 + GC 后 v3 仍逐字节复现", d3b.ok and d3b.content == d3.content)
    d2 = s.get(f"{BASE}/api/documents/{did}/versions/2/download")
    check("v2 内容已不可取（清单已删）", not d2.ok)

    # 清理：文档进回收站 → 删空间 → storage.delete 清 v1/v3 清单与块
    adm = requests.Session()
    la = adm.post(f"{BASE}/api/auth/login", json={"username": "admin", "password": "Admin@123"})
    if la.ok:
        adm.headers.update({"Authorization": f"Bearer {la.json()['token']}"})
        adm.delete(f"{BASE}/api/documents/{did}")
    s.delete(f"{BASE}/api/spaces/{sp['id']}")
    cleanup = f"""
import sys; sys.path.insert(0, '/app')
from app.storage import storage
for h in ({hashes[1]!r}, {hashes[3]!r}):
    storage.delete(h)
print("cleaned")
"""
    run_in_app(cleanup)
    print("      [Part C 测试数据已清理]")


if __name__ == "__main__":
    part_a()
    part_b()
    part_c()
    print(f"\n===== 去重测试结果：{ok} 通过 / {fail} 失败 =====")
    sys.exit(1 if fail else 0)
