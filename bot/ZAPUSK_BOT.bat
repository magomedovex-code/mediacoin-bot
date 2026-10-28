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
if not exist ".installed2" (
    echo Ставлю библиотеки ^(discord.py, yt-dlp^), это займёт 1-2 минуты...
    %PY% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Не получилось установить библиотеки. Пришли этот текст в Discord @gojooo69.
        pause
        exit /b
    )
    echo ok> .installed2
) else (
    echo Проверяю обновление yt-dlp ^(нужно для TikTok^)...
    %PY% -m pip install -q -U yt-dlp >nul 2>nul
)

echo Запускаю бота. Чтобы остановить - закрой это окно.
echo.
%PY% bot.py
echo.
echo Бот остановился.
pause
