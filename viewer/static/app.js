/* ==========================================================================
   WeChat Vault · 前端逻辑
   ========================================================================== */
(() => {
'use strict';

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const state = {
  convs: [],
  current: null,        // 当前会话 id
  sort: 'recent',
  filter: '',
  msgTotal: 0,
  msgLoaded: 0,
  pageSize: 200,
  findMatches: [],
  findIdx: -1,
  account: '',          // '' = 全部账号；否则为具体微信号
  accounts: [],         // [{account,n_conv,n_msg,...}]
  // 「查找聊天记录」筛选条件
  findScope: 'all',     // all | conv
  findTypes: '',        // '' = 全部；否则单类型
  findFrom: '',
  findTo: '',
  // 分页 / 无缝加载
  hasMore: false,
  loadingMore: false,
  // 按日期查找
  calView: 'cal',       // cal | list
  calDays: {},          // 'YYYY-MM-DD' → {n,img,vid,fil,lnk,voi}
  calFirst: '',
  calLast: '',
  calYM: '',
};

/* ------------------------------ 工具 ------------------------------ */

const esc = s => String(s ?? '')
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;').replace(/'/g,'&#39;');

const escRe = s => String(s).replace(/[.*+?^${}()|[\]\\]/g,'\\$&');

function fmtTime(ts, brief = true) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const y = new Date(now); y.setDate(y.getDate() - 1);
  const isYest = d.toDateString() === y.toDateString();
  const hm = `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
  if (!brief) {
    return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')} ${hm}`;
  }
  if (sameDay) return hm;
  if (isYest)  return `昨天 ${hm}`;
  if (d.getFullYear() === now.getFullYear())
    return `${d.getMonth()+1}月${d.getDate()}日 ${hm}`;
  return `${d.getFullYear()}/${d.getMonth()+1}/${d.getDate()}`;
}

