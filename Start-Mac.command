#!/bin/bash
set -e
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
if ! command -v python3 >/dev/null || ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
  printf '\nInstall Python 3.11+ and FFmpeg (including ffprobe), then retry. See README.md.\n'
  read -r -p 'Press Enter to close...'
  exit 1
fi
if ! python3 -c 'import sys,sqlite3; assert sys.version_info >= (3,11)' 2>/dev/null; then
  printf '\nPython 3.11+ with SQLite is required. See README.md.\n'
  read -r -p 'Press Enter to close...'
  exit 1
fi
python3 launch.py
