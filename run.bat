@echo off
chcp 65001 >nul
cd /d D:\Code\JungJingNew
call ".venv_win\Scripts\activate.bat"
python run.py
echo.
echo ==== Bot 进程退出，exit code: %ERRORLEVEL% ====
echo 0 = 正常退出；3221225477 / 0xC0000005 / 负数 = 原生崩溃（access violation 一类）
pause
