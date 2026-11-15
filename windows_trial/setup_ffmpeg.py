"""[INPUT]: Windows x64、厂商固定归档地址和归档哈希。
[OUTPUT]: 程序目录内的 FFmpeg、ffprobe 和组件许可。
[POS]: 用户主动执行的可选便携工具准备入口。
[PROTOCOL]: Update this header on change, then check AGENTS.md
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import platform
import re
import shutil
import tempfile
import urllib.error
import urllib.request
import zipfile

APP = Path(__file__).resolve().parent.parent
DOWNLOAD = 'https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.2-essentials_build.zip'
ARCHIVE_BYTES = 114_768_076
ARCHIVE_SHA256 = '60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba'
SOURCE_DIR = 'ffmpeg-9.0.2-essentials_build'
FFMPEG_FILES = {'bin/ffmpeg.exe': 'ffmpeg.exe', 'bin/ffprobe.exe': 'ffprobe.exe', 'LICENSE': 'FFmpeg-LICENSE.txt'}
BLOCK_BYTES = 4 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise urllib.error.URLError('上游返回了重定向；请从文档链接下载固定版本。')


def sha256(path):
    with path.open('rb') as file:
        return hashlib.file_digest(file, 'sha256').hexdigest()


def download_archive(target):
    opener = urllib.request.build_opener(NoRedirect())
    offset = 0
    with target.open('xb') as file:
        while offset < ARCHIVE_BYTES:
            end = min(offset + BLOCK_BYTES, ARCHIVE_BYTES) - 1
            request = urllib.request.Request(
                DOWNLOAD,
                headers={'Accept-Encoding': 'identity', 'Range': f'bytes={offset}-{end}'},
            )
            with opener.open(request, timeout=30) as response:
                expected = ARCHIVE_BYTES
                if response.status == 206:
                    match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
                    if not match:
                        raise RuntimeError('上游缺少有效的分段下载信息。')
                    start, last, total = map(int, match.groups())
                    if start != offset or not offset <= last <= end or total != ARCHIVE_BYTES:
                        raise RuntimeError('上游分段与固定归档大小不符。')
                    expected = last - start + 1
                elif response.status != 200 or offset:
                    raise RuntimeError('上游返回了非预期的下载状态。')
                remaining = expected
                while remaining:
                    chunk = response.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise RuntimeError('下载未完成，请重新运行准备工具。')
                    file.write(chunk)
                    remaining -= len(chunk)
                if response.read(1):
                    raise RuntimeError('下载内容超出固定归档范围。')
            offset += expected
            print(f'媒体组件已下载 {offset * 100 // ARCHIVE_BYTES}%', flush=True)


def install_archive(archive, stage):
    if archive.stat().st_size != ARCHIVE_BYTES or sha256(archive) != ARCHIVE_SHA256:
        raise RuntimeError('归档校验失败，请重新下载固定版本。')
    destination = APP / 'bin'
    if destination.is_symlink() or destination.is_junction():
        raise RuntimeError('媒体工具目录是链接，请换到一个正常解压的程序目录。')
    destination.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        for member, name in FFMPEG_FILES.items():
            with bundle.open(f'{SOURCE_DIR}/{member}') as source, (stage / name).open('xb') as target:
                shutil.copyfileobj(source, target, 1024 * 1024)
    for name in FFMPEG_FILES.values():
        target = destination / name
        if target.exists() and (not target.is_file() or target.is_symlink() or sha256(target) != sha256(stage / name)):
            raise RuntimeError(f'{name} 已存在且版本不同；保留原文件。')
    for name in FFMPEG_FILES.values():
        target = destination / name
        if not target.exists():
            (stage / name).rename(target)


def main():
    parser = argparse.ArgumentParser(description='准备映库便携版所需的本机媒体工具。')
    parser.add_argument('--archive', type=Path, help='使用已下载的固定版本归档，不连接网络。')
    args = parser.parse_args()
    if os.name != 'nt' or platform.machine().casefold() != 'amd64':
        parser.exit(1, '请在 Windows x64 运行此工具。\n')
    if all((APP / 'bin' / name).is_file() for name in ('ffmpeg.exe', 'ffprobe.exe')):
        print('已有程序目录内的 FFmpeg 和 ffprobe，将继续使用现有组件。')
        return
    if args.archive is None and shutil.which('ffmpeg.exe') and shutil.which('ffprobe.exe'):
        print('已找到系统路径里的 FFmpeg 和 ffprobe，将继续使用现有组件。')
        return
    if args.archive is None:
        print('将从 Gyan 下载 FFmpeg 9.0.2，约 110 MiB。')
        print('它用于扫描自己的影片、生成预览和兼容播放。')
        print('组件仅保存到映库目录。请先关闭正在运行的映库。')
        print(DOWNLOAD)
        if input('下载并启用？输入 Y 继续：').strip().casefold() != 'y':
            print('已取消。自带样片仍可离线播放。')
            return
    with tempfile.TemporaryDirectory(prefix='.ffmpeg-setup-', dir=APP) as directory:
        stage = Path(directory)
        archive = args.archive or stage / 'upstream.zip'
        if args.archive is None:
            download_archive(archive)
        install_archive(archive, stage)
    print('设置完成。双击 Start-Trial.cmd 打开映库。')


if __name__ == '__main__':
    try:
        main()
    except (OSError, EOFError, RuntimeError, ValueError, zipfile.BadZipFile) as error:
        raise SystemExit(f'准备失败：{error}') from None
