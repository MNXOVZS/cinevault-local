# [INPUT]: ZIP 备份、SQLite 校验与资料库锁。
# [OUTPUT]: 恢复数据库和封面。
# [POS]: 恢复入口。
# [PROTOCOL]: Update this header on change, then check AGENTS.md
"""Offline metadata restore; never touches media originals."""
import argparse
from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent

@contextmanager
def exclusive_library(data):
    data.mkdir(parents=True, exist_ok=True)
    handle = (data/'library.lock').open('a+b')
    try:
        if handle.seek(0, 2) == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('资料库仍在运行，请先停止映库，再恢复备份。') from exc
        yield
    finally:
        handle.close()

def validate_database(path):
    connection = sqlite3.connect(path)
    try:
        result = connection.execute('PRAGMA integrity_check').fetchone()[0]
        if result != 'ok':
            raise ValueError('数据库完整性检查失败：' + result)
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'videos' not in tables:
            raise ValueError('备份不是映库数据库。')
    finally:
        connection.close()

def restore(archive, data):
    archive, data = Path(archive).resolve(), Path(data).resolve()
    with exclusive_library(data), tempfile.TemporaryDirectory(prefix='.restore-', dir=data) as temporary:
        stage = Path(temporary)
        with zipfile.ZipFile(archive) as source:
            names = source.namelist()
            if 'library.sqlite3' not in names or 'manifest.json' not in names:
                raise ValueError('缺少数据库或 manifest.json。')
            manifest = json.loads(source.read('manifest.json'))
            if manifest.get('includes_originals') is not False:
                raise ValueError('仅支持恢复映库生成的资料库备份。')
            total = 0
            for entry in source.infolist():
                path = PurePosixPath(entry.filename)
                if path.is_absolute() or '..' in path.parts or '\\' in entry.filename or ':' in entry.filename:
                    raise ValueError('备份包含无效路径。')
                if entry.filename not in ('library.sqlite3', 'manifest.json') and (not path.parts or path.parts[0] != 'assets'):
                    raise ValueError('备份包含非资料库文件。')
                if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError('备份包含符号链接。')
                total += entry.file_size
                if total > 50*1024**3:
                    raise ValueError('备份解压大小超过 50 GB。')
                destination = stage.joinpath(*path.parts)
                if entry.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with source.open(entry) as reader, destination.open('wb') as writer:
                        shutil.copyfileobj(reader, writer, 1024*1024)
        validate_database(stage/'library.sqlite3')
        database, assets = data/'library.sqlite3', data/'assets'
        if database.is_symlink() or assets.is_symlink():
            raise ValueError('资料库数据库和缓存目录不能是符号链接。')
        previous = None
        if database.exists():
            backups = data/'backups'
            backups.mkdir(exist_ok=True)
            previous = backups / ('before-restore-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.zip')
            conn = sqlite3.connect(database)
            snapshot = sqlite3.connect(stage/'previous.sqlite3')
            try:
                conn.backup(snapshot)
            finally:
                snapshot.close()
                conn.close()
            with zipfile.ZipFile(previous, 'w', zipfile.ZIP_DEFLATED) as backup:
                backup.write(stage/'previous.sqlite3', 'library.sqlite3')
                backup.writestr('manifest.json', json.dumps({'version':1,'includes_originals':False,'created_at':datetime.now().isoformat(),'note':'恢复前自动保存；不包含原片。'}, ensure_ascii=False))
                if assets.exists():
                    for item in assets.rglob('*'):
                        if item.is_file() and not item.is_symlink():
                            backup.write(item, item.relative_to(data).as_posix())
        # Stage the entire replacement first. Roll back on any replacement error.
        old_assets = stage/'old-assets'
        new_assets = stage/'assets'
        new_assets.mkdir(exist_ok=True)
        assets_replaced = False
        assets_moved = False
        try:
            if assets.exists():
                assets.rename(old_assets)
                assets_moved = True
            new_assets.rename(assets)
            assets_replaced = True
            # No server is active: SQLite sidecars must not attach to the restored DB.
            for suffix in ('-wal','-shm'):
                sidecar = Path(str(database)+suffix)
                if sidecar.exists():
                    sidecar.rename(stage/('old-sidecar'+suffix))
            os.replace(stage/'library.sqlite3', database)
        except Exception:
            if assets_replaced and assets.exists():
                assets.rename(stage/'failed-assets')
            if assets_moved and old_assets.exists():
                old_assets.rename(assets)
            if (stage/'previous.sqlite3').exists():
                os.replace(stage/'previous.sqlite3', database)
            raise
        print('恢复完成。视频原片和访问令牌保持不变。')
        if previous:
            print('恢复前快照：', previous)
        print('请启动映库；外部硬盘路径如有变化，可在“管理与备份”重新关联。')
        return previous

def main():
    parser = argparse.ArgumentParser(description='停止映库后恢复资料库备份；不恢复或删除视频原片。')
    parser.add_argument('archive', nargs='?')
    parser.add_argument('--data-dir', default=str(ROOT/'data'))
    parser.add_argument('--yes', action='store_true')
    args = parser.parse_args()
    archive = args.archive or input('请粘贴备份 ZIP 的完整路径：').strip().strip('"')
    if not args.yes and input('这将回退片目、封面和观看记录；不会改动原片。输入 RESTORE 继续：') != 'RESTORE':
        print('已取消。')
        return
    try:
        restore(archive, args.data_dir)
    except Exception as exc:
        print('恢复失败：', exc)
        raise SystemExit(1)

if __name__ == '__main__':
    main()
