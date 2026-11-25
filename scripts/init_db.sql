-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Copyright (C) 2026 DocVault Contributors
--
-- 本地开发用的最小初始化（手动执行）。
-- 容器化部署不需要本文件：deploy/ 下由数据库容器首次启动自动建库建账号，
-- 口令取自 .env 的 DB_PASS。
CREATE DATABASE IF NOT EXISTS docvault DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'docvault'@'%' IDENTIFIED BY 'DocVault_2026';
GRANT ALL PRIVILEGES ON docvault.* TO 'docvault'@'%';
FLUSH PRIVILEGES;
