# [INPUT]: 固定上游清单与本机文件系统。
# [OUTPUT]: 核验并安装离线视觉工件。
# [POS]: 可选的模型准备入口。
# [PROTOCOL]: Update this header on change, then check AGENTS.md
"""Download the pinned offline vision package or its native runtime on demand."""
from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import stat
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile


APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))
from cinevault_services.managed_vision_assets import PROJECTOR, RUNTIME_RELEASE, RUNTIMES, WEIGHTS

VISION = APP/'ai'/'vision'
MIB = 1024*1024
ALLOWED_HOSTS = frozenset({'huggingface.co', 'cas-bridge.xethub.hf.co', 'cdn-lfs.huggingface.co',
                           'cdn-lfs-us-1.hf.co', 'cdn-lfs-eu-1.hf.co', 'us.aws.cdn.hf.co',
                           'eu.aws.cdn.hf.co', 'api.github.com',
                           'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'})


def native_platform():
    machine = platform.machine().lower()
    if sys.platform == 'win32' and machine in ('x86_64', 'amd64'):
        return 'windows-x64'
    if sys.platform == 'darwin':
        if machine in ('aarch64', 'arm64'):
            return 'macos-arm64'
        if machine in ('x86_64', 'amd64'):
            return 'macos-x64'
    return None


def allowed_url(url):
    parsed = urllib.parse.urlsplit(url)
    return parsed.scheme == 'https' and parsed.hostname in ALLOWED_HOSTS and parsed.port in (None, 443) and not parsed.username and not parsed.password


class PinnedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed_url(newurl):
            raise RuntimeError('上游跳转到未登记的主机，停止下载。')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def sha256_matches(path, asset):
    if path.is_symlink() or not path.is_file() or path.stat().st_size != asset.size:
        return False
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest() == asset.sha256


def fetch(asset, destination, workers, chunk_size):
    if sha256_matches(destination, asset):
        return
    if destination.exists():
        raise RuntimeError(f'已有文件的长度或 SHA-256 不匹配，请先将它移走：{destination.name}')
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=destination.name+'-', suffix='.partial', delete=False) as stream:
            partial = Path(stream.name)
            stream.truncate(asset.size)
        if not allowed_url(asset.url):
            raise RuntimeError('下载源未登记。')
        opener = urllib.request.build_opener(PinnedRedirect())
        headers = {'User-Agent':'CineVault-offline-vision', 'Accept':'application/octet-stream',
                   'X-GitHub-Api-Version':'2022-11-28', 'Accept-Encoding':'identity'}
        lock = threading.Lock()
        received = 0
        last_report = 0
        stop = threading.Event()
        first_target = asset.url

        def read_range(start, use_redirect):
            nonlocal received, last_report
            end = min(start+chunk_size, asset.size)-1
            expected = f'bytes {start}-{end}/{asset.size}'
            size = end-start+1
            for attempt in range(5):
                if stop.is_set():
                    return None
                url = first_target if use_redirect and attempt == 0 else asset.url
                if url == asset.url and urllib.parse.urlsplit(url).hostname == 'huggingface.co':
                    # Let the official resolver negotiate a fresh signed range
                    # if a CDN has returned a cached response for another range.
                    url += '?download=true&cv_range='+str(start)
                request = urllib.request.Request(url, headers=dict(headers, Range=f'bytes={start}-{end}'))
                try:
                    with opener.open(request, timeout=30) as response, partial.open('r+b') as output:
                        if response.status != 206 or response.headers.get('Content-Range') != expected:
                            raise RuntimeError('上游未返回预期的文件范围。')
                        remaining = size
                        output.seek(start)
                        while remaining:
                            if stop.is_set():
                                return None
                            block = response.read(min(MIB, remaining))
                            if not block:
                                raise RuntimeError('上游提前结束了文件范围。')
                            output.write(block)
                            remaining -= len(block)
                        with lock:
                            received += size
                            if received-last_report >= 64*MIB or received == asset.size:
                                print(f'{asset.name}: {received/MIB:.0f}/{asset.size/MIB:.0f} MiB', flush=True)
                                last_report = received
                        return response.geturl()
                except (OSError, RuntimeError) as exc:
                    if attempt == 4:
                        stop.set()
                        raise RuntimeError(f'{asset.name} 的范围下载失败（{type(exc).__name__}）。') from None
                    time.sleep(attempt+1)

        first_target = read_range(0, False)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for future in concurrent.futures.as_completed(
                pool.submit(read_range, offset, True) for offset in range(chunk_size, asset.size, chunk_size)
            ):
                future.result()
        if not sha256_matches(partial, asset):
            raise RuntimeError(f'下载文件 SHA-256 校验失败：{asset.name}')
        # Move the complete checked file into view only after all ranges passed.
        os.replace(partial, destination)
        print(f'{asset.name}: SHA-256 {asset.sha256}', flush=True)
    finally:
        if partial is not None:
            with contextlib.suppress(FileNotFoundError):
                partial.unlink()


