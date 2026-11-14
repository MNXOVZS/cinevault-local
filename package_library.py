# [INPUT]: 资料库锁、SQLite 快照和文件清单。
# [OUTPUT]: 程序包、片目包与便携包。
# [POS]: 可离线运行的迁移打包工具。
# [PROTOCOL]: Update this header on change, then check AGENTS.md
"""Build a verified, portable ZIP without moving or renaming any source media.

Program-only exports can run online. Catalogue/media exports require the shared
library lock, so stop CineVault first. External source folders are never copied.
"""
from __future__ import annotations
import argparse
import contextlib
import errno
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import sys
import tempfile
import zipfile

APP = Path(__file__).resolve().parent
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))
from restore_backup import exclusive_library

PROFILES = ('program', 'catalog', 'portable')
TARGETS = ('universal', 'windows', 'mac')
TEXT_EXTENSIONS = {'.py', '.md', '.cmd', '.command', '.txt'}

def is_link(path):
    return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)

def files_under(root):
    if not root.exists():
        return
    if is_link(root):
        raise ValueError('打包目录不能是符号链接或联接：' + str(root))
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if not is_link(Path(current)/name) and name != '__pycache__']
        for name in sorted(files):
            item = Path(current)/name
            if not is_link(item) and item.is_file():
                yield item

def program_files(app, target):
    for item in sorted(app.iterdir()):
        if item.is_file() and not is_link(item) and (item.suffix.lower() in TEXT_EXTENSIONS or item.name == '.gitignore'):
            if item.suffix.lower() == '.cmd' and target == 'mac':
                continue
            if item.suffix.lower() == '.command' and target == 'windows':
                continue
            yield item
    for dirname in ('web', 'licenses'):
        yield from files_under(app/dirname)
    yield from files_under(app/'cinevault_services')
    for item in files_under(app/'frontend'/'src'):
        yield item
    for name in ('package.json','package-lock.json','tsconfig.json','build.mjs','README.md'):
        item=app/'frontend'/name
        if item.is_file() and not is_link(item):
            yield item
    for item in files_under(app/'ai'):
        relative = item.relative_to(app/'ai')
        if relative.parts[0] == 'site-packages' and target == 'mac':
            continue
        if relative.parts[0] == 'site-packages-macos' and target == 'windows':
            continue
        if relative.parts[:2] == ('vision', 'runtime') and len(relative.parts) >= 3:
            platform_dir = relative.parts[2]
            if (target == 'mac' and platform_dir.startswith('windows-') or
                    target == 'windows' and platform_dir.startswith('macos-')):
                continue
        if item.name.endswith(('.download', '.partial', '.download-part')):
            continue
        yield item
    if target in ('universal', 'windows'):
        yield from files_under(app/'bin'/'mpv')
        entry = app/'映库.exe'
        if entry.is_file() and not is_link(entry):
            yield entry
        yield from files_under(app/'desktop')
        yield from files_under(app/'runtime'/'windows-x64')
        for filename in ('ffmpeg.exe', 'ffprobe.exe'):
            item = app/'bin'/filename
            if item.is_file() and not is_link(item):
                yield item
    if target in ('universal', 'mac'):
        for dirname in ('macos-arm64', 'macos-x64'):
            yield from files_under(app/'bin'/dirname)

def write_entry(archive, name, source=None, contents=None):
    name = 'CineVault/' + name
    info = zipfile.ZipInfo(name, date_time=datetime.now().timetuple()[:6])
    info.create_system = 3
    info.external_attr = (0o100755 if name.endswith('.command') else 0o100644) << 16
    info.compress_type = zipfile.ZIP_STORED if Path(name).suffix.lower() in ('.mp4','.mkv','.mov','.avi','.jpg','.jpeg','.webp','.png','.zip','.gif','.heic','.avif') else zipfile.ZIP_DEFLATED
    digest = hashlib.sha256()
    size = 0
    before = source.stat() if source else None
    with archive.open(info, 'w', force_zip64=True) as writer:
        if source:
            with source.open('rb') as reader:
                while block := reader.read(1024*1024):
                    writer.write(block)
                    digest.update(block)
                    size += len(block)
        else:
            block = contents.encode('utf-8') if isinstance(contents, str) else contents
            writer.write(block)
            digest.update(block)
            size = len(block)
    if source:
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or size != before.st_size:
            raise RuntimeError('打包期间文件发生变化，请关闭修改它的程序再试：' + str(source))
    return {'path':name, 'size':size, 'sha256':digest.hexdigest()}

