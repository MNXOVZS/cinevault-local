# [INPUT]: 片目服务和固定分类引擎清单。
# [OUTPUT]: 分类状态与已登记工件下载接口。
# [POS]: 本机分类适配层。首发清单为空。
# [PROTOCOL]: Update this header on change, then check AGENTS.md
"""Pinned, portable model registry. Only explicit weight downloads use the network.

Media never enters this service. Downloaded bytes are hashed before an atomic
replace; dependencies are platform-specific and installed by the setup scripts.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import re
from pathlib import Path
import shutil
import stat
import threading
import time
import urllib.parse
import urllib.request
import uuid

CLASSIFICATION_ENGINES = ('vit', 'nudenet', 'nudenet-640m')
REGION_NAMES = {
    'chest_exposed':'胸部暴露候选', 'buttocks_exposed':'臀部暴露候选',
    'genital_region_exposed':'生殖区域暴露候选', 'chest_covered':'胸部遮盖候选',
    'buttocks_covered':'臀部遮盖候选', 'genital_region_covered':'生殖区域遮盖候选',
    'abdomen_exposed':'腹部暴露候选', 'abdomen_covered':'腹部遮盖候选',
    'feet_exposed':'足部暴露候选', 'feet_covered':'足部遮盖候选',
    'armpits_exposed':'腋部暴露候选', 'armpits_covered':'腋部遮盖候选',
}
# 公开预览版保留已有标签读取。分类运行组件单独发行。
MODELS = {}
_ALLOWED_HOSTS = {'api.github.com','github.com','release-assets.githubusercontent.com',
 'raw.githubusercontent.com','huggingface.co','cas-bridge.xethub.hf.co',
 'cdn-lfs.huggingface.co','cdn-lfs-us-1.huggingface.co','cdn-lfs-us-1.hf.co',
 'cdn-lfs.hf.co','us.aws.cdn.hf.co','eu.aws.cdn.hf.co','transfer.xethub.hf.co'}


def _safe_url(url):
    parts=urllib.parse.urlsplit(url)
    return parts.scheme=='https' and not parts.username and not parts.password and parts.port in (None,443) and parts.hostname in _ALLOWED_HOSTS


class _PinnedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        hops=getattr(req,'model_redirects',0)
        if hops>=4 or not _safe_url(newurl): raise RuntimeError('模型下载跳转到了未登记地址。')
        result=super().redirect_request(req,fp,code,msg,headers,newurl)
        if result is not None: result.model_redirects=hops+1
        return result


class LocalModelRegistry:
    def __init__(self,library,error):
        self.lib,self.error,self.app=library,error,Path(library.app).resolve()
        self.lock=threading.RLock()
        self.downloads={}
        self.workers={}
        self.cancel_events={}
        self.verified={}
        # A crashed process can leave only these owned, bounded temporary files.
        # A normal failure/cancel removes its own partial immediately below.
        for spec in MODELS.values():
            try:
                target=self._path(spec)
                for partial in target.parent.glob(target.name+'.*.download-part'):
                    info=partial.lstat()
                    if (re.fullmatch(re.escape(target.name)+r'\.[a-f0-9]{32}\.download-part',partial.name)
                        and stat.S_ISREG(info.st_mode) and not getattr(info,'st_file_attributes',0)&0x400
                        and time.time()-info.st_mtime>86400): partial.unlink()
            except (OSError,self.error): pass

    def _path(self,spec):
        target=self.app/spec['path']
        for ancestor in (target,*target.parents):
            if ancestor==self.app.parent: break
            if os.path.lexists(ancestor):
                info=ancestor.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
                    raise self.error(409,'模型目录中出现链接，请使用普通文件夹。')
        if not target.is_relative_to(self.app): raise self.error(409,'模型路径越界。')
        return target

    def _installed(self,spec):
        path=self._path(spec)
        try:
            info=path.stat()
            if not path.is_file() or info.st_size!=spec['bytes']: return False
            key=(str(path),info.st_size,info.st_mtime_ns)
            with self.lock:
                if key in self.verified: return self.verified[key]
            with path.open('rb') as source: valid=hashlib.file_digest(source,'sha256').hexdigest()==spec['sha256']
            with self.lock:
                self.verified={k:v for k,v in self.verified.items() if k[0]!=str(path)}
                self.verified[key]=valid
            return valid
        except OSError: return False

    def describe(self):
        items=[]
        for model_id,spec in MODELS.items():
            try: installed=self._installed(spec); path_error=None
            except self.error as exc: installed=False; path_error=str(exc)
            status=self.lib.classifier_status(spec['engine'])
            with self.lock: download=dict(self.downloads.get(model_id,{'status':'idle','received':0,'total':spec['bytes'],'error':None}))
            runtime=bool(status.get('runtime_available',status.get('available',False)))
            items.append({'id':model_id,'engine':spec['engine'],'name':spec['name'],'bytes':spec['bytes'],
                'sha256':spec['sha256'],'installed':installed,'runtime_available':runtime,'available':installed and runtime,
                'state':'ready' if installed and runtime else 'runtime_missing' if installed else 'not_installed',
                'download':download,'license':spec['license'],'notice':path_error or spec['notice'],
                'relative_path':spec['path'],'source':spec['url'].split('/resolve/')[0],
                'portable':True,'can_download':True})
        return {'selected_engine':self.lib.settings().get('classification_engine','vit'),'models':items,'local_only':True,
            'notice':'公开预览版保留手动分组和已有分类结果。视觉概述可在分析中心选用。'}

    def _model(self,body):
        if not isinstance(body,dict) or set(body)-{'model_id'}: raise self.error(400,'只接受已登记的 model_id，不能传入下载地址。')
        model_id=body.get('model_id')
        if not isinstance(model_id,str) or model_id not in MODELS: raise self.error(400,'模型不在固定清单中。')
        return model_id,MODELS[model_id]

    def install(self,body):
        model_id,spec=self._model(body)
        if self._installed(spec): return {'model_id':model_id,'status':'ready','already_installed':True,'local_only':True}
        with self.lock:
            if any(worker.is_alive() for worker in self.workers.values()): raise self.error(409,'已有模型正在下载，请等待完成或取消。')
            target=self._path(spec)
            target.parent.mkdir(parents=True,exist_ok=True)
            # Recheck after mkdir, including all parent links.
            target=self._path(spec)
            if shutil.disk_usage(target.parent).free < spec['bytes']+16*1024*1024: raise self.error(507,'模型所在磁盘空间不足。')
            event=threading.Event();self.cancel_events[model_id]=event
            self.downloads[model_id]={'status':'downloading','received':0,'total':spec['bytes'],'error':None}
            worker=threading.Thread(target=self._download,args=(model_id,spec,event),daemon=True,name='CineVault-model-download')
            self.workers[model_id]=worker;worker.start()
        return {'model_id':model_id,'status':'downloading','already_installed':False,'local_only':True}

    def cancel(self,body):
        model_id,_=self._model(body)
        with self.lock:
            event=self.cancel_events.get(model_id)
            worker=self.workers.get(model_id)
            if event and worker and worker.is_alive():
                event.set();self.downloads[model_id]['status']='cancelling'
                return {'model_id':model_id,'status':'cancelling'}
        return {'model_id':model_id,'status':'idle'}

    def _download(self,model_id,spec,event):
        partial=None
        try:
            if not _safe_url(spec['url']): raise RuntimeError('模型下载地址未登记。')
            target=self._path(spec)
            partial=target.with_name(target.name+'.'+uuid.uuid4().hex+'.download-part')
            started=time.monotonic();received=0;digest=hashlib.sha256()
            headers={'User-Agent':'CineVault/1.7 model installer','Accept':'application/octet-stream','X-GitHub-Api-Version':'2022-11-28'}
            opener=urllib.request.build_opener(_PinnedRedirect())
            url=spec['url']
            with partial.open('xb') as output:
                while received<spec['bytes']:
                    if event.is_set() or self.lib.stop.is_set(): raise InterruptedError('下载已取消。')
                    if time.monotonic()-started>600: raise TimeoutError('模型下载超时，请重试。')
                    # Bounded ranges avoid a large stalled response monopolising
                    # the connection. The source's signed URL is kept in memory.
                    end=min(received+2*1024*1024,spec['bytes'])-1
                    request=urllib.request.Request(url,headers=dict(headers,Range=f'bytes={received}-{end}'))
                    with opener.open(request,timeout=10) as response:
                        if not _safe_url(response.geturl()): raise RuntimeError('模型源跳转到了未登记地址。')
                        url=response.geturl()
                        if response.status!=206 or response.headers.get('Content-Range')!=f'bytes {received}-{end}/{spec["bytes"]}':
                            raise RuntimeError('模型源未提供预期的分段下载，请稍后重试。')
                        length=response.headers.get('Content-Length')
                        if length and int(length)!=end-received+1: raise RuntimeError('模型分段大小与固定清单不符。')
                        if 'text/html' in response.headers.get('Content-Type','').lower(): raise RuntimeError('模型源返回网页，未作为权重保存。')
                        while received<=end:
                            if event.is_set() or self.lib.stop.is_set(): raise InterruptedError('下载已取消。')
                            if time.monotonic()-started>600: raise TimeoutError('模型下载超时，请重试。')
                            chunk=response.read1(min(64*1024,end-received+2))
                            if not chunk: raise RuntimeError('模型分段下载不完整，请重试。')
                            received+=len(chunk)
                            if received>end+1: raise RuntimeError('模型源返回了超出预期大小的数据。')
                            output.write(chunk);digest.update(chunk)
                            with self.lock: self.downloads[model_id]['received']=received
                if received!=spec['bytes'] or digest.hexdigest()!=spec['sha256']: raise RuntimeError('模型 SHA-256 或长度不匹配，未安装。')
                output.flush();os.fsync(output.fileno())
            if event.is_set() or self.lib.stop.is_set(): raise InterruptedError('下载已取消。')
            if self._path(spec)!=target: raise RuntimeError('模型路径在下载期间发生变化。')
            os.replace(partial,target)
            with self.lock: self.downloads[model_id].update(status='complete',received=spec['bytes'],error=None)
        except InterruptedError as exc:
            with self.lock: self.downloads[model_id].update(status='cancelled',error=str(exc))
        except Exception as exc:
            with self.lock: self.downloads[model_id].update(status='error',error=str(exc)[:400])
        finally:
            if partial is not None:
                with contextlib.suppress(OSError): partial.unlink()

    def close(self):
        with self.lock:
            for event in self.cancel_events.values(): event.set()
            workers=list(self.workers.values())
        # Never hold the state lock while a writer runs its finally block.
        deadline=time.monotonic()+11
        for worker in workers:
            if worker is not threading.current_thread(): worker.join(max(0,deadline-time.monotonic()))
