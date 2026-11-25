# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 DocVault Contributors
# Author: Amyson Wu
"""块级切分与清单哈希（存储去重的算法层）

- Office 容器切分：docx/xlsx/pptx/ODF 按 zip 内部件解出内容，每个部件一律再过
  CDC（小部件自然退化为单块），部件名在清单中连续出现，重组时按名聚合；
- CDC 内容定义分块：Gear hash 滚动窗口，边界由内容决定，
  插入/删除只影响局部块，任意格式兜底；
- 清单哈希：版本的逻辑指纹（DocumentVersion.blob_hash）。

确定性要求：GEAR 表与两个掩码由固定种子生成、跨进程/跨机器完全一致——
同一内容必须得到同一套边界，否则新旧版本无法共享块。不得改为运行时随机。
"""
import hashlib
import random
import zipfile

MASK64 = 0xFFFFFFFFFFFFFFFF

# ---- 固定种子的确定性常量（GEAR 256 项 + 两个掩码）----
_RNG = random.Random(0xD0C9A717)
GEAR = tuple(_RNG.getrandbits(64) & MASK64 for _ in range(256))


def _make_mask(rng: random.Random, bits: int) -> int:
    m = 0
    for p in rng.sample(range(64), bits):
        m |= 1 << p
    return m


MASK_EARLY = _make_mask(_RNG, 16)   # MIN→AVG 区：期望 ~64KB 出一个切点
MASK_LATE = _make_mask(_RNG, 13)    # AVG→MAX 区：收紧，~8KB 内必切

CDC_MIN = 16 * 1024
CDC_AVG = 64 * 1024
CDC_MAX = 256 * 1024

OFFICE_EXTS = {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---- L2：Gear CDC ----
def cdc_chunks(buf: bytes):
    """把字节流按内容定义边界切块（FastCDC 思路 + min-hash 锚定的强制切）。

    返回有序 bytes 列表；块间不重叠、拼接后等于原文。
    """
    chunks = []
    n = len(buf)
    start = 0
    gear = GEAR
    while start < n:
        remaining = n - start
        if remaining <= CDC_MIN:
            chunks.append(buf[start:])
            break
        end = min(start + CDC_MAX, n)
        split = end            # 兜底：滚到 MAX 强制切（下方可能被 min-hash 锚点改写）
        tail_only = end < start + CDC_MAX   # 尾段不足 MAX：天然到头，无需锚定
        h = 0
        min_h = None
        min_pos = end - 1
        for i in range(start, end):
            h = ((h << 1) + gear[buf[i]]) & MASK64
            size = i - start + 1
            if size < CDC_MIN:
                continue
            if size < CDC_AVG:
                if h & MASK_EARLY == 0:
                    split = i + 1
                    break
            else:
                if h & MASK_LATE == 0:
                    split = i + 1
                    break
                # LATE 区全程跟踪滚哈希最小值，供强制切锚定内容
                if min_h is None or h < min_h:
                    min_h = h
                    min_pos = i
        if split == end and not tail_only and min_h is not None:
            split = min_pos + 1   # 强制切 → 锚定内容（切点 ≥ start+AVG，≥MIN 有保障）
        chunks.append(buf[start:split])
        start = split
    return chunks


# ---- L1：Office 容器 ----
def split_office(src):
    """按 zip 内部件解出内容，**每个部件一律过 CDC**，返回有序 item 列表。

    - 部件按名排序（保证清单确定性）；同一部件的多个块以相同 name、
      连续 seq 表示，重组时按名聚合拼接。
    - 哈希取解压内容的块——zip 头的时间戳/CRC 等易变元数据不参与。
    - 小部件（< CDC_MIN）CDC 自然退化为单块，行为与旧"整存"完全一致；
      大部件按内容切，编辑只重存编辑点附近的块。
    """
    items = []
    with zipfile.ZipFile(src) as z:
        for info in sorted(z.infolist(), key=lambda i: i.filename):
            if info.is_dir():
                continue
            data = z.read(info.filename)
            for piece in cdc_chunks(data):
                items.append({"name": info.filename, "item_hash": sha256_bytes(piece),
                              "item_size": len(piece), "data": piece})
    return items


def assemble_office(items, dst, read_chunk):
    """按清单重组 zip：**连续同名 item 聚合为一个部件**，块拼接后写回。

    参数全固定 → 同一清单在任何机器重组出字节级相同的结果。
    read_chunk(item_hash, expect_size) -> bytes（读块并校验）。
    重组结果解压内容与原件逐部件相同；zip 头元数据归一化（1980-01-01）。
    兼容旧清单（一部件一 item，单 item 组聚合即原部件）。
    """
    groups = []   # [(name, [item, ...]), ...] —— 连续同名聚合
    for it in items:
        if groups and groups[-1][0] == it.get("name"):
            groups[-1][1].append(it)
        else:
            groups.append((it.get("name"), [it]))
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        for name, its in groups:
            zi = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            zi.create_system = 0
            zi.external_attr = 0o600 << 16
            data = b"".join(read_chunk(it["item_hash"], it["item_size"]) for it in its)
            z.writestr(zi, data, compresslevel=6)


# ---- 清单哈希（office/chunks 的逻辑指纹）----
def manifest_hash(items) -> str:
    """清单哈希：长度前缀拼接消除歧义，跨机器确定性。

    注意：raw 类不用此函数（保持整文件 SHA-256 原值，兼容既有语义）。
    """
    h = hashlib.sha256()
    for it in items:
        name = (it.get("name") or "").encode("utf-8")
        h.update(f"{len(name)}:".encode())
        h.update(name)
        h.update(f":{it['item_size']}:{it['item_hash']};".encode())
    return h.hexdigest()
