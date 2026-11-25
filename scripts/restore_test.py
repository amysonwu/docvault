# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 DocVault Contributors
# Author: Amyson Wu
"""文档修改 + 历史版本还原 专项回归

用法: python scripts/restore_test.py [BASE]   # BASE 默认 http://127.0.0.1:10001
      容器内: python scripts/restore_test.py http://edge:80

Part 1  docx：三版本链 → 逐版本历史还原 → 回滚 v1 → 历史不丢弃 → 重复回滚 409 → 版本标签
Part 2  md/txt：页面编辑三版本 → 逐版本内容还原 → 回滚 → 回滚后再编辑
Part 3  pptx：office 其他类型 两版本 → 回滚 → 部件级还原
Part 4  回收站：删除 → 不可访问 → 恢复 → 全部版本仍可还原
Part 5  存储一致性：回滚零物理拷贝（CAS 复用）→ GC 幂等 → 清理
"""
import io
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import requests

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:10001"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 直连 DB / app 模块需要

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {name}")
    else:
        fail += 1
        print(f"  ❌ {name}  {extra}")


def run_in_app(code: str):
    """本进程即 app 环境（脚本与 app 同仓运行）：直接执行片段"""
    ns = {"__name__": "__snippet__"}
    exec(compile(code, "<snippet>", "exec"), ns)
    return None


def run_gc():
    root = Path(__file__).resolve().parent.parent
    return subprocess.run([sys.executable, "scripts/gc_storage.py"], cwd=root,
                          capture_output=True, text=True)


def sql(q):
    from sqlalchemy import text
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        return s.execute(text(q)).fetchall()
    finally:
        s.close()


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


def make_pptx(lines):
    from pptx import Presentation
    from pptx.util import Pt
    p = Presentation()
    slide = p.slides.add_slide(p.slide_layouts[5])     # 标题版式
    slide.shapes.title.text = lines[0]
    box = slide.shapes.add_textbox(914400, 1828800, 5486400, 2743200)
    tf = box.text_frame
    tf.word_wrap = True
    for i, t in enumerate(lines[1:]):
        para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        run = para.add_run()
        run.text = t
        run.font.size = Pt(18)
    buf = io.BytesIO()
    p.save(buf)
    return buf.getvalue()


def pptx_texts(data: bytes):
    from pptx import Presentation
    p = Presentation(io.BytesIO(data))
    out = []
    for slide in p.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                out += [para.text for para in shape.text_frame.paragraphs if para.text.strip()]
    return out


