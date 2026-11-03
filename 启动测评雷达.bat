@echo off
chcp 936 >nul
cd /d "%~dp0"
title 测评雷达

rem ---------- 定位 Python：优先选能 import tkinter 的那个版本 ----------
rem 注意：不要用 for /f 回读 py -3 的输出，Python 输出 UTF-8 而 cmd 按 GBK 解码，
rem 用户名含中文时路径会变乱码（本项目实测踩坑）
set "PYC="
set "PYW="
for %%v in (Python311 Python312 Python313 Python314 Python310) do (
  if not defined PYC (
    if exist "%LOCALAPPDATA%\Programs\Python\%%v\python.exe" (
      "%LOCALAPPDATA%\Programs\Python\%%v\python.exe" -c "import tkinter" >nul 2>&1
      if not errorlevel 1 (
        set "PYC=%LOCALAPPDATA%\Programs\Python\%%v\python.exe"
        if exist "%LOCALAPPDATA%\Programs\Python\%%v\pythonw.exe" (
          set "PYW=%LOCALAPPDATA%\Programs\Python\%%v\pythonw.exe"
        ) else (
          set "PYW=%LOCALAPPDATA%\Programs\Python\%%v\python.exe"
        )
      )
    )
  )
)
rem 兜底：PATH 里的 python / pythonw
if not defined PYC (
  where python >nul 2>&1
  if not errorlevel 1 (
    python -c "import tkinter" >nul 2>&1
    if not errorlevel 1 set "PYC=python"
  )
)
if not defined PYC set "PYC=python"
if not defined PYW (
  where pythonw >nul 2>&1
  if not errorlevel 1 set "PYW=pythonw"
)
if not defined PYW set "PYW=pythonw"

rem ---------- 前置自检：确认这套 Python 能跑起来 ----------
"%PYC%" -c "import tkinter, imaplib, sqlite3, http.server" >nul 2>&1
if errorlevel 1 (
  echo.
  echo  [启动失败] 当前 Python 缺少 tkinter 或标准库组件
  echo  当前使用的 Python: %PYC%
  echo  请到 python.org 安装 Python 3.11 及以上「完整版」后重试
  echo  （注意别用精简版/嵌入式版，它们不带 tkinter）
  echo.
  pause
  exit /b 1
)

rem ---------- 后台启动（不占窗口）----------
start "" "%PYW%" "%~dp0app.py"

rem ---------- 等它真的起来，最多 8 秒 ----------
set /a n=0
:wait
ping -n 2 127.0.0.1 >nul
set /a n+=1
"%PYC%" -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8765/api/ping',timeout=1)" >nul 2>&1
if not errorlevel 1 goto ok
if %n% lss 8 goto wait

echo.
echo  ============================================
echo   没检测到测评雷达启动, 下面是具体错误信息
echo  ============================================
echo.
"%PYC%" "%~dp0app.py"
echo.
echo  (按任意键关闭本窗口)
pause >nul
exit /b 1

:ok
exit
