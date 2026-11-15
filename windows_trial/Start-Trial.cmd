@echo off
rem [INPUT]: Bundled Python and launch.py.
rem [OUTPUT]: Opens the loopback trial library in the default browser.
rem [POS]: Portable archive root entry.
rem [PROTOCOL]: Update this header on change, then check AGENTS.md
cd /d "%~dp0"
if not exist "%~dp0runtime\windows-x64\pythonw.exe" (
  echo Extract the entire ZIP before opening Start-Trial.cmd.
  pause
  exit /b 1
)
start "" "%~dp0runtime\windows-x64\pythonw.exe" "%~dp0launch.py" --port 8766
