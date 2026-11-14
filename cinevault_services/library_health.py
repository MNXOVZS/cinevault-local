"""SQL-backed source health and durable, bounded preview preparation."""
from __future__ import annotations

import datetime
import json
import math
from pathlib import Path, PurePosixPath
import re
import statistics
import time


MEDIA_KEYS=('total','videos','images','ready','analyzing','failed','missing','changed','needs_attention','playback_failed','no_cover')
PREVIEW_KEYS=('total','ready','queued','processing','pending','failed','unprepared','blocked','stale')
NOTICE='基于资料库记录和最近扫描；打开此页不会读取原片或执行完整解码。已探测格式不等于已验证浏览器播放。'
FOLDER="CASE WHEN length(coalesce(json_extract(data,'$.relative_path'),''))>length(filename) THEN substr(replace(json_extract(data,'$.relative_path'),'\\','/'),1,length(json_extract(data,'$.relative_path'))-length(filename)-1) ELSE '' END"
OFFLINE="source_id IN (SELECT id FROM sources WHERE coalesce(json_extract(data,'$.scan.status'),'') IN ('offline','device_changed'))"


class LibraryHealthService:
    def __init__(self,library,error,now):
        self.lib,self.error,self.now=library,error,now

    @staticmethod
    def first(query,name,default=''):
        value=query.get(name,default)
        return value[0] if isinstance(value,list) and value else value

    def scope(self,query):
        source=self.first(query,'source_id')
        if source in (None,'','all'): source=None
        if source is not None and source!='owned':
            if not isinstance(source,str) or not re.fullmatch('[a-f0-9]{32}',source): raise self.error(400,'来源ID无效。')
            with self.lib.db() as db:
                if not db.execute('SELECT 1 FROM sources WHERE id=?',(source,)).fetchone(): raise self.error(404,'来源目录不存在。')
        folder=self.first(query,'folder','')
        if not isinstance(folder,str) or len(folder)>2000 or '\\' in folder or folder.startswith('/') or any(ord(c)<32 for c in folder):
            raise self.error(400,'文件夹应为来源内的相对路径。')
        folder=folder.strip('/')
        if folder and (any(part in ('','..','.') for part in folder.split('/')) or ':' in folder):
            raise self.error(400,'文件夹不能包含上级或绝对路径。')
        recursive=self.first(query,'recursive',True)
        if recursive in ('1','true',True): recursive=True
        elif recursive in ('0','false',False): recursive=False
        else: raise self.error(400,'recursive必须为布尔值或0/1。')
        return {'source_id':source,'folder':folder,'recursive':recursive}

    def page(self,query):
        try:
            limit=int(self.first(query,'limit',50));offset=int(self.first(query,'offset',0))
            if not 1<=limit<=120 or offset<0: raise ValueError()
        except (ValueError,TypeError): raise self.error(400,'分页应为limit 1—120、offset非负整数。')
        return limit,offset

    @staticmethod
    def where(scope,prefix=''):
        clauses=[];values=[]
        if scope['source_id']=='owned': clauses.append(prefix+'owned=1')
        elif scope['source_id']:
            clauses.append(prefix+'source_id=?');values.append(scope['source_id'])
        folder=scope['folder']
        if folder or not scope['recursive']:
            if scope['recursive']:
                clauses.append('('+prefix+'folder=? OR '+prefix+"folder LIKE ? ESCAPE '\\')")
                values.extend((folder,folder.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'/%'))
            else: clauses.append(prefix+'folder=?');values.append(folder)
        return (' AND '.join(clauses) if clauses else '1'),values

    def cte(self):
        version=self.lib.playback.preview_target_version
        # Parameterized version strings are trusted app constants, not user SQL.
        quoted="'"+version.replace("'","''")+"'"
        current=("coalesce(json_extract(data,'$.has_preview'),0)=1 AND json_extract(data,'$.preview_version')="+quoted+
                 " AND json_extract(data,'$.preview_source.sha256') IS json_extract(data,'$.sha256')"
                 " AND json_extract(data,'$.preview_source.size') IS json_extract(data,'$.size')"
                 " AND json_extract(data,'$.preview_source.mtime_ns') IS json_extract(data,'$.mtime_ns')")
        bucket=("CASE WHEN kind<>'video' THEN 'not_video' WHEN status<>'ready' OR "+OFFLINE+" THEN 'blocked' "
                "WHEN coalesce(json_extract(data,'$.preview_status'),'')='processing' THEN 'processing' "
                "WHEN coalesce(json_extract(data,'$.preview_status'),'')='queued' THEN 'queued' "
                "WHEN coalesce(json_extract(data,'$.preview_check_pending'),0)=1 THEN 'pending' "
                "WHEN "+current+" THEN 'ready' "
                "WHEN coalesce(json_extract(data,'$.preview_batch_pending'),0)=1 OR coalesce(json_extract(data,'$.auto_preview_pending'),0)=1 THEN 'pending' "
                "WHEN json_extract(data,'$.preview_status')='error' AND json_extract(data,'$.preview_attempt_version')="+quoted+" THEN 'failed' "
                "ELSE 'unprepared' END")
        return ('WITH health AS (SELECT videos.*,'+FOLDER+' AS folder,'+bucket+' AS preview_bucket,'+
                "CASE WHEN kind='video' AND coalesce(json_extract(data,'$.has_preview'),0)=1 AND NOT ("+current+") THEN 1 ELSE 0 END AS stale FROM videos) ")

    @staticmethod
    def aggregates():
        media=["count(*)","sum(kind='video')","sum(kind='image')","sum(status='ready')",
               "sum(status IN ('queued','analyzing'))","sum(status='error')","sum(status='missing')","sum(status='changed')",
               "sum(status IN ('error','missing','changed') OR preview_bucket='failed' OR coalesce(json_extract(data,'$.playback_status'),'')='error' OR "+OFFLINE+")",
               "sum(coalesce(json_extract(data,'$.playback_status'),'')='error')","sum(coalesce(json_extract(data,'$.has_cover'),0)=0)"]
        preview=["sum(kind='video')"]+["sum(preview_bucket='"+name+"')" for name in PREVIEW_KEYS[1:-1]]+["sum(stale=1 AND preview_bucket='unprepared')"]
        return ','.join('coalesce('+part+',0)' for part in media+preview)

    @staticmethod
    def counts(row):
        values=list(row)
        media=dict(zip(MEDIA_KEYS,values[:len(MEDIA_KEYS)]))
        preview=dict(zip(PREVIEW_KEYS,values[len(MEDIA_KEYS):]))
        preview['completion_ratio']=round(preview['ready']/preview['total'],6) if preview['total'] else 1
        return media,preview

    def count_scope(self,scope,db=None):
        where,params=self.where(scope)
        if db is None:
            with self.lib.db() as connection: return self.count_scope(scope,connection)
        return self.counts(db.execute(self.cte()+'SELECT '+self.aggregates()+' FROM health WHERE '+where,params).fetchone())

    @staticmethod
    def elapsed(job):
        if job.get('started_at'):
            try:
                stamp=datetime.datetime.fromisoformat(job['started_at'])
                if stamp.tzinfo is None: stamp=stamp.replace(tzinfo=datetime.timezone.utc)
                return max(0,round((datetime.datetime.now(datetime.timezone.utc)-stamp).total_seconds(),1))
            except (TypeError,ValueError): pass
        return 0

    def queue_context(self,scope,preview):
        with self.lib.db() as db:
            jobs=[json.loads(row[0]) for row in db.execute("SELECT data FROM jobs WHERE json_extract(data,'$.status') IN ('queued','running') ORDER BY json_extract(data,'$.created_at'),id")]
            samples=[json.loads(row[0]) for row in db.execute("SELECT data FROM jobs WHERE json_extract(data,'$.kind')='preview' AND json_extract(data,'$.status')='complete' AND coalesce(json_extract(data,'$.skipped'),0)=0 ORDER BY json_extract(data,'$.finished_at') DESC LIMIT 40")]
            ids={job.get('video_id') for job in jobs if job.get('video_id')}
            videos={}
            if ids:
                videos={row[0]:{'title':row[1],'source_id':row[2],'owned':row[3],'folder':row[4]} for row in db.execute('SELECT id,title,source_id,owned,'+FOLDER+' FROM videos WHERE id IN ('+','.join('?' for _ in ids)+')',list(ids))}
        def in_scope(video):
            source=scope['source_id'];folder=scope['folder']
            if source=='owned' and not video.get('owned'): return False
            if source not in (None,'owned') and video.get('source_id')!=source: return False
            actual=video.get('folder','')
            return not folder and scope['recursive'] or actual==folder or bool(scope['recursive'] and folder and actual.startswith(folder+'/'))
        running=next((job for job in jobs if job['status']=='running' and job.get('kind')!='scan'),None)
        current=None
        if running:
            video=videos.get(running.get('video_id'),{})
            current={'kind':running['kind'],'video_id':running.get('video_id'),'title':video.get('title',''),
                     'started_at':running.get('started_at'),'elapsed_seconds':self.elapsed(running),'in_scope':in_scope(video)}
        durations=[float(job['elapsed_seconds']) for job in samples if job.get('profile')==self.lib.playback.preview_target_version
                   and isinstance(job.get('elapsed_seconds'),(int,float)) and 0<job['elapsed_seconds']<=180]
        durations=durations[:20]
        median=statistics.median(durations) if durations else None
        other_jobs=sum(job['kind'] not in ('preview','scan') for job in jobs)
        remaining=preview['queued']+preview['processing']+preview['pending']
        eta={'seconds':None,'lower_seconds':None,'upper_seconds':None,'sample_count':len(durations),
             'median_seconds':round(median,3) if median is not None else None,'estimated':True}
        if preview['blocked']: reason='部分原片仍待基础分析、离线或需要校验，无法可靠估计全部预览完成时间。'
        elif preview['unprepared']: reason='部分预览尚未安排。可先为此范围准备缺失预览，再查看预计时间。'
        elif not remaining: reason='此范围没有正在等待的预览任务。'
        elif other_jobs: reason='共享串行队列中还有基础分析、校验或模型等任务，完成时间暂无法可靠估计。'
        elif len(durations)<3: reason='当前版本完成样本不足3个，暂不估时。'
        else:
            ahead=sum(job['kind']=='preview' and not in_scope(videos.get(job.get('video_id'),{})) for job in jobs)
            seconds=median*(remaining+ahead)
            eta.update(seconds=max(1,math.ceil(seconds)),lower_seconds=max(1,math.ceil(seconds*.5)),upper_seconds=max(1,math.ceil(seconds*2)))
            reason='按当前版本最近完成预览的中位耗时和共享预览队列估计；包含范围外排队预览，慢盘及旧编码可能明显更久。'
        eta['reason']=reason
        return current,{'queued':sum(j['status']=='queued' for j in jobs),'running':sum(j['status']=='running' for j in jobs),'other_jobs':other_jobs},eta

    def summary(self,query):
        scope=self.scope(query)
        with self.lib.db() as db:
            media,preview=self.count_scope(scope,db);revision=self.lib.revision(db)
        current,shared,eta=self.queue_context(scope,preview)
        return {'scope':scope,'media':media,'preview':preview,'eta':eta,'current':current,
                'shared_queue':shared,'revision':revision,'basis':'catalogue_records','notice':NOTICE}

    @staticmethod
    def source_status(source):
        state=(source.get('scan') or {}).get('status')
        if state=='offline': return 'offline'
        if source.get('error') or state in ('device_changed','partial'): return 'attention'
        return 'available' if source.get('last_scan') or state=='complete' else 'unscanned'

    def sources(self,query):
        limit,offset=self.page(query)
        sources=[{'id':'owned','path':'','owned':True}]+self.lib.rows('sources')
        result=[]
        with self.lib.db() as db:
            rows=db.execute(self.cte()+"SELECT CASE WHEN owned=1 THEN 'owned' ELSE source_id END,"+self.aggregates()+" FROM health GROUP BY CASE WHEN owned=1 THEN 'owned' ELSE source_id END").fetchall()
            grouped={row[0]:self.counts(row[1:]) for row in rows}
            revision=self.lib.revision(db)
        for source in sources[offset:offset+limit]:
            zero=([0]*(len(MEDIA_KEYS)+len(PREVIEW_KEYS)))
            media,preview=grouped.get(source['id'],self.counts(zero))
            result.append({'id':source['id'],'label':'库内副本' if source.get('owned') else Path(source['path']).name or source['path'],
                'path':source['path'],'source_status':'available' if source.get('owned') else self.source_status(source),
                'error':source.get('error'),'last_scan':source.get('last_scan'),'scan':source.get('scan'),
                'media':media,'preview':preview})
        return {'items':result,'total':len(sources),'limit':limit,'offset':offset,'revision':revision,'basis':'catalogue_records','notice':NOTICE}

    def folders(self,query):
        scope=self.scope(query);limit,offset=self.page(query);where,params=self.where(scope)
        with self.lib.db() as db:
            total=db.execute(self.cte()+'SELECT count(DISTINCT folder) FROM health WHERE '+where,params).fetchone()[0]
            rows=db.execute(self.cte()+'SELECT folder,'+self.aggregates()+' FROM health WHERE '+where+' GROUP BY folder ORDER BY folder LIMIT ? OFFSET ?',params+[limit,offset]).fetchall()
            revision=self.lib.revision(db)
        items=[]
        for row in rows:
            media,preview=self.counts(row[1:]);items.append({'folder':row[0],'label':row[0] or '根目录','media':media,'preview':preview})
        return {'scope':scope,'items':items,'total':total,'limit':limit,'offset':offset,'revision':revision,'notice':NOTICE}

    def issues(self,query):
        scope=self.scope(query);limit,offset=self.page(query);where,params=self.where(scope)
        kind=self.first(query,'kind','all')
        predicates={'all':"status IN ('error','missing','changed') OR preview_bucket='failed' OR coalesce(json_extract(data,'$.playback_status'),'')='error' OR "+OFFLINE,
                    'analysis':"status<>'ready'",'preview':"kind='video' AND preview_bucket<>'ready'",
                    'playback':"kind='video' AND (coalesce(json_extract(data,'$.playback_status'),'')='error' OR coalesce(json_extract(data,'$.playback_status'),'unprepared')='unprepared')",
                    'source':"status IN ('missing','changed') OR "+OFFLINE}
        if kind not in predicates: raise self.error(400,'问题类型应为all/analysis/preview/playback/source。')
        where+=' AND ('+predicates[kind]+')'
        with self.lib.db() as db:
            total=db.execute(self.cte()+'SELECT count(*) FROM health WHERE '+where,params).fetchone()[0]
            rows=db.execute(self.cte()+'SELECT data,folder,preview_bucket FROM health WHERE '+where+' ORDER BY created_at,id LIMIT ? OFFSET ?',params+[limit,offset]).fetchall()
            revision=self.lib.revision(db)
        items=[]
        sources={s['id']:s for s in self.lib.rows('sources')}
        for raw,folder,bucket in rows:
            video=json.loads(raw);status=video.get('status','queued')
            source=sources.get(video.get('source_id'),{})
            blocked=(source.get('scan') or {}).get('status') in ('offline','device_changed')
            codes=[]
            if status!='ready': codes.append('analysis_'+status)
            if bucket!='ready' and video.get('kind','video')=='video': codes.append('preview_'+bucket)
            if video.get('playback_status')=='error': codes.append('playback_failed')
            if blocked: codes.append('source_offline_or_changed')
            diagnostic=self.lib.compatibility.explain(video)
            if blocked:
                diagnostic=dict(diagnostic,category='source_offline',severity='warning',title='来源离线或设备身份需要核对',
                    explanation='此状态来自最近一次来源扫描；保留片目与已有封面，不表示文件已永久丢失。',
                    recommendation='接回原盘，核对挂载路径与磁盘身份后重新扫描；不要反复导入或安装视频插件。')
            is_video=video.get('kind','video')=='video'
            items.append({'id':video['id'],'title':video.get('title',''),'filename':video.get('filename',''),
                'relative_folder':folder,'source_id':video.get('source_id'),'status':status,
                'preview_status':bucket,'playback_status':video.get('playback_status','unprepared'),
                'codec':video.get('codec'),'container':video.get('container'),'issue_codes':codes,'diagnostic':diagnostic,
                'error':video.get('error'),'preview_error':video.get('preview_error'),'playback_error':video.get('playback_error'),
                'actions':{'retry_analysis':not blocked and status=='error','prepare_preview':is_video and status=='ready' and not blocked and bucket in ('unprepared','failed'),
                           'retry_preview':is_video and status=='ready' and not blocked and bucket=='failed',
                           'prepare_playback':is_video and status=='ready' and not blocked,'verify':not blocked and status in ('missing','changed','ready')}})
        return {'scope':scope,'kind':kind,'items':items,'total':total,'limit':limit,'offset':offset,'revision':revision,'notice':NOTICE}

    def prepare_previews(self,body):
        if not isinstance(body,dict): raise self.error(400,'批量预览参数必须为对象。')
        scope=self.scope(body);retry=body.get('retry_failed',False)
        if not isinstance(retry,bool): raise self.error(400,'retry_failed必须为布尔值。')
        where,params=self.where(scope)
        kinds="('unprepared','failed')" if retry else "('unprepared')"
        with self.lib.db() as db:
            # Keep every selected record durable without creating unbounded jobs.
            selected=db.execute(self.cte()+'SELECT count(*) FROM health WHERE '+where+' AND preview_bucket IN '+kinds,params).fetchone()[0]
            checked=db.execute(self.cte()+"SELECT count(*) FROM health WHERE "+where+" AND preview_bucket='ready'",params).fetchone()[0]
            if checked:
                db.execute("UPDATE videos SET data=json_set(data,'$.preview_check_pending',json('true')) WHERE id IN ("+
                    self.cte()+"SELECT id FROM health WHERE "+where+" AND preview_bucket='ready')",params)
            if selected:
                db.execute("UPDATE videos SET data=json_set(data,'$.preview_batch_pending',json('true'),'$.preview_batch_retry',json(?),'$.preview_batch_automatic',json('false'),'$.preview_batch_profile',?) WHERE id IN ("+
                    self.cte()+'SELECT id FROM health WHERE '+where+' AND preview_bucket IN '+kinds+')', ['true' if retry else 'false',self.lib.playback.preview_target_version]+params)
            if selected or checked: self.lib.bump(db)
        self.schedule_pending()
        summary=self.summary(scope)
        return {'selected':selected,'cache_checks_selected':checked,'queued':summary['preview']['queued']+summary['preview']['processing'],
                'pending':summary['preview']['pending'],'skipped':summary['preview']['blocked'],
                'notice':'已为此范围安排缺失或旧版本预览，并分批复核已登记缓存是否存在；共享重任务最多128项，其余持久保留待排，不会无限重试失败。原片不变。'}

    def schedule_pending(self):
        if self.lib.stop.is_set(): return
        # Only an explicit scope request marks these flags. Ordinary progress
        # polling never stats originals or scans every cache directory.
        with self.lib.db() as db:
            checks=[json.loads(row[0]) for row in db.execute("SELECT data FROM videos WHERE kind='video' AND status='ready' AND coalesce(json_extract(data,'$.preview_check_pending'),0)=1 ORDER BY id LIMIT 64")]
        for video in checks:
            key=video['id']
            lock=self.lib.vlock(key)
            if not lock.acquire(blocking=False): continue
            try:
                with self.lib.jobs_lock: busy=any(ident==key for _,ident in self.lib.active)
                if busy: continue
                current=self.lib.video(key)
                if not current: continue
                path=self.lib.root/'assets'/key/'preview.mp4'
                present=self.lib.playback.safe_path(path,self.lib.root/'assets') and path.is_file() and self.lib.playback.preview_is_current(current)
                changes={'preview_check_pending':False}
                if not present:
                    changes.update(has_preview=False,preview_status='unavailable',preview_info=None,preview_error=None,
                        preview_version=None,preview_source=None,
                        preview_batch_pending=True,preview_batch_automatic=False,preview_batch_retry=False,
                        preview_batch_profile=self.lib.playback.preview_target_version)
                self.lib.update(key,**changes)
            finally: lock.release()
        with self.lib.jobs_lock:
            room=max(0,128-len([kind for kind,_ in self.lib.active if kind in ('analyze','verify','classify','timeline','transcode','preview','describe')]))
            active=set(self.lib.active)
        if not room or self.lib.stop.is_set(): return
        with self.lib.db() as db:
            rows=[json.loads(row[0]) for row in db.execute("SELECT data FROM videos WHERE kind='video' AND status='ready' AND coalesce(json_extract(data,'$.preview_batch_pending'),0)=1 ORDER BY created_at,id LIMIT ?",(min(64,room)+len(active),))]
        for video in rows:
            key=video['id']
            if any(ident==key for _,ident in active): continue
            automatic=bool(video.get('preview_batch_automatic'))
            settings=self.lib.settings()
            if automatic and not (settings.get('auto_preview_new_imports') and settings.get('card_auto_preview')):
                self.lib.update(key,preview_batch_pending=False,preview_status='unavailable',preview_error='自动预览已关闭，未运行。')
                continue
            if self.lib.playback.preview_is_current(video):
                self.lib.update(key,preview_batch_pending=False)
                continue
            if (video.get('preview_status')=='error' and not video.get('preview_batch_retry')
                and video.get('preview_attempt_version')==self.lib.playback.preview_target_version):
                self.lib.update(key,preview_batch_pending=False)
                continue
            with self.lib.db_lock:
                job=self.lib.enqueue('preview',key,automatic=automatic)
                if not job: continue
                self.lib.update(key,preview_batch_pending=False,preview_batch_retry=False,preview_status='queued',
                    preview_attempt_version=self.lib.playback.preview_target_version,preview_job_id=job,preview_error=None)
                self.lib.automation.record(key,'preview',status='queued',job_id=job,automatic=automatic,error=None,queued_at=self.now())
            active.add(('preview',key));room-=1
            if not room: break
