@echo off
cd /d "%~dp0"
title 测评雷达 - 调试模式
echo 正在以前台模式启动, 关闭本窗口即退出程序...
echo.
set PYC=C:/Users/司达达/AppData/Local/Programs/Python/Python311/python.exe
if not exist "%PYC%" set PYC=python
"%PYC%" "%~dp0app.py"
echo.
echo 程序已退出. 日志在 data\app.log
pause
