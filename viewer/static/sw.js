/* ==========================================================================
   WeChat Vault · Service Worker
   策略：
     · 外壳（HTML/CSS/JS/图标）→ stale-while-revalidate，离线可开
     · API 与媒体            → 只走网络（数据必须新鲜；离线时明确报错）
   不做：把聊天数据缓存到浏览器（隐私红线，也避免缓存体积失控）
   ========================================================================== */

const VER = 'wv-v1.9.0';   // v1.9.0 视觉系统 v2：Apple 材质/排印/动效 + 阅读器暗色
const SHELL = [
  '/',
  '/app.js',
  '/app.css',
  '/icon.svg',
  '/manifest.webmanifest',
  '/login',
  '/login.css',
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(VER)
      .then(c => Promise.allSettled(SHELL.map(u => c.add(new Request(u, { cache: 'reload' })))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== VER).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // API / 媒体：只走网络，不缓存（数据要新鲜）
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/media/')) {
    e.respondWith(
      fetch(req).catch(() => new Response(
        JSON.stringify({ detail: '离线状态，无法获取数据' }),
        { status: 503, headers: { 'Content-Type': 'application/json' } }
      ))
    );
    return;
  }

  // 导出下载：直接放行，不拦截
  if (url.pathname.startsWith('/api/export/')) return;

  // 外壳：先给缓存（快），同时后台更新（新）
  e.respondWith(
    caches.match(req).then(hit => {
      const net = fetch(req).then(res => {
        if (res && res.status === 200 && res.type === 'basic') {
          const copy = res.clone();
          caches.open(VER).then(c => c.put(req, copy)).catch(() => {});
        }
        return res;
      }).catch(() => hit);
      return hit || net;
    })
  );
});
