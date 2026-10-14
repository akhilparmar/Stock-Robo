@echo off
cd /d "%~dp0.."
title OI spurts paper · port 8002
echo.
echo  OI spurts paper tester
echo  Own database. Same .env and Kotak login. No real orders.
echo  Keep this PC awake. The phone only watches.
echo.
call .venv\Scripts\activate.bat
for /f "delims=" %%i in ('.\.venv\Scripts\python.exe -c "import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(('8.8.8.8',80));print(s.getsockname()[0]);s.close()"') do set LANIP=%%i
echo  PC:    http://127.0.0.1:8002/
echo  Phone: http://%LANIP%:8002/
echo.
.\.venv\Scripts\python.exe -m oi_spurts
pause
