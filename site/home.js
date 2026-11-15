/**
 * [INPUT]: 首页样片卡片、视口范围与用户播放偏好。
 * [OUTPUT]: 控制静音预览与暂停按钮。
 * [POS]: 产品页演示动态封面。静态海报可独立显示。
 * [PROTOCOL]: Update this header on change, then check AGENTS.md
 */
import { isEnglish } from './language.js';

const clips = [...document.querySelectorAll('[data-preview]')];
const toggle = document.getElementById('motion-toggle');
const motion = window.matchMedia?.('(prefers-reduced-motion: reduce)');
const visible = new Set();
let manuallyPaused = false;

function renderToggle() {
  const label = manuallyPaused
    ? (isEnglish() ? 'Resume previews' : '恢复预览')
    : (isEnglish() ? 'Pause previews' : '暂停预览');
  toggle.textContent = label;
}

function update() {
  const allowed = !motion?.matches && !navigator.connection?.saveData;
  toggle.hidden = !allowed;
  for (const video of clips) {
    if (!allowed || manuallyPaused || document.hidden || !visible.has(video)) {
      video.pause();
      continue;
    }
    if (!video.hasAttribute('src')) video.src = video.dataset.preview;
    if (video.paused) video.play()?.catch(() => {});
  }
}

if ('IntersectionObserver' in window) {
  const observer = new IntersectionObserver(entries => {
    for (const entry of entries) {
      if (entry.intersectionRatio >= .2) visible.add(entry.target);
      else visible.delete(entry.target);
    }
    update();
  }, { threshold: .2 });
  for (const video of clips) observer.observe(video);
  toggle.addEventListener('click', () => {
    manuallyPaused = !manuallyPaused;
    renderToggle();
    update();
  });
  document.addEventListener('cinevault-language', renderToggle);
  document.addEventListener('visibilitychange', update);
  if (motion?.addEventListener) motion.addEventListener('change', update);
  else motion?.addListener?.(update);
  renderToggle();
}
