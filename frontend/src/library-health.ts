import { errorMessage, isRecord, localPost } from './contracts';

export interface HealthScope { source_id: string | null; folder: string; recursive: boolean }
export interface HealthMountOptions {
  readonly mode: 'compact' | 'sources';
  readonly onMessage?: (message: string, error: boolean) => void;
  readonly onOpenVideo?: (id: string) => void;
  readonly onNavigateSources?: () => void;
}
type Counts = Readonly<Record<string, number>>;
interface PreviewCounts extends Counts { total: number; ready: number; queued: number; processing: number; pending: number; failed: number; unprepared: number; blocked: number; stale: number }
interface MediaCounts extends Counts { total: number; videos: number; images: number; ready: number; analyzing: number; failed: number; missing: number; changed: number; needs_attention: number }
export interface HealthSummary {
  scope: HealthScope; media: MediaCounts; preview: PreviewCounts; revision: number;
  eta: { seconds: number | null; lower_seconds: number | null; upper_seconds: number | null; sample_count: number; reason: string };
  current: { kind: string; title: string; elapsed_seconds: number; in_scope: boolean } | null;
  shared_queue: { queued: number; running: number; other_jobs: number };
}
interface SourceRow { id: string; label: string; path: string; source_status: string; error: string; media: MediaCounts; preview: PreviewCounts; unsupported: readonly { extension: string; count: number }[]; unsupported_files: number }
interface FolderRow { folder: string; label: string; media: MediaCounts; preview: PreviewCounts }
interface IssueRow { id: string; title: string; filename: string; relative_folder: string; status: string; preview_status: string; codec: string; container: string; issue_codes: readonly string[]; diagnostic: Readonly<Record<string, unknown>>; actions: Readonly<Record<string, unknown>> }
interface Page<T> { items: readonly T[]; total: number; limit: number; offset: number }

