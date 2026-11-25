# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 DocVault Contributors
# Author: Amyson Wu
"""用户组回归：多级树 / 一人多组 / 目录与文档按组授权

用法：python scripts/group_test.py [base_url]
结束时自动清理测试空间与测试用户组（需 admin）。
"""
import io
import sys

import requests

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:10001"
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  [{extra}]" if extra else ""))


def tok(u, p="Admin@123"):
    r = requests.post(f"{BASE}/api/auth/login", json={"username": u, "password": p})
    if r.ok:
        return {"Authorization": f"Bearer {r.json()['token']}"}
    requests.post(f"{BASE}/api/auth/register",
                  json={"username": u, "password": p, "display_name": u})
    r = requests.post(f"{BASE}/api/auth/login", json={"username": u, "password": p})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['token']}"}


def me(tk):
    return requests.get(f"{BASE}/api/auth/me", headers=tk).json()["id"]


def perm_of(tk, node, nid):
    r = requests.get(f"{BASE}/api/perms/{node}/{nid}", headers=tk)
    return r.json() if r.ok else {"my": {}}


def content_of(tk, node, nid):
    return (perm_of(tk, node, nid).get("my") or {}).get("content")


def source_of(tk, node, nid):
    return (perm_of(tk, node, nid).get("my") or {}).get("source")


