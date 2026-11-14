"""Durable local history for catalogue writes and observed source changes."""
from __future__ import annotations

import json
import re
import urllib.parse


SOURCE_ID = re.compile(r'[a-f0-9]{32}')
SCAN_RESULTS = {'complete', 'partial', 'offline', 'device_changed'}
SETTINGS_FIELDS = frozenset({
    'watch_enabled', 'watch_interval', 'classification_engine', 'card_auto_preview',
    'auto_classify_new_imports', 'auto_timeline_new_imports', 'auto_preview_new_imports',
    'auto_describe_new_imports', 'cache_auto_trim', 'cache_limit_gb',
    'kind', 'base_url', 'model', 'local_inference_only', 'enabled',
    'ollama_model', 'timeout_seconds', 'max_images', 'has_api_key',
})


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


class CatalogueAudit:
    def __init__(self, library, error, now):
        self.lib, self.error, self.now = library, error, now

    def initialize(self):
        with self.lib.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS audit_meta(key TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                    kind TEXT NOT NULL, source_id TEXT, video_id TEXT, detail TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS audit_source_id ON audit_events(source_id,id DESC);
                CREATE INDEX IF NOT EXISTS audit_kind ON audit_events(kind);
                CREATE TABLE IF NOT EXISTS audit_sources(
                    source_id TEXT PRIMARY KEY, baseline TEXT, latest TEXT, last_event_day TEXT
                );
            ''')
            if not db.execute("SELECT 1 FROM audit_meta WHERE key='baseline'").fetchone():
                stamp = self.now()
                totals = self.catalogue(db)
                db.execute("INSERT INTO audit_meta VALUES('baseline',?)", (dump({'at': stamp, **totals}),))
                for source_id, videos, images in db.execute('''SELECT sources.id,
                    coalesce(sum(videos.kind='video'),0),coalesce(sum(videos.kind='image'),0)
                    FROM sources LEFT JOIN videos ON videos.source_id=sources.id GROUP BY sources.id'''):
                    baseline = {'at': stamp, 'kind': 'migration', 'total': videos + images,
                                'videos': videos, 'images': images}
                    db.execute('INSERT INTO audit_sources(source_id,baseline) VALUES(?,?)', (source_id,dump(baseline)))
                self.record('ledger_started', {'catalogue': totals, 'basis': 'catalogue_at_upgrade',
                    'originals_modified': False}, db=db)
            db.execute('''CREATE TRIGGER IF NOT EXISTS audit_catalogue_add AFTER INSERT ON videos BEGIN
                INSERT INTO audit_events(created_at,kind,source_id,video_id,detail)
                VALUES(strftime('%Y-%m-%dT%H:%M:%SZ','now'),'catalogue_added',new.source_id,new.id,
                    json_object('filename',new.filename,'media_kind',new.kind,
                                'mode',CASE new.owned WHEN 0 THEN 'linked' ELSE 'copied' END,
                                'originals_modified',json('false')));
                END''')

    def record(self, kind, detail, *, db=None, source_id=None, video_id=None):
        if db is None:
            with self.lib.db() as connection:
                return self.record(kind, detail, db=connection, source_id=source_id, video_id=video_id)
        cursor = db.execute('INSERT INTO audit_events(created_at,kind,source_id,video_id,detail) VALUES(?,?,?,?,?)',
                           (self.now(),kind,source_id,video_id,dump(detail)))
        needs_attention = kind in ('source_changed','settings_changed','provider_changed','original_delete_failed')
        if kind == 'source_scan':
            needs_attention = (detail.get('status') in ('offline','device_changed','partial')
                               or bool(detail.get('missing_detected') or detail.get('changed_detected')))
        if needs_attention:
            db.execute('''INSERT INTO audit_meta VALUES('attention',?)
                ON CONFLICT(key) DO UPDATE SET data=excluded.data''',
                (dump({'id':cursor.lastrowid,'kind':kind}),))
        return cursor.lastrowid

    @staticmethod
    def attention(db):
        row=db.execute("SELECT data FROM audit_meta WHERE key='attention'").fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def catalogue(db):
        total, videos, images, copied = db.execute('''SELECT count(*),coalesce(sum(kind='video'),0),
            coalesce(sum(kind='image'),0),coalesce(sum(owned<>0),0) FROM videos''').fetchone()
        return {'total':total,'videos':videos,'images':images,'linked':total-copied,'copied':copied}

    def scan_result(self, db, source, scan):
        status = scan.get('status')
        if status not in SCAN_RESULTS:
            return
        key, stamp = source['id'], scan.get('finished_at') or self.now()
        row = db.execute('SELECT baseline,latest,last_event_day FROM audit_sources WHERE source_id=?',(key,)).fetchone()
        baseline = json.loads(row[0]) if row and row[0] else None
        previous = json.loads(row[1]) if row and row[1] else None
        last_event_day = row[2] if row else None
        counts = {name:int(scan.get(name,0)) for name in (
            'discovered','seen_videos','seen_images','inserted','missing_detected','changed_detected',
            'returned_detected','excluded','skipped','errors','unsupported_files','ignored_dirs')}
        current = {'at':stamp,'status':status,**counts}
        if status in ('offline','device_changed'):
            for name in ('discovered','seen_videos','seen_images'):
                current[name] = None
        first_complete = not baseline and status == 'complete'
        if first_complete:
            baseline = {'at':stamp,'kind':'first_scan','total':counts['discovered'],
                        'videos':counts['seen_videos'],'images':counts['seen_images']}
        changes = any(current[name] != (previous or {}).get(name) for name in (
            'status','discovered','seen_videos','seen_images','excluded','errors','unsupported_files','ignored_dirs'))
        detected = any(counts[name] for name in ('inserted','missing_detected','changed_detected','returned_detected'))
        if first_complete or changes or detected or last_event_day != stamp[:10]:
            self.record('source_scan', {**current,'path':source['path'],'initial_complete_scan':first_complete,
                'originals_modified':False}, db=db, source_id=key)
            last_event_day = stamp[:10]
        db.execute('''INSERT INTO audit_sources(source_id,baseline,latest,last_event_day) VALUES(?,?,?,?)
            ON CONFLICT(source_id) DO UPDATE SET baseline=excluded.baseline,latest=excluded.latest,
            last_event_day=excluded.last_event_day''',
            (key,dump(baseline) if baseline is not None else None,dump(current),last_event_day))

    @staticmethod
    def _setting_value(name, value):
        if name == 'base_url' and isinstance(value,str):
            try:
                parsed = urllib.parse.urlsplit(value)
                hostname = parsed.hostname
                if not hostname or parsed.scheme not in ('http','https'):
                    return '(地址已配置)'
                host = f'[{hostname}]' if ':' in hostname else hostname
                port = f':{parsed.port}' if parsed.port is not None else ''
                return f'{parsed.scheme}://{host}{port}'
            except ValueError:
                return '(地址已配置)'
        if value is None or isinstance(value,(bool,int,float)):
            return value
        return str(value)[:150]

    def settings_changed(self, kind, before, after, keys=None, *, db=None):
        fields = SETTINGS_FIELDS.intersection(keys if keys is not None else after)
        changes = {name:{'before':self._setting_value(name,before.get(name)),
                         'after':self._setting_value(name,after.get(name))}
                   for name in sorted(fields) if before.get(name) != after.get(name)}
        if changes:
            self.record(kind, {'changes':changes,'originals_modified':False}, db=db)

    def page(self, query):
        def one(name, default=''):
            values = query.get(name, [default])
            return values[0] if values else default

        try:
            limit = min(100,max(1,int(one('limit','50'))))
            before = int(one('before','0'))
            if not 0 <= before < 2**63:
                raise ValueError
        except (TypeError,ValueError):
            raise self.error(400,'操作记录分页参数无效。')
        source_id = one('source_id')
        if source_id and not SOURCE_ID.fullmatch(source_id):
            raise self.error(400,'来源编号无效。')
        terms, params = [], []
        if before:
            terms.append('id<?'); params.append(before)
        if source_id:
            terms.append('source_id=?'); params.append(source_id)
        where = (' WHERE '+' AND '.join(terms)) if terms else ''
        with self.lib.db() as db:
            rows = list(db.execute('SELECT id,created_at,kind,source_id,video_id,detail FROM audit_events'+where+
                                   ' ORDER BY id DESC LIMIT ?', params+[limit+1]))
            baseline = json.loads(db.execute("SELECT data FROM audit_meta WHERE key='baseline'").fetchone()[0])
            totals = self.catalogue(db)
            mutations = dict(db.execute("SELECT kind,count(*) FROM audit_events WHERE kind IN ('catalogue_added','catalogue_removed') GROUP BY kind"))
            counts = {key:dict(zip(('total','videos','images','missing','changed'),row))
                for key,*row in db.execute('''SELECT source_id,count(*),coalesce(sum(kind='video'),0),
                    coalesce(sum(kind='image'),0),coalesce(sum(status='missing'),0),coalesce(sum(status='changed'),0)
                    FROM videos WHERE source_id IS NOT NULL GROUP BY source_id''')}
            sources = []
            for raw, initial, latest in db.execute('''SELECT sources.data,audit_sources.baseline,audit_sources.latest
                FROM sources LEFT JOIN audit_sources ON sources.id=audit_sources.source_id ORDER BY sources.id'''):
                source = json.loads(raw)
                sources.append({'id':source['id'],'path':source['path'],'allow_delete':bool(source.get('allow_delete')),
                    'baseline':json.loads(initial) if initial else None,'latest':json.loads(latest) if latest else None,
                    'scan_status':(source.get('scan') or {}).get('status','idle'),
                    'catalogue':counts.get(source['id'],dict.fromkeys(('total','videos','images','missing','changed'),0))})
        keys = ('id','created_at','kind','source_id','video_id','detail')
        events = [dict(zip(keys,(*row[:-1],json.loads(row[-1])))) for row in rows[:limit]]
        added, removed = mutations.get('catalogue_added',0), mutations.get('catalogue_removed',0)
        expected = baseline['total'] + added - removed
        return {'events':events,'next_before':events[-1]['id'] if len(rows)>limit else None,
            'ledger_started_at':baseline['at'],'catalogue':totals,'sources':sources,
            'reconciliation':{'baseline':baseline['total'],'added':added,'removed':removed,
                'expected':expected,'actual':totals['total'],'difference':totals['total']-expected}}
