import { isRecord, localMediaUrl, localPost } from './contracts';
import type { CardPreviewOptions, CardPreviewItem } from './contracts';

interface PreviewCard {
  readonly element: HTMLElement;
  readonly image: HTMLImageElement;
  readonly label: HTMLElement;
  item: CardPreviewItem;
  ratio: number;
  clip: HTMLVideoElement | null;
  clipUrl: string | null;
  clipFailed: boolean;
  policyBlocked: boolean;
  recoveryQueued: boolean;
  active: boolean;
  playing: boolean;
  scrubbing: boolean;
  readonly leave: () => void;
  readonly move: () => void;
}

/** Limit encoding work, never which part of the viewport is allowed to play. */
export function previewPreparationPolicy(): { pending: number; interval: number } {
  const device = typeof navigator === 'undefined' ? null : navigator as Navigator & { deviceMemory?: number; connection?: { saveData?: boolean } };
  const constrained = !!device && (!!device.connection?.saveData || !!device.hardwareConcurrency && device.hardwareConcurrency < 4 || !!device.deviceMemory && device.deviceMemory < 4);
  return { pending: constrained ? 1 : 2, interval: constrained ? 3200 : 2200 };
}

export class CardPreviewManager {
  private cards: PreviewCard[] = [];
  private observer: IntersectionObserver | null = null;
  private timer: number | undefined;
  private options: CardPreviewOptions = { enabled: true };
  private readonly motion = window.matchMedia('(prefers-reduced-motion: reduce)');
  private readonly requested = new Set<string>();
  private readonly pending = new Set<string>();
  private readonly requests = new Set<AbortController>();
  private readonly brokenUrls = new Map<string, string>();
  private readonly recoveryRequested = new Set<string>();
  private requestBusy = false;
  private lastRequestAt = -Infinity;
  private suspended = false;

  constructor() {
    document.addEventListener('visibilitychange', this.onVisibility);
    this.motion.addEventListener('change', this.onVisibility);
    window.addEventListener('scroll', this.onFallbackScroll, { passive: true });
    window.addEventListener('resize', this.onFallbackScroll, { passive: true });
  }

  mount(root: HTMLElement, items: readonly CardPreviewItem[], options: CardPreviewOptions): void {
    this.suspended = false;
    this.options = options;
    const itemMap = new Map(items.map(item => [item.id, item]));
    const currentIds = new Set(itemMap.keys());
    for (const id of this.pending) if (!currentIds.has(id)) this.pending.delete(id);
    for (const item of items) {
      if (item.preview_status === 'ready' || item.preview_status === 'error') this.pending.delete(item.id);
      else if (item.preview_status === 'queued' || item.preview_status === 'processing') this.pending.add(item.id);
    }
    const existing = new Map(this.cards.map(card => [card.element, card]));
    const next: PreviewCard[] = [];
    for (const element of root.querySelectorAll<HTMLElement>('.card-visual[data-open]')) {
      const item = itemMap.get(element.dataset.open || '');
      const image = element.querySelector<HTMLImageElement>('img');
      if (!item || item.kind !== 'video' || !image) continue;
      let card = existing.get(element);
      if (card && card.item.id !== item.id) { this.release(card); card = undefined; }
      if (card) { existing.delete(element); card.item = item; this.updateUrl(card); }
      else {
        const label = document.createElement('span');
        label.className = 'automatic-preview-label'; label.hidden = true; element.append(label);
        const move = (): void => {
          if (card) { card.scrubbing = true; this.pauseClip(card); card.image.style.opacity = '1'; if (card.clip) card.clip.style.opacity = '0'; this.label(card, '鼠标位置 · 近似抽样'); }
        };
        const leave = (): void => {
          if (!card) return;
          card.scrubbing = false;
          card.element.classList.remove('scrubbing');
          this.activate(card);
        };
        card = { element, image, item, label, ratio: 0, clip: null, clipUrl: null, clipFailed: false, policyBlocked: false, recoveryQueued: false, active: false, playing: false, scrubbing: false, move, leave };
        this.updateUrl(card);
        element.addEventListener('pointermove', move);
        element.addEventListener('pointerleave', leave);
        this.ensureObserver();
        this.observer?.observe(element);
      }
      next.push(card);
    }
    for (const card of existing.values()) this.release(card);
    this.cards = next;
    if (!this.observer) this.measureVisibility();
    this.reconcile();
  }

  /** Modal occlusion pauses decoders without discarding their buffered media. */
  suspend(): void {
    this.suspended = true;
    this.clearTimer();
    for (const card of this.cards) this.deactivate(card);
  }

  resetPreparation(): void { this.requested.clear(); this.pending.clear(); this.brokenUrls.clear(); this.recoveryRequested.clear(); }

