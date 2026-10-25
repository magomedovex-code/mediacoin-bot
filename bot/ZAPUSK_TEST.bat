@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Тест RCON

echo Ищу Python...
set "PY="

py -3 --version >nul 2>nul
if not errorlevel 1 (
    set "PY=py -3"
    goto found
)

python --version >nul 2>nul
if not errorlevel 1 (
    set "PY=python"
    goto found
)

if exist "%LOCALAPPDATA%\Programs\Python\Python314\python.exe" (
    set "PY=%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
    goto found
)

echo.
echo Python не найден.
echo Переустанови Python с python.org и поставь галочку "Add python.exe to PATH".
echo.
pause
exit /b

:found
echo Найден:
%PY% --version
echo.

if not exist "rcon_test.py" (
    echo Файл rcon_test.py не найден. Он должен лежать в той же папке, что и этот bat-файл.
    pause
    exit /b
)

%PY% rcon_test.py

echo.
pause
