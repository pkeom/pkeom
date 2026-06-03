@echo off
chcp 65001 > nul

:: Python 후보 경로를 순서대로 시도
set PYTHON=

:: 1) 가상환경 (프로젝트 내 .venv)
if exist "%~dp0.venv\Scripts\python.exe" (
    set PYTHON=%~dp0.venv\Scripts\python.exe
    goto :run
)

:: 2) 실제 설치된 Python (Microsoft Store placeholder 제외)
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
        set PYTHON=%%~P
        goto :run
    )
)

:: 3) PATH에서 python 검색 (WindowsApps placeholder인지 확인)
for /f "tokens=*" %%P in ('where python 2^>nul') do (
    echo %%P | findstr /i "WindowsApps" > nul || (
        set PYTHON=%%P
        goto :run
    )
)

echo [오류] Python을 찾을 수 없습니다.
echo       Python 3.11 이상을 설치하거나 .venv 가상환경을 만들어 주세요.
pause
exit /b 1

:run
echo Python: %PYTHON%
cd /d "%~dp0"
"%PYTHON%" dashboard.py
pause
