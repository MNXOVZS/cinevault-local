/**
 * [INPUT]: 固定样片清单、预览媒体、缩略图条和语言状态。
 * [OUTPUT]: 提供筛选、静音预览、弹窗或原生播放及时间轴跳转。
 * [POS]: 公开演示的唯一交互模块。只读取站内的样片媒体。
 * [PROTOCOL]: Update this header on change, then check AGENTS.md
 */
import { isEnglish } from '../language.js';

const ASSETS = new URL('../assets/', import.meta.url);
const SAMPLES = [
  { id: 'valley', zh: '山谷列车', en: 'Valley Train', category: 'landscape', tags: ['列车', '山谷', '旅途'], tagsEn: ['Train', 'Valley', 'Travel'] },
  { id: 'bookshop', zh: '雨巷书店', en: 'Bookshop in the Rain', category: 'landscape', tags: ['雨景', '书店', '城市'], tagsEn: ['Rain', 'Bookshop', 'City'] },
  { id: 'lighthouse', zh: '灯塔日落', en: 'Lighthouse at Sunset', category: 'landscape', tags: ['海岸', '灯塔', '日落'], tagsEn: ['Coast', 'Lighthouse', 'Sunset'] },
  { id: 'phone', zh: '手机日记 · 竖屏', en: 'Phone Diary · Vertical', category: 'phone', tags: ['手机视频', '竖屏'], tagsEn: ['Phone video', 'Vertical'], size: '720 × 1280' },
  { id: 'comic', zh: '漫剧 · 分镜试片', en: 'Motion Comic · Storyboard', category: 'comic', tags: ['漫剧', '分镜'], tagsEn: ['Motion comic', 'Storyboard'] },
  { id: 'observatory', zh: '山间观星台', en: 'Mountain Observatory', category: 'landscape', tags: ['山景', '星空', '夜景'], tagsEn: ['Mountain', 'Stars', 'Night'] },
];

const grid = document.getElementById('clip-grid');
const interactive = document.getElementById('demo-interactive');
const fallback = document.getElementById('demo-fallback');
const fallbackVideo = document.getElementById('fallback-video');
const fallbackTitle = document.getElementById('fallback-title');
const fallbackDirect = document.getElementById('fallback-direct');
const query = document.getElementById('clip-search');
const counter = document.getElementById('clip-count');
const empty = document.getElementById('clip-empty');
const buttons = [...document.querySelectorAll('[data-category]')];
const viewer = document.getElementById('viewer');
const player = document.getElementById('viewer-video');
const title = document.getElementById('viewer-title');
const specs = document.getElementById('viewer-specs');
const playerError = document.getElementById('viewer-error');
const playerDirect = document.getElementById('viewer-direct');
const timeline = document.getElementById('viewer-timeline');
const thumb = document.getElementById('viewer-thumb');
const thumbTime = thumb.querySelector('span');
const currentTime = document.getElementById('viewer-time');
const motion = window.matchMedia?.('(prefers-reduced-motion: reduce)');
const hasModal = typeof viewer.showModal === 'function';
const visible = new Set();
let category = 'all';
let selected = null;
let fallbackClip = SAMPLES[0];

function asset(name) {
  return new URL(name, ASSETS).href;
}

function localName(clip) {
  return isEnglish() ? clip.en : clip.zh;
}

function localTags(clip) {
  return isEnglish() ? clip.tagsEn : clip.tags;
}

function clipElement(clip) {
  const button = document.createElement('button');
  button.className = 'clip';
  button.type = 'button';
  button.innerHTML = '<span class="clip-art"><img width="1280" height="720" alt=""><video muted loop playsinline preload="none" aria-hidden="true"></video><span class="clip-play" aria-hidden="true">▶</span><span class="clip-duration" aria-hidden="true">0:08</span></span><span class="clip-info"><span class="clip-title"></span><span class="clip-meta"></span></span>';
  const image = button.querySelector('img');
  const video = button.querySelector('video');
  image.src = asset(clip.id + '.webp');
  image.loading = 'lazy';
  video.muted = true;
  video.defaultMuted = true;
  video.addEventListener('playing', () => video.classList.add('is-playing'));
  video.addEventListener('pause', () => video.classList.remove('is-playing'));
  button.addEventListener('click', () => open(clip));
  return { clip, button, video, title: button.querySelector('.clip-title'), detail: button.querySelector('.clip-meta') };
}

const cards = SAMPLES.map(clipElement);
grid.append(...cards.map(card => card.button));

function previewVisible() {
  const enabled = !document.hidden && !viewer.open && fallbackVideo.paused && !motion?.matches && !navigator.connection?.saveData;
  const playing = new Set(enabled ? cards.filter(card => visible.has(card.button) && !card.button.hidden).slice(0, 4) : []);
  for (const card of cards) {
    const { video, clip } = card;
    if (playing.has(card)) {
      if (!video.hasAttribute('src')) video.src = asset(clip.id + '-preview.mp4');
      if (video.paused) video.play()?.catch(() => {});
    } else if (!video.paused) {
      video.pause();
    }
  }
}

if ('IntersectionObserver' in window) {
  const observer = new IntersectionObserver(entries => {
    for (const entry of entries) {
      if (entry.isIntersecting) visible.add(entry.target);
      else visible.delete(entry.target);
    }
    previewVisible();
  }, { threshold: .2 });
  for (const { button } of cards) observer.observe(button);
}

