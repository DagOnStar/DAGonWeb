// DAGonWeb service worker: network first, cached shell as an offline fallback.
const CACHE = 'dagonweb-v2';
const SHELL = [
  '/static/css/app.css',
  '/static/js/editor.js',
  '/static/js/run.js',
  '/static/js/theme.js',
  '/static/icons/icon.svg',
  '/static/manifest.webmanifest',
];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', event => {
  const request = event.request;
  // Only same-origin GET requests are cached; API mutations always hit the network.
  if (request.method !== 'GET' || new URL(request.url).origin !== self.location.origin) return;
  event.respondWith(
    fetch(request)
      .then(response => {
        if (response.ok && new URL(request.url).pathname.startsWith('/static/')) {
          const copy = response.clone();
          caches.open(CACHE).then(cache => cache.put(request, copy));
        }
        return response;
      })
      .catch(() => caches.match(request)),
  );
});