const escape = (value: string): string => value.replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char] || char);
const string = (value: unknown, fallback = ''): string => typeof value === 'string' ? value : fallback;
const nonnegative = (value: unknown): number => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : 0;
function numberField(value: Record<string, unknown>, name: string): number {
  const result = value[name];
  if (typeof result !== 'number' || !Number.isFinite(result) || result < 0) throw new Error('统计数据尚未完整返回，请稍后重试。');
  return result;
}
function counts<T extends Counts>(value: unknown, names: readonly string[]): T {
  if (!isRecord(value)) throw new Error('统计数据格式不完整，不能据此判断没有问题。');
  return Object.fromEntries(names.map(name => [name, numberField(value, name)])) as T;
}
const previewKeys = ['total', 'ready', 'queued', 'processing', 'pending', 'failed', 'unprepared', 'blocked', 'stale'];
const mediaKeys = ['total', 'videos', 'images', 'ready', 'analyzing', 'failed', 'missing', 'changed', 'needs_attention'];
function scopeFromUnknown(value: unknown): HealthScope {
  if (!isRecord(value) || !(value.source_id === null || typeof value.source_id === 'string') || typeof value.folder !== 'string' || typeof value.recursive !== 'boolean') throw new Error('来源范围无法识别。');
  return { source_id: value.source_id, folder: value.folder, recursive: value.recursive };
}
function nullableSeconds(value: unknown): number | null { return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null; }
export function healthFromUnknown(value: unknown): HealthSummary {
  if (!isRecord(value) || !isRecord(value.eta) || !isRecord(value.shared_queue)) throw new Error('进度尚未完整返回。');
  const media = counts<MediaCounts>(value.media, mediaKeys), preview = counts<PreviewCounts>(value.preview, previewKeys);
  if (preview.ready + preview.queued + preview.processing + preview.pending + preview.failed + preview.unprepared + preview.blocked !== preview.total) throw new Error('进度数量暂不一致，请刷新后查看。');
  return { scope: scopeFromUnknown(value.scope), media, preview, revision: nonnegative(value.revision),
    eta: { seconds: nullableSeconds(value.eta.seconds), lower_seconds: nullableSeconds(value.eta.lower_seconds), upper_seconds: nullableSeconds(value.eta.upper_seconds), sample_count: nonnegative(value.eta.sample_count), reason: string(value.eta.reason) },
    current: isRecord(value.current) ? { kind: string(value.current.kind), title: string(value.current.title), elapsed_seconds: nonnegative(value.current.elapsed_seconds), in_scope: value.current.in_scope === true } : null,
    shared_queue: { queued: numberField(value.shared_queue, 'queued'), running: numberField(value.shared_queue, 'running'), other_jobs: numberField(value.shared_queue, 'other_jobs') } };
}
function pageFromUnknown<T>(value: unknown, parse: (entry: Record<string, unknown>) => T): Page<T> {
  if (!isRecord(value) || !Array.isArray(value.items) || value.items.some(entry => !isRecord(entry))) throw new Error('清单尚未完整返回。');
  return { items: value.items.filter(isRecord).map(parse), total: numberField(value, 'total'), offset: numberField(value, 'offset'), limit: numberField(value, 'limit') };
}
function sourceFromUnknown(entry: Record<string, unknown>): SourceRow {
  const scan = isRecord(entry.scan) ? entry.scan : {};
  return { id: string(entry.id), label: string(entry.label, '未命名来源'), path: string(entry.path), source_status: string(entry.source_status, 'unscanned'), error: string(entry.error),
    media: counts<MediaCounts>(entry.media, mediaKeys), preview: counts<PreviewCounts>(entry.preview, previewKeys), unsupported_files: nonnegative(scan.unsupported_files),
    unsupported: Array.isArray(scan.unsupported_extensions) ? scan.unsupported_extensions.filter(isRecord).map(item => ({ extension: string(item.extension), count: nonnegative(item.count) })) : [] };
}
function folderFromUnknown(entry: Record<string, unknown>): FolderRow { return { folder: string(entry.folder), label: string(entry.label), media: counts<MediaCounts>(entry.media, mediaKeys), preview: counts<PreviewCounts>(entry.preview, previewKeys) }; }
function issueFromUnknown(entry: Record<string, unknown>): IssueRow {
  return { id: string(entry.id), title: string(entry.title, '未命名资料'), filename: string(entry.filename), relative_folder: string(entry.relative_folder), status: string(entry.status), preview_status: string(entry.preview_status), codec: string(entry.codec), container: string(entry.container),
    issue_codes: Array.isArray(entry.issue_codes) ? entry.issue_codes.filter((item): item is string => typeof item === 'string') : [], diagnostic: isRecord(entry.diagnostic) ? entry.diagnostic : {}, actions: isRecord(entry.actions) ? entry.actions : {} };
}
const kindNames: Readonly<Record<string, string>> = { analyze: '读取资料与抽帧', preview: '生成静音短片', timeline: '分析时间轴', classify: '本机分类', describe: '模型概述', transcode: '准备兼容播放', verify: '校验原片', cover: '生成封面', scan: '扫描来源' };
const issueNames: Readonly<Record<string, string>> = { analysis_failed: '基础分析失败', analysis_error: '基础分析失败', analysis_queued: '等待基础分析', analysis_analyzing: '正在基础分析', analysis_missing: '原片无法访问', analysis_changed: '原片发生变化', no_cover: '封面未生成', preview_failed: '预览生成失败', preview_unprepared: '预览尚未准备', preview_stale: '旧预览等待更新', preview_queued: '预览排队中', preview_processing: '预览正在生成', preview_pending: '预览等待入队', preview_blocked: '原片暂不可用于预览', missing: '原片无法访问', source_offline: '来源离线', source_offline_or_changed: '来源离线或设备身份变化', changed: '原片发生变化', playback_failed: '兼容播放准备失败', playback_unverified: '浏览器播放尚未验证', playback_conversion_needed: '需要兼容播放副本' };
const elapsed = (seconds: number): string => seconds >= 3600 ? `${(seconds / 3600).toFixed(1)} 小时` : seconds >= 60 ? `${Math.ceil(seconds / 60)} 分钟` : `${Math.ceil(seconds)} 秒`;
export function etaLabel(eta: HealthSummary['eta']): string {
  if (eta.seconds === null) return eta.reason || '尚无可靠耗时样本，暂不能估时。';
  const range = eta.lower_seconds !== null && eta.upper_seconds !== null && eta.lower_seconds !== eta.upper_seconds ? `${elapsed(eta.lower_seconds)}–${elapsed(eta.upper_seconds)}` : `约 ${elapsed(eta.seconds)}`;
  return `估计剩余 ${range}（${eta.sample_count} 个实际完成样本）。${eta.reason}`;
}
export function healthProgressMarkup(summary: HealthSummary, compact = false): string {
  const p = summary.preview, total = p.total, percent = total ? Math.min(100, p.ready / total * 100) : 0, failedPercent = total ? Math.min(100 - percent, p.failed / total * 100) : 0;
  const remaining = p.queued + p.processing + p.pending;
  if (compact) {
    const counts = [[p.processing, '生成中'], [p.queued + p.pending, '等待'], [p.unprepared, '待准备'], [p.failed, '预览失败'], [p.blocked, '原片待处理']] as const;
    const notes = counts.filter(([count]) => count > 0).map(([count, label]) => `<span>${label} ${count}</span>`).join('') || `<span>${total ? '全部已准备' : '尚无视频'}</span>`;
    return `<div class="health-progress-heading"><div><strong>静音预览</strong><span>${p.ready} / ${total} 个视频已就绪</span></div><span class="health-progress-percent">${Math.round(percent)}%</span></div><div class="health-progress-track" role="progressbar" aria-label="预览已就绪比例" aria-valuemin="0" aria-valuemax="${total || 1}" aria-valuenow="${p.ready}" aria-valuetext="${p.ready} / ${total} 个视频已就绪"><i style="width:${percent}%"></i><b style="width:${failedPercent}%"></b></div><div class="health-progress-brief">${notes || '<span>全部已准备</span>'}<details class="inline-help"><summary>进度说明</summary><div class="inline-help-copy"><p>${escape(etaLabel(summary.eta))}</p><p>就绪 ${p.ready}，生成 ${p.processing}，排队 ${p.queued}，等待入队 ${p.pending}，待准备 ${p.unprepared}，失败 ${p.failed}，原片或基础分析待处理 ${p.blocked}。${p.stale ? `旧缓存待更新 ${p.stale}。` : ''}</p>${summary.current ? `<p>当前：${escape(kindNames[summary.current.kind] || '后台任务')} · 已运行 ${elapsed(summary.current.elapsed_seconds)}。</p>` : ''}<p>按最近扫描与任务记录统计；离线、变化、分析失败和完整播放验证可在来源中查看。</p></div></details></div>`;
  }
  return `<div class="health-progress-heading"><div><strong>${compact ? '全资料库静音预览进度' : '当前范围的预览准备'}</strong><span>${p.ready} / ${total} 个视频已就绪</span></div><span class="health-progress-percent">${Math.round(percent)}%</span></div><div class="health-progress-track" role="progressbar" aria-label="预览已就绪比例" aria-valuemin="0" aria-valuemax="${total || 1}" aria-valuenow="${p.ready}" aria-valuetext="${p.ready} / ${total} 个视频已就绪"><i style="width:${percent}%"></i><b style="width:${failedPercent}%"></b></div><div class="health-progress-counts"><span class="good">已就绪 <b>${p.ready}</b></span><span>生成中 <b>${p.processing}</b></span><span>排队 <b>${p.queued}</b></span><span>等待入队 <b>${p.pending}</b></span><span>尚未准备 <b>${p.unprepared}</b>${p.stale ? `（旧缓存 ${p.stale}）` : ''}</span><span class="bad">失败 <b>${p.failed}</b></span><span class="warn">基础 / 原片未就绪 <b>${p.blocked}</b></span></div><p class="health-eta">${escape(etaLabel(summary.eta))}${remaining ? '' : ' 当前没有正在处理的预览任务。'}${p.unprepared ? ' 未准备的项目需要提交准备，或等进入视口时按需生成。' : ''}${p.blocked ? ' 基础 / 原片未就绪包含基础分析中、分析失败或离线/变更，具体原因见下方状态。' : ''}</p>${summary.current ? `<p class="health-current">当前${summary.current.in_scope ? '' : '其他范围'}：${escape(kindNames[summary.current.kind] || '后台任务')}${summary.current.title ? ` · ${escape(summary.current.title)}` : ''} · 已运行 ${elapsed(summary.current.elapsed_seconds)}。${summary.current.kind !== 'preview' ? '共享队列正处理其他工作，预览仍需等待。' : ''}</p>` : ''}${!compact ? `<div class="health-media-counts">已登记 ${summary.media.total} 项（视频 ${summary.media.videos} / 图片 ${summary.media.images}） · 基础资料就绪 ${summary.media.ready} · 分析中 ${summary.media.analyzing} · 分析失败 ${summary.media.failed} · 缺失/离线 ${summary.media.missing} · 内容变化 ${summary.media.changed}</div>` : ''}`;
}

