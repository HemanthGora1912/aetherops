@echo off
setlocal
title AetherOps Live Demo
cd /d "%~dp0"
set PYTHONDONTWRITEBYTECODE=1
color 0A

set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY if exist "%USERPROFILE%\anaconda3\python.exe" set "PY=%USERPROFILE%\anaconda3\python.exe"
if not defined PY set "PY=python"

echo ================================================================
echo   AetherOps - Distributed Database Node Provisioner and Controller
echo ================================================================
echo.
echo [1/5] Python interpreter
%PY% --version
if errorlevel 1 (
  echo Python 3.10+ not found.
  pause
  exit /b 1
)
echo.
echo [2/5] Installing dev dependencies: pytest, mypy
%PY% -m pip install -q --disable-pip-version-check -r requirements-dev.txt
echo.
echo [3/5] Unit tests - subprocess.run and builtins.open fully mocked
%PY% -m pytest -q -p no:cacheprovider
set "TEST_RC=%errorlevel%"
echo.
echo [3b]  Strict static typing - mypy --strict
%PY% -m mypy --cache-dir "%TEMP%\aetherops_mypy"
set "MYPY_RC=%errorlevel%"
echo.
echo [4/5] Host verification - native Windows run
echo       Expected: the platform guard rejects a non-Debian host.
%PY% -m src.cli verify
set "VERIFY_RC=%errorlevel%"
timeout /t 6 >nul

echo.
echo [5/5] Self-healing controller - 3 nodes, 15%% crash rate, 30 cycles
timeout /t 2 >nul
%PY% -m src.cli start --interval 0.4 --nodes 3 --failure-rate 0.15 --failure-threshold 2 --seed 7 --max-cycles 30
set "DAEMON_RC=%errorlevel%"

echo.
echo ================================================================
echo   RESULTS
echo ================================================================
if "%TEST_RC%"=="0" (echo   Unit tests ............ PASS) else (echo   Unit tests ............ FAIL rc=%TEST_RC%)
if "%MYPY_RC%"=="0" (echo   mypy --strict ......... PASS) else (echo   mypy --strict ......... FAIL rc=%MYPY_RC%)
if "%VERIFY_RC%"=="1" (echo   verify on Windows ..... correctly rejected non-Debian host) else (echo   verify ................ rc=%VERIFY_RC%)
if "%DAEMON_RC%"=="0" (echo   Controller daemon ..... PASS - ran 30 cycles cleanly) else (echo   Controller daemon ..... rc=%DAEMON_RC%)
echo ================================================================
echo Press any key to close.
pause >nul
