# [INPUT]: 固定工件、本机 llama.cpp 与任务取消信号。
# [OUTPUT]: 校验、启动、运行和停止离线推理。
# [POS]: providers 的本机推理后端。
# [PROTOCOL]: Update this header on change, then check AGENTS.md
"""Lazy, offline vision preset backed by a private llama.cpp child process."""
from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import os
import platform
import re
import secrets
import socket
import subprocess
import sys
import threading
import time

from .managed_vision_assets import MODEL_ID, PROJECTOR, RUNTIME_RELEASE, RUNTIMES, WEIGHTS


HOST = '127.0.0.1'
ENDPOINT = f'http://{HOST}'
IDLE_SECONDS = 120


def native_platform():
    machine = platform.machine().lower()
    if sys.platform == 'win32' and machine in ('x86_64', 'amd64'):
        return 'windows-x64'
    if sys.platform == 'darwin':
        if machine in ('arm64', 'aarch64'):
            return 'macos-arm64'
        if machine in ('x86_64', 'amd64'):
            return 'macos-x64'
    return None


class ManagedVision:
    def __init__(self, app, safe_path, stop):
        self.root = app/'ai'/'vision'
        self.platform = native_platform()
        self.safe_path = safe_path
        self.stop = stop
        self.lock = threading.RLock()
        self.serial = threading.Lock()
        self.process = None
        self.port = None
        self.key = None
        self.timer = None
        self.starting = False
        self.closed = False
        self.checked_files = {}
        self.last_error = None

    def _assets(self):
        if self.platform not in RUNTIMES:
            return 'unsupported_platform', '内置视觉模型支持 Windows x64 和原生 macOS。此平台可配置自己的本机模型服务。', None
        directory = self.root/'runtime'/self.platform
        name = 'llama-server.exe' if self.platform.startswith('windows') else 'llama-server'
        receipt = directory/'runtime-manifest.json'
        weights = [(self.root/'weights'/asset.name, asset) for asset in (WEIGHTS, PROJECTOR)]
        expected = [directory/name, receipt, *(path for path, _ in weights)]
        try:
            if any(not self.safe_path(path, self.root) for path in expected):
                raise ValueError()
            if any(not path.is_file() for path in expected):
                return 'missing_files', '离线视觉模型或本机运行组件未齐，请运行本页列出的准备命令。', None
            if any(path.stat().st_size != asset.size for path, asset in weights) or receipt.stat().st_size > 32*1024:
                raise ValueError()
            manifest = json.loads(receipt.read_text('utf-8'))
            if (not isinstance(manifest, dict) or manifest.get('release') != RUNTIME_RELEASE
                    or manifest.get('platform') != self.platform
                    or manifest.get('archive_sha256') != RUNTIMES[self.platform].sha256):
                raise ValueError()
            files = manifest.get('files')
            if not isinstance(files, dict) or not 1 <= len(files) <= 128 or name not in files:
                raise ValueError()
            library_suffix = '.dll' if self.platform.startswith('windows') else '.dylib'
            for leaf, digest in files.items():
                if (not re.fullmatch(r'[A-Za-z0-9._+-]{1,128}', leaf)
                        or (leaf != name and not leaf.lower().endswith(library_suffix))
                        or not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest)
                        or not self.safe_path(directory/leaf, self.root) or not (directory/leaf).is_file()):
                    raise ValueError()
            installed = {path.name for path in directory.iterdir()
                         if path.name == name or path.suffix.lower() in ('.exe', '.dll', '.dylib')}
            if installed != files.keys():
                raise ValueError()
        except (OSError, ValueError, TypeError):
            return 'invalid_files', '内置视觉文件与固定版本清单不符，请重新准备模型及运行组件。', None
        hashes = {path:asset.sha256 for path, asset in weights}
        hashes.update({directory/leaf:digest for leaf,digest in files.items()})
        return 'ready', '离线视觉文件齐全。使用时核验校验值并启动本机推理。', (directory/name, hashes)

    def status(self):
        status, reason, _ = self._assets()
        with self.lock:
            runtime = 'starting' if self.starting else 'running' if self.process and self.process.poll() is None else 'stopped'
            error = self.last_error
        return {'id':MODEL_ID,'name':'Qwen3-VL 2B 离线视觉概述','available':status=='ready','status':status,
                'reason':reason,'runtime_status':runtime,'runtime_error':error,'platform':self.platform,
                'weight_bytes':WEIGHTS.size+PROJECTOR.size,
                'setup_command':'python ai/setup_managed_vision.py' if sys.platform=='win32' else 'python3 ai/setup_managed_vision.py'}

    @staticmethod
    def _signature(path):
        info = path.stat()
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def _check_deadline(self, deadline):
        if self.closed or self.stop.is_set():
            raise RuntimeError('服务正在停止，已取消本机视觉推理。')
        if time.monotonic() >= deadline:
            raise RuntimeError('准备本机视觉模型超时，请重试或从较快的磁盘运行。')

    def _verify(self, hashes, deadline):
        checked = {}
        for path, expected in hashes.items():
            self._check_deadline(deadline)
            if not self.safe_path(path, self.root):
                raise RuntimeError('内置视觉文件路径出现链接，已停止启动。')
            signature = self._signature(path)
            if self.checked_files.get(path) != (signature, expected):
                digest = hashlib.sha256()
                with path.open('rb') as stream:
                    while block := stream.read(8*1024*1024):
                        self._check_deadline(deadline)
                        digest.update(block)
                if digest.hexdigest() != expected or self._signature(path) != signature:
                    raise RuntimeError('内置视觉文件校验失败，未启动模型。请重新准备该文件。')
            checked[path] = signature, expected
        self.checked_files = checked

    def _ensure(self, deadline):
        self._check_deadline(deadline)
        status, reason, assets = self._assets()
        if status != 'ready':
            raise RuntimeError(reason)
        executable, hashes = assets
        with self.lock:
            process = self.process
            existing = process is not None and process.poll() is None
            if existing and all(self.checked_files.get(path) == (self._signature(path),digest) for path,digest in hashes.items()):
                return f'{ENDPOINT}:{self.port}/v1', self.key
            self.starting = True
        self._stop_runtime()
        try:
            self._verify(hashes, deadline)
            with socket.socket() as listener:
                listener.bind((HOST,0))
                port = listener.getsockname()[1]
            key = secrets.token_urlsafe(32)
            threads = max(1,min(4,(os.cpu_count() or 2)//2))
            command = [str(executable),'--model',str(self.root/'weights'/WEIGHTS.name),
                       '--mmproj',str(self.root/'weights'/PROJECTOR.name),'--alias',MODEL_ID,
                       '--host',HOST,'--port',str(port),'--ctx-size','4096','--parallel','1',
                       '--threads',str(threads),'--threads-batch',str(threads),'--threads-http','2',
                       '--gpu-layers','0','--mmproj-device','none','--image-max-tokens','512',
                       '--offline','--no-webui','--no-agent','--no-slots','--no-cache-prompt','--log-disable']
            # Do not inherit proxy, tool, tunnel, HF or DLL-injection settings.
            env = {key:value for key,value in os.environ.items()
                   if key.upper() in {'SYSTEMROOT','WINDIR','SYSTEMDRIVE','TEMP','TMP','TMPDIR','LANG','LC_ALL'}}
            env['LLAMA_API_KEY'] = key
            self._check_deadline(deadline)
            process = subprocess.Popen(command,cwd=executable.parent,env=env,stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            with self.lock:
                self.process,self.port,self.key = process,port,key
            start_deadline = min(deadline,time.monotonic()+75)
            while True:
                self._check_deadline(start_deadline)
                if process.poll() is not None:
                    raise RuntimeError('本机视觉运行组件未能启动。请检查运行组件、系统兼容性与可用内存。')
                connection = http.client.HTTPConnection(HOST,port,timeout=min(.5,start_deadline-time.monotonic()))
                try:
                    connection.request('GET','/health')
                    response = connection.getresponse()
                    healthy = response.status == 200 and json.loads(response.read(1024)).get('status') == 'ok'
                    if healthy and process.poll() is None:
                        # Check that the endpoint rejects an unauthenticated
                        # empty request before any real sampling data is sent.
                        connection.close()
                        connection.request('POST','/v1/chat/completions',b'{}',{'Content-Type':'application/json'})
                        if connection.getresponse().status != 401:
                            raise RuntimeError('本机视觉组件的身份验证未生效，已停止推理。')
                        return f'{ENDPOINT}:{port}/v1', key
                except (OSError,ValueError,AttributeError,http.client.HTTPException):
                    pass
                finally:
                    connection.close()
                self.stop.wait(.2)
        except OSError as exc:
            raise RuntimeError('无法读取或启动本机视觉组件，请检查文件、权限与系统兼容性。') from exc
        finally:
            with self.lock:
                self.starting = False

    def _cancel_timer(self):
        if self.timer:
            self.timer.cancel()
            self.timer = None

    def _stop_runtime(self, expected_timer=None):
        with self.lock:
            if expected_timer is not None and expected_timer is not self.timer:
                return
            self._cancel_timer()
            process = self.process
            self.process = self.port = self.key = None
        if process and process.poll() is None:
            with contextlib.suppress(OSError):
                process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError,subprocess.TimeoutExpired):
                    process.kill()
                    process.wait(timeout=1)

    def run(self, send, deadline):
        with self.serial:
            with self.lock:
                self._cancel_timer()
            try:
                address, key = self._ensure(deadline)
                self._check_deadline(deadline)
                result = send(address,key)
            except BaseException as exc:
                with self.lock:
                    self.last_error = str(exc) if isinstance(exc,RuntimeError) else '本机视觉任务未完成。'
                self._stop_runtime()
                raise
            with self.lock:
                self.last_error = None
                if not self.closed and not self.stop.is_set():
                    timer = threading.Timer(IDLE_SECONDS,lambda:self._stop_runtime(timer))
                    timer.daemon = True
                    self.timer = timer
                    timer.start()
            return result

    def close(self):
        with self.lock:
            self.closed = True
        self._stop_runtime()
