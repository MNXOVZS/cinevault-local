import { displayTime, errorMessage, isRecord, localPost, nearestFrame, preparationFromUnknown } from './contracts';
import type { ExperienceMedia, PlaybackMode, PlaybackPreparation, PlayerMountOptions } from './contracts';

function abortableDelay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const abort = (): void => { window.clearTimeout(timer); reject(new DOMException('Stopped', 'AbortError')); };
    const timer = window.setTimeout(() => { signal.removeEventListener('abort', abort); resolve(); }, ms);
    signal.addEventListener('abort', abort, { once: true });
    if (signal.aborted) abort();
  });
}

export class CinemaPlayer {
  private item: ExperienceMedia;
  private readonly controller = new AbortController();
  private readonly overlay = document.createElement('div');
  private readonly message = document.createElement('p');
  private readonly action = document.createElement('button');
  private readonly preview = document.createElement('div');
  private readonly previewImage = document.createElement('img');
  private readonly previewTime = document.createElement('span');
  private readonly previewMessage = document.createElement('p');
  private readonly generateFrames = document.createElement('button');
  private disposed = false;
  private preparing = false;
  private forcedFallback = false;
  private currentMode: PlaybackMode = 'original';
  private actionKind: 'play' | 'retry' | 'sound' = 'play';
  private waitingForVisibility = false;
  private hidePreviewTimer: number | undefined;
  private previewFraction: number | undefined;
  private currentFrameUrl = '';
  private requestingFrames = false;
  private generationBaseline = '';
  private generationSawActive = false;
  private generationJobId: string | null = null;
  private previewInput: HTMLInputElement | null = null;

  constructor(private readonly options: PlayerMountOptions) {
    this.item = options.item;
    this.overlay.className = 'experience-playback-overlay';
    const box = document.createElement('div');
    box.className = 'experience-playback-message';
    this.action.className = 'button primary';
    this.action.type = 'button';
    this.action.hidden = true;
    box.append(this.message, this.action);
    this.overlay.append(box);
    options.video.parentElement?.append(this.overlay);
    this.preview.className = 'seek-frame-preview';
    this.preview.id = `seek-frame-preview-${this.item.id}`;
    this.preview.setAttribute('role', 'group');
    this.preview.setAttribute('aria-label', '时间轴抽样画面');
    this.preview.hidden = true;
    this.previewImage.alt = '时间轴附近的抽样画面';
    this.previewImage.decoding = 'async';
    this.previewMessage.className = 'seek-preview-message';
    this.previewMessage.setAttribute('role', 'status');
    this.generateFrames.className = 'button ghost small seek-preview-generate';
    this.generateFrames.type = 'button';
    this.generateFrames.textContent = '生成抽样画面';
    this.preview.append(this.previewImage, this.previewTime, this.previewMessage, this.generateFrames);
    options.video.muted = false;
    options.video.playsInline = true;
    this.action.addEventListener('click', this.onAction);
    options.video.addEventListener('error', this.onVideoError);
    options.root.addEventListener('pointermove', this.onPointerMove);
    options.root.addEventListener('pointerdown', this.onPointerMove);
    options.root.addEventListener('pointerout', this.onPointerOut);
    options.root.addEventListener('pointerup', this.onPointerUp);
    options.root.addEventListener('pointercancel', this.onPointerUp);
    options.root.addEventListener('focusin', this.onSeekFocus);
    options.root.addEventListener('focusout', this.onSeekFocusOut);
    options.root.addEventListener('input', this.onSeekInput);
    this.previewImage.addEventListener('load', this.onPreviewImageLoad);
    this.previewImage.addEventListener('error', this.onPreviewImageError);
    this.generateFrames.addEventListener('click', this.onGenerateFrames);
    document.addEventListener('visibilitychange', this.onVisibility);
    void this.prepare(false, false);
  }

