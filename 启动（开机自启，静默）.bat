@echo off
chcp 936 >nul
rem 开机自启：等 15 秒让系统缓过来，再静默启动（不弹浏览器）
rem 路径全部相对脚本自身；把本文件快捷方式放进启动文件夹即可，换机器无需修改
cd /d "%~dp0"
ping -n 15 127.0.0.1 >nul

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

start "" "%PYW%" "%~dp0app.py" --silent
exit