def verify_package(path):
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if len(set(names)) != len(names):
            raise ValueError('压缩包包含重复路径。')
        for entry in entries:
            relative = PurePosixPath(entry.filename)
            if relative.is_absolute() or '..' in relative.parts or '\\' in entry.filename or ':' in entry.filename or relative.parts[:1] != ('CineVault',):
                raise ValueError('压缩包路径不合法。')
            if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('压缩包包含符号链接。')
        manifest_path = 'CineVault/PACKAGE-MANIFEST.json'
        info = archive.getinfo(manifest_path)
        if info.file_size > 32*1024*1024:
            raise ValueError('打包清单过大。')
        manifest = json.loads(archive.read(manifest_path))
        expected = {entry['path']:entry for entry in manifest['files']}
        if len(expected) != len(manifest['files']) or set(names) != set(expected) | {manifest_path}:
            raise ValueError('文件列表与打包清单不一致。')
        for name, entry in expected.items():
            actual = archive.getinfo(name)
            if actual.file_size != entry['size']:
                raise ValueError('文件大小不匹配：' + name)
            digest = hashlib.sha256()
            with archive.open(name) as reader:
                while block := reader.read(1024*1024):
                    digest.update(block)
            if digest.hexdigest() != entry['sha256']:
                raise ValueError('文件校验失败：' + name)
        return manifest

def publish_without_overwrite(partial, destination):
    """Keep an unrelated destination safe, including a late-arriving file."""
    if os.name == 'nt':
        # Unlike POSIX rename, Windows rename refuses an existing destination.
        os.rename(partial,destination)
        return
    try:
        os.link(partial,destination)
    except FileExistsError:
        raise
    except OSError as exc:
        if exc.errno not in (errno.EPERM,errno.EOPNOTSUPP,errno.ENOTSUP,errno.EXDEV):
            raise
        # FAT/exFAT may lack hard links. Exclusive creation still never clobbers
        # an existing file, but a power loss can leave an incomplete destination.
        # Keep the already verified .partial until the final copy is verified.
        created = None
        try:
            with destination.open('xb') as writer, partial.open('rb') as reader:
                created = os.fstat(writer.fileno())
                shutil.copyfileobj(reader,writer,1024*1024)
                writer.flush()
                os.fsync(writer.fileno())
            verify_package(destination)
            partial.unlink()
        except BaseException:
            if created is not None:
                with contextlib.suppress(OSError):
                    actual = destination.stat()
                    if (actual.st_dev,actual.st_ino)==(created.st_dev,created.st_ino):
                        destination.unlink()
            raise
    else:
        partial.unlink()