  /** Navigation/page removal releases resources; ordinary scroll and polling do not. */
  stop(): void {
    this.clearTimer();
    for (const request of this.requests) request.abort();
    this.requests.clear();
    this.observer?.disconnect(); this.observer = null;
    for (const card of this.cards) this.release(card);
    this.cards = [];
  }

  private allowed(): boolean { return this.options.enabled && !this.motion.matches && !document.hidden && !this.suspended; }

  private ensureObserver(): void {
    if (this.observer || typeof IntersectionObserver === 'undefined') return;
    this.observer = new IntersectionObserver(entries => {
      for (const entry of entries) {
        const card = this.cards.find(candidate => candidate.element === entry.target);
        if (card) card.ratio = entry.isIntersecting ? entry.intersectionRatio : 0;
      }
      this.reconcile();
    }, { threshold: [0, .0001] });
  }

  private measureVisibility(): void {
    for (const card of this.cards) {
      const rect = card.element.getBoundingClientRect();
      card.ratio = rect.bottom > 0 && rect.top < window.innerHeight && rect.right > 0 && rect.left < window.innerWidth ? 1 : 0;
    }
  }

  private updateUrl(card: PreviewCard): void {
    const url = localMediaUrl(card.item.preview_url);
    const broken = !!url && this.brokenUrls.get(card.item.id) === url;
    if (url && !broken && url !== card.clipUrl) {
      this.brokenUrls.delete(card.item.id);
      card.clipFailed = false; card.policyBlocked = false; card.recoveryQueued = false;
      card.clipUrl = url;
      if (card.clip) { this.pauseClip(card); card.clip.style.opacity = '0'; card.clip.src = url; card.clip.load(); }
    } else if (broken) {
      card.clipUrl = null; card.clipFailed = true;
      card.recoveryQueued = !this.recoveryRequested.has(card.item.id);
    }
    if (!url && card.clipUrl) {
      card.clipUrl = null;
      this.pauseClip(card);
      if (card.clip) card.clip.style.opacity = '0';
      card.image.style.opacity = '1';
    }
  }

  private reconcile(): void {
    this.clearTimer();
    if (!this.allowed()) { for (const card of this.cards) this.deactivate(card); return; }
    const visible = this.cards.filter(card => card.ratio > 0 && card.element.isConnected);
    for (const card of this.cards) {
      if (card.ratio > 0 && card.element.isConnected) this.activate(card);
      else this.deactivate(card);
    }
    const recovery = visible.find(card => card.recoveryQueued && !this.recoveryRequested.has(card.item.id));
    const candidate = visible.find(card => !this.requested.has(card.item.id) && !card.clipUrl && !card.clipFailed && !['error', 'queued', 'processing'].includes(card.item.preview_status || ''));
    if (recovery) void this.requestClip(recovery, true);
    else if (candidate) void this.requestClip(candidate);
    // This timer schedules preparation only; it never flips image frames or repeats play calls.
    if (recovery || candidate) this.timer = window.setTimeout(() => { this.timer = undefined; if (!this.observer) this.measureVisibility(); this.reconcile(); }, Math.max(700, previewPreparationPolicy().interval - (performance.now() - this.lastRequestAt)));
  }

  private label(card: PreviewCard, text: string): void {
    card.label.hidden = !card.active;
    if (card.label.textContent !== text) card.label.textContent = text;
    card.element.dataset.previewState = card.clipFailed ? 'error' : card.policyBlocked ? 'blocked' : card.clipUrl ? 'ready' : card.item.preview_status || 'unavailable';
  }

  private activate(card: PreviewCard): void {
    card.active = this.allowed() && card.ratio > 0 && card.element.isConnected;
    if (!card.active) return;
    if (card.scrubbing || card.element.classList.contains('scrubbing')) { this.label(card, '鼠标位置 · 近似抽样'); return; }
    if (!card.clipUrl || card.clipFailed || card.policyBlocked) {
      const status = card.item.preview_status;
      this.label(card, card.policyBlocked ? '自动播放受限 · 点击观看' : status === 'processing' ? '短片准备中 · 静态封面' : status === 'queued' || this.pending.has(card.item.id) ? '短片已排队 · 静态封面' : card.clipFailed || status === 'error' ? '短片暂不可用 · 静态封面' : '等待短片预览 · 静态封面');
      return;
    }
    if (!card.clip) this.createClip(card);
    const clip = card.clip;
    if (!clip || card.playing || !clip.paused) return;
    card.playing = true;
    this.label(card, '正在载入静音短片');
    void clip.play().then(() => {
      card.playing = false;
      if (!card.active || !this.allowed() || card.scrubbing) { clip.pause(); return; }
      if (clip.readyState >= 2) this.reveal(card, clip);
    }).catch((error: unknown) => {
      card.playing = false;
      if (card.clip !== clip || !card.element.isConnected || card.clipFailed || !card.active || !this.allowed() || card.scrubbing) return;
      if (error instanceof Error && error.name === 'AbortError') return;
      card.policyBlocked = true;
      clip.pause(); clip.style.opacity = '0'; card.image.style.opacity = '1';
      this.label(card, '自动播放受限 · 点击观看');
    });
  }