def main():
    admin = tok("admin")
    zs, ls, ww = tok("zhangsan"), tok("lisi"), tok("wangwu")
    uid = {k: me(v) for k, v in (("admin", admin), ("zhangsan", zs), ("lisi", ls), ("wangwu", ww))}

    print("\n[1] 用户组多级树")
    # 清掉上次异常退出残留的测试组
    for g in requests.get(f"{BASE}/api/groups", headers=admin).json()["groups"]:
        if g["name"].startswith("_gt_") or g["name"].startswith("_dbg"):
            requests.delete(f"{BASE}/api/groups/{g['id']}", headers=admin)
    root = requests.post(f"{BASE}/api/groups", headers=admin,
                         json={"name": "_gt_研发中心"}).json()
    back = requests.post(f"{BASE}/api/groups", headers=admin,
                         json={"name": "_gt_后端组", "parent_id": root["id"]}).json()
    front = requests.post(f"{BASE}/api/groups", headers=admin,
                          json={"name": "_gt_前端组", "parent_id": root["id"]}).json()
    check("创建父组", bool(root.get("id")))
    check("创建子组（多级）", bool(back.get("id")) and bool(front.get("id")))
    tree = requests.get(f"{BASE}/api/groups", headers=admin).json()["groups"]
    d = {g["id"]: g for g in tree}
    check("子组挂在父组下", d[back["id"]]["parent_id"] == root["id"])
    check("层级深度正确", d[back["id"]]["depth"] == 1, f"depth={d[back['id']]['depth']}")

    print("\n[2] 一人多组")
    requests.post(f"{BASE}/api/groups/{back['id']}/members", headers=admin, json={"user_id": uid["zhangsan"]})
    requests.post(f"{BASE}/api/groups/{front['id']}/members", headers=admin, json={"user_id": uid["wangwu"]})
    requests.post(f"{BASE}/api/groups/{front['id']}/members", headers=admin, json={"user_id": uid["zhangsan"]})
    gb = requests.get(f"{BASE}/api/groups/{back['id']}", headers=admin).json()
    gf = requests.get(f"{BASE}/api/groups/{front['id']}", headers=admin).json()
    check("zhangsan 在 后端组", any(m["user_id"] == uid["zhangsan"] for m in gb["members"]))
    check("zhangsan 同时在 前端组", any(m["user_id"] == uid["zhangsan"] for m in gf["members"]))
    check("wangwu 只在 前端组", any(m["user_id"] == uid["wangwu"] for m in gf["members"]))

    print("\n[3] 目录授权给用户组（含子组成员）")
    sp = requests.post(f"{BASE}/api/spaces", headers=admin, json={"name": "_gt_空间"}).json()
    sid = sp["id"]
    f1 = requests.post(f"{BASE}/api/spaces/{sid}/folders", headers=admin, json={"name": "_gt_目录"}).json()
    f2 = requests.post(f"{BASE}/api/spaces/{sid}/folders", headers=admin,
                       json={"name": "_gt_子目录", "parent_id": f1["id"]}).json()
    r = requests.post(f"{BASE}/api/perms/folder/{f1['id']}", headers=admin,
                      json={"subject_type": "group", "subject_id": root["id"], "content": "write"})
    check("授权给父组", r.ok, r.text[:80])
    check("后端组成员继承到写权", content_of(zs, "folder", f1["id"]) == "write",
          source_of(zs, "folder", f1["id"]))
    check("前端组成员继承到写权", content_of(ww, "folder", f1["id"]) == "write")
    check("无关用户仍无权限", content_of(ls, "folder", f1["id"]) is None)
    entries = perm_of(admin, "folder", f1["id"])["entries"]
    check("授权列表里能看到组条目", any(e["subject_type"] == "group" for e in entries))
    names = [s["name"] for s in requests.get(f"{BASE}/api/spaces", headers=zs).json()]
    check("组员非空间成员也能在左侧看到该空间", "_gt_空间" in names, str(names))
    rows = requests.get(f"{BASE}/api/spaces/{sid}/folders", headers=zs).json()
    check("组员能在目录树里看到被授权的目录", any(r["id"] == f1["id"] for r in rows))

    print("\n[4] 组成员按组权限上传 / 编辑")
    files = {"file": ("_gt_文档.txt", io.BytesIO("hello 用户组".encode()), "text/plain")}
    up = requests.post(f"{BASE}/api/documents/upload", headers=zs,
                       params={"space_id": sid, "folder_id": f1["id"]}, files=files)
    check("组成员可上传到该目录", up.ok, str(up.status_code))
    did = up.json().get("id") if up.ok else None
    if did:
        def doc_of(tk):
            r = requests.get(f"{BASE}/api/documents?space_id={sid}&folder_id={f1['id']}", headers=tk)
            return next((x for x in r.json() if x["id"] == did), {})
        check("上传者（组员）对文档可写", doc_of(zs).get("my_content") == "write")
        check("另一组员对文档同样可写", doc_of(ww).get("my_content") == "write")
        check("无关用户看不到该文档", doc_of(ls).get("id") is None)

    print("\n[5] 文档级组授权就近覆盖（组读 < 目录写）")
    requests.post(f"{BASE}/api/perms/document/{did}", headers=admin,
                  json={"subject_type": "group", "subject_id": front["id"], "content": "read"})
    check("前端组成员在文档上降为只读", content_of(ww, "document", did) == "read",
          source_of(ww, "document", did))
    check("同时在两组的成员也按文档级只读（多组取该文档的显式授权）",
          content_of(zs, "document", did) == "read")
    requests.delete(f"{BASE}/api/perms/document/{did}/group/{front['id']}", headers=admin)
    check("撤销文档组授权后恢复目录写权",
          content_of(ww, "document", did) == "write" and content_of(zs, "document", did) == "write")

    print("\n[6] 目录权限不向上/向旁扩散")
    check("子目录未授权前：组员不可见", content_of(ww, "folder", f2["id"]) is None)
    requests.post(f"{BASE}/api/perms/folder/{f2['id']}", headers=admin,
                  json={"subject_type": "group", "subject_id": back["id"], "content": "read"})
    check("子目录单独授权给后端组 → 只读", content_of(zs, "folder", f2["id"]) == "read")
    check("未在该组的成员仍无权限", content_of(ww, "folder", f2["id"]) is None)

    print("\n[7] 删除组 = 撤销该组全部授权")
    requests.delete(f"{BASE}/api/groups/{root['id']}", headers=admin)
    check("父组删除后子组一并删除",
          requests.get(f"{BASE}/api/groups/{back['id']}", headers=admin).status_code == 404)
    check("原组成员失去目录写权", content_of(zs, "folder", f1["id"]) is None)

    print("\n[清理]")
    if did:
        requests.delete(f"{BASE}/api/documents/{did}", headers=admin)   # 先移入回收站
    r = requests.delete(f"{BASE}/api/spaces/{sid}", headers=admin)
    for g in requests.get(f"{BASE}/api/groups", headers=admin).json()["groups"]:
        if g["name"].startswith("_gt_"):
            requests.delete(f"{BASE}/api/groups/{g['id']}", headers=admin)
    check("测试空间与测试组已清理", r.ok, r.text[:80])

    print(f"\n通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项：", "；".join(FAIL))
        sys.exit(1)


if __name__ == "__main__":
    main()
