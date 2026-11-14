import { errorMessage, isRecord } from './contracts';

type EngineId = 'visual' | 'audio' | 'speech';
interface AnalysisEngine {
  readonly id: string;
  readonly name: string;
  readonly kind: string;
  readonly installed: boolean | null;
  readonly available: boolean;
  readonly status: string;
  readonly description: string;
  readonly limitations: readonly string[];
}
interface SignalConfig { readonly enabled: boolean; readonly sensitivity: number; readonly minimum_event_gap: number; readonly merge_window: number }
interface SpeechConfig { readonly enabled: boolean; readonly threshold: number; readonly min_speech_duration: number; readonly min_silence_duration: number }
interface AnalysisConfig { readonly visual: SignalConfig; readonly audio: SignalConfig; readonly speech: SpeechConfig }
interface EngineResponse { readonly engines: readonly AnalysisEngine[]; readonly config: AnalysisConfig; readonly limitations: readonly string[] }

const escape = (value: string | number): string => String(value).replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character] || character);
function strings(value: unknown): string[] { return Array.isArray(value) ? value.filter((entry): entry is string => typeof entry === 'string') : []; }
function numeric(record: Record<string, unknown>, key: string): number {
  const value = record[key];
  if (typeof value !== 'number' || !Number.isFinite(value)) throw new Error('分析参数尚未完整返回，请刷新后重试。');
  return value;
}
function signalConfig(value: unknown): SignalConfig {
  if (!isRecord(value) || typeof value.enabled !== 'boolean') throw new Error('分析参数格式无法识别。');
  return { enabled: value.enabled, sensitivity: numeric(value, 'sensitivity'), minimum_event_gap: numeric(value, 'minimum_event_gap'), merge_window: numeric(value, 'merge_window') };
}
export function enginesFromUnknown(value: unknown): EngineResponse {
  if (!isRecord(value) || !Array.isArray(value.engines) || !isRecord(value.config) || !isRecord(value.config.speech)) throw new Error('分析引擎信息尚未就绪。');
  const speech = value.config.speech;
  if (typeof speech.enabled !== 'boolean') throw new Error('语音参数格式无法识别。');
  const engines: AnalysisEngine[] = [];
  for (const item of value.engines) {
    if (!isRecord(item) || typeof item.id !== 'string' || typeof item.name !== 'string') continue;
    engines.push({ id: item.id, name: item.name, kind: typeof item.kind === 'string' ? item.kind : 'unknown', installed: typeof item.installed === 'boolean' ? item.installed : null, available: item.available === true, status: typeof item.status === 'string' ? item.status : 'unknown', description: typeof item.description === 'string' ? item.description : '', limitations: strings(item.limitations) });
  }
  return { engines, limitations: strings(value.limitations), config: { visual: signalConfig(value.config.visual), audio: signalConfig(value.config.audio), speech: { enabled: speech.enabled, threshold: numeric(speech, 'threshold'), min_speech_duration: numeric(speech, 'min_speech_duration'), min_silence_duration: numeric(speech, 'min_silence_duration') } } };
}

