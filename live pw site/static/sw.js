/* Service worker for the PW League home-screen app.
 *
 * Pages are network-first: the site is live data, so a cached page is only
 * ever a fallback for when there's no signal. The last copy of each page you
 * opened is kept for that, and anything never visited gets the offline page.
 * Icons and the Bootstrap files rarely change, so they're served from cache.
 *
 * Bump VERSION whenever this file or the offline page changes.
 */
const VERSION = 'pw-v1';
const OFFLINE_URL = '/offline';
const PRECACHE = [OFFLINE_URL, '/static/icons/icon-192.png'];
const MAX_PAGES = 40;

self.addEventListener('install', event => {
    event.waitUntil(caches.open(VERSION).then(c => c.addAll(PRECACHE)));
    self.skipWaiting();
});

self.addEventListener('activate', event => {
    event.waitUntil(
        caches.keys()
            .then(keys => Promise.all(keys.filter(k => k !== VERSION).map(k => caches.delete(k))))
            .then(() => self.clients.claim()));
});

async function trimPages(cache) {
    const keys = await cache.keys();
    const pages = keys.filter(r => !r.url.includes('/static/') && !r.url.endsWith(OFFLINE_URL)
                                   && !r.url.includes('cdn.jsdelivr.net'));
    for (const r of pages.slice(0, Math.max(0, pages.length - MAX_PAGES))) {
        await cache.delete(r);
    }
}

self.addEventListener('fetch', event => {
    const req = event.request;
    if (req.method !== 'GET') return;
    const url = new URL(req.url);

    if (req.mode === 'navigate') {
        event.respondWith((async () => {
            try {
                const fresh = await fetch(req);
                if (fresh.ok) {
                    const cache = await caches.open(VERSION);
                    await cache.put(req, fresh.clone());
                    trimPages(cache);
                }
                return fresh;
            } catch (e) {
                return (await caches.match(req)) || (await caches.match(OFFLINE_URL));
            }
        })());
        return;
    }

    const cacheable = (url.origin === location.origin && url.pathname.startsWith('/static/'))
                      || url.hostname === 'cdn.jsdelivr.net';
    if (cacheable) {
        event.respondWith((async () => {
            const hit = await caches.match(req);
            if (hit) return hit;
            const fresh = await fetch(req);
            if (fresh.ok || fresh.type === 'opaque') {
                (await caches.open(VERSION)).put(req, fresh.clone());
            }
            return fresh;
        })());
    }
    // Everything else (API calls, live scores) goes straight to the network.
});
