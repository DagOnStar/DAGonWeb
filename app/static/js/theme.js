// Light/dark/auto theme switcher for Bootstrap 5.3 (stored per browser).
(() => {
  const KEY = 'dagonweb.theme';
  const media = window.matchMedia('(prefers-color-scheme: dark)');
  const stored = () => { try { return localStorage.getItem(KEY) || 'auto'; } catch { return 'auto'; } };
  const apply = choice => {
    const theme = choice === 'auto' ? (media.matches ? 'dark' : 'light') : choice;
    document.documentElement.setAttribute('data-bs-theme', theme);
    document.querySelectorAll('[data-theme-choice]').forEach(item => {
      item.classList.toggle('active', item.dataset.themeChoice === choice);
      item.setAttribute('aria-pressed', String(item.dataset.themeChoice === choice));
    });
    const label = document.getElementById('themeLabel');
    if (label) label.textContent = { auto: '◐', light: '☀', dark: '☾' }[choice] || '◐';
  };
  apply(stored());
  media.addEventListener('change', () => { if (stored() === 'auto') apply('auto'); });
  document.addEventListener('DOMContentLoaded', () => {
    apply(stored());
    document.querySelectorAll('[data-theme-choice]').forEach(item => item.addEventListener('click', () => {
      try { localStorage.setItem(KEY, item.dataset.themeChoice); } catch { /* private mode */ }
      apply(item.dataset.themeChoice);
    }));
  });
})();
