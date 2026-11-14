/**
 * [INPUT]: 接口契约与模型和缓存 DTO。
 * [OUTPUT]: 模型、缓存配置和队列交互。
 * [POS]: 网页管理模块。
 * [PROTOCOL]: Update this header on change, then check AGENTS.md
 */
import { errorMessage, isRecord, localPost } from './contracts';

type Notify = (message: string, error: boolean) => void;
interface ManagedVision { name: string; installed: boolean; available: boolean; bytes: number; notice: string; setup: string; runtime: string }
interface Provider { kind: 'ollama' | 'openai_compatible' | 'managed_vision'; managed: ManagedVision | null; base_url: string; model: string; timeout_seconds: number; max_images: number; local_inference_only: boolean; local_only: boolean; automatic_allowed: boolean; has_api_key: boolean; status: string }
interface CacheGroup { files: number; bytes: number; reclaimable_bytes: number }
interface CacheState { groups: Record<string, CacheGroup>; total_bytes: number; config: { cache_auto_trim: boolean; cache_limit_gb: number; protect_registered_previews: boolean }; protected_notice: string }
interface CleanupPlan { token: string; scope: string; files: number; bytes: number; expires: number; skipped: number }
const escape = (value: string | number): string => String(value).replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character] || character);
const finite = (value: unknown, fallback = 0): number => typeof value === 'number' && Number.isFinite(value) ? value : fallback;
const bytes = (value: number): string => value >= 1073741824 ? `${(value / 1073741824).toFixed(2)} GiB` : value >= 1048576 ? `${(value / 1048576).toFixed(1)} MiB` : `${Math.max(0, Math.round(value / 1024))} KiB`;
function help(label: string, content: string): string {
  return `<details class="inline-help"><summary>${escape(label)}</summary><div class="inline-help-copy">${content}</div></details>`;
}
function input(form: HTMLFormElement, name: string): HTMLInputElement | HTMLSelectElement {
  const field = form.elements.namedItem(name);
  if (!(field instanceof HTMLInputElement) && !(field instanceof HTMLSelectElement)) throw new Error('表单尚未完整加载。');
  return field;
}
function checked(form: HTMLFormElement, name: string): boolean { const field = input(form, name); return field instanceof HTMLInputElement && field.checked; }
function number(form: HTMLFormElement, name: string, min: number, max: number): number {
  const value = Number(input(form, name).value);
  if (!Number.isInteger(value) || value < min || value > max) throw new Error(`请检查 ${name} 的整数范围：${min}–${max}。`);
  return value;
}
async function get(path: string, signal: AbortSignal): Promise<unknown> {
  const response = await fetch(path, { credentials: 'same-origin', signal });
  const value: unknown = await response.json();
  if (!response.ok) throw new Error(isRecord(value) && typeof value.error === 'string' ? value.error : '本机管理接口暂不可用。');
  return value;
}
export function providerFromUnknown(value: unknown): Provider {
  if (!isRecord(value) || !isRecord(value.provider)) throw new Error('模型服务信息未完整返回。');
  const p = value.provider;
  if ((p.kind !== 'ollama' && p.kind !== 'openai_compatible' && p.kind !== 'managed_vision') || typeof p.base_url !== 'string' || typeof p.model !== 'string') throw new Error('模型服务配置格式无法识别。');
  const managed = isRecord(value.managed_model) ? value.managed_model : null;
  const localVision: ManagedVision | null = managed ? {
    name: typeof managed.name === 'string' ? managed.name : 'Qwen3-VL-2B-Instruct',
    installed: managed.installed === true || finite(managed.weight_bytes) > 0,
    available: managed.available === true,
    bytes: finite(managed.weight_bytes, finite(managed.bytes)),
    notice: typeof managed.reason === 'string' ? managed.reason : typeof managed.notice === 'string' ? managed.notice : '',
    setup: typeof managed.setup_command === 'string' ? managed.setup_command : '',
    runtime: typeof managed.runtime_status === 'string' ? managed.runtime_status : ''
  } : null;
  return { kind: p.kind, managed: localVision, base_url: p.base_url, model: p.model, timeout_seconds: finite(p.timeout_seconds, 120), max_images: finite(p.max_images, 3), local_inference_only: p.local_inference_only === true, local_only: p.local_only === true, automatic_allowed: p.automatic_allowed === true, has_api_key: p.has_api_key === true, status: typeof p.status === 'string' ? p.status : 'not_configured' };
}
export function cacheFromUnknown(value: unknown): CacheState {
  if (!isRecord(value) || !isRecord(value.groups) || !isRecord(value.config)) throw new Error('缓存统计未完整返回。');
  const groups: Record<string, CacheGroup> = {};
  for (const name of ['previews', 'compatible', 'temporary', 'orphans']) {
    const group = value.groups[name];
    if (!isRecord(group)) throw new Error('缓存分组统计尚未就绪。');
    groups[name] = { files: finite(group.files), bytes: finite(group.bytes), reclaimable_bytes: finite(group.reclaimable_bytes) };
  }
  return { groups, total_bytes: finite(value.total_bytes), config: { cache_auto_trim: value.config.cache_auto_trim === true, cache_limit_gb: finite(value.config.cache_limit_gb, 8), protect_registered_previews: value.config.protect_registered_previews === true }, protected_notice: typeof value.protected_notice === 'string' ? value.protected_notice : '原片、片目、已登记封面与备份不在清理范围。' };
}

