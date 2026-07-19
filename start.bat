@echo off
chcp 65001 >nul
echo ================================================
echo   Apple Store 充值服务
echo ================================================
echo.
cd /d %~dp0

echo [1/2] 安装依赖...
pip install -r requirements.txt --quiet 2>nul

echo [2/2] 启动服务 + 公网隧道...
echo.
start "Apple-Topup-Server" python server.py
timeout /t 4 /nobreak >nul
start "Cloudflare-Tunnel" cloudflared.exe tunnel --url http://localhost:5000
timeout /t 8 /nobreak >nul

echo.
echo ================================================
echo   本地地址: http://127.0.0.1:5000
echo   公网地址: 查看 Cloudflare-Tunnel 窗口
echo   按 Ctrl+C 停止所有服务
echo ================================================
echo.
pause
