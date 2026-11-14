@echo off
cd /d "%~dp0"
if exist "%~dp0runtime\windows-x64\pythonw.exe" (
  start "" "%~dp0runtime\windows-x64\pythonw.exe" "%~dp0launch.py" --stop
) else (
  python "%~dp0launch.py" --stop
)
