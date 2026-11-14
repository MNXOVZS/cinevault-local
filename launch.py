# [INPUT]: 系统 Python、回环 HTTP 和 server.py。
# [OUTPUT]: 启动、复用或停止本机服务。
# [POS]: 平台启动命令共用的入口。
# [PROTOCOL]: Update this header on change, then check AGENTS.md
"""Portable launcher. No registry, service installation or system changes."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent
PORT = 8765
URL = f'http://127.0.0.1:{PORT}'
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def notice(message):
    if sys.stdout:
        print(message, flush=True)
    if os.name == 'nt':
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, message, '映库 CineVault', 0)

def state():
    try:
        with HTTP.open(URL + '/api/state?compact=1', timeout=2) as response:
            value = json.load(response)
            return value if isinstance(value, dict) else None
    except Exception:
        return None

def library_locked(data_dir):
    path = Path(data_dir)/'library.lock'
    if not path.exists():
        return False
    with path.open('r+b') as handle:
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle, fcntl.LOCK_UN)
            return False
        except OSError:
            return True

def start_server(command, log):
    kwargs = {'cwd': str(ROOT), 'stdin': subprocess.DEVNULL, 'stdout': log, 'stderr': log}
    if os.name != 'nt':
        return subprocess.Popen(command, start_new_session=True, **kwargs)

    flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    try:
        # DETACHED_PROCESS releases the console, but a Windows Job can still
        # end its children when the parent session closes. Ask Windows to
        # start the background service separately when the Job permits it.
        return subprocess.Popen(command, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kwargs)
    except OSError as exc:
        if getattr(exc, 'winerror', None) != 5:  # ERROR_ACCESS_DENIED
            raise
        return subprocess.Popen(command, creationflags=flags, **kwargs)


def open_library():
    try:
        if webbrowser.open(URL):
            return
    except (OSError, webbrowser.Error):
        pass
    notice(f'映库已启动。请在浏览器地址栏输入 {URL}。')


def service_matches_library(response):
    system = response.get('system')
    if not isinstance(system, dict) or system.get('app') != 'cinevault':
        notice(f'端口 {PORT} 已被其他程序使用，请先释放此端口。')
        return False
    active_dir = system.get('data_dir')
    if not isinstance(active_dir, str) or not active_dir or Path(active_dir).resolve() != (ROOT/'data').resolve():
        notice('另一个目录的映库正在运行，请先停止它，再打开这个资料库。')
        return False
    return True


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--lan', action='store_true')
    parser.add_argument('--stop', action='store_true')
    parser.add_argument('--no-open', action='store_true')
    args = parser.parse_args(argv)
    current = state()
    if current is not None and not service_matches_library(current):
        return 1
    if args.stop:
        if current is None:
            notice('映库未在默认端口运行。')
            return 0
        request = urllib.request.Request(URL + '/api/shutdown', data=b'{}', headers={'Content-Type':'application/json', 'Origin': URL}, method='POST')
        try:
            HTTP.open(request, timeout=4).close()
            for _ in range(120):
                if not library_locked(ROOT/'data'):
                    notice('映库已停止。下次双击启动即可继续。')
                    return 0
                time.sleep(.1)
            notice('停止请求已发送，后台正在结束任务。请稍后再迁移或恢复资料库。')
        except Exception as exc:
            notice(f'停止失败：{exc}')
            return 1
        return 0
    if current is not None:
        if args.lan and not current.get('system', {}).get('lan'):
            notice('请先双击“停止映库”，再双击“手机访问”以启用局域网。')
        if not args.no_open:
            open_library()
        return 0
    logs = ROOT/'data'/'logs'
    command = [sys.executable, '-X', 'utf8', str(ROOT/'server.py'), '--port', str(PORT)]
    if args.lan:
        command.append('--lan')
    try:
        logs.mkdir(parents=True, exist_ok=True)
        with (logs/'server.log').open('ab') as log:
            process = start_server(command, log)
    except OSError as exc:
        notice(f'无法启动映库：{exc}')
        return 1
    try:
        (logs/'launcher-pid.txt').write_text(str(process.pid), encoding='ascii')
    except OSError:
        # PID logging is diagnostic. Readiness is confirmed through HTTP.
        pass
    for _ in range(150):
        running = state()
        if running is not None:
            if not service_matches_library(running):
                return 1
            if not args.no_open:
                open_library()
            return 0
        if process.poll() is not None:
            break
        time.sleep(.2)
    notice('映库启动失败。请查看 data/logs/server.log 或双击“运行诊断”。')
    return 1

if __name__ == '__main__':
    raise SystemExit(main())
