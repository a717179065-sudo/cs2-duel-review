@echo off
rem cs2-review: start the local review window server (127.0.0.1:8770) and open it in the default browser.
rem If the server is already listening, only open the browser.
chcp 65001 >nul
cd /d "%~dp0"
call :listening
if not errorlevel 1 goto open
echo 正在启动复盘窗口服务，关掉那个最小化的窗口就会停止服务。
start "cs2-review-server" /min py -I app\server.py
set n=0
:wait
rem wait about 1 second (ping works even when stdin is redirected)
ping -n 2 127.0.0.1 >nul
call :listening
if not errorlevel 1 goto open
set /a n+=1
if %n% lss 20 goto wait
echo 服务没有起来。请确认装了 Python（py 命令能用），或者在这个目录手动运行：py -I app\server.py
pause
exit /b 1
:open
echo 打开 http://127.0.0.1:8770/
start "" "http://127.0.0.1:8770/"
exit /b 0

:listening
netstat -ano -p tcp | findstr /C:"127.0.0.1:8770 " | findstr /C:"LISTENING" >nul
exit /b %errorlevel%
