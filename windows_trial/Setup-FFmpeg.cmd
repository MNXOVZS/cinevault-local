@echo off
rem [INPUT]: Bundled Python and the pinned tool installer.
rem [OUTPUT]: Runs interactive local media tool setup.
rem [POS]: Separate, user-triggered setup entry.
rem [PROTOCOL]: Update this header on change, then check AGENTS.md
cd /d "%~dp0"
if not exist "%~dp0runtime\windows-x64\python.exe" (
  echo Extract the entire ZIP before opening Setup-FFmpeg.cmd.
  pause
  exit /b 1
)
chcp 65001 >nul
"%~dp0runtime\windows-x64\python.exe" -X utf8 "%~dp0windows_trial\setup_ffmpeg.py"
set "CINEVAULT_SETUP_RESULT=%ERRORLEVEL%"
pause
exit /b %CINEVAULT_SETUP_RESULT%
