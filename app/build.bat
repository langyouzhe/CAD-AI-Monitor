@echo off
rem ============================================================
rem  Build script: package app\app.py into a single exe
rem  Usage: double-click this file (it always cd's to repo root)
rem  Output: release\CAD-AI-Collector.exe
rem
rem  NOTE: keep this file ASCII-only. cmd.exe parses .bat files with
rem  the OEM codepage, so non-ASCII text here breaks the script.
rem
rem  To use a specific Python interpreter:
rem      set CADAI_PYTHON=D:\Python312\python.exe
rem ============================================================
setlocal
cd /d "%~dp0.."

rem ---------- 1. find Python ----------
set "PY="
if defined CADAI_PYTHON set "PY=%CADAI_PYTHON%"
if defined PY goto have_py
where py >nul 2>nul && set "PY=py -3"
if defined PY goto have_py
where python >nul 2>nul && set "PY=python"
if defined PY goto have_py
echo.
echo [ERROR] Python not found. Please install Python 3.10 or newer
echo         and tick "Add python.exe to PATH" during setup.
echo         Download: https://www.python.org/downloads/
echo.
pause
exit /b 1

:have_py
echo Using Python: %PY%
%PY% --version

rem ---------- 2. check dependencies (we never install anything silently) ----------
%PY% -c "import PyInstaller, pynput, win32com" >nul 2>nul
if errorlevel 1 goto no_deps

rem ---------- 3. build ----------
rem NOTE: app\sw_command_ids.json (the command-ID table) is NOT shipped: it is
rem generated on first run from the user's own SolidWorks type library.
rem PyInstaller therefore gets no --add-data for it.
%PY% -m PyInstaller --noconfirm --onefile --noconsole ^
    --name CAD-AI-Collector ^
    --hidden-import pynput.keyboard._win32 ^
    --hidden-import pynput.mouse._win32 ^
    --hidden-import win32com ^
    --hidden-import win32com.client ^
    --hidden-import pythoncom ^
    --hidden-import pywintypes ^
    app\app.py
if errorlevel 1 goto build_failed

if not exist dist\CAD-AI-Collector.exe goto build_failed
if not exist release mkdir release
copy /Y dist\CAD-AI-Collector.exe release\ >nul
echo.
echo Build OK: release\CAD-AI-Collector.exe
echo Verify with: release\CAD-AI-Collector.exe --selftest   (exit code 0 = healthy)
echo.
pause
exit /b 0

:no_deps
echo.
echo [MISSING DEPENDENCIES] Run the command below (needs internet),
echo then double-click this script again:
echo.
echo     %PY% -m pip install -r app\requirements.txt
echo.
pause
exit /b 1

:build_failed
echo.
echo [BUILD FAILED] See the error messages above.
echo.
pause
exit /b 1