  update(item: ExperienceMedia): void {
    if (item.id !== this.item.id) return;
    const previousFrames = JSON.stringify(this.item.frames);
    this.item = item;
    if (this.requestingFrames) {
      const baseline = JSON.stringify([item.status, item.error]);
      const job = item.frame_analysis?.id === this.generationJobId ? item.frame_analysis : null;
      if (item.status === 'analyzing' || item.status === 'queued') this.generationSawActive = true;
      if (job && ['error', 'cancelled', 'interrupted'].includes(job.status)) {
        this.requestingFrames = false;
        this.setPreviewState('error', job.error || '这次抽样分析没有完成，可稍后重试。');
      } else if (previousFrames !== JSON.stringify(item.frames) || job?.status === 'complete') {
        this.requestingFrames = false;
        this.currentFrameUrl = '';
        if (!nearestFrame(item.frames, 0)) this.setPreviewState('error', '分析已完成，但没有可读取的抽样画面，请检查媒体情况。');
      } else if (['error', 'missing', 'changed'].includes(item.status || '') && (this.generationSawActive || baseline !== this.generationBaseline)) {
        this.requestingFrames = false;
        this.setPreviewState('error', item.error || '未能生成抽样画面，请检查原片和来源。');
      }
    }
    // The legacy timeline may replace its children after an analysis poll. Keep
    // the preview attached to the new track without replacing the video.
    if (!this.preview.hidden && this.previewFraction !== undefined) {
      const input = this.options.root.querySelector<HTMLInputElement>('#custom-seek');
      if (input) this.renderPreview(input, this.previewFraction);
    }
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.controller.abort();
    this.options.video.pause();
    if (this.hidePreviewTimer !== undefined) window.clearTimeout(this.hidePreviewTimer);
    this.action.removeEventListener('click', this.onAction);
    this.options.video.removeEventListener('error', this.onVideoError);
    this.options.root.removeEventListener('pointermove', this.onPointerMove);
    this.options.root.removeEventListener('pointerdown', this.onPointerMove);
    this.options.root.removeEventListener('pointerout', this.onPointerOut);
    this.options.root.removeEventListener('pointerup', this.onPointerUp);
    this.options.root.removeEventListener('pointercancel', this.onPointerUp);
    this.options.root.removeEventListener('focusin', this.onSeekFocus);
    this.options.root.removeEventListener('focusout', this.onSeekFocusOut);
    this.options.root.removeEventListener('input', this.onSeekInput);
    this.previewImage.removeEventListener('load', this.onPreviewImageLoad);
    this.previewImage.removeEventListener('error', this.onPreviewImageError);
    this.generateFrames.removeEventListener('click', this.onGenerateFrames);
    document.removeEventListener('visibilitychange', this.onVisibility);
    this.preview.remove();
    if (this.previewInput?.getAttribute('aria-describedby') === this.preview.id) this.previewInput.removeAttribute('aria-describedby');
    this.overlay.remove();
  }

  private show(message: string, action?: 'play' | 'retry' | 'sound'): void {
    if (this.disposed) return;
    this.overlay.hidden = false;
    this.overlay.classList.toggle('actionable', !!action);
    this.overlay.classList.toggle('sound-prompt', action === 'sound');
    this.message.textContent = message;
    this.action.hidden = !action;
    if (action) {
      this.actionKind = action;
      this.action.textContent = action === 'sound' ? '点击开启声音' : action === 'play' ? '播放（开启声音）' : '重试准备播放';
    }
    this.options.onState?.(message);
  }

  private async prepare(force: boolean, retry: boolean): Promise<void> {
    if (this.disposed || this.preparing) return;
    this.preparing = true;
    this.show(force ? '正在自动准备浏览器兼容版本，原片保留…' : '正在准备播放…');
    try {
      for (let attempt = 0; attempt < 240 && !this.disposed; attempt++) {
        const body: Record<string, unknown> = {};
        if (force) body.force_compatible = true;
        if (retry && attempt === 0) body.retry = true;
        const result = preparationFromUnknown(await localPost(`/api/videos/${encodeURIComponent(this.item.id)}/prepare-playback`, body, this.controller.signal));
        if (this.disposed) return;
        if (result.status === 'ready') {
          this.preparing = false;
          await this.useSource(result);
          return;
        }
        if (result.status === 'error') {
          this.show(result.notice || '准备播放失败。请检查原片与媒体引擎后重试。', 'retry');
          return;
        }
        this.show(result.notice || (result.status === 'queued' ? '兼容播放已排队；准备完成后会自动尝试播放。' : '正在准备兼容播放，原片不会被覆盖…'));
        await abortableDelay(1500, this.controller.signal);
      }
      if (!this.disposed) this.show('准备仍未完成。可以稍后重试，后台任务会保留。', 'retry');
    } catch (error: unknown) {
      if (!this.disposed) this.show(errorMessage(error), 'retry');
    } finally { this.preparing = false; }
  }

