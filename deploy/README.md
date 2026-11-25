# DocVault 智档 —— 容器化部署

单端口对外：**10001**。应用、数据库、OnlyOffice、入口网关全部跑在容器里，主机只需 Docker。

## 一键启动

```bash
cd deploy
cp .env.example .env        # 按需改端口、数据库口令与两个密钥
docker compose up -d --build
```

启动后访问 **http://<主机IP>:10001**。

首次启动会：建库建账号 → 建表 → 起文档服务器，约 1~2 分钟（OnlyOffice 初始化较慢）。

## 组成

| 容器 | 镜像 | 对外端口 | 说明 |
|---|---|---|---|
| edge | nginx:alpine | **10001** | 唯一入口：`/` → 应用，`/web-apps`、`/doc`、`/cache` 等 → 文档服务器（含协同编辑 WebSocket） |
| app | 本项目 Dockerfile | 无 | FastAPI 应用，仅集群内可达 |
| oo | onlyoffice/documentserver | 无 | 在线编辑 + 保真预览（转 PDF），浏览器经网关同源访问 |
| db | greatsql/greatsql:8.0.32-26 | 无 | 业务库，仅集群内可达 |

只有 10001 暴露到主机；数据库和文档服务器都不发布端口，不会被外部直接访问。

## 数据落在哪

| 路径 | 内容 | 备份方式 |
|---|---|---|
| 命名卷 `<项目名>_db-data`（项目名默认为 deploy 目录名） | 数据库（GreatSQL 的 datadir 是 `/data/GreatSQL`，所以卷挂在 `/data`） | `docker run --rm -v <项目名>_db-data:/v -v $PWD:/b alpine tar czf /b/db.tgz -C /v .` |
| 命名卷 `<项目名>_oo-*` | OnlyOffice 内部数据（字体/缓存/日志） | 一般无需备份（可重建） |
| `../data`（可用 `DATA_DIR` 改） | 文档原件（内容寻址 CAS） | 直接复制该目录 |

`../data` 内部结构：

- `blobs/<hash前2位>/<次2位>/<sha256>` —— 文档正文，按 SHA-256 内容寻址，相同内容只存一份，版本之间共用
- `preview_pdf/` —— OnlyOffice 转出的 PDF 预览缓存（删除后自动重建，不必备份）
- `tmp/` —— 上传中转

**只备份 `../data` + 数据库卷即可完整还原。**

```bash
# 数据库卷：卷名 = <项目名>_db-data，项目名默认为 deploy 目录名
# （若 .env 里设置过 COMPOSE_PROJECT_NAME，请用该值替换 $(basename "$PWD")）
docker run --rm -v "$(basename "$PWD")_db-data:/v" -v "$PWD":/b alpine tar czf /b/backup-db.tgz -C /v .
# 文档原件
tar czf backup-data.tgz -C ../data .
```

恢复时把两个 tar 解回原位置，再 `docker compose up -d` 即可。

## 中文字体

官方 OnlyOffice 镜像只带 Latin 字体，不额外挂字体时中文文档转 PDF 会掉字/换字形。

`../fonts`（可用 `FONT_DIR` 改）会被只读挂到容器的 `/usr/share/fonts/truetype/custom`，
放入 `msyh.ttc`、`simsun.ttc`、`simhei.ttf`、`simkai.ttf`、`simfang.ttf`、`Deng.ttf` 等即可，
改完 `docker compose up -d --force-recreate oo` 让字体缓存重建。


## 可调参数

在 `deploy/.env` 里覆盖（不写则用默认值）：

```ini
PUBLIC_PORT=10001                 # 对外端口
DATA_DIR=../data                  # 文档原件目录
FONT_DIR=../fonts                 # 中文字体目录（只读挂给文档服务器）
DB_PASS=DocVault_2026             # 应用账号口令（首次启动时写入数据库，之后修改需 ALTER USER）
DB_ROOT_PASS=DocVault_Root_2026
OO_SECRET=<32位随机串>            # 应用与文档服务器共享的 JWT 密钥
JWT_SECRET=<32位随机串>           # 应用登录令牌密钥，正式环境务必改
```

## 常见运维

```bash
docker compose ps                     # 状态
docker compose logs -f app         # 应用日志
docker compose restart app         # 改完代码后（需先 --build）
docker compose down                   # 停止（数据卷保留）
docker compose down -v                # 连数据一起删（慎用）
```

## 迁移到另一台主机

1. 目标机装 Docker；
2. 拷贝整个 `docvault/` 目录（含 `data/`）；
3. 导出/导入数据库卷（见上），或直接 `docker compose up -d --build` 后重新初始化；
4. `cd deploy && docker compose up -d --build`。

## 排错

- **10001 打不开**：`docker compose ps` 看 `edge` 服务是否 Up；云主机安全组/防火墙放行 10001。
- **保存文档报"无法保存"**：检查 app 服务的 `DOCVault_OO_INTERNAL` 是否为 `http://app:8000`（文档服务器回访用，必须是容器网络内地址）。
- **编辑器白屏**：`curl -I http://<host>:10001/web-apps/apps/api/documents/api.js` 应返回 200；否则看 `docker compose logs edge`。
- **预览转 PDF 失败**：`docker compose logs oo` 看文档服务器是否 OOM（OnlyOffice 需要约 1GB 内存）。
