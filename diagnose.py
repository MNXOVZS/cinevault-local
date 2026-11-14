# [INPUT]: 本机依赖与回环服务状态。
# [OUTPUT]: 输出诊断报告。
# [POS]: 只读诊断入口。
# [PROTOCOL]: Update this header on change, then check AGENTS.md
"""Read-only local diagnostics. Does not scan source folders or print tokens."""
import json
import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import sqlite3
import urllib.request
APP=Path(__file__).resolve().parent

def main():
    print('CineVault local diagnostics')
    print('OS:',platform.system(),platform.machine())
    print('Python:',platform.python_version())
    print('SQLite:',sqlite3.sqlite_version)
    print('Application:',APP)
    print('Free space (GB):',round(shutil.disk_usage(APP).free/1024**3,2))
    config_path=APP/'ai'/'config.json'
    if config_path.is_file():
        try:
            config=json.loads(config_path.read_text(encoding='utf-8'))
            model=APP/config['model']
            checksum=None
            if model.is_file():
                with model.open('rb') as model_file:
                    checksum=hashlib.file_digest(model_file,'sha256').hexdigest()
            print('Local classifier model:', 'SHA256 OK' if checksum==config.get('sha256') else 'MISSING or HASH MISMATCH')
            print('Local classifier platform:', 'windows' if os.name=='nt' else 'macos')
        except Exception as exc:
            print('Local classifier:',type(exc).__name__)
    for name in ('ffmpeg','ffprobe'):
        bundled=APP/'bin'/(name+'.exe' if os.name=='nt' else name)
        executable=str(bundled) if bundled.exists() else shutil.which(name)
        if not executable:
            print(name+': NOT FOUND')
            continue
        try:
            result=subprocess.run([executable,'-version'],capture_output=True,text=True,errors='replace',timeout=8,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            print(name+':',result.stdout.splitlines()[0] if result.stdout else 'failed')
        except Exception as exc:
            print(name+':',type(exc).__name__)
    try:
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open('http://127.0.0.1:8765/api/state?compact=1',timeout=3) as response:
            value=json.load(response)
        if value.get('system',{}).get('app')!='cinevault':
            print('Local service: another application occupies this port.')
            return
        stats=value.get('stats',{})
        print('Service:',value['system'].get('version'),'running')
        print('Registered media:',stats.get('total',0),'Ready:',stats.get('ready',0),'Missing:',stats.get('missing',0))
        print('Source folders:',len(value.get('sources',[])))
        print('Content groups:',{key:stats.get('scope_'+key,0) for key in ('adult','personal','review')})
        print('Classifier ready:',value['system'].get('classifier',{}).get('available',False))
        print('Data location:',value['system'].get('data_dir'))
    except Exception:
        print('Local service: not reachable. Start CineVault, or inspect data/logs/server.log.')
    print('No source folders were scanned. No filenames or pairing tokens were exported.')
if __name__=='__main__':
    main()