# =====================================================================
def main():
    print(f"== 文档修改 + 历史版本还原回归（BASE={BASE}）==")
    tag = f"restore_{int(time.time())}"
    s = requests.Session()
    s.post(f"{BASE}/api/auth/register",
           json={"username": tag, "password": "restore123", "display_name": "还原测试"})
    r = s.post(f"{BASE}/api/auth/login", json={"username": tag, "password": "restore123"})
    check("注册并登录测试用户", r.ok, r.text[:120])
    s.headers.update({"Authorization": f"Bearer {r.json()['token']}"})
    adm = requests.Session()
    la = adm.post(f"{BASE}/api/auth/login", json={"username": "admin", "password": "Admin@123"})
    adm.headers.update({"Authorization": f"Bearer {la.json()['token']}"})

    sp = s.post(f"{BASE}/api/spaces", json={"name": f"_{tag}"}).json()
    manifests = []   # 收集所有清单哈希，收尾清理

    def track(doc_id):
        rows = sql(f"SELECT blob_hash FROM document_versions WHERE document_id={doc_id} ORDER BY seq")
        manifests.extend(x[0] for x in rows)

    # ---------------- Part 1: docx ----------------
    print("\n== Part 1. docx 三版本链 + 历史还原 + 回滚 ==")
    p1 = ["还原验证文档", "第一章 背景说明", "初始预算 100 万元。", "结尾备注：无。"]
    p2 = ["还原验证文档", "第一章 背景说明", "预算调整为 120 万元。", "结尾备注：无。"]
    p3 = ["还原验证文档", "第二章 新增章节", "预算调整为 120 万元。", "结尾备注：有待评审。"]
    up = s.post(f"{BASE}/api/documents/upload?space_id={sp['id']}",
                files={"file": ("还原测试.docx", make_docx(p1), "application/octet-stream")})
    check("上传 v1", up.ok, up.text[:150])
    did = up.json()["id"]
    s.post(f"{BASE}/api/documents/{did}/lock")
    for note, paras in (("修改预算", p2), ("新增章节", p3)):
        c = s.post(f"{BASE}/api/documents/{did}/versions/commit?note={note}",
                   files={"file": ("还原测试.docx", make_docx(paras), "application/octet-stream")})
        check(f"提交新版本（{note}）", c.ok, c.text[:150])

    hist = s.get(f"{BASE}/api/documents/{did}/versions").json()
    check("版本历史共 3 条且来源正确",
          [v["seq"] for v in hist] == [3, 2, 1]
          and hist[2]["source"] == "upload" and hist[1]["source"] == "edit",
          str([(v["seq"], v["source"]) for v in hist]))
    d1 = s.get(f"{BASE}/api/documents/{did}/versions/1/download")
    d2 = s.get(f"{BASE}/api/documents/{did}/versions/2/download")
    d3 = s.get(f"{BASE}/api/documents/{did}/versions/3/download")
    check("三个历史版本均可下载", d1.ok and d2.ok and d3.ok)
    check("v1 历史还原逐段落保真", docx_paragraphs(d1.content) == p1,
          str(docx_paragraphs(d1.content))[:120])
    check("v2 历史还原逐段落保真", docx_paragraphs(d2.content) == p2)
    check("v3 历史还原逐段落保真", docx_paragraphs(d3.content) == p3)
    diff = s.get(f"{BASE}/api/documents/{did}/versions/diff?old=1&new=3")
    check("任意两版本结构化 diff 可用", diff.ok, diff.text[:120])

    rb = s.post(f"{BASE}/api/documents/{did}/versions/rollback?target_seq=1")
    check("回滚到 v1 成功", rb.ok, rb.text[:150])
    check("回滚生成 v4 且来源=rollback", rb.json().get("seq") == 4
          and rb.json().get("source") == "rollback", rb.text[:150])
    rows = sql(f"SELECT seq, blob_hash FROM document_versions WHERE document_id={did} ORDER BY seq")
    check("v4 与 v1 共享同一清单哈希（CAS 零拷贝）", rows[0][1] == rows[3][1])
    d4 = s.get(f"{BASE}/api/documents/{did}/versions/4/download")
    check("v4 内容 == v1 原始内容", docx_paragraphs(d4.content) == p1)
    d1b = s.get(f"{BASE}/api/documents/{did}/versions/1/download")
    d2b = s.get(f"{BASE}/api/documents/{did}/versions/2/download")
    check("回滚不丢弃历史：v1/v2 仍可还原", d1b.ok and d2b.ok
          and docx_paragraphs(d1b.content) == p1 and docx_paragraphs(d2b.content) == p2)

    tg = s.post(f"{BASE}/api/documents/{did}/versions/tags?version_seq=1&name=里程碑")
    hist2 = s.get(f"{BASE}/api/documents/{did}/versions").json()
    v1out = next(v for v in hist2 if v["seq"] == 1)
    check("版本标签正常挂载在 v1", tg.ok and "里程碑" in v1out["tags"], tg.text[:120])

    rb409 = s.post(f"{BASE}/api/documents/{did}/versions/rollback?target_seq=4")
    check("回滚到当前最新版本被拒（409）", rb409.status_code == 409, rb409.text[:120])
    rb404 = s.post(f"{BASE}/api/documents/{did}/versions/rollback?target_seq=99")
    check("回滚到不存在版本被拒（404）", rb404.status_code == 404)
    track(did)

    # ---------------- Part 2: md ----------------
    print("\n== Part 2. md 页面编辑 + 历史还原 + 回滚 ==")
    cr = s.post(f"{BASE}/api/documents/create?space_id={sp['id']}",
                json={"name": "还原验证笔记", "ext": ".md"})
    check("创建 md 文档", cr.ok, cr.text[:150])
    mid = cr.json()["id"]
    c1 = "# 笔记\n\n第一版内容。"
    c2 = "# 笔记\n\n第一版内容。\n\n第二版补充。"
    c3 = "# 笔记\n\n第三版重写。"
    for note, content in (("初稿", c1), ("补充", c2), ("重写", c3)):
        sv = s.post(f"{BASE}/api/documents/{mid}/save_text",
                    json={"content": content, "note": note})
        check(f"save_text 提交（{note}）", sv.ok, sv.text[:150])
    # 创建时会自动生成模板 v1，save_text 从 v2 起 → 按 note 定位版本号
    seq_of = {v["note"]: v["seq"] for v in s.get(f"{BASE}/api/documents/{mid}/versions").json()}
    m1 = s.get(f"{BASE}/api/documents/{mid}/versions/{seq_of['初稿']}/download")
    m2 = s.get(f"{BASE}/api/documents/{mid}/versions/{seq_of['补充']}/download")
    m3 = s.get(f"{BASE}/api/documents/{mid}/versions/{seq_of['重写']}/download")
    check("md 三个历史版本内容逐字还原",
          m1.content.decode("utf-8") == c1 and m2.content.decode("utf-8") == c2
          and m3.content.decode("utf-8") == c3,
          repr(m1.content[:80]))
    mrb = s.post(f"{BASE}/api/documents/{mid}/versions/rollback?target_seq={seq_of['初稿']}")
    check("md 回滚到初稿", mrb.ok, mrb.text[:150])
    rb_seq = mrb.json().get("seq")
    m4 = s.get(f"{BASE}/api/documents/{mid}/versions/{rb_seq}/download")
    check("回滚版本内容 == 初稿", m4.content.decode("utf-8") == c1)
    c5 = "# 笔记\n\n回滚后继续编辑。"
    sv5 = s.post(f"{BASE}/api/documents/{mid}/save_text",
                 json={"content": c5, "note": "回滚后继续"})
    v5_seq = sv5.json().get("seq")
    m5 = s.get(f"{BASE}/api/documents/{mid}/versions/{v5_seq}/download")
    check("回滚后可继续编辑出新版本", sv5.ok and m5.ok
          and m5.content.decode("utf-8") == c5)
    mhist = sorted(s.get(f"{BASE}/api/documents/{mid}/versions").json(),
                   key=lambda x: x["seq"])
    check("md 版本链完整（模板+3 编辑+回滚+续编，连续无缺口，含 rollback 来源）",
          [v["seq"] for v in mhist] == list(range(1, v5_seq + 1))
          and any(v["source"] == "rollback" for v in mhist),
          str([(v["seq"], v["source"]) for v in mhist]))
    track(mid)

    # ---------------- Part 3: pptx ----------------
    print("\n== Part 3. pptx 回滚与内容还原 ==")
    pp1 = ["还原测试演示", "要点一：初始内容"]
    pp2 = ["还原测试演示", "要点一：已修改内容"]
    upp = s.post(f"{BASE}/api/documents/upload?space_id={sp['id']}",
                 files={"file": ("还原测试.pptx", make_pptx(pp1), "application/octet-stream")})
    check("上传 pptx v1", upp.ok, upp.text[:150])
    pid = upp.json()["id"]
    cp = s.post(f"{BASE}/api/documents/{pid}/versions/commit?note=改要点",
                files={"file": ("还原测试.pptx", make_pptx(pp2), "application/octet-stream")})
    check("提交 pptx v2", cp.ok, cp.text[:150])
    prb = s.post(f"{BASE}/api/documents/{pid}/versions/rollback?target_seq=1")
    check("pptx 回滚到 v1", prb.ok, prb.text[:150])
    t1 = pptx_texts(s.get(f"{BASE}/api/documents/{pid}/versions/1/download").content)
    t3 = pptx_texts(s.get(f"{BASE}/api/documents/{pid}/versions/3/download").content)
    check("pptx v1/v3（回滚后）文本还原一致",
          t1 == pp1 and t3 == pp1, f"v1={t1} v3={t3}")
    t2 = pptx_texts(s.get(f"{BASE}/api/documents/{pid}/versions/2/download").content)
    check("pptx v2 历史仍还原为修改后内容", t2 == pp2, f"v2={t2}")
    track(pid)

    # ---------------- Part 4: 回收站 ----------------
    print("\n== Part 4. 回收站删除/恢复后版本完整 ==")
    dele = adm.delete(f"{BASE}/api/documents/{did}")
    check("管理员删除文档进回收站", dele.ok, dele.text[:150])
    gone = s.get(f"{BASE}/api/documents/{did}/versions")
    check("回收站中文档不可访问", gone.status_code in (403, 404), str(gone.status_code))
    rest = adm.post(f"{BASE}/api/documents/{did}/restore")
    check("管理员恢复文档", rest.ok, rest.text[:150])
    d1c = s.get(f"{BASE}/api/documents/{did}/versions/1/download")
    d3c = s.get(f"{BASE}/api/documents/{did}/versions/3/download")
    d4c = s.get(f"{BASE}/api/documents/{did}/versions/4/download")
    check("恢复后全部版本仍可还原",
          d1c.ok and d3c.ok and d4c.ok
          and docx_paragraphs(d1c.content) == p1
          and docx_paragraphs(d3c.content) == p3
          and docx_paragraphs(d4c.content) == p1)
    h3 = s.get(f"{BASE}/api/documents/{did}/versions").json()
    check("恢复后版本历史完整（4 条）", len(h3) == 4)

    # ---------------- Part 5: 存储一致性 + 清理 ----------------
    print("\n== Part 5. 存储一致性与收尾 ==")
    g1 = run_gc()
    g2 = run_gc()
    check("GC 幂等两连跑", g1.returncode == 0 and g2.returncode == 0,
          (g1.stderr or g1.stdout or "")[-200:])

    # 清理测试数据：文档 → 空间 → 清单/块
    for x in (did, mid, pid):
        adm.delete(f"{BASE}/api/documents/{x}")
    s.delete(f"{BASE}/api/spaces/{sp['id']}")
    hs = ", ".join(f"'{h}'" for h in set(manifests))
    if hs:
        run_in_app(f"""
from app.storage import storage
for h in [{', '.join(repr(h) for h in set(manifests))}]:
    try:
        storage.delete(h)
    except Exception as e:
        print("del fail", e)
print("manifests cleaned: {len(set(manifests))}")
""")
    run_gc()
    left = sql(f"SELECT COUNT(*) FROM storage_manifests WHERE file_hash IN ({hs})") if hs else [(0,)]
    check("测试清单已全部回收", left[0][0] == 0, str(left))
    print(f"\n===== 修改/还原回归结果：{ok} 通过 / {fail} 失败 =====")
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