  private async useSource(result: PlaybackPreparation): Promise<void> {
    if (!result.url || this.disposed) return;
    const video = this.options.video;
    const position = video.readyState >= 1 && video.currentTime > 0 ? video.currentTime : this.item.last_position || 0;
    this.currentMode = result.mode;
    video.dataset.playbackMode = result.mode;
    if (video.getAttribute('src') !== result.url) {
      video.addEventListener('loadedmetadata', () => {
        if (!this.disposed && position > 0 && Number.isFinite(video.duration) && position < video.duration - 1) video.currentTime = position;
      }, { once: true });
      video.src = result.url;
      video.load();
    }
    this.options.onPrepared?.(result);
    if (document.hidden) {
      this.waitingForVisibility = true;
      this.show('播放已准备完成，返回此页面后会尝试播放。');
      return;
    }
    await this.attemptPlay();
  }

  private async attemptPlay(): Promise<void> {
    if (this.disposed) return;
    const video = this.options.video;
    video.muted = false;
    try {
      await video.play();
      if (!this.disposed) { this.overlay.hidden = true; this.action.hidden = true; }
    } catch (error: unknown) {
      if (this.disposed) return;
      if (error instanceof DOMException && error.name === 'NotAllowedError' || error instanceof Error && error.name === 'NotAllowedError') {
        // Browsers may allow muted playback after an asynchronous compatibility preparation.
        // A later explicit click enables sound; no policy bypass or native window is attempted.
        video.muted = true;
        try {
          await video.play();
          if (!this.disposed) this.show('已静音播放', 'sound');
        } catch (mutedError: unknown) {
          if (this.disposed) return;
          if (mutedError instanceof Error && mutedError.name === 'NotSupportedError' || video.error?.code === 4) this.recoverDecode();
          else this.show('浏览器需要一次点击才能播放。', 'play');
        }
      } else if (error instanceof Error && error.name === 'NotSupportedError' || video.error?.code === 4) {
        this.recoverDecode();
      } else { this.show('当前流暂时无法播放，可重试准备。' + (errorMessage(error) ? ` ${errorMessage(error)}` : ''), 'retry'); }
    }
  }

  private recoverDecode(): void {
    if (this.disposed || this.preparing) return;
    if (!this.forcedFallback && this.currentMode !== 'transcode') {
      this.forcedFallback = true;
      this.options.video.pause();
      void this.prepare(true, false);
    } else { this.show('此浏览器仍无法播放当前流。可重试准备；原片保持原状。', 'retry'); }
  }

  private readonly onAction = (): void => {
    this.action.hidden = true;
    if (this.actionKind === 'play' || this.actionKind === 'sound') void this.attemptPlay();
    else void this.prepare(this.forcedFallback, true);
  };

  private readonly onVideoError = (): void => {
    if (!this.preparing && this.options.video.error?.code !== 1) this.recoverDecode();
  };

  private readonly onVisibility = (): void => {
    if (!document.hidden && this.waitingForVisibility) { this.waitingForVisibility = false; void this.attemptPlay(); }
  };

  private readonly onPointerMove = (event: PointerEvent): void => {
    if (!(event.target instanceof Element)) return;
    if (event.target.closest('.seek-frame-preview')) return;
    const track = event.target.closest<HTMLElement>('.custom-seek');
    const input = event.target.closest<HTMLInputElement>('#custom-seek') || track?.querySelector<HTMLInputElement>('#custom-seek');
    if (!input) return;
    const rect = input.getBoundingClientRect();
    if (!rect.width) return;
    const fraction = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
    this.renderPreview(input, fraction);
  };

  private renderPreview(input: HTMLInputElement, fraction: number): void {
    if (this.disposed) return;
    const parent = input.closest<HTMLElement>('.custom-seek');
    if (!parent) return;
    if (this.hidePreviewTimer !== undefined) window.clearTimeout(this.hidePreviewTimer);
    this.hidePreviewTimer = undefined;
    this.previewFraction = fraction;
    const duration = Number.isFinite(this.options.video.duration) && this.options.video.duration > 0 ? this.options.video.duration : this.item.duration;
    const target = fraction * Math.max(0, duration);
    const frame = nearestFrame(this.item.frames, target);
    if (this.preview.parentElement !== parent) parent.append(this.preview);
    input.setAttribute('aria-describedby', this.preview.id);
    this.previewInput = input;
    this.preview.hidden = false;
    if (frame) {
      this.previewTime.textContent = `${displayTime(target)} · 近似抽样 ${displayTime(frame.time)}`;
      if (this.currentFrameUrl !== frame.url) {
        this.currentFrameUrl = frame.url;
        this.setPreviewState('loading', '正在读取抽样画面…');
        this.previewImage.src = frame.url;
        if (this.previewImage.complete && this.previewImage.naturalWidth > 0) this.onPreviewImageLoad();
      }
    } else {
      this.currentFrameUrl = '';
      this.previewImage.removeAttribute('src');
      this.previewTime.textContent = displayTime(target);
      if (this.preview.dataset.state !== 'error' || this.requestingFrames) this.setPreviewState(this.requestingFrames ? 'queued' : 'empty', this.requestingFrames ? '抽样画面正在后台准备。' : '尚无抽样画面，可重新分析生成。');
    }
    // Anchor to the actual track, including padding and narrow phone layouts.
    const rect = input.getBoundingClientRect();
    const parentRect = parent.getBoundingClientRect();
    const width = this.preview.offsetWidth || 176;
    const x = rect.left - parentRect.left + fraction * rect.width;
    const left = parentRect.width <= width ? parentRect.width / 2 : Math.max(width / 2, Math.min(parentRect.width - width / 2, x));
    this.preview.style.left = `${left}px`;
  }

