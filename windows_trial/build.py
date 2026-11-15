"""[INPUT]: 公共源码、PSF 便携 Python、SQLite 和六段合成短片。
[OUTPUT]: 带逐文件校验列表的 Windows x64 试用 ZIP。
[POS]: 在发布前打包；保留已有同名包。
[PROTOCOL]: Update this header on change, then check AGENTS.md
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import tempfile
import zipfile

from setup_ffmpeg import sha256

APP = Path(__file__).resolve().parent.parent
PYTHON_DOWNLOAD = 'https://www.python.org/ftp/python/3.14.10/python-3.14.10-embed-amd64.zip'
PYTHON_SHA256 = '90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159'
SQLITE_DOWNLOAD = 'https://www.sqlite.org/2026/sqlite-dll-win-x64-3540100.zip'
SQLITE_SHA256 = 'f436f53ad8f965af43f3817b42c99c0689e6787f6b7ace208fe38dab7ffc4971'
SAMPLES = {
    'lighthouse': ('灯塔日落', '9ecb11be2a60643cc0c07c8a0c997883e20a958289d012966513c633bc6e12d0'),
    'comic': ('漫剧 · 分镜试片', '816024caac7fe97c8fb23cbfdbf3fe6c6d3c7a38b9a428d31b920ebfc2e9ecb8'),
    'valley': ('山谷列车', 'e86170bedba84b6951731c43069d1543927bb75469aa03582ad7b6c37c244ac2'),
    'observatory': ('山间观星台', '3fb21b56adf8afc9dfe3e34c58220836be0a17c23623f07e6d9aeba85039ee89'),
    'phone': ('手机日记 · 竖屏', '8254a527fe954f94cb914099ad6e387111f460fbf65d7a95b73790680b4c435d'),
    'bookshop': ('雨巷书店', '020ab6d2c612f7aeaa4d8bd811dfea73c60066ee8266f1c6863beadf52799e77'),
}
RUNTIME_FILES = re.compile(r'[A-Za-z0-9_.-]+\.(?:cat|dll|exe|pyd|_pth|txt|zip)\Z')
FIXED_TOP = (
    'LICENSE', 'launch.py', 'server.py', 'package_library.py', 'restore_backup.py',
    'verify_package.py', 'diagnose.py', 'ai/setup_managed_vision.py',
    'windows_trial/setup_ffmpeg.py',
)
MODULES = {
    'cinevault_services': {'.py'},
    'web': {'.css', '.html', '.js', '.png', '.svg', '.webmanifest'},
    'ai/vision': {'.json', '.txt'},
}
TRIAL_TOP = ('Start-Trial.cmd', 'Stop-Trial.cmd', 'Setup-FFmpeg.cmd', 'QUICK-START.md', 'THIRD-PARTY.md')
ARCHIVE_ROOT = 'CineVault-Local'


def source_files():
    for name in FIXED_TOP:
        yield name, APP / name
    for directory, extensions in MODULES.items():
        for source in sorted((APP / directory).iterdir()):
            if source.is_file() and source.suffix in extensions:
                yield source.relative_to(APP).as_posix(), source
    for name in TRIAL_TOP:
        yield name, APP / 'windows_trial' / name


def write_verified(archive, python_zip, sqlite_zip, samples):
    manifest = []

    def write(name, data):
        member = zipfile.ZipInfo(f'{ARCHIVE_ROOT}/{name}')
        member.external_attr = 0o100644 << 16
        archive.writestr(member, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=5)

    def add(name, data):
        write(name, data)
        manifest.append(f'{hashlib.sha256(data).hexdigest()}  {name}')

    for leaf, source in source_files():
        if not source.is_file() or source.is_symlink() or not source.resolve().is_relative_to(APP):
            raise RuntimeError(f'发布源码文件无效：{leaf}')
        add(leaf, source.read_bytes())

    if sha256(python_zip) != PYTHON_SHA256:
        raise RuntimeError(f'请使用 PSF 原始嵌入包：{PYTHON_DOWNLOAD}')
    if sha256(sqlite_zip) != SQLITE_SHA256:
        raise RuntimeError(f'请使用 SQLite 官方 x64 归档：{SQLITE_DOWNLOAD}')
    with zipfile.ZipFile(sqlite_zip) as sqlite:
        sqlite_dll = sqlite.read('sqlite3.dll')
    with zipfile.ZipFile(python_zip) as runtime:
        for member in sorted(runtime.namelist()):
            if not RUNTIME_FILES.fullmatch(member):
                raise RuntimeError(f'便携 Python 包含非预期成员：{member}')
            content = sqlite_dll if member == 'sqlite3.dll' else runtime.read(member)
            add('runtime/windows-x64/' + member, content)

    for slug, (title, expected) in SAMPLES.items():
        source = samples / (slug + '.mp4')
        if not source.is_file() or source.is_symlink() or sha256(source) != expected:
            raise RuntimeError(f'合成样片缺失或校验失败：{source.name}')
        add('samples/media/' + title + '.mp4', source.read_bytes())

    write('SHA256SUMS.txt', ('\n'.join(manifest) + '\n').encode('utf-8'))


def main():
    parser = argparse.ArgumentParser(description='构建 Windows 便携试用包。')
    parser.add_argument('--python-zip', type=Path, required=True)
    parser.add_argument('--sqlite-zip', type=Path, required=True)
    parser.add_argument('--samples', type=Path, default=APP / 'site' / 'assets')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    destination = args.output.resolve()
    if destination.is_relative_to(APP) or destination.exists():
        parser.error('请在源码目录之外指定新的 ZIP 路径。')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='cinevault-trial-build-', dir=destination.parent) as working:
        pending = Path(working) / destination.name
        with zipfile.ZipFile(pending, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=5) as archive:
            write_verified(archive, args.python_zip, args.sqlite_zip, args.samples)
        with zipfile.ZipFile(pending) as archive:
            if archive.testzip() is not None:
                raise RuntimeError('便携 ZIP 完整性检查失败。')
        pending.rename(destination)
    print(f'{sha256(destination)}  {destination.name}')


if __name__ == '__main__':
    main()
