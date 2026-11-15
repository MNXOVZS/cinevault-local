/**
 * [INPUT]: 双语文案、链接语言参数和浏览器语言。
 * [OUTPUT]: 更新双语文案、辅助标签和当前语言事件。
 * [POS]: 介绍页与演示页共用语言入口。
 * [PROTOCOL]: Update this header on change, then check AGENTS.md
 */
const entries = [...document.querySelectorAll('[data-en], [data-en-label], [data-en-alt], [data-en-placeholder]')].map(element => ({
  element,
  chinese: {
    text: element.dataset.en && element.textContent,
    label: element.getAttribute('aria-label'),
    alt: element.getAttribute('alt'),
    placeholder: element.getAttribute('placeholder'),
  },
}));
const button = document.querySelector('[data-language]');
const requested = new URLSearchParams(window.location.search).get('lang');
const hasLanguage = requested === 'en' || requested === 'zh';
let saved = '';
try {
  saved = localStorage.getItem('cinevault-site-language') || '';
} catch {
  // 储存不可用时仍可在本页切换。
}

function apply(language) {
  const english = language === 'en';
  document.documentElement.lang = english ? 'en' : 'zh-CN';
  for (const { element, chinese } of entries) {
    if (element.dataset.en) element.textContent = english ? element.dataset.en : chinese.text;
    if (element.dataset.enLabel) element.setAttribute('aria-label', english ? element.dataset.enLabel : chinese.label);
    if (element.dataset.enAlt) element.setAttribute('alt', english ? element.dataset.enAlt : chinese.alt);
    if (element.dataset.enPlaceholder) element.setAttribute('placeholder', english ? element.dataset.enPlaceholder : chinese.placeholder);
  }
  if (button) {
    button.textContent = english ? '中文' : 'EN';
    button.setAttribute('aria-label', english ? '切换为简体中文' : 'Switch to English');
  }
  document.dispatchEvent(new Event('cinevault-language'));
}

export function isEnglish() {
  return document.documentElement.lang === 'en';
}

function remember(language) {
  try {
    localStorage.setItem('cinevault-site-language', language);
  } catch {
    // 切换仍能生效。
  }
}

const browserLanguage = navigator.language.toLowerCase().startsWith('zh') ? 'zh' : 'en';
apply(hasLanguage ? requested : saved || browserLanguage);
if (hasLanguage) remember(requested);
button?.addEventListener('click', () => {
  const language = isEnglish() ? 'zh' : 'en';
  apply(language);
  remember(language);
  if (hasLanguage && window.location.protocol.startsWith('http')) {
    const current = new URL(window.location.href);
    current.searchParams.set('lang', language);
    history.replaceState(history.state, '', current);
  }
});
