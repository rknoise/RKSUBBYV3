@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem fejlesztoi inditas (a telepitett valtozat: Asztal > rksubby V3)
if exist ".venv-v3\Scripts\python.exe" (
  ".venv-v3\Scripts\python.exe" rksubby.py %*
) else (
  python rksubby.py %*
)
