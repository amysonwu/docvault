# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 DocVault Contributors
# Author: Amyson Wu
"""版本标签回归：多标签 / 去重 / 大小写 / 按标签找版本 / 权限隔离

用法：python scripts/tag_test.py [base_url]
结束时自动清理测试空间。
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
    requests.post(f"{BASE}/api/auth/register", json={"username": u, "password": p, "display_name": u})
    r = requests.post(f"{BASE}/api/auth/login", json={"username": u, "password": p})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['token']}"}


def main():
    admin, zs = tok("admin"), tok("zhangsan")

    print("\n[准备] 建空间 + 上传一份文档")
    sp = requests.post(f"{BASE}/api/spaces", headers=admin, json={"name": "_tag_空间"}).json()
    sid = sp["id"]
    f = requests.post(f"{BASE}/api/spaces/{sid}/folders", headers=admin, json={"name": "_tag_目录"}).json()
    up = requests.post(f"{BASE}/api/documents/upload", headers=admin,
                       params={"space_id": sid, "folder_id": f["id"], "note": "初始版本"},
                       files={"file": ("_tag_文档.txt", io.BytesIO("标签测试".encode()), "text/plain")})
    check("上传成功", up.ok, str(up.status_code))
    did = up.json()["id"]

    print("\n[1] 一次打多个标签")
    r = requests.post(f"{BASE}/api/documents/{did}/versions/tags", headers=admin,
                      params={"version_seq": 1, "names": "对外发布, 评审稿"}).json()
    check("一次加两个", r.get("added") == ["对外发布", "评审稿"], str(r))
    vers = requests.get(f"{BASE}/api/documents/{did}/versions", headers=admin).json()
    check("版本上带 2 个标签", sorted(vers[0]["tags"]) == sorted(["对外发布", "评审稿"]), str(vers[0]["tags"]))

    print("\n[2] 重复标签静默忽略 + 大小写不敏感")
    r = requests.post(f"{BASE}/api/documents/{did}/versions/tags", headers=admin,
                      params={"version_seq": 1, "names": "对外发布"}).json()
    check("重复标签被忽略", r.get("added") == [] and r.get("skipped") == ["对外发布"], str(r))
    requests.post(f"{BASE}/api/documents/{did}/versions/tags", headers=admin,
                  params={"version_seq": 1, "names": "Release"})
    r = requests.post(f"{BASE}/api/documents/{did}/versions/tags", headers=admin,
                      params={"version_seq": 1, "names": "release"}).json()
    check("大小写不同视为同一标签", r.get("added") == [], str(r))

    print("\n[3] 标签清单（计数正确）")
    tags = requests.get(f"{BASE}/api/tags", headers=admin).json()
    by = {t["name"]: t for t in tags}
    check("清单里有「对外发布」", "对外发布" in by, str(list(by)[:5]))
    check("版本数=1、文档数=1", by["对外发布"]["version_count"] == 1 and by["对外发布"]["doc_count"] == 1,
          str(by["对外发布"]))

    print("\n[4] 按标签找「打了该标签的版本」")
    rows = requests.get(f"{BASE}/api/tags/versions", headers=admin, params={"tag": "对外发布"}).json()
    check("命中 1 条（文档+版本）", len(rows) == 1, str(len(rows)))
    if rows:
        check("指向的是 v1", rows[0]["version"]["seq"] == 1, str(rows[0]["version"]["seq"]))
        check("标记为最新版本", rows[0].get("is_latest") is True)
        check("带回文档与空间信息",
              rows[0]["document"]["id"] == did and rows[0]["document"]["space_name"] == "_tag_空间")

    print("\n[5] 权限隔离：无权限用户看不到标签，也搜不到")
    check("zhangsan 的标签清单为空", len([t for t in requests.get(f"{BASE}/api/tags", headers=zs).json()
                                   if t["name"] == "对外发布"]) == 0)
    check("zhangsan 按标签查不到版本",
          requests.get(f"{BASE}/api/tags/versions", headers=zs, params={"tag": "对外发布"}).json() == [])

    print("\n[6] 搜索支持标签过滤")
    res = requests.get(f"{BASE}/api/documents/search", headers=admin,
                       params={"q": "", "tag": "对外发布"}).json()
    check("tag 过滤命中该文档", any(x["id"] == did for x in res), str(len(res)))
    check("搜索结果带标签字段", any(x["id"] == did and "对外发布" in x.get("tags", []) for x in res))
    res2 = requests.get(f"{BASE}/api/documents/search", headers=admin,
                        params={"q": "_tag_", "tag": "不存在的标签"}).json()
    check("不存在的标签不误命中", len(res2) == 0, str(len(res2)))

    print("\n[7] 删除标签")
    d = requests.delete(f"{BASE}/api/documents/{did}/versions/tags", headers=admin,
                        params={"version_seq": 1, "name": "评审稿"})
    check("打标签人可删除", d.ok, str(d.status_code))
    vers = requests.get(f"{BASE}/api/documents/{did}/versions", headers=admin).json()
    check("删除后不再出现", "评审稿" not in vers[0]["tags"], str(vers[0]["tags"]))
    d = requests.delete(f"{BASE}/api/documents/{did}/versions/tags", headers=zs,
                        params={"version_seq": 1, "name": "对外发布"})
    check("无权限用户删不掉", d.status_code in (403, 404), str(d.status_code))

    print("\n[清理]")
    requests.delete(f"{BASE}/api/documents/{did}", headers=admin)
    r = requests.delete(f"{BASE}/api/spaces/{sid}", headers=admin)
    check("测试空间已清理", r.ok, r.text[:60])

    print(f"\n通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项：", "；".join(FAIL))
        sys.exit(1)


if __name__ == "__main__":
    main()
