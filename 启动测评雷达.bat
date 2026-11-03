@echo off
cd /d "%~dp0"
title 测评雷达

set PYDIR=C:/Users/司达达/AppData/Local/Programs/Python/Python311
set PYW=%PYDIR%\pythonw.exe
set PYC=%PYDIR%\python.exe
if not exist "%PYW%" set PYW=pythonw
if not exist "%PYC%" set PYC=python

rem ---------- qianzhi自检 ----------
"%PYC%" -c "import tkinter, imaplib, sqlite3, http.server" >nul 2>&1
if errorlevel 1 (
  echo.
  echo  [启动失败] 当前 Python 缺少 tkinter 或标准库组件
  echo  找的路径: %PYC%
  echo  请安装 Python 3.11 完整版后再试
  echo.
  pause
  exit /b 1
)

rem ---------- 后台启动 ----------
start "" "%PYW%" "%~dp0app.py"

rem ---------- 确认真的起来了 ----------
set /a n=0
:wait
timeout /t 1 /nobreak >nul
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
