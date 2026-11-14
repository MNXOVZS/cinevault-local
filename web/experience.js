// CineVault typed playback/preview experience. Source: frontend/src; rebuild: npm ci && npm run build.
"use strict";
(() => {
  // src/contracts.ts
  function isRecord(value) {
    return typeof value === "object" && value !== null && !Array.isArray(value);
  }
  function localMediaUrl(value) {
    return typeof value === "string" && /^\/(?:media|assets)\//.test(value) && !value.startsWith("//") ? value : null;
  }
  function preparationFromUnknown(value) {
    if (!isRecord(value)) throw new Error("播放准备返回了无效数据。");
    const status = value.status;
    if (status !== "ready" && status !== "queued" && status !== "processing" && status !== "error") {
      throw new Error("播放准备状态无法识别，请重试。");
    }
    const mode = value.mode === "remux" || value.mode === "transcode" ? value.mode : "original";
    const url = localMediaUrl(value.url);
    if (status === "ready" && !url) throw new Error("播放地址尚未就绪。");
    const notice = typeof value.notice === "string" ? value.notice : typeof value.error === "string" ? value.error : "";
    return { status, mode, url, notice };
  }
  function errorMessage(error) {
    return error instanceof Error ? error.message : "本地服务暂时无法完成操作。";
  }
  async function localPost(path, body, signal) {
    if (!path.startsWith("/api/")) throw new Error("仅允许请求本机资料库接口。");
    const response = await fetch(path, {
      method: "POST",
      credentials: "same-origin",
      signal,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    const value = await response.json();
    if (!response.ok) {
      throw new Error(isRecord(value) && typeof value.error === "string" ? value.error : `本机请求失败（${response.status}）`);
    }
    return value;
  }
  function displayTime(seconds) {
    const value = Math.max(0, Math.floor(Number.isFinite(seconds) ? seconds : 0));
    const hours = Math.floor(value / 3600);
    return `${hours ? `${hours}:` : ""}${String(Math.floor(value % 3600 / 60)).padStart(2, "0")}:${String(value % 60).padStart(2, "0")}`;
  }
  function nearestFrame(frames, seconds) {
    let selected = null;
    let distance = Infinity;
    for (const frame of frames) {
      if (!Number.isFinite(frame.time) || !localMediaUrl(frame.url)) continue;
      const candidate = Math.abs(frame.time - seconds);
      if (candidate < distance) {
        selected = frame;
        distance = candidate;
      }
    }
    return selected;
  }

  // src/player.ts
  function abortableDelay(ms, signal) {
    return new Promise((resolve, reject) => {
      const abort = () => {
        window.clearTimeout(timer);
        reject(new DOMException("Stopped", "AbortError"));
      };
      const timer = window.setTimeout(() => {
        signal.removeEventListener("abort", abort);
        resolve();
      }, ms);
      signal.addEventListener("abort", abort, { once: true });
      if (signal.aborted) abort();
    });
  }
  var CinemaPlayer = class {
    constructor(options) {
      this.options = options;
      this.controller = new AbortController();
      this.overlay = document.createElement("div");
      this.message = document.createElement("p");
      this.action = document.createElement("button");
      this.preview = document.createElement("div");
      this.previewImage = document.createElement("img");
      this.previewTime = document.createElement("span");
      this.previewMessage = document.createElement("p");
      this.generateFrames = document.createElement("button");
      this.disposed = false;
      this.preparing = false;
      this.forcedFallback = false;
      this.currentMode = "original";
      this.actionKind = "play";
      this.waitingForVisibility = false;
      this.currentFrameUrl = "";
      this.requestingFrames = false;
      this.generationBaseline = "";
      this.generationSawActive = false;
      this.generationJobId = null;
      this.previewInput = null;
      this.onAction = () => {
        this.action.hidden = true;
        if (this.actionKind === "play" || this.actionKind === "sound") void this.attemptPlay();
        else void this.prepare(this.forcedFallback, true);
      };
      this.onVideoError = () => {
        if (!this.preparing && this.options.video.error?.code !== 1) this.recoverDecode();
      };
      this.onVisibility = () => {
        if (!document.hidden && this.waitingForVisibility) {
          this.waitingForVisibility = false;
          void this.attemptPlay();
        }
      };
      this.onPointerMove = (event) => {
        if (!(event.target instanceof Element)) return;
        if (event.target.closest(".seek-frame-preview")) return;
        const track = event.target.closest(".custom-seek");
        const input2 = event.target.closest("#custom-seek") || track?.querySelector("#custom-seek");
        if (!input2) return;
        const rect = input2.getBoundingClientRect();
        if (!rect.width) return;
        const fraction = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
        this.renderPreview(input2, fraction);
      };
      this.onPreviewImageLoad = () => {
        if (!this.disposed && !this.requestingFrames && this.currentFrameUrl) this.setPreviewState("ready");
      };
      this.onPreviewImageError = () => {
        if (!this.disposed && !this.requestingFrames && this.currentFrameUrl) this.setPreviewState("error", "抽样画面读取失败，可重新分析修复。");
      };
      this.onGenerateFrames = (event) => {
        event.preventDefault();
        event.stopPropagation();
        if (this.requestingFrames || this.disposed) return;
        this.requestingFrames = true;
        this.generationJobId = null;
        this.generationBaseline = JSON.stringify([this.item.status, this.item.error]);
        this.generationSawActive = false;
        this.setPreviewState("queued", "正在提交画面分析…");
        void localPost(`/api/videos/${encodeURIComponent(this.item.id)}/analyze`, {}, this.controller.signal).then((result) => {
          if (this.disposed) return;
          if (!isRecord(result) || typeof result.job_id !== "string" || !result.job_id) throw new Error("分析已在队列中，或后台任务已满，请稍后查看或重试。");
          this.generationJobId = result.job_id;
          if (this.requestingFrames) this.setPreviewState("queued", "已加入画面分析队列，完成后自动显示。");
          this.options.onState?.("抽样画面分析已加入队列。");
        }).catch((error) => {
          this.requestingFrames = false;
          if (!this.disposed) this.setPreviewState("error", errorMessage(error));
        });
      };
      this.onSeekFocus = (event) => {
        if (!(event.target instanceof Element)) return;
        const input2 = event.target.closest("#custom-seek");
        if (!input2) return;
        const max = Number(input2.max) || this.item.duration || 1;
        this.renderPreview(input2, Math.min(1, Math.max(0, Number(input2.value) / max || 0)));
      };
      this.onSeekInput = (event) => {
        this.onSeekFocus(event);
      };
      this.onSeekFocusOut = (event) => {
        const next = event.relatedTarget;
        if (!(next instanceof Element) || next.closest(".custom-seek") !== this.preview.parentElement) this.preview.hidden = true;
      };
      this.onPointerOut = (event) => {
        if (!(event.target instanceof Element)) return;
        const parent = event.target.closest(".custom-seek");
        if (!parent) return;
        const next = event.relatedTarget;
        if (!(next instanceof Element) || next.closest(".custom-seek") !== parent) this.preview.hidden = true;
      };
      this.onPointerUp = (event) => {
        if (event.type !== "pointercancel" && event.pointerType !== "touch" && event.pointerType !== "pen") return;
        if (this.hidePreviewTimer !== void 0) window.clearTimeout(this.hidePreviewTimer);
        this.hidePreviewTimer = window.setTimeout(() => {
          this.preview.hidden = true;
        }, 1500);
      };
      this.item = options.item;
      this.overlay.className = "experience-playback-overlay";
      const box = document.createElement("div");
      box.className = "experience-playback-message";
      this.action.className = "button primary";
      this.action.type = "button";
      this.action.hidden = true;
      box.append(this.message, this.action);
      this.overlay.append(box);
      options.video.parentElement?.append(this.overlay);
      this.preview.className = "seek-frame-preview";
      this.preview.id = `seek-frame-preview-${this.item.id}`;
      this.preview.setAttribute("role", "group");
      this.preview.setAttribute("aria-label", "时间轴抽样画面");
      this.preview.hidden = true;
      this.previewImage.alt = "时间轴附近的抽样画面";
      this.previewImage.decoding = "async";
      this.previewMessage.className = "seek-preview-message";
      this.previewMessage.setAttribute("role", "status");
      this.generateFrames.className = "button ghost small seek-preview-generate";
      this.generateFrames.type = "button";
      this.generateFrames.textContent = "生成抽样画面";
      this.preview.append(this.previewImage, this.previewTime, this.previewMessage, this.generateFrames);
      options.video.muted = false;
      options.video.playsInline = true;
      this.action.addEventListener("click", this.onAction);
      options.video.addEventListener("error", this.onVideoError);
      options.root.addEventListener("pointermove", this.onPointerMove);
      options.root.addEventListener("pointerdown", this.onPointerMove);
      options.root.addEventListener("pointerout", this.onPointerOut);
      options.root.addEventListener("pointerup", this.onPointerUp);
      options.root.addEventListener("pointercancel", this.onPointerUp);
      options.root.addEventListener("focusin", this.onSeekFocus);
      options.root.addEventListener("focusout", this.onSeekFocusOut);
      options.root.addEventListener("input", this.onSeekInput);
      this.previewImage.addEventListener("load", this.onPreviewImageLoad);
      this.previewImage.addEventListener("error", this.onPreviewImageError);
      this.generateFrames.addEventListener("click", this.onGenerateFrames);
      document.addEventListener("visibilitychange", this.onVisibility);
      void this.prepare(false, false);
    }
    update(item) {
      if (item.id !== this.item.id) return;
      const previousFrames = JSON.stringify(this.item.frames);
      this.item = item;
      if (this.requestingFrames) {
        const baseline = JSON.stringify([item.status, item.error]);
        const job = item.frame_analysis?.id === this.generationJobId ? item.frame_analysis : null;
        if (item.status === "analyzing" || item.status === "queued") this.generationSawActive = true;
        if (job && ["error", "cancelled", "interrupted"].includes(job.status)) {
          this.requestingFrames = false;
          this.setPreviewState("error", job.error || "这次抽样分析没有完成，可稍后重试。");
        } else if (previousFrames !== JSON.stringify(item.frames) || job?.status === "complete") {
          this.requestingFrames = false;
          this.currentFrameUrl = "";
          if (!nearestFrame(item.frames, 0)) this.setPreviewState("error", "分析已完成，但没有可读取的抽样画面，请检查媒体情况。");
        } else if (["error", "missing", "changed"].includes(item.status || "") && (this.generationSawActive || baseline !== this.generationBaseline)) {
          this.requestingFrames = false;
          this.setPreviewState("error", item.error || "未能生成抽样画面，请检查原片和来源。");
        }
      }
      if (!this.preview.hidden && this.previewFraction !== void 0) {
        const input2 = this.options.root.querySelector("#custom-seek");
        if (input2) this.renderPreview(input2, this.previewFraction);
      }
    }
    dispose() {
      if (this.disposed) return;
      this.disposed = true;
      this.controller.abort();
      this.options.video.pause();
      if (this.hidePreviewTimer !== void 0) window.clearTimeout(this.hidePreviewTimer);
      this.action.removeEventListener("click", this.onAction);
      this.options.video.removeEventListener("error", this.onVideoError);
      this.options.root.removeEventListener("pointermove", this.onPointerMove);
      this.options.root.removeEventListener("pointerdown", this.onPointerMove);
      this.options.root.removeEventListener("pointerout", this.onPointerOut);
      this.options.root.removeEventListener("pointerup", this.onPointerUp);
      this.options.root.removeEventListener("pointercancel", this.onPointerUp);
      this.options.root.removeEventListener("focusin", this.onSeekFocus);
      this.options.root.removeEventListener("focusout", this.onSeekFocusOut);
      this.options.root.removeEventListener("input", this.onSeekInput);
      this.previewImage.removeEventListener("load", this.onPreviewImageLoad);
      this.previewImage.removeEventListener("error", this.onPreviewImageError);
      this.generateFrames.removeEventListener("click", this.onGenerateFrames);
      document.removeEventListener("visibilitychange", this.onVisibility);
      this.preview.remove();
      if (this.previewInput?.getAttribute("aria-describedby") === this.preview.id) this.previewInput.removeAttribute("aria-describedby");
      this.overlay.remove();
    }
    show(message, action) {
      if (this.disposed) return;
      this.overlay.hidden = false;
      this.overlay.classList.toggle("actionable", !!action);
      this.overlay.classList.toggle("sound-prompt", action === "sound");
      this.message.textContent = message;
      this.action.hidden = !action;
      if (action) {
        this.actionKind = action;
        this.action.textContent = action === "sound" ? "点击开启声音" : action === "play" ? "播放（开启声音）" : "重试准备播放";
      }
      this.options.onState?.(message);
    }
    async prepare(force, retry) {
      if (this.disposed || this.preparing) return;
      this.preparing = true;
      this.show(force ? "正在自动准备浏览器兼容版本，原片保留…" : "正在准备播放…");
      try {
        for (let attempt = 0; attempt < 240 && !this.disposed; attempt++) {
          const body = {};
          if (force) body.force_compatible = true;
          if (retry && attempt === 0) body.retry = true;
          const result = preparationFromUnknown(await localPost(`/api/videos/${encodeURIComponent(this.item.id)}/prepare-playback`, body, this.controller.signal));
          if (this.disposed) return;
          if (result.status === "ready") {
            this.preparing = false;
            await this.useSource(result);
            return;
          }
          if (result.status === "error") {
            this.show(result.notice || "准备播放失败。请检查原片与媒体引擎后重试。", "retry");
            return;
          }
          this.show(result.notice || (result.status === "queued" ? "兼容播放已排队；准备完成后会自动尝试播放。" : "正在准备兼容播放，原片不会被覆盖…"));
          await abortableDelay(1500, this.controller.signal);
        }
        if (!this.disposed) this.show("准备仍未完成。可以稍后重试，后台任务会保留。", "retry");
      } catch (error) {
        if (!this.disposed) this.show(errorMessage(error), "retry");
      } finally {
        this.preparing = false;
      }
    }
    async useSource(result) {
      if (!result.url || this.disposed) return;
      const video = this.options.video;
      const position = video.readyState >= 1 && video.currentTime > 0 ? video.currentTime : this.item.last_position || 0;
      this.currentMode = result.mode;
      video.dataset.playbackMode = result.mode;
      if (video.getAttribute("src") !== result.url) {
        video.addEventListener("loadedmetadata", () => {
          if (!this.disposed && position > 0 && Number.isFinite(video.duration) && position < video.duration - 1) video.currentTime = position;
        }, { once: true });
        video.src = result.url;
        video.load();
      }
      this.options.onPrepared?.(result);
      if (document.hidden) {
        this.waitingForVisibility = true;
        this.show("播放已准备完成，返回此页面后会尝试播放。");
        return;
      }
      await this.attemptPlay();
    }
    async attemptPlay() {
      if (this.disposed) return;
      const video = this.options.video;
      video.muted = false;
      try {
        await video.play();
        if (!this.disposed) {
          this.overlay.hidden = true;
          this.action.hidden = true;
        }
      } catch (error) {
        if (this.disposed) return;
        if (error instanceof DOMException && error.name === "NotAllowedError" || error instanceof Error && error.name === "NotAllowedError") {
          video.muted = true;
          try {
            await video.play();
            if (!this.disposed) this.show("已静音播放", "sound");
          } catch (mutedError) {
            if (this.disposed) return;
            if (mutedError instanceof Error && mutedError.name === "NotSupportedError" || video.error?.code === 4) this.recoverDecode();
            else this.show("浏览器需要一次点击才能播放。", "play");
          }
        } else if (error instanceof Error && error.name === "NotSupportedError" || video.error?.code === 4) {
          this.recoverDecode();
        } else {
          this.show("当前流暂时无法播放，可重试准备。" + (errorMessage(error) ? ` ${errorMessage(error)}` : ""), "retry");
        }
      }
    }
    recoverDecode() {
      if (this.disposed || this.preparing) return;
      if (!this.forcedFallback && this.currentMode !== "transcode") {
        this.forcedFallback = true;
        this.options.video.pause();
        void this.prepare(true, false);
      } else {
        this.show("此浏览器仍无法播放当前流。可重试准备；原片保持原状。", "retry");
      }
    }
    renderPreview(input2, fraction) {
      if (this.disposed) return;
      const parent = input2.closest(".custom-seek");
      if (!parent) return;
      if (this.hidePreviewTimer !== void 0) window.clearTimeout(this.hidePreviewTimer);
      this.hidePreviewTimer = void 0;
      this.previewFraction = fraction;
      const duration = Number.isFinite(this.options.video.duration) && this.options.video.duration > 0 ? this.options.video.duration : this.item.duration;
      const target = fraction * Math.max(0, duration);
      const frame = nearestFrame(this.item.frames, target);
      if (this.preview.parentElement !== parent) parent.append(this.preview);
      input2.setAttribute("aria-describedby", this.preview.id);
      this.previewInput = input2;
      this.preview.hidden = false;
      if (frame) {
        this.previewTime.textContent = `${displayTime(target)} · 近似抽样 ${displayTime(frame.time)}`;
        if (this.currentFrameUrl !== frame.url) {
          this.currentFrameUrl = frame.url;
          this.setPreviewState("loading", "正在读取抽样画面…");
          this.previewImage.src = frame.url;
          if (this.previewImage.complete && this.previewImage.naturalWidth > 0) this.onPreviewImageLoad();
        }
      } else {
        this.currentFrameUrl = "";
        this.previewImage.removeAttribute("src");
        this.previewTime.textContent = displayTime(target);
        if (this.preview.dataset.state !== "error" || this.requestingFrames) this.setPreviewState(this.requestingFrames ? "queued" : "empty", this.requestingFrames ? "抽样画面正在后台准备。" : "尚无抽样画面，可重新分析生成。");
      }
      const rect = input2.getBoundingClientRect();
      const parentRect = parent.getBoundingClientRect();
      const width = this.preview.offsetWidth || 176;
      const x = rect.left - parentRect.left + fraction * rect.width;
      const left = parentRect.width <= width ? parentRect.width / 2 : Math.max(width / 2, Math.min(parentRect.width - width / 2, x));
      this.preview.style.left = `${left}px`;
    }
    setPreviewState(state, message = "") {
      this.preview.dataset.state = state;
      this.previewImage.hidden = state !== "ready";
      this.previewMessage.hidden = state === "ready";
      this.previewMessage.textContent = message;
      this.generateFrames.hidden = state !== "empty" && state !== "error";
      this.generateFrames.disabled = this.requestingFrames;
    }
  };

  // src/previews.ts
  function previewPreparationPolicy() {
    const device = typeof navigator === "undefined" ? null : navigator;
    const constrained = !!device && (!!device.connection?.saveData || !!device.hardwareConcurrency && device.hardwareConcurrency < 4 || !!device.deviceMemory && device.deviceMemory < 4);
    return { pending: constrained ? 1 : 2, interval: constrained ? 3200 : 2200 };
  }
  var CardPreviewManager = class {
    constructor() {
      this.cards = [];
      this.observer = null;
      this.options = { enabled: true };
      this.motion = window.matchMedia("(prefers-reduced-motion: reduce)");
      this.requested = /* @__PURE__ */ new Set();
      this.pending = /* @__PURE__ */ new Set();
      this.requests = /* @__PURE__ */ new Set();
      this.brokenUrls = /* @__PURE__ */ new Map();
      this.recoveryRequested = /* @__PURE__ */ new Set();
      this.requestBusy = false;
      this.lastRequestAt = -Infinity;
      this.suspended = false;
      this.onVisibility = () => {
        if (!this.observer) this.measureVisibility();
        this.reconcile();
      };
      this.onFallbackScroll = () => {
        if (!this.observer) {
          this.measureVisibility();
          this.reconcile();
        }
      };
      document.addEventListener("visibilitychange", this.onVisibility);
      this.motion.addEventListener("change", this.onVisibility);
      window.addEventListener("scroll", this.onFallbackScroll, { passive: true });
      window.addEventListener("resize", this.onFallbackScroll, { passive: true });
    }
    mount(root, items, options) {
      this.suspended = false;
      this.options = options;
      const itemMap = new Map(items.map((item) => [item.id, item]));
      const currentIds = new Set(itemMap.keys());
      for (const id of this.pending) if (!currentIds.has(id)) this.pending.delete(id);
      for (const item of items) {
        if (item.preview_status === "ready" || item.preview_status === "error") this.pending.delete(item.id);
        else if (item.preview_status === "queued" || item.preview_status === "processing") this.pending.add(item.id);
      }
      const existing = new Map(this.cards.map((card) => [card.element, card]));
      const next = [];
      for (const element of root.querySelectorAll(".card-visual[data-open]")) {
        const item = itemMap.get(element.dataset.open || "");
        const image = element.querySelector("img");
        if (!item || item.kind !== "video" || !image) continue;
        let card = existing.get(element);
        if (card && card.item.id !== item.id) {
          this.release(card);
          card = void 0;
        }
        if (card) {
          existing.delete(element);
          card.item = item;
          this.updateUrl(card);
        } else {
          const label = document.createElement("span");
          label.className = "automatic-preview-label";
          label.hidden = true;
          element.append(label);
          const move = () => {
            if (card) {
              card.scrubbing = true;
              this.pauseClip(card);
              card.image.style.opacity = "1";
              if (card.clip) card.clip.style.opacity = "0";
              this.label(card, "鼠标位置 · 近似抽样");
            }
          };
          const leave = () => {
            if (!card) return;
            card.scrubbing = false;
            card.element.classList.remove("scrubbing");
            this.activate(card);
          };
          card = { element, image, item, label, ratio: 0, clip: null, clipUrl: null, clipFailed: false, policyBlocked: false, recoveryQueued: false, active: false, playing: false, scrubbing: false, move, leave };
          this.updateUrl(card);
          element.addEventListener("pointermove", move);
          element.addEventListener("pointerleave", leave);
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
    suspend() {
      this.suspended = true;
      this.clearTimer();
      for (const card of this.cards) this.deactivate(card);
    }
    resetPreparation() {
      this.requested.clear();
      this.pending.clear();
      this.brokenUrls.clear();
      this.recoveryRequested.clear();
    }
    /** Navigation/page removal releases resources; ordinary scroll and polling do not. */
    stop() {
      this.clearTimer();
      for (const request of this.requests) request.abort();
      this.requests.clear();
      this.observer?.disconnect();
      this.observer = null;
      for (const card of this.cards) this.release(card);
      this.cards = [];
    }
    allowed() {
      return this.options.enabled && !this.motion.matches && !document.hidden && !this.suspended;
    }
    ensureObserver() {
      if (this.observer || typeof IntersectionObserver === "undefined") return;
      this.observer = new IntersectionObserver((entries) => {
        for (const entry of entries) {
          const card = this.cards.find((candidate) => candidate.element === entry.target);
          if (card) card.ratio = entry.isIntersecting ? entry.intersectionRatio : 0;
        }
        this.reconcile();
      }, { threshold: [0, 1e-4] });
    }
    measureVisibility() {
      for (const card of this.cards) {
        const rect = card.element.getBoundingClientRect();
        card.ratio = rect.bottom > 0 && rect.top < window.innerHeight && rect.right > 0 && rect.left < window.innerWidth ? 1 : 0;
      }
    }
    updateUrl(card) {
      const url = localMediaUrl(card.item.preview_url);
      const broken = !!url && this.brokenUrls.get(card.item.id) === url;
      if (url && !broken && url !== card.clipUrl) {
        this.brokenUrls.delete(card.item.id);
        card.clipFailed = false;
        card.policyBlocked = false;
        card.recoveryQueued = false;
        card.clipUrl = url;
        if (card.clip) {
          this.pauseClip(card);
          card.clip.style.opacity = "0";
          card.clip.src = url;
          card.clip.load();
        }
      } else if (broken) {
        card.clipUrl = null;
        card.clipFailed = true;
        card.recoveryQueued = !this.recoveryRequested.has(card.item.id);
      }
      if (!url && card.clipUrl) {
        card.clipUrl = null;
        this.pauseClip(card);
        if (card.clip) card.clip.style.opacity = "0";
        card.image.style.opacity = "1";
      }
    }
    reconcile() {
      this.clearTimer();
      if (!this.allowed()) {
        for (const card of this.cards) this.deactivate(card);
        return;
      }
      const visible = this.cards.filter((card) => card.ratio > 0 && card.element.isConnected);
      for (const card of this.cards) {
        if (card.ratio > 0 && card.element.isConnected) this.activate(card);
        else this.deactivate(card);
      }
      const recovery = visible.find((card) => card.recoveryQueued && !this.recoveryRequested.has(card.item.id));
      const candidate = visible.find((card) => !this.requested.has(card.item.id) && !card.clipUrl && !card.clipFailed && !["error", "queued", "processing"].includes(card.item.preview_status || ""));
      if (recovery) void this.requestClip(recovery, true);
      else if (candidate) void this.requestClip(candidate);
      if (recovery || candidate) this.timer = window.setTimeout(() => {
        this.timer = void 0;
        if (!this.observer) this.measureVisibility();
        this.reconcile();
      }, Math.max(700, previewPreparationPolicy().interval - (performance.now() - this.lastRequestAt)));
    }
    label(card, text) {
      card.label.hidden = !card.active;
      if (card.label.textContent !== text) card.label.textContent = text;
      card.element.dataset.previewState = card.clipFailed ? "error" : card.policyBlocked ? "blocked" : card.clipUrl ? "ready" : card.item.preview_status || "unavailable";
    }
    activate(card) {
      card.active = this.allowed() && card.ratio > 0 && card.element.isConnected;
      if (!card.active) return;
      if (card.scrubbing || card.element.classList.contains("scrubbing")) {
        this.label(card, "鼠标位置 · 近似抽样");
        return;
      }
      if (!card.clipUrl || card.clipFailed || card.policyBlocked) {
        const status = card.item.preview_status;
        this.label(card, card.policyBlocked ? "自动播放受限 · 点击观看" : status === "processing" ? "短片准备中 · 静态封面" : status === "queued" || this.pending.has(card.item.id) ? "短片已排队 · 静态封面" : card.clipFailed || status === "error" ? "短片暂不可用 · 静态封面" : "等待短片预览 · 静态封面");
        return;
      }
      if (!card.clip) this.createClip(card);
      const clip = card.clip;
      if (!clip || card.playing || !clip.paused) return;
      card.playing = true;
      this.label(card, "正在载入静音短片");
      void clip.play().then(() => {
        card.playing = false;
        if (!card.active || !this.allowed() || card.scrubbing) {
          clip.pause();
          return;
        }
        if (clip.readyState >= 2) this.reveal(card, clip);
      }).catch((error) => {
        card.playing = false;
        if (card.clip !== clip || !card.element.isConnected || card.clipFailed || !card.active || !this.allowed() || card.scrubbing) return;
        if (error instanceof Error && error.name === "AbortError") return;
        card.policyBlocked = true;
        clip.pause();
        clip.style.opacity = "0";
        card.image.style.opacity = "1";
        this.label(card, "自动播放受限 · 点击观看");
      });
    }
    createClip(card) {
      const clip = document.createElement("video");
      clip.className = "automatic-card-preview";
      clip.muted = true;
      clip.loop = true;
      clip.playsInline = true;
      clip.preload = "metadata";
      clip.disablePictureInPicture = true;
      clip.style.opacity = "0";
      clip.src = card.clipUrl || "";
      clip.addEventListener("playing", () => this.reveal(card, clip));
      clip.addEventListener("loadeddata", () => {
        if (!clip.paused) this.reveal(card, clip);
      });
      clip.addEventListener("error", () => {
        if (card.clip !== clip) return;
        const failed = card.clipUrl;
        if (failed) this.brokenUrls.set(card.item.id, failed);
        card.clipFailed = true;
        card.clipUrl = null;
        card.playing = false;
        card.recoveryQueued = !this.recoveryRequested.has(card.item.id);
        this.pauseClip(card);
        clip.style.opacity = "0";
        card.image.style.opacity = "1";
        this.label(card, "短片暂不可用 · 静态封面");
        void this.requestClip(card, true);
      });
      card.element.append(clip);
      card.clip = clip;
    }
    reveal(card, clip) {
      if (card.clip !== clip || !card.active || !this.allowed() || card.scrubbing || clip.readyState < 2) return;
      clip.style.opacity = "1";
      card.image.style.opacity = "0";
      this.label(card, "静音短片预览");
      this.recordQuality(card);
    }
    recordQuality(card) {
      if (!card.clip || typeof card.clip.getVideoPlaybackQuality !== "function") return;
      const quality = card.clip.getVideoPlaybackQuality();
      card.element.dataset.previewDecodedFrames = String(quality.totalVideoFrames);
      card.element.dataset.previewDroppedFrames = String(quality.droppedVideoFrames);
    }
    pauseClip(card) {
      if (card.clip) {
        if (!card.clip.paused) card.clip.pause();
        this.recordQuality(card);
      }
    }
    deactivate(card) {
      card.active = false;
      card.label.hidden = true;
      this.pauseClip(card);
    }
    release(card) {
      this.deactivate(card);
      this.observer?.unobserve(card.element);
      card.element.removeEventListener("pointermove", card.move);
      card.element.removeEventListener("pointerleave", card.leave);
      if (card.clip) {
        card.clip.removeAttribute("src");
        card.clip.load();
        card.clip.remove();
        card.clip = null;
      }
      card.image.style.opacity = "1";
      card.label.remove();
    }
    async requestClip(card, recover = false) {
      const policy = previewPreparationPolicy();
      if (!this.allowed() || !card.active || card.ratio <= 0 || this.requestBusy || this.pending.size >= policy.pending || performance.now() - this.lastRequestAt < policy.interval) return;
      if (recover ? !card.recoveryQueued || this.recoveryRequested.has(card.item.id) : this.requested.has(card.item.id) || card.clipUrl || card.clipFailed || ["error", "queued", "processing"].includes(card.item.preview_status || "")) return;
      this.requestBusy = true;
      this.lastRequestAt = performance.now();
      this.requested.add(card.item.id);
      if (recover) {
        this.recoveryRequested.add(card.item.id);
        card.recoveryQueued = false;
      }
      const request = new AbortController();
      this.requests.add(request);
      this.label(card, "正在请求短片 · 静态封面");
      try {
        const value = await localPost(`/api/videos/${encodeURIComponent(card.item.id)}/preview`, recover ? { retry: true } : {}, request.signal);
        if (!request.signal.aborted && isRecord(value)) {
          if (value.status === "queued" || value.status === "processing") {
            this.pending.add(card.item.id);
            this.label(card, "短片已排队 · 静态封面");
          }
          if (value.status === "ready") {
            const url = localMediaUrl(value.url);
            this.pending.delete(card.item.id);
            if (url && this.brokenUrls.get(card.item.id) !== url) {
              card.item = { ...card.item, preview_url: url, preview_status: "ready" };
              this.updateUrl(card);
              this.activate(card);
            }
          }
          if (value.status === "error") {
            this.pending.delete(card.item.id);
            this.label(card, "短片暂不可用 · 静态封面");
          }
        }
      } catch {
        if (!request.signal.aborted) this.label(card, "短片请求失败 · 静态封面");
      } finally {
        this.requests.delete(request);
        this.requestBusy = false;
      }
    }
    clearTimer() {
      if (this.timer !== void 0) window.clearTimeout(this.timer);
      this.timer = void 0;
    }
  };

  // src/analysis.ts
  var escape = (value) => String(value).replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character] || character);
  function strings(value) {
    return Array.isArray(value) ? value.filter((entry) => typeof entry === "string") : [];
  }
  function numeric(record, key) {
    const value = record[key];
    if (typeof value !== "number" || !Number.isFinite(value)) throw new Error("分析参数尚未完整返回，请刷新后重试。");
    return value;
  }
  function signalConfig(value) {
    if (!isRecord(value) || typeof value.enabled !== "boolean") throw new Error("分析参数格式无法识别。");
    return { enabled: value.enabled, sensitivity: numeric(value, "sensitivity"), minimum_event_gap: numeric(value, "minimum_event_gap"), merge_window: numeric(value, "merge_window") };
  }
  function enginesFromUnknown(value) {
    if (!isRecord(value) || !Array.isArray(value.engines) || !isRecord(value.config) || !isRecord(value.config.speech)) throw new Error("分析引擎信息尚未就绪。");
    const speech = value.config.speech;
    if (typeof speech.enabled !== "boolean") throw new Error("语音参数格式无法识别。");
    const engines = [];
    for (const item of value.engines) {
      if (!isRecord(item) || typeof item.id !== "string" || typeof item.name !== "string") continue;
      engines.push({ id: item.id, name: item.name, kind: typeof item.kind === "string" ? item.kind : "unknown", installed: typeof item.installed === "boolean" ? item.installed : null, available: item.available === true, status: typeof item.status === "string" ? item.status : "unknown", description: typeof item.description === "string" ? item.description : "", limitations: strings(item.limitations) });
    }
    return { engines, limitations: strings(value.limitations), config: { visual: signalConfig(value.config.visual), audio: signalConfig(value.config.audio), speech: { enabled: speech.enabled, threshold: numeric(speech, "threshold"), min_speech_duration: numeric(speech, "min_speech_duration"), min_silence_duration: numeric(speech, "min_silence_duration") } } };
  }
  var AnalysisManager = class {
    constructor(root, notify) {
      this.root = root;
      this.notify = notify;
      this.disposed = false;
      this.response = null;
      this.controller = new AbortController();
      this.onSubmit = (event) => {
        if (!(event.target instanceof HTMLFormElement) || !event.target.hasAttribute("data-analysis-config")) return;
        event.preventDefault();
        event.stopPropagation();
        const form = event.target;
        const button = form.querySelector('button[type="submit"]');
        if (button) button.disabled = true;
        void (async () => {
          try {
            const config = { visual: this.readSignal(form, "visual"), audio: this.readSignal(form, "audio"), speech: { enabled: this.readInput(form, "speech.enabled").checked, threshold: this.readNumber(form, "speech.threshold", 0.1, 0.95), min_speech_duration: this.readNumber(form, "speech.min_speech_duration", 0.1, 5), min_silence_duration: this.readNumber(form, "speech.min_silence_duration", 0.1, 3) } };
            const result = await this.request("POST", config);
            if (!this.disposed) {
              this.response = result;
              this.render(result);
              this.notify?.("分析参数已保存，随后执行的分析使用这些参数。", false);
            }
          } catch (error) {
            if (!this.disposed) this.notify?.(errorMessage(error), true);
          } finally {
            if (button?.isConnected) button.disabled = false;
          }
        })();
      };
      this.onClick = (event) => {
        if (event.target instanceof Element && event.target.closest("[data-engine-refresh]")) {
          event.preventDefault();
          void this.load();
        }
      };
      root.addEventListener("submit", this.onSubmit);
      root.addEventListener("click", this.onClick);
      void this.load();
    }
    dispose() {
      this.disposed = true;
      this.controller.abort();
      this.root.removeEventListener("submit", this.onSubmit);
      this.root.removeEventListener("click", this.onClick);
    }
    refresh() {
      void this.load();
    }
    async request(method, config) {
      const response = await fetch("/api/analysis-engines", { method, credentials: "same-origin", signal: this.controller.signal, ...config ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(config) } : {} });
      const value = await response.json();
      if (!response.ok) throw new Error(isRecord(value) && typeof value.error === "string" ? value.error : `引擎请求失败（${response.status}）`);
      return enginesFromUnknown(value);
    }
    async load() {
      if (this.disposed) return;
      if (!this.response) this.root.innerHTML = '<div class="loading"><span class="spinner"></span>正在读取本机引擎状态…</div>';
      try {
        const result = await this.request("GET");
        if (!this.disposed) {
          this.response = result;
          this.render(result);
        }
      } catch (error) {
        if (!this.disposed) {
          this.root.innerHTML = `<div class="notice error">${escape(errorMessage(error))}</div><button class="button ghost" data-engine-refresh>重新读取引擎状态</button>`;
        }
      }
    }
    engineStatus(engine) {
      if (engine.status === "configured_unverified") return "已配置 · 未核验";
      if (engine.available) return engine.kind === "algorithm" ? "算法可用" : engine.installed === true ? "本机模型已安装" : "当前可用";
      if (engine.installed === false || engine.status === "not_installed") return "未安装";
      return engine.installed === true ? "已安装 · 暂不可运行" : "尚未就绪";
    }
    numberInput(name, label, value, min, max, step, suffix = "") {
      return `<label class="analysis-number"><span>${escape(label)}${suffix ? `（${escape(suffix)}）` : ""}</span><input name="${name}" type="number" required min="${min}" max="${max}" step="${step}" value="${value}"></label>`;
    }
    signalForm(id, config) {
      const available = this.response?.engines.find((engine) => engine.id === id)?.available === true;
      return `<section class="analysis-parameters"><div><h3>${id === "visual" ? "场景切换" : "音量变化"}算法</h3><label class="check-row"><input type="checkbox" name="${id}.enabled" ${config.enabled ? "checked" : ""} ${available ? "" : "disabled"}>纳入时间轴分析</label></div><div class="analysis-number-grid">${this.numberInput(`${id}.sensitivity`, "灵敏度", config.sensitivity, 0, 1, 0.05)}${this.numberInput(`${id}.minimum_event_gap`, "最小节点间隔", config.minimum_event_gap, 0.5, 30, 0.5, "秒")}${this.numberInput(`${id}.merge_window`, "相近候选合并窗口", config.merge_window, 0, 10, 0.25, "秒")}</div></section>`;
    }
    render(response) {
      this.root.innerHTML = `<div class="engine-grid">${response.engines.map((engine) => `<article class="engine-card ${engine.available ? "available" : "unavailable"}"><div class="engine-card-heading"><span class="engine-kind">${engine.kind === "algorithm" ? "信号算法" : engine.kind === "local_model" ? "本机模型" : engine.kind === "provider" ? "模型服务" : "能力状态"}</span><span class="engine-status">${escape(this.engineStatus(engine))}</span></div><h2>${escape(engine.name)}</h2><p>${escape(engine.description)}</p>${engine.limitations.length ? `<details><summary>能力范围与限制</summary><ul>${engine.limitations.map((limit) => `<li>${escape(limit)}</li>`).join("")}</ul></details>` : ""}</article>`).join("")}</div><section class="panel analysis-config-panel"><div class="panel-header"><h2>时间轴检测参数</h2><button class="button small ghost" data-engine-refresh>重新读取</button></div><p>参数只影响随后执行的分析。场景切换和音量变化来自信号算法；语音片段来自本机模型，不生成转写或识别说话人。</p><form data-analysis-config>${this.signalForm("visual", response.config.visual)}${this.signalForm("audio", response.config.audio)}<section class="analysis-parameters"><div><h3>语音片段检测</h3><label class="check-row"><input type="checkbox" name="speech.enabled" ${response.config.speech.enabled ? "checked" : ""} ${response.engines.find((engine) => engine.id === "speech")?.available ? "" : "disabled"}>纳入时间轴分析</label></div><div class="analysis-number-grid">${this.numberInput("speech.threshold", "语音概率阈值", response.config.speech.threshold, 0.1, 0.95, 0.05)}${this.numberInput("speech.min_speech_duration", "最短语音长度", response.config.speech.min_speech_duration, 0.1, 5, 0.05, "秒")}${this.numberInput("speech.min_silence_duration", "分段静音间隔", response.config.speech.min_silence_duration, 0.1, 3, 0.05, "秒")}</div></section><div class="button-row"><button class="button primary" type="submit">保存分析参数</button></div></form>${response.limitations.length ? `<details class="analysis-global-limits"><summary>共同限制</summary><ul>${response.limitations.map((limit) => `<li>${escape(limit)}</li>`).join("")}</ul></details>` : ""}</section>`;
    }
    readInput(form, name) {
      const input2 = form.elements.namedItem(name);
      if (!(input2 instanceof HTMLInputElement)) throw new Error("分析表单未完整加载，请刷新后重试。");
      return input2;
    }
    readNumber(form, name, min, max) {
      const value = Number(this.readInput(form, name).value);
      if (!Number.isFinite(value) || value < min || value > max) throw new Error("请检查参数范围后再保存。");
      return value;
    }
    readSignal(form, id) {
      return { enabled: this.readInput(form, `${id}.enabled`).checked, sensitivity: this.readNumber(form, `${id}.sensitivity`, 0, 1), minimum_event_gap: this.readNumber(form, `${id}.minimum_event_gap`, 0.5, 30), merge_window: this.readNumber(form, `${id}.merge_window`, 0, 10) };
    }
  };

  // src/management.ts
  var escape2 = (value) => String(value).replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character] || character);
  var finite = (value, fallback = 0) => typeof value === "number" && Number.isFinite(value) ? value : fallback;
  var bytes = (value) => value >= 1073741824 ? `${(value / 1073741824).toFixed(2)} GiB` : value >= 1048576 ? `${(value / 1048576).toFixed(1)} MiB` : `${Math.max(0, Math.round(value / 1024))} KiB`;
  function help(label, content) {
    return `<details class="inline-help"><summary>${escape2(label)}</summary><div class="inline-help-copy">${content}</div></details>`;
  }
  function input(form, name) {
    const field = form.elements.namedItem(name);
    if (!(field instanceof HTMLInputElement) && !(field instanceof HTMLSelectElement)) throw new Error("表单尚未完整加载。");
    return field;
  }
  function checked(form, name) {
    const field = input(form, name);
    return field instanceof HTMLInputElement && field.checked;
  }
  function number(form, name, min, max) {
    const value = Number(input(form, name).value);
    if (!Number.isInteger(value) || value < min || value > max) throw new Error(`请检查 ${name} 的整数范围：${min}–${max}。`);
    return value;
  }
  async function get(path, signal) {
    const response = await fetch(path, { credentials: "same-origin", signal });
    const value = await response.json();
    if (!response.ok) throw new Error(isRecord(value) && typeof value.error === "string" ? value.error : "本机管理接口暂不可用。");
    return value;
  }
  function providerFromUnknown(value) {
    if (!isRecord(value) || !isRecord(value.provider)) throw new Error("模型服务信息未完整返回。");
    const p = value.provider;
    if (p.kind !== "ollama" && p.kind !== "openai_compatible" && p.kind !== "managed_vision" || typeof p.base_url !== "string" || typeof p.model !== "string") throw new Error("模型服务配置格式无法识别。");
    const managed = isRecord(value.managed_model) ? value.managed_model : null;
    const localVision = managed ? {
      name: typeof managed.name === "string" ? managed.name : "Qwen3-VL-2B-Instruct",
      installed: managed.installed === true || finite(managed.weight_bytes) > 0,
      available: managed.available === true,
      bytes: finite(managed.weight_bytes, finite(managed.bytes)),
      notice: typeof managed.reason === "string" ? managed.reason : typeof managed.notice === "string" ? managed.notice : "",
      setup: typeof managed.setup_command === "string" ? managed.setup_command : "",
      runtime: typeof managed.runtime_status === "string" ? managed.runtime_status : ""
    } : null;
    return { kind: p.kind, managed: localVision, base_url: p.base_url, model: p.model, timeout_seconds: finite(p.timeout_seconds, 120), max_images: finite(p.max_images, 3), local_inference_only: p.local_inference_only === true, local_only: p.local_only === true, automatic_allowed: p.automatic_allowed === true, has_api_key: p.has_api_key === true, status: typeof p.status === "string" ? p.status : "not_configured" };
  }
  function cacheFromUnknown(value) {
    if (!isRecord(value) || !isRecord(value.groups) || !isRecord(value.config)) throw new Error("缓存统计未完整返回。");
    const groups = {};
    for (const name of ["previews", "compatible", "temporary", "orphans"]) {
      const group = value.groups[name];
      if (!isRecord(group)) throw new Error("缓存分组统计尚未就绪。");
      groups[name] = { files: finite(group.files), bytes: finite(group.bytes), reclaimable_bytes: finite(group.reclaimable_bytes) };
    }
    return { groups, total_bytes: finite(value.total_bytes), config: { cache_auto_trim: value.config.cache_auto_trim === true, cache_limit_gb: finite(value.config.cache_limit_gb, 8), protect_registered_previews: value.config.protect_registered_previews === true }, protected_notice: typeof value.protected_notice === "string" ? value.protected_notice : "原片、片目、已登记封面与备份不在清理范围。" };
  }
  var LibraryManagement = class {
    constructor(root, notify, changed) {
      this.root = root;
      this.notify = notify;
      this.changed = changed;
      this.disposed = false;
      this.provider = null;
      this.controller = new AbortController();
      this.onSubmit = (event) => {
        const form = event.target;
        if (!(form instanceof HTMLFormElement) || !form.matches("[data-provider-config],[data-analysis-batch]")) return;
        event.preventDefault();
        event.stopPropagation();
        const button = form.querySelector('button[type="submit"]');
        if (button) button.disabled = true;
        void (async () => {
          try {
            if (form.hasAttribute("data-provider-config")) {
              const body = { kind: input(form, "kind").value, base_url: input(form, "base_url").value.trim(), model: input(form, "model").value.trim(), timeout_seconds: number(form, "timeout_seconds", 10, 180), max_images: number(form, "max_images", 1, 6), local_inference_only: checked(form, "local_inference_only"), clear_api_key: checked(form, "clear_api_key") };
              const key = input(form, "api_key").value;
              if (key) body.api_key = key;
              const result = providerFromUnknown(await localPost("/api/providers", body, this.controller.signal));
              if (!this.disposed) {
                this.provider = result;
                this.render(result);
                this.changed?.();
                this.notify?.("模型配置已保存；没有连接服务或发送图片。", false);
              }
            } else {
              const tasks = ["timeline", "describe"].filter((name) => checked(form, name));
              if (!tasks.length) throw new Error("请至少选择一项本机任务。");
              if (tasks.includes("describe") && !this.provider?.automatic_allowed) throw new Error("当前模型服务不能用于自动本机描述。");
              const value = await localPost("/api/automation/batch", { tasks, scope: input(form, "scope").value, limit: number(form, "limit", 1, 128), retry_failed: checked(form, "retry_failed") }, this.controller.signal);
              if (!this.disposed) {
                const host = form.querySelector(".batch-submit-result");
                if (host) host.textContent = isRecord(value) ? `本批已提交。${typeof value.queued === "number" ? `排入 ${value.queued} 项任务。` : ""}${typeof value.skipped === "number" ? `跳过 ${value.skipped} 项。` : ""}实际进度与失败原因见最近后台任务。` : "本批已提交，请看最近后台任务。";
                this.notify?.("本机批处理已提交，远程图片没有批量发送。", false);
              }
            }
          } catch (error) {
            if (!this.disposed) this.notify?.(errorMessage(error), true);
          } finally {
            if (button?.isConnected) button.disabled = false;
          }
        })();
      };
      this.onClick = (event) => {
        if (!(event.target instanceof Element)) return;
        if (event.target.closest("[data-provider-reload]")) {
          void this.load();
          return;
        }
        const managed = event.target.closest("[data-provider-use-managed]");
        if (managed) {
          managed.disabled = true;
          void this.useManaged(managed);
          return;
        }
        const button = event.target.closest("[data-provider-probe]");
        if (!button || !this.provider) return;
        event.preventDefault();
        button.disabled = true;
        const endpoint = this.provider.base_url;
        void (async () => {
          try {
            const value = await localPost("/api/providers/probe", { endpoint }, this.controller.signal);
            if (this.disposed || !isRecord(value)) return;
            const models = Array.isArray(value.models) ? value.models.filter(isRecord).filter((model) => typeof model.id === "string") : [];
            const options = this.root.querySelector("#provider-models");
            if (options) options.innerHTML = models.map((model) => `<option value="${escape2(String(model.id))}">${model.remote ? "服务标注为远程" : "服务返回的模型"}</option>`).join("");
            const host = this.root.querySelector(".provider-probe-result");
            if (host) host.innerHTML = `<div class="notice ${value.available === true ? "success" : "error"}">${value.available === true ? this.provider?.kind === "managed_vision" ? "本机运行组件已响应。" : `服务已响应，返回 ${models.length} 个模型；可选填模型名并保存。` : escape2(typeof value.error === "string" ? value.error : "服务未响应。")}<br>本次目标：<code>${escape2(endpoint)}</code>。列模型不证明支持图片，不会自动下载或分析媒体。</div>`;
          } catch (error) {
            if (!this.disposed) this.notify?.(errorMessage(error), true);
          } finally {
            if (button.isConnected) button.disabled = false;
          }
        })();
      };
      root.addEventListener("submit", this.onSubmit);
      root.addEventListener("click", this.onClick);
      void this.load();
    }
    dispose() {
      this.disposed = true;
      this.controller.abort();
      this.root.removeEventListener("submit", this.onSubmit);
      this.root.removeEventListener("click", this.onClick);
    }
    async load() {
      try {
        const provider = providerFromUnknown(await get("/api/providers", this.controller.signal));
        if (!this.disposed) {
          this.provider = provider;
          this.render(provider);
        }
      } catch (error) {
        if (!this.disposed) this.root.innerHTML = `<div class="notice error">${escape2(errorMessage(error))}</div><button class="button ghost" data-provider-reload>重新读取模型配置</button>`;
      }
    }
    render(p) {
      const managed = p.managed;
      const selected = p.kind === "managed_vision";
      const runtime = managed?.runtime === "running" ? " · 已运行" : managed?.runtime === "starting" ? " · 正在启动" : managed?.available ? " · 按需启动" : "";
      const managedMarkup = managed ? `<div class="managed-vision-card"><div><strong>${escape2(managed.name)}</strong><p class="hint">${managed.available ? "权重和本平台运行组件已就绪" : managed.installed ? "已带权重，需准备本平台运行组件" : "本机模型权重尚未就绪"}${managed.bytes ? ` · ${bytes(managed.bytes)}` : ""}${runtime}</p>${managed.notice ? `<p class="hint">${escape2(managed.notice)}</p>` : ""}${!managed.available && managed.setup ? `<p class="hint">准备入口 <code>${escape2(managed.setup)}</code></p>` : ""}</div><div class="button-row">${selected ? `<span class="quality-badge">当前模型</span><button type="button" class="button small ghost" data-provider-probe ${managed.available ? "" : "disabled"}>检查本机运行</button>` : `<button type="button" class="button small ghost" data-provider-use-managed ${managed.available ? "" : "disabled"}>使用内置模型</button>`}</div></div>` : "";
      const manualKind = p.kind === "openai_compatible" ? "openai_compatible" : "ollama";
      const manualUrl = selected ? "http://127.0.0.1:11434" : p.base_url;
      const manualModel = selected ? "" : p.model;
      this.root.innerHTML = `<section class="panel provider-panel"><div class="panel-header"><h2>视觉概述模型服务</h2><span class="quality-badge">${p.local_only ? "已声明仅本机" : "逐次确认图片发送"}</span></div>${managedMarkup}<details class="advanced-settings" ${selected ? "" : "open"}><summary>配置其他视觉模型服务</summary><form data-provider-config><div class="form-columns"><div class="field"><label for="provider-kind">接口类型</label><select id="provider-kind" name="kind"><option value="ollama" ${manualKind === "ollama" ? "selected" : ""}>Ollama</option><option value="openai_compatible" ${manualKind === "openai_compatible" ? "selected" : ""}>OpenAI 兼容接口</option></select></div><div class="field"><label for="provider-model">模型名</label><input id="provider-model" name="model" list="provider-models" maxlength="200" value="${escape2(manualModel)}" placeholder="服务中已安装的视觉模型"><datalist id="provider-models"></datalist></div></div><div class="field"><label for="provider-base-url">服务地址</label><input id="provider-base-url" type="url" name="base_url" required value="${escape2(manualUrl)}">${help("地址与图片发送", "Ollama 填服务根地址；兼容接口可含 /v1。网络与未声明仅本机的服务，每次发送前显示确切目标和抽样图。")}</div><details class="advanced-settings"><summary>连接参数与密钥</summary><div class="form-columns"><div class="field"><label for="provider-timeout">超时（10–180 秒）</label><input id="provider-timeout" type="number" name="timeout_seconds" min="10" max="180" step="1" value="${p.timeout_seconds}"></div><div class="field"><label for="provider-max-images">每次最多抽样图（1–6）</label><input id="provider-max-images" type="number" name="max_images" min="1" max="6" step="1" value="${p.max_images}"></div></div><div class="field"><label for="provider-api-key">API 密钥（可选）</label><input id="provider-api-key" type="password" name="api_key" autocomplete="new-password" placeholder="${p.has_api_key ? "已有密钥；留空保留，不回显" : "需要时填写"}">${help("密钥保存位置", "密钥只保存到本机独立文件，不放进模型列表或资料备份；更换目标地址会清除旧密钥。")}<label class="check-row"><input type="checkbox" name="clear_api_key">明确清除已保存密钥</label></div></details><div class="field"><label class="check-row"><input type="checkbox" name="local_inference_only" ${!selected && p.local_inference_only ? "checked" : ""}>我确认这个本机服务只做本地推理，不转发云端</label>${help("本机推理条件", "localhost 也可能代理云模型。自动描述需同时满足回环地址、你的本地推理声明及非云模型。Ollama 可设 OLLAMA_NO_CLOUD=1；此声明不会更改模型服务自身设置。")}</div><div class="button-row"><button class="button primary" type="submit">保存配置（不连接服务）</button>${selected ? "" : '<button class="button ghost" type="button" data-provider-probe>连接已保存地址并列模型</button>'}</div></form></details><div class="provider-probe-result" role="status"><p class="hint">已保存目标：<code>${escape2(p.base_url)}</code>；${p.model ? "配置尚未核验视觉推理。" : "尚未选择模型。"} 列模型会连接此目标，但不发送媒体图片。</p></div></section><section class="panel automation-panel"><div class="panel-header"><h2>对已有资料执行本机分析</h2></div>${help("批处理规则", "新资料的自动规则在设置中。这里提交有限批次，已有成功或失败版本默认跳过。需要替换已有分类结果时，使用设置中的重新分类；远程描述不会批量发送。")}<form data-analysis-batch><div class="batch-task-choices"><label class="check-row"><input type="checkbox" name="timeline" checked>音画时间轴</label><label class="check-row"><input type="checkbox" name="describe" ${p.automatic_allowed ? "" : "disabled"}>仅本机视觉概述</label></div><div class="form-columns"><div class="field"><label for="batch-scope">内容分区</label><select id="batch-scope" name="scope"><option value="all">全部资料</option><option value="review">待确认</option><option value="personal">个人资料</option><option value="adult">18+ 视频</option></select></div><div class="field"><label for="batch-limit">本次最多资料（1–128）</label><input id="batch-limit" type="number" min="1" max="128" step="1" name="limit" value="32"></div></div><label class="check-row"><input type="checkbox" name="retry_failed">本次明确重试已有失败版本</label><div class="button-row"><button class="button primary" type="submit">在本机排入这一批任务</button></div><div class="batch-submit-result" role="status"></div></form></section>`;
    }
    async useManaged(button) {
      try {
        await localPost("/api/providers", { kind: "managed_vision" }, this.controller.signal);
        await this.load();
        if (!this.disposed) {
          this.changed?.();
          this.notify?.("内置模型已设为当前概述模型，设置中可开启自动概述。", false);
        }
      } catch (error) {
        if (!this.disposed) this.notify?.(errorMessage(error), true);
      } finally {
        if (button.isConnected) button.disabled = false;
      }
    }
  };
  var CacheManager = class {
    constructor(root, notify, cleaned) {
      this.root = root;
      this.notify = notify;
      this.cleaned = cleaned;
      this.disposed = false;
      this.plan = null;
      this.controller = new AbortController();
      this.onSubmit = (event) => {
        const form = event.target;
        if (!(form instanceof HTMLFormElement) || !form.hasAttribute("data-cache-settings")) return;
        event.preventDefault();
        event.stopPropagation();
        const button = form.querySelector('button[type="submit"]');
        if (button) button.disabled = true;
        void (async () => {
          try {
            await localPost("/api/settings", { cache_auto_trim: checked(form, "cache_auto_trim"), cache_limit_gb: number(form, "cache_limit_gb", 1, 512) }, this.controller.signal);
            if (!this.disposed) {
              this.notify?.("缓存策略已保存。", false);
              await this.load();
            }
          } catch (error) {
            if (!this.disposed) this.notify?.(errorMessage(error), true);
          } finally {
            if (button?.isConnected) button.disabled = false;
          }
        })();
      };
      this.onClick = (event) => {
        if (!(event.target instanceof Element)) return;
        const button = event.target.closest("[data-cache-plan],[data-cache-cleanup],[data-cache-refresh]");
        if (!button) return;
        event.preventDefault();
        button.disabled = true;
        void (async () => {
          try {
            if (button.hasAttribute("data-cache-refresh")) {
              await this.load();
              return;
            }
            if (button.dataset.cachePlan) {
              this.plan = null;
              const scope = button.dataset.cachePlan;
              const value = await localPost("/api/cache/plan", { scope }, this.controller.signal);
              if (!isRecord(value) || typeof value.plan_token !== "string") throw new Error("清理计划未返回有效凭据，未删除任何文件。");
              if (this.disposed) return;
              this.plan = { token: value.plan_token, scope, files: finite(value.files), bytes: finite(value.bytes), expires: Date.now() + finite(value.ttl_seconds) * 1e3, skipped: finite(value.skipped_active) };
              const host = this.root.querySelector(".cache-plan-confirmation");
              if (host) host.innerHTML = `<div class="notice"><strong>请核对本次计划</strong><p>范围：${escape2({ previews: "预览短片", compatible: "兼容副本", temporary: "过期临时输出", orphans: "孤立派生文件", all_regenerable: "全部可再生成缓存" }[scope] || scope)}。${this.plan.files} 个文件，${bytes(this.plan.bytes)}；跳过 ${this.plan.skipped} 项使用中内容。</p><p>只清理此计划，原片、已登记封面、片目和备份保留。计划过期后需重新计算。</p><button class="button danger" data-cache-cleanup>确认清理这 ${this.plan.files} 个缓存文件</button></div>`;
            } else {
              const plan = this.plan;
              if (!plan || Date.now() >= plan.expires) throw new Error("清理计划已失效，请重新查看计划。");
              this.plan = null;
              const value = await localPost("/api/cache/cleanup", { plan_token: plan.token, confirm: true }, this.controller.signal);
              if (!this.disposed) {
                this.cleaned?.();
                this.notify?.(isRecord(value) ? `缓存已清理 ${finite(value.deleted_files)} 个，${bytes(finite(value.deleted_bytes))}。${typeof value.notice === "string" ? value.notice : ""}` : "缓存清理完成。", false);
                await this.load();
              }
            }
          } catch (error) {
            if (!this.disposed) this.notify?.(errorMessage(error), true);
          } finally {
            if (button.isConnected) button.disabled = false;
          }
        })();
      };
      root.addEventListener("submit", this.onSubmit);
      root.addEventListener("click", this.onClick);
      void this.load();
    }
    dispose() {
      this.disposed = true;
      this.controller.abort();
      this.plan = null;
      this.root.removeEventListener("submit", this.onSubmit);
      this.root.removeEventListener("click", this.onClick);
    }
    async load() {
      try {
        const value = cacheFromUnknown(await get("/api/cache", this.controller.signal));
        if (!this.disposed) this.render(value);
      } catch (error) {
        if (!this.disposed) this.root.innerHTML = `<div class="notice error">${escape2(errorMessage(error))}</div><button class="button ghost" data-cache-refresh>重新读取缓存</button>`;
      }
    }
    render(value) {
      this.plan = null;
      const names = { previews: "静音预览短片", compatible: "兼容播放副本", temporary: "过期临时输出", orphans: "无片目关联的派生文件" };
      this.root.innerHTML = `<section class="panel"><div class="panel-header"><h2>缓存与保留策略</h2><button class="button small ghost" data-cache-refresh>刷新统计</button></div><p class="hint">当前合计 ${bytes(value.total_bytes)}。清理前先计算计划，再由你确认。重新生成需要原片仍可访问。</p><div class="cache-groups">${Object.entries(value.groups).map(([name, group]) => `<div class="cache-group"><div><strong>${escape2(names[name] || name)}</strong><span>${group.files} 个文件 · ${bytes(group.bytes)}</span><small>${name === "previews" && value.config.protect_registered_previews ? "已登记资料的短片持续保留" : `当前可回收 ${bytes(group.reclaimable_bytes)}`}</small></div>${name === "previews" && value.config.protect_registered_previews ? '<span class="quality-badge readonly">已保护</span>' : `<button class="button small ghost" data-cache-plan="${name}">查看清理计划</button>`}</div>`).join("")}</div><div class="notice">${escape2(value.protected_notice)}</div><form data-cache-settings><label class="check-row"><input type="checkbox" name="cache_auto_trim" ${value.config.cache_auto_trim ? "checked" : ""}>自动控制可再生成 MP4 缓存</label><div class="field"><label for="cache-limit">软预算（GiB，1–512）</label><input id="cache-limit" type="number" name="cache_limit_gb" min="1" max="512" step="1" value="${value.config.cache_limit_gb}">${help("缓存预算", "这是软预算，使用中和正在生成的项目会跳过。只按规则清理可再生成文件，保留原片、片目、封面、模型和备份。")}</div><button class="button primary" type="submit">保存缓存策略</button></form><div class="button-row"><button class="button ghost" data-cache-plan="all_regenerable">查看全部可再生成缓存清理计划</button></div><div class="cache-plan-confirmation" role="status"></div></section>`;
    }
  };

  // src/library-health.ts
  var escape3 = (value) => value.replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char] || char);
  var string = (value, fallback = "") => typeof value === "string" ? value : fallback;
  var nonnegative = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : 0;
  function numberField(value, name) {
    const result = value[name];
    if (typeof result !== "number" || !Number.isFinite(result) || result < 0) throw new Error("统计数据尚未完整返回，请稍后重试。");
    return result;
  }
  function counts(value, names) {
    if (!isRecord(value)) throw new Error("统计数据格式不完整，不能据此判断没有问题。");
    return Object.fromEntries(names.map((name) => [name, numberField(value, name)]));
  }
  var previewKeys = ["total", "ready", "queued", "processing", "pending", "failed", "unprepared", "blocked", "stale"];
  var mediaKeys = ["total", "videos", "images", "ready", "analyzing", "failed", "missing", "changed", "needs_attention"];
  function scopeFromUnknown(value) {
    if (!isRecord(value) || !(value.source_id === null || typeof value.source_id === "string") || typeof value.folder !== "string" || typeof value.recursive !== "boolean") throw new Error("来源范围无法识别。");
    return { source_id: value.source_id, folder: value.folder, recursive: value.recursive };
  }
  function nullableSeconds(value) {
    return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
  }
  function healthFromUnknown(value) {
    if (!isRecord(value) || !isRecord(value.eta) || !isRecord(value.shared_queue)) throw new Error("进度尚未完整返回。");
    const media = counts(value.media, mediaKeys), preview = counts(value.preview, previewKeys);
    if (preview.ready + preview.queued + preview.processing + preview.pending + preview.failed + preview.unprepared + preview.blocked !== preview.total) throw new Error("进度数量暂不一致，请刷新后查看。");
    return {
      scope: scopeFromUnknown(value.scope),
      media,
      preview,
      revision: nonnegative(value.revision),
      eta: { seconds: nullableSeconds(value.eta.seconds), lower_seconds: nullableSeconds(value.eta.lower_seconds), upper_seconds: nullableSeconds(value.eta.upper_seconds), sample_count: nonnegative(value.eta.sample_count), reason: string(value.eta.reason) },
      current: isRecord(value.current) ? { kind: string(value.current.kind), title: string(value.current.title), elapsed_seconds: nonnegative(value.current.elapsed_seconds), in_scope: value.current.in_scope === true } : null,
      shared_queue: { queued: numberField(value.shared_queue, "queued"), running: numberField(value.shared_queue, "running"), other_jobs: numberField(value.shared_queue, "other_jobs") }
    };
  }
  function pageFromUnknown(value, parse) {
    if (!isRecord(value) || !Array.isArray(value.items) || value.items.some((entry) => !isRecord(entry))) throw new Error("清单尚未完整返回。");
    return { items: value.items.filter(isRecord).map(parse), total: numberField(value, "total"), offset: numberField(value, "offset"), limit: numberField(value, "limit") };
  }
  function sourceFromUnknown(entry) {
    const scan = isRecord(entry.scan) ? entry.scan : {};
    return {
      id: string(entry.id),
      label: string(entry.label, "未命名来源"),
      path: string(entry.path),
      source_status: string(entry.source_status, "unscanned"),
      error: string(entry.error),
      media: counts(entry.media, mediaKeys),
      preview: counts(entry.preview, previewKeys),
      unsupported_files: nonnegative(scan.unsupported_files),
      unsupported: Array.isArray(scan.unsupported_extensions) ? scan.unsupported_extensions.filter(isRecord).map((item) => ({ extension: string(item.extension), count: nonnegative(item.count) })) : []
    };
  }
  function folderFromUnknown(entry) {
    return { folder: string(entry.folder), label: string(entry.label), media: counts(entry.media, mediaKeys), preview: counts(entry.preview, previewKeys) };
  }
  function issueFromUnknown(entry) {
    return {
      id: string(entry.id),
      title: string(entry.title, "未命名资料"),
      filename: string(entry.filename),
      relative_folder: string(entry.relative_folder),
      status: string(entry.status),
      preview_status: string(entry.preview_status),
      codec: string(entry.codec),
      container: string(entry.container),
      issue_codes: Array.isArray(entry.issue_codes) ? entry.issue_codes.filter((item) => typeof item === "string") : [],
      diagnostic: isRecord(entry.diagnostic) ? entry.diagnostic : {},
      actions: isRecord(entry.actions) ? entry.actions : {}
    };
  }
  var kindNames = { analyze: "读取资料与抽帧", preview: "生成静音短片", timeline: "分析时间轴", classify: "本机分类", describe: "模型概述", transcode: "准备兼容播放", verify: "校验原片", cover: "生成封面", scan: "扫描来源" };
  var issueNames = { analysis_failed: "基础分析失败", analysis_error: "基础分析失败", analysis_queued: "等待基础分析", analysis_analyzing: "正在基础分析", analysis_missing: "原片无法访问", analysis_changed: "原片发生变化", no_cover: "封面未生成", preview_failed: "预览生成失败", preview_unprepared: "预览尚未准备", preview_stale: "旧预览等待更新", preview_queued: "预览排队中", preview_processing: "预览正在生成", preview_pending: "预览等待入队", preview_blocked: "原片暂不可用于预览", missing: "原片无法访问", source_offline: "来源离线", source_offline_or_changed: "来源离线或设备身份变化", changed: "原片发生变化", playback_failed: "兼容播放准备失败", playback_unverified: "浏览器播放尚未验证", playback_conversion_needed: "需要兼容播放副本" };
  var elapsed = (seconds) => seconds >= 3600 ? `${(seconds / 3600).toFixed(1)} 小时` : seconds >= 60 ? `${Math.ceil(seconds / 60)} 分钟` : `${Math.ceil(seconds)} 秒`;
  function etaLabel(eta) {
    if (eta.seconds === null) return eta.reason || "尚无可靠耗时样本，暂不能估时。";
    const range = eta.lower_seconds !== null && eta.upper_seconds !== null && eta.lower_seconds !== eta.upper_seconds ? `${elapsed(eta.lower_seconds)}–${elapsed(eta.upper_seconds)}` : `约 ${elapsed(eta.seconds)}`;
    return `估计剩余 ${range}（${eta.sample_count} 个实际完成样本）。${eta.reason}`;
  }
  function healthProgressMarkup(summary, compact = false) {
    const p = summary.preview, total = p.total, percent = total ? Math.min(100, p.ready / total * 100) : 0, failedPercent = total ? Math.min(100 - percent, p.failed / total * 100) : 0;
    const remaining = p.queued + p.processing + p.pending;
    if (compact) {
      const counts2 = [[p.processing, "生成中"], [p.queued + p.pending, "等待"], [p.unprepared, "待准备"], [p.failed, "预览失败"], [p.blocked, "原片待处理"]];
      const notes = counts2.filter(([count]) => count > 0).map(([count, label]) => `<span>${label} ${count}</span>`).join("") || `<span>${total ? "全部已准备" : "尚无视频"}</span>`;
      return `<div class="health-progress-heading"><div><strong>静音预览</strong><span>${p.ready} / ${total} 个视频已就绪</span></div><span class="health-progress-percent">${Math.round(percent)}%</span></div><div class="health-progress-track" role="progressbar" aria-label="预览已就绪比例" aria-valuemin="0" aria-valuemax="${total || 1}" aria-valuenow="${p.ready}" aria-valuetext="${p.ready} / ${total} 个视频已就绪"><i style="width:${percent}%"></i><b style="width:${failedPercent}%"></b></div><div class="health-progress-brief">${notes || "<span>全部已准备</span>"}<details class="inline-help"><summary>进度说明</summary><div class="inline-help-copy"><p>${escape3(etaLabel(summary.eta))}</p><p>就绪 ${p.ready}，生成 ${p.processing}，排队 ${p.queued}，等待入队 ${p.pending}，待准备 ${p.unprepared}，失败 ${p.failed}，原片或基础分析待处理 ${p.blocked}。${p.stale ? `旧缓存待更新 ${p.stale}。` : ""}</p>${summary.current ? `<p>当前：${escape3(kindNames[summary.current.kind] || "后台任务")} · 已运行 ${elapsed(summary.current.elapsed_seconds)}。</p>` : ""}<p>按最近扫描与任务记录统计；离线、变化、分析失败和完整播放验证可在来源中查看。</p></div></details></div>`;
    }
    return `<div class="health-progress-heading"><div><strong>${compact ? "全资料库静音预览进度" : "当前范围的预览准备"}</strong><span>${p.ready} / ${total} 个视频已就绪</span></div><span class="health-progress-percent">${Math.round(percent)}%</span></div><div class="health-progress-track" role="progressbar" aria-label="预览已就绪比例" aria-valuemin="0" aria-valuemax="${total || 1}" aria-valuenow="${p.ready}" aria-valuetext="${p.ready} / ${total} 个视频已就绪"><i style="width:${percent}%"></i><b style="width:${failedPercent}%"></b></div><div class="health-progress-counts"><span class="good">已就绪 <b>${p.ready}</b></span><span>生成中 <b>${p.processing}</b></span><span>排队 <b>${p.queued}</b></span><span>等待入队 <b>${p.pending}</b></span><span>尚未准备 <b>${p.unprepared}</b>${p.stale ? `（旧缓存 ${p.stale}）` : ""}</span><span class="bad">失败 <b>${p.failed}</b></span><span class="warn">基础 / 原片未就绪 <b>${p.blocked}</b></span></div><p class="health-eta">${escape3(etaLabel(summary.eta))}${remaining ? "" : " 当前没有正在处理的预览任务。"}${p.unprepared ? " 未准备的项目需要提交准备，或等进入视口时按需生成。" : ""}${p.blocked ? " 基础 / 原片未就绪包含基础分析中、分析失败或离线/变更，具体原因见下方状态。" : ""}</p>${summary.current ? `<p class="health-current">当前${summary.current.in_scope ? "" : "其他范围"}：${escape3(kindNames[summary.current.kind] || "后台任务")}${summary.current.title ? ` · ${escape3(summary.current.title)}` : ""} · 已运行 ${elapsed(summary.current.elapsed_seconds)}。${summary.current.kind !== "preview" ? "共享队列正处理其他工作，预览仍需等待。" : ""}</p>` : ""}${!compact ? `<div class="health-media-counts">已登记 ${summary.media.total} 项（视频 ${summary.media.videos} / 图片 ${summary.media.images}） · 基础资料就绪 ${summary.media.ready} · 分析中 ${summary.media.analyzing} · 分析失败 ${summary.media.failed} · 缺失/离线 ${summary.media.missing} · 内容变化 ${summary.media.changed}</div>` : ""}`;
  }
  var LibraryHealthManager = class {
    constructor(root, options) {
      this.root = root;
      this.options = options;
      this.scope = { source_id: null, folder: "", recursive: true };
      this.displayedScope = null;
      this.scopeLoading = false;
      this.disposed = false;
      this.requests = /* @__PURE__ */ new Set();
      this.refreshing = false;
      this.lastRevision = null;
      this.sequence = 0;
      this.sourceOffset = 0;
      this.folderOffset = 0;
      this.issueOffset = 0;
      this.issueKind = "all";
      this.sourcePage = null;
      this.selectedSource = null;
      this.folderPage = null;
      this.issuePage = null;
      this.html = /* @__PURE__ */ new Map();
      this.onSubmit = (event) => {
        const form = event.target;
        if (!(form instanceof HTMLFormElement) || !form.hasAttribute("data-health-scope")) return;
        event.preventDefault();
        event.stopPropagation();
        const source = form.elements.namedItem("source_id"), folder = form.elements.namedItem("folder"), recursive = form.elements.namedItem("recursive");
        if (!(source instanceof HTMLSelectElement) || !(folder instanceof HTMLInputElement) || !(recursive instanceof HTMLInputElement)) return;
        this.changeScope({ source_id: source.value || null, folder: folder.value.trim().replace(/\\/g, "/"), recursive: recursive.checked });
      };
      this.onChange = (event) => {
        const target = event.target;
        if (!(target instanceof HTMLSelectElement) || !target.hasAttribute("data-health-issue-kind")) return;
        this.issueKind = target.value;
        this.issueOffset = 0;
        this.lastRevision = null;
        this.sequence++;
        void this.refresh(true);
      };
      this.onClick = (event) => {
        if (!(event.target instanceof Element)) return;
        const button = event.target.closest("[data-health-capabilities],[data-health-refresh],[data-health-navigate],[data-health-source],[data-health-folder],[data-health-root],[data-health-prepare],[data-health-retry],[data-health-page],[data-health-open],[data-health-action]");
        if (!button) return;
        event.preventDefault();
        event.stopPropagation();
        if (button.hasAttribute("data-health-capabilities")) {
          void this.loadCapabilities(button);
          return;
        }
        if (button.hasAttribute("data-health-navigate")) {
          this.options.onNavigateSources?.();
          return;
        }
        if (button.hasAttribute("data-health-open")) {
          this.options.onOpenVideo?.(button.dataset.healthOpen || "");
          return;
        }
        if (button.hasAttribute("data-health-source")) {
          this.changeScope({ source_id: button.dataset.healthSource || null, folder: "", recursive: true });
          return;
        }
        if (button.hasAttribute("data-health-folder")) {
          this.changeScope({ ...this.scope, folder: button.dataset.healthFolder || "" });
          return;
        }
        if (button.hasAttribute("data-health-root")) {
          this.changeScope({ ...this.scope, folder: "" });
          return;
        }
        if (button.dataset.healthPage) {
          const [kind, direction] = button.dataset.healthPage.split(":"), amount = direction === "prev" ? -1 : 1;
          if (kind === "sources") this.sourceOffset = Math.max(0, this.sourceOffset + amount * 12);
          else if (kind === "folders") this.folderOffset = Math.max(0, this.folderOffset + amount * 12);
          else this.issueOffset = Math.max(0, this.issueOffset + amount * 20);
          this.lastRevision = null;
          this.sequence++;
          void this.refresh(true);
          return;
        }
        if (button.hasAttribute("data-health-refresh")) {
          void this.refresh(true);
          return;
        }
        button.disabled = true;
        void (async () => {
          try {
            if (button.hasAttribute("data-health-prepare") || button.hasAttribute("data-health-retry")) {
              const result = await localPost("/api/library-health/previews", { ...this.scope, retry_failed: button.hasAttribute("data-health-retry") });
              if (!isRecord(result)) throw new Error("准备批次未返回有效结果。");
              const message = `选中 ${nonnegative(result.selected)} 个视频；排入 ${nonnegative(result.queued)}，等待入队 ${nonnegative(result.pending)}，跳过 ${nonnegative(result.skipped)}。${string(result.notice)}`;
              if (!this.disposed) {
                const host = this.root.querySelector("[data-health-result]");
                if (host) host.textContent = message;
                this.options.onMessage?.(message, false);
              }
            } else {
              const id = button.dataset.healthId, action = button.dataset.healthAction;
              if (!id || !action) return;
              const endpoint = { playback: "prepare-playback", "preview-retry": "preview", preview: "preview", analyze: "analyze", verify: "verify" }[action];
              if (!endpoint) throw new Error("未知的资料操作，未提交任务。");
              const value = await localPost(`/api/videos/${encodeURIComponent(id)}/${endpoint}`, action === "preview-retry" ? { retry: true } : {});
              if (isRecord(value) && value.job_id === null && value.status === void 0) throw new Error("同项任务可能已排队或队列已满，请查看当前进度后再试。");
              if (isRecord(value) && value.status === "error") throw new Error(string(value.error, "准备失败，请查看本条记录的原因。"));
              if (!this.disposed) this.options.onMessage?.(isRecord(value) && value.status === "ready" ? "播放地址已经就绪，可打开资料播放。" : "已提交本机任务；进度和错误会更新在此清单。", false);
            }
            this.lastRevision = null;
            await this.refresh(true);
          } catch (error) {
            if (!this.disposed) this.options.onMessage?.(errorMessage(error), true);
          } finally {
            if (button.isConnected) button.disabled = false;
          }
        })();
      };
      root.addEventListener("click", this.onClick);
      root.addEventListener("submit", this.onSubmit);
      root.addEventListener("change", this.onChange);
      this.renderShell();
      void this.refresh(true);
    }
    dispose() {
      this.disposed = true;
      for (const request of this.requests) request.abort();
      this.requests.clear();
      this.root.removeEventListener("click", this.onClick);
      this.root.removeEventListener("submit", this.onSubmit);
      this.root.removeEventListener("change", this.onChange);
    }
    setHTML(selector, value) {
      if (this.html.get(selector) === value) return;
      const node = this.root.querySelector(selector);
      if (node) {
        node.innerHTML = value;
        this.html.set(selector, value);
      }
    }
    renderShell() {
      if (this.options.mode === "compact") {
        this.root.innerHTML = '<section class="preview-progress-compact panel"><div data-health-summary><p class="hint">正在读取预览进度…</p></div><button class="button small ghost" data-health-navigate title="按硬盘或文件夹查看预览与识别情况">查看状态</button></section>';
        return;
      }
      this.root.innerHTML = `<section class="panel health-dashboard"><div class="panel-header"><div><h2>来源与预览状态</h2><p class="hint">来自已登记片目和最近扫描记录，不会因打开此页重新读取全盘。</p></div><div class="button-row"><button class="button small ghost" data-health-capabilities>查看本机解码能力</button><button class="button small ghost" data-health-refresh>刷新状态</button></div></div><div data-health-capabilities-result></div><div data-health-sources>正在读取各来源…</div><form data-health-scope class="health-scope-form"><div class="field"><label for="health-source">来源范围</label><select id="health-source" name="source_id"><option value="">全部来源（含库内）</option></select></div><div class="field"><label for="health-folder">相对文件夹</label><input id="health-folder" name="folder" placeholder="留空为来源根目录，例如 动画/旧片"></div><label class="check-row"><input type="checkbox" name="recursive" checked>包含下级文件夹</label><button class="button primary" type="submit">查看此范围</button><button class="button ghost" type="button" data-health-root>返回来源根目录</button></form><div class="health-scope-caption" data-health-caption>全部已登记来源</div><div data-health-summary>正在读取预览进度…</div><div class="button-row health-batch-actions"><button class="button primary" data-health-prepare>准备此范围缺少的预览</button><button class="button ghost" data-health-retry>明确重试失败的预览</button></div><p class="hint">只生成可再生静音短片，不改动或移动原片。任务较多时先保存待处理记录，再分批进入共享队列。</p><div data-health-result role="status"></div></section><section class="panel"><div class="panel-header"><h2>文件夹明细</h2><span class="hint">按完整相对路径列出；点击选择，可包含下级</span></div><div data-health-folders>正在读取文件夹…</div></section><section class="panel"><div class="panel-header"><h2>识别与播放情况</h2><label class="health-issue-kind">显示范围<select aria-label="识别情况筛选" data-health-issue-kind><option value="all">实际异常</option><option value="analysis">基础分析异常</option><option value="preview">所有预览准备状态</option><option value="playback">播放兼容与未验证</option><option value="source">来源 / 原片不可用</option></select></label></div><p class="hint">“尚未准备”“浏览器未验证”不等于失败。读取参数成功，也不保证浏览器可以直接播放；每条资料的依据和可用操作分别显示。</p><div data-health-issues>正在读取清单…</div></section>`;
    }
    query(extra = {}) {
      const params = new URLSearchParams({ folder: this.scope.folder, recursive: this.scope.recursive ? "1" : "0", ...extra });
      if (this.scope.source_id) params.set("source_id", this.scope.source_id);
      return params.toString();
    }
    async get(path) {
      const controller = new AbortController();
      this.requests.add(controller);
      try {
        const response = await fetch(path, { credentials: "same-origin", signal: controller.signal });
        const value = await response.json();
        if (!response.ok) throw new Error(isRecord(value) && typeof value.error === "string" ? value.error : "来源统计暂时无法读取。");
        return value;
      } finally {
        this.requests.delete(controller);
      }
    }
    async loadCapabilities(button) {
      button.disabled = true;
      this.setHTML("[data-health-capabilities-result]", '<div class="notice">正在查询本机媒体引擎的版本与编译能力；不会读取原片。</div>');
      try {
        const value = await this.get("/api/media-capabilities");
        if (!isRecord(value) || !Array.isArray(value.video_decoders) || !Array.isArray(value.audio_decoders) || !Array.isArray(value.demuxers)) throw new Error("媒体引擎未返回完整能力清单。");
        const lists = [
          { name: "视频解码器", values: value.video_decoders },
          { name: "音频解码器", values: value.audio_decoders },
          { name: "容器读取格式", values: value.demuxers }
        ].map((list) => ({ ...list, values: list.values.filter((entry) => typeof entry === "string") }));
        const errors = Array.isArray(value.errors) ? value.errors.filter((entry) => typeof entry === "string") : [];
        const version = string(value.ffmpeg_version, value.ffmpeg_available === true ? "FFmpeg 未返回版本" : "当前未找到 FFmpeg");
        if (!this.disposed) this.setHTML("[data-health-capabilities-result]", `<section class="notice health-capabilities"><strong>当前本机媒体引擎</strong><p><code>${escape3(version)}</code></p><p>${lists.map((list) => `${list.name} ${list.values.length} 项`).join(" · ")}${value.cached === true ? " · 使用一小时内缓存" : ""}</p><p>${escape3(string(value.notice, "编译能力清单不代表某个文件已完整解码，或浏览器一定可以播放。"))}</p>${errors.length ? `<p class="health-source-error">${errors.map(escape3).join("<br>")}</p>` : ""}${lists.map((list) => `<details><summary>${list.name}名单（${list.values.length}）</summary><p><code>${list.values.length ? list.values.map(escape3).join(" · ") : "本次没有返回名单；请检查工具状态与错误。"}</code></p></details>`).join("")}</section>`);
      } catch (error) {
        if (!this.disposed) this.setHTML("[data-health-capabilities-result]", `<div class="notice error">无法查询本机解码能力：${escape3(errorMessage(error))}。可在运行映库的电脑本机重试；未据此判断原片不受支持。</div>`);
      } finally {
        if (button.isConnected) button.disabled = false;
      }
    }
    async refresh(force = false) {
      if (this.disposed || this.refreshing) return;
      this.refreshing = true;
      const sequence = this.sequence;
      try {
        const summary = healthFromUnknown(await this.get("/api/library-health?" + this.query()));
        if (this.disposed || sequence !== this.sequence) return;
        this.displayedScope = summary.scope;
        this.scopeLoading = false;
        this.setHTML("[data-health-summary]", healthProgressMarkup(summary, this.options.mode === "compact"));
        this.updateScopeCaption();
        if (this.options.mode === "sources" && (force || summary.revision !== this.lastRevision)) await this.loadLists(sequence);
        if (sequence === this.sequence) this.lastRevision = summary.revision;
      } catch (error) {
        if (!this.disposed && sequence === this.sequence) {
          this.scopeLoading = false;
          this.updateScopeCaption();
          this.setHTML("[data-health-summary]", `<div class="notice error">${escape3(errorMessage(error))} 统计失败不代表零异常。<button class="button small ghost" data-health-refresh>重试读取</button></div>`);
        }
      } finally {
        this.refreshing = false;
        if (!this.disposed && sequence !== this.sequence) void this.refresh(true);
      }
    }
    async loadLists(sequence) {
      const values = await Promise.allSettled([
        this.get("/api/library-health/sources?" + new URLSearchParams({ limit: "12", offset: String(this.sourceOffset) }).toString()),
        this.get("/api/library-health/folders?" + this.query({ limit: "12", offset: String(this.folderOffset) })),
        this.get("/api/library-health/issues?" + this.query({ kind: this.issueKind, limit: "20", offset: String(this.issueOffset) }))
      ]);
      if (this.disposed || sequence !== this.sequence) return;
      const [sources, folders, issues] = values;
      try {
        if (sources?.status !== "fulfilled") throw sources?.status === "rejected" ? sources.reason : new Error("来源未返回");
        this.sourcePage = pageFromUnknown(sources.value, sourceFromUnknown);
        this.renderSources();
      } catch (error) {
        this.setHTML("[data-health-sources]", `<div class="notice error">来源清单读取失败：${escape3(errorMessage(error))}</div>`);
      }
      try {
        if (folders?.status !== "fulfilled") throw folders?.status === "rejected" ? folders.reason : new Error("文件夹未返回");
        this.folderPage = pageFromUnknown(folders.value, folderFromUnknown);
        this.renderFolders();
      } catch (error) {
        this.setHTML("[data-health-folders]", `<div class="notice error">文件夹统计读取失败：${escape3(errorMessage(error))}</div>`);
      }
      try {
        if (issues?.status !== "fulfilled") throw issues?.status === "rejected" ? issues.reason : new Error("异常清单未返回");
        this.issuePage = pageFromUnknown(issues.value, issueFromUnknown);
        this.renderIssues();
      } catch (error) {
        this.setHTML("[data-health-issues]", `<div class="notice error">识别清单读取失败：${escape3(errorMessage(error))}，尚不能判断没有异常。</div>`);
      }
      this.updateScopeCaption();
    }
    pager(kind, page) {
      const from = page.total ? page.offset + 1 : 0;
      return `<div class="health-pager"><span>${from}–${Math.min(page.total, page.offset + page.items.length)} / ${page.total}</span><div><button class="button small ghost" data-health-page="${kind}:prev" ${page.offset <= 0 ? "disabled" : ""}>上一页</button><button class="button small ghost" data-health-page="${kind}:next" ${page.offset + page.items.length >= page.total ? "disabled" : ""}>下一页</button></div></div>`;
    }
    renderSources() {
      const page = this.sourcePage;
      if (!page) return;
      const statuses = { available: "可访问", offline: "来源离线", attention: "扫描需关注", unscanned: "尚未扫描" };
      this.setHTML("[data-health-sources]", `<div class="source-health-grid">${page.items.map((source) => `<article class="source-health-card ${source.id === this.scope.source_id ? "selected" : ""}"><div><h3>${escape3(source.label)}</h3><span class="health-source-state">${escape3(statuses[source.source_status] || source.source_status)}</span></div><code>${escape3(source.path || (source.id === "owned" ? "data/media（库内副本）" : "路径未返回"))}</code><p>${source.media.videos} 视频 / ${source.media.images} 图片 · 基础资料就绪 ${source.media.ready} · 分析失败 ${source.media.failed}</p><p>短片就绪 ${source.preview.ready}/${source.preview.total} · 排队/生成 ${source.preview.queued + source.preview.processing + source.preview.pending} · 失败 ${source.preview.failed}</p>${source.error ? `<p class="health-source-error">${escape3(source.error)}</p>` : ""}${source.unsupported_files || source.unsupported.length ? `<p class="health-source-unsupported">未收录扩展名文件 ${source.unsupported_files} 项${source.unsupported.length ? "：" + source.unsupported.slice(0, 8).map((item) => `${escape3(item.extension || "无扩展名")} × ${item.count}`).join("，") : ""}。这些不一定是视频；与已登记分析失败分别统计。</p>` : ""}<button class="button small ghost" data-health-source="${escape3(source.id)}">查看此来源</button></article>`).join("")}</div>${this.pager("sources", page)}`);
      const select = this.root.querySelector("#health-source");
      if (select) {
        const choices = this.selectedSource && !page.items.some((item) => item.id === this.selectedSource?.id) ? [this.selectedSource, ...page.items] : page.items;
        const options = '<option value="">全部来源（含库内）</option>' + choices.map((item) => `<option value="${escape3(item.id)}" ${item.id === this.scope.source_id ? "selected" : ""}>${escape3(item.label)}</option>`).join("");
        if (this.html.get("source-options") !== options) {
          select.innerHTML = options;
          this.html.set("source-options", options);
        }
        select.value = this.scope.source_id || "";
      }
    }
    renderFolders() {
      const page = this.folderPage;
      if (!page) return;
      this.setHTML("[data-health-folders]", page.items.length ? `<div class="health-table-scroll"><table class="folder-health-table"><thead><tr><th>相对文件夹</th><th>视频 / 图片</th><th>基础资料失败 / 原片不可用</th><th>预览就绪 / 总视频</th><th>操作</th></tr></thead><tbody>${page.items.map((item) => `<tr><td><code>${escape3(item.folder || item.label || "根目录")}</code></td><td>${item.media.videos} / ${item.media.images}</td><td>${item.media.failed} / ${item.media.missing + item.media.changed}</td><td>${item.preview.ready} / ${item.preview.total}${item.preview.failed ? ` · 失败 ${item.preview.failed}` : ""}</td><td><button class="button small ghost" data-health-folder="${escape3(item.folder)}">选择此文件夹</button></td></tr>`).join("")}</tbody></table></div>${this.pager("folders", page)}` : '<p class="hint">此范围暂没有已登记文件夹。尚未登记或被排除的文件请另外核对来源扫描结果。</p>');
    }
    renderIssues() {
      const page = this.issuePage;
      if (!page) return;
      this.setHTML("[data-health-issues]", page.items.length ? `<div class="health-issue-list">${page.items.map((item) => {
        const summary = string(item.diagnostic.summary) || string(item.diagnostic.title) || string(item.diagnostic.message) || string(item.diagnostic.reason);
        const explanation = string(item.diagnostic.explanation);
        const recommendations = [item.diagnostic.recommendation, item.diagnostic.recommendations, item.diagnostic.next_steps, item.diagnostic.suggestions].flatMap((value) => Array.isArray(value) ? value.filter((entry) => typeof entry === "string") : typeof value === "string" ? [value] : []);
        const evidence = Array.isArray(item.diagnostic.evidence) ? item.diagnostic.evidence.filter(isRecord).filter((entry) => typeof entry.message === "string").slice(0, 4) : [];
        const actions = [["retry_analysis", "analyze", "重试基础分析"], ["prepare_preview", "preview", "准备预览"], ["retry_preview", "preview-retry", "重试失败预览"], ["prepare_playback", "playback", "准备兼容播放"], ["verify", "verify", "核对原片"]];
        return `<article class="health-issue"><div class="health-issue-title"><button class="health-title-button" data-health-open="${escape3(item.id)}">${escape3(item.title || item.filename)}</button><span>${escape3(item.codec ? item.codec.toUpperCase() : "编码未识别")}${item.container ? " / " + escape3(item.container) : ""}</span></div><code>${escape3((item.relative_folder ? item.relative_folder + "/" : "") + item.filename)}</code><div class="health-issue-codes">${item.issue_codes.map((code) => `<span>${escape3(issueNames[code] || code)}</span>`).join("")}</div>${summary ? `<p><strong>${escape3(summary)}</strong></p>` : "<p>当前记录需要核对；打开资料查看最近错误，未据此断定必须安装插件。</p>"}${explanation ? `<p>${escape3(explanation)}</p>` : ""}${recommendations.length ? `<ul>${recommendations.map((text) => `<li>${escape3(text)}</li>`).join("")}</ul>` : ""}${evidence.length ? `<details class="health-evidence"><summary>查看检测依据与错误</summary>${evidence.map((entry) => `<p><strong>${escape3(string(entry.stage, "检测"))}</strong><br><code>${escape3(string(entry.message).slice(0, 1400))}</code></p>`).join("")}</details>` : ""}<div class="button-row">${actions.filter(([key]) => !!key && item.actions[key] === true && !(key === "prepare_preview" && item.actions.retry_preview === true)).map(([, action, label]) => `<button class="button small ghost" data-health-action="${action}" data-health-id="${escape3(item.id)}">${label}</button>`).join("")}</div></article>`;
      }).join("")}</div>${this.pager("issues", page)}` : `<p class="hint">当前筛选没有返回${this.issueKind === "all" ? "已记录的实际异常" : "匹配记录"}。这只表示此范围的片目状态；没有对所有文件执行新的完整解码或浏览器实播检查。</p>`);
    }
    updateScopeCaption() {
      const scope = this.displayedScope || this.scope;
      const source = this.sourcePage?.items.find((item) => item.id === scope.source_id) || (this.selectedSource?.id === scope.source_id ? this.selectedSource : null);
      const caption = `${scope.source_id ? source?.label || "所选来源" : "全部来源"} / ${scope.folder || "根目录"}${scope.recursive ? "（含所有下级）" : "（仅此层）"}${this.scopeLoading ? " · 正在读取新范围" : ""}`;
      const node = this.root.querySelector("[data-health-caption]");
      if (node) node.textContent = caption;
    }
    changeScope(scope) {
      this.selectedSource = this.sourcePage?.items.find((item) => item.id === scope.source_id) || (this.selectedSource?.id === scope.source_id ? this.selectedSource : null);
      this.scope = scope;
      this.displayedScope = null;
      this.scopeLoading = true;
      this.folderOffset = this.issueOffset = 0;
      this.lastRevision = null;
      this.sequence++;
      const folder = this.root.querySelector("#health-folder");
      if (folder) folder.value = scope.folder;
      const recursive = this.root.querySelector('[name="recursive"]');
      if (recursive) recursive.checked = scope.recursive;
      this.setHTML("[data-health-summary]", '<p class="hint" role="status">正在读取新范围的预览与分析状态…</p>');
      this.setHTML("[data-health-folders]", '<p class="hint">正在读取新范围的文件夹…</p>');
      this.setHTML("[data-health-issues]", '<p class="hint">正在读取新范围的识别情况…</p>');
      this.renderSources();
      this.updateScopeCaption();
      void this.refresh(true);
    }
  };

  // src/main.ts
  var player = null;
  var previews = new CardPreviewManager();
  var analysis = null;
  var management = null;
  var cache = null;
  var health = null;
  var bridge = {
    mountPlayer(options) {
      player?.dispose();
      player = new CinemaPlayer(options);
    },
    syncPlayer(item) {
      player?.update(item);
    },
    unmountPlayer() {
      player?.dispose();
      player = null;
    },
    syncCards(root, items, options) {
      previews.mount(root, items, options);
    },
    stopCards() {
      previews.stop();
    },
    suspendCards() {
      previews.suspend();
    },
    mountAnalysis(root, onMessage) {
      analysis?.dispose();
      analysis = new AnalysisManager(root, onMessage);
    },
    refreshAnalysis() {
      analysis?.refresh();
    },
    unmountAnalysis() {
      analysis?.dispose();
      analysis = null;
    },
    mountManagement(root, onMessage) {
      management?.dispose();
      management = new LibraryManagement(root, onMessage, () => analysis?.refresh());
    },
    unmountManagement() {
      management?.dispose();
      management = null;
    },
    mountCache(root, onMessage) {
      cache?.dispose();
      cache = new CacheManager(root, onMessage, () => previews.resetPreparation());
    },
    unmountCache() {
      cache?.dispose();
      cache = null;
    },
    mountHealth(root, options) {
      health?.dispose();
      health = new LibraryHealthManager(root, options);
    },
    refreshHealth() {
      void health?.refresh();
    },
    unmountHealth() {
      health?.dispose();
      health = null;
    }
  };
  window.CineVaultExperience = bridge;
})();
