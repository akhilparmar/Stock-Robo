@echo off
cd /d "%~dp0.."
title Trading paper server · port 8010
echo.
echo  Paper trading server (stock + Nifty + OI spurts)
echo  Keep this PC awake. Phone only monitors — it does not place trades.
echo.
call .venv\Scripts\activate.bat
for /f "delims=" %%i in ('.\.venv\Scripts\python.exe -c "import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(('8.8.8.8',80));print(s.getsockname()[0]);s.close()"') do set LANIP=%%i
echo  Stock:     http://127.0.0.1:8010/
echo  Nifty:     http://127.0.0.1:8010/nifty/
echo  OI spurts: http://127.0.0.1:8010/oi-spurts/
echo  Phone:     http://%LANIP%:8010/
echo.
.\.venv\Scripts\uvicorn.exe app.main:app --host 0.0.0.0 --port 8010
pause