export class AnalysisManager {
  private disposed = false;
  private response: EngineResponse | null = null;
  private readonly controller = new AbortController();
  constructor(private readonly root: HTMLElement, private readonly notify?: (message: string, error: boolean) => void) {
    root.addEventListener('submit', this.onSubmit);
    root.addEventListener('click', this.onClick);
    void this.load();
  }
  dispose(): void { this.disposed = true; this.controller.abort(); this.root.removeEventListener('submit', this.onSubmit); this.root.removeEventListener('click', this.onClick); }
  refresh(): void { void this.load(); }
  private async request(method: 'GET' | 'POST', config?: AnalysisConfig): Promise<EngineResponse> {
    const response = await fetch('/api/analysis-engines', { method, credentials: 'same-origin', signal: this.controller.signal, ...(config ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(config) } : {}) });
    const value: unknown = await response.json();
    if (!response.ok) throw new Error(isRecord(value) && typeof value.error === 'string' ? value.error : `引擎请求失败（${response.status}）`);
    return enginesFromUnknown(value);
  }
  private async load(): Promise<void> {
    if (this.disposed) return;
    if (!this.response) this.root.innerHTML = '<div class="loading"><span class="spinner"></span>正在读取本机引擎状态…</div>';
    try { const result = await this.request('GET'); if (!this.disposed) { this.response = result; this.render(result); } }
    catch (error: unknown) { if (!this.disposed) { this.root.innerHTML = `<div class="notice error">${escape(errorMessage(error))}</div><button class="button ghost" data-engine-refresh>重新读取引擎状态</button>`; } }
  }
  private engineStatus(engine: AnalysisEngine): string {
    if (engine.status === 'configured_unverified') return '已配置 · 未核验';
    if (engine.available) return engine.kind === 'algorithm' ? '算法可用' : engine.installed === true ? '本机模型已安装' : '当前可用';
    if (engine.installed === false || engine.status === 'not_installed') return '未安装';
    return engine.installed === true ? '已安装 · 暂不可运行' : '尚未就绪';
  }
  private numberInput(name: string, label: string, value: number, min: number, max: number, step: number, suffix = ''): string {
    return `<label class="analysis-number"><span>${escape(label)}${suffix ? `（${escape(suffix)}）` : ''}</span><input name="${name}" type="number" required min="${min}" max="${max}" step="${step}" value="${value}"></label>`;
  }
  private signalForm(id: 'visual' | 'audio', config: SignalConfig): string {
    const available = this.response?.engines.find(engine => engine.id === id)?.available === true;
    return `<section class="analysis-parameters"><div><h3>${id === 'visual' ? '场景切换' : '音量变化'}算法</h3><label class="check-row"><input type="checkbox" name="${id}.enabled" ${config.enabled ? 'checked' : ''} ${available ? '' : 'disabled'}>纳入时间轴分析</label></div><div class="analysis-number-grid">${this.numberInput(`${id}.sensitivity`, '灵敏度', config.sensitivity, 0, 1, .05)}${this.numberInput(`${id}.minimum_event_gap`, '最小节点间隔', config.minimum_event_gap, .5, 30, .5, '秒')}${this.numberInput(`${id}.merge_window`, '相近候选合并窗口', config.merge_window, 0, 10, .25, '秒')}</div></section>`;
  }
  private render(response: EngineResponse): void {
    this.root.innerHTML = `<div class="engine-grid">${response.engines.map(engine => `<article class="engine-card ${engine.available ? 'available' : 'unavailable'}"><div class="engine-card-heading"><span class="engine-kind">${engine.kind === 'algorithm' ? '信号算法' : engine.kind === 'local_model' ? '本机模型' : engine.kind === 'provider' ? '模型服务' : '能力状态'}</span><span class="engine-status">${escape(this.engineStatus(engine))}</span></div><h2>${escape(engine.name)}</h2><p>${escape(engine.description)}</p>${engine.limitations.length ? `<details><summary>能力范围与限制</summary><ul>${engine.limitations.map(limit => `<li>${escape(limit)}</li>`).join('')}</ul></details>` : ''}</article>`).join('')}</div><section class="panel analysis-config-panel"><div class="panel-header"><h2>时间轴检测参数</h2><button class="button small ghost" data-engine-refresh>重新读取</button></div><p>参数只影响随后执行的分析。场景切换和音量变化来自信号算法；语音片段来自本机模型，不生成转写或识别说话人。</p><form data-analysis-config>${this.signalForm('visual', response.config.visual)}${this.signalForm('audio', response.config.audio)}<section class="analysis-parameters"><div><h3>语音片段检测</h3><label class="check-row"><input type="checkbox" name="speech.enabled" ${response.config.speech.enabled ? 'checked' : ''} ${response.engines.find(engine => engine.id === 'speech')?.available ? '' : 'disabled'}>纳入时间轴分析</label></div><div class="analysis-number-grid">${this.numberInput('speech.threshold', '语音概率阈值', response.config.speech.threshold, .1, .95, .05)}${this.numberInput('speech.min_speech_duration', '最短语音长度', response.config.speech.min_speech_duration, .1, 5, .05, '秒')}${this.numberInput('speech.min_silence_duration', '分段静音间隔', response.config.speech.min_silence_duration, .1, 3, .05, '秒')}</div></section><div class="button-row"><button class="button primary" type="submit">保存分析参数</button></div></form>${response.limitations.length ? `<details class="analysis-global-limits"><summary>共同限制</summary><ul>${response.limitations.map(limit => `<li>${escape(limit)}</li>`).join('')}</ul></details>` : ''}</section>`;
  }
  private readInput(form: HTMLFormElement, name: string): HTMLInputElement {
    const input = form.elements.namedItem(name);
    if (!(input instanceof HTMLInputElement)) throw new Error('分析表单未完整加载，请刷新后重试。');
    return input;
  }
  private readNumber(form: HTMLFormElement, name: string, min: number, max: number): number {
    const value = Number(this.readInput(form, name).value);
    if (!Number.isFinite(value) || value < min || value > max) throw new Error('请检查参数范围后再保存。');
    return value;
  }
  private readSignal(form: HTMLFormElement, id: EngineId): SignalConfig {
    return { enabled: this.readInput(form, `${id}.enabled`).checked, sensitivity: this.readNumber(form, `${id}.sensitivity`, 0, 1), minimum_event_gap: this.readNumber(form, `${id}.minimum_event_gap`, .5, 30), merge_window: this.readNumber(form, `${id}.merge_window`, 0, 10) };
  }
  private readonly onSubmit = (event: SubmitEvent): void => {
    if (!(event.target instanceof HTMLFormElement) || !event.target.hasAttribute('data-analysis-config')) return;
    event.preventDefault();
    event.stopPropagation();
    const form = event.target;
    const button = form.querySelector<HTMLButtonElement>('button[type="submit"]');
    if (button) button.disabled = true;
    void (async () => {
      try {
        const config: AnalysisConfig = { visual: this.readSignal(form, 'visual'), audio: this.readSignal(form, 'audio'), speech: { enabled: this.readInput(form, 'speech.enabled').checked, threshold: this.readNumber(form, 'speech.threshold', .1, .95), min_speech_duration: this.readNumber(form, 'speech.min_speech_duration', .1, 5), min_silence_duration: this.readNumber(form, 'speech.min_silence_duration', .1, 3) } };
        const result = await this.request('POST', config);
        if (!this.disposed) { this.response = result; this.render(result); this.notify?.('分析参数已保存，随后执行的分析使用这些参数。', false); }
      } catch (error: unknown) { if (!this.disposed) this.notify?.(errorMessage(error), true); }
      finally { if (button?.isConnected) button.disabled = false; }
    })();
  };
  private readonly onClick = (event: MouseEvent): void => {
    if (event.target instanceof Element && event.target.closest('[data-engine-refresh]')) { event.preventDefault(); void this.load(); }
  };
}