  private setPreviewState(state: 'loading' | 'ready' | 'empty' | 'error' | 'queued', message = ''): void {
    this.preview.dataset.state = state;
    this.previewImage.hidden = state !== 'ready';
    this.previewMessage.hidden = state === 'ready';
    this.previewMessage.textContent = message;
    this.generateFrames.hidden = state !== 'empty' && state !== 'error';
    this.generateFrames.disabled = this.requestingFrames;
  }

  private readonly onPreviewImageLoad = (): void => {
    if (!this.disposed && !this.requestingFrames && this.currentFrameUrl) this.setPreviewState('ready');
  };

  private readonly onPreviewImageError = (): void => {
    if (!this.disposed && !this.requestingFrames && this.currentFrameUrl) this.setPreviewState('error', '抽样画面读取失败，可重新分析修复。');
  };

  private readonly onGenerateFrames = (event: MouseEvent): void => {
    event.preventDefault();
    event.stopPropagation();
    if (this.requestingFrames || this.disposed) return;
    this.requestingFrames = true;
    this.generationJobId = null;
    this.generationBaseline = JSON.stringify([this.item.status, this.item.error]);
    this.generationSawActive = false;
    this.setPreviewState('queued', '正在提交画面分析…');
    void localPost(`/api/videos/${encodeURIComponent(this.item.id)}/analyze`, {}, this.controller.signal).then((result: unknown) => {
      if (this.disposed) return;
      if (!isRecord(result) || typeof result.job_id !== 'string' || !result.job_id) throw new Error('分析已在队列中，或后台任务已满，请稍后查看或重试。');
      this.generationJobId = result.job_id;
      if (this.requestingFrames) this.setPreviewState('queued', '已加入画面分析队列，完成后自动显示。');
      this.options.onState?.('抽样画面分析已加入队列。');
    }).catch((error: unknown) => {
      this.requestingFrames = false;
      if (!this.disposed) this.setPreviewState('error', errorMessage(error));
    });
  };

  private readonly onSeekFocus = (event: FocusEvent): void => {
    if (!(event.target instanceof Element)) return;
    const input = event.target.closest<HTMLInputElement>('#custom-seek');
    if (!input) return;
    const max = Number(input.max) || this.item.duration || 1;
    this.renderPreview(input, Math.min(1, Math.max(0, Number(input.value) / max || 0)));
  };

  private readonly onSeekInput = (event: Event): void => { this.onSeekFocus(event as FocusEvent); };

  private readonly onSeekFocusOut = (event: FocusEvent): void => {
    const next = event.relatedTarget;
    if (!(next instanceof Element) || next.closest('.custom-seek') !== this.preview.parentElement) this.preview.hidden = true;
  };

  private readonly onPointerOut = (event: PointerEvent): void => {
    if (!(event.target instanceof Element)) return;
    const parent = event.target.closest('.custom-seek');
    if (!parent) return;
    const next = event.relatedTarget;
    if (!(next instanceof Element) || next.closest('.custom-seek') !== parent) this.preview.hidden = true;
  };

  private readonly onPointerUp = (event: PointerEvent): void => {
    if (event.type !== 'pointercancel' && event.pointerType !== 'touch' && event.pointerType !== 'pen') return;
    if (this.hidePreviewTimer !== undefined) window.clearTimeout(this.hidePreviewTimer);
    this.hidePreviewTimer = window.setTimeout(() => { this.preview.hidden = true; }, 1500);
  };
}