export class LibraryManagement {
  private disposed = false;
  private provider: Provider | null = null;
  private readonly controller = new AbortController();
  constructor(private readonly root: HTMLElement, private readonly notify?: Notify, private readonly changed?: () => void) {
    root.addEventListener('submit', this.onSubmit); root.addEventListener('click', this.onClick);
    void this.load();
  }
  dispose(): void { this.disposed = true; this.controller.abort(); this.root.removeEventListener('submit', this.onSubmit); this.root.removeEventListener('click', this.onClick); }
  private async load(): Promise<void> {
    try { const provider = providerFromUnknown(await get('/api/providers', this.controller.signal)); if (!this.disposed) { this.provider = provider; this.render(provider); } }
    catch (error: unknown) { if (!this.disposed) this.root.innerHTML = `<div class="notice error">${escape(errorMessage(error))}</div><button class="button ghost" data-provider-reload>重新读取模型配置</button>`; }
  }
  private render(p: Provider): void {
    const managed = p.managed;
    const selected = p.kind === 'managed_vision';
    const runtime = managed?.runtime === 'running' ? ' · 已运行' : managed?.runtime === 'starting' ? ' · 正在启动' : managed?.available ? ' · 按需启动' : '';
    const managedMarkup = managed ? `<div class="managed-vision-card"><div><strong>${escape(managed.name)}</strong><p class="hint">${managed.available ? '权重和本平台运行组件已就绪' : managed.installed ? '已带权重，需准备本平台运行组件' : '本机模型权重尚未就绪'}${managed.bytes ? ` · ${bytes(managed.bytes)}` : ''}${runtime}</p>${managed.notice ? `<p class="hint">${escape(managed.notice)}</p>` : ''}${!managed.available && managed.setup ? `<p class="hint">准备入口 <code>${escape(managed.setup)}</code></p>` : ''}</div><div class="button-row">${selected ? `<span class="quality-badge">当前模型</span><button type="button" class="button small ghost" data-provider-probe ${managed.available ? '' : 'disabled'}>检查本机运行</button>` : `<button type="button" class="button small ghost" data-provider-use-managed ${managed.available ? '' : 'disabled'}>使用内置模型</button>`}</div></div>` : '';
    const manualKind = p.kind === 'openai_compatible' ? 'openai_compatible' : 'ollama';
    const manualUrl = selected ? 'http://127.0.0.1:11434' : p.base_url;
    const manualModel = selected ? '' : p.model;
    this.root.innerHTML = `<section class="panel provider-panel"><div class="panel-header"><h2>视觉概述模型服务</h2><span class="quality-badge">${p.local_only ? '已声明仅本机' : '逐次确认图片发送'}</span></div>${managedMarkup}<details class="advanced-settings" ${selected ? '' : 'open'}><summary>配置其他视觉模型服务</summary><form data-provider-config><div class="form-columns"><div class="field"><label for="provider-kind">接口类型</label><select id="provider-kind" name="kind"><option value="ollama" ${manualKind === 'ollama' ? 'selected' : ''}>Ollama</option><option value="openai_compatible" ${manualKind === 'openai_compatible' ? 'selected' : ''}>OpenAI 兼容接口</option></select></div><div class="field"><label for="provider-model">模型名</label><input id="provider-model" name="model" list="provider-models" maxlength="200" value="${escape(manualModel)}" placeholder="服务中已安装的视觉模型"><datalist id="provider-models"></datalist></div></div><div class="field"><label for="provider-base-url">服务地址</label><input id="provider-base-url" type="url" name="base_url" required value="${escape(manualUrl)}">${help("地址与图片发送", "Ollama 填服务根地址；兼容接口可含 /v1。网络与未声明仅本机的服务，每次发送前显示确切目标和抽样图。")}</div><details class="advanced-settings"><summary>连接参数与密钥</summary><div class="form-columns"><div class="field"><label for="provider-timeout">超时（10–180 秒）</label><input id="provider-timeout" type="number" name="timeout_seconds" min="10" max="180" step="1" value="${p.timeout_seconds}"></div><div class="field"><label for="provider-max-images">每次最多抽样图（1–6）</label><input id="provider-max-images" type="number" name="max_images" min="1" max="6" step="1" value="${p.max_images}"></div></div><div class="field"><label for="provider-api-key">API 密钥（可选）</label><input id="provider-api-key" type="password" name="api_key" autocomplete="new-password" placeholder="${p.has_api_key ? '已有密钥；留空保留，不回显' : '需要时填写'}">${help("密钥保存位置", "密钥只保存到本机独立文件，不放进模型列表或资料备份；更换目标地址会清除旧密钥。")}<label class="check-row"><input type="checkbox" name="clear_api_key">明确清除已保存密钥</label></div></details><div class="field"><label class="check-row"><input type="checkbox" name="local_inference_only" ${!selected && p.local_inference_only ? 'checked' : ''}>我确认这个本机服务只做本地推理，不转发云端</label>${help("本机推理条件", "localhost 也可能代理云模型。自动描述需同时满足回环地址、你的本地推理声明及非云模型。Ollama 可设 OLLAMA_NO_CLOUD=1；此声明不会更改模型服务自身设置。")}</div><div class="button-row"><button class="button primary" type="submit">保存配置（不连接服务）</button>${selected ? '' : '<button class="button ghost" type="button" data-provider-probe>连接已保存地址并列模型</button>'}</div></form></details><div class="provider-probe-result" role="status"><p class="hint">已保存目标：<code>${escape(p.base_url)}</code>；${p.model ? '配置尚未核验视觉推理。' : '尚未选择模型。'} 列模型会连接此目标，但不发送媒体图片。</p></div></section><section class="panel automation-panel"><div class="panel-header"><h2>对已有资料执行本机分析</h2></div>${help("批处理规则", "新资料的自动规则在设置中。这里提交有限批次，已有成功或失败版本默认跳过。需要替换已有分类结果时，使用设置中的重新分类；远程描述不会批量发送。")}<form data-analysis-batch><div class="batch-task-choices"><label class="check-row"><input type="checkbox" name="timeline" checked>音画时间轴</label><label class="check-row"><input type="checkbox" name="describe" ${p.automatic_allowed ? '' : 'disabled'}>仅本机视觉概述</label></div><div class="form-columns"><div class="field"><label for="batch-scope">内容分区</label><select id="batch-scope" name="scope"><option value="all">全部资料</option><option value="review">待确认</option><option value="personal">个人资料</option><option value="adult">18+ 视频</option></select></div><div class="field"><label for="batch-limit">本次最多资料（1–128）</label><input id="batch-limit" type="number" min="1" max="128" step="1" name="limit" value="32"></div></div><label class="check-row"><input type="checkbox" name="retry_failed">本次明确重试已有失败版本</label><div class="button-row"><button class="button primary" type="submit">在本机排入这一批任务</button></div><div class="batch-submit-result" role="status"></div></form></section>`;
  }
  private readonly onSubmit = (event: SubmitEvent): void => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.matches('[data-provider-config],[data-analysis-batch]')) return;
    event.preventDefault(); event.stopPropagation();
    const button = form.querySelector<HTMLButtonElement>('button[type="submit"]'); if (button) button.disabled = true;
    void (async () => {
      try {
        if (form.hasAttribute('data-provider-config')) {
          const body: Record<string, unknown> = { kind: input(form, 'kind').value, base_url: input(form, 'base_url').value.trim(), model: input(form, 'model').value.trim(), timeout_seconds: number(form, 'timeout_seconds', 10, 180), max_images: number(form, 'max_images', 1, 6), local_inference_only: checked(form, 'local_inference_only'), clear_api_key: checked(form, 'clear_api_key') };
          const key = input(form, 'api_key').value; if (key) body.api_key = key;
          const result = providerFromUnknown(await localPost('/api/providers', body, this.controller.signal));
          if (!this.disposed) { this.provider = result; this.render(result); this.changed?.(); this.notify?.('模型配置已保存；没有连接服务或发送图片。', false); }
        } else {
          const tasks = ['timeline', 'describe'].filter(name => checked(form, name));
          if (!tasks.length) throw new Error('请至少选择一项本机任务。');
          if (tasks.includes('describe') && !this.provider?.automatic_allowed) throw new Error('当前模型服务不能用于自动本机描述。');
          const value = await localPost('/api/automation/batch', { tasks, scope: input(form, 'scope').value, limit: number(form, 'limit', 1, 128), retry_failed: checked(form, 'retry_failed') }, this.controller.signal);
          if (!this.disposed) {
            const host = form.querySelector('.batch-submit-result');
            if (host) host.textContent = isRecord(value) ? `本批已提交。${typeof value.queued === 'number' ? `排入 ${value.queued} 项任务。` : ''}${typeof value.skipped === 'number' ? `跳过 ${value.skipped} 项。` : ''}实际进度与失败原因见最近后台任务。` : '本批已提交，请看最近后台任务。';
            this.notify?.('本机批处理已提交，远程图片没有批量发送。', false);
          }
        }
      } catch (error: unknown) { if (!this.disposed) this.notify?.(errorMessage(error), true); }
      finally { if (button?.isConnected) button.disabled = false; }
    })();
  };
  private async useManaged(button: HTMLButtonElement): Promise<void> {
    try {
      await localPost('/api/providers', { kind: 'managed_vision' }, this.controller.signal);
      await this.load();
      if (!this.disposed) { this.changed?.(); this.notify?.('内置模型已设为当前概述模型，设置中可开启自动概述。', false); }
    } catch (error: unknown) {
      if (!this.disposed) this.notify?.(errorMessage(error), true);
    } finally { if (button.isConnected) button.disabled = false; }
  }
  private readonly onClick = (event: MouseEvent): void => {
    if (!(event.target instanceof Element)) return;
    if (event.target.closest('[data-provider-reload]')) { void this.load(); return; }
    const managed = event.target.closest<HTMLButtonElement>('[data-provider-use-managed]');
    if (managed) {
      managed.disabled = true;
      void this.useManaged(managed);
      return;
    }
    const button = event.target.closest<HTMLButtonElement>('[data-provider-probe]');
    if (!button || !this.provider) return;
    event.preventDefault(); button.disabled = true;
    const endpoint = this.provider.base_url;
    void (async () => {
      try {
        const value = await localPost('/api/providers/probe', { endpoint }, this.controller.signal);
        if (this.disposed || !isRecord(value)) return;
        const models = Array.isArray(value.models) ? value.models.filter(isRecord).filter(model => typeof model.id === 'string') : [];
        const options = this.root.querySelector('#provider-models'); if (options) options.innerHTML = models.map(model => `<option value="${escape(String(model.id))}">${model.remote ? '服务标注为远程' : '服务返回的模型'}</option>`).join('');
        const host = this.root.querySelector('.provider-probe-result');
        if (host) host.innerHTML = `<div class="notice ${value.available === true ? 'success' : 'error'}">${value.available === true ? (this.provider?.kind === 'managed_vision' ? '本机运行组件已响应。' : `服务已响应，返回 ${models.length} 个模型；可选填模型名并保存。`) : escape(typeof value.error === 'string' ? value.error : '服务未响应。')}<br>本次目标：<code>${escape(endpoint)}</code>。列模型不证明支持图片，不会自动下载或分析媒体。</div>`;
      } catch (error: unknown) { if (!this.disposed) this.notify?.(errorMessage(error), true); }
      finally { if (button.isConnected) button.disabled = false; }
    })();
  };
}