function dayKey(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${d.getMonth()+1}-${d.getDate()}`;
}

function fmtDay(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const now = new Date();
  const hm = `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
  if (d.toDateString() === now.toDateString()) return `今天 ${hm}`;
  const y = new Date(now); y.setDate(y.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return `昨天 ${hm}`;
  return `${d.getFullYear()}年${d.getMonth()+1}月${d.getDate()}日 ${hm}`;
}

/* 消息类型 → 图标 + 文案 */
const TYPE_ICON = {
  image:'[图片]', voice:'[语音]', video:'[视频]', file:'[文件]',
  emoji:'[表情]', link:'[链接]', location:'[位置]', card:'[名片]',
  transfer:'[转账]', redpacket:'[红包]', system:'', other:'[消息]'
};

/* 稳定配色头像 */
const AV_COLORS = ['#5b8ff9','#61ddaa','#65789b','#f6bd16','#7262fd',
                   '#78d3f8','#9661bc','#f6903d','#008685','#f08bb4'];
function avatarColor(name) {
  let h = 0;
  for (const ch of String(name)) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return AV_COLORS[h % AV_COLORS.length];
}
function initial(name) {
  const s = String(name || '?').trim();
  return s ? s[0].toUpperCase() : '?';
}

/* 头像：优先用微信头像（/api/avatar），拿不到就自动回退成稳定色块首字。
   实现要点：色块始终先渲染（含首字），头像图覆盖在其上；
   接口 404 时 onerror 直接移除 <img>，自然露出色块——无需额外请求编排。 */
function avatarHTML(name, cls = 'avatar') {
  const n = String(name || '');
  // 自己这一侧用品牌绿（.me-avatar 渐变），不再走随机色
  const selfSide = cls.indexOf('me-avatar') >= 0;
  const bg = selfSide ? '' : ` style="background:${avatarColor(n)}"`;
  return `<div class="${cls}"${bg}>`
    + `<span class="av-fb">${esc(initial(n))}</span>`
    + `<img class="av-img" src="/api/avatar?name=${encodeURIComponent(n)}"`
    + ` alt="" loading="lazy" onerror="this.remove()">`
    + `</div>`;
}

async function api(path) {
  const r = await fetch(path);
  if (r.status === 401) {
    // 会话失效 → 回登录页
    location.href = '/login';
    throw new Error('401 需要登录');
  }
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

/* ------------------------------ 账号切换器 ------------------------------ */

async function loadAccounts() {
  try {
    const d = await api('/api/accounts');
    state.accounts = d.items || [];
  } catch {
    state.accounts = [];
  }
  renderAccount();
}

function currentAccountInfo() {
  if (!state.account) {
    const total = state.accounts.reduce((a, b) => a + b.n_msg, 0);
    const nc = state.accounts.reduce((a, b) => a + b.n_conv, 0);
    return { name: '全部账号', sub: `${state.accounts.length} 个微信号 · ${nc} 会话 · ${total} 条` };
  }
  const it = state.accounts.find(a => a.account === state.account);
  if (!it) return { name: state.account, sub: '' };
  return { name: it.account, sub: `${it.n_conv} 会话 · ${it.n_msg} 条` };
}

function renderAccount() {
  const cur = currentAccountInfo();
  $('#acctName').textContent = cur.name;
  $('#acctSub').textContent = cur.sub;
  $('#acctAvatar').textContent = initial(cur.name);

  const box = $('#acctMenu');
  const total = state.accounts.reduce((a, b) => a + b.n_msg, 0);
  const nc = state.accounts.reduce((a, b) => a + b.n_conv, 0);
  const rows = [{ account: '', n_conv: nc, n_msg: total, _all: true }, ...state.accounts];
  box.innerHTML = rows.map(a => {
    const name = a._all ? '全部账号' : a.account;
    const active = (a.account || '') === state.account ? ' active' : '';
    return `<div class="acct-item${active}" data-acct="${esc(a.account)}">
      ${avatarHTML(name)}
      <div class="ai-meta">
        <div class="ai-name">${esc(name)}</div>
        <div class="ai-sub">${a.n_conv} 会话 · ${a.n_msg} 条消息</div>
      </div>
      <svg class="ai-check" viewBox="0 0 24 24"><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>
    </div>`;
  }).join('');

  $$('.acct-item', box).forEach(el => {
    el.onclick = () => {
      state.account = el.dataset.acct || '';
      localStorage.setItem('wv-account', state.account);
      closeAcctMenu();
      renderAccount();
      resetChat();
      loadConvs().then(loadAccounts);
    };
  });
}

function openAcctMenu() {
  $('#acctMenu').style.display = 'block';
  $('#acctSwitcher').classList.add('open');
}
function closeAcctMenu() {
  $('#acctMenu').style.display = 'none';
  $('#acctSwitcher').classList.remove('open');
}

function resetChat() {
  state.current = null;
  state.msgLoaded = 0;
  state.msgTotal = 0;
  $('#chatHeader').style.display = 'none';
  $('#emptyState').style.display = '';
  $('#msgList').innerHTML = '';
}

/* ------------------------------ 会话列表 ------------------------------ */

async function loadConvs() {
  const p = new URLSearchParams({ sort: state.sort, limit: 2000 });
  if (state.filter) p.set('q', state.filter);
  if (state.account) p.set('account', state.account);
  const d = await api('/api/conversations?' + p);
  state.convs = d.items;
  renderConvs();
}

function ym(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
}

/* 会话行的第二行：归档场景下"最近消息预览"拿不到，改为展示**时间跨度**——
   一眼看出这段关系持续了多久，比重复一遍条数有用得多。 */
function convPreview(c) {
  const a = ym(c.start_ts), b = ym(c.end_ts);
  const span = a && b ? (a === b ? a : `${a} ~ ${b}`) : '';
  const kind = c.is_group ? '群聊' : '单聊';
  return span ? `${kind} · ${span}` : kind;
}

function renderConvs() {
  const box = $('#convList');
  if (!state.convs.length) {
    box.innerHTML = '<div class="hint">没有会话</div>';
    return;
  }
  box.innerHTML = state.convs.map(c => {
    const active = c.conv_id === state.current ? ' active' : '';
    const grp = c.is_group ? '<span class="tag-group">群</span>' : '';
    return `
      <div class="conv-item${active}" data-id="${esc(c.conv_id)}">
        ${avatarHTML(c.title)}
        <div class="meta">
          <div class="row1">
            <span class="name">${esc(c.title)}${grp}</span>
            <span class="time">${esc(fmtTime(c.end_ts))}</span>
          </div>
          <div class="row2">
            <span class="preview">${esc(convPreview(c))}</span>
            <span class="cnt">${c.n_msg > 9999 ? (Math.round(c.n_msg / 1000) + 'k') : c.n_msg}</span>
          </div>
        </div>
      </div>`;
  }).join('');

  $$('.conv-item', box).forEach(el => {
    el.onclick = () => openConv(el.dataset.id);
  });
}

/* ------------------------------ 打开会话 ------------------------------ */

async function openConv(id) {
  state.current = id;
  const conv = state.convs.find(c => c.conv_id === id);
  if (!conv) return;

  $('#emptyState').style.display = 'none';
  $('#chatHeader').style.display = 'flex';
  $('#chatTitle').textContent = conv.title;
  $('#chatSub').textContent =
    `${conv.n_msg} 条消息 · ${conv.source.toUpperCase()}` +
    (conv.is_group ? ` · ${conv.members.length} 人` : '');

  renderConvs();
  $('#sidebar').classList.add('hidden');
  syncFinderUI();   // 打开会话后「当前会话」范围才可用

  // 首屏：加载最后 N 条
  state.msgLoaded = 0;
  state.hasMore = false;
  state.loadingMore = false;
  $('#msgList').innerHTML = '';
  const last = await api(`/api/messages?conv_id=${encodeURIComponent(id)}&offset=0&limit=${state.pageSize}&order=desc`);
  state.msgTotal = last.total;
  state.hasMore = last.has_more;
  renderMessages(last.items, 'prepend');
  state.msgLoaded = last.items.length;
  updateLoadMoreHint();

  // 首屏不足一屏（长会话很常见）时继续补页，避免出现"半屏空白 + 按钮"
  let guard = 0;
  const sc = $('#msgScroller');
  while (state.hasMore && guard < 8 &&
         sc.scrollHeight <= sc.clientHeight + 60) {
    await loadMore();
    guard++;
  }
  scrollToBottom(false);
  updateFindBar();
}

/* ------------------------------ 渲染消息 ------------------------------ */

function renderMessages(msgs, mode = 'append') {
  const list = $('#msgList');
  const frag = document.createDocumentFragment();
  let lastDay = null;
  let lastTs = 0;

  // prepend 时，需要知道已有首条的时间，避免重复插入日期分隔
  if (mode === 'prepend') {
    const first = list.querySelector('.msg');
    if (first) { lastTs = Number(first.dataset.ts || 0); }
    lastDay = lastTs ? dayKey(lastTs) : null;
  }

  msgs.forEach(m => {
    // 日期分隔：与上一条跨天 / 间隔超过 5 分钟
    const dk = dayKey(m.ts);
    const gap = m.ts && lastTs ? (m.ts - lastTs) : 999;
    if (dk && dk !== lastDay) {
      const sep = document.createElement('div');
      sep.className = 'time-sep';
      sep.dataset.ts = m.ts;
      sep.innerHTML = `<span>${esc(fmtDay(m.ts))}</span>`;
      frag.appendChild(sep);
    } else if (m.ts && lastTs && gap > 300) {
      const sep = document.createElement('div');
      sep.className = 'time-sep';
      sep.dataset.ts = m.ts;
      sep.innerHTML = `<span>${esc(fmtDay(m.ts))}</span>`;
      frag.appendChild(sep);
    }

    if (m.type === 'system' || (!m.content && !m.media)) {
      const el = document.createElement('div');
      el.className = 'sys-msg';
      el.dataset.ts = m.ts || '';
      el.dataset.mid = m.msg_id;
      el.innerHTML = `<span>${esc(m.content || '')}</span>`;
      frag.appendChild(el);
    } else {
      frag.appendChild(buildMsg(m));
    }
    if (m.ts) lastTs = m.ts;
    if (dk) lastDay = dk;
  });

  if (mode === 'prepend') {
    list.insertBefore(frag, list.firstChild);
  } else {
    list.appendChild(frag);
  }
}

function buildMsg(m) {
  const el = document.createElement('div');
  el.className = 'msg' + (m.is_self ? ' self' : '');
  el.dataset.mid = m.msg_id;
  el.dataset.ts = m.ts || '';
  el.dataset.text = (m.content || '').toLowerCase();

  const av = avatarHTML(m.sender, m.is_self ? 'avatar me-avatar' : 'avatar');
  const name = m.is_self ? '' : `<div class="sender-name">${esc(m.sender)}</div>`;

  let inner = '';
  const t = m.type;

  if (t === 'image') {
    if (m.media) {
      // media = 缩略图（列表快），media_full = WxAM 解出的原图（点开才加载）
      inner = `<div class="bubble media"><img src="${esc(m.media)}" loading="lazy" alt="图片" data-zoom="1"`
        + (m.media_full ? ` data-full="${esc(m.media_full)}"` : "") + `></div>`;
    } else {
      inner = `<div class="bubble"><div class="media-ph">🖼 ${esc(m.content || '[图片]')}</div></div>`;
    }
  } else if (t === 'voice') {
    // 语音：已解码为 WAV → 直接内联播放器；未解出则保留占位
    inner = m.media
      ? `<div class="bubble voice"><audio controls preload="none" src="${esc(m.media)}"></audio>`
        + `<span class="voice-len">${esc((m.content || '').replace(/[\[\]]/g, ''))}</span></div>`
      : `<div class="bubble"><div class="media-ph">🎤 ${esc(m.content || '[语音]')}</div></div>`;
  } else if (t === 'video') {
    // 视频：有 mp4 就播；只有封面（_thumb.jpg）时显示封面并提示
    if (m.media && /\.mp4$/i.test(m.media)) {
      inner = `<div class="bubble media"><video src="${esc(m.media)}" controls preload="metadata"`
        + ` style="max-width:300px;border-radius:5px"></video></div>`;
    } else if (m.media) {
      inner = `<div class="bubble media video-cover" title="本机未留存原视频，仅封面">`
        + `<img src="${esc(m.media)}" loading="lazy" alt="视频封面" data-zoom="1">`
        + `<span class="vc-badge">▶</span></div>`;
    } else {
      inner = `<div class="bubble"><div class="media-ph">🎬 ${esc(m.content || '[视频]')}</div></div>`;
    }
  } else if (t === 'file') {
    // 文件：本机留存的原始文件 → 直接给下载链接（保留真实文件名）
    const fm = /\[文件\]\s*(.+)$/.exec(m.content || '');
    inner = m.media
      ? `<div class="bubble file-b"><a class="file-link" href="${esc(m.media)}"`
        + ` download="${esc(fm ? fm[1] : '')}"><span class="fl-ic">📄</span>`
        + `<span class="fl-nm">${esc(fm ? fm[1] : (m.content || '文件'))}</span>`
        + `<span class="fl-dl">下载</span></a></div>`
      : `<div class="bubble"><div class="media-ph">📎 ${esc(m.content || '[文件]')}</div></div>`;
  } else if (t === 'emoji') {
    // 表情包：已下载到本地 → 直接显示动图；否则显示占位
    inner = m.media
      ? `<div class="bubble sticker"><img src="${esc(m.media)}" loading="lazy" alt="表情" data-zoom="1"></div>`
      : `<div class="bubble sticker-ph"><span>🙂</span></div>`;
  } else if (t === 'link') {
    inner = `<div class="bubble">${linkify(m.content)}</div>`;
  } else {
    inner = `<div class="bubble">${linkify(m.content)}</div>`;
  }

  el.innerHTML = `${av}<div class="body">${name}${inner}</div>`;
  return el;
}

/* 文本里的 URL 变链接 */
function linkify(text) {
  let h = esc(text || '');
  h = h.replace(/(https?:\/\/[^\s<]+)/g,
    u => `<a href="${u}" target="_blank" rel="noopener">${u}</a>`);
  return h;
}

/* ------------------------------ 分页（无缝上滑加载） ------------------------------ */

/* 底部提示条：还有更早 → 转圈提示；已到底 → 明确告知 */
function updateLoadMoreHint() {
  const box = $('#loadMore');
  if (!box) return;
  if (state.hasMore || state.loadingMore) {
    box.style.display = 'flex';
    $('#loadMoreText').textContent = state.loadingMore
      ? '正在加载更早的消息…' : '继续上滑加载更早的消息';
  } else {
    box.style.display = 'flex';
    $('#loadMoreText').textContent = '— 已经是最早的消息了 —';
  }
  $('#btnLoadMore').style.display = 'none';
  box.classList.toggle('lm-end', !state.hasMore && !state.loadingMore);
}

async function loadMore() {
  if (!state.current || state.loadingMore || !state.hasMore) return;
  const sc = $('#msgScroller');
  const prevH = sc.scrollHeight;
  state.loadingMore = true;
  updateLoadMoreHint();

  let d = null;
  try {
    d = await api(`/api/messages?conv_id=${encodeURIComponent(state.current)}`
      + `&offset=${state.msgLoaded}&limit=${state.pageSize}&order=desc`);
  } catch (err) {
    state.loadingMore = false;
    $('#loadMoreText').textContent = '加载失败';
    $('#btnLoadMore').style.display = 'inline-block';
    $('#btnLoadMore').textContent = '重试';
    return;
  }
  if (!d.items.length) {
    state.hasMore = false;
    state.loadingMore = false;
    updateLoadMoreHint();
    return;
  }
  renderMessages(d.items, 'prepend');
  state.msgLoaded += d.items.length;
  state.hasMore = d.has_more;
  state.loadingMore = false;

  // 保持视口位置：新内容在顶部插入，补偿滚动高度差 → 视觉上"无缝"
  sc.scrollTop = sc.scrollHeight - prevH;
  updateLoadMoreHint();
}

/* ------------------------------ 滚动 ------------------------------ */

function scrollToBottom(smooth = true) {
  const sc = $('#msgScroller');
  sc.scrollTo({ top: sc.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
}

function scrollToTop() {
  const sc = $('#msgScroller');
  sc.scrollTo({ top: 0, behavior: 'smooth' });
}

/* 滚动时显示当日提示 + 最新按钮 + 无缝上滑加载 */
let divTimer = null;
function onScroll() {
  const sc = $('#msgScroller');
  const nearBottom = sc.scrollHeight - sc.scrollTop - sc.clientHeight < 120;
  $('#jumpToday').style.display = nearBottom ? 'none' : 'block';

  // 无缝加载：接近顶部就自动取更早的一页（有并发保护，见 loadMore）
  if (sc.scrollTop < 300 && state.hasMore && !state.loadingMore) loadMore();

  // 找当前视口顶部的消息，显示其日期
  const list = $('#msgList');
  const mid = sc.scrollTop + 20;
  const nodes = $$('.msg, .sys-msg', list);
  for (let i = nodes.length - 1; i >= 0; i--) {
    if (nodes[i].offsetTop <= mid) {
      const ts = Number(nodes[i].dataset.ts || 0);
      if (ts) {
        const dv = $('#dayDivider');
        dv.textContent = fmtDay(ts);
        dv.classList.add('show');
        clearTimeout(divTimer);
        divTimer = setTimeout(() => dv.classList.remove('show'), 900);
      }
      break;
    }
  }
}

/* ------------------------------ 查找聊天记录 ------------------------------ */

function localDate(d) {
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function openSearch() {
  if (state.current && state.findScope === 'conv') { /* 保持当前选择 */ }
  $('#searchPanel').style.display = 'flex';
  syncFinderUI();
  const i = $('#globalSearchInput');
  if (!i.value.trim() && !state.findTypes && !state.findFrom && !state.findTo) i.value = '';
  i.focus();
  runFinder();
}

/* 把 state 里的筛选条件回灌到控件 */
function syncFinderUI() {
  $$('#findScope .scope-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.scope === state.findScope));
  $$('#findTypes .chip').forEach(b =>
    b.classList.toggle('active', (b.dataset.type || '') === state.findTypes));
  $('#findFrom').value = state.findFrom;
  $('#findTo').value = state.findTo;
  const convBtn = $('#findScope').querySelector('[data-scope="conv"]');
  if (convBtn) {
    convBtn.disabled = !state.current;
    convBtn.title = state.current ? '' : '请先打开一个会话';
  }
  if (!state.current && state.findScope === 'conv') state.findScope = 'all';
}

function finderQuery(extra = {}) {
  const qs = new URLSearchParams();
  const kw = $('#globalSearchInput').value.trim();
  if (kw) qs.set('q', kw);
  if (state.findTypes) qs.set('types', state.findTypes);
  if (state.findFrom) qs.set('date_from', state.findFrom);
  if (state.findTo) qs.set('date_to', state.findTo);
  if (state.account) qs.set('account', state.account);
  if (state.findScope === 'conv' && state.current) qs.set('conv_id', state.current);
  Object.entries(extra).forEach(([k, v]) => qs.set(k, v));
  return qs.toString();
}

let searchTimer = null;

async function runFinder() {
  const kw = $('#globalSearchInput').value.trim();
  const hasFilter = !!(state.findTypes || state.findFrom || state.findTo);
  const body = $('#gSearchBody');
  if (!kw && !hasFilter) {
    body.innerHTML = '<div class="hint">输入关键词，或选「类型 / 日期」开始查找</div>';
    return;
  }
  body.innerHTML = '<div class="hint">查找中…</div>';
  let d;
  try {
    d = await api('/api/search?' + finderQuery({ limit: 300 }));
  } catch (err) {
    body.innerHTML = `<div class="hint">查找失败：${esc(err.message)}</div>`;
    return;
  }
  if (!d.total) {
    body.innerHTML = '<div class="hint">没有符合条件的聊天记录</div>';
    return;
  }
  const re = kw ? new RegExp(`(${escRe(kw)})`, 'gi') : null;
  const scopeTip = (state.findScope === 'conv' && state.current)
    ? ' · 仅当前会话'
    : (state.account ? ' · 当前账号' : ' · 全部会话');
  body.innerHTML =
    `<div class="find-sum">共 <b>${d.total}</b> 条${scopeTip}` +
    (d.total > d.count ? `，显示最近 ${d.count} 条` : '') + '</div>' +
    d.items.map(h => {
      const conv = state.convs.find(c => c.conv_id === h.conv_id);
      const title = conv ? conv.title : h.conv_id;
      const txt = h.content || '';
      const hl = re ? esc(txt).replace(re, '<span class="hit">$1</span>') : esc(txt);
      return `<div class="res-item" data-conv="${esc(h.conv_id)}" data-mid="${esc(h.msg_id)}" data-ts="${h.ts || 0}">
        <div class="res-head">
          <span class="res-conv">${esc(title)}</span>
          <span class="res-time">${esc(fmtTime(h.ts, false))}</span>
        </div>
        <div class="res-text">${hl}</div>
        <div class="res-sender">${esc(h.sender)}${h.is_self ? '（我）' : ''}
          <span class="res-type">${esc(TYPE_ICON[h.type] || '')}</span></div>
      </div>`;
    }).join('');

  $$('.res-item', body).forEach(el => {
    el.onclick = async () => {
      $('#searchPanel').style.display = 'none';
      if (el.dataset.conv !== state.current) await openConv(el.dataset.conv);
      setTimeout(() => jumpToMsg(el.dataset.mid, Number(el.dataset.ts || 0)), 260);
    };
  });
}

/* ------------------------------ 按日期查找（月历 / 列表） ------------------------------ */

const WEEK_CN = ['日', '一', '二', '三', '四', '五', '六'];

async function openDatePicker() {
  if (!state.current) { alert('请先打开一个会话'); return; }
  $('#datePanel').style.display = 'flex';
  const body = $('#dateBody');
  body.innerHTML = '<div class="hint">读取中…</div>';
  let d;
  try {
    d = await api(`/api/calendar?conv_id=${encodeURIComponent(state.current)}&limit=4000`);
  } catch (err) {
    body.innerHTML = `<div class="hint">读取失败：${esc(err.message)}</div>`;
    return;
  }
  if (!d.days || !d.days.length) {
    body.innerHTML = '<div class="hint">该会话暂无可用时间数据</div>';
    return;
  }
  state.calDays = {};
  d.days.forEach(x => { state.calDays[x.d] = x; });
  state.calFirst = d.first || d.days[d.days.length - 1].d;
  state.calLast = d.last || d.days[0].d;
  // 默认定位到最近有记录的月份（与微信「查找聊天记录 → 日期」一致）
  state.calYM = String(state.calLast).slice(0, 7);
  renderDateBody();
}

function renderDateBody() {
  $$('#calView .scope-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.view === state.calView));
  if (state.calView === 'list') renderDayList();
  else renderMonthCalendar();
}

/* 月历：与原生微信的「按日期查找」一致 —— 有记录的日期可点，圆点深浅表示消息量 */
function renderMonthCalendar() {
  const body = $('#dateBody');
  const [ys, ms] = state.calYM.split('-');
  const y = Number(ys), m = Number(ms);
  const pad2 = n => String(n).padStart(2, '0');
  const daysInMonth = new Date(y, m, 0).getDate();
  const lead = new Date(y, m - 1, 1).getDay();
  const todayKey = localDate(new Date());
  const nums = Object.values(state.calDays).map(x => x.n);
  const maxN = Math.max(1, ...nums);

  let cells = '';
  for (let i = 0; i < lead; i++) cells += '<div class="cal-cell empty"></div>';
  let monthTotal = 0;
  for (let d = 1; d <= daysInMonth; d++) {
    const key = `${ys}-${pad2(m)}-${pad2(d)}`;
    const rec = state.calDays[key];
    const cls = ['cal-cell'];
    if (rec) { cls.push('has'); monthTotal += rec.n; }
    if (key === todayKey) cls.push('today');
    const lvl = rec ? Math.min(3, Math.max(1, Math.ceil(rec.n / maxN * 3))) : 0;
    cells += `<div class="${cls.join(' ')}"${rec ? ` data-day="${key}" title="${rec.n} 条"` : ''}`
      + ` data-lvl="${lvl}"><span class="cc-d">${d}</span>`
      + (rec ? `<span class="cc-n">${rec.n}</span>` : '')
      + `<span class="cc-dot"></span></div>`;
  }
  const tail = (7 - (lead + daysInMonth) % 7) % 7;
  for (let i = 0; i < tail; i++) cells += '<div class="cal-cell empty"></div>';

  body.innerHTML =
    `<div class="cal-head">
       <button class="ic-btn sm" id="calPrev" title="上一月">‹</button>
       <b class="cal-ym">${y} 年 ${m} 月</b>
       <button class="ic-btn sm" id="calNext" title="下一月">›</button>
       <span class="cal-month-sum">本月 ${monthTotal} 条</span>
     </div>
     <div class="cal-week">${WEEK_CN.map(w => `<span>${w}</span>`).join('')}</div>
     <div class="cal-grid">${cells}</div>
     <div class="cal-foot">记录范围 ${state.calFirst} ~ ${state.calLast}
       · 圆点越大消息越多 · 点日期跳转</div>`;

  $('#calPrev').onclick = () => shiftMonth(-1);
  $('#calNext').onclick = () => shiftMonth(1);
  $$('.cal-cell.has', body).forEach(el => { el.onclick = () => jumpToDay(el.dataset.day); });
}

function shiftMonth(delta) {
  let [y, m] = state.calYM.split('-').map(Number);
  m += delta;
  if (m < 1) { m = 12; y--; }
  if (m > 12) { m = 1; y++; }
  state.calYM = `${y}-${String(m).padStart(2, '0')}`;
  renderMonthCalendar();
}

/* 列表：逐日条数 + 类型分布条（长跨度快速扫视用） */
function renderDayList() {
  const body = $('#dateBody');
  const days = Object.values(state.calDays).sort((a, b) => a.d < b.d ? -1 : 1);
  const max = Math.max(1, ...days.map(x => x.n));
  body.innerHTML =
    `<div class="cal-sum">${days.length} 天有记录 · ${state.calFirst} ~ ${state.calLast}</div>`
    + '<div class="cal-list">' + days.map(x => {
      const pct = Math.max(4, Math.round(x.n / max * 100));
      const badge = [
        x.img ? `${x.img} 图` : '', x.vid ? `${x.vid} 视频` : '',
        x.fil ? `${x.fil} 文件` : '', x.voi ? `${x.voi} 语音` : '',
      ].filter(Boolean).join(' · ');
      return `<div class="cal-item" data-day="${x.d}">
        <div class="cal-day">${x.d}</div>
        <div class="cal-meta"><span class="cal-n">${x.n} 条</span>${
          badge ? `<span class="cal-badge">${badge}</span>` : ''}</div>
        <div class="cal-bar"><i style="width:${pct}%"></i></div>
      </div>`;
    }).join('') + '</div>';
  $$('.cal-item', body).forEach(el => {
    el.onclick = () => jumpToDay(el.dataset.day);
  });
}

async function jumpToDay(day) {
  if (!state.current) return;
  const loc = await api(
    `/api/locate?conv_id=${encodeURIComponent(state.current)}&date=${day}`);
  if (!loc.found) { alert('该日期没有消息'); return; }
  $('#datePanel').style.display = 'none';
  await renderWindowAt(loc, loc.msg_id);
}

/* 把消息列表切到 locate 结果所在窗口（复用既有 offset 分页语义） */
async function renderWindowAt(loc, mid) {
  const win = state.pageSize;
  const offset = Math.max(0, loc.total - loc.index - win);
  const page = await api(
    `/api/messages?conv_id=${encodeURIComponent(state.current)}` +
    `&offset=${offset}&limit=${win}&order=desc`);
  $('#msgList').innerHTML = '';
  renderMessages(page.items, 'prepend');
  state.msgTotal = page.total;
  state.msgLoaded = offset + page.items.length;
  state.hasMore = page.has_more;
  state.loadingMore = false;
  updateLoadMoreHint();
  updateFindBar();

  const el = mid
    ? $(`.msg[data-mid="${mid}"], .sys-msg[data-mid="${mid}"]`)
    : null;
  if (el) {
    el.scrollIntoView({ block: 'center' });
    el.classList.add('flash');
    setTimeout(() => el.classList.remove('flash'), 1200);
  } else {
    $('#msgScroller').scrollTop = 0;
  }
}

async function jumpToMsg(mid, ts = 0) {
  // 已在列表 → 直接滚动
  let el = $(`.msg[data-mid="${mid}"], .sys-msg[data-mid="${mid}"]`);
  if (!el && ts) {
    // 有精确时间戳 → 直接定位窗口（跨百万条也只需一次查询）
    const loc = await api(
      `/api/locate?conv_id=${encodeURIComponent(state.current)}&ts=${ts}`);
    if (loc.found) return renderWindowAt(loc, mid);
  }
  if (!el) {
    // 兜底：从最早的开始逐页加载直到找到（有上限）
    let tries = 0;
    while (!el && tries < 30) {
      const before = state.msgLoaded;
      await loadMore();
      if (state.msgLoaded === before) break;
      el = $(`.msg[data-mid="${mid}"], .sys-msg[data-mid="${mid}"]`);
      tries++;
    }
  }
  if (!el) return;
  el.scrollIntoView({ block: 'center', behavior: 'smooth' });
  el.classList.add('flash');
  setTimeout(() => el.classList.remove('flash'), 1200);
}

/* ------------------------------ 会话内搜索 ------------------------------ */

function updateFindBar() {
  const bar = $('#findBar');
  if (bar.style.display === 'none') return;
  const q = $('#findInput').value.trim();
  const list = $('#msgList');
  $$('.hit', list).forEach(h => {
    const p = h.parentNode;
    p.replaceChild(document.createTextNode(h.textContent), h);
    p.normalize();
  });
  state.findMatches = []; state.findIdx = -1;
  if (!q) { $('#findCount').textContent = ''; return; }

  const re = new RegExp(escRe(q), 'gi');
  // 只高亮已加载的消息
  $$('.msg .bubble', list).forEach(b => {
    if (b.classList.contains('media')) return;
    const walker = document.createTreeWalker(b, NodeFilter.SHOW_TEXT);
    const targets = [];
    let n;
    while ((n = walker.nextNode())) {
      if (n.parentNode.classList.contains('hit')) continue;
      re.lastIndex = 0;
      if (re.test(n.nodeValue)) targets.push(n);
    }
    targets.forEach(tn => {
      const frag = document.createDocumentFragment();
      let last = 0, s = tn.nodeValue;
      re.lastIndex = 0;
      let m;
      while ((m = re.exec(s))) {
        if (m.index > last) frag.appendChild(document.createTextNode(s.slice(last, m.index)));
        const sp = document.createElement('span');
        sp.className = 'hit'; sp.textContent = m[0];
        frag.appendChild(sp);
        last = m.index + m[0].length;
      }
      if (last < s.length) frag.appendChild(document.createTextNode(s.slice(last)));
      tn.parentNode.replaceChild(frag, tn);
    });
  });
  state.findMatches = $$('.hit', list);
  $('#findCount').textContent = state.findMatches.length ? `0/${state.findMatches.length}` : '0';
  if (state.findMatches.length) { state.findIdx = 0; focusMatch(0); }
}

function focusMatch(i) {
  const ms = state.findMatches;
  if (!ms.length) return;
  state.findIdx = (i + ms.length) % ms.length;
  const el = ms[state.findIdx];
  el.scrollIntoView({ block: 'center', behavior: 'smooth' });
  $('#findCount').textContent = `${state.findIdx + 1}/${ms.length}`;
  ms.forEach(m => m.style.outline = '');
  el.style.outline = '2px solid #07c160';
}

/* ------------------------------ 统计 ------------------------------ */

async function openStats() {
  $('#statsPanel').style.display = 'flex';
  $('#statsBody').innerHTML = '<div class="hint">统计中…</div>';
  const qs = [];
  if (state.current) qs.push(`conv_id=${encodeURIComponent(state.current)}`);
  else if (state.account) qs.push(`account=${encodeURIComponent(state.account)}`);
  const q = qs.length ? `?${qs.join('&')}` : '';
  const d = await api('/api/stats' + q);
  if (!d.total) { $('#statsBody').innerHTML = '<div class="hint">暂无数据</div>'; return; }

  const typeName = { text:'文本', image:'图片', voice:'语音', video:'视频',
    file:'文件', emoji:'表情', link:'链接', system:'系统', other:'其他' };
  const maxType = Math.max(...d.by_type.map(x => x.n), 1);
  const maxHour = Math.max(...d.by_hour.map(x => x.n), 1);
  const maxSender = Math.max(...d.by_sender.map(x => x.n), 1);

  const bars = (arr, labelFn, max) => arr.map(x => `
    <div class="bar-row">
      <div class="bar-label" title="${esc(labelFn(x))}">${esc(labelFn(x))}</div>
      <div class="bar-track"><div class="bar-fill" style="width:${(x.n/max*100).toFixed(1)}%"></div></div>
      <div class="bar-val">${x.n}</div>
    </div>`).join('');

  $('#statsBody').innerHTML = `
    <div class="stat-grid">
      <div class="stat-card"><div class="v">${d.total}</div><div class="l">总消息数</div></div>
      <div class="stat-card"><div class="v">${d.self}</div><div class="l">我发出</div></div>
      <div class="stat-card"><div class="v">${d.other}</div><div class="l">对方发出</div></div>
      <div class="stat-card"><div class="v">${d.by_sender.length}</div><div class="l">参与人</div></div>
    </div>
    <div class="stat-sec">
      <h4>时间跨度</h4>
      <div style="font-size:13px;color:var(--wx-text-2)">${esc(d.start)} → ${esc(d.end)}</div>
    </div>
    <div class="stat-sec">
      <h4>消息类型分布</h4>
      ${bars(d.by_type, x => typeName[x.type] || x.type, maxType)}
    </div>
    <div class="stat-sec">
      <h4>活跃时段（24 小时）</h4>
      ${bars(d.by_hour, x => `${String(x.h).padStart(2,'0')}:00`, maxHour)}
    </div>
    <div class="stat-sec">
      <h4>发言排行</h4>
      ${bars(d.by_sender, x => x.sender, maxSender)}
    </div>`;
}

/* ------------------------------ 导出 ------------------------------ */

function download(url) {
  const a = document.createElement('a');
  a.href = url;
  a.rel = 'noopener';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function openExport(asConv = null) {
  const p = $('#exportPanel');
  p.style.display = 'flex';
  const body = $('#exportBody');
  const scopeName = asConv
    ? (state.convs.find(c => c.conv_id === asConv)?.title || asConv)
    : (state.account || '全部账号');
  const scopeQ = asConv
    ? `conv_id=${encodeURIComponent(asConv)}`
    : (state.account ? `account=${encodeURIComponent(state.account)}` : '');
  const apiPath = asConv ? '/api/export/conv' : '/api/export/all';
  const joiner = scopeQ ? '&' : '';

  const row = (label, desc, fmt, extra = '') => `
    <div class="exp-row" data-fmt="${fmt}" ${extra}>
      <div class="exp-meta">
        <div class="exp-name">${esc(label)}</div>
        <div class="exp-desc">${esc(desc)}</div>
      </div>
      <button class="exp-btn" data-fmt="${fmt}" data-extra="${esc(extra)}">下载</button>
    </div>`;

  body.innerHTML = `
    <div class="exp-scope">导出范围：<b>${esc(scopeName)}</b></div>
    ${row('HTML 网页', '单文件、带样式，浏览器可「打印 → 另存为 PDF」', 'html')}
    ${row('CSV 表格', 'Excel / WPS 直接打开，中文不乱码', 'csv')}
    ${row('纯文本 TXT', '最通用的流水格式，任何编辑器可读', 'txt')}
    <div class="exp-note">
      HTML 默认引用 <code>/media/</code> 下的图片，需在本服务内查看才完整。
      想要彻底离线的单文件（图片转 base64，体积会变大），用下面的按钮。
    </div>
    ${row('HTML（内嵌图片）', '图片转 base64 内嵌，单文件自带全部内容', 'html', 'inline=1')}
    ${scopeQ ? '' : `<div class="exp-scope" style="margin-top:16px">
      另可导出 <a href="/api/export/manifest" download>会话清单 JSON</a>（便于批量处理）。
    </div>`}
  `;

  $$('.exp-btn', body).forEach(b => {
    b.onclick = () => {
      const fmt = b.dataset.fmt;
      const extra = b.dataset.extra ? '&' + b.dataset.extra : '';
      download(`${apiPath}?${scopeQ}${joiner}fmt=${fmt}${extra}`);
      b.textContent = '已开始';
      setTimeout(() => { b.textContent = '下载'; }, 1600);
    };
  });
}

/* ------------------------------ 登录态 ------------------------------ */

async function checkAuth() {
  try {
    const d = await api('/api/auth');
    if (d.enabled) $('#btnLogout').style.display = '';
  } catch (e) {
    // 401 → 会话过期，跳登录
    if (String(e.message).startsWith('401')) location.href = '/login';
  }
}

/* ------------------------------ 归档 / 双轨面板 ------------------------------ */

const fmtBytes = n => {
  if (!n) return '0 B';
  const u = ['B','KB','MB','GB','TB'];
  let i = 0, v = Number(n);
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${u[i]}`;
};

async function openArchive() {
  $('#archivePanel').style.display = 'flex';
  const body = $('#archiveBody');
  body.innerHTML = '<div class="hint">读取中…</div>';
  let st, arch;
  try {
    st = await api('/api/status');
  } catch (e) {
    body.innerHTML = `<div class="hint">读取失败：${esc(e.message)}</div>`;
    return;
  }
  try {
    arch = await api('/api/archive');
  } catch {
    arch = null;
  }

  const trackCard = (title, color, rows) => `
    <div class="arch-card">
      <div class="arch-card-head"><span class="arch-dot" style="background:${color}"></span>${esc(title)}</div>
      ${rows.map(([k, v]) => `<div class="arch-row"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join('')}
    </div>`;

  let html = '';

  // 双轨总览
  html += `<div class="stat-sec"><h4>双轨状态</h4>
    <div class="arch-grid">
      ${trackCard('可读轨（内存库）', '#07c160', [
        ['会话', `${st.conv_count ?? 0}`],
        ['消息', `${st.msg_count ?? 0}`],
        ['账号', `${st.account_count ?? 0}`],
        ['状态', st.loaded_at ? '已载入' : '未载入'],
      ])}
      ${trackCard('存档轨（原始包）', '#f6903d', [
        ['快照代次', `${arch?.snapshots?.length ?? 0}`],
        ['原始文件', `${arch?.file_count ?? 0}`],
        ['占用空间', fmtBytes(arch?.total_bytes ?? 0)],
        ['模式', arch?.enabled ? '已启用' : '未启用'],
      ])}
    </div>
  </div>`;

  // 存档轨快照（按日期分代）
  if (arch?.snapshots?.length) {
    html += `<div class="stat-sec"><h4>存档快照（按日期分代）</h4>
      <div class="arch-list">` + arch.snapshots.map(s => `
        <div class="arch-snap">
          <div class="as-left">
            <div class="as-day">${esc(s.day)}</div>
            <div class="as-sub">${s.n_files} 个文件 · ${fmtBytes(s.bytes)}</div>
          </div>
          <div class="as-badge">${esc(s.kind || '备份包')}</div>
        </div>`).join('') + `</div></div>`;
  }

  // 账号分布
  if (state.accounts.length) {
    html += `<div class="stat-sec"><h4>账号分布</h4>` +
      state.accounts.map(a => `
        <div class="bar-row">
          <div class="bar-label" title="${esc(a.account)}">${esc(a.account)}</div>
          <div class="bar-track"><div class="bar-fill" style="width:${(a.n_msg / Math.max(...state.accounts.map(x => x.n_msg), 1) * 100).toFixed(1)}%"></div></div>
          <div class="bar-val">${a.n_msg}</div>
        </div>`).join('') + `</div>`;
  }

  // 投放口说明
  html += `<div class="stat-sec"><h4>投放目录</h4>
    <div class="arch-note">
      <div><code>inbox/&lt;微信号&gt;/</code> — 可读轨：微信官方导出的 TXT / HTML / CSV / JSON</div>
      <div><code>inbox/raw/</code> — 存档轨：手机备份包（Backup.db、BAK_0_TEXT、BAK_0_MEDIA…）</div>
      <div>放进目录后点下方按钮，或等每天 03:00 自动归档</div>
    </div>
    <button class="arch-btn" id="btnScanNow">立即归档</button>
    <span id="scanMsg" class="arch-scan-msg"></span>
  </div>`;

  body.innerHTML = html;

  const btn = $('#btnScanNow');
  if (btn) btn.onclick = doScan;
}

async function doScan() {
  const btn = $('#btnScanNow');
  const msg = $('#scanMsg');
  btn.disabled = true;
  msg.textContent = '归档中…';
  try {
    const r = await fetch('/api/ingest', { method: 'POST' });
    const d = await r.json();
    msg.textContent = d.ok ? '完成，正在重载…' : '归档失败';
    setTimeout(() => { location.reload(); }, 1200);
  } catch (e) {
    msg.textContent = '失败：' + e.message;
    btn.disabled = false;
  }
}

/* ------------------------------ 界面状态同步（Apple：wayfinding） ------------------------------ */
/* 面板开合时，让工具条上对应的按钮亮起 —— 用户随时知道"我在哪、怎么出去"。
   用 MutationObserver 观察面板的 style 变化，因此无需改动任何既有开合代码。 */
const PANEL_BTN = {
  searchPanel: '#btnGlobalSearch',
  statsPanel: '#btnStats',
  archivePanel: '#btnArchive',
  exportPanel: '#btnExportAll',
};

function syncPanelBtns() {
  for (const [pid, sel] of Object.entries(PANEL_BTN)) {
    const panel = document.getElementById(pid);
    const btn = document.querySelector(sel);
    if (panel && btn) {
      btn.classList.toggle('active', panel.style.display !== 'none');
    }
  }
}

let _panelRaf = 0;
function watchPanels() {
  const appEl = document.getElementById('app');
  if (!appEl || !window.MutationObserver) return;
  new MutationObserver(() => {
    if (_panelRaf) return;
    _panelRaf = requestAnimationFrame(() => { _panelRaf = 0; syncPanelBtns(); });
  }).observe(appEl, { subtree: true, attributes: true, attributeFilter: ['style'] });
  syncPanelBtns();
}

/* 主题色跟随（移动端/PWA 的地址栏与状态栏颜色随主题走） */
function syncThemeColor() {
  const dark = document.documentElement.getAttribute('data-theme') === 'dark';
  let m = document.querySelector('meta[name="theme-color"]');
  if (!m) { m = document.createElement('meta'); m.name = 'theme-color';
            document.head.appendChild(m); }
  m.content = dark ? '#0b0b11' : '#fbfbfd';
}

/* ------------------------------ 事件绑定 ------------------------------ */

function bind() {
  // 账号切换器
  $('#acctSwitcher').onclick = e => {
    e.stopPropagation();
    $('#acctMenu').style.display === 'block' ? closeAcctMenu() : openAcctMenu();
  };
  document.addEventListener('click', e => {
    if (!e.target.closest('#acctSwitcher') && !e.target.closest('#acctMenu')) closeAcctMenu();
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeAcctMenu(); });

  // 会话搜索
  $('#convSearch').oninput = e => {
    state.filter = e.target.value.trim();
    clearTimeout(bind._t);
    bind._t = setTimeout(loadConvs, 200);
  };

  // 排序 tab
  $$('.tab').forEach(t => {
    t.onclick = () => {
      $$('.tab').forEach(x => x.classList.remove('active'));
      t.classList.add('active');
      state.sort = t.dataset.sort;
      loadConvs();
    };
  });

  // 主题
  $('#btnTheme').onclick = () => {
    const cur = document.documentElement.getAttribute('data-theme');
    const next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('wv-theme', next);
    if (typeof syncThemeColor === 'function') syncThemeColor();
  };

  // 查找聊天记录（关键词 + 类型 + 日期）
  $('#btnGlobalSearch').onclick = openSearch;
  $('#gSearchClose').onclick = () => $('#searchPanel').style.display = 'none';
  $('#globalSearchInput').oninput = () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(runFinder, 280);
  };
  $('#globalSearchInput').onkeydown = e => {
    if (e.key === 'Escape') $('#searchPanel').style.display = 'none';
    if (e.key === 'Enter') { clearTimeout(searchTimer); runFinder(); }
  };

  // 范围：全部会话 / 当前会话
  $$('#findScope .scope-btn').forEach(b => {
    b.onclick = () => {
      if (b.disabled) return;
      state.findScope = b.dataset.scope;
      syncFinderUI();
      runFinder();
    };
  });

  // 类型 chips
  $$('#findTypes .chip').forEach(c => {
    c.onclick = () => {
      state.findTypes = c.dataset.type || '';
      syncFinderUI();
      runFinder();
    };
  });

  // 日期区间
  $('#findFrom').onchange = () => { state.findFrom = $('#findFrom').value; runFinder(); };
  $('#findTo').onchange = () => { state.findTo = $('#findTo').value; runFinder(); };
  $$('#findQuick .quick-btn').forEach(q => {
    q.onclick = () => {
      $$('#findQuick .quick-btn').forEach(x => x.classList.remove('active'));
      q.classList.add('active');
      const now = new Date();
      const key = q.dataset.quick;
      if (key === 'today') { state.findFrom = localDate(now); state.findTo = localDate(now); }
      else if (key === '7' || key === '30') {
        const d = new Date(now); d.setDate(d.getDate() - (Number(key) - 1));
        state.findFrom = localDate(d); state.findTo = localDate(now);
      } else if (key === 'year') {
        state.findFrom = `${now.getFullYear()}-01-01`; state.findTo = localDate(now);
      } else { state.findFrom = ''; state.findTo = ''; }
      syncFinderUI();
      runFinder();
    };
  });

  // 会话内「按日期查找」（月历 / 列表）
  $('#btnFindDate').onclick = openDatePicker;
  $('#dateClose').onclick = () => $('#datePanel').style.display = 'none';
  $$('#calView .scope-btn').forEach(b => {
    b.onclick = () => { state.calView = b.dataset.view; renderDateBody(); };
  });
  $('#findOpenCal').onclick = () => {
    if (!state.current) { alert('请先打开一个会话，再按日期查找'); return; }
    $('#searchPanel').style.display = 'none';
    openDatePicker();
  };

  $('#btnStats').onclick = openStats;
  $('#statsClose').onclick = () => $('#statsPanel').style.display = 'none';

  $('#btnArchive').onclick = openArchive;
  $('#archiveClose').onclick = () => $('#archivePanel').style.display = 'none';

  $('#btnExportAll').onclick = () => openExport();
  $('#exportClose').onclick = () => $('#exportPanel').style.display = 'none';
  $('#btnExport').onclick = () => {
    if (!state.current) { alert('请先打开一个会话'); return; }
    openExport(state.current);
  };

  $('#btnLogout').onclick = async () => {
    if (!confirm('确定退出登录？')) return;
    try { await fetch('/api/logout', { method: 'POST' }); } catch {}
    location.href = '/login';
  };

  $('#btnLoadMore').onclick = loadMore;
  $('#btnJumpTop').onclick = scrollToTop;
  $('#btnJumpBottom').onclick = () => scrollToBottom();
  $('#jumpToday').onclick = () => scrollToBottom();
  $('#btnBack').onclick = () => $('#sidebar').classList.remove('hidden');

  // 会话内搜索
  $('#btnFind').onclick = () => {
    const b = $('#findBar');
    const show = b.style.display === 'none';
    b.style.display = show ? 'flex' : 'none';
    if (show) $('#findInput').focus();
    else updateFindBar();
  };
  $('#findClose').onclick = () => {
    $('#findBar').style.display = 'none';
    $('#findInput').value = '';
    updateFindBar();
  };
  $('#findInput').oninput = () => { clearTimeout(bind._f); bind._f = setTimeout(updateFindBar, 200); };
  $('#findInput').onkeydown = e => {
    if (e.key === 'Enter') focusMatch(state.findIdx + (e.shiftKey ? -1 : 1));
  };
  $('#findPrev').onclick = () => focusMatch(state.findIdx - 1);
  $('#findNext').onclick = () => focusMatch(state.findIdx + 1);

  // 滚动监听
  $('#msgScroller').onscroll = onScroll;

  // 图片放大
  document.addEventListener('click', e => {
    const img = e.target.closest('img[data-zoom]');
    if (!img) return;
    let lb = $('#lightbox');
    if (!lb) {
      lb = document.createElement('div');
      lb.id = 'lightbox';
      lb.onclick = () => lb.style.display = 'none';
      document.body.appendChild(lb);
    }
    // 优先加载原图（media_full）；失败或无原图则退回当前缩略图
    const full = img.dataset.full || img.src;
    lb.innerHTML = `<img src="${esc(full)}" alt="">`
      + (img.dataset.full ? '<span class="lb-tag">原图</span>' : '');
    const big = lb.querySelector('img');
    big.onerror = () => { big.onerror = null; big.src = img.src; };
    lb.style.display = 'flex';
  });

  // 快捷键
  document.addEventListener('keydown', e => {
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.key === 'f') {
      if (state.current) { e.preventDefault(); $('#btnFind').click(); }
    }
    if (mod && e.key === 'k') { e.preventDefault(); openSearch(); }
    if (e.key === 'Escape') {
      $('#searchPanel').style.display = 'none';
      $('#statsPanel').style.display = 'none';
      $('#datePanel').style.display = 'none';
      $('#archivePanel').style.display = 'none';
      $('#exportPanel').style.display = 'none';
      const lb = $('#lightbox'); if (lb) lb.style.display = 'none';
    }
  });
}

/* ------------------------------ 启动 ------------------------------ */

async function boot() {
  const saved = localStorage.getItem('wv-theme');
  if (saved) document.documentElement.setAttribute('data-theme', saved);
  syncThemeColor();
  watchPanels();
  state.account = localStorage.getItem('wv-account') || '';

  bind();
  await checkAuth();
  try {
    const st = await api('/api/status');
    $('#sbStatus').textContent =
      `${st.conv_count} 个会话 · ${st.msg_count} 条消息` +
      (st.account_count > 1 ? ` · ${st.account_count} 个账号` : '');

    await loadAccounts();

    if (!st.conv_count) {
      $('#convList').innerHTML =
        '<div class="hint">归档目录里还没有数据<br><br>请把微信导出的聊天记录放进去<br>（左侧「归档」按钮可立即归档）</div>';
      return;
    }
    await loadConvs();
  } catch (err) {
    $('#sbStatus').textContent = '加载失败';
    $('#convList').innerHTML = `<div class="hint">${esc(err.message)}</div>`;
  }
}

document.addEventListener('DOMContentLoaded', boot);
})();


/* ============================ 微信运行时卡片 ============================ */
(function () {
  let pollTimer = null;

  function $(sel) { return document.querySelector(sel); }

  // 独立 fetch 封装：本 IIFE 与主逻辑隔离，无法访问其闭包内的 api()，
  // 否则 refresh() 里 `api(...)` 会抛 ReferenceError 被静默吞掉、卡片永不显示
  async function wvApi(path, opts = {}) {
    const r = await fetch(path, opts);
    if (r.status === 401) { location.href = '/login'; throw new Error('401'); }
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    return r.json();
  }

  function renderCard(s) {
    const card = $('#wechatCard');
    if (!card) return;
    card.style.display = 'flex';

    const txt = $('#wechatStatusText');
    const bar = $('#wechatBar');
    const prog = $('#wechatProgress');
    const btnInstall = $('#btnWechatInstall');
    const btnDesktop = $('#btnWechatDesktop');

    if (s.phase === 'downloading' || s.phase === 'extracting' || s.phase === 'installing') {
      txt.textContent = '微信：' + (s.message || '安装中…');
      prog.style.display = 'block';
      bar.style.width = (s.percent || 0) + '%';
      btnInstall.style.display = 'none';
      btnDesktop.style.display = 'none';
      if (!pollTimer) pollTimer = setInterval(refresh, 2000);
    } else if (s.installed) {
      txt.textContent = '微信运行时：已就绪' + (s.version ? '（' + s.version + '）' : '');
      prog.style.display = 'none';
      btnInstall.style.display = 'none';
      btnDesktop.style.display = 'inline-block';
      clearInterval(pollTimer); pollTimer = null;
    } else if (s.phase === 'error') {
      txt.textContent = '微信安装失败：' + (s.message || '未知错误');
      prog.style.display = 'none';
      btnInstall.style.display = 'inline-block';
      btnInstall.textContent = '重试安装';
      clearInterval(pollTimer); pollTimer = null;
    } else {
      txt.textContent = '微信运行时：未安装';
      prog.style.display = 'none';
      btnInstall.style.display = 'inline-block';
      btnInstall.textContent = '安装微信';
      clearInterval(pollTimer); pollTimer = null;
    }
  }

  async function refresh() {
    try {
      const s = await wvApi('/api/wechat/status');
      renderCard(s);
    } catch (e) { /* 未登录等场景静默 */ }
  }

  async function install() {
    const btn = $('#btnWechatInstall');
    btn.disabled = true;
    try {
      await wvApi('/api/wechat/install', { method: 'POST' });
      $('#wechatStatusText').textContent = '微信：开始下载…';
      if (!pollTimer) pollTimer = setInterval(refresh, 2000);
    } catch (e) {
      alert('安装失败：' + e.message);
    } finally {
      btn.disabled = false;
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    const btn = $('#btnWechatInstall');
    if (btn) btn.addEventListener('click', install);
    setTimeout(refresh, 600);
  });
})();
