#!/usr/bin/env python3
# [INPUT]: SQLite、FFmpeg 与 cinevault_services。
# [OUTPUT]: 片目、媒体、来源及后台任务 HTTP 接口。
# [POS]: 本机服务入口。
# [PROTOCOL]: Update this header on change, then check AGENTS.md
"""CineVault: local, portable media catalogue. Python 3.11+, stdlib only.

The source files are read-only except for an explicit, filename-confirmed delete.
Derived media and the catalogue live together in --data-dir.
"""
from __future__ import annotations

import argparse
import array
import base64
import contextlib
import datetime
import hashlib
import http.cookies
import ipaddress
import json
import math
import mimetypes
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import queue
import re
import secrets
import shutil
import socket
import sqlite3
import stat as stat_module
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP = Path(__file__).resolve().parent
if str(APP) not in sys.path: sys.path.insert(0,str(APP))
from cinevault_services.playback import PlaybackService
from cinevault_services.analysis import AnalysisService
from cinevault_services.models import load_model_config
from cinevault_services.model_registry import LocalModelRegistry, REGION_NAMES, CLASSIFICATION_ENGINES
from cinevault_services.providers import ProviderService
from cinevault_services.automation import AutomationService
from cinevault_services.cache import CacheService
from cinevault_services.library_health import LibraryHealthService
from cinevault_services.compatibility import CompatibilityService
from cinevault_services.audit import CatalogueAudit
from cinevault_services.model_tags import install_model_tag_index

VERSION = "1.8.0"
CONTENT_SCOPES = {'adult','personal','review'}
VIDEO_EXTENSIONS = {'.mp4','.m4v','.mkv','.mov','.avi','.webm','.wmv','.mpg','.mpeg','.ts','.mts','.m2ts','.flv','.vob','.ogv','.3gp','.3g2','.asf','.divx','.mxf','.rm','.rmvb','.f4v','.m2v','.mpe','.mpv','.qt','.y4m'}
IMAGE_EXTENSIONS = {'.jpg','.jpeg','.jfif','.png','.webp','.gif','.bmp','.tif','.tiff','.heic','.heif','.avif'}
EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS
DEFAULT_SETTINGS = {'watch_enabled': True, 'watch_interval': 300, 'ollama_model': '', 'auto_classify_new_imports':False,'card_auto_preview':True,
                    'auto_timeline_new_imports':True,'auto_preview_new_imports':True,'auto_describe_new_imports':False,'cache_auto_trim':True,'cache_limit_gb':8,'classification_engine':'vit'}
DEFAULT_EXCLUDE_DIRS = {
    '$recycle.bin','system volume information','recovery','windows','program files','program files (x86)',
    'programdata','appdata','documents and settings','$windows.~bt','$winreagent','perflogs','msocache','config.msi',
    '.git','.hg','.svn','node_modules','__pycache__','.cache','cache','caches','tmp','temp','runtime',
    '.trash','.trashes','.spotlight-v100','.fseventsd','.temporaryitems','.vol',
    'system','library','applications','lost+found',
}
SYSTEM_ROOT_DIRS = {'bin','sbin','usr','private','dev','proc','sys','run','etc','var','boot','snap'}


def path_key(path):
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def is_link_or_reparse(path):
    """Never traverse Windows junctions, symlinks, or other reparse directories."""
    try:
        info = os.lstat(path)
        return stat_module.S_ISLNK(info.st_mode) or bool(getattr(info,'st_file_attributes',0) & 0x400)
    except OSError:
        return True


def is_within(path, parent):
    try:
        return os.path.commonpath((path_key(path),path_key(parent))) == path_key(parent)
    except ValueError:
        return False


def device_identity(path):
    """Read only: identify the mounted filesystem, never enumerate its files."""
    path = Path(path)
    if os.name == 'nt':
        import ctypes
        kernel = ctypes.windll.kernel32
        volume_root = ctypes.create_unicode_buffer(32768)
        if not kernel.GetVolumePathNameW(str(path),volume_root,len(volume_root)):
            raise OSError('无法读取此目录所在磁盘的标识。')
        serial = ctypes.c_uint32()
        if not kernel.GetVolumeInformationW(volume_root.value,None,0,ctypes.byref(serial),None,None,None,0):
            raise OSError('无法读取磁盘卷标识，请检查磁盘是否在线。')
        return {'type':'windows-volume','id':f'{serial.value:08x}','root':volume_root.value}
    if sys.platform == 'darwin':
        try:
            import plistlib
            result = subprocess.run(['/usr/sbin/diskutil','info','-plist',str(path)],capture_output=True,timeout=5)
            metadata = plistlib.loads(result.stdout)
            stable = metadata.get('VolumeUUID') or metadata.get('DiskUUID')
            if stable:
                return {'type':'volume-uuid','id':stable,'root':metadata.get('MountPoint','')}
        except (OSError,ValueError,subprocess.TimeoutExpired):
            pass
    return {'type':'unix-device','id':str(path.stat().st_dev),'root':str(path.anchor)}


def same_device(left,right):
    return not left or not right or (left.get('type'),left.get('id')) == (right.get('type'),right.get('id'))


def platform_name(path=None):
    if path and re.match(r'^[A-Za-z]:[\\/]|^\\\\',str(path)): return 'windows'
    if path and str(path).startswith(('/Volumes/','/Users/')): return 'macos'
    return 'windows' if os.name=='nt' else 'macos' if sys.platform=='darwin' else 'linux'


def plain_descendant(path,root):
    path,root=Path(path).absolute(),Path(root).absolute()
    if not is_within(path,root): return False
    for part in (path,*path.parents):
        if os.path.lexists(part) and is_link_or_reparse(part): return False
    return is_within(path.resolve(strict=False),root.resolve(strict=False))


def now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def uid():
    return uuid.uuid4().hex


def read_json(text, fallback=None):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return fallback


def validated_scope(value):
    if not isinstance(value,str) or value not in CONTENT_SCOPES:
        raise ApiError(400,'内容分区必须为 adult、personal 或 review。')
    return value


def validated_date(value,label):
    if value is None or value=='': return None
    if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):
        raise ApiError(400,label+'应为 YYYY-MM-DD，未知时请留空。')
    try: datetime.date.fromisoformat(value)
    except ValueError: raise ApiError(400,label+'不是有效日期。')
    return value


def validated_source_url(value):
    if value is None or value=='': return None
    if not isinstance(value,str) or len(value)>2000:
        raise ApiError(400,'资料来源链接无效。')
    parsed=urllib.parse.urlsplit(value.strip())
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:
        raise ApiError(400,'资料来源必须是没有账号口令的 http 或 https 链接。')
    return value.strip()


def age_at_release(birth,release):
    try:
        born=datetime.date.fromisoformat(birth)
        released=datetime.date.fromisoformat(release)
        age=released.year-born.year-((released.month,released.day)<(born.month,born.day))
        return age if 0<=age<=120 else None
    except (TypeError,ValueError): return None


def visual_metadata(stream):
    pixel_format=str(stream.get('pix_fmt') or '')
    try: depth=int(stream.get('bits_per_raw_sample') or stream.get('bits_per_sample') or 0)
    except (TypeError,ValueError): depth=0
    if not depth:
        match=re.search(r'(?:p|gray)(9|10|12|14|16)',pixel_format)
        depth=int(match.group(1)) if match else 16 if any(t in pixel_format for t in ('rgb48','bgr48','rgba64','bgra64')) else 8 if pixel_format else None
    transfer=stream.get('color_transfer')
    side_data=stream.get('side_data_list',[])
    hdr=('Dolby Vision' if any('dovi' in str(item.get('side_data_type','')).lower() or 'dolby' in str(item.get('side_data_type','')).lower() for item in side_data)
         else 'HDR10 / PQ' if transfer=='smpte2084' else 'HLG' if transfer=='arib-std-b67'
         else 'SDR' if transfer in ('bt709','iec61966-2-1','gamma22','gamma28','smpte170m','bt470m','bt470bg') else '未标注')
    return {'pixel_format':pixel_format,'bit_depth':depth,'dynamic_range':hdr,'color_transfer':transfer,
            'color_primaries':stream.get('color_primaries'),'color_space':stream.get('color_space')}