  private createClip(card: PreviewCard): void {
    const clip = document.createElement('video');
    clip.className = 'automatic-card-preview';
    clip.muted = true; clip.loop = true; clip.playsInline = true; clip.preload = 'metadata';
    clip.disablePictureInPicture = true;
    clip.style.opacity = '0'; clip.src = card.clipUrl || '';
    clip.addEventListener('playing', () => this.reveal(card, clip));
    clip.addEventListener('loadeddata', () => { if (!clip.paused) this.reveal(card, clip); });
    clip.addEventListener('error', () => {
      if (card.clip !== clip) return;
      const failed = card.clipUrl;
      if (failed) this.brokenUrls.set(card.item.id, failed);
      card.clipFailed = true; card.clipUrl = null; card.playing = false;
      card.recoveryQueued = !this.recoveryRequested.has(card.item.id);
      this.pauseClip(card); clip.style.opacity = '0'; card.image.style.opacity = '1';
      this.label(card, '短片暂不可用 · 静态封面');
      void this.requestClip(card, true);
    });
    card.element.append(clip); card.clip = clip;
  }

  private reveal(card: PreviewCard, clip: HTMLVideoElement): void {
    if (card.clip !== clip || !card.active || !this.allowed() || card.scrubbing || clip.readyState < 2) return;
    clip.style.opacity = '1'; card.image.style.opacity = '0';
    this.label(card, '静音短片预览');
    this.recordQuality(card);
  }

  private recordQuality(card: PreviewCard): void {
    if (!card.clip || typeof card.clip.getVideoPlaybackQuality !== 'function') return;
    const quality = card.clip.getVideoPlaybackQuality();
    card.element.dataset.previewDecodedFrames = String(quality.totalVideoFrames);
    card.element.dataset.previewDroppedFrames = String(quality.droppedVideoFrames);
  }

  private pauseClip(card: PreviewCard): void { if (card.clip) { if (!card.clip.paused) card.clip.pause(); this.recordQuality(card); } }
  private deactivate(card: PreviewCard): void { card.active = false; card.label.hidden = true; this.pauseClip(card); }
  private release(card: PreviewCard): void {
    this.deactivate(card);
    this.observer?.unobserve(card.element);
    card.element.removeEventListener('pointermove', card.move); card.element.removeEventListener('pointerleave', card.leave);
    if (card.clip) { card.clip.removeAttribute('src'); card.clip.load(); card.clip.remove(); card.clip = null; }
    card.image.style.opacity = '1'; card.label.remove();
  }

  private async requestClip(card: PreviewCard, recover = false): Promise<void> {
    const policy = previewPreparationPolicy();
    if (!this.allowed() || !card.active || card.ratio <= 0 || this.requestBusy || this.pending.size >= policy.pending || performance.now() - this.lastRequestAt < policy.interval) return;
    if (recover ? !card.recoveryQueued || this.recoveryRequested.has(card.item.id) : this.requested.has(card.item.id) || card.clipUrl || card.clipFailed || ['error', 'queued', 'processing'].includes(card.item.preview_status || '')) return;
    this.requestBusy = true; this.lastRequestAt = performance.now(); this.requested.add(card.item.id);
    if (recover) { this.recoveryRequested.add(card.item.id); card.recoveryQueued = false; }
    const request = new AbortController(); this.requests.add(request);
    this.label(card, '正在请求短片 · 静态封面');
    try {
      const value = await localPost(`/api/videos/${encodeURIComponent(card.item.id)}/preview`, recover ? { retry: true } : {}, request.signal);
      if (!request.signal.aborted && isRecord(value)) {
        if (value.status === 'queued' || value.status === 'processing') { this.pending.add(card.item.id); this.label(card, '短片已排队 · 静态封面'); }
        if (value.status === 'ready') {
          const url = localMediaUrl(value.url);
          this.pending.delete(card.item.id);
          if (url && this.brokenUrls.get(card.item.id) !== url) { card.item = { ...card.item, preview_url: url, preview_status: 'ready' }; this.updateUrl(card); this.activate(card); }
        }
        if (value.status === 'error') { this.pending.delete(card.item.id); this.label(card, '短片暂不可用 · 静态封面'); }
      }
    } catch { if (!request.signal.aborted) this.label(card, '短片请求失败 · 静态封面'); }
    finally { this.requests.delete(request); this.requestBusy = false; }
  }

  private clearTimer(): void { if (this.timer !== undefined) window.clearTimeout(this.timer); this.timer = undefined; }
  private readonly onVisibility = (): void => { if (!this.observer) this.measureVisibility(); this.reconcile(); };
  private readonly onFallbackScroll = (): void => { if (!this.observer) { this.measureVisibility(); this.reconcile(); } };
}
