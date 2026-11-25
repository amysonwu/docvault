#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 DocVault Contributors
#
# 数据库首次启动初始化（仅在数据卷为空时执行一次）：
#   建业务库、建应用账号，口令取自容器环境变量 DB_PASS。
# 已初始化的数据卷不会再次执行；此后改 DB_PASS 需自行执行 ALTER USER。
#
# 注意：本脚本由镜像入口脚本 source 执行，不要设置 set -euo 等会外溢的选项。
# 入口脚本已提供 ${mysql[@]}（本地 socket 连接）。
if [ -z "${DB_PASS:-}" ]; then
    echo "[init] DB_PASS 未设置，无法创建应用账号" >&2
    exit 1
fi

"${mysql[@]}" <<SQL || exit 1
CREATE DATABASE IF NOT EXISTS docvault DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'docvault'@'%' IDENTIFIED BY '${DB_PASS}';
ALTER USER 'docvault'@'%' IDENTIFIED BY '${DB_PASS}';
GRANT ALL PRIVILEGES ON docvault.* TO 'docvault'@'%';
FLUSH PRIVILEGES;
SQL
echo "[init] 业务库与账号已就绪"
