# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 DocVault Contributors
# Author: Amyson Wu
"""权限矩阵回归：按规划验证 4 类角色、继承/覆盖、写含读、不放大、提权、删除限制

用法：python scripts/perm_test.py [base_url]
结束时自动清理测试空间（需 admin）。
"""
import sys
import requests

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  [{extra}]" if extra else ""))


def login(u):
    """登录；全新安装上用户可能不存在，先注册（首个注册者会成为系统管理员）"""
    r = requests.post(f"{BASE}/api/auth/login", json={"username": u, "password": "Admin@123"})
    if not r.ok:
        requests.post(f"{BASE}/api/auth/register",
                      json={"username": u, "password": "Admin@123", "display_name": u})
        r = requests.post(f"{BASE}/api/auth/login", json={"username": u, "password": "Admin@123"})
    r.raise_for_status()
    t = r.json()["token"]
    return {"Authorization": f"Bearer {t}"}


def perm(token, node_type, node_id):
    r = requests.get(f"{BASE}/api/perms/{node_type}/{node_id}", headers=token)
    return r.status_code, (r.json() if r.ok else r.text)


def grant(token, node_type, node_id, uid, **kw):
    return requests.post(f"{BASE}/api/perms/{node_type}/{node_id}",
                         headers=token, json={"user_id": uid, **kw})


