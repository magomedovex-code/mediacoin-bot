@echo off
chcp 65001 >nul
cd /d "%~dp0"
title MediaCoin-бот

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
echo Python не найден. Установи с python.org с галочкой "Add python.exe to PATH".
pause
exit /b

:found
if not exist ".installed" (
    echo Первый запуск: ставлю discord.py, это займёт 1-2 минуты...
    %PY% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Не получилось установить библиотеки. Пришли этот текст Claude.
        pause
        exit /b
    )
    echo ok> .installed
)

echo Запускаю бота. Чтобы остановить - закрой это окно.
echo.
%PY% bot.py
echo.
echo Бот остановился.
pause
