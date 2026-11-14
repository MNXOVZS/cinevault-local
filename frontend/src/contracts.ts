export type MediaKind = 'video' | 'image';
export type PlaybackMode = 'original' | 'remux' | 'transcode';
export type PreparationStatus = 'ready' | 'queued' | 'processing' | 'error';

export interface SampleFrame {
  readonly time: number;
  readonly url: string;
}

export interface ExperienceMedia {
  readonly id: string;
  readonly kind: MediaKind;
  readonly title: string;
  readonly duration: number;
  readonly status?: string;
  readonly error?: string | null;
  readonly frame_analysis?: { readonly id: string; readonly status: string; readonly error?: string | null } | null;
  readonly cover?: string | null;
  readonly frames: readonly SampleFrame[];
  readonly last_position?: number;
  readonly playback_status?: string;
  readonly playback_url?: string | null;
  readonly playback_mode?: PlaybackMode;
  readonly preview_url?: string | null;
  readonly preview_status?: string;
}

export interface PlaybackPreparation {
  readonly status: PreparationStatus;
  readonly url: string | null;
  readonly mode: PlaybackMode;
  readonly notice: string;
}

export interface PlayerMountOptions {
  readonly root: HTMLElement;
  readonly video: HTMLVideoElement;
  readonly item: ExperienceMedia;
  readonly onPrepared?: (result: PlaybackPreparation) => void;
  readonly onState?: (message: string) => void;
}

export type CardPreviewItem = Pick<ExperienceMedia, 'id' | 'kind' | 'preview_status' | 'preview_url'>;

export interface CardPreviewOptions {
  readonly enabled: boolean;
}

export interface ExperienceBridge {
  mountPlayer(options: PlayerMountOptions): void;
  syncPlayer(item: ExperienceMedia): void;
  unmountPlayer(): void;
  syncCards(root: HTMLElement, items: readonly CardPreviewItem[], options: CardPreviewOptions): void;
  stopCards(): void;
  suspendCards(): void;
  mountAnalysis(root: HTMLElement, onMessage?: (message: string, error: boolean) => void): void;
  refreshAnalysis(): void;
  unmountAnalysis(): void;
  mountManagement(root: HTMLElement, onMessage?: (message: string, error: boolean) => void): void;
  unmountManagement(): void;
  mountCache(root: HTMLElement, onMessage?: (message: string, error: boolean) => void): void;
  unmountCache(): void;
  mountHealth(root: HTMLElement, options: import('./library-health').HealthMountOptions): void;
  refreshHealth(): void;
  unmountHealth(): void;
}

declare global {
  interface Window {
    CineVaultExperience?: ExperienceBridge;
  }
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function localMediaUrl(value: unknown): string | null {
  return typeof value === 'string' && /^\/(?:media|assets)\//.test(value) && !value.startsWith('//') ? value : null;
}

export function preparationFromUnknown(value: unknown): PlaybackPreparation {
  if (!isRecord(value)) throw new Error('播放准备返回了无效数据。');
  const status = value.status;
  if (status !== 'ready' && status !== 'queued' && status !== 'processing' && status !== 'error') {
    throw new Error('播放准备状态无法识别，请重试。');
  }
  const mode: PlaybackMode = value.mode === 'remux' || value.mode === 'transcode' ? value.mode : 'original';
  const url = localMediaUrl(value.url);
  if (status === 'ready' && !url) throw new Error('播放地址尚未就绪。');
  const notice = typeof value.notice === 'string' ? value.notice : typeof value.error === 'string' ? value.error : '';
  return { status, mode, url, notice };
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : '本地服务暂时无法完成操作。';
}

export async function localPost(path: string, body: Readonly<Record<string, unknown>>, signal?: AbortSignal): Promise<unknown> {
  if (!path.startsWith('/api/')) throw new Error('仅允许请求本机资料库接口。');
  const response = await fetch(path, {
    method: 'POST', credentials: 'same-origin', signal,
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  const value: unknown = await response.json();
  if (!response.ok) {
    throw new Error(isRecord(value) && typeof value.error === 'string' ? value.error : `本机请求失败（${response.status}）`);
  }
  return value;
}

export function displayTime(seconds: number): string {
  const value = Math.max(0, Math.floor(Number.isFinite(seconds) ? seconds : 0));
  const hours = Math.floor(value / 3600);
  return `${hours ? `${hours}:` : ''}${String(Math.floor(value % 3600 / 60)).padStart(2, '0')}:${String(value % 60).padStart(2, '0')}`;
}

export function nearestFrame(frames: readonly SampleFrame[], seconds: number): SampleFrame | null {
  let selected: SampleFrame | null = null;
  let distance = Infinity;
  for (const frame of frames) {
    if (!Number.isFinite(frame.time) || !localMediaUrl(frame.url)) continue;
    const candidate = Math.abs(frame.time - seconds);
    if (candidate < distance) { selected = frame; distance = candidate; }
  }
  return selected;
}