def extract_runtime(archive, target, native):
    def wanted(name):
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or '\\' in name:
            raise RuntimeError('运行时压缩包的文件路径异常。')
        return path.name in ('llama-server', 'llama-server.exe') or path.suffix in ('.dll', '.dylib')

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise RuntimeError('运行时目标已经存在，请先移走旧运行时目录。')
    with tempfile.TemporaryDirectory(prefix='vision-runtime-', dir=target.parent) as directory:
        stage = Path(directory)
        names = set()

        def write(name, stream, executable=False):
            leaf = PurePosixPath(name).name
            if leaf.casefold() in names:
                raise RuntimeError('运行时压缩包中存在重名文件。')
            names.add(leaf.casefold())
            with (stage/leaf).open('xb') as output:
                shutil.copyfileobj(stream, output)
            if executable:
                (stage/leaf).chmod(0o755)

        if native.startswith('windows'):
            with zipfile.ZipFile(archive) as bundle:
                for member in bundle.infolist():
                    if member.is_dir() or not wanted(member.filename):
                        continue
                    if stat.S_ISLNK(member.external_attr>>16):
                        raise RuntimeError('运行时压缩包包含异常链接。')
                    with bundle.open(member) as source:
                        write(member.filename, source)
        else:
            with tarfile.open(archive, 'r:gz') as bundle:
                for member in bundle.getmembers():
                    if not wanted(member.name):
                        continue
                    if not (member.isfile() or member.issym() or member.islnk()):
                        raise RuntimeError('运行时压缩包包含异常文件。')
                    # tarfile resolves archive-internal links. Write regular
                    # copies so the portable folder does not depend on symlinks.
                    source = bundle.extractfile(member)
                    if source is None:
                        raise RuntimeError('运行时压缩包中的链接缺少目标。')
                    with source:
                        write(member.name, source, True)
        exe = 'llama-server.exe' if native.startswith('windows') else 'llama-server'
        if not (stage/exe).is_file():
            raise RuntimeError('上游压缩包中缺少 llama-server。')
        write_runtime_manifest(stage,native)
        stage.rename(target)


def write_runtime_manifest(target, native):
    hashes = {}
    for path in sorted(target.iterdir()):
        if path.name not in ('llama-server','llama-server.exe') and path.suffix not in ('.dll','.dylib'):
            continue
        if path.is_symlink() or not path.is_file():
            raise RuntimeError('运行时组件必须为普通文件。')
        with path.open('rb') as stream:
            hashes[path.name] = hashlib.file_digest(stream,'sha256').hexdigest()
    (target/'release.txt').write_text(RUNTIME_RELEASE+'\n', encoding='ascii')
    manifest = {'release':RUNTIME_RELEASE,'platform':native,'archive_sha256':RUNTIMES[native].sha256,'files':hashes}
    (target/'runtime-manifest.json').write_text(json.dumps(manifest,sort_keys=True,indent=2)+'\n',encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description='准备固定版本的映库本地视觉概述模型。')
    parser.add_argument('--runtime-only', action='store_true', help='只为当前平台准备运行组件，保留随程序携带的权重。')
    parser.add_argument('--cache-dir', type=Path, default=VISION/'download-cache')
    args = parser.parse_args(argv)
    native = native_platform()
    if native not in RUNTIMES:
        parser.error('此安装器仅提供 Windows x64 与 macOS 原生运行组件。')
    spec = RUNTIMES[native]
    runtime = VISION/'runtime'/native
    exe = runtime/('llama-server.exe' if native.startswith('windows') else 'llama-server')
    jobs = []
    if not exe.is_file():
        jobs.append((spec,args.cache_dir/spec.name,3,4*MIB))
    elif not (runtime/'runtime-manifest.json').is_file():
        raise RuntimeError('运行组件缺少校验清单。请将当前平台运行组件移出后重新准备。')
    if not args.runtime_only:
        jobs.extend(((WEIGHTS,VISION/'weights'/WEIGHTS.name,6,16*MIB),
                     (PROJECTOR,VISION/'weights'/PROJECTOR.name,3,16*MIB)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        for result in concurrent.futures.as_completed(pool.submit(fetch,*job) for job in jobs):
            result.result()
    if not exe.is_file():
        archive = args.cache_dir/spec.name
        extract_runtime(archive,runtime,native)
        archive.unlink()
    print(f'本机运行组件就绪：llama.cpp {RUNTIME_RELEASE} / {native}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
