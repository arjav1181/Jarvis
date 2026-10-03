/*
 * dashboard/static/sw.js — the phone half of the control surface.
 *
 * Two jobs, and only two:
 *
 *   1. Keep the shell alive. The interesting moments (a task finishing, an
 *      approval waiting) happen when the tab is closed — a service worker
 *      that serves the app from cache is what makes the notification's
 *      "open JARVIS" tap land on a working page instead of a dead network
 *      error. API calls are never cached: a stale task list is worse than
 *      an honest error.
 *
 *   2. Turn a tap into a decision. Notification action buttons cannot run
 *      page JS, so the worker POSTs the signed action token the server put
 *      in the payload. That token is the only credential this file needs,
 *      and it is single-purpose and short-lived by construction.
 */

const VERSION = 'jarvis-v4';
// '/' is deliberately NOT cached. It is the live dashboard document, and a
// cached copy is how a PWA ends up showing yesterday's UI with no way to tell.
// Offline navigation is handled by offline.html instead.
const SHELL = [
  '/login',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/static/widget.html',
  '/static/offline.html',
  '/manifest.webmanifest',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(VERSION)
      // addAll is all-or-nothing; a single 404 during deploy would leave the
      // worker permanently uninstalled, so each entry is cached on its own.
      .then((cache) => Promise.all(
        SHELL.map((url) => cache.add(url).catch(() => null))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  // Live data, websockets and the confirm endpoint stay off the cache.
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/ws')) return;

  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req)
        .catch(() => caches.match('/static/offline.html')
          .then((hit) => hit || new Response(
            '<!doctype html><title>JARVIS</title>'
            + '<body style="background:#00060a;color:#00d4ff;font:16px system-ui;'
            + 'padding:32px">JARVIS is offline.<br><br>'
            + 'Queued work, sent messages and pending approvals will still go out '
            + 'when the network returns.</body>',
            { headers: { 'Content-Type': 'text/html' } })))
        .then((res) => {
          const copy = res.clone();
          caches.open(VERSION).then((c) => c.put('/', copy)).catch(() => {});
          return res;
        })
        .catch(() => caches.match('/', { ignoreSearch: true })
          .then((hit) => hit || caches.match('/login')))
    );
    return;
  }
  event.respondWith(
    caches.match(req).then((hit) => hit || fetch(req).then((res) => {
      if (res.ok && url.pathname.startsWith('/static/')) {
        const copy = res.clone();
        caches.open(VERSION).then((c) => c.put(req, copy)).catch(() => {});
      }
      return res;
    }))
  );
});

/* ── push ─────────────────────────────────────────────────────────────────── */

self.addEventListener('push', (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    data = { title: 'JARVIS', body: event.data ? event.data.text() : '' };
  }
  const title = data.title || 'JARVIS';
  const opts = {
    body: data.body || '',
    icon: '/static/icon-192.png',
    badge: '/static/icon-192.png',
    tag: data.tag || 'jarvis',
    data: data,
    // Approvals must survive a locked screen until a human decides.
    requireInteraction: !!data.actions,
    silent: false,
    vibrate: data.actions ? [40, 60, 40] : [30],
  };
  if (Array.isArray(data.actions) && data.actions.length) {
    opts.actions = data.actions;
  }
  event.waitUntil(self.registration.showNotification(title, opts));
});

/* ── notification taps ────────────────────────────────────────────────────── */

async function focusOrOpen(url) {
  const target = new URL(url || '/', self.location.origin).href;
  const clients = await self.clients.matchAll(
    { type: 'window', includeUncontrolled: true });
  for (const c of clients) {
    if ('focus' in c) {
      c.postMessage({ type: 'navigate', url: target });
      return c.focus();
    }
  }
  return self.clients.openWindow(target);
}

self.addEventListener('notificationclick', (event) => {
  const data = (event.notification && event.notification.data) || {};
  const action = event.action || '';
  event.notification.close();

  if ((action === 'accept' || action === 'reject') && data[action + '_token']) {
    // Resolve the approval the same way the dashboard button does. No auth
    // header: the signed token is the credential.
    event.waitUntil(
      fetch('/api/push/respond', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: data[action + '_token'] }),
      })
        .then((r) => r.json())
        .then((r) => {
          if (r && r.ok) {
            return self.registration.showNotification('JARVIS', {
              body: action === 'accept'
                ? 'Approved — running it now.'
                : 'Rejected — nothing was done.',
              icon: '/static/icon-192.png',
              tag: 'jarvis-ack',
            });
          }
          return self.registration.showNotification('JARVIS', {
            body: (r && r.error) || 'That request expired.',
            icon: '/static/icon-192.png',
            tag: 'jarvis-ack',
          });
        })
        .catch(() => self.registration.showNotification('JARVIS', {
          body: 'Could not reach JARVIS — is the Space awake?',
          icon: '/static/icon-192.png',
          tag: 'jarvis-ack',
        }))
    );
    return;
  }
  event.waitUntil(focusOrOpen(data.url || '/'));
});

/* ── page asks the worker to do something ─────────────────────────────────── */

self.addEventListener('message', (event) => {
  const msg = event.data || {};
  if (msg.type === 'skip-waiting') self.skipWaiting();
  if (msg.type === 'ping' && event.ports && event.ports[0]) {
    event.ports[0].postMessage({ ok: true, version: VERSION });
  }
});


/* A home-screen widget cannot authenticate on its own, so it asks the page for
 * a short-lived read token and then polls the two numbers a person actually
 * wants on a lock screen: how many things are waiting, and what the briefing
 * said. Nothing else is exposed, and the token dies with the widget. */
self.addEventListener('message', (event) => {
  const data = event.data || {};
  if (data.type === 'jarvis.skipWaiting') {
    self.skipWaiting();
    return;
  }
  if (data.type === 'jarvis.widget.ping' && data.token) {
    event.waitUntil((async () => {
      const base = new URL('/api/widget', self.location.origin).toString();
      const r = await fetch(base, { headers: { Authorization: 'Bearer ' + data.token } });
      const payload = await r.json().catch(() => ({}));
      const clients = await self.clients.matchAll({ type: 'window' });
      for (const c of clients) c.postMessage({ type: 'jarvis.widget.data', payload });
    })());
  }
});
