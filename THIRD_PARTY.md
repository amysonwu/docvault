# 第三方组件清单

本项目遵循「少依赖、可离线」原则：后端依赖 14 个运行库，前端仅 3 个静态库（均已内置仓库）。

> 说明：所有依赖均为宽松许可（MIT / BSD / ISC / Apache-2.0），**不引入 GPL 传染风险**。
> 唯一带 AGPL 的 OnlyOffice 以独立进程运行、通过 HTTP 调用，不构成衍生作品（见 NOTICE）。

## 后端（requirements.txt）

| 组件 | 版本约束 | 许可 | 用途 |
|---|---|---|---|
| fastapi | >=0.115 | MIT | Web 框架 |
| uvicorn[standard] | >=0.30 | BSD-3-Clause | ASGI 服务器 |
| sqlalchemy | >=2.0 | MIT | ORM |
| pymysql | >=1.1 | MIT | MySQL 驱动 |
| cryptography | >=42 | Apache-2.0 OR BSD-3-Clause | 加密原语 |
| pyjwt | >=2.8 | MIT | JWT 签发与校验 |
| python-multipart | >=0.0.9 | Apache-2.0 | 表单/文件上传解析 |
| python-docx | >=1.1 | MIT | docx 解析、变更摘要 |
| python-pptx | >=0.6.23 | MIT | pptx 解析 |
| openpyxl | >=3.1 | MIT | xlsx 解析 |
| pypdf | >=4.0 | BSD-3-Clause | PDF 处理 |
| markdown | >=3.5 | BSD-3-Clause | md 预览渲染 |
| requests | >=2.31 | Apache-2.0 | 离线工具/脚本 |
| zstandard | >=0.22 | BSD-3-Clause | 存储块 zstd 压缩（绑定 zstd 库，同为 BSD） |

> 注：`uvicorn[standard]` 会附带安装 uvloop / httptools / websockets / watchfiles / python-dotenv，许可均为 MIT 或 BSD-3-Clause，同为宽松许可，无传染风险。

## 前端（web/vendor，随仓库分发）

| 组件 | 版本 | 许可 | 说明 |
|---|---|---|---|
| Vue.js | 3.4.38 | MIT | 全局构建版，无打包器 |
| PDF.js | 见 `web/vendor/pdfjs` | Apache-2.0 | PDF 渲染 |
| Lucide Icons | 内联 SVG | ISC | 侧栏图标，按原样内联，未修改图形 |

## 服务形态依赖（独立容器）

| 组件 | 许可 | 交互方式 |
|---|---|---|
| OnlyOffice Document Server | AGPL-3.0 | HTTP API（编辑/转换），独立容器 |
| GreatSQL / MySQL 8 | GPL-2.0 | SQL 协议 |
| Nginx | BSD-2-Clause | 反向代理 |
| python:3.13-slim（基础镜像） | PSF-2.0 | Python 运行环境 |

## 字体（可选，仅在线编辑需要）

若部署 OnlyOffice 并需要中文排版，需自行准备字体（如思源系列 SIL OFL-1.1、文泉驿 GPL+FE）。
本仓库**不附带**任何商业字体（微软雅黑、宋体等版权字体请自行确保授权）。

## 保留的版权声明

```text
Vue.js        Copyright (c) 2018-present, Yuxi (Evan) You and Vue contributors — MIT
PDF.js        Copyright 2023 Mozilla Foundation — Apache-2.0
Lucide Icons  Copyright (c) for portions of Lucide are held by Cole Bemis 2013-2022
              as part of Feather (MIT); all other copyright (c) for Lucide are
              held by Lucide Contributors 2022 — ISC
```
