@echo off
chcp 65001 >nul
cd /d "%~dp0"
title A股量化交易系统

echo.
echo ╔═══════════════════════════════════════╗
echo ║    📊 A股量化交易系统 v2.0            ║
echo ║                                       ║
echo ║  内置Web服务，无需 Flask              ║
echo ║  浏览器打开 http://localhost:5000      ║
echo ║  Ctrl+C 停止服务                      ║
echo ╚═══════════════════════════════════════╝
echo.

python server.py
pause
