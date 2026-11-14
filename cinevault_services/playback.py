"""Browser compatibility and bounded hover previews. Originals are read-only."""
from __future__ import annotations

import contextlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


PREVIEW_VERSION = 'hover-v2-480x270-24fps'
PREVIEW_WIDTH, PREVIEW_HEIGHT, PREVIEW_FPS = 480, 270, 24
PREVIEW_MAX_SECONDS = 4.5


class PlaybackService:
    preview_target_version = PREVIEW_VERSION

    def __init__(self,library,now,metadata,safe_path,error):
        self.lib,self.now,self.metadata,self.safe_path,self.error=library,now,metadata,safe_path,error

    @staticmethod
    def signature(video):
        return {name:video.get(name) for name in ('sha256','size','mtime_ns')}

    @classmethod
    def preview_is_current(cls,video):
        """A generation tag invalidates legacy low-frame-rate cache once."""
        return (video.get('preview_version')==PREVIEW_VERSION
                and video.get('preview_source')==cls.signature(video))

    @staticmethod
    def copyable_video(video):
        return (video.get('codec')=='h264' and int(video.get('bit_depth') or 8)<=8
                and video.get('pixel_format','yuv420p') in ('','yuv420p','yuvj420p'))

    @classmethod
    def original_safe(cls,video):
        extension=Path(video.get('filename','')).suffix.lower()
        if extension in ('.mp4','.m4v'):
            return cls.copyable_video(video) and video.get('audio_codec') in (None,'','aac')
        return (extension=='.webm' and video.get('codec') in ('vp8','vp9')
                and int(video.get('bit_depth') or 8)<=8 and video.get('audio_codec') in (None,'','opus','vorbis'))

    def check(self,key):
        video=self.lib.require_video(key)
        if video.get('kind')=='image': raise self.error(400,'图片不需要准备视频播放或短片预览。')
        if video.get('status') in ('queued','analyzing'): return video,None
        if video.get('status')!='ready': raise self.error(409,'原片尚未就绪或需要校验，请先核对文件状态。')
        try:
            self.lib.check_source_device(video)
            path=self.lib.path(video)
            if video.get('owned') and not self.safe_path(path,self.lib.root/'media'):
                raise RuntimeError('媒体路径中出现链接，已停止准备。')
            info=path.stat()
            if not path.is_file(): raise FileNotFoundError()
        except (OSError,RuntimeError) as exc: raise self.error(409,'无法读取已登记的原片：'+str(exc))
        if (info.st_size,info.st_mtime_ns)!=(video.get('size'),video.get('mtime_ns')):
            raise self.error(409,'原片大小或修改时间已变化，请先校验；不会把旧缓存当作已验证的原片。')
        return video,path

    def asset(self,key):
        path=self.lib.root/'assets'/key
        if not self.safe_path(path,self.lib.root/'assets'): raise self.error(409,'缓存路径中出现链接，已停止操作。')
        path.mkdir(exist_ok=True)
        return path

    def active(self,kind,key):
        with self.lib.jobs_lock: return (kind,key) in self.lib.active

    def prepare(self,key,body):
        for field in ('force_compatible','force_transcode','retry'):
            if field in body and not isinstance(body[field],bool): raise self.error(400,field+' 必须为布尔值。')
        with self.lib.db_lock:
            video,path=self.check(key)
            if path is None: return {'status':'processing','url':None,'mode':'original','job_id':None,'notice':'媒体资料仍在分析，完成后继续准备播放。'}
            force=bool(body.get('force_compatible') or body.get('force_transcode') or video.get('playback_force_compatible'))
            retry=bool(body.get('retry'))
            compatible=self.lib.root/'assets'/key/'compatible.mp4'
            mode=video.get('playback_mode') or ('remux' if self.copyable_video(video) else 'transcode')
            signature=video.get('playback_source')
            cache_matches=not signature or signature==self.signature(video)
            if compatible.is_file() and cache_matches and not retry and (not force or mode=='transcode'):
                return {'status':'ready','url':f'/media/{key}/compatible','mode':mode,'job_id':None,
                        'info':video.get('compatible_info'),'notice':'播放兼容副本，原片保持不变。'+('旧副本未记录生成时指纹。' if not signature else '')}
            if self.active('transcode',key):
                if force: self.lib.update(key,playback_force_compatible=True)
                return {'status':video.get('playback_status') if video.get('playback_status') in ('queued','processing') else 'queued',
                        'url':None,'mode':'transcode' if force else mode,'job_id':video.get('playback_job_id'),
                        'notice':'正在准备兼容副本；长片需要更多时间。'}
            if video.get('playback_status')=='error' and not retry:
                return {'status':'error','url':None,'mode':mode,'job_id':None,'error':video.get('playback_error'),'notice':'准备失败，可明确重试；轮询不会重复创建任务。'}
            if self.original_safe(video) and not force:
                return {'status':'ready','url':f'/media/{key}','mode':'original','job_id':None,'info':None,'notice':'直接播放原片。浏览器解码失败时可自动准备兼容副本。'}
            if not self.lib.ffmpeg: raise self.error(503,'准备兼容播放需要 FFmpeg。')
            mode='transcode' if force or not self.copyable_video(video) else 'remux'
            job=self.lib.enqueue('transcode',key)
            if not job: raise self.error(429,'后台队列已满，请稍后重试。')
            self.lib.update(key,playback_status='queued',playback_mode=mode,playback_job_id=job,
                            playback_force_compatible=force,playback_error=None)
            return {'status':'queued','url':None,'mode':mode,'job_id':job,'notice':'正在自动准备浏览器兼容副本；原片不会改变。'}

    def probe(self,path):
        if not self.lib.ffprobe: return None
        data=json.loads(self.lib.run_tool([self.lib.ffprobe,'-v','error','-show_streams','-show_format','-of','json',str(path)],timeout=60).stdout)
        visual=next(stream for stream in data.get('streams',[]) if stream.get('codec_type')=='video')
        audio=next((stream for stream in data.get('streams',[]) if stream.get('codec_type')=='audio'),{})
        rate=visual.get('avg_frame_rate') or visual.get('r_frame_rate') or '0/1'
        try:
            numerator,denominator=map(float,str(rate).split('/',1))
            fps=numerator/denominator if denominator else None
        except (ValueError,TypeError): fps=None
        if fps is not None and not math.isfinite(fps): fps=None
        try: duration=float(data.get('format',{}).get('duration') or visual.get('duration') or 0)
        except (ValueError,TypeError): duration=None
        if duration is not None and not math.isfinite(duration): duration=None
        return dict(self.metadata(visual),width=visual.get('width'),height=visual.get('height'),codec=visual.get('codec_name'),
                    audio_codec=audio.get('codec_name'),audio_channels=audio.get('channels'),
                    fps=fps,duration=duration,size=path.stat().st_size,
                    container=data.get('format',{}).get('format_name'),measured_at=self.now())

    def transcode(self,key):
        video,path=self.check(key)
        if path is None: raise RuntimeError('请先完成媒体分析。')
        if not self.lib.ffmpeg: raise RuntimeError('兼容副本需要 FFmpeg。')
        if shutil.disk_usage(self.lib.root).free<128*1024*1024: raise RuntimeError('缓存磁盘空间不足。')
        asset=self.asset(key)
        temporary=asset/'compatible.partial.mp4'
        mode='transcode' if video.get('playback_force_compatible') or not self.copyable_video(video) else 'remux'
        self.lib.update(key,playback_status='processing',playback_mode=mode,playback_error=None)
        attempts=[]
        try:
            for attempt in range(2):
                args=[self.lib.ffmpeg,'-hide_banner','-loglevel','error','-threads','2','-i',str(path),'-map','0:v:0','-map','0:a:0?']
                if mode=='remux':
                    args+=['-c:v','copy','-c:a','copy' if video.get('audio_codec') in (None,'','aac') else 'aac']
                    if video.get('audio_codec') not in (None,'','aac'): args+=['-b:a','192k']
                else:
                    args+=['-vf',"scale='min(1920,iw)':-2",'-c:v','libx264','-preset','veryfast','-threads','2',
                           '-crf','20','-pix_fmt','yuv420p','-c:a','aac','-b:a','192k']
                args+=['-map_metadata','-1','-movflags','+faststart','-y',str(temporary)]
                try:
                    self.lib.run_tool(args,timeout=24*3600)
                    break
                except (RuntimeError,subprocess.TimeoutExpired) as exc:
                    attempts.append({'mode':mode,'error':str(exc)[-800:]})
                    if mode!='remux' or self.lib.stop.is_set() or key in self.lib.cancelled: raise
                    mode='transcode'
                    self.lib.update(key,playback_mode=mode,playback_notice='重新封装未成功，已尝试一次兼容编码。')
            current=path.stat()
            if (current.st_size,current.st_mtime_ns)!=(video['size'],video['mtime_ns']): raise RuntimeError('准备期间原片发生变化，请先校验。')
            info=self.probe(temporary)
            os.replace(temporary,asset/'compatible.mp4')
            self.lib.update(key,has_compatible=True,compatible_info=info,playback_status='ready',playback_mode=mode,
                            playback_error=None,playback_source=self.signature(video),playback_attempts=attempts,
                            playback_ready_at=self.now(),playback_conversion={'mode':mode,'video_copied':mode=='remux',
                                'notice':'重新封装会保留视频流；兼容编码是有损副本，原片不会改变。'})
        except Exception as exc:
            self.lib.update(key,playback_status='error',playback_error=str(exc)[:1600],playback_attempts=attempts)
            raise
        finally:
            with contextlib.suppress(OSError): temporary.unlink()

    def external_status(self):
        if os.name=='nt':
            executable=self.lib.app/'bin'/'mpv'/'mpv.exe'
            return {'available':executable.is_file(),'name':'mpv','notice':'独立窗口按原片播放；不会自动回传观看进度。'}
        if sys.platform=='darwin' and Path('/Applications/IINA.app/Contents/MacOS/iina-cli').is_file():
            return {'available':True,'name':'IINA','notice':'独立窗口按原片播放；不会自动回传观看进度。'}
        return {'available':bool(shutil.which('mpv')),'name':'mpv','notice':'独立窗口按原片播放；不会自动回传观看进度。'}

    def external_play(self,key):
        video,path=self.check(key)
        if path is None: raise self.error(409,'请先完成媒体分析并核对原片状态。')
        status=self.external_status()
        if not status['available']: raise self.error(503,'没有找到本机原片播放器。')
        if status['name']=='IINA':
            command=['/Applications/IINA.app/Contents/MacOS/iina-cli','--',str(path)]
        else:
            executable=self.lib.app/'bin'/'mpv'/'mpv.exe' if os.name=='nt' else Path(shutil.which('mpv'))
            command=[str(executable),'--no-config','--save-position-on-quit=no','--resume-playback=no','--ytdl=no','--hwdec=auto-safe','--',str(path)]
        options={'stdin':subprocess.DEVNULL,'stdout':subprocess.DEVNULL,'stderr':subprocess.DEVNULL}
        if os.name=='nt': options['creationflags']=subprocess.CREATE_NO_WINDOW|subprocess.DETACHED_PROCESS
        else: options['start_new_session']=True
        subprocess.Popen(command,**options)
        return {'ok':True,'player':status['name'],'notice':status['notice']}

    def prepare_preview(self,key,body):
        if 'retry' in body and not isinstance(body['retry'],bool): raise self.error(400,'retry 必须为布尔值。')
        with self.lib.db_lock:
            video,path=self.check(key)
            if path is None: return {'status':'processing','url':None,'job_id':None,'version':PREVIEW_VERSION}
            target=self.lib.root/'assets'/key/'preview.mp4'
            if target.is_file() and self.preview_is_current(video) and not body.get('retry'):
                return {'status':'ready','url':f'/media/{key}/preview?v={video.get("preview_revision",0)}','job_id':None,
                        'version':PREVIEW_VERSION,'info':video.get('preview_info')}
            if self.active('preview',key):
                return {'status':video.get('preview_status') if video.get('preview_status') in ('queued','processing') else 'queued',
                        'url':None,'job_id':video.get('preview_job_id'),'version':PREVIEW_VERSION}
            if (video.get('preview_status')=='error' and video.get('preview_attempt_version')==PREVIEW_VERSION
                and not body.get('retry')):
                return {'status':'error','url':None,'job_id':None,'error':video.get('preview_error'),'version':PREVIEW_VERSION}
            if not self.lib.ffmpeg or not self.lib.ffprobe: raise self.error(503,'短片预览需要 FFmpeg 和 FFprobe。')
            job=self.lib.enqueue('preview',key)
            if not job: raise self.error(429,'后台队列已满，请稍后再预览。')
            self.lib.update(key,preview_status='queued',preview_job_id=job,preview_error=None,preview_attempt_version=PREVIEW_VERSION)
            return {'status':'queued','url':None,'job_id':job,'version':PREVIEW_VERSION}

    def generate_preview(self,key):
        video,path=self.check(key)
        if path is None: raise RuntimeError('请先完成媒体分析。')
        asset=self.asset(key)
        temporary=asset/'preview.partial.mp4'
        length=min(PREVIEW_MAX_SECONDS,float(video.get('duration') or 0))
        if length<=0: raise RuntimeError('没有有效的视频时长。')
        start=max(0,min(float(video.get('cover_time') or video['duration']*.45)-.5,max(0,video['duration']-length)))
        self.lib.update(key,preview_status='processing',preview_error=None,preview_attempt_version=PREVIEW_VERSION)
        try:
            if shutil.disk_usage(self.lib.root).free<32*1024*1024: raise RuntimeError('预览缓存空间不足。')
            self.lib.run_tool([self.lib.ffmpeg,'-hide_banner','-loglevel','error','-threads','2','-ss',str(start),'-i',str(path),
                               '-t',str(length),'-map','0:v:0','-an','-vf',
                               f'scale={PREVIEW_WIDTH}:{PREVIEW_HEIGHT}:force_original_aspect_ratio=decrease,pad={PREVIEW_WIDTH}:{PREVIEW_HEIGHT}:(ow-iw)/2:(oh-ih)/2,fps={PREVIEW_FPS}',
                               '-c:v','libx264','-preset','veryfast','-threads','2','-crf','27','-pix_fmt','yuv420p',
                               '-maxrate','900k','-bufsize','1800k',
                               '-map_metadata','-1','-map_metadata:s:v','-1','-movflags','+faststart','-y',str(temporary)],timeout=90)
            current=path.stat()
            if (current.st_size,current.st_mtime_ns)!=(video['size'],video['mtime_ns']): raise RuntimeError('原片在预览期间发生变化。')
            info=self.probe(temporary)
            if (not info or info.get('codec')!='h264' or info.get('audio_codec')
                or (info.get('width'),info.get('height'))!=(PREVIEW_WIDTH,PREVIEW_HEIGHT)
                or info.get('pixel_format')!='yuv420p' or abs((info.get('fps') or 0)-PREVIEW_FPS)>.01
                or not 0<(info.get('duration') or 0)<=length+.1 or info.get('size',0)>2*1024*1024):
                raise RuntimeError('短片预览未通过格式、帧率或大小检查，未发布缓存。')
            os.replace(temporary,asset/'preview.mp4')
            self.lib.update(key,preview_status='ready',has_preview=True,preview_error=None,preview_revision=time.time_ns(),
                            preview_source=self.signature(video),preview_version=PREVIEW_VERSION,
                            preview_info=info,preview_start=start,preview_duration=length)
        except Exception as exc:
            self.lib.update(key,preview_status='error',preview_error=str(exc)[:1500])
            raise
        finally:
            with contextlib.suppress(OSError): temporary.unlink()