function filter() {
  const terms = query.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
  let count = 0;
  for (const { clip, button, title, detail } of cards) {
    const haystack = [clip.zh, clip.en, ...clip.tags, ...clip.tagsEn].join(' ').toLowerCase();
    const matches = (category === 'all' || category === clip.category) && terms.every(term => haystack.includes(term));
    button.hidden = !matches;
    count += Number(matches);
    title.textContent = localName(clip);
    detail.textContent = localTags(clip).join(' · ');
    button.setAttribute('aria-label', isEnglish() ? `Play ${clip.en}` : `播放 ${clip.zh}`);
  }
  counter.textContent = isEnglish() ? `${count} of ${cards.length} clips` : `${cards.length} 段样片中的 ${count} 段`;
  empty.hidden = count !== 0;
  previewVisible();
}

function formatTime(seconds) {
  return `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`;
}

function refreshMetadata() {
  document.title = isEnglish() ? 'Sample library | CineVault Local' : '演示片库｜映库 · CineVault Local';
  fallbackTitle.textContent = localName(fallbackClip);
  if (!selected) return;
  title.textContent = localName(selected);
  specs.textContent = `${selected.size ?? '1280 × 720'} · H.264 · ${localTags(selected).join(' · ')}`;
}

function openInline(clip) {
  fallbackClip = clip;
  fallbackTitle.textContent = localName(clip);
  fallbackDirect.href = asset(clip.id + '.mp4');
  fallbackVideo.src = fallbackDirect.href;
  fallbackVideo.poster = asset(clip.id + '.webp');
  fallback.hidden = false;
  fallback.scrollIntoView();
  fallbackVideo.play()?.catch(() => {});
}

function open(clip) {
  if (!hasModal) {
    openInline(clip);
    return;
  }
  selected = clip;
  refreshMetadata();
  try {
    viewer.showModal();
  } catch {
    selected = null;
    openInline(clip);
    return;
  }
  playerError.hidden = true;
  timeline.max = 8;
  timeline.value = 0;
  thumb.hidden = true;
  thumb.style.setProperty('--thumb-image', `url("${asset(clip.id + '-frames.webp')}")`);
  currentTime.textContent = '0:00 / 0:08';
  fallbackVideo.pause();
  player.poster = asset(clip.id + '.webp');
  player.src = asset(clip.id + '.mp4');
  playerDirect.href = player.src;
  previewVisible();
  player.play()?.catch(() => {});
}

function showThumbnail(fraction) {
  if (!selected) return;
  const time = fraction * Number(timeline.max);
  const frame = Math.min(3, Math.round(time / 2));
  const width = timeline.getBoundingClientRect().width;
  const padding = Math.min(82, width / 2);
  const position = Math.max(padding, Math.min(width - padding, fraction * width));
  thumb.style.setProperty('--thumb-x', position + 'px');
  thumb.style.setProperty('--thumb-frame', (frame / 3 * 100) + '%');
  thumbTime.textContent = formatTime(time);
  thumb.hidden = false;
}

function rangeFraction() {
  return Number(timeline.value) / Number(timeline.max);
}

function timeChanged() {
  const duration = Number(timeline.max);
  timeline.value = player.currentTime;
  timeline.setAttribute('aria-valuetext', formatTime(player.currentTime));
  currentTime.textContent = `${formatTime(player.currentTime)} / ${formatTime(duration)}`;
}

for (const button of buttons) {
  button.addEventListener('click', () => {
    category = button.dataset.category;
    for (const candidate of buttons) candidate.setAttribute('aria-pressed', String(candidate === button));
    filter();
  });
}
query.addEventListener('input', filter);
document.addEventListener('visibilitychange', previewVisible);
document.addEventListener('cinevault-language', () => {
  filter();
  refreshMetadata();
});
if (motion?.addEventListener) motion.addEventListener('change', previewVisible);
else motion?.addListener?.(previewVisible);
fallbackVideo.addEventListener('play', previewVisible);
fallbackVideo.addEventListener('pause', previewVisible);
fallbackVideo.addEventListener('ended', previewVisible);
viewer.addEventListener('close', () => {
  selected = null;
  player.pause();
  player.removeAttribute('src');
  player.load();
  previewVisible();
});
player.addEventListener('loadedmetadata', () => {
  if (Number.isFinite(player.duration) && player.duration > 0) timeline.max = player.duration;
  timeChanged();
});
player.addEventListener('timeupdate', timeChanged);
player.addEventListener('error', () => {
  if (viewer.open) playerError.hidden = false;
});
timeline.addEventListener('input', () => {
  showThumbnail(rangeFraction());
  if (player.readyState > 0) player.currentTime = Number(timeline.value);
});
timeline.addEventListener('focus', () => showThumbnail(rangeFraction()));
timeline.addEventListener('blur', () => { thumb.hidden = true; });
timeline.addEventListener('pointermove', event => {
  const box = timeline.getBoundingClientRect();
  if (box.width > 0) showThumbnail(Math.max(0, Math.min(1, (event.clientX - box.left) / box.width)));
});
timeline.addEventListener('pointerleave', () => { thumb.hidden = true; });

filter();
refreshMetadata();
interactive.hidden = false;
if (hasModal && fallbackVideo.paused) fallback.hidden = true;
