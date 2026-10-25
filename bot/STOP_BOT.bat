@echo off
chcp 65001 >nul
echo Закрываю все копии бота (Python)...
taskkill /F /IM python.exe >nul 2>nul
taskkill /F /IM pythonw.exe >nul 2>nul
taskkill /F /IM py.exe >nul 2>nul
echo.
echo Готово. Все копии бота на этом компьютере закрыты.
echo Теперь запусти ZAPUSK_BOT.bat ОДИН раз.
echo.
pause
