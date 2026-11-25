@echo off
rem SPDX-License-Identifier: AGPL-3.0-or-later
rem Copyright (C) 2026 DocVault Contributors
chcp 65001 >nul
cd /d "%~dp0"

rem ---- 本机开发连接参数（按需修改；容器化部署见 deploy/）----
if "%DOCVault_DB_PORT%"=="" set "DOCVault_DB_PORT=3306"
if "%DOCVault_DB_USER%"=="" set "DOCVault_DB_USER=docvault"
if "%DOCVault_DB_PASS%"=="" set "DOCVault_DB_PASS=DocVault_2026"
if "%DOCVault_DB_NAME%"=="" set "DOCVault_DB_NAME=docvault"

echo ============================================
echo  DocVault dev server
echo  http://127.0.0.1:8000
echo ============================================

rem 依次尝试 python / py -3；也可用环境变量 DOCVault_PYTHON 指定解释器完整路径
if not "%DOCVault_PYTHON%"=="" goto run_py
python -c "import sys" >nul 2>nul && (set "DOCVault_PYTHON=python" & goto run_py)
py -3 -c "import sys" >nul 2>nul && (set "DOCVault_PYTHON=py -3" & goto run_py)
echo [ERROR] 未找到可用的 Python 解释器。
echo         请安装 Python 3.11+，或设置环境变量 DOCVault_PYTHON 指向解释器完整路径。
pause >nul
exit /b 1

:run_py
%DOCVault_PYTHON% -m uvicorn app.main:app --host 0.0.0.0 --port 8000

echo.
echo [server exited] press any key to close...
pause >nul