def get_tool(name):
    candidates = [APP / 'bin' / (name + ('.exe' if os.name == 'nt' else ''))]
    # macOS launchers can put architecture-specific tools here.
    candidates += [APP / 'bin' / ('macos-' + ('arm64' if os.uname().machine == 'arm64' else 'x64')) / name] if os.name != 'nt' else []
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which(name)


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Library:
    def __init__(self, data_dir, port=8765, lan=False):
        self.app=APP
        self.root = Path(data_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock_file = open(self.root / 'library.lock', 'a+b')
        self.lock_file.seek(0, 2)
        if not self.lock_file.tell():
            self.lock_file.write(b'0')
            self.lock_file.flush()
        self.lock_file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock_file.close()
            raise SystemExit('此资料库已由另一个映库服务打开，请先停止该服务。')
        for name in ('media', 'assets', 'backups', 'tmp'):
            if os.path.lexists(self.root/name) and is_link_or_reparse(self.root/name):
                raise RuntimeError('资料库内部目录不能使用链接或目录联接：'+str(self.root/name))
            (self.root / name).mkdir(exist_ok=True)
        # The singleton lock is already held. Clean only files created by this
        # app as temporary outputs, never originals or arbitrary directories.
        for temp in (self.root/'tmp').iterdir():
            if temp.is_file() and re.fullmatch(r'(?:[a-f0-9]{32}\.upload|backup-[a-f0-9]{32}\.sqlite3|classify-[a-f0-9]{32}\.json|timeline-[a-f0-9]{32}\.pcm|audio-analysis-[a-f0-9]{32}\.json)',temp.name):
                with contextlib.suppress(OSError): temp.unlink()
        for temp in (self.root/'backups').glob('*.partial'):
            if temp.is_file() and not temp.is_symlink():
                with contextlib.suppress(OSError): temp.unlink()
        for asset in (self.root/'assets').iterdir():
            if asset.is_dir() and not asset.is_symlink() and re.fullmatch('[a-f0-9]{32}',asset.name):
                for name in ('compatible.partial.mp4','preview.partial.mp4','cover-new.jpg'):
                    with contextlib.suppress(OSError): (asset/name).unlink()
        self.db_path = self.root / 'library.sqlite3'
        self.db_lock = threading.RLock()
        self.catalog_lock = threading.RLock()
        self.backup_lock = threading.Lock()
        self.video_locks = {}
        self.stop = threading.Event()
        self.queue = queue.Queue()
        self.scan_queue = queue.Queue()
        self.jobs_lock = threading.Lock()
        self.process_lock = threading.Lock()
        self.processes = set()
        self.current_video = None
        self.current_kind = None
        self.cancelled = set()
        self.timeline_cancelled=set()
        self.active = set()
        self.rescan_requested=False
        self.last_seen = {}
        self.port, self.lan = port, lan
        self.ffmpeg, self.ffprobe = get_tool('ffmpeg'), get_tool('ffprobe')
        self.playback=PlaybackService(self,now,visual_metadata,plain_descendant,ApiError)
        self.analysis=AnalysisService(self,now,ApiError)
        self.providers=ProviderService(self,plain_descendant,ApiError,now)
        self.automation=AutomationService(self,ApiError,now)
        self.cache=CacheService(self,plain_descendant,ApiError,now)
        self.compatibility=CompatibilityService(self)
        self.health=LibraryHealthService(self,ApiError,now)
        self.local_models=LocalModelRegistry(self,ApiError)
        self.audit=CatalogueAudit(self,ApiError,now)
        self.config_path = self.root / 'config.json'
        config = read_json(self.config_path.read_text('utf-8'), {}) if self.config_path.exists() else {}
        self.token = config.get('access_token') or secrets.token_urlsafe(24)
        if not config.get('access_token'):
            self.config_path.write_text(json.dumps({'access_token': self.token}, indent=2), 'utf-8')
            with contextlib.suppress(OSError):
                os.chmod(self.config_path, 0o600)
        self.sessions = set()
        self.login_attempts = {}
        with self.db() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS videos (id TEXT PRIMARY KEY, location TEXT UNIQUE NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sources (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS settings (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS excluded (location TEXT PRIMARY KEY);
            ''')
            db.execute("INSERT OR IGNORE INTO settings VALUES ('settings', ?)", (json.dumps(DEFAULT_SETTINGS),))
        self.migrate()
        self.audit.initialize()
        # A process interrupted halfway through a job resumes work after restart.
        for job in self.rows('jobs'):
            if job['status'] in ('queued', 'running'):
                self._save('jobs', job['id'], dict(job, status='interrupted', error='服务上次停止，任务已重新排队。'))
                if job.get('video_id') and self.video(job['video_id']):
                    if job['kind']=='describe' and not (job.get('options',{}).get('provider_request') or {}).get('provider',{}).get('local_only'):
                        video=self.require_video(job['video_id'])
                        self.update(job['video_id'],description_analysis=dict(video.get('description_analysis') or {},status='error',error='服务重启后，远程任务必须重新预览并确认；未自动重发。'))
                        continue
                    self.enqueue(job['kind'], job['video_id'], **job.get('options', {}))
        with self.db() as db:
            interrupted = [r[0] for r in db.execute("SELECT id FROM videos WHERE status='analyzing'")]
        for key in interrupted:
            self.update(key,status='queued')
        self.schedule_pending()
        self.worker = threading.Thread(target=self.work, name='media-worker', daemon=True)
        self.worker.start()
        self.scanner = threading.Thread(target=self.scan_work,name='source-scanner',daemon=True)
        self.scanner.start()
        self.watcher = threading.Thread(target=self.watch, name='folder-watcher', daemon=True)
        self.watcher.start()

    @contextlib.contextmanager
    def db(self):
        with self.db_lock:
            connection = sqlite3.connect(self.db_path, timeout=30)
            try:
                connection.execute('PRAGMA busy_timeout=30000')
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def rows(self, table):
        with self.db() as db:
            return [json.loads(row[0]) for row in db.execute(f'SELECT data FROM {table}')]

    def migrate(self):
        with self.db() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version > 3:
                raise RuntimeError('资料库由较新版本创建，请使用相同或更新版本映库。')
            columns = {r[1] for r in db.execute('PRAGMA table_xinfo(videos)')}
            count = db.execute('SELECT count(*) FROM videos').fetchone()[0]
        if version < 3 or 'kind' not in columns or 'content_scope' not in columns:
            if count:
                backup = self.root/'backups'/(f'migration-v{version or 1}-'+time.strftime('%Y-%m-%d-%H%M%S',time.gmtime())+'-'+uid()[:6]+'.sqlite3')
                with contextlib.closing(sqlite3.connect(self.db_path)) as source,contextlib.closing(sqlite3.connect(backup)) as dest:
                    source.backup(dest)
            # One transaction keeps the old table intact if any migration step
            # fails. STORED fields make paging/counting independent of JSON size.
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('''CREATE TABLE videos_v3 (
                    id TEXT PRIMARY KEY, location TEXT UNIQUE NOT NULL, data TEXT NOT NULL,
                    kind TEXT GENERATED ALWAYS AS (coalesce(json_extract(data,'$.kind'),'video')) STORED,
                    content_scope TEXT GENERATED ALWAYS AS (CASE WHEN json_extract(data,'$.content_scope') IN ('adult','personal','review') THEN json_extract(data,'$.content_scope') ELSE 'review' END) STORED,
                    title TEXT GENERATED ALWAYS AS (coalesce(json_extract(data,'$.title'),'')) STORED,
                    filename TEXT GENERATED ALWAYS AS (coalesce(json_extract(data,'$.filename'),'')) STORED,
                    description TEXT GENERATED ALWAYS AS (coalesce(json_extract(data,'$.description'),'')) STORED,
                    status TEXT GENERATED ALWAYS AS (coalesce(json_extract(data,'$.status'),'queued')) STORED,
                    favorite INTEGER GENERATED ALWAYS AS (coalesce(json_extract(data,'$.favorite'),0)) STORED,
                    owned INTEGER GENERATED ALWAYS AS (coalesce(json_extract(data,'$.owned'),0)) STORED,
                    size INTEGER GENERATED ALWAYS AS (coalesce(json_extract(data,'$.size'),0)) STORED,
                    duration REAL GENERATED ALWAYS AS (coalesce(json_extract(data,'$.duration'),0)) STORED,
                    last_position REAL GENERATED ALWAYS AS (coalesce(json_extract(data,'$.last_position'),0)) STORED,
                    created_at TEXT GENERATED ALWAYS AS (coalesce(json_extract(data,'$.created_at'),'')) STORED,
                    source_id TEXT GENERATED ALWAYS AS (json_extract(data,'$.source_id')) STORED,
                    mtime_ns INTEGER GENERATED ALWAYS AS (coalesce(json_extract(data,'$.mtime_ns'),0)) STORED,
                    has_sha INTEGER GENERATED ALWAYS AS (json_extract(data,'$.sha256') IS NOT NULL) STORED
                )''')
                db.execute('INSERT INTO videos_v3(id,location,data) SELECT id,location,data FROM videos')
                db.execute('DROP TABLE videos')
                db.execute('ALTER TABLE videos_v3 RENAME TO videos')
                db.execute('PRAGMA user_version=3')
        with self.db() as db:
            for name,fields in (
                ('created','created_at DESC,id DESC'),('kind_status_created','kind,status,created_at DESC'),
                ('status_created','status,created_at DESC'),('favorite_created','favorite,created_at DESC'),
                ('title','title COLLATE NOCASE,id'),('size','size DESC,id'),('duration','duration DESC,id'),('source','source_id'),
                ('stats','kind,status,size,duration,favorite,content_scope'),('scope_created','content_scope,created_at DESC,id DESC'),
            ):
                db.execute(f'CREATE INDEX IF NOT EXISTS videos_{name} ON videos({fields})')
            db.execute("CREATE INDEX IF NOT EXISTS videos_pending_verify ON videos(coalesce(json_extract(data,'$.needs_verify'),0),created_at)")
            db.execute("CREATE INDEX IF NOT EXISTS jobs_status ON jobs(json_extract(data,'$.status'))")
            db.execute("CREATE INDEX IF NOT EXISTS jobs_created ON jobs(json_extract(data,'$.created_at') DESC)")
            db.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value INTEGER NOT NULL)')
            db.execute("INSERT OR IGNORE INTO meta VALUES('revision',0)")
            db.execute('CREATE TABLE IF NOT EXISTS video_tags(video_id TEXT NOT NULL,tag TEXT NOT NULL,PRIMARY KEY(video_id,tag))')
            db.execute('CREATE INDEX IF NOT EXISTS video_tags_tag ON video_tags(tag,video_id)')
            db.execute("""CREATE TRIGGER IF NOT EXISTS videos_tags_insert AFTER INSERT ON videos BEGIN
                INSERT OR IGNORE INTO video_tags SELECT new.id,value FROM json_each(new.data,'$.tags') WHERE type='text'; END""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS videos_tags_delete AFTER DELETE ON videos BEGIN
                DELETE FROM video_tags WHERE video_id=old.id; END""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS videos_tags_update AFTER UPDATE OF data ON videos
                WHEN coalesce(json_extract(old.data,'$.tags'),'[]')<>coalesce(json_extract(new.data,'$.tags'),'[]') BEGIN
                DELETE FROM video_tags WHERE video_id=old.id;
                INSERT OR IGNORE INTO video_tags SELECT new.id,value FROM json_each(new.data,'$.tags') WHERE type='text'; END""")
            if not db.execute('SELECT 1 FROM video_tags LIMIT 1').fetchone():
                db.execute("INSERT OR IGNORE INTO video_tags SELECT videos.id,j.value FROM videos,json_each(videos.data,'$.tags') j WHERE j.type='text'")
            install_model_tag_index(db)
        for source in self.rows('sources'):
            dirty = False
            for key,value in (('exclude_dirs',[]),('allow_delete',False),('content_scope','review'),('config_revision',1),('scan',{'status':'idle','discovered':0,'processed':0,'skipped':0,'errors':0})):
                if key not in source:
                    source[key] = value
                    dirty = True
            if 'device_identity' not in source:
                with contextlib.suppress(OSError): source['device_identity'] = device_identity(source['path'])
                dirty = True
            if not isinstance(source.get('platform_paths'),dict):
                source['platform_paths']={platform_name(source['path']):source['path']}
                dirty=True
            if dirty: self._save('sources',source['id'],source)

    def revision(self,db=None):
        if db is not None: return db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0]
        with self.db() as connection: return self.revision(connection)

    def bump(self,db):
        db.execute("UPDATE meta SET value=value+1 WHERE key='revision'")

    def _save(self, table, key, value, location=None):
        with self.db() as db:
            if table == 'videos':
                db.execute('INSERT INTO videos(id,location,data) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET location=excluded.location,data=excluded.data',
                           (key, location or value['location'], json.dumps(value, ensure_ascii=False)))
            else:
                db.execute(f'INSERT INTO {table} VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                           (key, json.dumps(value, ensure_ascii=False)))
            self.bump(db)

    def video(self, key):
        with self.db() as db:
            row = db.execute('SELECT data FROM videos WHERE id=?', (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def require_video(self, key):
        video = self.video(key)
        if not video:
            raise ApiError(404, '影片不存在，可能已移出资料库。')
        return video

    def update(self, key, **changes):
        with self.db_lock:
            video = self.video(key)
            if video:
                video.update(changes)
                self._save('videos', key, video)
            return video

    def vlock(self, key):
        with self.db_lock:
            return self.video_locks.setdefault(key, threading.RLock())

    def settings(self):
        rows = self.rows('settings')
        return dict(DEFAULT_SETTINGS, **(rows[0] if rows else {}))

    def classifier_config(self,engine=None):
        engine=engine or self.settings().get('classification_engine','vit')
        if engine not in CLASSIFICATION_ENGINES: raise ApiError(400,'未知的本地分类引擎。')
        if engine in ('nudenet','nudenet-640m'):
            config=load_model_config(APP,'ai/nudenet_config.json','CINEVAULT_NUDENET_CONFIG','ai/classify_nudenet.py','ai/models/nudenet/320n.onnx')
            if config and engine=='nudenet-640m':
                config=dict(config,model_path=APP/'ai/models/nudenet/640m.onnx',model_id='notAI-tech/NudeNet@3.4-weights (640m ONNX)',variant='640m',sha256='04fe3d77980780c1f8297dc6d7f942fd5b3abe6942a188f742a85241e4f634eb')
            return config
        return load_model_config(APP,'ai/config.json','CINEVAULT_CLASSIFIER_CONFIG','ai/classify_media.py','ai/models/classifier.onnx')

    def classifier_status(self,engine=None):
        engine=engine or self.settings().get('classification_engine','vit')
        config=self.classifier_config(engine)
        if not config:
            return {'available':False,'model':None,'local_only':True,'reason':'本地分类模型或此系统的运行时尚未配置。'}
        missing=[name for name in ('python_path','script_path','model_path') if not config[name].is_file()]
        if config.get('packages_path') is not None and not config['packages_path'].is_dir(): missing.append('packages_path')
        platform='windows' if os.name=='nt' else 'macos' if sys.platform=='darwin' else 'linux'
        for relative in config.get('required_package_dirs_'+platform,[]):
            if not isinstance(relative,str) or not (APP/relative).is_dir(): missing.append(str(relative))
        return {'available':not missing,'runtime_available':not any(name!='model_path' for name in missing),'model':config.get('model_id') or config.get('model_name') or 'local-nsfw-classifier',
                'engine':engine,'score_kind':'exposure_candidate_score' if engine.startswith('nudenet') else 'classification_probability',
                'local_only':True,'reason':'本地模型文件与运行时已找到。' if not missing else '缺少本地分类组件：'+', '.join(missing),
                'sample_notice':('NudeNet仅提供抽样暴露候选建议；无检出保持待确认，分数不是普通/成人全片概率。' if engine.startswith('nudenet') else '只根据抽样画面估计内容类别，结果需核对。')+'不能确认人物身份、性别或年龄，也不识别姿势或高潮。'}

    def classify_media(self,key,engine=None):
        video=self.require_video(key)
        status=self.classifier_status(engine)
        if not status['available']:
            self.update(key,classification_status='error',classification_error=status['reason'],classification_engine_pending=None)
            raise RuntimeError(status['reason'])
        config=self.classifier_config(status.get('engine',engine))
        asset=self.root/'assets'/key
        samples=[(asset/'cover.jpg',None)] if video.get('kind')=='image' else [(asset/Path(urllib.parse.urlsplit(frame['url']).path).name,frame.get('time')) for frame in video.get('frames',[])[:12]]
        samples=[(path,timestamp) for path,timestamp in samples if path.is_file() and plain_descendant(path,self.root/'assets')]
        images=[path for path,_ in samples]
        if not images: raise RuntimeError('请先完成媒体分析并生成预览画面，再执行内容分类。')
        request_path=self.root/'tmp'/('classify-'+uid()+'.json')
        self.update(key,classification_status='running',classification_error=None)
        try:
            request_path.write_text(json.dumps({'images':[str(path) for path in images],'media_kind':video.get('kind','video'),'variant':config.get('variant','320n')},ensure_ascii=False),'utf-8')
            process=self.run_tool([str(config['python_path']),str(config['script_path']),'--request',str(request_path)],timeout=180)
            try:
                result=json.loads(process.stdout.decode('utf-8-sig'))
                label=validated_scope(result.get('label'))
                score=float(result.get('score'))
                sampled_count=int(result.get('sampled_count'))
                if not math.isfinite(score) or not 0<=score<=1 or not 1<=sampled_count<=len(images): raise ValueError()
            except (ValueError,TypeError,AttributeError,ApiError):
                raise RuntimeError('本地分类器返回了无效结果，未更改内容分区。')
            classes={}
            if isinstance(result.get('classes'),dict):
                for name,value in list(result['classes'].items())[:20]:
                    if isinstance(value,(float,int)) and math.isfinite(value) and 0<=value<=1:
                        classes[str(name)[:60]]=round(value,6)
            details=[]
            if isinstance(result.get('details'),list):
                for detail in result['details'][:12]:
                    if not isinstance(detail,dict) or detail.get('key') not in REGION_NAMES: continue
                    candidate=detail.get('score')
                    indices=detail.get('sample_indices')
                    if isinstance(candidate,bool) or not isinstance(candidate,(float,int)) or not math.isfinite(candidate) or not .5<=candidate<=1: continue
                    times=sorted({round(float(samples[index][1]),3) for index in indices[:24] if isinstance(index,int) and not isinstance(index,bool) and 0<=index<len(samples) and isinstance(samples[index][1],(float,int)) and math.isfinite(samples[index][1]) and samples[index][1]>=0}) if isinstance(indices,list) else []
                    details.append({'key':detail['key'],'label':REGION_NAMES[detail['key']],'score':round(candidate,6),'sample_times':times})
            with self.db_lock:
                current=self.video(key)
                if not current: return
                protected=current.get('scope_source')=='manual' or current.get('scope_lock',False)
                classification={'label':label,'score':round(score,6),'model':str(result.get('model') or status['model'])[:200],
                                'sampled_count':sampled_count,'classes':classes,'details':details,'analyzed_at':now(),
                                'engine':status.get('engine','vit'),'score_kind':str(result.get('score_kind') or status.get('score_kind','classification_probability'))[:80],
                                'review_status':'manual_preserved' if protected else 'needs_review' if label=='review' else 'auto_assigned',
                                'notice':status['sample_notice']}
                changes={'classification':classification,'classification_status':'complete','classification_error':None,'classification_engine_pending':None}
                if not protected: changes.update(content_scope=label,scope_source='classifier',scope_lock=False)
                self.update(key,**changes)
        except Exception as exc:
            self.update(key,classification_status='error',classification_error=str(exc)[:1500],classification_engine_pending=None)
            raise
        finally:
            with contextlib.suppress(OSError): request_path.unlink()

    def classify_batch(self,body):
        if not isinstance(body,dict): raise ApiError(400,'分类参数必须为对象。')
        engine=body.get('engine') or self.settings().get('classification_engine','vit')
        status=self.classifier_status(engine)
        if not status['available']: raise ApiError(503,status['reason'])
        scope=body.get('scope','review')
        if scope!='all': validated_scope(scope)
        limit=body.get('limit',128)
        if isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=128: raise ApiError(400,'每批分类数量应为1至128的整数。')
        if 'reclassify' in body and not isinstance(body['reclassify'],bool): raise ApiError(400,'重新分类开关必须为布尔值。')
        include_manual=body.get('include_manual',False)
        if not isinstance(include_manual,bool): raise ApiError(400,'include_manual必须为布尔值。')
        offset=body.get('offset',0)
        if isinstance(offset,bool) or not isinstance(offset,int) or not 0<=offset<=10000000: raise ApiError(400,'offset必须为非负整数。')
        selection=['1=1'];parameters=[]
        if scope!='all': selection.append('content_scope=?'); parameters.append(scope)
        ids=body.get('ids')
        if ids is not None:
            if not isinstance(ids,list) or not 1<=len(ids)<=128 or any(not isinstance(key,str) or not re.fullmatch('[a-f0-9]{32}',key) for key in ids): raise ApiError(400,'ids应为1至128个资料ID。')
            selection.append('id IN ('+','.join('?' for _ in ids)+')'); parameters.extend(ids)
        source_id=body.get('source_id')
        if source_id is not None:
            if not isinstance(source_id,str) or not re.fullmatch('[a-f0-9]{32}',source_id): raise ApiError(400,'来源ID无效。')
            if not any(source.get('id')==source_id for source in self.rows('sources')): raise ApiError(404,'来源不存在。')
            selection.append('source_id=?'); parameters.append(source_id)
        selected_where=' AND '.join(selection)
        eligible=[selected_where,"status='ready'", "(json_extract(data,'$.has_cover')=1 OR json_extract(data,'$.cover_revision') IS NOT NULL)"]
        if not include_manual:
            eligible.extend(["coalesce(json_extract(data,'$.scope_source'),'')<>'manual'","coalesce(json_extract(data,'$.scope_lock'),0)=0"])
        if not body.get('reclassify'):
            eligible.append("coalesce(json_extract(data,'$.classification_status'),'unclassified')<>'complete'")
        where=' AND '.join(eligible)
        with self.db() as db:
            requested=db.execute('SELECT count(*) FROM videos WHERE '+selected_where,parameters).fetchone()[0]
            total=db.execute('SELECT count(*) FROM videos WHERE '+where,parameters).fetchone()[0]
            keys=[r[0] for r in db.execute('SELECT id FROM videos WHERE '+where+' ORDER BY created_at,id LIMIT ? OFFSET ?',parameters+[limit,offset])]
        jobs=[];in_queue=0;capacity_skipped=0;retry_ids=[]
        for key in keys:
            with self.jobs_lock: active=('classify',key) in self.active
            if active: in_queue+=1;continue
            job=self.enqueue('classify',key,engine=engine)
            if job: jobs.append(job)
            else: capacity_skipped+=1;retry_ids.append(key)
        return {'requested':requested,'eligible':total,'excluded':requested-total,
                'selected':len(keys),'queued':len(jobs),'skipped':len(keys)-len(jobs),'remaining':max(0,total-offset-len(keys)),
                'total':total,'offset':offset,'next_offset':offset+len(keys),'in_queue':in_queue,'capacity_skipped':capacity_skipped,'retry_ids':retry_ids,
                'limit':limit,'engine':engine,'model':status['model'],'local_only':True,'job_ids':jobs,
                'notice':'只更新本机分类建议，人工分区与标签保留。remaining为尚未遍历的符合条件条目；队列已满未入队条目见retry_ids，可空闲后另行提交。'}

    def auto_classify_new(self,key):
        """Only records explicitly flagged when created are eligible, once."""
        with self.db_lock:
            video=self.video(key)
            if not video or not video.get('auto_classify_pending') or video.get('status')!='ready': return
            if (not self.settings().get('auto_classify_new_imports',False) or video.get('content_scope','review')!='review'
                or video.get('scope_source')=='manual' or video.get('scope_lock')):
                self.update(key,auto_classify_pending=False)
                return
            with self.jobs_lock: already_queued=('classify',key) in self.active
            job=None if already_queued else self.enqueue('classify',key,automatic=True)
            if job or already_queued:
                self.update(key,auto_classify_pending=False,classification_status='queued',classification_error=None)
            else:
                message='自动分类未入队：后台队列已满。资料分析已完成，可稍后手动分类。'
                self.update(key,auto_classify_pending=False,classification_status='error',classification_error=message)
                failed={'id':uid(),'kind':'classify','video_id':key,'status':'error','progress':0,'error':message,
                        'created_at':now(),'finished_at':now(),'options':{'automatic':True}}
                self._save('jobs',failed['id'],failed)

    def path(self, video):
        return self.root / video['location'] if video.get('owned') else Path(video['location'])

    def public_video(self, video, full=True, sources=None):
        value = dict(video)
        value['kind'] = value.get('kind','video')
        value['content_scope']=value.get('content_scope') if value.get('content_scope') in CONTENT_SCOPES else 'review'
        value['scope_source']=value.get('scope_source','unclassified')
        value['scope_lock']=bool(value.get('scope_lock') or value['scope_source']=='manual')
        value['classification']=value.get('classification')
        value['classification_status']=value.get('classification_status','unclassified')
        value['manual_summary']=str(value.get('description') or '')[:160] if value.get('description_source')=='manual' else ''
        value['temporal_event_count']=int(value.get('temporal_event_count') or len(value.get('temporal_events',[])))
        value['performers']=[dict(performer,age_at_release=age_at_release(performer.get('birth_date'),value.get('release_date')),
                                  age_note='按资料中的出生日期与发行日期计算；不是拍摄年龄，也不是影像年龄验证。')
                             for performer in value.get('performers',[]) if isinstance(performer,dict)]
        value['path'] = str(self.path(video))
        key = video['id']
        asset = self.root / 'assets' / key
        # Listing never stats the media or cache: offline disks must not block
        # browsing. Full detail checks only this one item's derived assets.
        has_cover = (asset/'cover.jpg').is_file() if full else bool(video.get('has_cover') or video.get('cover_revision'))
        has_sprite = (asset/'sprite.jpg').is_file() if full else bool(video.get('has_sprite') or video.get('frames'))
        has_compatible = (asset/'compatible.mp4').is_file() if full else bool(video.get('has_compatible'))
        has_preview=(asset/'preview.mp4').is_file() if full else bool(video.get('has_preview'))
        # Derived streams are useful only for the currently verified source.
        # This uses stored metadata; listing still never touches original media.
        signature=self.playback.signature(video)
        has_compatible=has_compatible and video.get('status')=='ready' and (not video.get('playback_source') or video['playback_source']==signature)
        has_preview=has_preview and video.get('status')=='ready' and self.playback.preview_is_current(video) and (not video.get('preview_source') or video['preview_source']==signature)
        value['cover'] = f'/assets/{key}/cover.jpg?v={video.get("cover_revision",0)}' if has_cover else None
        frame_revision=video.get('frames_revision') or video.get('cover_revision') or 0
        value['sprite'] = f'/assets/{key}/sprite.jpg?rev={frame_revision}' if has_sprite else None
        value['sprite_columns'], value['sprite_rows'] = 4, 3
        value['compatible_url'] = f'/media/{key}/compatible' if has_compatible else None
        value['preview_url']=f'/media/{key}/preview?v={video.get("preview_revision",0)}' if has_preview else None
        value['preview_status']=video.get('preview_status','ready' if has_preview else 'unavailable')
        value['preview_version']=video.get('preview_version')
        value['preview_target_version']=self.playback.preview_target_version
        value['playback_status']=video.get('playback_status','ready' if has_compatible else 'unprepared')
        value['playback_url']=value['compatible_url'] if value['playback_status']=='ready' and has_compatible else None
        value['playback_info']=video.get('compatible_info') if has_compatible else None
        if sources is None: sources = {s['id']:s for s in self.rows('sources')}
        source = sources.get(video.get('source_id'),{})
        value['source_label']=Path(source.get('path','')).name or source.get('path','').rstrip('\\/') or ('导入副本' if video.get('owned') else '关联原片')
        folder=PurePosixPath(video.get('relative_path') or video.get('filename') or '').parent.as_posix()
        value['relative_folder']='' if folder=='.' else folder
        value['can_delete'] = bool(video.get('owned') or source.get('allow_delete',False))
        value['frames'] = [dict(frame,url=f'/assets/{key}/'+Path(urllib.parse.urlsplit(str(frame.get('url',''))).path).name+'?rev='+str(frame_revision)) for frame in value.get('frames',[])[:12]]
        if value['kind']=='image':
            value['image_url'] = f'/media/{key}'
            value['preview_url'] = value['cover']
            value['display_url'] = value['image_url'] if Path(video.get('filename','')).suffix.lower() in {'.jpg','.jpeg','.jfif','.png','.webp','.gif','.bmp','.avif'} else value['cover']
            value['sprite'],value['compatible_url'],value['frames'] = None,None,[]
        value.pop('location', None)
        return value

    def stats(self,db):
        row = db.execute('''SELECT count(*),coalesce(sum(kind='video'),0),coalesce(sum(kind='image'),0),
            coalesce(sum(status='ready'),0),coalesce(sum(status='missing'),0),coalesce(sum(status='error'),0),
            coalesce(sum(status='changed'),0),coalesce(sum(size),0),coalesce(sum(CASE WHEN kind='video' THEN duration ELSE 0 END),0),
            coalesce(sum(favorite<>0),0),coalesce(sum(status IN ('queued','analyzing')),0),
            coalesce(sum(content_scope='adult'),0),coalesce(sum(content_scope='personal'),0),coalesce(sum(content_scope='review'),0)
            FROM videos INDEXED BY videos_stats''').fetchone()
        return dict(zip(('total','video_count','image_count','ready','missing','errors','changed','total_size','total_duration','favorites','processing',
                         'scope_adult','scope_personal','scope_review'),row))

    def list_videos(self,query):
        def first(name,default=''):
            value = query.get(name,[default])
            return str(value[0] if isinstance(value,list) else value)
        try:
            limit = max(1,min(120,int(first('limit','60'))))
            offset = max(0,int(first('offset','0')))
        except ValueError: raise ApiError(400,'分页参数必须是整数。')
        clauses,values = [],[]
        kind = first('kind','all')
        if kind not in ('all','video','image'): raise ApiError(400,'媒体类型无效。')
        if kind!='all': clauses.append('kind=?'); values.append(kind)
        scope=first('scope','all')
        if scope!='all': clauses.append('content_scope=?'); values.append(validated_scope(scope))
        filter_name = first('filter','all')
        if filter_name=='ready': clauses.append("status='ready'")
        elif filter_name=='processing': clauses.append("status IN ('queued','analyzing')")
        elif filter_name=='issues': clauses.append("status IN ('missing','error','changed')")
        elif filter_name!='all': raise ApiError(400,'状态筛选无效。')
        if first('favorite')=='1': clauses.append('favorite<>0')
        if first('continue')=='1': clauses.append("kind='video' AND last_position>0 AND duration>0 AND last_position<duration-1")
        tag = first('tag')[:100]
        if tag:
            clauses.append("EXISTS (SELECT 1 FROM video_tags WHERE video_id=videos.id AND tag=?)")
            values.append(tag)
        model_tag = first('model_tag').strip()
        if len(model_tag)>100: raise ApiError(400,'模型标签过长。')
        if model_tag:
            clauses.append('EXISTS (SELECT 1 FROM model_tags WHERE video_id=videos.id AND label=?)')
            values.append(model_tag)
        search = first('q').strip()[:500]
        if search:
            literal = search.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
            clauses.append("""(title LIKE ? ESCAPE '\\' OR filename LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\'
                OR EXISTS (SELECT 1 FROM video_tags WHERE video_id=videos.id AND tag LIKE ? ESCAPE '\\')
                OR EXISTS (SELECT 1 FROM model_tags WHERE video_id=videos.id AND label LIKE ? ESCAPE '\\')
                OR json_extract(videos.data,'$.ai_summary') LIKE ? ESCAPE '\\'
                OR EXISTS (SELECT 1 FROM json_each(videos.data,'$.classification.details') AS detail
                    WHERE (CASE WHEN detail.type='object' THEN json_extract(detail.value,'$.key') END) LIKE ? ESCAPE '\\'))""")
            values += ['%'+literal+'%']*7
        order = {'newest':'created_at DESC,id DESC','title':'title COLLATE NOCASE,id','duration':'duration DESC,id','size':'size DESC,id'}.get(first('sort','newest'))
        if not order: raise ApiError(400,'排序方式无效。')
        where = ' WHERE '+' AND '.join(clauses) if clauses else ''
        with self.db() as db:
            total = db.execute('SELECT count(*) FROM videos'+where,values).fetchone()[0]
            rows = db.execute("SELECT json_remove(data,'$.description','$.markers','$.performers','$.temporal_events','$.timeline_analysis'),coalesce(json_array_length(data,'$.markers'),0),CASE WHEN json_extract(data,'$.description_source')='manual' THEN substr(description,1,160) ELSE '' END FROM videos"+where+' ORDER BY '+order+' LIMIT ? OFFSET ?',values+[limit,offset]).fetchall()
            revision = self.revision(db)
        sources = {s['id']:s for s in self.rows('sources')}
        items = []
        for raw,marker_count,manual_summary in rows:
            video = json.loads(raw)
            video['marker_count'] = marker_count
            public=self.public_video(video,full=False,sources=sources)
            public['manual_summary']=manual_summary
            items.append(public)
        return {'items':items,'total':total,'limit':limit,'offset':offset,'revision':revision}

    def model_tag_facets(self,query):
        try: limit=min(100,max(1,int(query.get('limit',['60'])[0])))
        except (ValueError,TypeError,IndexError): raise ApiError(400,'标签数量参数无效。')
        with self.db() as db:
            total=db.execute('SELECT count(DISTINCT label) FROM model_tags').fetchone()[0]
            rows=list(db.execute('SELECT label,count(*) FROM model_tags GROUP BY label ORDER BY count(*) DESC,label LIMIT ?', (limit,)))
        return {'items':[{'label':label,'count':count} for label,count in rows],'total':total}

    def state(self, local=False, compact=False):
        sources = self.rows('sources')
        source_map = {s['id']:s for s in sources}
        videos = [] if compact else [self.public_video(v,full=False,sources=source_map) for v in self.rows('videos')]
        videos.sort(key=lambda v: v.get('created_at', ''), reverse=True)
        with self.db() as db:
            jobs = [json.loads(r[0]) for r in db.execute("SELECT data FROM jobs ORDER BY json_extract(data,'$.created_at') DESC LIMIT 100")]
            titles = {row[0]:row[1] for row in db.execute('SELECT id,title FROM videos WHERE id IN ('+','.join('?' for _ in jobs)+')',[j.get('video_id') for j in jobs])} if jobs else {}
            stats = self.stats(db)
            tags = [r[0] for r in db.execute('SELECT tag,count(*) AS n FROM video_tags GROUP BY tag ORDER BY n DESC,tag LIMIT 50')]
            revision = self.revision(db)
            queue_summary = {row[0]:row[1] for row in db.execute("SELECT json_extract(data,'$.status'),count(*) FROM jobs WHERE json_extract(data,'$.status') IN ('queued','running') GROUP BY json_extract(data,'$.status')")}
            audit_attention = self.audit.attention(db) if local else None
        for job in jobs: job['video_title'] = titles.get(job.get('video_id'))
        result = {
            'videos': videos, 'sources': sources, 'settings': self.settings(), 'jobs': jobs,
            'stats': stats,'tags':tags,'revision':revision,'queue':queue_summary,'audit_attention':audit_attention,
            'system': {'app':'cinevault','version':VERSION,'python_version':sys.version.split()[0],'sqlite_version':sqlite3.sqlite_version,
                       'ffmpeg':bool(self.ffmpeg),'ffprobe':bool(self.ffprobe),
                       'data_dir': str(self.root), 'port':self.port, 'lan':self.lan, 'lan_enabled':self.lan,
                       'classifier':self.classifier_status(),'external_player':self.playback.external_status()},
            'backups': self.backups()
        }
        result['system']['external_player']['local_only']=True
        if not local:
            result['system']['external_player'].update(available=False,notice='原片播放器只能在资料库所在电脑本机打开；手机请使用网页兼容播放。')
        if local and self.lan:
            result['system']['pairing_token'] = self.token
            addresses = set()
            with contextlib.suppress(OSError):
                for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                    if not item[4][0].startswith('127.'):
                        addresses.add(f'http://{item[4][0]}:{self.port}')
            result['system']['lan_urls'] = sorted(addresses)
        return result

    def new_video(self,path,info,owned=False,filename=None,source_id=None,relative_path=None,content_scope='review',auto_classify=False,settings=None):
        filename = filename or path.name
        settings = self.settings() if settings is None else settings
        location = path.relative_to(self.root).as_posix() if owned else str(path.absolute())
        key = uid()
        title = re.sub(r'[._]+', ' ', Path(filename).stem).strip() or filename
        return {'id':key,'location':location,'filename':filename,'title':title,'owned':owned,
                'kind':'image' if Path(filename).suffix.lower() in IMAGE_EXTENSIONS else 'video',
                'content_scope':content_scope,'scope_source':'source' if source_id and content_scope!='review' else 'unclassified','scope_lock':False,
                'classification':None,'classification_status':'unclassified',
                'auto_classify_pending':bool(auto_classify and content_scope=='review'),
                'auto_timeline_pending':bool(settings.get('auto_timeline_new_imports') and Path(filename).suffix.lower() in VIDEO_EXTENSIONS),
                'auto_preview_pending':bool(settings.get('auto_preview_new_imports') and settings.get('card_auto_preview') and Path(filename).suffix.lower() in VIDEO_EXTENSIONS),
                'auto_description_pending':bool(settings.get('auto_describe_new_imports')),
                'source_id':source_id,'relative_path':relative_path,'description':'','description_source':'metadata',
                'tags':[],'favorite':False,'status':'queued','size':info.st_size,'mtime_ns':info.st_mtime_ns,
                'duration':0,'width':0,'height':0,'codec':'','container':path.suffix.lstrip('.'),
                'frames':[],'markers':[],'last_position':0,'progress':0,'sha256':None,'error':None,'created_at':now()}

    def register(self, path, owned=False, filename=None, source_id=None, relative_path=None):
        path = Path(path)
        if not path.is_file() or is_link_or_reparse(path):
            raise ApiError(400, '请选择存在的普通图片或视频文件，不要使用链接。')
        filename = filename or path.name
        if Path(filename).suffix.lower() not in EXTENSIONS:
            raise ApiError(400, '暂不支持此文件扩展名。请导入图片或视频文件。')
        location = path.relative_to(self.root).as_posix() if owned else str(path.absolute())
        with self.catalog_lock, self.db() as db:
            if db.execute('SELECT 1 FROM excluded WHERE location=?', (location,)).fetchone():
                return None
            row = db.execute('SELECT id FROM videos WHERE location=?', (location,)).fetchone()
            if row:
                return self.video(row[0])
            scope='review'
            if source_id:
                source_row=db.execute('SELECT data FROM sources WHERE id=?',(source_id,)).fetchone()
                if source_row: scope=json.loads(source_row[0]).get('content_scope','review')
            settings = self.settings()
            video = self.new_video(path,path.stat(),owned,filename,source_id,relative_path,scope,
                                   auto_classify=bool(settings.get('auto_classify_new_imports',False)),settings=settings)
            key = video['id']
            # Use the already open transaction to publish one unique entry.
            db.execute('INSERT INTO videos(id,location,data) VALUES(?,?,?)', (key,location,json.dumps(video,ensure_ascii=False)))
            self.bump(db)
        self.enqueue('analyze', key)
        return video

    def source_path(self,raw):
        path = Path(str(raw).strip().strip('"')).expanduser()
        if not path.is_absolute() or not path.is_dir():
            raise ApiError(400,'请输入存在且在线的文件夹或磁盘根目录完整路径。')
        path = path.absolute()
        if any(is_link_or_reparse(part) for part in (path,*path.parents)):
            raise ApiError(400,'不能关联符号链接、目录联接或重解析目录，请选择原始目录。')
        if is_within(path,APP) or is_within(path,self.root):
            raise ApiError(400,'请选择媒体目录，不要关联映库程序或资料库 data 目录。')
        return path

    def normalize_excludes(self,values,root):
        if values is None: return []
        if not isinstance(values,list) or len(values)>100:
            raise ApiError(400,'排除目录应为不超过100项的列表。')
        result=[]
        for value in values:
            if not isinstance(value,str) or not value.strip(): raise ApiError(400,'排除目录必须是文字。')
            value=value.strip().strip('"')[:1000]
            if '..' in Path(value).parts or value in ('.','/','\\'):
                raise ApiError(400,'排除目录不能使用上级目录或整个根目录。')
            if Path(value).is_absolute() and not is_within(value,root):
                raise ApiError(400,'绝对排除路径必须位于所选媒体目录内。')
            if value not in result: result.append(value)
        return result

    def source_overlap(self,path,ignore=None):
        for source in self.rows('sources'):
            if source['id']==ignore: continue
            if is_within(path,source['path']) or is_within(source['path'],path):
                if path_key(path)==path_key(source['path']): return source
                raise ApiError(409,'此目录与已关联目录重叠：'+source['path']+'。请保留其中一个目录，避免重复索引。')
        return None

    def add_source(self, raw, options=None):
        options=options or {}
        path=self.source_path(raw)
        excludes=self.normalize_excludes(options.get('exclude_dirs',[]),path)
        existing=self.source_overlap(path)
        if existing:
            self.enqueue('scan')
            return existing
        allow_delete=options.get('allow_delete',False)
        if not isinstance(allow_delete,bool): raise ApiError(400,'原片删除开关必须为布尔值。')
        if allow_delete and options.get('confirm_path')!=str(path):
            raise ApiError(400,'启用删除原片需要准确输入关联目录完整路径。')
        scope=validated_scope(options.get('content_scope','review'))
        source={'id':uid(),'path':str(path),'created_at':now(),'last_scan':None,'count':0,'error':None,'config_revision':1,'content_scope':scope,
                'platform_paths':{platform_name():str(path)},
                'exclude_dirs':excludes,'allow_delete':allow_delete,'device_identity':device_identity(path),
                'scan':{'status':'idle','discovered':0,'processed':0,'skipped':0,'errors':0}}
        with self.catalog_lock,self.db() as db:
            # Recheck after the possibly slow device lookup and serialize add/relink.
            existing=self.source_overlap(path)
            if existing:
                source=existing
            else:
                db.execute('INSERT INTO sources VALUES(?,?)',(source['id'],json.dumps(source,ensure_ascii=False)))
                self.audit.record('source_added',{'path':source['path'],'allow_delete':allow_delete,
                    'exclude_dirs':excludes,'originals_modified':False},db=db,source_id=source['id'])
                self.bump(db)
        self.enqueue('scan')
        return source

    def walk_media(self,root,excludes,summary,max_entries=None,max_seconds=None):
        """Read-only bounded-memory walk. Entries are never opened for decoding."""
        started=time.monotonic()
        stack=[Path(root)]
        excluded_names={x.casefold() for x in excludes if not Path(x).is_absolute() and '/' not in x and '\\' not in x}
        excluded_paths=[Path(x).absolute() if Path(x).is_absolute() else (Path(root)/x).absolute() for x in excludes if Path(x).is_absolute() or '/' in x or '\\' in x]
        is_disk_root=path_key(root)==path_key(Path(root).anchor)
        while stack and not self.stop.is_set():
            current=stack.pop()
            if max_seconds is not None and time.monotonic()-started>=max_seconds:
                summary['complete']=False; break
            try:
                if is_link_or_reparse(current):
                    summary['counts']['ignored_dirs']+=1; continue
                with os.scandir(current) as entries:
                    summary.setdefault('_visited',set()).add(path_key(current))
                    for entry in entries:
                        summary['_entries']=summary.get('_entries',0)+1
                        if (max_entries and summary['_entries']>max_entries) or (max_seconds and time.monotonic()-started>=max_seconds):
                            summary['complete']=False; return
                        try:
                            info=entry.stat(follow_symlinks=False)
                            linked=stat_module.S_ISLNK(info.st_mode) or bool(getattr(info,'st_file_attributes',0)&0x400)
                            if linked:
                                summary['counts']['ignored_dirs']+=1
                                continue
                            if stat_module.S_ISDIR(info.st_mode):
                                candidate=Path(entry.path)
                                name=entry.name.casefold()
                                if (name in DEFAULT_EXCLUDE_DIRS or name in excluded_names
                                    or (is_disk_root and path_key(current)==path_key(root) and name in SYSTEM_ROOT_DIRS)
                                    or is_within(candidate,APP) or is_within(candidate,self.root)
                                    or any(is_within(candidate,excluded) for excluded in excluded_paths)):
                                    summary['counts']['ignored_dirs']+=1
                                    continue
                                stack.append(candidate)
                            elif stat_module.S_ISREG(info.st_mode):
                                candidate=Path(entry.path)
                                suffix=candidate.suffix.lower()
                                if suffix not in EXTENSIONS:
                                    summary['counts']['unsupported_files']+=1
                                    extensions=summary.setdefault('unsupported_extensions',{})
                                    label=suffix[:24] or '(无扩展名)'
                                    if label not in extensions and len(extensions)>=50: label='(其他扩展名)'
                                    extensions[label]=extensions.get(label,0)+1
                                    continue
                                kind='image' if suffix in IMAGE_EXTENSIONS else 'video'
                                summary['counts']['images' if kind=='image' else 'videos']+=1
                                yield candidate,info,kind
                        except OSError as exc:
                            summary['counts']['errors']+=1
                            summary.setdefault('_unreadable_paths',set()).add(path_key(entry.path))
                            if len(summary['errors'])<20: summary['errors'].append({'path':entry.path,'error':str(exc)})
            except OSError as exc:
                summary['counts']['errors']+=1
                summary.setdefault('_failed',set()).add(path_key(current))
                if len(summary['errors'])<20: summary['errors'].append({'path':str(current),'error':str(exc)})
        if self.stop.is_set(): summary['complete']=False

    def preview_source(self,body):
        path=self.source_path(body.get('path',''))
        excludes=self.normalize_excludes(body.get('exclude_dirs',[]),path)
        result={'path':str(path),'counts':{'videos':0,'images':0,'ignored_dirs':0,'unsupported_files':0,'errors':0},
                'examples':[],'errors':[],'device_identity':device_identity(path),'complete':True,'read_only':True,
                'default_exclude_dirs':sorted(DEFAULT_EXCLUDE_DIRS),'exclude_dirs':excludes}
        for candidate,info,kind in self.walk_media(path,excludes,result,max_entries=100000,max_seconds=30):
            if len(result['examples'])<12: result['examples'].append({'path':str(candidate),'kind':kind})
        result['scan_status']='complete' if result['complete'] and not result['counts']['errors'] else 'partial'
        result['entries_examined']=result.pop('_entries',0)
        result.pop('_visited',None); result.pop('_failed',None); result.pop('_unreadable_paths',None)
        return result

    def relink_source(self, key, raw=None, options=None):
        options=options or {}
        source = next((s for s in self.rows('sources') if s['id'] == key), None)
        if not source:
            raise ApiError(404, '关联目录不存在。')
        relinking=raw is not None
        path=self.source_path(raw) if relinking else Path(source['path'])
        self.source_overlap(path,ignore=key)
        if 'allow_delete' in options:
            if not isinstance(options['allow_delete'],bool): raise ApiError(400,'原片删除开关必须为布尔值。')
            if options['allow_delete'] and options.get('confirm_path')!=str(path):
                raise ApiError(400,'启用删除原片需要准确输入关联目录完整路径。')
        excludes=self.normalize_excludes(options.get('exclude_dirs',source.get('exclude_dirs',[])),path)
        scope=validated_scope(options.get('content_scope',source.get('content_scope','review')))
        if 'apply_existing' in options and not isinstance(options['apply_existing'],bool): raise ApiError(400,'应用现有条目开关必须为布尔值。')
        if 'acknowledge_metadata_only' in options and not isinstance(options['acknowledge_metadata_only'],bool):
            raise ApiError(400,'仅路径大小核对的确认开关必须为布尔值。')
        identity=device_identity(path) if relinking or options.get('allow_delete') else source.get('device_identity')
        if not relinking and options.get('allow_delete') and not same_device(source.get('device_identity'),identity):
            raise ApiError(409,'磁盘标识已改变，请先核对并明确重新关联目录，再启用删除权限。')
        with self.catalog_lock,self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self.source_overlap(path,ignore=key)
            row=db.execute('SELECT data FROM sources WHERE id=?',(key,)).fetchone()
            if not row: raise ApiError(404,'关联目录不存在。')
            source=json.loads(row[0])
            old=source['path']
            before={name:source.get(name) for name in ('path','allow_delete','exclude_dirs','content_scope')}
            if relinking:
                aliases=dict(source.get('platform_paths') or {})
                aliases[platform_name(old)]=old
                aliases[platform_name()]=str(path)
                source['platform_paths']=aliases
                source['reattach_verification']='metadata_only_user_confirmed' if options.get('acknowledge_metadata_only') else 'manual_path_confirmed'
                records=[json.loads(r[0]) for r in db.execute('SELECT data FROM videos WHERE source_id=?',(key,))]
                occupied={path_key(r[0]):r[1] for r in db.execute('SELECT location,id FROM videos')}
                updates=[]
                for video in records:
                    relative=PurePosixPath(video.get('relative_path') or '')
                    if not relative.parts or relative.is_absolute() or '..' in relative.parts:
                        raise ApiError(409,'已有资料包含无效相对路径，已停止整个重关联操作。')
                    new_location=str(path.joinpath(*relative.parts).absolute())
                    if path_key(new_location) in occupied and occupied[path_key(new_location)]!=video['id']:
                        raise ApiError(409,'目标位置有媒体已关联，已停止整个重关联操作。')
                    video.update(location=new_location,needs_verify=True)
                    updates.append((new_location,json.dumps(video,ensure_ascii=False),video['id']))
                # Pure paths interpret tombstones in the previous OS's syntax.
                old_type=PureWindowsPath if re.match(r'^[A-Za-z]:[\\/]|^\\\\',old) else PurePosixPath
                old_root=old_type(old)
                tombstones=[]
                for (location,) in db.execute('SELECT location FROM excluded'):
                    with contextlib.suppress(ValueError):
                        relative=old_type(location).relative_to(old_root)
                        if '..' not in relative.parts: tombstones.append((str(path.joinpath(*relative.parts).absolute()),))
                db.executemany('UPDATE videos SET location=?,data=? WHERE id=?',updates)
                db.executemany('INSERT OR IGNORE INTO excluded VALUES(?)',tombstones)
            source.update(path=str(path),error=None,exclude_dirs=excludes,
                          allow_delete=options.get('allow_delete',False if relinking else source.get('allow_delete',False)),content_scope=scope)
            if options.get('apply_existing'):
                db.execute("""UPDATE videos SET data=json_set(data,'$.content_scope',?,'$.scope_source','source','$.scope_lock',json('false'))
                    WHERE source_id=? AND coalesce(json_extract(data,'$.scope_source'),'')<>'manual' AND coalesce(json_extract(data,'$.scope_lock'),0)=0""",(scope,key))
            source['config_revision']=source.get('config_revision',1)+1
            if identity: source['device_identity']=identity
            source['scan']={'status':'idle','discovered':0,'processed':0,'skipped':0,'errors':0}
            db.execute('UPDATE sources SET data=? WHERE id=?',(json.dumps(source,ensure_ascii=False),key))
            changes={name:{'before':value,'after':source.get(name)} for name,value in before.items() if value!=source.get(name)}
            if changes or options.get('apply_existing'):
                self.audit.record('source_changed',{'path':source['path'],'changes':changes,
                    'applied_to_existing':bool(options.get('apply_existing')),'originals_modified':False},db=db,source_id=key)
            self.bump(db)
        self.enqueue('scan')
        self.schedule_pending()
        return source

    def reattach_preview(self,key,body):
        with self.db() as db:
            row=db.execute('SELECT data FROM sources WHERE id=?',(key,)).fetchone()
            if not row: raise ApiError(404,'关联目录不存在。')
            source=json.loads(row[0])
            total=db.execute('SELECT count(*) FROM videos WHERE source_id=?',(key,)).fetchone()[0]
            records=list(db.execute("SELECT id,json_extract(data,'$.relative_path'),size,json_extract(data,'$.sha256') FROM videos WHERE source_id=? ORDER BY size,id LIMIT 64",(key,)))
        candidate=self.source_path(body.get('path') or body.get('candidate_path') or '')
        overlap=self.source_overlap(candidate,ignore=key)
        if overlap: raise ApiError(409,'候选目录已由另一来源使用，请先处理重叠来源。')
        if 'verify_hashes' in body and not isinstance(body['verify_hashes'],bool): raise ApiError(400,'指纹核验开关必须为布尔值。')
        fingerprint=hashlib.sha256(json.dumps(sorted((relative,size,digest) for _,relative,size,digest in records if relative),ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()
        result={'source_id':key,'path':str(candidate),'read_only':True,'mutated':False,'catalog_count':total,
                'catalog_fingerprint':fingerprint,'fingerprint_basis':'up to 64 smallest catalog entries: relative_path,size,known SHA-256',
                'checked_count':0,'size_matches':0,'missing_count':0,'size_mismatches':0,'unsafe_paths':0,
                'hash_verified':0,'hash_mismatches':0,'hash_skipped':0,'hash_bytes_read':0,'examples':[],
                'device_identity':device_identity(candidate),'platform_paths':source.get('platform_paths',{}),
                'limitations':['只读核验，不创建标记文件，也不会自动修改来源路径。',
                               '最多核对64个条目的路径与大小，最多完整核验2个已有SHA-256的文件，读取预算512MiB、约15秒。',
                               '抽样匹配不能证明所有文件完整；首次跨系统切换仍需你明确重新关联。']}
        deadline=time.monotonic()+15
        budget=512*1024*1024
        for _,relative,size,digest in records:
            result['checked_count']+=1
            rel=PurePosixPath(relative or '')
            target=candidate.joinpath(*rel.parts)
            if not rel.parts or rel.is_absolute() or '..' in rel.parts or not plain_descendant(target,candidate):
                result['unsafe_paths']+=1; continue
            example={'relative_path':relative}
            try:
                before=target.stat()
                if not target.is_file(): raise FileNotFoundError()
                if before.st_size!=size:
                    result['size_mismatches']+=1
                    example['status']='size_mismatch'
                else:
                    result['size_matches']+=1
                    example['status']='size_match'
                    if (digest and size>0 and body.get('verify_hashes',True) and result['hash_verified']+result['hash_mismatches']<2
                        and size<=budget and time.monotonic()<deadline):
                        checksum=hashlib.sha256()
                        complete=True
                        with target.open('rb') as file:
                            while chunk:=file.read(4*1024*1024):
                                if self.stop.is_set() or time.monotonic()>=deadline or len(chunk)>budget:
                                    complete=False; break
                                checksum.update(chunk); budget-=len(chunk); result['hash_bytes_read']+=len(chunk)
                        after=target.stat()
                        if complete and (before.st_size,before.st_mtime_ns)==(after.st_size,after.st_mtime_ns):
                            if checksum.hexdigest()==digest:
                                result['hash_verified']+=1; example['status']='hash_match'
                            else:
                                result['hash_mismatches']+=1; example['status']='hash_mismatch'
                        else: result['hash_skipped']+=1
                    else: result['hash_skipped']+=1
            except OSError:
                result['missing_count']+=1; example['status']='missing_or_unreadable'
            if len(result['examples'])<12: result['examples'].append(example)
        mismatch=result['missing_count']+result['size_mismatches']+result['unsafe_paths']+result['hash_mismatches']
        result['can_reattach']=bool(records and not mismatch and result['hash_verified']>0)
        result['status']='sample_verified' if result['can_reattach'] else 'mismatch' if mismatch else 'metadata_only' if records else 'no_catalog_samples'
        result['sampled_all_entries']=len(records)==total
        return result

    def enqueue(self, kind, video_id=None, **options):
        if self.stop.is_set(): return None
        if kind=='classify':
            options=dict(options,engine=options.get('engine') or self.settings().get('classification_engine','vit'))
            if options['engine'] not in CLASSIFICATION_ENGINES: raise ApiError(400,'未知的本地分类引擎。')
        ident = (kind, video_id)
        deferred=False
        with self.jobs_lock:
            if ident in self.active:
                if kind=='scan': self.rescan_requested=True
                return None
            if kind in ('analyze','verify','classify','timeline','transcode','preview','describe') and sum(k in ('analyze','verify','classify','timeline','transcode','preview','describe') for k,_ in self.active)>=128:
                deferred=True
            else:
                self.active.add(ident)
        if deferred:
            if kind=='verify' and video_id:
                self.update(video_id,needs_verify=True,verify_deep=bool(options.get('deep')))
            if kind=='preview' and video_id:
                # The caller has already checked whether this request is a
                # permitted retry. Preserve it until a bounded slot is free.
                self.update(video_id,preview_batch_pending=True,preview_batch_retry=True,
                            preview_batch_automatic=bool(options.get('automatic')),
                            preview_batch_profile=self.playback.preview_target_version)
            return None
        job = {'id':uid(),'kind':kind,'video_id':video_id,'status':'queued','progress':0,'error':None,'created_at':now(),'options':options}
        if kind=='preview': job['profile']=self.playback.preview_target_version
        self._save('jobs',job['id'],job)
        if kind=='classify' and video_id:
            self.update(video_id,classification_status='queued',classification_error=None,classification_engine_pending=options['engine'])
        (self.scan_queue if kind=='scan' else self.queue).put(job)
        return job['id']

    def schedule_pending(self):
        with self.jobs_lock:
            room=max(0,128-sum(kind in ('analyze','verify','classify','timeline','transcode','preview','describe') for kind,_ in self.active))
            active={key for kind,key in self.active if kind in ('analyze','verify','classify','timeline','transcode','preview','describe')}
        if not room or self.stop.is_set(): return
        with self.db() as db:
            verification=list(db.execute("SELECT id,coalesce(json_extract(data,'$.verify_deep'),0) FROM videos WHERE coalesce(json_extract(data,'$.needs_verify'),0)=1 ORDER BY created_at,id LIMIT ?",(room+len(active),)))
            candidates=[r[0] for r in db.execute("SELECT id FROM videos WHERE status='queued' ORDER BY created_at,id LIMIT ?",(room+len(active),))]
        for key,deep in verification:
            if key not in active:
                self.enqueue('verify',key,deep=bool(deep))
                active.add(key)
        for key in candidates:
            if key not in active:
                self.enqueue('analyze',key)
        self.health.schedule_pending()

    def scan_work(self):
        while not self.stop.is_set():
            try: job=self.scan_queue.get(timeout=.5)
            except queue.Empty: continue
            job.update(status='running',started_at=now())
            self._save('jobs',job['id'],job)
            try:
                self.scan()
                job.update(status='queued' if self.stop.is_set() else 'complete',progress=1,finished_at=now())
            except Exception as exc:
                job.update(status='error',error=str(exc)[:2000],finished_at=now())
                traceback.print_exc()
            finally:
                self._save('jobs',job['id'],job)
                with self.jobs_lock:
                    self.active.discard(('scan',None))
                    again=self.rescan_requested
                    self.rescan_requested=False
                self.scan_queue.task_done()
                if again: self.enqueue('scan')

    def source_progress(self,source,scan,**changes):
        with self.db() as db:
            row=db.execute('SELECT data FROM sources WHERE id=?',(source['id'],)).fetchone()
            current=json.loads(row[0]) if row else None
            if not current or current['path']!=source['path'] or current.get('config_revision',1)!=source.get('config_revision',1):
                return False
            current.update(changes,scan=dict(scan))
            if not current.get('device_identity') and source.get('device_identity'):
                current['device_identity']=source['device_identity']
            db.execute('UPDATE sources SET data=? WHERE id=?',(json.dumps(current,ensure_ascii=False),source['id']))
            self.audit.scan_result(db,current,scan)
            self.bump(db)
            return True

    def work(self):
        while not self.stop.is_set():
            try:
                job = self.queue.get(timeout=.5)
            except queue.Empty:
                self.schedule_pending()
                continue
            job.update(status='running',started_at=now())
            self._save('jobs',job['id'],job)
            work_started=time.monotonic()
            try:
                key, kind, options = job['video_id'], job['kind'], job.get('options',{})
                if kind == 'scan':
                    self.scan()
                elif kind == 'health':
                    for video in self.rows('videos'):
                        self.enqueue('verify',video['id'],deep=bool(options.get('deep')))
                elif key:
                    self.current_video = key
                    self.current_kind = kind
                    # Model inference consumes immutable frame bytes and only
                    # updates an existing row; it must not delay file deletion.
                    with contextlib.nullcontext() if kind=='describe' else self.vlock(key):
                        if self.video(key) and key not in self.cancelled:
                            if kind == 'analyze': self.analyze(key)
                            elif kind == 'verify': self.verify(key, bool(options.get('deep')))
                            elif kind == 'transcode': self.transcode(key)
                            elif kind == 'timeline':
                                if self.automation.allowed(key,kind,options): self.analyze_timeline(key,options)
                                else: job['skipped']=True
                            elif kind == 'preview':
                                if self.automation.allowed(key,kind,options): self.playback.generate_preview(key)
                                else: job['skipped']=True
                            elif kind == 'describe':
                                if self.automation.allowed(key,kind,options): self.describe(key,options)
                                else: job['skipped']=True
                            elif kind == 'classify':
                                current=self.require_video(key)
                                automatic=bool(options.get('automatic'))
                                allowed=(self.settings().get('auto_classify_new_imports',False) and current.get('content_scope','review')=='review'
                                         and current.get('scope_source')!='manual' and not current.get('scope_lock')
                                         and current.get('classification_status')!='complete')
                                if automatic and not allowed:
                                    if current.get('classification_status')!='complete':
                                        self.update(key,classification_status='unclassified',classification_error=None)
                                else:
                                    self.classify_media(key,options.get('engine'))
                            elif kind == 'cover': self.cover(key,float(options.get('time',0)))
                job.update(status='complete',progress=1,finished_at=now())
            except Exception as exc:
                job.update(status='queued' if self.stop.is_set() else 'cancelled' if job['kind']=='timeline' and job.get('video_id') in self.timeline_cancelled else 'error',error=str(exc)[:2000],finished_at=now())
                if not self.stop.is_set() and job.get('video_id') and job['kind'] in ('analyze','verify'):
                    self.update(job['video_id'],status='missing' if isinstance(exc,FileNotFoundError) else 'error',error=str(exc)[:2000],progress=0,needs_verify=False)
                if job['kind']=='timeline' and self.video(job.get('video_id')):
                    current=self.require_video(job['video_id'])
                    self.update(job['video_id'],timeline_analysis=dict(current.get('timeline_analysis') or {},
                                status='cancelled' if job.get('video_id') in self.timeline_cancelled or self.stop.is_set() else 'error',
                                error=str(exc)[:1200],finished_at=now()))
                if job['kind'] in ('transcode','preview') and self.video(job.get('video_id')):
                    prefix='playback' if job['kind']=='transcode' else 'preview'
                    self.update(job['video_id'],**{prefix+'_status':'queued' if self.stop.is_set() else 'error',
                                                   prefix+'_error':str(exc)[:1600]})
                if job['kind']=='describe' and self.video(job.get('video_id')):
                    current=self.require_video(job['video_id'])
                    self.update(job['video_id'],description_analysis=dict(current.get('description_analysis') or {},status='error',error=str(exc)[:1200],finished_at=now()))
                traceback.print_exc()
            finally:
                job['elapsed_seconds']=round(time.monotonic()-work_started,4)
                self.current_video = None
                self.current_kind = None
                # Removing a film also removes its history and queued jobs.
                # A queued job dequeued after deletion must not recreate it.
                if not job.get('video_id') or self.video(job['video_id']):
                    self._save('jobs',job['id'],job)
                else:
                    with self.db() as db:
                        db.execute('DELETE FROM jobs WHERE id=?',(job['id'],))
                with self.jobs_lock:
                    self.active.discard((job['kind'],job['video_id']))
                if job['kind']=='timeline': self.timeline_cancelled.discard(job['video_id'])
                self.queue.task_done()
                self.automation.finished(job)
                if job['kind']=='analyze' and job['status']=='complete':
                    try: self.automation.after_first_analysis(job['video_id'],tasks=('preview',))
                    except Exception: traceback.print_exc()
                    try: self.auto_classify_new(job['video_id'])
                    except Exception: traceback.print_exc()
                    try: self.automation.after_first_analysis(job['video_id'])
                    except Exception: traceback.print_exc()
                self.schedule_pending()
                try: self.cache.maintain()
                except Exception: traceback.print_exc()
                # Retain bounded task history; video error details stay in the catalogue.
                jobs = sorted(self.rows('jobs'),key=lambda j:j['created_at'])
                old = [j for j in jobs[:-150] if j['status'] not in ('running','queued')]
                if old:
                    with self.db() as db:
                        db.executemany('DELETE FROM jobs WHERE id=?',[(j['id'],) for j in old])

    def update_scan_status(self,source,previous,status,error):
        # A scan sees a path snapshot. Never publish a finding after a source
        # has been reattached or its scope has changed underneath that scan.
        with self.db() as db:
            row=db.execute('''SELECT videos.data FROM videos JOIN sources ON sources.id=videos.source_id
                WHERE videos.id=? AND videos.location=? AND sources.id=? AND videos.status<>?
                AND json_extract(sources.data,'$.path')=?
                AND coalesce(json_extract(sources.data,'$.config_revision'),1)=?''',
                (previous['id'],previous['location'],source['id'],status,source['path'],source.get('config_revision',1))).fetchone()
            if not row:
                return False
            value=json.loads(row[0])
            value.update(status=status,error=error,last_checked=now())
            db.execute('UPDATE videos SET data=? WHERE id=?',(json.dumps(value,ensure_ascii=False),previous['id']))
            self.bump(db)
            return True

    @staticmethod
    def missing_in_walk(location,root,visited,uncertain,known_absent):
        # A vanished nested directory is missing only when an observed ancestor
        # has a directly absent child. Existing excluded and inaccessible paths
        # keep their prior state, even when another sibling was readable.
        parent=Path(location).parent
        while is_within(parent,root):
            normalized=path_key(parent)
            if normalized in uncertain:
                return False
            if normalized in visited:
                return True
            if normalized in known_absent:
                return known_absent[normalized]
            ancestor=parent.parent
            if path_key(ancestor) in visited:
                try:
                    parent.lstat()
                    missing=False
                except FileNotFoundError:
                    missing=True
                except OSError:
                    missing=False
                known_absent[normalized]=missing
                return missing
            if ancestor==parent:
                break
            parent=ancestor
        return False

    def scan(self):
        # Snapshot only the fields needed for discovery once. Existing unchanged
        # files require no SELECT/JSON parse/commit per entry during the walk.
        with self.db() as db:
            known={path_key(row[1]):dict(zip(('id','location','status','size','mtime_ns','has_sha','source_id'),row))
                   for row in db.execute('SELECT id,location,status,size,mtime_ns,has_sha,source_id FROM videos WHERE owned=0')}
            excluded={path_key(r[0]) for r in db.execute('SELECT location FROM excluded')}
        for source in self.rows('sources'):
            if self.stop.is_set(): break
            root=Path(source['path'])
            scan={'status':'scanning','discovered':0,'processed':0,'skipped':0,'errors':0,'started_at':now(),'finished_at':None,
                  'inserted':0,'missing_detected':0,'changed_detected':0,'returned_detected':0,'excluded':0}
            source.update(scan=scan,error=None)
            try:
                if not root.is_dir() or is_link_or_reparse(root):
                    source.update(error='目录离线、不存在或已变为链接；原有资料保留。')
                    scan.update(status='offline',finished_at=now())
                    self.source_progress(source,scan,error=source['error'])
                    continue
                identity=device_identity(root)
                if not same_device(source.get('device_identity'),identity):
                    source.update(error='此路径对应的磁盘标识已改变。已停止自动扫描，请核对后重新关联。',observed_device_identity=identity)
                    scan.update(status='device_changed',finished_at=now())
                    self.source_progress(source,scan,error=source['error'],observed_device_identity=identity)
                    continue
                if not source.get('device_identity'): source['device_identity']=identity
            except OSError as exc:
                source.update(error='无法读取磁盘标识，保留原有资料：'+str(exc))
                scan.update(status='offline',errors=1,finished_at=now())
                self.source_progress(source,scan,error=source['error'])
                continue
            if not self.source_progress(source,scan,error=None): continue
            summary={'counts':{'videos':0,'images':0,'ignored_dirs':0,'unsupported_files':0,'errors':0},'errors':[],'complete':True}
            pending=[]
            seen=set()
            last_report=time.monotonic()
            settings=self.settings()
            auto_classify=bool(settings.get('auto_classify_new_imports',False))

            def flush():
                nonlocal pending
                if not pending: return
                with self.catalog_lock,self.db() as db:
                    # A user may have changed source options while this scan was
                    # walking. Never republish records against a relinked path.
                    current=db.execute('SELECT data FROM sources WHERE id=?',(source['id'],)).fetchone()
                    current=json.loads(current[0]) if current else None
                    if not current or current['path']!=source['path'] or current.get('config_revision',1)!=source.get('config_revision',1):
                        pending=[]; return
                    inserted=[]
                    for value in pending:
                        cursor=db.execute('INSERT OR IGNORE INTO videos(id,location,data) SELECT ?,?,? WHERE NOT EXISTS(SELECT 1 FROM excluded WHERE location=?)',
                                          (value['id'],value['location'],json.dumps(value,ensure_ascii=False),value['location']))
                        if cursor.rowcount:
                            inserted.append(value['id'])
                            self.last_seen.pop(path_key(value['location']),None)
                    if inserted: self.bump(db)
                pending=[]
                scan['inserted']+=len(inserted)
                for key in inserted: self.enqueue('analyze',key)

            for path,info,kind in self.walk_media(root,source.get('exclude_dirs',[]),summary):
                location=path_key(path)
                seen.add(location)
                scan['discovered']+=1
                if location in excluded:
                    scan['skipped']+=1
                    scan['excluded']+=1
                    continue
                old=known.get(location)
                if old:
                    scan['processed']+=1
                    if old['status']=='missing':
                        if self.enqueue('verify',old['id']): scan['returned_detected']+=1
                    elif old['has_sha'] and (old['size'],old['mtime_ns'])!=(info.st_size,info.st_mtime_ns) and old['status']!='changed':
                        if self.update_scan_status(source,old,'changed','文件大小或修改时间发生变化，请校验原片。'):
                            scan['changed_detected']+=1
                else:
                    observed=(info.st_size,info.st_mtime_ns)
                    previous=self.last_seen.get(location)
                    self.last_seen[location]=observed
                    if previous and previous!=observed:
                        scan['skipped']+=1
                        continue
                    value=self.new_video(path,info,source_id=source['id'],relative_path=path.relative_to(root).as_posix(),
                                         content_scope=source.get('content_scope','review'),auto_classify=auto_classify,settings=settings)
                    pending.append(value)
                    known[location]={'id':value['id'],'location':value['location'],'status':'queued','size':info.st_size,'mtime_ns':info.st_mtime_ns,'has_sha':False,'source_id':source['id']}
                    scan['processed']+=1
                    if len(pending)>=64: flush()
                if time.monotonic()-last_report>=.75:
                    flush()
                    scan['errors']=summary['counts']['errors']
                    if not self.source_progress(source,scan):
                        summary['complete']=False
                        break
                    last_report=time.monotonic()
            flush()
            # Offline, inaccessible and excluded trees retain their records.
            visited=summary.get('_visited',set())-summary.get('_failed',set())
            uncertain=summary.get('_failed',set())|summary.get('_unreadable_paths',set())
            known_absent={}
            if summary['complete']:
                for location,old in known.items():
                    if (old['source_id']==source['id'] and location not in seen and location not in uncertain
                        and old['status']!='missing' and self.missing_in_walk(old['location'],root,visited,uncertain,known_absent)):
                        if self.update_scan_status(source,old,'missing','原片未在已成功读取的目录中找到，资料已保留。'):
                            scan['missing_detected']+=1
            scan.update(status='complete' if summary['complete'] and not summary['counts']['errors'] else 'partial',
                        seen_videos=summary['counts']['videos'],seen_images=summary['counts']['images'],
                        errors=summary['counts']['errors'],ignored_dirs=summary['counts']['ignored_dirs'],
                        unsupported_files=summary['counts']['unsupported_files'],
                        unsupported_extensions=[{'extension':name,'count':count} for name,count in sorted(summary.get('unsupported_extensions',{}).items(),key=lambda item:(-item[1],item[0]))],
                        finished_at=now())
            # Preserve concurrent user edits to permissions/exclusions.
            self.source_progress(source,scan,last_scan=now(),count=scan['discovered'],error='; '.join(e['error'] for e in summary['errors'][:3]) or None)
            self.schedule_pending()

    def watch(self):
        next_scan, next_backup = time.monotonic()+3, time.monotonic()+10
        while not self.stop.wait(1):
            current = time.monotonic()
            settings = self.settings()
            try: self.cache.maintain()
            except Exception: traceback.print_exc()
            if current >= next_scan:
                if settings['watch_enabled']:
                    self.enqueue('scan')
                next_scan = current + max(15,int(settings['watch_interval']))
            if current >= next_backup:
                today = time.strftime('%Y-%m-%d',time.gmtime())
                existing = list((self.root/'backups').glob(f'auto-{today}-*.zip'))
                if not existing and self.rows('videos'):
                    try:
                        self.backup(auto=True)
                        automatic = sorted((self.root/'backups').glob('auto-*.zip'))
                        for old in automatic[:-7]:
                            old.unlink()
                    except Exception:
                        traceback.print_exc()
                next_backup = current+3600

    def run_tool(self, args, timeout=180):
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        if args[0] == self.ffmpeg:
            args = [args[0],'-nostdin','-filter_threads','1','-filter_complex_threads','1',*args[1:]]
            if Path(args[-1]).suffix.lower() in ('.jpg','.jpeg','.png','.webp'):
                args=[*args[:-1],'-map_metadata','-1','-map_metadata:s:v','-1',args[-1]]
        with self.process_lock:
            if self.stop.is_set(): raise RuntimeError('服务正在停止，任务将在下次启动继续。')
            if self.current_video in self.cancelled: raise RuntimeError('影片任务已取消。')
            process = subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=flags)
            self.processes.add(process)
        try:
            try:
                stdout,stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise
            if process.returncode:
                message = stderr.decode('utf-8',errors='replace')[-1600:].strip()
                raise RuntimeError(message or '媒体引擎处理失败。')
            return subprocess.CompletedProcess(args,process.returncode,stdout,stderr)
        finally:
            with self.process_lock:
                self.processes.discard(process)

    def hash_file(self,path,key=None):
        digest = hashlib.sha256()
        size = path.stat().st_size
        read, last_report = 0, 0
        with open(path,'rb') as file:
            while chunk := file.read(4*1024*1024):
                if self.stop.is_set(): raise RuntimeError('服务正在停止，稍后可重新校验。')
                if key in self.cancelled: raise RuntimeError('影片任务已取消。')
                digest.update(chunk)
                read += len(chunk)
                if key and time.monotonic()-last_report > 1:
                    self.update(key,progress=min(.35,.35*read/max(1,size)))
                    last_report = time.monotonic()
        return digest.hexdigest()

    def check_source_device(self,video):
        if video.get('owned') or not video.get('source_id'): return
        with self.db() as db:
            row=db.execute('SELECT data FROM sources WHERE id=?',(video['source_id'],)).fetchone()
        if not row: raise RuntimeError('关联目录已不存在，请重新关联后操作。')
        source=json.loads(row[0])
        root=Path(source['path'])
        if any(part.exists() and is_link_or_reparse(part) for part in (root,*root.parents)) or not plain_descendant(self.path(video),root):
            raise RuntimeError('关联路径中出现链接或目录联接；已停止操作，请检查原始路径。')
        try: identity=device_identity(source['path'])
        except OSError: raise FileNotFoundError('关联磁盘离线，原有资料与指纹已保留。')
        if not same_device(source.get('device_identity'),identity):
            raise RuntimeError('关联路径的磁盘标识已改变；请核对并重新关联后再操作。')

    def analyze_image(self,key):
        if not self.ffmpeg or not self.ffprobe:
            raise RuntimeError('图片分析需要 FFmpeg/FFprobe。')
        video=self.require_video(key)
        self.check_source_device(video)
        path=self.path(video)
        before=path.stat()
        self.update(key,status='analyzing',progress=.01,error=None)
        digest=self.hash_file(path,key)
        after=path.stat()
        if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
            self.update(key,status='queued',progress=0,error='图片正在复制，稍后重试。')
            retry=threading.Timer(20,lambda:self.enqueue('analyze',key)); retry.daemon=True; retry.start()
            return
        try:
            metadata=json.loads(self.run_tool([self.ffprobe,'-v','error','-show_format','-show_streams','-of','json',str(path)],timeout=60).stdout)
            stream=next((s for s in metadata.get('streams',[]) if s.get('codec_type')=='video'),None)
            if not stream or not stream.get('width') or not stream.get('height'):
                raise RuntimeError('未找到有效图片画面。')
            width,height=int(stream['width']),int(stream['height'])
            asset=self.root/'assets'/key
            asset.mkdir(exist_ok=True)
            temporary=asset/'cover-new.jpg'
            self.run_tool([self.ffmpeg,'-hide_banner','-loglevel','error','-threads','2','-i',str(path),'-frames:v','1','-an',
                           '-vf','scale=1200:1200:force_original_aspect_ratio=decrease','-q:v','3','-y',str(temporary)],timeout=90)
            if not temporary.is_file(): raise RuntimeError('无法生成图片预览。')
            os.replace(temporary,asset/'cover.jpg')
        except (RuntimeError,ValueError,subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f'{path.suffix.lstrip(".").upper()} 图片解码失败；实际支持范围取决于媒体引擎：{str(exc)[-1200:]}')
        final=path.stat()
        changed=(bool(video.get('sha256') and digest!=video['sha256']) or
                 (before.st_size,before.st_mtime_ns)!=(final.st_size,final.st_mtime_ns))
        changes={'kind':'image','width':width,'height':height,'duration':0,'codec':stream.get('codec_name',''),
                 'container':path.suffix.lstrip('.'),'frames':[],'markers':[],'progress':1,'has_cover':True,
                 'has_sprite':False,'has_compatible':False,'cover_revision':time.time_ns(),'last_checked':now(),
                 'status':'changed' if changed else 'ready','error':'图片内容与首次入库指纹不同；原始指纹已保留。' if changed else None}
        changes.update(visual_metadata(stream))
        if not video.get('sha256'): changes.update(sha256=digest,size=before.st_size,mtime_ns=before.st_mtime_ns)
        if not video.get('description') or video.get('description_source')=='metadata':
            changes.update(description=f'{width} × {height} 像素 · {path.suffix.lstrip(".").upper()} 图片。此描述来自文件参数，画面内容可自行补充。',description_source='metadata')
        self.update(key,**changes)

    def analyze(self,key):
        video=self.require_video(key)
        if video.get('kind')=='image':
            return self.analyze_image(key)
        self.check_source_device(video)
        if not self.ffmpeg or not self.ffprobe:
            raise RuntimeError('未找到 FFmpeg/FFprobe。请将对应系统的媒体引擎放入 bin 目录后重启。')
        video = self.require_video(key)
        path = self.path(video)
        before = path.stat()
        # Brief settling window catches new files still being copied.
        self.stop.wait(.35)
        settled = path.stat()
        if (before.st_size,before.st_mtime_ns) != (settled.st_size,settled.st_mtime_ns):
            self.update(key,status='queued',error='文件正在复制，将在下一次扫描后处理。')
            # Retry from the watcher after the active job leaves the dedup set.
            threading.Timer(20,lambda:self.enqueue('analyze',key)).start()
            return
        self.update(key,status='analyzing',progress=.01,error=None)
        digest = self.hash_file(path,key)
        hashed_stat = path.stat()
        if (before.st_size,before.st_mtime_ns)!=(hashed_stat.st_size,hashed_stat.st_mtime_ns):
            self.update(key,status='queued',progress=0,error='计算指纹时文件发生变化，稍后将重试。')
            retry = threading.Timer(20,lambda:self.enqueue('analyze',key))
            retry.daemon = True
            retry.start()
            return
        changed = bool(video.get('sha256') and digest != video['sha256'])
        metadata = json.loads(self.run_tool([self.ffprobe,'-v','error','-show_format','-show_streams','-of','json',str(path)],timeout=120).stdout)
        streams = metadata.get('streams',[])
        visual = next((s for s in streams if s.get('codec_type') == 'video' and not s.get('disposition',{}).get('attached_pic')),None)
        if not visual:
            raise RuntimeError('文件中没有可识别的视频流。')
        fmt = metadata.get('format',{})
        duration = float(fmt.get('duration') or visual.get('duration') or 0)
        if duration <= 0:
            raise RuntimeError('无法读取有效视频时长，文件可能不完整。')
        width,height = int(visual.get('width',0)),int(visual.get('height',0))
        codec = visual.get('codec_name','unknown')
        formats = fmt.get('format_name',path.suffix[1:]).split(',')
        container = formats[0]
        if 'mov' in formats and 'mp4' in formats:
            brand = str(fmt.get('tags',{}).get('major_brand','')).strip().lower()
            suffix = path.suffix.lower().lstrip('.')
            container = 'mov' if brand=='qt' else suffix if suffix in ('mp4','m4v','3gp','3g2','mj2') else 'mp4'
        elif 'matroska' in formats and 'webm' in formats:
            container = 'webm' if path.suffix.lower()=='.webm' else 'matroska'
        frame_rate = visual.get('avg_frame_rate','0/1')
        try:
            numerator,denominator = frame_rate.split('/')
            fps = round(float(numerator)/max(1,float(denominator)),3)
        except (ValueError,ZeroDivisionError):
            fps = 0
        changes = dict(duration=duration,width=width,height=height,codec=codec,container=container,
                       fps=fps,audio_codec=next((s.get('codec_name') for s in streams if s.get('codec_type')=='audio'),None),
                       bitrate=int(fmt.get('bit_rate') or 0),progress=.4)
        changes.update(visual_metadata(visual))
        audio=next((s for s in streams if s.get('codec_type')=='audio'),{})
        changes.update(audio_channels=audio.get('channels'),audio_sample_rate=audio.get('sample_rate'),
                       subtitle_tracks=[{'codec':s.get('codec_name'),'language':s.get('tags',{}).get('language')} for s in streams if s.get('codec_type')=='subtitle'])
        if not video.get('sha256'):
            changes.update(sha256=digest,size=before.st_size,mtime_ns=before.st_mtime_ns)
        if not video.get('description') or video.get('description_source')=='metadata':
            changes.update(description=f'{int(duration//60)} 分 {int(duration%60)} 秒 · {width} × {height} · {codec.upper()} / {container.upper()}'+(f' · {fps:g} fps' if fps else '')+'。此描述来自文件参数；画面内容可自行补充或使用本地视觉模型生成。',description_source='metadata')
        self.update(key,**changes)
        asset = self.root/'assets'/key
        asset.mkdir(exist_ok=True)
        frames = []
        # Seek before decode, so even a long film uses only tiny windows.
        for index in range(12):
            timestamp = max(0,min(duration-.04,duration*(index+.5)/12))
            target = asset/f'frame{index:02}.jpg'
            self.run_tool([self.ffmpeg,'-hide_banner','-loglevel','error','-threads','2','-ss',str(timestamp),'-i',str(path),'-frames:v','1','-an','-vf','scale=320:180:force_original_aspect_ratio=decrease,pad=320:180:(ow-iw)/2:(oh-ih)/2','-q:v','4','-y',str(target)],timeout=60)
            frames.append({'time':round(timestamp,3),'url':f'/assets/{key}/{target.name}'})
            self.update(key,progress=.4+.4*(index+1)/12)
        self.run_tool([self.ffmpeg,'-hide_banner','-loglevel','error','-framerate','1','-i',str(asset/'frame%02d.jpg'),'-frames:v','1','-vf','tile=4x3','-q:v','4','-y',str(asset/'sprite.jpg')],timeout=60)
        if video.get('cover_manual'):
            self.cover(key,video.get('cover_time') or 0,representative=False)
        else:
            selected=self.select_cover(key,frames)
            self.cover(key,selected['selected_time'],representative=True)
            self.update(key,cover_selection=selected)
        markers = [m for m in self.require_video(key).get('markers',[]) if m.get('kind')=='manual']
        # Scene analysis is bounded to the first two minutes to avoid a full
        # decode on every import. These are actual cuts, not semantic highlights.
        scan_duration = min(120,duration)
        try:
            scene = self.run_tool([self.ffmpeg,'-hide_banner','-threads','2','-i',str(path),'-t',str(scan_duration),'-an','-vf',"scale=160:-2,select='gt(scene,0.38)',showinfo",'-vsync','vfr','-f','null','-'],timeout=90)
            times = [float(s) for s in re.findall(r'pts_time:([0-9.]+)',scene.stderr.decode('utf-8',errors='replace'))]
            selected = []
            for timestamp in times:
                if timestamp > 1 and (not selected or timestamp-selected[-1] > 2):
                    selected.append(timestamp)
                    if len(selected)>=16: break
            markers += [{'id':'scene-'+str(i),'time':round(t,3),'title':f'场景切换 {i+1}','kind':'scene'} for i,t in enumerate(selected)]
        except (RuntimeError,subprocess.TimeoutExpired):
            pass
        after = path.stat()
        if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
            changed = True
        self.update(key,frames=frames,frames_revision=time.time_ns(),markers=sorted(markers,key=lambda m:m['time']),scene_scan_duration=scan_duration,has_cover=True,has_sprite=True,
                    status='changed' if changed else 'ready',progress=1,last_checked=now(),
                    error='内容与首次入库指纹不同，原始指纹已保留。' if changed else None)

    def select_cover(self,key,frames):
        asset=self.root/'assets'/key
        try:
            output=self.run_tool([self.ffmpeg,'-hide_banner','-loglevel','error','-framerate','1','-i',str(asset/'frame%02d.jpg'),
                                  '-frames:v',str(len(frames)),'-vf','scale=64:36,format=gray','-f','rawvideo','-pix_fmt','gray','-'],timeout=45).stdout
            scores=[]
            frame_size=64*36
            for index,frame in enumerate(frames):
                pixels=output[index*frame_size:(index+1)*frame_size]
                if len(pixels)!=frame_size: continue
                # Letterbox borders matter less than the picture's centre.
                sample=[pixels[y*64+x] for y in range(3,33) for x in range(4,60)]
                mean=sum(sample)/len(sample)
                variance=sum((value-mean)**2 for value in sample)/len(sample)
                contrast=math.sqrt(variance)
                edges=sum(abs(pixels[y*64+x]-pixels[y*64+x-1])+abs(pixels[y*64+x]-pixels[(y-1)*64+x]) for y in range(4,32) for x in range(5,59))/(28*54*2)
                black=sum(value<12 for value in sample)/len(sample)
                white=sum(value>245 for value in sample)/len(sample)
                exposure=max(0,1-abs(mean-125)/125)
                score=.45*min(1,contrast/64)+.35*min(1,edges/35)+.2*exposure-.8*black-.5*white-.01*abs(index-(len(frames)-1)/2)
                scores.append((score,float(frame['time'])))
            if not scores: raise ValueError('没有可评分的抽样帧。')
            score,timestamp=max(scores)
            return {'method':'luminance-contrast-edges-v1','candidate_count':len(scores),'selected_time':timestamp,'score':round(score,4),
                    'notice':'根据明暗、对比度和边缘清晰度选择代表帧，避免黑屏；不按露骨程度评分。'}
        except (RuntimeError,ValueError,subprocess.TimeoutExpired):
            return {'method':'middle-sample-fallback','candidate_count':len(frames),'selected_time':float(frames[len(frames)//2]['time']),
                    'notice':'画面质量评分不可用，已回退到中间抽样帧。'}

    def cover(self,key,timestamp,representative=False):
        video = self.require_video(key)
        if video.get('kind')=='image': raise RuntimeError('静态图片不支持视频时间点封面。')
        path = self.path(video)
        end_margin = max(.1,1/max(1,float(video.get('fps') or 24)))
        timestamp = max(0,min(float(timestamp),max(0,video.get('duration',1)-end_margin)))
        asset = self.root/'assets'/key
        asset.mkdir(exist_ok=True)
        temp = asset/'cover-new.jpg'
        # Automatic covers select a representative frame. User-selected covers
        # capture the requested frame directly, including a safe end-of-file seek.
        filters = 'scale=960:540:force_original_aspect_ratio=decrease,pad=960:540:(ow-iw)/2:(oh-ih)/2'
        self.run_tool([self.ffmpeg,'-hide_banner','-loglevel','error','-threads','2','-ss',str(timestamp),'-i',str(path),'-frames:v','1','-an','-vf',filters,'-q:v','3','-y',str(temp)],timeout=90)
        if not temp.exists(): raise RuntimeError('这个时间点没有可提取的画面，请尝试稍早的位置。')
        os.replace(temp,asset/'cover.jpg')
        changes={'cover_time':timestamp,'cover_manual':not representative,'cover_revision':time.time_ns(),'has_cover':True}
        if not representative:
            changes['cover_selection']={'method':'manual','selected_time':timestamp,'notice':'用户指定的画面。'}
        self.update(key,**changes)

    def timeline_candidates(self,start,frame_bytes,audio_bytes,duration):
        events,count,_=self.analysis.candidates(start,frame_bytes,audio_bytes,duration)
        return events,count

    def queue_timeline(self,key,body=None):
        return self.analysis.queue(key,body)

    def cancel_timeline(self,key):
        with self.db_lock:
            video=self.require_video(key)
            analysis=video.get('timeline_analysis') or {}
            if analysis.get('status') not in ('queued','running'): return {'ok':True,'cancelled':False}
            self.timeline_cancelled.add(key)
            with self.process_lock:
                if self.current_video==key and self.current_kind=='timeline':
                    for process in self.processes:
                        with contextlib.suppress(OSError): process.terminate()
            self.update(key,timeline_analysis=dict(analysis,status='cancelled',finished_at=now(),error='用户取消了时间轴分析。'))
            return {'ok':True,'cancelled':True,'notice':'取消请求已登记，后台释放任务后可重新提交。'}

    def analyze_timeline(self,key,options=None):
        return self.analysis.analyze(key,options)

    def verify(self,key,deep=False):
        video = self.require_video(key)
        self.check_source_device(video)
        path = self.path(video)
        if not path.is_file():
            self.update(key,status='missing',error='原片不存在或硬盘离线。',last_checked=now(),progress=0,needs_verify=False)
            return
        self.update(key,progress=.01)
        before = path.stat()
        digest = self.hash_file(path,key)
        after = path.stat()
        if video.get('sha256') and digest != video['sha256']:
            self.update(key,status='changed',error='SHA-256 与首次入库指纹不同。原始指纹已保留，请核对文件。',last_checked=now(),observed_sha256=digest,progress=1,needs_verify=False)
            return
        if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
            self.update(key,status='changed',error='校验期间文件发生变化，请待复制完成后重试。',last_checked=now(),progress=1,needs_verify=False)
            return
        if deep:
            if not self.ffmpeg: raise RuntimeError('完整解码需要 FFmpeg。')
            self.update(key,progress=.5)
            self.run_tool([self.ffmpeg,'-hide_banner','-loglevel','error','-xerror','-threads','2','-i',str(path),'-map','0:v?','-map','0:a?','-f','null','-'],timeout=24*3600)
            decoded_stat = path.stat()
            if (before.st_size,before.st_mtime_ns)!=(decoded_stat.st_size,decoded_stat.st_mtime_ns):
                self.update(key,status='changed',error='完整解码期间文件发生变化，请待文件稳定后重试。',last_checked=now(),progress=1,needs_verify=False)
                return
        # A matching checksum says bytes are stable, not that an unsupported or
        # corrupt item suddenly decoded successfully. Incomplete analysis must
        # pass the decoder again before the item can become ready.
        ready=(bool(video.get('width') and video.get('height') and (video.get('has_cover') or video.get('cover_revision')))
               if video.get('kind')=='image' else bool(video.get('duration') and video.get('frames') and (video.get('has_cover') or video.get('cover_revision'))))
        changes = dict(status='ready' if ready else 'queued',error=None,last_checked=now(),verification='decoded' if deep else 'sha256',progress=1,
                       size=before.st_size,mtime_ns=before.st_mtime_ns,needs_verify=False,verify_deep=False)
        if not video.get('sha256'):
            changes.update(sha256=digest,size=before.st_size,mtime_ns=before.st_mtime_ns)
        self.update(key,**changes)
        if not ready:
            self.enqueue('analyze',key)

    def transcode(self,key):
        return self.playback.transcode(key)

    def describe(self,key,options=None):
        return self.providers.run(key,options or {})

    def edit(self,key,body):
        if self.require_video(key).get('kind')=='image' and ('cover_time' in body or 'last_position' in body):
            raise ApiError(400,'图片不支持视频时间点或观看进度。')
        changes = {}
        if 'people_count' in body:
            count=body['people_count']
            if count is not None and (isinstance(count,bool) or not isinstance(count,int) or not 0<=count<=100):
                raise ApiError(400,'手动人数应为0至100的整数，未知时请留空。')
            changes.update(people_count=count,people_count_source='manual')
        if 'scene_type' in body:
            scene=body['scene_type']
            if scene is not None and (not isinstance(scene,str) or len(scene)>80):
                raise ApiError(400,'手动场景类型应不超过80个字符，未知时请留空。')
            changes.update(scene_type=scene.strip() if isinstance(scene,str) else None,scene_type_source='manual')
        if 'content_scope' in body:
            changes.update(content_scope=validated_scope(body['content_scope']),scope_source='manual',scope_lock=True)
        if 'catalog_code' in body:
            if not isinstance(body['catalog_code'],str): raise ApiError(400,'番号或资料编号必须是文字。')
            changes['catalog_code']=body['catalog_code'].strip()[:120]
        if 'release_date' in body: changes['release_date']=validated_date(body['release_date'],'发行日期')
        if 'metadata_source_url' in body: changes['metadata_source_url']=validated_source_url(body['metadata_source_url'])
        if 'performers' in body:
            if not isinstance(body['performers'],list) or len(body['performers'])>50: raise ApiError(400,'出演者应为不超过50项的列表。')
            performers=[]
            for performer in body['performers']:
                if not isinstance(performer,dict) or not isinstance(performer.get('name'),str) or not performer['name'].strip():
                    raise ApiError(400,'每位出演者都需要填写姓名，未知时请不要添加。')
                performers.append({'name':performer['name'].strip()[:120],
                                   'birth_date':validated_date(performer.get('birth_date'),'出生日期'),
                                   'source_url':validated_source_url(performer.get('source_url'))})
            changes['performers']=performers
        if any(field in body for field in ('catalog_code','release_date','metadata_source_url','performers')):
            changes['metadata_verification']='manual_unverified'
        for field,limit in (('title',300),('description',8000)):
            if field in body:
                if not isinstance(body[field],str): raise ApiError(400,'标题和描述必须是文字。')
                changes[field] = body[field].strip()[:limit]
        if 'title' in changes and not changes['title']: raise ApiError(400,'标题不能为空。')
        if 'description' in changes: changes['description_source']='manual'
        if 'tags' in body:
            if not isinstance(body['tags'],list): raise ApiError(400,'标签必须是列表。')
            changes['tags'] = list(dict.fromkeys(str(v).strip()[:50] for v in body['tags'][:30] if str(v).strip()))
        if 'favorite' in body: changes['favorite'] = bool(body['favorite'])
        video = self.require_video(key)
        if 'last_position' in body:
            try: changes['last_position'] = max(0,min(float(body['last_position']),video.get('duration') or 1e9))
            except (TypeError,ValueError): raise ApiError(400,'观看时间无效。')
        self.update(key,**changes)
        if 'cover_time' in body:
            try: timestamp = float(body['cover_time'])
            except (TypeError,ValueError): raise ApiError(400,'封面时间无效。')
            self.enqueue('cover',key,time=timestamp)
        return self.public_video(self.require_video(key))

    @contextlib.contextmanager
    def cancel_video_work(self,key):
        # Explicit deletion cancels this film's work rather than waiting for a
        # potentially hours-long transcode or deep decode. Other films continue.
        self.cancelled.add(key)
        with self.process_lock:
            if self.current_video == key:
                for process in self.processes:
                    with contextlib.suppress(OSError): process.terminate()
        try:
            yield
        finally:
            self.cancelled.discard(key)

    def original_to_delete(self,video,confirmation):
        if confirmation != video['filename']:
            raise ApiError(409,'永久删除原片需要准确输入原文件名。')
        path = self.path(video)
        if video.get('owned'):
            if not plain_descendant(path,self.root/'media'):
                raise ApiError(409,'资料库原片路径已变为链接，已停止删除。')
        else:
            with self.db() as db:
                row=db.execute('SELECT data FROM sources WHERE id=?',(video.get('source_id'),)).fetchone()
            source=json.loads(row[0]) if row else {}
            if not source.get('allow_delete',False):
                raise ApiError(403,'关联目录为只读索引。删除原片须先在目录设置中明确授权。')
            try: self.check_source_device(video)
            except (OSError,RuntimeError) as exc: raise ApiError(409,str(exc)) from exc
        try: info=path.lstat()
        except OSError as exc:
            raise ApiError(409,'原片无法访问，已停止删除。可选择仅移出资料库。') from exc
        if not stat_module.S_ISREG(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
            raise ApiError(409,'原片已不是普通文件，已停止删除。')
        if (info.st_size,info.st_mtime_ns)!=(video.get('size'),video.get('mtime_ns')):
            raise ApiError(409,'原片已发生变化。请先重新核对并分析该文件，再删除原片。')
        return path

    def delete(self,key,body):
        mode=body.get('mode')
        if mode not in ('index','file'): raise ApiError(400,'请选择删除资料或同时删除原片。')
        initial=self.require_video(key)
        if mode=='file': self.original_to_delete(initial,body.get('confirm'))
        asset=self.root/'assets'/key
        if not plain_descendant(asset,self.root/'assets'):
            raise ApiError(409,'生成资料路径中出现链接，已停止删除。')
        with self.cancel_video_work(key),self.vlock(key),self.catalog_lock:
            video = self.require_video(key)
            source_id=video.get('source_id')
            detail={'filename':video['filename'],'media_kind':video.get('kind','video'),'mode':mode}
            if mode=='file':
                path=self.original_to_delete(video,body.get('confirm'))
                # The journal intent survives a process loss between unlink and
                # the catalogue commit. It must never be read as a success.
                intent=self.audit.record('original_delete_started',dict(detail,status='pending'),source_id=source_id,video_id=key)
                detail['intent_id']=intent
                try:
                    path=self.original_to_delete(video,body.get('confirm'))
                    path.unlink()
                except (OSError,ApiError) as exc:
                    self.audit.record('original_delete_failed',dict(detail,status='failed',originals_modified=False),
                                      source_id=source_id,video_id=key)
                    if isinstance(exc,ApiError): raise
                    raise ApiError(409,'系统未能删除原片，资料条目已保留。请检查占用或权限。') from exc
            with self.db() as db:
                db.execute('INSERT OR IGNORE INTO excluded VALUES(?)',(video['location'],))
                db.execute('DELETE FROM videos WHERE id=?',(key,))
                db.execute("DELETE FROM jobs WHERE json_extract(data,'$.video_id')=?",(key,))
                event_id=self.audit.record('catalogue_removed',dict(detail,status='complete',originals_modified=mode=='file',
                    original_deleted=mode=='file'),db=db,source_id=source_id,video_id=key)
                self.bump(db)
            cleanup_pending=False
            if os.path.lexists(asset):
                try:
                    assets=self.root/'assets'
                    if not plain_descendant(asset,assets) or asset.resolve(strict=True)!=assets.resolve(strict=True)/key:
                        raise OSError('缓存目录发生变化')
                    shutil.rmtree(asset)
                except OSError:
                    cleanup_pending=True
                    self.audit.record('derivative_cleanup_deferred',{'removal_event':event_id,'originals_modified':False},
                                      source_id=source_id,video_id=key)
            return {'ok':True,'original_deleted':mode=='file','audit_event_id':event_id,'cleanup_pending':cleanup_pending,
                    'note':'缓存文件暂被占用，后续清理可重试。' if cleanup_pending else '删除已记录。旧备份作为独立快照保留。'}

    def backups(self):
        result = []
        for path in sorted((self.root/'backups').glob('*.zip')):
            if path.is_symlink(): continue
            stat = path.stat()
            result.append({'name':path.name,'url':'/backups/'+urllib.parse.quote(path.name),'size':stat.st_size,
                           'created_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(stat.st_mtime))})
        return result

    def backup(self,auto=False):
        with self.backup_lock:
            name = ('auto-' if auto else 'catalog-')+time.strftime('%Y-%m-%d-%H%M%S',time.gmtime())+'-'+uid()[:6]+'.zip'
            temp_db = self.root/'tmp'/('backup-'+uid()+'.sqlite3')
            target = self.root/'backups'/name
            part = target.with_suffix('.partial')
            try:
                with self.catalog_lock,self.db_lock:
                    with contextlib.closing(sqlite3.connect(self.db_path)) as source,contextlib.closing(sqlite3.connect(temp_db)) as dest:
                        source.backup(dest)
                    with zipfile.ZipFile(part,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=3) as archive:
                        archive.write(temp_db,'library.sqlite3')
                        # Manual backups retain approved silent previews. Keep
                        # scheduled recovery snapshots light and exclude transcodes.
                        with self.db() as db:
                            registered={key for key, in db.execute('SELECT id FROM videos')}
                        for path in (self.root/'assets').glob('*/*'):
                            supported=(path.suffix.lower() in ('.jpg','.webp','.vtt') or (not auto and path.name=='preview.mp4'))
                            if supported and path.parent.name in registered and path.is_file() and plain_descendant(path,self.root/'assets'):
                                archive.write(path,path.relative_to(self.root).as_posix(),compress_type=zipfile.ZIP_STORED)
                        manifest = {'app':'cinevault','schema_version':3,'version':VERSION,'created_at':now(),
                                    'includes_originals':False,'includes_transcodes':False,
                                    'includes_previews':not auto,'includes_audit':True,
                                    'original_media_note':'Original video files are NOT included. Back up data/media and external source folders separately.',
                                    'sources':self.rows('sources')}
                        archive.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2))
                    os.replace(part,target)
                return {'url':'/backups/'+urllib.parse.quote(name),'filename':name,'includes_originals':False,
                        'includes_previews':not auto,'includes_audit':True}
            finally:
                with contextlib.suppress(OSError): temp_db.unlink()
                with contextlib.suppress(OSError): part.unlink()

    def close(self):
        self.stop.set()
        self.providers.close()
        self.local_models.close()
        # Stop derived-media writers before releasing the data-directory lock.
        # Persist interrupted work as queued for the next launch.
        with self.process_lock:
            processes = list(self.processes)
            for process in processes:
                with contextlib.suppress(OSError): process.terminate()
        for process in processes:
            with contextlib.suppress(OSError,subprocess.TimeoutExpired): process.wait(timeout=3)
            if process.poll() is None:
                with contextlib.suppress(OSError): process.kill()
        self.worker.join(timeout=5)
        self.scanner.join(timeout=5)
        self.watcher.join(timeout=2)
        with self.db() as db:
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        # A slow local model request can still be winding down. In that case
        # keep the lock until the OS closes it at process exit, so no restore
        # or second server can start during the remaining exit window.
        if not self.worker.is_alive() and not self.scanner.is_alive() and not self.watcher.is_alive():
            with contextlib.suppress(OSError): self.lock_file.close()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'CineVault/1.0'

    @property
    def lib(self): return self.server.library

    @property
    def local(self):
        try: return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError: return False

    def log_message(self,fmt,*args):
        # No query strings or pairing tokens in access logs.
        if len(args)>1 and str(args[1]).startswith(('4','5')):
            print(now(),self.command,urllib.parse.urlsplit(self.path).path,args[1],flush=True)

    def guard(self):
        host = self.headers.get('Host','')
        parsed = urllib.parse.urlsplit('//'+host)
        hostname = parsed.hostname
        allowed = hostname in ('localhost','127.0.0.1','::1',socket.gethostname().lower())
        if not allowed and hostname:
            try:
                address = ipaddress.ip_address(hostname)
                allowed = address.is_loopback or (self.lib.lan and address.is_private)
            except ValueError: pass
        if not allowed or parsed.port not in (None,self.lib.port):
            raise ApiError(403,'不受支持的访问地址。请使用本机或局域网地址。')
        origin = self.headers.get('Origin')
        if origin:
            source = urllib.parse.urlsplit(origin)
            if source.scheme not in ('http','https') or source.netloc.lower()!=host.lower():
                raise ApiError(403,'禁止来自其他网站的访问。')
        if self.command in ('POST','PATCH','DELETE') and self.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise ApiError(403,'禁止跨网站修改资料库。')
        path = urllib.parse.urlsplit(self.path).path
        if not self.local:
            if not self.lib.lan: raise ApiError(403,'仅允许本机访问。')
            if path.startswith(('/api/','/media/','/assets/','/backups/')) and path != '/api/login':
                cookie = http.cookies.SimpleCookie()
                with contextlib.suppress(http.cookies.CookieError): cookie.load(self.headers.get('Cookie',''))
                session = cookie.get('cinevault_session')
                token = self.headers.get('Authorization','').removeprefix('Bearer ')
                if (not session or session.value not in self.lib.sessions) and not secrets.compare_digest(token,self.lib.token):
                    raise ApiError(401,'请输入电脑端显示的局域网访问口令。')

    def response(self,status,value=None,headers=None):
        data = value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False,allow_nan=False).encode('utf-8') if value is not None else b''
        self.send_response(status)
        self.send_header('Content-Type',(headers or {}).get('Content-Type','application/json; charset=utf-8'))
        self.send_header('Content-Length',str(len(data)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','same-origin')
        for key,value in (headers or {}).items():
            if key.lower()!='content-type': self.send_header(key,value)
        self.end_headers()
        if self.command!='HEAD': self.wfile.write(data)

    def body(self):
        try: length = int(self.headers.get('Content-Length','0'))
        except ValueError: raise ApiError(400,'请求长度无效。')
        if length<0 or length>1024*1024: raise ApiError(413,'资料请求过大。视频请使用导入接口。')
        if length==0: return {}
        try:
            def invalid_constant(value):
                raise ValueError('非有限数值不受支持')
            value = json.loads(self.rfile.read(length),parse_constant=invalid_constant)
            if not isinstance(value,dict): raise ValueError()
            return value
        except (ValueError,UnicodeError): raise ApiError(400,'请求内容不是有效 JSON 对象。')

    def file(self,path,download=False):
        if not path.is_file() or path.is_symlink(): raise ApiError(404,'文件不存在。')
        total = path.stat().st_size
        start,end,status = 0,total-1,200
        value = self.headers.get('Range')
        if value:
            match = re.fullmatch(r'bytes=(\d*)-(\d*)',value.strip())
            if not match or not any(match.groups()) or total==0:
                self.response(416,{'error':'请求范围无效。'},{'Content-Range':f'bytes */{total}'})
                return
            left,right = match.groups()
            if not left:
                suffix = int(right)
                if suffix<=0:
                    self.response(416,{'error':'请求范围无效。'},{'Content-Range':f'bytes */{total}'})
                    return
                start = max(0,total-suffix)
            else:
                start = int(left)
                end = min(total-1,int(right)) if right else total-1
            if start>=total or end<start:
                self.response(416,{'error':'请求范围越界。'},{'Content-Range':f'bytes */{total}'})
                return
            status = 206
        mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        if path.suffix.lower()=='.mkv': mime='video/x-matroska'
        self.send_response(status)
        self.send_header('Content-Type',mime)
        self.send_header('Content-Length',str(max(0,end-start+1)))
        self.send_header('Accept-Ranges','bytes')
        self.send_header('Cache-Control','private, max-age=3600' if '/assets/' in self.path else 'no-cache')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'self'")
        if status==206: self.send_header('Content-Range',f'bytes {start}-{end}/{total}')
        if download: self.send_header('Content-Disposition',"attachment; filename*=UTF-8''"+urllib.parse.quote(path.name))
        self.end_headers()
        if self.command=='HEAD': return
        with open(path,'rb') as file:
            file.seek(start)
            left = end-start+1
            while left>0:
                chunk=file.read(min(512*1024,left))
                if not chunk: break
                self.wfile.write(chunk)
                left-=len(chunk)

    def upload(self,query):
        raw = urllib.parse.parse_qs(query).get('name',[''])[0]
        name = re.split(r'[/\\]',raw)[-1].strip().rstrip('.')
        name = re.sub(r'[\x00-\x1f<>:"|?*]','_',name)[:220]
        if not name or Path(name).suffix.lower() not in EXTENSIONS: raise ApiError(400,'请选择支持的视频文件。')
        if self.headers.get('Transfer-Encoding'): raise ApiError(411,'上传需要明确文件长度。')
        try: length=int(self.headers.get('Content-Length','0'))
        except ValueError: raise ApiError(400,'上传长度无效。')
        if length<=0: raise ApiError(400,'文件为空。')
        if length > shutil.disk_usage(self.lib.root).free-64*1024*1024: raise ApiError(507,'资料库磁盘空间不足，请先腾出空间。')
        key=uid()
        temp=self.lib.root/'tmp'/(key+'.upload')
        destination=self.lib.root/'media'/(key+'-'+name)
        self.connection.settimeout(120)
        try:
            with open(temp,'xb') as file:
                left=length
                while left:
                    chunk=self.rfile.read(min(left,1024*1024))
                    if not chunk: raise ApiError(400,'上传中断，未将不完整文件入库。')
                    file.write(chunk)
                    left-=len(chunk)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp,destination)
            try: video=self.lib.register(destination,owned=True,filename=name)
            except BaseException:
                destination.unlink(missing_ok=True)
                raise
            self.response(201,{'id':video['id'],'video':self.lib.public_video(video)})
        finally:
            temp.unlink(missing_ok=True)

    def dispatch(self):
        self.guard()
        parsed=urllib.parse.urlsplit(self.path)
        path=urllib.parse.unquote(parsed.path)
        parts=path.strip('/').split('/')
        method=self.command
        if method=='HEAD': method='GET'
        if method=='GET':
            query=urllib.parse.parse_qs(parsed.query)
            if path=='/api/state': return self.response(200,self.lib.state(local=self.local,compact=query.get('compact',['0'])[0]=='1'))
            if path=='/api/videos': return self.response(200,self.lib.list_videos(query))
            if path=='/api/model-tags': return self.response(200,self.lib.model_tag_facets(query))
            if path=='/api/audit':
                if not self.local: raise ApiError(403,'仅在资料库所在电脑查看操作记录。')
                return self.response(200,self.lib.audit.page(query))
            if parts[:2]==['api','videos'] and len(parts)==3:
                return self.response(200,{'video':self.lib.public_video(self.lib.require_video(parts[2]))})
            if parts[:2]==['api','videos'] and len(parts)==4 and parts[3]=='search-context':
                video=self.lib.public_video(self.lib.require_video(parts[2]))
                return self.response(200,{'title':video.get('title',''),'catalog_code':video.get('catalog_code',''),
                                          'cover_url':video.get('cover'),'frames':video.get('frames',[]),
                                          'metadata_source_url':video.get('metadata_source_url'),
                                          'exposure_notice':'此接口不会联网。网页搜索会把你确认的检索词交给目标网站；截图检索需要逐次确认具体图片与目标网站，再由你自行上传。'})
            if parts[:2]==['api','videos'] and len(parts)==5 and parts[3]=='provider-samples':
                content=self.lib.providers.sample_image(parts[2],parts[4],query.get('sha256',[''])[0])
                return self.response(200,content,{'Content-Type':'image/jpeg','Cache-Control':'no-store'})
            if path=='/api/settings': return self.response(200,self.lib.settings())
            if path=='/api/local-models':
                if not self.local: raise ApiError(403,'请在资料库本机管理离线模型。')
                return self.response(200,self.lib.local_models.describe())
            if path=='/api/analysis-engines': return self.response(200,self.lib.analysis.describe())
            if path=='/api/library-health': return self.response(200,self.lib.health.summary(query))
            if path=='/api/library-health/sources': return self.response(200,self.lib.health.sources(query))
            if path=='/api/library-health/folders': return self.response(200,self.lib.health.folders(query))
            if path=='/api/library-health/issues': return self.response(200,self.lib.health.issues(query))
            if path=='/api/media-capabilities':
                if not self.local: raise ApiError(403,'请在本机核对媒体引擎能力。')
                return self.response(200,self.lib.compatibility.capabilities())
            if path=='/api/providers':
                if not self.local: raise ApiError(403,'请在资料库本机管理模型服务。')
                return self.response(200,self.lib.providers.public())
            if path=='/api/cache':
                if not self.local: raise ApiError(403,'请在资料库本机管理缓存。')
                return self.response(200,self.lib.cache.inspect())
            if path=='/api/export':
                return self.response(200,{'app':'cinevault','version':VERSION,'exported_at':now(),'includes_originals':False,'videos':[self.lib.public_video(v) for v in self.lib.rows('videos')],'sources':self.lib.rows('sources')},{'Content-Disposition':'attachment; filename="cinevault-catalog.json"'})
            if parts[:1]==['media'] and len(parts) in (2,3):
                video=self.lib.require_video(parts[1])
                if len(parts)==2:
                    self.lib.check_source_device(video)
                    if video.get('owned') and not plain_descendant(self.lib.path(video),self.lib.root/'media'):
                        raise ApiError(409,'资料库媒体路径中出现链接，已停止读取。')
                media=self.lib.root/'assets'/video['id']/('compatible.mp4' if parts[2]=='compatible' else 'preview.mp4') if len(parts)==3 and parts[2] in ('compatible','preview') else self.lib.path(video) if len(parts)==2 else None
                if media is None: raise ApiError(404,'媒体接口不存在。')
                if len(parts)==3:
                    _,original=self.lib.playback.check(video['id'])
                    field='playback_source' if parts[2]=='compatible' else 'preview_source'
                    if (original is None or (video.get(field) and video[field]!=self.lib.playback.signature(video))
                        or (parts[2]=='preview' and not self.lib.playback.preview_is_current(video))
                        or not plain_descendant(media,self.lib.root/'assets')):
                        raise ApiError(409,'派生视频不对应当前已核对的原片，请重新准备。')
                    with self.lib.cache.reading(video['id']): return self.file(media)
                return self.file(media)
            if parts[:1]==['assets'] and len(parts)==3 and re.fullmatch('[a-f0-9]{32}',parts[1]) and re.fullmatch(r'(cover|sprite|frame\d{2})\.jpg',parts[2]):
                self.lib.require_video(parts[1])
                return self.file(self.lib.root/'assets'/parts[1]/parts[2])
            if parts[:1]==['backups'] and len(parts)==2 and Path(parts[1]).name==parts[1] and parts[1].endswith('.zip'):
                return self.file(self.lib.root/'backups'/parts[1],download=True)
            if path.startswith('/api/'): raise ApiError(404,'接口不存在。')
            if path=='/': return self.file(APP/'web'/'index.html')
            if path in ('/app.js','/experience.js','/style.css','/favicon.svg','/manifest.webmanifest'):
                return self.file(APP/'web'/path.lstrip('/'))
            raise ApiError(404,'页面不存在。')
        if method=='POST' and path=='/api/upload': return self.upload(parsed.query)
        body=self.body()
        if method=='POST' and path in ('/api/local-models/install','/api/local-models/cancel'):
            if not self.local: raise ApiError(403,'请在资料库本机管理离线模型。')
            return self.response(202,self.lib.local_models.install(body) if path.endswith('/install') else self.lib.local_models.cancel(body))
        if method=='POST' and path=='/api/library-health/previews':
            if not self.local: raise ApiError(403,'请在资料库本机安排目录预览任务。')
            return self.response(202,self.lib.health.prepare_previews(body))
        if path=='/api/analysis-engines' and method in ('POST','PATCH'):
            if not self.local: raise ApiError(403,'请在电脑本机管理分析引擎。')
            return self.response(200,self.lib.analysis.configure(body))
        if path in ('/api/providers','/api/providers/probe','/api/automation/batch','/api/cache/plan','/api/cache/cleanup') and method=='POST':
            if not self.local: raise ApiError(403,'请在资料库本机明确管理模型、批量任务与缓存。')
            if path=='/api/providers': return self.response(200,self.lib.providers.configure(body))
            if path=='/api/providers/probe': return self.response(200,self.lib.providers.probe(body))
            if path=='/api/automation/batch': return self.response(202,self.lib.automation.batch(body))
            if path=='/api/cache/plan': return self.response(200,self.lib.cache.plan(body))
            if path=='/api/cache/cleanup': return self.response(200,self.lib.cache.cleanup(body))
        if method=='POST':
            if path=='/api/login':
                remote=self.client_address[0]
                count,stamp=self.lib.login_attempts.get(remote,(0,0))
                if time.monotonic()-stamp>60: count=0
                if count>=15: raise ApiError(429,'尝试次数较多，请一分钟后重试。')
                self.lib.login_attempts[remote]=(count+1,time.monotonic())
                token=body.get('token','')
                if not isinstance(token,str) or not secrets.compare_digest(token,self.lib.token): raise ApiError(401,'访问口令不正确。')
                session=secrets.token_urlsafe(32)
                self.lib.sessions.add(session)
                if len(self.lib.sessions)>100: self.lib.sessions={session}
                return self.response(200,{'ok':True},{'Set-Cookie':f'cinevault_session={session}; Path=/; HttpOnly; SameSite=Strict; Max-Age=604800'})
            if path=='/api/sources':
                if not self.local: raise ApiError(403,'请在电脑本机管理原片文件夹。')
                return self.response(201,{'source':self.lib.add_source(body.get('path',''),body)})
            if path=='/api/sources/preview':
                if not self.local: raise ApiError(403,'请在电脑本机预览关联目录。')
                return self.response(200,self.lib.preview_source(body))
            if path=='/api/scan': return self.response(202,{'job_id':self.lib.enqueue('scan')})
            if path=='/api/health': return self.response(202,{'job_id':self.lib.enqueue('health',deep=bool(body.get('deep')))})
            if path=='/api/classify': return self.response(202,self.lib.classify_batch(body))
            if path=='/api/backup': return self.response(201,self.lib.backup())
            if path=='/api/shutdown':
                if not self.local: raise ApiError(403,'只允许电脑本机停止服务。')
                self.response(200,{'ok':True})
                threading.Thread(target=self.server.shutdown,daemon=True).start()
                return
            if path=='/api/settings':
                if not self.local: raise ApiError(403,'请在电脑本机修改设置。')
                with self.lib.db_lock:
                    settings=self.lib.settings()
                    before=dict(settings)
                    if 'classification_engine' in body:
                        if body['classification_engine'] not in CLASSIFICATION_ENGINES: raise ApiError(400,'未知的本地分类引擎。')
                        settings['classification_engine']=body['classification_engine']
                    if 'card_auto_preview' in body:
                        if not isinstance(body['card_auto_preview'],bool): raise ApiError(400,'卡片自动预览开关必须为布尔值。')
                        settings['card_auto_preview']=body['card_auto_preview']
                    if 'auto_classify_new_imports' in body:
                        if not isinstance(body['auto_classify_new_imports'],bool): raise ApiError(400,'新入库自动分类开关必须为布尔值。')
                        settings['auto_classify_new_imports']=body['auto_classify_new_imports']
                    for name in ('auto_timeline_new_imports','auto_preview_new_imports','auto_describe_new_imports','cache_auto_trim'):
                        if name in body:
                            if not isinstance(body[name],bool): raise ApiError(400,name+'必须为布尔值。')
                            if name=='auto_describe_new_imports' and body[name]:
                                provider=self.lib.providers.config()
                                if not provider.get('model') or not self.lib.providers.local_only(provider):
                                    reason=None
                                    if provider.get('kind')=='managed_vision':
                                        reason=(self.lib.providers.public().get('managed_model') or {}).get('reason')
                                    raise ApiError(409,reason or '请先启用随附视觉概述模型，或连接已就绪的本机视觉模型，再开启自动概述。')
                            settings[name]=body[name]
                    if 'cache_limit_gb' in body:
                        value=body['cache_limit_gb']
                        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not 1<=value<=512:
                            raise ApiError(400,'缓存软限应为1至512GiB。')
                        settings['cache_limit_gb']=value
                    if 'watch_enabled' in body:
                        if not isinstance(body['watch_enabled'],bool): raise ApiError(400,'定时扫描开关必须为布尔值。')
                        settings['watch_enabled']=body['watch_enabled']
                    if 'watch_interval' in body:
                        try: settings['watch_interval']=max(15,min(86400,int(body['watch_interval'])))
                        except (ValueError,TypeError): raise ApiError(400,'扫描间隔应为整数秒。')
                    if 'ollama_model' in body: settings['ollama_model']=str(body['ollama_model']).strip()[:200]
                    if settings!=before:
                        with self.lib.db() as db:
                            db.execute("UPDATE settings SET data=? WHERE id='settings'",(json.dumps(settings,ensure_ascii=False),))
                            self.lib.audit.settings_changed('settings_changed',before,settings,db=db)
                            self.lib.bump(db)
                    return self.response(200,settings)
        if parts[:2]==['api','videos'] and len(parts)>=3:
            key=parts[2]
            self.lib.require_video(key)
            if len(parts)==3:
                if method=='PATCH': return self.response(200,{'video':self.lib.edit(key,body)})
                if method=='DELETE':
                    if not self.local: raise ApiError(403,'请在资料库所在电脑明确确认删除。')
                    return self.response(200,self.lib.delete(key,body))
            if len(parts)==4 and method=='POST':
                action=parts[3]
                if action=='prepare-playback':
                    result=self.lib.playback.prepare(key,body)
                    return self.response(202 if result['status'] in ('queued','processing') else 200,result)
                if action=='preview':
                    result=self.lib.playback.prepare_preview(key,body)
                    return self.response(202 if result['status'] in ('queued','processing') else 200,result)
                if action=='external-play':
                    if not self.local: raise ApiError(403,'仅允许电脑本机明确打开原片播放器。')
                    return self.response(200,self.lib.playback.external_play(key))
                if action=='timeline': return self.response(202,{'job_id':self.lib.queue_timeline(key,body)})
                if action=='describe-preview': return self.response(200,self.lib.providers.preview(key,body))
                if action=='describe': return self.response(202,self.lib.providers.queue(key,body))
                if action=='classify':
                    engine=body.get('engine') or self.lib.settings().get('classification_engine','vit')
                    status=self.lib.classifier_status(engine)
                    if not status['available']: raise ApiError(503,status['reason'])
                    current=self.lib.require_video(key)
                    if current.get('status')!='ready' or not (current.get('has_cover') or current.get('cover_revision')):
                        raise ApiError(409,'请先完成媒体分析并生成封面，再执行分类。')
                    job=self.lib.enqueue('classify',key,engine=engine)
                    if not job: raise ApiError(429,'分类已在队列中，或后台队列已满，请稍后查看。')
                    return self.response(202,{'job_id':job,'engine':engine,'model':status['model'],'local_only':True,'status':'queued','manual_scope_preserved':current.get('scope_source')=='manual' or bool(current.get('scope_lock'))})
                if action in ('analyze','verify','transcode'):
                    current=self.lib.require_video(key)
                    if action=='transcode' and current.get('kind')=='image': raise ApiError(400,'图片不支持视频转码。')
                    return self.response(202,{'job_id':self.lib.enqueue(action,key,**({'deep':bool(body.get('deep'))} if action=='verify' else {}))})
                if action=='markers':
                    with self.lib.vlock(key):
                        video=self.lib.require_video(key)
                        if video.get('kind')=='image': raise ApiError(400,'图片不支持视频时间标记。')
                        try: timestamp=max(0,min(float(body.get('time',0)),video.get('duration',0)))
                        except (TypeError,ValueError): raise ApiError(400,'标记时间无效。')
                        marker={'id':uid(),'time':timestamp,'title':str(body.get('title') or '我的片段')[:200],'kind':'manual'}
                        self.lib.update(key,markers=video.get('markers',[])+[marker])
                    return self.response(201,{'marker':marker})
            if len(parts)==5 and parts[3]=='markers' and method=='DELETE':
                with self.lib.vlock(key):
                    video=self.lib.require_video(key)
                    self.lib.update(key,markers=[m for m in video.get('markers',[]) if m['id']!=parts[4]])
                return self.response(200,{'ok':True})
            if len(parts)==5 and parts[3:]==['timeline','cancel'] and method=='POST':
                return self.response(200,self.lib.cancel_timeline(key))
        if parts[:2]==['api','sources'] and len(parts)==3 and method=='PATCH':
            if not self.local: raise ApiError(403,'请在电脑本机管理目录。')
            return self.response(200,{'source':self.lib.relink_source(parts[2],body.get('path'),body)})
        if parts[:2]==['api','sources'] and len(parts)==4 and parts[3]=='reattach-preview' and method=='POST':
            if not self.local: raise ApiError(403,'请在电脑本机核验来源目录。')
            return self.response(200,self.lib.reattach_preview(parts[2],body))
        raise ApiError(404,'接口不存在。')

    def handle_request(self):
        try: self.dispatch()
        except ApiError as exc:
            self.close_connection=True
            self.response(exc.status,{'error':str(exc)})
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError,TimeoutError):
            self.close_connection=True
        except (ValueError,TypeError,OverflowError) as exc:
            self.close_connection=True
            self.response(400,{'error':'请求参数无效：'+str(exc)[:300]})
        except Exception as exc:
            self.close_connection=True
            traceback.print_exc()
            self.response(500,{'error':'处理失败：'+str(exc)[:1000]})

    do_GET=handle_request
    do_HEAD=handle_request
    do_POST=handle_request
    do_PATCH=handle_request
    do_DELETE=handle_request


def main():
    parser=argparse.ArgumentParser(description='映库 CineVault 本地视频资料库')
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--data-dir',type=Path,default=APP/'data')
    parser.add_argument('--lan',action='store_true',help='允许局域网设备凭口令访问')
    args=parser.parse_args()
    if not 1<=args.port<=65535: parser.error('端口应在1到65535之间')
    library=Library(args.data_dir,args.port,args.lan)
    server=Server(('0.0.0.0' if args.lan else '127.0.0.1',args.port),Handler)
    server.library=library
    print(f'CineVault {VERSION} — http://127.0.0.1:{args.port}',flush=True)
    print(f'Library: {library.root}',flush=True)
    if args.lan:
        print('LAN access enabled. Pairing token: '+library.token,flush=True)
        for address in library.state(local=True)['system'].get('lan_urls',[]): print('Mobile: '+address,flush=True)
    try: server.serve_forever(poll_interval=.3)
    except KeyboardInterrupt: pass
    finally:
        library.close()
        server.server_close()


if __name__=='__main__':
    main()