export class LibraryHealthManager {
  private scope: HealthScope = { source_id: null, folder: '', recursive: true };
  private displayedScope: HealthScope | null = null;
  private scopeLoading = false;
  private disposed = false;
  private readonly requests = new Set<AbortController>();
  private refreshing = false;
  private lastRevision: number | null = null;
  private sequence = 0;
  private sourceOffset = 0;
  private folderOffset = 0;
  private issueOffset = 0;
  private issueKind = 'all';
  private sourcePage: Page<SourceRow> | null = null;
  private selectedSource: SourceRow | null = null;
  private folderPage: Page<FolderRow> | null = null;
  private issuePage: Page<IssueRow> | null = null;
  private readonly html = new Map<string, string>();

  constructor(private readonly root: HTMLElement, private readonly options: HealthMountOptions) {
    root.addEventListener('click', this.onClick); root.addEventListener('submit', this.onSubmit); root.addEventListener('change', this.onChange);
    this.renderShell(); void this.refresh(true);
  }
  dispose(): void { this.disposed = true; for (const request of this.requests) request.abort(); this.requests.clear(); this.root.removeEventListener('click', this.onClick); this.root.removeEventListener('submit', this.onSubmit); this.root.removeEventListener('change', this.onChange); }
  private setHTML(selector: string, value: string): void { if (this.html.get(selector) === value) return; const node = this.root.querySelector(selector); if (node) { node.innerHTML = value; this.html.set(selector, value); } }
  private renderShell(): void {
    if (this.options.mode === 'compact') {
      this.root.innerHTML = '<section class="preview-progress-compact panel"><div data-health-summary><p class="hint">正在读取预览进度…</p></div><button class="button small ghost" data-health-navigate title="按硬盘或文件夹查看预览与识别情况">查看状态</button></section>'; return;
    }
    this.root.innerHTML = `<section class="panel health-dashboard"><div class="panel-header"><div><h2>来源与预览状态</h2><p class="hint">来自已登记片目和最近扫描记录，不会因打开此页重新读取全盘。</p></div><div class="button-row"><button class="button small ghost" data-health-capabilities>查看本机解码能力</button><button class="button small ghost" data-health-refresh>刷新状态</button></div></div><div data-health-capabilities-result></div><div data-health-sources>正在读取各来源…</div><form data-health-scope class="health-scope-form"><div class="field"><label for="health-source">来源范围</label><select id="health-source" name="source_id"><option value="">全部来源（含库内）</option></select></div><div class="field"><label for="health-folder">相对文件夹</label><input id="health-folder" name="folder" placeholder="留空为来源根目录，例如 动画/旧片"></div><label class="check-row"><input type="checkbox" name="recursive" checked>包含下级文件夹</label><button class="button primary" type="submit">查看此范围</button><button class="button ghost" type="button" data-health-root>返回来源根目录</button></form><div class="health-scope-caption" data-health-caption>全部已登记来源</div><div data-health-summary>正在读取预览进度…</div><div class="button-row health-batch-actions"><button class="button primary" data-health-prepare>准备此范围缺少的预览</button><button class="button ghost" data-health-retry>明确重试失败的预览</button></div><p class="hint">只生成可再生静音短片，不改动或移动原片。任务较多时先保存待处理记录，再分批进入共享队列。</p><div data-health-result role="status"></div></section><section class="panel"><div class="panel-header"><h2>文件夹明细</h2><span class="hint">按完整相对路径列出；点击选择，可包含下级</span></div><div data-health-folders>正在读取文件夹…</div></section><section class="panel"><div class="panel-header"><h2>识别与播放情况</h2><label class="health-issue-kind">显示范围<select aria-label="识别情况筛选" data-health-issue-kind><option value="all">实际异常</option><option value="analysis">基础分析异常</option><option value="preview">所有预览准备状态</option><option value="playback">播放兼容与未验证</option><option value="source">来源 / 原片不可用</option></select></label></div><p class="hint">“尚未准备”“浏览器未验证”不等于失败。读取参数成功，也不保证浏览器可以直接播放；每条资料的依据和可用操作分别显示。</p><div data-health-issues>正在读取清单…</div></section>`;
  }
  private query(extra: Readonly<Record<string, string>> = {}): string { const params = new URLSearchParams({ folder: this.scope.folder, recursive: this.scope.recursive ? '1' : '0', ...extra }); if (this.scope.source_id) params.set('source_id', this.scope.source_id); return params.toString(); }
  private async get(path: string): Promise<unknown> {
    const controller = new AbortController(); this.requests.add(controller);
    try { const response = await fetch(path, { credentials: 'same-origin', signal: controller.signal }); const value: unknown = await response.json(); if (!response.ok) throw new Error(isRecord(value) && typeof value.error === 'string' ? value.error : '来源统计暂时无法读取。'); return value; }
    finally { this.requests.delete(controller); }
  }
  private async loadCapabilities(button: HTMLButtonElement): Promise<void> {
    button.disabled = true;
    this.setHTML('[data-health-capabilities-result]', '<div class="notice">正在查询本机媒体引擎的版本与编译能力；不会读取原片。</div>');
    try {
      const value = await this.get('/api/media-capabilities');
      if (!isRecord(value) || !Array.isArray(value.video_decoders) || !Array.isArray(value.audio_decoders) || !Array.isArray(value.demuxers)) throw new Error('媒体引擎未返回完整能力清单。');
      const lists = [
        { name: '视频解码器', values: value.video_decoders },
        { name: '音频解码器', values: value.audio_decoders },
        { name: '容器读取格式', values: value.demuxers },
      ].map(list => ({ ...list, values: list.values.filter((entry): entry is string => typeof entry === 'string') }));
      const errors = Array.isArray(value.errors) ? value.errors.filter((entry): entry is string => typeof entry === 'string') : [];
      const version = string(value.ffmpeg_version, value.ffmpeg_available === true ? 'FFmpeg 未返回版本' : '当前未找到 FFmpeg');
      if (!this.disposed) this.setHTML('[data-health-capabilities-result]', `<section class="notice health-capabilities"><strong>当前本机媒体引擎</strong><p><code>${escape(version)}</code></p><p>${lists.map(list => `${list.name} ${list.values.length} 项`).join(' · ')}${value.cached === true ? ' · 使用一小时内缓存' : ''}</p><p>${escape(string(value.notice, '编译能力清单不代表某个文件已完整解码，或浏览器一定可以播放。'))}</p>${errors.length ? `<p class="health-source-error">${errors.map(escape).join('<br>')}</p>` : ''}${lists.map(list => `<details><summary>${list.name}名单（${list.values.length}）</summary><p><code>${list.values.length ? list.values.map(escape).join(' · ') : '本次没有返回名单；请检查工具状态与错误。'}</code></p></details>`).join('')}</section>`);
    } catch (error: unknown) {
      if (!this.disposed) this.setHTML('[data-health-capabilities-result]', `<div class="notice error">无法查询本机解码能力：${escape(errorMessage(error))}。可在运行映库的电脑本机重试；未据此判断原片不受支持。</div>`);
    } finally { if (button.isConnected) button.disabled = false; }
  }
  async refresh(force = false): Promise<void> {
    if (this.disposed || this.refreshing) return;
    this.refreshing = true; const sequence = this.sequence;
    try {
      const summary = healthFromUnknown(await this.get('/api/library-health?' + this.query()));
      if (this.disposed || sequence !== this.sequence) return;
      this.displayedScope = summary.scope;
      this.scopeLoading = false;
      this.setHTML('[data-health-summary]', healthProgressMarkup(summary, this.options.mode === 'compact'));
      this.updateScopeCaption();
      if (this.options.mode === 'sources' && (force || summary.revision !== this.lastRevision)) await this.loadLists(sequence);
      if (sequence === this.sequence) this.lastRevision = summary.revision;
    } catch (error: unknown) { if (!this.disposed && sequence === this.sequence) { this.scopeLoading = false; this.updateScopeCaption(); this.setHTML('[data-health-summary]', `<div class="notice error">${escape(errorMessage(error))} 统计失败不代表零异常。<button class="button small ghost" data-health-refresh>重试读取</button></div>`); } }
    finally { this.refreshing = false; if (!this.disposed && sequence !== this.sequence) void this.refresh(true); }
  }
  private async loadLists(sequence: number): Promise<void> {
    const values = await Promise.allSettled([
      this.get('/api/library-health/sources?' + new URLSearchParams({ limit: '12', offset: String(this.sourceOffset) }).toString()),
      this.get('/api/library-health/folders?' + this.query({ limit: '12', offset: String(this.folderOffset) })),
      this.get('/api/library-health/issues?' + this.query({ kind: this.issueKind, limit: '20', offset: String(this.issueOffset) })),
    ]);
    if (this.disposed || sequence !== this.sequence) return;
    const [sources, folders, issues] = values;
    try { if (sources?.status !== 'fulfilled') throw sources?.status === 'rejected' ? sources.reason : new Error('来源未返回'); this.sourcePage = pageFromUnknown(sources.value, sourceFromUnknown); this.renderSources(); } catch (error: unknown) { this.setHTML('[data-health-sources]', `<div class="notice error">来源清单读取失败：${escape(errorMessage(error))}</div>`); }
    try { if (folders?.status !== 'fulfilled') throw folders?.status === 'rejected' ? folders.reason : new Error('文件夹未返回'); this.folderPage = pageFromUnknown(folders.value, folderFromUnknown); this.renderFolders(); } catch (error: unknown) { this.setHTML('[data-health-folders]', `<div class="notice error">文件夹统计读取失败：${escape(errorMessage(error))}</div>`); }
    try { if (issues?.status !== 'fulfilled') throw issues?.status === 'rejected' ? issues.reason : new Error('异常清单未返回'); this.issuePage = pageFromUnknown(issues.value, issueFromUnknown); this.renderIssues(); } catch (error: unknown) { this.setHTML('[data-health-issues]', `<div class="notice error">识别清单读取失败：${escape(errorMessage(error))}，尚不能判断没有异常。</div>`); }
    this.updateScopeCaption();
  }
  private pager(kind: string, page: Page<unknown>): string { const from = page.total ? page.offset + 1 : 0; return `<div class="health-pager"><span>${from}–${Math.min(page.total, page.offset + page.items.length)} / ${page.total}</span><div><button class="button small ghost" data-health-page="${kind}:prev" ${page.offset <= 0 ? 'disabled' : ''}>上一页</button><button class="button small ghost" data-health-page="${kind}:next" ${page.offset + page.items.length >= page.total ? 'disabled' : ''}>下一页</button></div></div>`; }
  private renderSources(): void {
    const page = this.sourcePage; if (!page) return;
    const statuses: Readonly<Record<string, string>> = { available: '可访问', offline: '来源离线', attention: '扫描需关注', unscanned: '尚未扫描' };
    this.setHTML('[data-health-sources]', `<div class="source-health-grid">${page.items.map(source => `<article class="source-health-card ${source.id === this.scope.source_id ? 'selected' : ''}"><div><h3>${escape(source.label)}</h3><span class="health-source-state">${escape(statuses[source.source_status] || source.source_status)}</span></div><code>${escape(source.path || (source.id === 'owned' ? 'data/media（库内副本）' : '路径未返回'))}</code><p>${source.media.videos} 视频 / ${source.media.images} 图片 · 基础资料就绪 ${source.media.ready} · 分析失败 ${source.media.failed}</p><p>短片就绪 ${source.preview.ready}/${source.preview.total} · 排队/生成 ${source.preview.queued + source.preview.processing + source.preview.pending} · 失败 ${source.preview.failed}</p>${source.error ? `<p class="health-source-error">${escape(source.error)}</p>` : ''}${source.unsupported_files || source.unsupported.length ? `<p class="health-source-unsupported">未收录扩展名文件 ${source.unsupported_files} 项${source.unsupported.length ? '：' + source.unsupported.slice(0, 8).map(item => `${escape(item.extension || '无扩展名')} × ${item.count}`).join('，') : ''}。这些不一定是视频；与已登记分析失败分别统计。</p>` : ''}<button class="button small ghost" data-health-source="${escape(source.id)}">查看此来源</button></article>`).join('')}</div>${this.pager('sources', page)}`);
    const select = this.root.querySelector<HTMLSelectElement>('#health-source');
    if (select) { const choices = this.selectedSource && !page.items.some(item => item.id === this.selectedSource?.id) ? [this.selectedSource, ...page.items] : page.items; const options = '<option value="">全部来源（含库内）</option>' + choices.map(item => `<option value="${escape(item.id)}" ${item.id === this.scope.source_id ? 'selected' : ''}>${escape(item.label)}</option>`).join(''); if (this.html.get('source-options') !== options) { select.innerHTML = options; this.html.set('source-options', options); } select.value = this.scope.source_id || ''; }
  }
  private renderFolders(): void {
    const page = this.folderPage; if (!page) return;
    this.setHTML('[data-health-folders]', page.items.length ? `<div class="health-table-scroll"><table class="folder-health-table"><thead><tr><th>相对文件夹</th><th>视频 / 图片</th><th>基础资料失败 / 原片不可用</th><th>预览就绪 / 总视频</th><th>操作</th></tr></thead><tbody>${page.items.map(item => `<tr><td><code>${escape(item.folder || item.label || '根目录')}</code></td><td>${item.media.videos} / ${item.media.images}</td><td>${item.media.failed} / ${item.media.missing + item.media.changed}</td><td>${item.preview.ready} / ${item.preview.total}${item.preview.failed ? ` · 失败 ${item.preview.failed}` : ''}</td><td><button class="button small ghost" data-health-folder="${escape(item.folder)}">选择此文件夹</button></td></tr>`).join('')}</tbody></table></div>${this.pager('folders', page)}` : '<p class="hint">此范围暂没有已登记文件夹。尚未登记或被排除的文件请另外核对来源扫描结果。</p>');
  }
  private renderIssues(): void {
    const page = this.issuePage; if (!page) return;
    this.setHTML('[data-health-issues]', page.items.length ? `<div class="health-issue-list">${page.items.map(item => {
      const summary = string(item.diagnostic.summary) || string(item.diagnostic.title) || string(item.diagnostic.message) || string(item.diagnostic.reason);
      const explanation = string(item.diagnostic.explanation);
      const recommendations = [item.diagnostic.recommendation, item.diagnostic.recommendations, item.diagnostic.next_steps, item.diagnostic.suggestions].flatMap(value => Array.isArray(value) ? value.filter((entry): entry is string => typeof entry === 'string') : typeof value === 'string' ? [value] : []);
      const evidence = Array.isArray(item.diagnostic.evidence) ? item.diagnostic.evidence.filter(isRecord).filter(entry => typeof entry.message === 'string').slice(0, 4) : [];
      const actions = [['retry_analysis', 'analyze', '重试基础分析'], ['prepare_preview', 'preview', '准备预览'], ['retry_preview', 'preview-retry', '重试失败预览'], ['prepare_playback', 'playback', '准备兼容播放'], ['verify', 'verify', '核对原片']];
      return `<article class="health-issue"><div class="health-issue-title"><button class="health-title-button" data-health-open="${escape(item.id)}">${escape(item.title || item.filename)}</button><span>${escape(item.codec ? item.codec.toUpperCase() : '编码未识别')}${item.container ? ' / ' + escape(item.container) : ''}</span></div><code>${escape((item.relative_folder ? item.relative_folder + '/' : '') + item.filename)}</code><div class="health-issue-codes">${item.issue_codes.map(code => `<span>${escape(issueNames[code] || code)}</span>`).join('')}</div>${summary ? `<p><strong>${escape(summary)}</strong></p>` : '<p>当前记录需要核对；打开资料查看最近错误，未据此断定必须安装插件。</p>'}${explanation ? `<p>${escape(explanation)}</p>` : ''}${recommendations.length ? `<ul>${recommendations.map(text => `<li>${escape(text)}</li>`).join('')}</ul>` : ''}${evidence.length ? `<details class="health-evidence"><summary>查看检测依据与错误</summary>${evidence.map(entry => `<p><strong>${escape(string(entry.stage, '检测'))}</strong><br><code>${escape(string(entry.message).slice(0, 1400))}</code></p>`).join('')}</details>` : ''}<div class="button-row">${actions.filter(([key]) => !!key && item.actions[key] === true && !(key === 'prepare_preview' && item.actions.retry_preview === true)).map(([, action, label]) => `<button class="button small ghost" data-health-action="${action}" data-health-id="${escape(item.id)}">${label}</button>`).join('')}</div></article>`;
    }).join('')}</div>${this.pager('issues', page)}` : `<p class="hint">当前筛选没有返回${this.issueKind === 'all' ? '已记录的实际异常' : '匹配记录'}。这只表示此范围的片目状态；没有对所有文件执行新的完整解码或浏览器实播检查。</p>`);
  }
  private updateScopeCaption(): void { const scope = this.displayedScope || this.scope; const source = this.sourcePage?.items.find(item => item.id === scope.source_id) || (this.selectedSource?.id === scope.source_id ? this.selectedSource : null); const caption = `${scope.source_id ? source?.label || '所选来源' : '全部来源'} / ${scope.folder || '根目录'}${scope.recursive ? '（含所有下级）' : '（仅此层）'}${this.scopeLoading ? ' · 正在读取新范围' : ''}`; const node = this.root.querySelector('[data-health-caption]'); if (node) node.textContent = caption; }
  private changeScope(scope: HealthScope): void {
    this.selectedSource = this.sourcePage?.items.find(item => item.id === scope.source_id) || (this.selectedSource?.id === scope.source_id ? this.selectedSource : null);
    this.scope = scope; this.displayedScope = null; this.scopeLoading = true;
    this.folderOffset = this.issueOffset = 0; this.lastRevision = null; this.sequence++;
    const folder = this.root.querySelector<HTMLInputElement>('#health-folder'); if (folder) folder.value = scope.folder;
    const recursive = this.root.querySelector<HTMLInputElement>('[name="recursive"]'); if (recursive) recursive.checked = scope.recursive;
    this.setHTML('[data-health-summary]', '<p class="hint" role="status">正在读取新范围的预览与分析状态…</p>');
    this.setHTML('[data-health-folders]', '<p class="hint">正在读取新范围的文件夹…</p>');
    this.setHTML('[data-health-issues]', '<p class="hint">正在读取新范围的识别情况…</p>');
    this.renderSources(); this.updateScopeCaption(); void this.refresh(true);
  }
  private readonly onSubmit = (event: SubmitEvent): void => {
    const form = event.target; if (!(form instanceof HTMLFormElement) || !form.hasAttribute('data-health-scope')) return;
    event.preventDefault(); event.stopPropagation(); const source = form.elements.namedItem('source_id'), folder = form.elements.namedItem('folder'), recursive = form.elements.namedItem('recursive');
    if (!(source instanceof HTMLSelectElement) || !(folder instanceof HTMLInputElement) || !(recursive instanceof HTMLInputElement)) return;
    this.changeScope({ source_id: source.value || null, folder: folder.value.trim().replace(/\\/g, '/'), recursive: recursive.checked });
  };
  private readonly onChange = (event: Event): void => { const target = event.target; if (!(target instanceof HTMLSelectElement) || !target.hasAttribute('data-health-issue-kind')) return; this.issueKind = target.value; this.issueOffset = 0; this.lastRevision = null; this.sequence++; void this.refresh(true); };
  private readonly onClick = (event: MouseEvent): void => {
    if (!(event.target instanceof Element)) return;
    const button = event.target.closest<HTMLButtonElement>('[data-health-capabilities],[data-health-refresh],[data-health-navigate],[data-health-source],[data-health-folder],[data-health-root],[data-health-prepare],[data-health-retry],[data-health-page],[data-health-open],[data-health-action]'); if (!button) return;
    event.preventDefault(); event.stopPropagation();
    if (button.hasAttribute('data-health-capabilities')) { void this.loadCapabilities(button); return; }
    if (button.hasAttribute('data-health-navigate')) { this.options.onNavigateSources?.(); return; }
    if (button.hasAttribute('data-health-open')) { this.options.onOpenVideo?.(button.dataset.healthOpen || ''); return; }
    if (button.hasAttribute('data-health-source')) { this.changeScope({ source_id: button.dataset.healthSource || null, folder: '', recursive: true }); return; }
    if (button.hasAttribute('data-health-folder')) { this.changeScope({ ...this.scope, folder: button.dataset.healthFolder || '' }); return; }
    if (button.hasAttribute('data-health-root')) { this.changeScope({ ...this.scope, folder: '' }); return; }
    if (button.dataset.healthPage) {
      const [kind, direction] = button.dataset.healthPage.split(':'), amount = direction === 'prev' ? -1 : 1;
      if (kind === 'sources') this.sourceOffset = Math.max(0, this.sourceOffset + amount * 12); else if (kind === 'folders') this.folderOffset = Math.max(0, this.folderOffset + amount * 12); else this.issueOffset = Math.max(0, this.issueOffset + amount * 20);
      this.lastRevision = null; this.sequence++; void this.refresh(true); return;
    }
    if (button.hasAttribute('data-health-refresh')) { void this.refresh(true); return; }
    button.disabled = true;
    void (async () => {
      try {
        if (button.hasAttribute('data-health-prepare') || button.hasAttribute('data-health-retry')) {
          const result = await localPost('/api/library-health/previews', { ...this.scope, retry_failed: button.hasAttribute('data-health-retry') });
          if (!isRecord(result)) throw new Error('准备批次未返回有效结果。');
          const message = `选中 ${nonnegative(result.selected)} 个视频；排入 ${nonnegative(result.queued)}，等待入队 ${nonnegative(result.pending)}，跳过 ${nonnegative(result.skipped)}。${string(result.notice)}`;
          if (!this.disposed) { const host = this.root.querySelector('[data-health-result]'); if (host) host.textContent = message; this.options.onMessage?.(message, false); }
        } else {
          const id = button.dataset.healthId, action = button.dataset.healthAction; if (!id || !action) return;
          const endpoint = ({ playback: 'prepare-playback', 'preview-retry': 'preview', preview: 'preview', analyze: 'analyze', verify: 'verify' } as Readonly<Record<string, string>>)[action];
          if (!endpoint) throw new Error('未知的资料操作，未提交任务。');
          const value = await localPost(`/api/videos/${encodeURIComponent(id)}/${endpoint}`, action === 'preview-retry' ? { retry: true } : {});
          if (isRecord(value) && value.job_id === null && value.status === undefined) throw new Error('同项任务可能已排队或队列已满，请查看当前进度后再试。');
          if (isRecord(value) && value.status === 'error') throw new Error(string(value.error, '准备失败，请查看本条记录的原因。'));
          if (!this.disposed) this.options.onMessage?.(isRecord(value) && value.status === 'ready' ? '播放地址已经就绪，可打开资料播放。' : '已提交本机任务；进度和错误会更新在此清单。', false);
        }
        this.lastRevision = null; await this.refresh(true);
      } catch (error: unknown) { if (!this.disposed) this.options.onMessage?.(errorMessage(error), true); }
      finally { if (button.isConnected) button.disabled = false; }
    })();
  };
}
