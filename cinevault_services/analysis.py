"""Objective visual/audio signals and optional local speech VAD.

No story, action, identity, age, or sexual-pose inference is implemented here.
"""
from __future__ import annotations

import array
import contextlib
import copy
import json
import math
from pathlib import Path
import subprocess
import sys
import time
import uuid

from .models import load_model_config,model_available


DEFAULT_CONFIG={
    'visual':{'enabled':True,'sensitivity':.5,'minimum_event_gap':2.0,'merge_window':1.0},
    'audio':{'enabled':True,'sensitivity':.5,'minimum_event_gap':3.0,'merge_window':1.5},
    'speech':{'enabled':True,'threshold':.5,'min_speech_duration':.3,'min_silence_duration':.6},
}
RANGES={'visual':{'sensitivity':(0,1),'minimum_event_gap':(.5,30),'merge_window':(0,10)},
        'audio':{'sensitivity':(0,1),'minimum_event_gap':(.5,30),'merge_window':(0,10)},
        'speech':{'threshold':(.1,.95),'min_speech_duration':(.1,5),'min_silence_duration':(.1,3)}}


class AnalysisService:
    def __init__(self,library,now,error):
        self.lib,self.now,self.error=library,now,error

    def config(self):
        config=copy.deepcopy(DEFAULT_CONFIG)
        saved=self.lib.settings().get('analysis_config')
        if isinstance(saved,dict):
            for engine in config:
                if isinstance(saved.get(engine),dict):
                    for name,value in saved[engine].items():
                        if name in config[engine]: config[engine][name]=value
        return config

    def configure(self,body):
        if not isinstance(body,dict) or any(name not in DEFAULT_CONFIG for name in body):
            raise self.error(400,'分析设置仅支持 visual、audio 和 speech。')
        config=self.config()
        for engine,changes in body.items():
            if not isinstance(changes,dict): raise self.error(400,'每个分析引擎的设置必须是对象。')
            for name,value in changes.items():
                if name=='enabled':
                    if not isinstance(value,bool): raise self.error(400,'enabled 必须是布尔值。')
                elif name in RANGES[engine]:
                    low,high=RANGES[engine][name]
                    if isinstance(value,bool) or not isinstance(value,(float,int)) or not math.isfinite(value) or not low<=value<=high:
                        raise self.error(400,f'{engine}.{name} 应在 {low} 至 {high} 之间。')
                    value=float(value)
                else: raise self.error(400,'未知分析参数：'+engine+'.'+name)
                config[engine][name]=value
        settings=self.lib.settings()
        settings['analysis_config']=config
        self.lib._save('settings','settings',settings)
        return self.describe()

    def speech_config(self):
        return load_model_config(self.lib.app,'ai/audio_config.json','CINEVAULT_AUDIO_CONFIG',
                                 'ai/analyze_audio.py','ai/models/silero_vad.onnx')

    def describe(self):
        config=self.config()
        speech=self.speech_config()
        speech_ready=model_available(speech)
        nsfw=self.lib.classifier_status()
        provider=self.lib.providers.config()
        vision_configured=bool(provider.get('model'))
        engines=[
            {'id':'visual','name':'画面变化候选','kind':'algorithm','installed':bool(self.lib.ffmpeg),'available':bool(self.lib.ffmpeg),
             'status':'ready' if self.lib.ffmpeg else 'not_installed','description':'4 fps 灰度帧差；相近候选合并。不是动作或人物识别。'},
            {'id':'audio','name':'音频电平与瞬态区间','kind':'algorithm','installed':bool(self.lib.ffmpeg),'available':bool(self.lib.ffmpeg),
             'status':'ready' if self.lib.ffmpeg else 'not_installed','description':'16 kHz 单声道、250 ms RMS/峰值，使用回滞与区间合并减少零碎点。不是语义理解。'},
            {'id':'speech','name':'Silero VAD 人声活动区间','kind':'local_model','installed':speech_ready,'available':speech_ready,
             'status':'ready' if speech_ready else 'not_installed','model':speech.get('model_id') if speech else None,
             'description':'本地 ONNX 模型检测可能包含语音的区间；不转写、不识别人，也不判断剧情或行为。'},
            {'id':'nsfw','name':'本地内容分类','kind':'local_model','installed':nsfw['available'],'available':nsfw['available'],
             'status':'ready' if nsfw['available'] else 'not_installed','model':nsfw.get('model'),'description':'只提供内容类别概率，不用于人物或年龄验证。'},
            {'id':'ollama','name':'可替换视觉概述','kind':'provider','installed':None,'available':False,
             'status':'configured_unverified' if vision_configured else 'not_configured',
             'description':'支持 Ollama/OpenAI-compatible 通用图像概述。请在模型服务区配置并明确测试；此列表不会探测、下载或宣称模型已安装。'},
            {'id':'semantic','name':'通用语义事件模型','kind':'local_model','installed':False,'available':False,'status':'not_installed',
             'description':'未安装可核验的通用语义事件模型；不提供动作、姿势、人物身份或年龄推断。'},
        ]
        return {'engines':engines,'config':config,'ranges':RANGES,
                'limitations':['算法输出是信号变化候选，不等于精彩程度或情节判断。',
                               '人声活动模型可能把音乐或其他声音误判为语音；没有转写或身份识别。',
                               '修改设置不会自动重跑已有资料；只有明确发起的分析任务会执行。']}

    def select_engines(self,body):
        config=self.config()
        explicit='engines' in body
        selected=body.get('engines') if explicit else [name for name,value in config.items() if value['enabled']]
        if not isinstance(selected,list) or not selected or any(name not in DEFAULT_CONFIG for name in selected):
            raise self.error(400,'请选择 visual、audio 或 speech 分析引擎。')
        selected=list(dict.fromkeys(selected))
        if 'speech' in selected and not model_available(self.speech_config()):
            if explicit: raise self.error(503,'尚未安装此系统可用的 Silero VAD 模型或依赖。')
            selected.remove('speech')
        if not selected: raise self.error(400,'没有已启用且可用的分析引擎。')
        return selected

    def queue(self,key,body=None):
        body=body or {}
        if 'automatic' in body and not isinstance(body['automatic'],bool): raise self.error(400,'automatic必须为布尔值。')
        selected=self.select_engines(body)
        with self.lib.db_lock:
            video=self.lib.require_video(key)
            if video.get('kind')=='image': raise self.error(400,'图片没有视频时间轴。')
            if video.get('status')!='ready' or not video.get('duration'): raise self.error(409,'请先完成视频分析并核对原片状态。')
            if not self.lib.ffmpeg: raise self.error(503,'时间轴分析需要 FFmpeg。')
            if not video.get('audio_codec') and 'visual' not in selected: raise self.error(409,'该视频未记录可用音轨，无法单独分析音频。')
            job=self.lib.enqueue('timeline',key,engines=selected,automatic=bool(body.get('automatic')))
            if not job: raise self.error(429,'分析已在队列中，或后台队列已满。')
            self.lib.update(key,timeline_analysis=dict(video.get('timeline_analysis') or {},status='queued',queued_at=self.now(),requested_engines=selected))
            self.lib.automation.record(key,'timeline',status='queued',job_id=job,automatic=bool(body.get('automatic')),queued_at=self.now(),error=None)
            return job

    def candidates(self,start,frame_bytes,audio_bytes,duration,config=None,engines=None,clip_index=0):
        config=config or self.config()
        engines=engines or ['visual','audio']
        events=[]
        size=96*54
        count=len(frame_bytes)//size
        if 'visual' in engines:
            threshold=.18-.16*config['visual']['sensitivity']
            previous=None
            for index in range(count):
                current=frame_bytes[index*size:(index+1)*size]
                if previous is not None:
                    change=sum(abs(a-b) for a,b in zip(previous,current))/(size*255)
                    if change>=threshold:
                        timestamp=min(duration,start+index/4)
                        events.append({'time':timestamp,'start':max(start,timestamp-.25),'end':min(duration,timestamp+.25),
                                       'kind':'scene_cut','title':'画面切换候选','score':round(min(1,change/.5),4),
                                       'score_kind':'normalized_change_strength','clip_index':clip_index,
                                       'basis':f'4 fps 灰度帧差 {change:.3f}；阈值 {threshold:.3f}，运动或闪光也可能触发。'})
                previous=current
        samples=array.array('h');samples.frombytes(audio_bytes[:len(audio_bytes)//2*2])
        if sys.byteorder!='little': samples.byteswap()
        threshold=16-12*config['audio']['sensitivity']
        hysteresis=max(2,threshold*.3)
        previous_rms=previous_peak=None
        armed=True;stable=0;active=None
        if 'audio' in engines:
            for index in range(0,len(samples),4000):
                values=samples[index:index+4000]
                if len(values)<2000: continue
                rms=math.sqrt(sum(float(value)*value for value in values)/len(values))/32768
                peak=max(abs(value) for value in values)/32768
                rms_db=20*math.log10(max(rms,1e-5));peak_db=20*math.log10(max(peak,1e-5))
                timestamp=min(duration,start+index/16000)
                if previous_rms is not None:
                    delta=abs(rms_db-previous_rms);rise=peak_db-previous_peak
                    trigger=(delta>=threshold and max(rms_db,previous_rms)>-55) or (rise>=threshold+4 and peak_db>-35 and peak_db-rms_db>=6)
                    if armed and trigger:
                        active={'time':timestamp,'start':timestamp,'end':min(duration,timestamp+.25),'kind':'audio_change','title':'音频变化区间',
                                'score':round(min(1,max(delta,rise)/36),4),'score_kind':'normalized_change_strength','clip_index':clip_index,
                                'basis':f'250 ms RMS/峰值变化；触发阈值 {threshold:g} dB，回滞 {hysteresis:g} dB。仅信号强弱变化。'}
                        events.append(active);armed=False;stable=0
                    elif not armed:
                        if active: active['end']=min(duration,timestamp+.25)
                        stable=stable+1 if delta<=hysteresis else 0
                        if stable>=2: armed=True;active=None
                previous_rms,previous_peak=rms_db,peak_db
        return events,count,len(samples)/16000

    @staticmethod
    def merge(events,config):
        merged=[]
        for kind,engine in (('scene_cut','visual'),('audio_change','audio')):
            clip_ids=sorted({event.get('clip_index',0) for event in events if event['kind']==kind})
            for clip in clip_ids:
                items=sorted((dict(event) for event in events if event['kind']==kind and event.get('clip_index',0)==clip),key=lambda item:item['start'])
                groups=[]
                for event in items:
                    event['candidate_count']=1
                    if groups and event['start']-groups[-1]['end']<=config[engine]['merge_window']:
                        old=groups[-1];old['end']=max(old['end'],event['end']);old['candidate_count']+=1
                        if event['score']>old['score']: old['score'],old['time']=event['score'],event['time']
                    else: groups.append(event)
                kept=[]
                for event in sorted(groups,key=lambda item:item['score'],reverse=True):
                    if all(abs(event['time']-old['time'])>=config[engine]['minimum_event_gap'] for old in kept): kept.append(event)
                merged.extend(kept)
        merged.extend(event for event in events if event['kind']=='speech')
        return sorted(sorted(merged,key=lambda item:item['score'],reverse=True)[:256],key=lambda item:(item['time'],item['kind']))

    def analyze(self,key,options=None):
        lib=self.lib
        if key in lib.timeline_cancelled: raise RuntimeError('用户取消了分析。')
        video=lib.require_video(key);lib.check_source_device(video)
        path=lib.path(video);before=path.stat();duration=float(video.get('duration') or 0)
        if duration<=0 or (before.st_size,before.st_mtime_ns)!=(video.get('size'),video.get('mtime_ns')):
            raise RuntimeError('原片资料无效或已发生变化，请先校验。')
        config=self.config();engines=self.select_engines(options or {})
        has_audio=bool(video.get('audio_codec'))
        if not has_audio:
            engines=[engine for engine in engines if engine=='visual']
            if not engines: raise RuntimeError('没有可分析的音轨。')
        windows=[(0,duration)] if duration<=120 else [(index*(duration-2.5)/47,2.5) for index in range(48)]
        speech_config=self.speech_config() if 'speech' in engines else None
        analysis={'version':'objective-regions-v2','method':'4fps frame difference; 16kHz RMS hysteresis/merged regions; optional local Silero VAD',
                  'status':'running','started_at':self.now(),'media_duration':duration,'sampling_fps':4 if 'visual' in engines else None,
                  'time_resolution_seconds':.25,'audio_window_seconds':.25,'requested_engines':engines,'parameters':config,
                  'engine_versions':{'visual':'frame-difference-v2','audio':'rms-hysteresis-v2','speech':speech_config.get('model_id') if speech_config else None},
                  'coverage':{'analyzed_seconds':0,'ratio':0,'windows':[],'max_gap_seconds':duration},'event_count':0,
                  'planned_windows':len(windows),'processed_windows':0,'sample_budget_seconds':min(120,duration),
                  'limitations':['输出是客观候选与语音活动区间，不理解剧情，不识别人、人数、性别、年龄或动作。',
                                 '250 ms 信号采样、编码延迟和重采样会造成时间偏移；变化强度不是模型置信度。',
                                 '长视频最多分析约120秒的分散窗口，未检查窗口间内容；不会跨未分析区间合并事件。',
                                 'Silero VAD 不转写，也可能把音乐或其他声音误判为语音；不代表情节或行为。',
                                 '本次不重新计算全文件 SHA-256，不能代替完整性校验。']}
        lib.update(key,timeline_analysis=analysis)
        deadline=time.monotonic()+300;candidates=[];covered=[];errors=[];pcm_files=[];clips=[]
        try:
            for clip_index,(start,length) in enumerate(windows):
                self.check_cancel(key)
                if time.monotonic()>=deadline: errors.append('达到300秒计算时限。');break
                audio_file=lib.root/'tmp'/('timeline-'+uuid.uuid4().hex+'.pcm')
                pcm_files.append(audio_file)
                audio_enabled=has_audio and bool({'audio','speech'}&set(engines))
                args=[lib.ffmpeg,'-hide_banner','-loglevel','error','-threads','2','-ss',str(start),'-i',str(path)]
                if 'visual' in engines:
                    args+=['-t',str(length),'-map','0:v:0','-an','-vf','fps=4,scale=96:54,format=gray','-pix_fmt','gray','-f','rawvideo','pipe:1']
                    if audio_enabled: args+=['-t',str(length),'-map','0:a:0','-vn','-ac','1','-ar','16000','-c:a','pcm_s16le','-f','s16le','-y',str(audio_file)]
                else:
                    args+=['-t',str(length),'-map','0:a:0','-vn','-ac','1','-ar','16000','-c:a','pcm_s16le','-f','s16le','-y',str(audio_file)]
                try:
                    result=lib.run_tool(args,timeout=min(180 if len(windows)==1 else 60,max(1,deadline-time.monotonic())))
                    audio_bytes=audio_file.read_bytes() if audio_file.exists() else b''
                    events,count,audio_seconds=self.candidates(start,result.stdout if 'visual' in engines else b'',audio_bytes,duration,config,engines,clip_index)
                    sampled=count/4 if 'visual' in engines else audio_seconds
                    if sampled<=0: raise RuntimeError('窗口没有解码出有效信号。')
                    candidates.extend(events);covered.append({'start':round(start,3),'end':round(min(duration,start+length,start+sampled),3)})
                    if 'speech' in engines and audio_bytes: clips.append({'pcm_path':str(audio_file),'start':start})
                    else:
                        with contextlib.suppress(OSError): audio_file.unlink()
                    analysis['processed_windows']+=1;analysis['progress']=round(analysis['processed_windows']/len(windows),4)
                    lib.update(key,timeline_analysis=dict(analysis))
                except (RuntimeError,subprocess.TimeoutExpired) as exc:
                    self.check_cancel(key)
                    errors.append(f'{start:.2f}秒窗口未完成：{str(exc)[-350:]}')
                    if len(errors)>=5: break
            if 'speech' in engines and clips:
                manifest=lib.root/'tmp'/('audio-analysis-'+uuid.uuid4().hex+'.json')
                try:
                    manifest.write_text(json.dumps({'clips':clips,'sample_rate':16000,'max_seconds':120,
                                                    'config':{name:value for name,value in config['speech'].items() if name!='enabled'}}),'utf-8')
                    result=lib.run_tool([str(speech_config['python_path']),str(speech_config['script_path']),'--request',str(manifest)],timeout=max(1,min(120,deadline-time.monotonic())))
                    output=json.loads(result.stdout.decode('utf-8-sig'))
                    if not isinstance(output.get('segments'),list): raise ValueError('语音模型未返回有效区间。')
                    for segment in output['segments'][:512]:
                        start,end,score=float(segment['start']),float(segment['end']),float(segment['score'])
                        clip_index=int(segment['clip_index'])
                        if (not all(math.isfinite(v) for v in (start,end,score)) or not 0<=score<=1 or not 0<=start<end<=duration+.1
                            or not 0<=clip_index<len(clips)): raise ValueError('语音区间超出已分析范围。')
                        clip=clips[clip_index]
                        clip_seconds=Path(clip['pcm_path']).stat().st_size/32000
                        if start<clip['start']-.01 or end>clip['start']+clip_seconds+.1: raise ValueError('语音区间跨越未分析内容。')
                        candidates.append({'time':round(start,3),'start':round(start,3),'end':round(min(duration,end),3),'kind':'speech',
                                           'title':'可能包含语音的区间','score':round(score,4),'score_kind':'model_speech_probability','clip_index':clip_index,
                                           'model':output.get('model'),'basis':'本机 Silero VAD 语音活动检测；没有转写、身份或行为识别。'})
                    analysis['speech_model']=output.get('model');analysis['speech_status']='complete'
                except (RuntimeError,ValueError,KeyError,TypeError,subprocess.TimeoutExpired) as exc:
                    self.check_cancel(key);errors.append('语音模型未完成：'+str(exc)[-500:]);analysis['speech_status']='error'
                finally:
                    with contextlib.suppress(OSError): manifest.unlink()
            selected=self.merge(candidates,config)
            for event in selected:
                for name in ('time','start','end'): event[name]=round(max(0,min(duration,event[name])),3)
                event['id']=event['kind']+'-'+str(round(event['time']*1000))
            analyzed=sum(max(0,item['end']-item['start']) for item in covered)
            gaps=([covered[0]['start']] if covered else [duration])+[max(0,b['start']-a['end']) for a,b in zip(covered,covered[1:])]+([duration-covered[-1]['end']] if covered else [])
            self.check_cancel(key);after=path.stat()
            if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns): errors.append('原片在分析期间发生变化，请校验后重试。')
            analysis.update(status='partial' if errors or len(covered)<len(windows) else 'complete',analyzed_at=self.now(),event_count=len(selected),
                            candidate_count=len(candidates),discarded_candidate_count=max(0,len(candidates)-len(selected)),audio_analyzed=has_audio and 'audio' in engines,
                            coverage={'analyzed_seconds':round(analyzed,3),'ratio':round(min(1,analyzed/duration),6),'windows':covered,'max_gap_seconds':round(max(gaps),3)},
                            errors=errors[:5],progress=1)
            lib.update(key,temporal_events=selected,temporal_event_count=len(selected),timeline_analysis=analysis)
        finally:
            for path in pcm_files:
                with contextlib.suppress(OSError): path.unlink()

    def check_cancel(self,key):
        if self.lib.stop.is_set() or key in self.lib.cancelled or key in self.lib.timeline_cancelled:
            raise RuntimeError('分析已取消。')
