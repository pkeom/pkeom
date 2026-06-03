@echo off
chcp 65001 > nul
cd /d "%~dp0"
setlocal enabledelayedexpansion

set SCRIPT=dashboard.py
set PYTHON=

:: 1) Python Launcher (py.exe) — 가장 신뢰할 수 있음
where py >nul 2>&1
if !ERRORLEVEL! == 0 (
    set PYTHON=py
    goto :run
)

:: 2) 가상환경 (.venv)
if exist "%~dp0.venv\Scripts\python.exe" (
    set PYTHON="%~dp0.venv\Scripts\python.exe"
    goto :run
)

:: 3) 알려진 경로 순서대로 탐색
for %%P in (
    "%LOCALAPPDATA%\Python\bin\python.exe"
    "%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "C:\Python313\python.exe"
    "C:\Python312\python.exe"
    "C:\Python311\python.exe"
) do (
    if exist %%P (
        set PYTHON=%%P
        goto :run
    )
)

:: 4) PATH에서 검색 (Microsoft Store placeholder 제외)
for /f "tokens=*" %%P in ('where python 2^>nul') do (
    echo %%P | findstr /i "WindowsApps" >nul
    if !ERRORLEVEL! == 1 (
        set PYTHON="%%P"
        goto :run
    )
)

echo [오류] Python을 찾을 수 없습니다.
echo       Python 3.11 이상을 설치하거나 .venv 가상환경을 만들어 주세요.
pause
exit /b 1

:run
echo Python: %PYTHON%
%PYTHON% %SCRIPT%
pause