def build_package(destination, profile='catalog', target='universal', app_dir=APP, data_dir=None):
    if profile not in PROFILES or target not in TARGETS:
        raise ValueError('打包类型或目标平台无效。')
    app = Path(app_dir).resolve()
    data = Path(data_dir).resolve() if data_dir else app/'data'
    destination = Path(destination).expanduser().resolve()
    if destination == app or app in destination.parents or destination == data or data in destination.parents:
        raise ValueError('请将 ZIP 保存到映库文件夹外，避免重复打包自身。')
    if destination.exists():
        raise ValueError('目标文件已存在，请使用新的文件名。')
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name+'.partial')
    if partial.exists():
        raise ValueError('存在同名未完成包，请换一个文件名。')
    lock = exclusive_library(data) if profile != 'program' else contextlib.nullcontext()
    with lock, tempfile.TemporaryDirectory(prefix='cinevault-package-') as temporary:
        temp = Path(temporary)
        originals = []
        sources = []
        media_counts = {'video':0, 'image':0}
        registered_ids = set()
        preview_count = 0
        database_snapshot = None
        if profile != 'program':
            database = data/'library.sqlite3'
            if not database.is_file() or is_link(database):
                raise ValueError('找不到正常的资料库数据库。')
            database_snapshot = temp/'library.sqlite3'
            source = sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)
            backup = sqlite3.connect(database_snapshot)
            try:
                source.backup(backup)
                if backup.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('数据库检查失败，已停止打包。')
                sources = [json.loads(row[0]) for row in source.execute('SELECT data FROM sources')]
                for key, raw in source.execute('SELECT id,data FROM videos'):
                    registered_ids.add(key)
                    item = json.loads(raw)
                    kind = item.get('kind','video')
                    media_counts[kind] = media_counts.get(kind,0)+1
            finally:
                backup.close()
                source.close()
            if profile == 'portable':
                originals = list(files_under(data/'media'))
        selected = [(path, path.relative_to(app).as_posix()) for path in program_files(app,target)]
        if database_snapshot:
            selected.append((database_snapshot,'data/library.sqlite3'))
            for path in files_under(data/'assets'):
                relative = path.relative_to(data/'assets')
                if len(relative.parts) != 2 or relative.parts[0] not in registered_ids:
                    continue
                # Retain registered silent previews. Compatible playback
                # transcodes remain disposable and can be much larger.
                if path.name == 'preview.mp4' or path.suffix.lower() in ('.jpg','.jpeg','.png','.webp','.vtt'):
                    selected.append((path,'data/'+path.relative_to(data).as_posix()))
                    preview_count += path.name == 'preview.mp4'
            selected.extend((path,'data/'+path.relative_to(data).as_posix()) for path in originals)
        total_bytes = sum(path.stat().st_size for path,_ in selected)
        if shutil.disk_usage(destination.parent).free < total_bytes + 64*1024*1024:
            raise RuntimeError('目标空间不足；为避免中途失败，需要至少约源文件总大小的可用空间。')
        version_source = (app/'server.py').read_text(encoding='utf-8')
        match = re.search(r'^VERSION\s*=\s*[\'"]([^\'"]+)',version_source,re.M)
        manifest = {'app':'cinevault','format_version':1,'app_version':match.group(1) if match else 'unknown',
                    'created_at':datetime.now(timezone.utc).isoformat(),'profile':profile,'target':target,
                    'external_originals_included':False,'owned_originals_included':profile=='portable',
                    'included_owned_files':len(originals),'catalog_counts':media_counts,
                    'included_registered_previews':preview_count,
                    'credentials_included':False,'logs_included':False,'historical_backups_included':False,
                    'compatible_transcodes_included':False,'encrypted':False,
                    'mac_requires_native_runtime':True,'files':[]}
        migration = {'profile':profile,'external_originals_included':False,
                     'external_sources':[{'id':source.get('id'),'old_path':source.get('path'),'action':'复制原片目录或接回原硬盘后，在管理页重新关联路径。'} for source in sources],
                     'note':'程序包不含资料；catalog 不含任何原片；portable 只带 data/media 内原片，不会复制外部硬盘。'}
        guidance = f'''映库迁移包\n\n内容模式：{profile}\n目标平台：{target}\n这个 ZIP 没有加密，片名、封面和原片（如选择包含）都属于私人资料，请妥善保管。\n\nprogram：只有程序，无收藏。\ncatalog：程序、数据库、封面和预览，不含原片。\nportable：再带 data/media 内的原片；外部硬盘原片始终不包含。\n\n运行 python verify_package.py 检查包。\n在新位置解压 CineVault；不要覆盖已有且有收藏的映库目录。\nWindows 双击 Start-Windows.cmd。\nMac 准备 Python 3.11+ 与 FFmpeg/FFprobe 后，执行 bash Start-Mac.command。\n迁移目录与数据跨平台，Windows 运行组件不能在 Mac 上运行。\n外部硬盘目录请参考 MIGRATION.json，在管理页更换路径。\n访问口令、日志、历史备份和兼容转码不随包复制；新电脑会生成新口令。\n打包清单逐项带 SHA-256，可查损坏；它不是数字签名，不能证明文件来源可信。\n'''
        try:
            with zipfile.ZipFile(partial,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=2,allowZip64=True) as archive:
                for path,name in selected:
                    manifest['files'].append(write_entry(archive,name,source=path))
                for name,contents in [('MIGRATION.json',json.dumps(migration,ensure_ascii=False,indent=2)),('迁移包说明.txt',guidance)]:
                    manifest['files'].append(write_entry(archive,name,contents=contents))
                write_entry(archive,'PACKAGE-MANIFEST.json',contents=json.dumps(manifest,ensure_ascii=False,indent=2))
            verify_package(partial)
            publish_without_overwrite(partial,destination)
        except BaseException:
            with contextlib.suppress(OSError): partial.unlink()
            raise
    return manifest

def main():
    parser = argparse.ArgumentParser(description='打包映库；不修改原片，不自动复制外部硬盘。')
    parser.add_argument('--profile',choices=PROFILES,default='catalog')
    parser.add_argument('--target',choices=TARGETS,default='universal')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--data-dir',type=Path)
    parser.add_argument('--interactive',action='store_true')
    args = parser.parse_args()
    if args.interactive:
        print('先停止映库，再打包片目或原片。打包只读取文件，不改名、不移动外部原片。')
        args.target = {'1':'universal','2':'windows','3':'mac'}.get(input('目标：1 通用，2 Windows，3 Mac [1]：').strip(),'universal')
        args.profile = {'1':'program','2':'catalog','3':'portable'}.get(input('内容：1 仅程序，2 程序+片目/封面，3 再包含库内原片 [2]：').strip(),'catalog')
        print('注意：外部硬盘原片从不自动包含。ZIP 不加密。')
        if input('输入 PACK 开始：').strip() != 'PACK':
            print('已取消。')
            return
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    destination = args.output or APP.parent/f'CineVault-{args.target}-{args.profile}-{stamp}.zip'
    try:
        manifest = build_package(destination,args.profile,args.target,data_dir=args.data_dir)
        print('打包并校验完成：',destination.resolve())
        print('模式：',manifest['profile'],'；库内原片文件数：',manifest['included_owned_files'],'；外部原片：不包含。')
    except Exception as exc:
        print('打包失败：',exc)
        raise SystemExit(1)

if __name__ == '__main__':
    main()
