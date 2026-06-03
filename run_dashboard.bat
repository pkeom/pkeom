@echo off
chcp 65001 > nul
cd /d "%~dp0"
"C:\Users\user\AppData\Local\Python\bin\python.exe" dashboard.py
pause
