# 贡献指南

感谢你愿意让 DocVault 变得更好。以下约定都很短，读完大约 3 分钟。

## 行为准则

参与本项目即表示你同意遵守 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。

## 先讨论，再写码

- **Bug**：直接开 Issue，附版本、部署方式、复现步骤、日志（脱敏）。
- **新功能**：先开 Issue 说明场景与预期，等维护者确认方向后再动手，避免白做。
- **安全问题**：**不要**开公开 Issue，见 [SECURITY.md](SECURITY.md)。

## 开发环境

```bash
# 依赖：Python 3.13、Docker（跑数据库与 OnlyOffice 可选）
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 最小启动（需要可用的 MySQL 8，或用 deploy/ 下的 compose 起一个）
export DOCVault_DB_HOST=127.0.0.1 DOCVault_DB_PORT=3306
export DOCVault_DB_USER=docvault DOCVault_DB_PASS=****** DOCVault_DB_NAME=docvault
uvicorn app.main:app --reload --port 8000
```

完整栈（app + db + OnlyOffice + nginx）：

```bash
cd deploy && cp .env.example .env && docker compose up -d --build
```

> 首个注册的账号自动成为系统管理员。在线编辑需要 OnlyOffice；只做后端开发时可以不带。

## 分支与提交

| 项 | 约定 |
|---|---|
| 分支 | `feat/<简述>`、`fix/<简述>`、`docs/<简述>`；主干为 `main` |
| 提交信息 | 采用 Conventional Commits：`feat: 支持按标签反查版本`、`fix(authz): 修正组授权跨空间越权` |
| 粒度 | 一个 PR 只做一件事；格式化改动单独提交 |
| 合并 | Squash merge；提交信息用它描述「为什么」而非「改了哪几行」 |

## 代码约定

- **Python**：PEP 8 风格，行宽 110；类型标注尽量齐全；业务错误用 `HTTPException` 并给出**可读中文提示**（前端直接展示）。
- **权限相关改动**：必须走 `app/authz.py` 的判定函数，**不要**在路由里另写 if 判断。权限是这个项目最容易出越权的地方，集中在一处才好查。
- **前端**：单文件 Vue 3（无打包器），改动后刷新即生效；新增图标请用 Lucide（ISC）并内联 SVG，不要引外部 CDN。
- **数据库**：新表/字段请在 `app/models.py` 定义，并说明是否需要迁移脚本；不要写会在生产静默失败的 DDL。
- **依赖**：新增依赖需在 PR 中说明理由、许可类型（必须为宽松许可）与体积影响。

## 测试

提交前至少跑通与改动相关的回归脚本（都只依赖 `requests`，直接打 HTTP 接口）：

```bash
python scripts/smoke_test.py http://127.0.0.1:10001   # 冒烟
python scripts/perm_test.py  http://127.0.0.1:10001   # 权限矩阵（改权限必跑）
python scripts/group_test.py http://127.0.0.1:10001   # 用户组
python scripts/tag_test.py   http://127.0.0.1:10001   # 版本标签
python scripts/restore_test.py http://127.0.0.1:10001 # 版本还原
python scripts/dedup_test.py   http://127.0.0.1:10001 # 块级去重（需在同仓目录跑，会直连 DB 校验）
```

CI 会把这些套件全跑一遍（`.github/workflows/regression.yml`，真 MySQL + 真 HTTP），PR 必须全绿才能合并。

脚本会自建 `_xxx_` 前缀的临时空间与用户并在结束时清理；若中途失败，请手动清理残留再重跑。

> 脚本需要一个系统管理员账号登录，默认取 `admin` / `Admin@123`；你的环境不同时，先在服务上注册该账号，或改脚本顶部的登录凭据常量。

## 签署 DCO

本项目使用 [Developer Certificate of Origin](https://developercertificate.org/)，不用 CLA。提交时加签：

```bash
git commit -s -m "feat: 支持按标签反查版本"
```

即表示你确认：

1. 该贡献由你原创，或你有权以本项目许可提交；
2. 你同意以 AGPL-3.0-or-later 授权该贡献；
3. 你授予维护者在未来协议迁移/双许可场景下继续使用该贡献的许可（详见 [docs/LICENSE-FAQ.md](docs/LICENSE-FAQ.md) 第 5 节）。

## 文档与示例

- 改行为请同步改 `README.md` / `docs/ROADMAP.md`；
- 新增配置项必须进 `deploy/.env.example`，并在 README 的表格里补一行；
- 截图请存 `docs/assets/screenshots/`，宽度建议 1280，避免包含真实业务数据。

## 评审标准

一个 PR 会被这样看：

1. **正确**：有回归覆盖，边界（权限、并发、超大文件）想过；
2. **安全**：不引入越权、不外联、不记录敏感信息到日志；
3. **可维护**：没有重复实现已有工具函数，命名说人话；
4. **不破坏离线**：任何新增前端资源必须随仓库分发。
