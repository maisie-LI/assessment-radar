@echo off
rem 开机自启：等 15 秒让系统缓过来，再静默启动（不弹浏览器）
cd /d "C:/Users/司达达/WorkBuddy/2026-10-27-18-45-46/assessment-radar"
ping -n 15 127.0.0.1 >nul
set PYW=C:/Users/司达达/AppData/Local/Programs/Python/Python311\pythonw.exe
if not exist "%PYW%" set PYW=pythonw
start "" "%PYW%" "C:/Users/司达达/WorkBuddy/2026-10-27-18-45-46/assessment-radar\app.py" --silent
exit
