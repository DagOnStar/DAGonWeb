/* Cookie and storage consent (GDPR / ePrivacy).
   Strictly necessary storage needs no consent. Optional "preferences"
   storage (theme, panel widths in localStorage) is used only after the
   visitor allows it. The choice lives in the first-party dw_consent cookie. */
(() => {
  'use strict';
  const NAME = 'dw_consent';
  const VERSION = '1';
  const MAX_AGE = 180 * 24 * 3600;
  const PREFERENCE_KEYS = ['dagonweb.theme', 'dagonweb.inspector-width', 'dagonweb.run-details-width'];

  function read() {
    const raw = document.cookie.split('; ').find(part => part.startsWith(`${NAME}=`));
    if (!raw) return null;
    const params = new URLSearchParams(decodeURIComponent(raw.slice(NAME.length + 1)));
    if (params.get('v') !== VERSION) return null;
    return { preferences: params.get('preferences') === '1', date: params.get('date') || '' };
  }

  function write(choice) {
    const value = new URLSearchParams({ v: VERSION, preferences: choice.preferences ? '1' : '0', date: new Date().toISOString().slice(0, 10) }).toString();
    const secure = window.location.protocol === 'https:' ? '; Secure' : '';
    document.cookie = `${NAME}=${encodeURIComponent(value)}; Max-Age=${MAX_AGE}; Path=/; SameSite=Lax${secure}`;
    if (!choice.preferences) {
      PREFERENCE_KEYS.forEach(key => { try { localStorage.removeItem(key); } catch { /* ignore */ } });
    }
    document.dispatchEvent(new CustomEvent('dagonweb:consent', { detail: choice }));
  }

  window.DAGonWebConsent = {
    allows(category) {
      if (category === 'necessary') return true;
      const choice = read();
      return Boolean(choice && choice[category]);
    },
    choice: read,
    set: write,
  };

  document.addEventListener('DOMContentLoaded', () => {
    const banner = document.getElementById('cookieBanner');
    const dialog = document.getElementById('cookieSettings');
    const toggle = document.getElementById('consentPreferences');
    const close = () => { if (banner) banner.hidden = true; };
    const save = choice => { write(choice); close(); window.bootstrap?.Modal.getInstance(dialog)?.hide(); };
    if (banner && banner.dataset.enabled === '1' && !read()) banner.hidden = false;
    document.querySelectorAll('[data-consent="all"]').forEach(button => button.addEventListener('click', () => save({ preferences: true })));
    document.querySelectorAll('[data-consent="necessary"]').forEach(button => button.addEventListener('click', () => save({ preferences: false })));
    document.querySelectorAll('[data-consent="save"]').forEach(button => button.addEventListener('click', () => save({ preferences: Boolean(toggle?.checked) })));
    dialog?.addEventListener('show.bs.modal', () => { if (toggle) toggle.checked = Boolean(read()?.preferences); });
  });
})();
