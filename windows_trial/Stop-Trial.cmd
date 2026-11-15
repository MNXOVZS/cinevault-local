@echo off
rem [INPUT]: Bundled Python and launch.py.
rem [OUTPUT]: Stops the matching trial library.
rem [POS]: Portable archive root exit.
rem [PROTOCOL]: Update this header on change, then check AGENTS.md
cd /d "%~dp0"
if not exist "%~dp0runtime\windows-x64\pythonw.exe" exit /b 1
start "" "%~dp0runtime\windows-x64\pythonw.exe" "%~dp0launch.py" --port 8766 --stop