export class CacheManager {
  private disposed = false;
  private plan: CleanupPlan | null = null;
  private readonly controller = new AbortController();
  constructor(private readonly root: HTMLElement, private readonly notify?: Notify, private readonly cleaned?: () => void) {
    root.addEventListener('submit', this.onSubmit); root.addEventListener('click', this.onClick); void this.load();
  }
  dispose(): void { this.disposed = true; this.controller.abort(); this.plan = null; this.root.removeEventListener('submit', this.onSubmit); this.root.removeEventListener('click', this.onClick); }
  private async load(): Promise<void> {
    try { const value = cacheFromUnknown(await get('/api/cache', this.controller.signal)); if (!this.disposed) this.render(value); }
    catch (error: unknown) { if (!this.disposed) this.root.innerHTML = `<div class="notice error">${escape(errorMessage(error))}</div><button class="button ghost" data-cache-refresh>重新读取缓存</button>`; }
  }
  private render(value: CacheState): void {
    this.plan = null;
    const names: Record<string, string> = { previews: '静音预览短片', compatible: '兼容播放副本', temporary: '过期临时输出', orphans: '无片目关联的派生文件' };
    this.root.innerHTML = `<section class="panel"><div class="panel-header"><h2>缓存与保留策略</h2><button class="button small ghost" data-cache-refresh>刷新统计</button></div><p class="hint">当前合计 ${bytes(value.total_bytes)}。清理前先计算计划，再由你确认。重新生成需要原片仍可访问。</p><div class="cache-groups">${Object.entries(value.groups).map(([name, group]) => `<div class="cache-group"><div><strong>${escape(names[name] || name)}</strong><span>${group.files} 个文件 · ${bytes(group.bytes)}</span><small>${name === 'previews' && value.config.protect_registered_previews ? '已登记资料的短片持续保留' : `当前可回收 ${bytes(group.reclaimable_bytes)}`}</small></div>${name === 'previews' && value.config.protect_registered_previews ? '<span class="quality-badge readonly">已保护</span>' : `<button class="button small ghost" data-cache-plan="${name}">查看清理计划</button>`}</div>`).join('')}</div><div class="notice">${escape(value.protected_notice)}</div><form data-cache-settings><label class="check-row"><input type="checkbox" name="cache_auto_trim" ${value.config.cache_auto_trim ? 'checked' : ''}>自动控制可再生成 MP4 缓存</label><div class="field"><label for="cache-limit">软预算（GiB，1–512）</label><input id="cache-limit" type="number" name="cache_limit_gb" min="1" max="512" step="1" value="${value.config.cache_limit_gb}">${help("缓存预算", "这是软预算，使用中和正在生成的项目会跳过。只按规则清理可再生成文件，保留原片、片目、封面、模型和备份。")}</div><button class="button primary" type="submit">保存缓存策略</button></form><div class="button-row"><button class="button ghost" data-cache-plan="all_regenerable">查看全部可再生成缓存清理计划</button></div><div class="cache-plan-confirmation" role="status"></div></section>`;
  }
  private readonly onSubmit = (event: SubmitEvent): void => {
    const form = event.target; if (!(form instanceof HTMLFormElement) || !form.hasAttribute('data-cache-settings')) return;
    event.preventDefault(); event.stopPropagation(); const button = form.querySelector<HTMLButtonElement>('button[type="submit"]'); if (button) button.disabled = true;
    void (async () => {
      try { await localPost('/api/settings', { cache_auto_trim: checked(form, 'cache_auto_trim'), cache_limit_gb: number(form, 'cache_limit_gb', 1, 512) }, this.controller.signal); if (!this.disposed) { this.notify?.('缓存策略已保存。', false); await this.load(); } }
      catch (error: unknown) { if (!this.disposed) this.notify?.(errorMessage(error), true); }
      finally { if (button?.isConnected) button.disabled = false; }
    })();
  };
  private readonly onClick = (event: MouseEvent): void => {
    if (!(event.target instanceof Element)) return;
    const button = event.target.closest<HTMLButtonElement>('[data-cache-plan],[data-cache-cleanup],[data-cache-refresh]'); if (!button) return;
    event.preventDefault(); button.disabled = true;
    void (async () => {
      try {
        if (button.hasAttribute('data-cache-refresh')) { await this.load(); return; }
        if (button.dataset.cachePlan) {
          this.plan = null;
          const scope = button.dataset.cachePlan;
          const value = await localPost('/api/cache/plan', { scope }, this.controller.signal);
          if (!isRecord(value) || typeof value.plan_token !== 'string') throw new Error('清理计划未返回有效凭据，未删除任何文件。');
          if (this.disposed) return;
          this.plan = { token: value.plan_token, scope, files: finite(value.files), bytes: finite(value.bytes), expires: Date.now() + finite(value.ttl_seconds) * 1000, skipped: finite(value.skipped_active) };
          const host = this.root.querySelector('.cache-plan-confirmation');
          if (host) host.innerHTML = `<div class="notice"><strong>请核对本次计划</strong><p>范围：${escape(({ previews: '预览短片', compatible: '兼容副本', temporary: '过期临时输出', orphans: '孤立派生文件', all_regenerable: '全部可再生成缓存' } as Record<string, string>)[scope] || scope)}。${this.plan.files} 个文件，${bytes(this.plan.bytes)}；跳过 ${this.plan.skipped} 项使用中内容。</p><p>只清理此计划，原片、已登记封面、片目和备份保留。计划过期后需重新计算。</p><button class="button danger" data-cache-cleanup>确认清理这 ${this.plan.files} 个缓存文件</button></div>`;
        } else {
          const plan = this.plan;
          if (!plan || Date.now() >= plan.expires) throw new Error('清理计划已失效，请重新查看计划。');
          this.plan = null;
          const value = await localPost('/api/cache/cleanup', { plan_token: plan.token, confirm: true }, this.controller.signal);
          if (!this.disposed) { this.cleaned?.(); this.notify?.(isRecord(value) ? `缓存已清理 ${finite(value.deleted_files)} 个，${bytes(finite(value.deleted_bytes))}。${typeof value.notice === 'string' ? value.notice : ''}` : '缓存清理完成。', false); await this.load(); }
        }
      } catch (error: unknown) { if (!this.disposed) this.notify?.(errorMessage(error), true); }
      finally { if (button.isConnected) button.disabled = false; }
    })();
  };
}
