"""New-record-only, one-attempt automation and explicit bounded local batches."""
from __future__ import annotations

import json
import re

VERSIONS={'timeline':'objective-regions-v2','describe':'generic-summary-v1','classify':'nsfw-classifier-v1','preview':'hover-v2-480x270-24fps'}


class AutomationService:
    def __init__(self,library,error,now):
        self.lib,self.error,self.now=library,error,now

    def record(self,key,task,**changes):
        with self.lib.db_lock:
            video=self.lib.video(key)
            if not video: return
            automation=dict(video.get('automation') or {})
            automation[task]=dict(automation.get(task) or {},version=VERSIONS[task],**changes)
            self.lib.update(key,automation=automation)

    def queue(self,key,task,automatic=False):
        if task=='timeline':
            return self.lib.analysis.queue(key,{'automatic':automatic})
        elif task=='describe':
            if not self.lib.providers.local_only(self.lib.providers.config()): raise self.error(409,'批量/自动描述仅允许已声明仅本机推理的服务；远程需逐项预览和确认。')
            return self.lib.providers.queue(key,automatic=automatic)['job_id']
        elif task=='preview':
            with self.lib.db_lock:
                video=self.lib.require_video(key)
                if self.lib.playback.preview_is_current(video): raise self.error(409,'已有当前版本预览。')
                job=self.lib.enqueue('preview',key,automatic=automatic)
                if not job: raise self.error(429,'后台队列已满或已有相同预览。')
                self.lib.update(key,preview_status='queued',preview_job_id=job,preview_error=None,
                                preview_attempt_version=self.lib.playback.preview_target_version)
                self.record(key,task,status='queued',job_id=job,automatic=automatic,queued_at=self.now(),error=None)
                return job
        elif task=='classify':
            status=self.lib.classifier_status()
            if not status['available']: raise self.error(503,status['reason'])
            with self.lib.db_lock:
                job=self.lib.enqueue('classify',key)
                if not job: raise self.error(429,'后台队列已满或已有相同任务。')
                self.lib.update(key,classification_status='queued',classification_error=None)
                self.record(key,task,status='queued',job_id=job,automatic=automatic,queued_at=self.now(),error=None)
                return job
        else: raise self.error(400,'未知自动分析任务。')
    def after_first_analysis(self,key,tasks=('timeline','describe')):
        for task,flag,setting in (('preview','auto_preview_pending','auto_preview_new_imports'),
                                  ('timeline','auto_timeline_pending','auto_timeline_new_imports'),
                                  ('describe','auto_description_pending','auto_describe_new_imports')):
            if task not in tasks: continue
            with self.lib.db_lock:
                video=self.lib.video(key)
                if not video or video.get('status')!='ready': return
                if not video.get(flag): continue
                # Clear the new-record flag before enqueueing. Queue/model errors
                # are visible once; scanning or reanalysis cannot create a loop.
                self.lib.update(key,**{flag:False})
            if (not self.lib.settings().get(setting) or (task in ('timeline','preview') and video.get('kind')=='image')
                or (task=='preview' and not self.lib.settings().get('card_auto_preview'))):
                self.record(key,task,status='skipped',automatic=True,finished_at=self.now(),error='自动任务已关闭或此资料类型不适用。')
                continue
            field='timeline_analysis' if task=='timeline' else 'description_analysis' if task=='describe' else None
            if field and (video.get(field) or {}).get('status') in ('queued','running','complete','partial'):
                self.record(key,task,status='skipped',automatic=True,finished_at=self.now(),error='已有本项分析或正在处理，未重复排队。')
                continue
            with self.lib.jobs_lock: active=(task,key) in self.lib.active
            if active:
                self.record(key,task,status='skipped',automatic=True,finished_at=self.now(),error='已有相同任务。')
                continue
            try: self.queue(key,task,automatic=True)
            except Exception as exc:
                self.record(key,task,status='error',automatic=True,finished_at=self.now(),error=str(exc)[:1200])

    def allowed(self,key,task,options):
        if not options.get('automatic'): return True
        setting={'timeline':'auto_timeline_new_imports','describe':'auto_describe_new_imports','preview':'auto_preview_new_imports'}.get(task)
        if not setting or (self.lib.settings().get(setting) and (task!='preview' or self.lib.settings().get('card_auto_preview'))): return True
        self.record(key,task,status='skipped',automatic=True,finished_at=self.now(),error='执行前自动开关已关闭，未运行。')
        if task=='timeline':
            current=self.lib.require_video(key)
            self.lib.update(key,timeline_analysis=dict(current.get('timeline_analysis') or {},status='cancelled',error='自动时间轴已关闭，未运行。'))
        elif task=='describe':
            current=self.lib.require_video(key)
            self.lib.update(key,description_analysis=dict(current.get('description_analysis') or {},status='cancelled',error='自动描述已关闭，未发送图片。'))
        else: self.lib.update(key,preview_status='unavailable',preview_error='自动预览已关闭，未运行。')
        return False

    def finished(self,job):
        task,key=job.get('kind'),job.get('video_id')
        if task not in VERSIONS or not key or not self.lib.video(key): return
        video=self.lib.require_video(key);entry=(video.get('automation') or {}).get(task) or {}
        if entry.get('job_id')!=job['id']: return
        self.record(key,task,status='skipped' if job.get('skipped') else job['status'],finished_at=self.now(),error=job.get('error'))

    def batch(self,body):
        if not isinstance(body,dict): raise self.error(400,'批量分析参数必须是对象。')
        tasks=body.get('tasks',['timeline'])
        if not isinstance(tasks,list) or not 1<=len(tasks)<=3 or any(t not in ('timeline','classify','describe') for t in tasks) or len(set(tasks))!=len(tasks):
            raise self.error(400,'tasks应为不重复的timeline/classify/describe列表。')
        limit=body.get('limit',20)
        if isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=128: raise self.error(400,'每批资料数量应为1至128的整数。')
        retry=body.get('retry_failed',False)
        if not isinstance(retry,bool): raise self.error(400,'retry_failed必须为布尔值。')
        scope=body.get('scope','all')
        if scope not in ('all','review','personal','adult'): raise self.error(400,'分区无效。')
        if 'describe' in tasks and not self.lib.providers.local_only(self.lib.providers.config()):
            raise self.error(409,'批量描述不能发送至远程或未声明仅本地的服务，请逐项预览确认。')
        ids=body.get('video_ids')
        if ids is not None and (not isinstance(ids,list) or not 1<=len(ids)<=128 or any(not isinstance(i,str) or not re.fullmatch('[a-f0-9]{32}',i) for i in ids)):
            raise self.error(400,'video_ids应为1至128个资料ID。')
        clauses=["status='ready'"];params=[]
        if scope!='all': clauses.append('content_scope=?');params.append(scope)
        if ids is not None: clauses.append('id IN ('+','.join('?' for _ in ids)+')');params.extend(ids)
        eligibility=[]
        for task in tasks:
            state=("coalesce(json_extract(data,'$.classification_status'),'')" if task=='classify' else
                   "coalesce(json_extract(data,'$."+('timeline_analysis' if task=='timeline' else 'description_analysis')+".status'),'')")
            predicate=state+" NOT IN ('complete','partial','queued','running')"
            if task=='timeline': predicate+=" AND kind='video'"
            if task=='classify': predicate+=" AND coalesce(json_extract(data,'$.scope_source'),'')<>'manual' AND coalesce(json_extract(data,'$.scope_lock'),0)=0"
            if not retry:
                predicate+=' AND '+state+"<>'error' AND coalesce(json_extract(data,'$.automation."+task+".status'),'') NOT IN ('error','cancelled','skipped')"
            eligibility.append('('+predicate+')')
        clauses.append('('+' OR '.join(eligibility)+')')
        with self.lib.db() as db:
            rows=[json.loads(r[0]) for r in db.execute('SELECT data FROM videos WHERE '+' AND '.join(clauses)+' ORDER BY created_at,id LIMIT ?',params+[limit])]
        result={'selected':len(rows),'queued':0,'skipped':0,'errors':[],'job_ids':[],'limit':limit,'local_only':True}
        for video in rows:
            for task in tasks:
                key=video['id'];field={'timeline':'timeline_analysis','describe':'description_analysis'}.get(task)
                state=(video.get(field) or {}).get('status') if field else video.get('classification_status')
                previous=(video.get('automation') or {}).get(task) or {}
                with self.lib.jobs_lock: active=(task,key) in self.lib.active
                if (active or (task=='timeline' and video.get('kind')=='image') or state in ('complete','partial','queued','running')
                    or (task=='classify' and (video.get('scope_source')=='manual' or video.get('scope_lock')))
                    or (not retry and (state=='error' or previous.get('status') in ('error','cancelled','skipped')))):
                    result['skipped']+=1;continue
                try:
                    job=self.queue(key,task)
                    result['job_ids'].append(job);result['queued']+=1
                except Exception as exc:
                    self.record(key,task,status='error',automatic=False,finished_at=self.now(),error=str(exc)[:1200])
                    if len(result['errors'])<12: result['errors'].append({'video_id':key,'task':task,'error':str(exc)[:400]})
        result['notice']='只排本地任务；已有分析或已有同项任务会跳过。失败不会自动重试，可显式retry_failed。旧资料只因本次明确请求入队。'
        return result
