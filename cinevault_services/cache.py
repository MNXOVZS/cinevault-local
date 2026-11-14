"""Bounded, inspectable cleanup of app-created derivatives only.

Never enumerates a source directory, data/media, backups or catalogue contents.
Registered covers, frames and silent previews are protected. Unknown files are left alone.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time


GROUPS = ('previews', 'compatible', 'temporary', 'orphans')
SCOPES = (*GROUPS, 'all_regenerable')
IDENTIFIER = re.compile(r'[a-f0-9]{32}')
TEMP_NAME = re.compile(r'(?:[a-f0-9]{32}\.upload|backup-[a-f0-9]{32}\.sqlite3|classify-[a-f0-9]{32}\.json|timeline-[a-f0-9]{32}\.pcm|audio-analysis-[a-f0-9]{32}\.json|describe-[a-f0-9]{32}\.json)')
ASSET_NAME = re.compile(r'(?:cover|sprite|frame\d{2})\.jpg|(?:compatible|preview)(?:\.partial)?\.mp4|cover-new\.jpg')
DAY = 24 * 3600
RECENT_SECONDS = 10 * 60
PLAN_TTL = 300


class CacheService:
    def __init__(self, library, safe_path, error, now):
        self.lib, self.safe_path, self.error, self.now = library, safe_path, error, now
        self.lock = threading.RLock()
        self.maintenance_lock = threading.Lock()
        self.plans = {}
        self.in_use = {}
        self.last_used = {}
        self.last_maintenance = 0.0
        self.last_result = None

    @contextmanager
    def reading(self, key):
        with self.lock:
            self.in_use[key] = self.in_use.get(key, 0) + 1
            self.last_used[key] = time.time()
        try:
            yield
        finally:
            with self.lock:
                self.in_use[key] -= 1
                if not self.in_use[key]:
                    del self.in_use[key]
                self.last_used[key] = time.time()

    def _busy(self, key, recent=False):
        with self.lib.jobs_lock:
            if key and any(ident == key for _, ident in self.lib.active):
                return True
        with self.lock:
            return bool(self.in_use.get(key) or (recent and time.time() - self.last_used.get(key, 0) < RECENT_SECONDS))

    @staticmethod
    def _fingerprint(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)

    def _entry(self, path, parent, group, key=None):
        try:
            if not self.safe_path(path, parent):
                return None
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                return None
            return {'path': path, 'parent': parent, 'group': group, 'key': key,
                    'size': info.st_size, 'mtime': info.st_mtime,
                    'fingerprint': self._fingerprint(info)}
        except OSError:
            return None

    def _entries(self):
        # Fetch only identifiers, not private titles or source paths.
        with self.lib.db() as db:
            registered = {row[0] for row in db.execute('SELECT id FROM videos')}
        entries = []
        assets, temporary = self.lib.root / 'assets', self.lib.root / 'tmp'
        stamp = time.time()
        if self.safe_path(assets, self.lib.root):
            for directory in assets.iterdir():
                if not IDENTIFIER.fullmatch(directory.name) or not self.safe_path(directory, assets):
                    continue
                try:
                    if not directory.is_dir():
                        continue
                    known = directory.name in registered
                    # Registered folders may contain many representative frames;
                    # the quota never needs to walk/stat those protected images.
                    candidates = (directory / name for name in (
                        'preview.mp4', 'compatible.mp4', 'preview.partial.mp4',
                        'compatible.partial.mp4', 'cover-new.jpg')) if known else directory.iterdir()
                    for path in candidates:
                        if not ASSET_NAME.fullmatch(path.name):
                            continue
                        group = ('previews' if path.name == 'preview.mp4' else
                                 'compatible' if path.name == 'compatible.mp4' else 'temporary') if known else 'orphans'
                        if known and path.suffix.lower() != '.mp4' and path.name != 'cover-new.jpg':
                            continue  # Keep every registered cover, sprite and frame.
                        entry = self._entry(path, assets, group, directory.name)
                        if entry and (group not in ('temporary', 'orphans') or stamp - entry['mtime'] >= DAY):
                            entries.append(entry)
                except OSError:
                    continue
        if self.safe_path(temporary, self.lib.root):
            for path in temporary.iterdir():
                if TEMP_NAME.fullmatch(path.name):
                    entry = self._entry(path, temporary, 'temporary')
                    if entry and stamp - entry['mtime'] >= DAY:
                        entries.append(entry)
        return entries

    def _config(self):
        settings = self.lib.settings()
        return {'cache_auto_trim': bool(settings.get('cache_auto_trim', True)),
                'cache_limit_gb': settings.get('cache_limit_gb', 8),
                'protect_registered_previews': True}

    def inspect(self):
        entries = self._entries()
        groups = {name: {'files': 0, 'bytes': 0, 'reclaimable_bytes': 0} for name in GROUPS}
        skipped = 0
        for entry in entries:
            group = groups[entry['group']]
            group['files'] += 1
            group['bytes'] += entry['size']
            if entry['group']=='previews':
                continue
            if self._busy(entry['key'], recent=True):
                skipped += 1
            else:
                group['reclaimable_bytes'] += entry['size']
        return {'groups': groups, 'total_bytes': sum(g['bytes'] for g in groups.values()),
                'reclaimable_bytes': sum(g['reclaimable_bytes'] for g in groups.values()),
                'skipped_active': skipped, 'config': self._config(),
                'limit_is_soft': True, 'temporary_minimum_age_hours': 24,
                'protected_notice': '已入库的静音短片、封面与抽帧固定保留。清理只处理兼容副本、超过 24 小时的应用临时文件及无片目派生文件。原片、片目、模型、备份及未知文件均保留。',
                'last_maintenance': self.last_result}

    def plan(self, body):
        scope = body.get('scope', 'all_regenerable')
        if scope not in SCOPES:
            raise self.error(400, '无效缓存范围。')
        entries = [entry for entry in self._entries() if entry['group']!='previews'
                   and (scope == 'all_regenerable' or entry['group'] == scope)]
        selected = [entry for entry in entries if not self._busy(entry['key'], recent=True)]
        token, stamp = secrets.token_urlsafe(24), time.monotonic()
        with self.lock:
            self.plans = {key: plan for key, plan in self.plans.items() if stamp - plan['created'] < PLAN_TTL}
            while len(self.plans) >= 8:
                del self.plans[next(iter(self.plans))]
            self.plans[token] = {'created': stamp, 'entries': selected, 'scope': scope}
        return {'plan_token': token, 'scope': scope, 'files': len(selected),
                'bytes': sum(entry['size'] for entry in selected), 'ttl_seconds': PLAN_TTL,
                'skipped_active': len(entries) - len(selected),
                'sample_paths': [entry['path'].relative_to(self.lib.root).as_posix() for entry in selected[:50]],
                'notice': '已入库的静音短片受保护。确认后仅处理本次清单；文件改变、出现链接或正在使用时跳过。'}

    def _reset(self, key, group):
        if not key or not self.lib.video(key):
            return
        if group == 'previews':
            self.lib.update(key, has_preview=False, preview_status='unavailable', preview_error=None,
                preview_source=None, preview_info=None, preview_version=None,
                preview_job_id=None, preview_revision=time.time_ns())
        elif group == 'compatible':
            self.lib.update(key, has_compatible=False, playback_status='unavailable', playback_error=None,
                playback_source=None, compatible_info=None, playback_job_id=None)

    def _delete(self, entries, automatic=False):
        backup_lock = getattr(self.lib, 'backup_lock', None)
        if backup_lock and not backup_lock.acquire(blocking=False):
            return {'deleted_files': 0, 'deleted_bytes': 0, 'skipped': len(entries),
                    'automatic': automatic, 'notice': '资料库正在备份，缓存清理已跳过，请稍后重新预览。'}
        try:
            return self._delete_unlocked(entries, automatic)
        finally:
            if backup_lock:
                backup_lock.release()

    def _delete_unlocked(self, entries, automatic=False):
        deleted_files = deleted_bytes = skipped = 0
        for entry in entries:
            key = entry['key']
            if entry['group']=='previews':
                skipped += 1
                continue
            lock = self.lib.vlock(key) if key else None
            if lock and not lock.acquire(blocking=False):
                skipped += 1
                continue
            try:
                if self._busy(key, recent=True):
                    skipped += 1
                    continue
                # A newly re-created catalogue row protects formerly orphaned covers.
                if entry['group'] == 'orphans' and self.lib.video(key):
                    skipped += 1
                    continue
                # A temporary upload may still be owned by an in-flight handler.
                if entry['group'] == 'temporary':
                    with self.lib.jobs_lock:
                        has_active_work = bool(self.lib.active)
                    if has_active_work:
                        skipped += 1
                        continue
                # Serialize the final check/unlink with media-reader pinning.
                # Do not hold this lock while updating SQLite.
                with self.lock:
                    if self.in_use.get(key) or time.time() - self.last_used.get(key, 0) < RECENT_SECONDS:
                        skipped += 1
                        continue
                    current = self._entry(entry['path'], entry['parent'], entry['group'], key)
                    if not current or current['fingerprint'] != entry['fingerprint']:
                        skipped += 1
                        continue
                    entry['path'].unlink()
                deleted_files += 1
                deleted_bytes += entry['size']
                self._reset(key, entry['group'])
            except OSError:
                skipped += 1
            finally:
                if lock:
                    lock.release()
        return {'deleted_files': deleted_files, 'deleted_bytes': deleted_bytes, 'skipped': skipped,
                'automatic': automatic, 'notice': '缓存清理完成，已入库的静音短片和封面均保留。正在使用、变化或无法删除的文件已跳过。'}

    def cleanup(self, body):
        if body.get('confirm') is not True or not isinstance(body.get('plan_token'), str):
            raise self.error(400, '请先预览清理清单，再明确确认。')
        with self.lock:
            plan = self.plans.pop(body['plan_token'], None)
        if not plan or time.monotonic() - plan['created'] >= PLAN_TTL:
            raise self.error(409, '清理清单已过期或已使用，请重新预览。')
        return self._delete(plan['entries'])

    def maintain(self, force=False):
        if not self._config()['cache_auto_trim']:
            return None
        if not self.maintenance_lock.acquire(blocking=False):
            return None
        try:
            stamp = time.monotonic()
            if not force and stamp - self.last_maintenance < 600:
                return None
            self.last_maintenance = stamp
            entries = self._entries()
            stale = [entry for entry in entries if entry['group'] in ('temporary', 'orphans')]
            result = self._delete(stale, automatic=True)
            media = [entry for entry in entries if entry['group']=='compatible']
            total = sum(entry['size'] for entry in media)
            limit = float(self._config()['cache_limit_gb']) * 1024 ** 3
            with self.lock:
                usage = dict(self.last_used)
            media.sort(key=lambda entry: usage.get(entry['key'], entry['mtime']))
            for entry in media:
                if total <= limit:
                    break
                part = self._delete([entry], automatic=True)
                for name in ('deleted_files', 'deleted_bytes', 'skipped'):
                    result[name] += part[name]
                total -= part['deleted_bytes']
            result.update(finished_at=self.now(), remaining_mp4_bytes=total,
                          protected_preview_bytes=sum(entry['size'] for entry in entries if entry['group']=='previews'),limit_bytes=int(limit),
                          over_limit=total > limit, limit_is_soft=True)
            self.last_result = result
            return result
        finally:
            self.maintenance_lock.release()