def main():
    admin, lisi, zs, ww = login("admin"), login("lisi"), login("zhangsan"), login("wangwu")
    uid = {}
    for name, tk in (("admin", admin), ("lisi", lisi), ("zhangsan", zs), ("wangwu", ww)):
        me = requests.get(f"{BASE}/api/auth/me", headers=tk).json()
        uid[name] = me["id"]

    print("\n[准备] lisi 创建测试空间 / 目录 / 文档")
    sp = requests.post(f"{BASE}/api/spaces", headers=lisi, json={"name": "_perm_test_space"}).json()
    sid = sp["id"]
    f1 = requests.post(f"{BASE}/api/spaces/{sid}/folders", headers=lisi,
                       json={"name": "_shared", "parent_id": None}).json()
    f1id = f1["id"]
    doc = requests.post(f"{BASE}/api/documents/create", headers=lisi,
                        params={"space_id": sid}, json={"name": "_perm_doc", "ext": ".md",
                                                       "folder_id": f1id}).json()
    did = doc["id"]
    print(f"  space={sid} folder={f1id} doc={did}")

    print("\n[1] 创建者自动赋权（空间/目录/文档）")
    st, d = perm(lisi, "space", sid)
    check("lisi 是空间管理员+可写", st == 200 and d["my"]["content"] == "write" and d["my"]["is_admin"])
    st, d = perm(lisi, "document", did)
    check("lisi 对文档可写+管理员", st == 200 and d["my"]["content"] == "write" and d["my"]["is_admin"])

    print("\n[2] 未授权即无权限（zhangsan 不在该空间）")
    r = requests.get(f"{BASE}/api/spaces", headers=zs)
    check("zhangsan 的空间列表无该空间", all(s["id"] != sid for s in r.json()))
    st, d = perm(zs, "document", did)
    check("zhangsan 读不到该文档", st == 404)

    print("\n[3] 授读 → 沿空间继承到目录与文档（向下继承）")
    check("lisi 给 zhangsan 授空间读", grant(lisi, "space", sid, uid["zhangsan"], content="read").ok)
    st, d = perm(zs, "document", did)
    check("zhangsan 继承得到文档读", st == 200 and d["my"]["content"] == "read")
    check("zhangsan 无写权（写需另授）", st == 200 and not d["my"]["can_write"])

    print("\n[4] 授写自动含读 + 就近覆盖")
    check("给 zhangsan 授目录写", grant(lisi, "folder", f1id, uid["zhangsan"], content="write").ok)
    st, d = perm(zs, "document", did)
    check("文档层继承目录写（含读）", st == 200 and d["my"]["content"] == "write")

    print("\n[5] 子目录收紧（父写、子读 → 子只读）")
    f2 = requests.post(f"{BASE}/api/spaces/{sid}/folders", headers=lisi,
                       json={"name": "_secret", "parent_id": f1id}).json()
    f2id = f2["id"]
    d2 = requests.post(f"{BASE}/api/documents/create", headers=lisi,
                       params={"space_id": sid}, json={"name": "_secret_doc", "ext": ".md",
                                                      "folder_id": f2id}).json()
    d2id = d2["id"]
    grant(lisi, "folder", f2id, uid["zhangsan"], content="read")
    st, d = perm(zs, "document", d2id)
    check("子目录收紧生效：zhangsan 只读", st == 200 and d["my"]["content"] == "read")
    st, d = perm(zs, "document", did)
    check("父目录仍为可写", st == 200 and d["my"]["content"] == "write")

    print("\n[6] 私有目录（未受邀不可见，不继承空间权限）")
    f3 = requests.post(f"{BASE}/api/spaces/{sid}/folders", headers=lisi,
                       json={"name": "_private", "parent_id": None}).json()
    f3id = f3["id"]
    requests.patch(f"{BASE}/api/folders/{f3id}", headers=lisi, json={"is_private": True})
    d3 = requests.post(f"{BASE}/api/documents/create", headers=lisi,
                       params={"space_id": sid}, json={"name": "_private_doc", "ext": ".md",
                                                      "folder_id": f3id}).json()
    st, d = perm(zs, "document", d3["id"])
    check("zhangsan 看不到 lisi 的私有目录文档", st == 404)
    st, d = perm(lisi, "document", d3["id"])
    check("lisi 自己可见可写", st == 200 and d["my"]["content"] == "write")

    print("\n[7] 权限不放大")
    r = grant(zs, "document", did, uid["wangwu"], content="write")
    check("内容操作员无权授权（403）", r.status_code == 403, r.text[:60])
    r = grant(lisi, "document", did, uid["wangwu"], content="write")
    check("文档管理员可授权", r.ok)

    print("\n[8] 系统管理员不介入内容 + 临时提权")
    st, d = perm(admin, "document", did)
    check("admin 对他人文档只读", st == 200 and d["my"]["content"] == "read", d["my"]["source"])
    check("admin 拥有管理权（管人不越界）", st == 200 and d["my"]["is_admin"])
    r = requests.post(f"{BASE}/api/admin/elevate/{did}", headers=admin,
                      params={"hours": 1, "content": "write"})
    check("admin 临时提权成功", r.ok)
    st, d = perm(admin, "document", did)
    check("提权后可写", st == 200 and d["my"]["content"] == "write" and d["my"]["elevated"])
    requests.delete(f"{BASE}/api/perms/document/{did}/{uid['admin']}", headers=admin)

    print("\n[9] 删除属结构操作，仅系统管理员")
    r = requests.delete(f"{BASE}/api/documents/{d2id}", headers=zs)
    check("内容操作员删除被拒（403）", r.status_code == 403, r.text[:50])
    r = requests.delete(f"{BASE}/api/documents/{d2id}", headers=admin)
    check("系统管理员可删除", r.ok)

    print("\n[清理] 删除测试空间（先清空在用文档，回收站随空间彻底清理）")
    folders = requests.get(f"{BASE}/api/spaces/{sid}/folders", headers=admin).json()
    for fid in [None] + [f["id"] for f in folders]:
        params = {"space_id": sid}
        if fid:
            params["folder_id"] = fid
        for d in requests.get(f"{BASE}/api/documents", headers=admin, params=params).json():
            requests.delete(f"{BASE}/api/documents/{d['id']}", headers=admin)
    r = requests.delete(f"{BASE}/api/spaces/{sid}", headers=admin)
    check("测试空间已清理", r.ok and all(s["id"] != sid for s in
                                   requests.get(f"{BASE}/api/spaces", headers=admin).json()),
          r.text[:60])

    print(f"\n通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项：", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
