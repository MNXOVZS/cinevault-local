"""Explain observed media failures; never infer support from an extension alone."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import threading
import time


class CompatibilityService:
    def __init__(self, library):
        self.lib = library
        self.lock = threading.Lock()
        self.cached = None
        self.cached_at = 0.0
        self.cached_signature = None

    def _signature(self):
        result = []
        for executable in (self.lib.ffmpeg, self.lib.ffprobe):
            try:
                info = Path(executable).stat()
                result.append((str(executable), info.st_size, info.st_mtime_ns))
            except (OSError, TypeError):
                result.append(None)
        return tuple(result)

    @staticmethod
    def _run(executable, option):
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        result = subprocess.run([str(executable), '-hide_banner', option], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=12, creationflags=flags)
        if result.returncode:
            raise RuntimeError('媒体工具未成功返回能力列表。')
        return result.stdout.decode('utf-8', errors='replace')

    def capabilities(self, force=False):
        signature = self._signature()
        with self.lock:
            if not force and self.cached and signature == self.cached_signature and time.monotonic() - self.cached_at < 3600:
                return dict(self.cached, cached=True)
            result = {'ffmpeg_available': bool(self.lib.ffmpeg), 'ffprobe_available': bool(self.lib.ffprobe),
                'video_decoders': [], 'audio_decoders': [], 'demuxers': [], 'errors': [],
                'notice': '这是当前本机工具的编译能力清单，不证明某个文件已完整解码或浏览器一定可播放。系统或浏览器插件不会自动增加这份 FFmpeg 构建的解码能力。',
                'cached': False}
            if self.lib.ffmpeg:
                try:
                    version = self._run(self.lib.ffmpeg, '-version')
                    result['ffmpeg_version'] = version.splitlines()[0][:240] if version else '未返回版本'
                    decoders = self._run(self.lib.ffmpeg, '-decoders')
                    for line in decoders.splitlines():
                        match = re.match(r'^\s*([VAS][A-Z.]{5})\s+(\S+)\s+', line)
                        if match and match[2] != '=':
                            if match[1][0] == 'V': result['video_decoders'].append(match[2])
                            elif match[1][0] == 'A': result['audio_decoders'].append(match[2])
                    demuxers = self._run(self.lib.ffmpeg, '-demuxers')
                    for line in demuxers.splitlines():
                        match = re.match(r'^\s*D\s+(\S+)\s+', line)
                        if match and match[1] != '=':
                            result['demuxers'].extend(match[1].split(','))
                except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                    result['errors'].append(type(exc).__name__ + '：能力查询失败或超时，请检查本机媒体工具。')
            for field in ('video_decoders', 'audio_decoders', 'demuxers'):
                result[field] = sorted(set(result[field]))
                result[field + '_count'] = len(result[field])
            self.cached, self.cached_at, self.cached_signature = result, time.monotonic(), signature
            return dict(result)

    def explain(self, video):
        evidence = []
        for field, stage in (('error', '基础分析/校验'), ('preview_error', '短片预览'),
                             ('playback_error', '兼容播放')):
            message = video.get(field)
            if isinstance(message, str) and message.strip():
                evidence.append({'stage': stage, 'message': message.strip()[:1400]})
        text = '\n'.join(item['message'] for item in evidence).lower()
        status = video.get('status')
        codec = str(video.get('codec') or '')
        container = str(video.get('container') or '')
        candidate = 'unknown'
        if video.get('kind') == 'image':
            candidate = 'image'
        elif status == 'ready':
            candidate = 'direct' if self.lib.playback.original_safe(video) else 'needs_prepare'
        result = {'category': 'ready_unverified', 'severity': 'info', 'title': '参数已登记，实际播放仍需验证',
            'explanation': '成功读取时长、编码或生成短片，不等于已经完整解码全片；网页播放还受系统和浏览器能力影响。',
            'recommendation': '可打开播放；遇到旧编码时准备兼容副本，或明确使用本机原生播放器。',
            'actions': [{'id': 'open', 'label': '查看资料'}, {'id': 'prepare_playback', 'label': '准备兼容播放'}],
            'evidence': evidence, 'codec': codec or None, 'container': container or None,
            'browser_candidate': candidate, 'full_playback_verified': False}

        def change(category, severity, title, explanation, recommendation, actions):
            result.update(category=category, severity=severity, title=title, explanation=explanation,
                          recommendation=recommendation,
                          actions=[{'id': ident, 'label': label} for ident, label in actions])

        if status == 'missing':
            change('source_unavailable', 'warning', '原片当前不可用',
                '可能是硬盘未连接、挂载位置变化、权限或文件缺失；当前记录不足以证明文件已永久丢失。',
                '先连接并核对原硬盘及来源路径，再重新扫描或校验。保留片目，不重复移动原片。',
                [('source', '检查来源'), ('verify', '校验原片')])
        elif status == 'changed':
            change('source_changed', 'warning', '原片发生变化，需要核对',
                '已登记的大小、修改时间或指纹与当前文件不同，旧缓存不能证明新文件内容。',
                '先校验并确认当前文件，再重新生成派生预览。', [('verify', '校验原片')])
        elif status in ('queued', 'analyzing'):
            change('base_pending', 'info', '基础分析尚未完成',
                '正在等待文件指纹、格式探测或抽帧，预览不能先于这些步骤完成。',
                '查看当前任务与排队数量；大文件校验和慢盘读取可能占用较多时间。', [('tasks', '查看任务')])
        elif not self.lib.ffmpeg or not self.lib.ffprobe:
            change('missing_tools', 'error', '媒体处理工具缺失',
                '没有找到 FFmpeg 或 FFprobe，无法正常探测或生成视频预览。',
                '恢复本程序对应平台的媒体工具；Mac 首次运行配置脚本。不要用网页插件替代本机媒体引擎。',
                [('capabilities', '查看媒体引擎')])
        elif re.search(r'encrypt|\bdrm\b|decrypt|no key|密钥|加密', text):
            change('protected_media', 'warning', '可能是加密或受保护媒体',
                '错误提示涉及加密或密钥，单纯安装解码包未必有用。',
                '使用获得授权的播放方式；当前程序不提供绕过保护或保证转换的能力。', [('external', '原生播放器')])
        elif re.search(r'no space|disk.*full|空间不足|空间已满|errno 28', text):
            change('storage_limit', 'error', '派生输出空间不足',
                '失败原因涉及写入空间，不等于原片编码不支持。',
                '查看缓存占用并清理可再生成缓存，保留原片与独立备份后再重试。', [('cache', '管理缓存')])
        elif re.search(r'permission|access.*denied|拒绝访问|权限|errno 13', text):
            change('access_denied', 'error', '当前无法访问文件',
                '可能是权限、占用或磁盘连接状态，尚不能判断编码是否支持。',
                '在本机核对文件与硬盘的可读权限，然后重试分析；不需要扩大整个硬盘的删除权限。',
                [('source', '检查来源'), ('retry_analysis', '重试分析')])
        elif re.search(r'timeout|timed out|time.*limit|超时', text):
            change('timeout', 'warning', '处理超时，格式支持尚未确定',
                '源盘慢、索引缺失、超长片或异常文件均可能导致超时；不能据此判定完全不支持。',
                '检查磁盘与文件状态，可明确重试；必要时使用本机原生播放器核对。',
                [('retry_preview', '重试预览'), ('external', '原生播放器')])
        elif re.search(r'decoder.*not found|unknown decoder|no decoder|unsupported codec|codec.*not.*supported|解码器.*缺|未找到.*解码', text):
            change('decoder_missing', 'error', '当前工具可能缺少所需解码能力',
                '错误明确涉及解码器或编码支持，需要结合实际编码及当前构建清单确认。',
                '查看媒体引擎能力。可评估官方较完整的 FFmpeg 构建或本机 mpv；K-Lite、浏览器扩展不会自动改变本程序的独立 FFmpeg。',
                [('capabilities', '查看媒体引擎'), ('external', '原生播放器')])
        elif re.search(r'moov atom not found|invalid data found|corrupt|truncat|end of file|文件.*损坏|无效.*数据', text):
            change('incomplete_or_unknown_media', 'error', '文件可能不完整、异常或尚不受支持',
                '已有错误不足以区分损坏、下载未完成、扩展名不符和工具不支持。',
                '先保留独立原片备份，再校验或完整解码体检；安装插件不能保证修复。不要覆盖原片尝试修复。',
                [('verify', '校验原片'), ('deep_verify', '完整解码体检'), ('external', '原生播放器')])
        elif evidence or status == 'error':
            change('processing_error', 'error', '处理失败，原因待核对',
                '请查看对应阶段的原始错误；没有足够证据将其统一归为“古老格式不支持”。',
                '先核对原片与磁盘，再选择重试分析、预览或兼容播放。',
                [('retry_analysis', '重试分析'), ('retry_preview', '重试预览'), ('external', '原生播放器')])
        elif video.get('preview_status') in ('queued', 'processing'):
            change('preview_pending', 'info', '短片预览正在排队或处理',
                '该项已有准备任务，不应因等待重复创建相同任务。',
                '查看来源进度和前面的共享队列任务；进度为真实完成数量，估时不足时显示未知。', [('tasks', '查看任务')])
        elif video.get('kind') != 'image' and not self.lib.playback.preview_is_current(video):
            change('preview_not_prepared', 'info', '预览尚未准备或需要升级',
                '尚未生成当前版本短片，不能据此认定原片不可播放。',
                '为此来源或文件夹提交预览准备，再观察排队、完成与失败数量。', [('prepare_preview', '准备预览')])
        elif video.get('kind') == 'image':
            change('image_ready', 'info', '图片参数与预览已登记',
                '特殊图片格式可使用派生 JPEG 查看；这不是视频解码或播放验证。',
                '打开图片查看，原始图片保持不变。', [('open', '查看图片')])
        if status == 'error' and any(action['id'] == 'external' for action in result['actions']):
            result['recommendation'] += ' 基础分析失败时，应用内原生播放入口可能不可用；可从原片所在目录手动使用 mpv/VLC 核对，不保证该文件能够播放。'
            result['actions'] = [dict(action, id='manual_external', label='从原目录用原生播放器核对')
                if action['id'] == 'external' else action for action in result['actions']]
        return result
