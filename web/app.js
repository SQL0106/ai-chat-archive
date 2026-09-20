/* AI 聊天归档 前端 */
'use strict';

let TZ_OFFSET = 8;

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const SRC_LABEL = { chatgpt: 'ChatGPT', gemini: 'Gemini', deepseek: 'DeepSeek' };
const srcLabel = (s) => SRC_LABEL[s] || (s ? String(s) : '未知');
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

/* ---------------- 时间 ---------------- */
function tzLabel() {
  const sign = TZ_OFFSET >= 0 ? '+' : '-';
  const a = Math.abs(TZ_OFFSET);
  const h = Math.floor(a), m = Math.round((a - h) * 60);
  return 'UTC' + sign + h + (m ? ':' + String(m).padStart(2, '0') : '');
}
function fmtTs(iso, withSec) {
  if (!iso) return '无时间';
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const t = new Date(d.getTime() + TZ_OFFSET * 3600 * 1000);
  const p = (n) => String(n).padStart(2, '0');
  let s = t.getUTCFullYear() + '-' + p(t.getUTCMonth() + 1) + '-' + p(t.getUTCDate()) +
          ' ' + p(t.getUTCHours()) + ':' + p(t.getUTCMinutes());
  if (withSec !== false) s += ':' + p(t.getUTCSeconds());
  return s;
}
const fmtDate = (iso) => (iso ? fmtTs(iso, false).slice(0, 10) : '—');

function relTime(iso) {
  if (!iso) return '';
  const days = Math.round((Date.now() - new Date(iso).getTime()) / 86400000);
  if (days <= 0) return '今天';
  if (days === 1) return '昨天';
  if (days < 30) return days + ' 天前';
  if (days < 365) return Math.round(days / 30) + ' 个月前';
  return Math.round(days / 365) + ' 年前';
}

const fmtNum = (n) => (n == null ? '—' : Number(n).toLocaleString());
const fmtSize = (n) => {
  if (!n) return '0 字';
  if (n < 10000) return n + ' 字';
  return (n / 10000).toFixed(1) + ' 万字';
};
/* 单位不跟在数字后面，而是并进标签里，显示成「项目（单位）」 */
const sizeUnit = (n) => (Number(n) >= 10000 ? '万字' : '字');
const sizeNum = (n) => {
  const v = Number(n) || 0;
  return v >= 10000 ? (v / 10000).toFixed(1) : fmtNum(v);
};

/* 阅读器的滚动容器：桌面端滚消息区；窄屏顶栏不钉死，改由整个阅读器滚动。
   断点必须和 style.css 里 @media (max-width: 820px) 保持一致。 */
const MOBILE_READER = window.matchMedia('(max-width: 820px)');
const readerScroller = () => (MOBILE_READER.matches ? $('#reader') : $('#readerBody'));

/* ---------------- 请求 ---------------- */
async function api(path, params) {
  const u = new URL(path, location.origin);
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v !== '' && v !== null && v !== undefined) u.searchParams.set(k, v);
  });
  const r = await fetch(u);
  if (!r.ok) {
    let msg = 'HTTP ' + r.status;
    try { const j = await r.json(); if (j.error) msg = j.error; } catch (e) { /* ignore */ }
    throw new Error(msg);
  }
  return r.json();
}

let toastTimer = null;
function toast(msg, ms) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add('hidden'), ms || 3000);
}

const debounce = (fn, ms) => {
  let h = null;
  return (...a) => { clearTimeout(h); h = setTimeout(() => fn(...a), ms); };
};

function safeUrl(u) {
  return /^https?:\/\//i.test(u || '') ? u : '#';
}

/* ---------------- 富文本（安全） ---------------- */
const escHtml = (s) => String(s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

function renderRich(text) {
  const parts = String(text || '').split('```');
  let html = '';
  parts.forEach((p, i) => {
    if (i % 2 === 1) {
      const code = p.replace(/^[a-zA-Z0-9_+#-]*\r?\n/, '');
      html += '<pre class="code"><code>' + escHtml(code) + '</code></pre>';
    } else {
      html += escHtml(p).replace(/`([^`\n]+)`/g, '<code>$1</code>');
    }
  });
  return html;
}

/* ---------------- 关键词高亮 ---------------- */
function queryTerms(q) {
  if (!q) return [];
  const out = [];
  const re = /"([^"]+)"|(\S+)/g;
  let m;
  while ((m = re.exec(q))) {
    if (m[1]) { out.push(m[1]); continue; }
    let w = m[2].replace(/^\w+:/, '').replace(/^[-+]+/, '').replace(/\*+$/, '');
    if (!w || /^(AND|OR|NOT)$/i.test(w)) continue;
    out.push(w);
  }
  const seen = new Set();
  return out.filter((t) => {
    const k = t.toLowerCase();
    if (!t || seen.has(k)) return false;
    seen.add(k); return true;
  });
}

const escRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

function highlightIn(root, terms) {
  const marks = [];
  if (!terms || !terms.length) return marks;
  const re = new RegExp('(' + terms.map(escRe).join('|') + ')', 'gi');
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  nodes.forEach((node) => {
    const text = node.nodeValue;
    if (!text || !text.trim()) return;
    re.lastIndex = 0;
    if (!re.test(text)) return;
    re.lastIndex = 0;
    const frag = document.createDocumentFragment();
    let last = 0, m;
    while ((m = re.exec(text)) !== null) {
      if (!m[0]) { re.lastIndex++; continue; }
      if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
      const mk = document.createElement('mark');
      mk.textContent = m[0];
      frag.appendChild(mk);
      marks.push(mk);
      last = m.index + m[0].length;
    }
    if (!last) return;
    if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
    node.parentNode.replaceChild(frag, node);
  });
  return marks;
}

/* ---------------- 图表 ---------------- */
function renderBars(box, items, opt) {
  opt = opt || {};
  box.innerHTML = '';
  if (!items.length) { box.appendChild(el('div', 'empty', '暂无数据')); return; }
  const max = opt.max || Math.max(...items.map((i) => i.n)) || 1;
  items.forEach((it) => {
    const b = el('div', 'bar');
    b.style.height = Math.max(2, (it.n / max) * 100) + '%';
    if (opt.minWidth) b.style.minWidth = opt.minWidth;
    b.innerHTML = '<div class="tip">' + escHtml(opt.tip ? opt.tip(it) : (it.k + ' · ' + it.n)) + '</div>' +
                  '<div class="lbl">' + escHtml(opt.label ? opt.label(it) : it.k) + '</div>';
    if (opt.onClick) { b.onclick = () => opt.onClick(it); b.style.cursor = 'pointer'; }
    box.appendChild(b);
  });
}

function renderHBars(box, items, opt) {
  opt = opt || {};
  box.innerHTML = '';
  if (!items.length) { box.appendChild(el('div', 'empty', '暂无数据')); return; }
  const max = Math.max(...items.map((i) => i.n)) || 1;
  items.forEach((it) => {
    const row = el('div', 'hbar');
    row.appendChild(el('div', 'name', opt.label ? opt.label(it) : it.k));
    const track = el('div', 'track');
    const fill = el('div', 'fill');
    fill.style.width = Math.max(1, (it.n / max) * 100) + '%';
    if (opt.color) fill.style.background = opt.color(it);
    track.appendChild(fill);
    row.appendChild(track);
    row.appendChild(el('div', 'num', String(it.n)));
    box.appendChild(row);
  });
}

/* 时间序列：SVG 柱状图，带 Y 轴刻度、按月 X 轴标签、悬浮提示、点击下钻 */
function niceScale(v) {
  if (!(v > 0)) v = 1;
  const raw = v / 4;
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = ([1, 2, 2.5, 5, 10].find((x) => x * p >= raw) || 10) * p;
  const top = Math.ceil(v / step) * step;
  const ticks = [];
  for (let t = 0; t <= top + step * 1e-6; t += step) ticks.push(t);
  return { top, ticks };
}
const fmtAxis = (v) => (v >= 10000 ? Math.round(v / 1000) + 'k'
  : v >= 1000 ? (v / 1000).toFixed(1).replace(/\.0$/, '') + 'k' : String(Math.round(v)));

function renderTimeSeries(box, items, opt) {
  opt = opt || {};
  box.innerHTML = '';
  if (!items.length) { box.appendChild(el('div', 'empty', '暂无数据')); return; }

  // 移动端切标签页时容器可能还没完成布局（clientWidth 为 0），
  // 那样算出来的 viewBox 会远大于实际显示宽度，整个 SVG 被等比缩小，
  // X 轴标签就糊成一团认不出来。所以先量宽度，量不到就等下一帧重来。
  const cw = box.clientWidth;
  if (cw < 200) {
    const tries = (opt._tries || 0) + 1;
    if (tries <= 10) {
      requestAnimationFrame(() => renderTimeSeries(box, items, Object.assign({}, opt, { _tries: tries })));
      return;
    }
  }
  // viewBox 宽度 = 实际显示宽度 → 缩放比 1:1，字号就是 CSS 里的字号，不会被缩小。
  const W = cw >= 200 ? cw : 360;
  const narrow = W < 460;
  const H = 230, padL = narrow ? 32 : 48, padR = narrow ? 8 : 16, padT = 16, padB = 42;
  const iw = W - padL - padR, ih = H - padT - padB;
  const n = items.length;
  const sc = niceScale(Math.max(...items.map((i) => i.n)) || 1);
  const max = sc.top;
  const step = iw / n;
  const bw = Math.max(1, Math.min(step * (step > 8 ? 0.7 : 0.92), 30));
  const yOf = (v) => padT + ih - (v / max) * ih;
  const xOf = (i) => padL + step * (i + 0.5);

  const NS = 'http://www.w3.org/2000/svg';
  const mk = (t, a) => {
    const e = document.createElementNS(NS, t);
    for (const k in a) e.setAttribute(k, a[k]);
    return e;
  };
  const svg = mk('svg', { viewBox: '0 0 ' + W + ' ' + H, class: 'tsvg' });

  sc.ticks.forEach((v, t) => {
    const y = yOf(v);
    svg.appendChild(mk('line', { x1: padL, x2: W - padR, y1: y, y2: y, class: t ? 'grid' : 'axis' }));
    const lab = mk('text', { x: padL - 8, y: y + 3.5, class: 'ylab' });
    lab.textContent = fmtAxis(v);
    svg.appendChild(lab);
  });

  const bars = [];
  let lastLabX = -Infinity;
  items.forEach((it, i) => {
    const h = Math.max(it.n > 0 ? 1.5 : 0, (it.n / max) * ih);
    const r = mk('rect', {
      x: xOf(i) - bw / 2, y: padT + ih - h, width: bw, height: h,
      rx: bw > 5 ? 2 : 0, class: 'col',
    });
    bars.push(r);
    svg.appendChild(r);
    if (opt.xlabel) {
      const lb = opt.xlabel(it, i, items);
      const x = xOf(i);
      const gap = Math.max(opt.minGap || 24, lb ? lb.length * (narrow ? 7 : 6) + 10 : 0);
      if (lb && x - lastLabX >= gap) {
        const txt = mk('text', { x, y: padT + ih + 16, class: 'xlab' });
        txt.textContent = lb;
        svg.appendChild(txt);
        lastLabX = x;
      }
    }
  });

  const tip = el('div', 'ts-tip');
  box.appendChild(tip);
  const show = (i) => {
    const it = items[i];
    tip.textContent = opt.tip ? opt.tip(it) : it.k + ' · ' + fmtNum(it.n);
    tip.style.left = (xOf(i) / W) * 100 + '%';
    tip.style.top = (yOf(it.n) / H) * 100 + '%';
    tip.style.display = 'block';
    bars.forEach((b, j) => b.classList.toggle('hit', j === i));
  };
  const hide = () => { tip.style.display = 'none'; bars.forEach((b) => b.classList.remove('hit')); };

  items.forEach((it, i) => {
    const hit = mk('rect', { x: padL + step * i, y: padT, width: step, height: ih, class: 'hitrect' });
    hit.addEventListener('mouseenter', () => show(i));
    hit.addEventListener('mouseleave', hide);
    if (opt.onClick) {
      hit.style.cursor = 'pointer';
      hit.addEventListener('click', () => opt.onClick(it));
    }
    svg.appendChild(hit);
  });

  box.appendChild(svg);
}

/* ---------------- 统计 ---------------- */
const WEEKDAYS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
const SRC_COLOR = { chatgpt: '#6ee7b7', gemini: '#4c9aff', deepseek: '#a78bfa' };

/* 可折叠分区：展开状态存 localStorage，下次打开保持原样 */
function setupFolds() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem('statsFolds') || '{}'); } catch (e) { saved = {}; }
  $$('.fold').forEach((f) => {
    const name = f.dataset.fold;
    f.classList.toggle('open', saved[name] !== false);
    const h = f.querySelector('.fold-h');
    if (!h) return;
    h.onclick = () => {
      const open = f.classList.toggle('open');
      saved[name] = open;
      try { localStorage.setItem('statsFolds', JSON.stringify(saved)); } catch (e) { /* 隐私模式忽略 */ }
    };
  });
}

function setFold(name, text) {
  const x = $('#foldX-' + name);
  if (x) x.textContent = text;
}

/* 星期 × 小时热力图：按分位数分 5 档上色 */
function renderHeatmap(box, grid) {
  box.innerHTML = '';
  const sorted = grid.flat().slice().sort((a, b) => a - b);
  const q = (p) => sorted[Math.floor(sorted.length * p)] || 0;
  const cuts = [q(0.2), q(0.4), q(0.6), q(0.8)];
  const lv = (n) => (!n ? 0 : n <= cuts[0] ? 1 : n <= cuts[1] ? 2 : n <= cuts[2] ? 3 : 4);
  grid.forEach((row, w) => {
    box.appendChild(el('div', 'hl', WEEKDAYS[w]));
    row.forEach((n, h) => {
      const c = el('div', 'hc heat-' + lv(n));
      c.title = WEEKDAYS[w] + ' ' + h + ':00 · ' + fmtNum(n) + ' 条';
      box.appendChild(c);
    });
  });
}

function kvBox(box, rows) {
  box.innerHTML = '';
  rows.forEach((r) => {
    const d = el('div', 'r');
    d.appendChild(el('div', 'n', String(r.n)));
    d.appendChild(el('div', 't', r.t));
    if (r.l) d.appendChild(el('div', 'l', r.l));
    box.appendChild(d);
  });
}

async function loadStats() {
  const d = await api('/api/stats');
  TZ_OFFSET = d.tz_offset;
  const t = d.totals;
  const pct = (a, b) => (b ? Math.round((a / b) * 100) : 0);

  /* ---------- 概览 ---------- */
  const cards = [
    { v: fmtNum(t.conversations), k: '对话总数（个）' },
    { v: fmtNum(t.messages), k: '消息总数（条）' },
    { v: fmtNum(t.user_messages), k: '我发出的（条）' },
    { v: fmtNum(t.assistant_messages), k: 'AI 回复的（条）' },
    { v: sizeNum(t.chars), k: '正文总量（' + sizeUnit(t.chars) + '）' },
    { v: sizeNum(t.thinking_chars), k: '思维链（' + sizeUnit(t.thinking_chars) + '）' },
    { v: fmtNum(t.active_days), k: '活跃天数（天）' },
    { v: fmtNum(t.span_days), k: '跨度（天）' },
  ];
  const cbox = $('#statCards');
  cbox.innerHTML = '';
  cards.forEach((c) => {
    const s = el('div', 'stat');
    s.appendChild(el('div', 'v', String(c.v)));
    s.appendChild(el('div', 'k', c.k));
    cbox.appendChild(s);
  });
  const range = el('div', 'stat');
  range.appendChild(el('div', 'v small', fmtDate(d.range.min) + ' →'));
  range.appendChild(el('div', 'v small', fmtDate(d.range.max)));
  range.appendChild(el('div', 'k', '时间范围 · ' + tzLabel()));
  cbox.appendChild(range);
  if (d.busiest_day) {
    const b = el('div', 'stat');
    b.appendChild(el('div', 'v', fmtNum(d.busiest_day.n)));
    b.appendChild(el('div', 'k', '最忙一天（条） · ' + d.busiest_day.k));
    cbox.appendChild(b);
  }

  /* ---------- 时间分布 ---------- */
  renderHBars($('#chartYear'), d.by_year.map((y) => ({ k: y.k, n: y.n })), { color: () => '#4c9aff' });
  renderBars($('#chartMonth'), d.by_month, {
    label: (i) => i.k.slice(2),
    tip: (i) => i.k + ' · ' + i.n + ' 条 / ' + i.c + ' 对话',
  });
  renderBars($('#chartHour'), d.by_hour, {
    label: (i) => i.k,
    tip: (i) => i.k + ':00 · ' + i.n + ' 条',
  });
  renderBars($('#chartWeekday'), d.by_weekday, {
    label: (i) => WEEKDAYS[+i.k].slice(1),
    tip: (i) => WEEKDAYS[+i.k] + ' · ' + i.n + ' 条',
  });
  renderHeatmap($('#heatmap'), d.heatmap);
  setFold('time', fmtNum(t.messages) + ' 条 · ' + d.by_month.length + ' 个月 · 最忙 '
    + (d.busiest_month ? d.busiest_month.k : '—'));

  /* ---------- 活跃度 ---------- */
  const abox = $('#activeCards');
  abox.innerHTML = '';
  [
    { v: t.avg_per_day, k: '日均消息（条）' },
    { v: t.avg_per_active_day, k: '活跃日均（条）' },
    { v: t.avg_per_conversation, k: '平均每对话（条）' },
    { v: t.avg_chars_message, k: '平均每条（字）' },
    { v: t.avg_chars_user, k: '我平均（字）' },
    { v: t.avg_chars_assistant, k: 'AI 平均（字）' },
    { v: t.convs_per_active_day, k: '活跃日对话（个）' },
    { v: pct(t.active_days, t.span_days), k: '活跃率（%）' },
  ].forEach((c) => {
    const s = el('div', 'stat');
    s.appendChild(el('div', 'v', String(c.v)));
    s.appendChild(el('div', 'k', c.k));
    abox.appendChild(s);
  });
  kvBox($('#streakBox'), [
    { n: fmtNum(t.max_streak), t: '最长连续（天）', l: '不断更' },
    { n: fmtNum(t.last_streak), t: '最近连续（天）', l: '到 ' + fmtDate(d.range.max) },
    { n: fmtNum(t.active_days) + ' / ' + fmtNum(t.span_days), t: '活跃 / 跨度（天）', l: '占 ' + pct(t.active_days, t.span_days) + '%' },
  ]);
  const bw = d.busiest_weekday;
  kvBox($('#busyBox'), [
    { n: d.busiest_day ? fmtNum(d.busiest_day.n) : '—', t: '最忙一天（条）', l: d.busiest_day ? d.busiest_day.k : '' },
    { n: d.busiest_month ? fmtNum(d.busiest_month.n) : '—', t: '最忙一月（条）', l: d.busiest_month ? d.busiest_month.k + ' · ' + d.busiest_month.c + ' 对话' : '' },
    { n: d.busiest_hour ? fmtNum(d.busiest_hour.n) : '—', t: '最忙时段（条）', l: d.busiest_hour ? d.busiest_hour.k + ':00' : '' },
    { n: bw ? fmtNum(bw.n) : '—', t: '最忙星期（条）', l: bw ? WEEKDAYS[+bw.k] : '' },
  ]);
  setFold('active', '活跃 ' + fmtNum(t.active_days) + ' 天 · 最长连续 ' + fmtNum(t.max_streak) + ' 天');

  /* ---------- 内容构成 ---------- */
  renderHBars($('#chartRoleChars'), [
    { k: '我', n: t.user_chars, c: '#6ee7b7' },
    { k: 'AI', n: t.assistant_chars, c: '#4c9aff' },
  ], { color: (i) => i.c });
  renderHBars($('#chartModel'), d.models.slice(0, 8), { color: () => '#a78bfa' });
  const lm = d.longest_message;
  const lbox = $('#longestMsg');
  lbox.innerHTML = '';
  if (lm) {
    const meta = el('div', 'meta');
    meta.innerHTML = '<b>' + fmtNum(lm.n) + '</b> · ' + (lm.role === 'user' ? '我' : 'AI')
      + ' · ' + srcLabel(lm.source) + ' · 第 ' + (lm.message_index + 1) + ' 条';
    lbox.appendChild(meta);
    lbox.appendChild(el('div', 'body', lm.s));
    lbox.style.cursor = 'pointer';
    lbox.title = '点击打开这个对话';
    lbox.onclick = () => openConversation(lm.conversation_id);
  } else {
    lbox.appendChild(el('div', 'empty', '暂无'));
  }
  setFold('content', fmtSize(t.chars) + ' · 思维链 ' + fmtSize(t.thinking_chars)
    + '（' + fmtNum(t.thinking_messages) + ' 条）');

  /* ---------- 长度分布 ---------- */
  renderBars($('#chartConvHist'), d.conv_hist, {
    label: (i) => i.k,
    tip: (i) => i.k + ' 条消息 · ' + i.n + ' 个对话',
  });
  renderBars($('#chartMsgHist'), d.msg_hist, {
    label: (i) => i.k,
    tip: (i) => i.k + ' 字符 · ' + i.n + ' 条消息',
  });
  setFold('length', '平均每对话 ' + t.avg_per_conversation + ' 条 · 平均每条 ' + t.avg_chars_message + ' 字符');

  /* ---------- 来源 ---------- */
  renderHBars($('#chartSource'), d.sources.map((s) => ({ k: srcLabel(s.source), n: s.messages, s: s.source })),
    { color: (i) => SRC_COLOR[i.s] || '#6ee7b7' });
  renderHBars($('#chartTsSrc'),
    Object.entries(d.timestamp_sources).map(([k, n]) => ({ k, n })),
    { color: () => '#f0b429' });
  setFold('source', d.sources.map((s) => srcLabel(s.source) + ' ' + fmtNum(s.messages)).join(' · '));

  /* ---------- 榜单 ---------- */
  const mini = (box, rows, extra) => {
    box.innerHTML = '';
    if (!rows.length) { box.appendChild(el('div', 'empty', '暂无')); return; }
    rows.forEach((c) => {
      const r = el('div', 'mini-row');
      r.appendChild(el('div', 'mt', c.title || c.conversation_id));
      r.appendChild(el('div', 'mn', extra ? extra(c) : (c.message_count || 0) + ' 条 · ' + fmtDate(c.updated_at)));
      r.onclick = () => openConversation(c.conversation_id);
      box.appendChild(r);
    });
  };
  mini($('#tableLongest'), d.longest_conversations, (c) => fmtNum(c.chars));
  mini($('#tableTop'), d.top_conversations);
  mini($('#tableSpan'), d.span_conversations, (c) => {
    const a = Date.parse(c.created_at);
    const b = Date.parse(c.updated_at);
    const days = (isNaN(a) || isNaN(b)) ? '?' : Math.max(1, Math.round((b - a) / 86400000));
    return days + ' 天 · ' + (c.message_count || 0) + ' 条';
  });
  mini($('#tableRecent'), d.recent_conversations);
  setFold('tables', '最长 ' + (d.longest_conversations[0] ? fmtSize(d.longest_conversations[0].chars) : '—'));
}

/* ---------------- 浏览 ---------------- */
const browse = { q: '', source: '', from: '', to: '', sort: 'updated', offset: 0, limit: 50, total: 0, busy: false };

function convRow(c) {
  const n = el('div', 'conv');
  n.dataset.id = c.conversation_id;
  const t = el('div', 't');
  t.appendChild(el('span', 'src ' + c.source, srcLabel(c.source)));
  t.appendChild(el('span', 'title', c.title || c.conversation_id));
  t.appendChild(el('span', 'n', (c.message_count || 0) + ' 条'));
  n.appendChild(t);
  const m = el('div', 'meta');
  m.appendChild(el('span', null, fmtTs(c.updated_at, false) + ' · ' + relTime(c.updated_at)));
  if (c.created_at && c.created_at !== c.updated_at) {
    m.appendChild(el('span', null, '创建于 ' + fmtDate(c.created_at)));
  }
  n.appendChild(m);
  if (c.preview) n.appendChild(el('div', 'prev', c.preview));
  const on = selHas(c.conversation_id);
  const star = el('button', 'conv-star' + (on ? ' on' : ''), on ? '★' : '☆');
  star.title = on ? '移出选集' : '加入选集';
  star.onclick = (ev) => {
    ev.stopPropagation();
    toggleStar(c.conversation_id).catch((e) => toast('选集失败: ' + e.message));
  };
  n.appendChild(star);
  n.onclick = () => openConversation(c.conversation_id);
  return n;
}

async function loadBrowse(more) {
  if (browse.busy) return;
  browse.busy = true;
  if (!more) browse.offset = 0;
  try {
    const d = await api('/api/conversations', {
      q: browse.q, source: browse.source, from: browse.from, to: browse.to,
      sort: browse.sort, offset: browse.offset, limit: browse.limit,
    });
    const box = $('#browseList');
    if (!more) box.innerHTML = '';
    browse.total = d.total;
    if (!d.items.length && !more) box.appendChild(el('div', 'empty', '没有匹配的对话'));
    d.items.forEach((c) => box.appendChild(convRow(c)));
    browse.offset = d.offset + d.items.length;
    $('#browseCount').textContent = '共 ' + fmtNum(d.total) + ' 个对话 · 已显示 ' + browse.offset;
    const btn = $('#browseMore');
    if (browse.offset >= d.total) {
      btn.disabled = true;
      btn.textContent = d.total ? '已全部加载' : '无结果';
    } else {
      btn.disabled = false;
      btn.textContent = '加载更多（还有 ' + fmtNum(d.total - browse.offset) + ' 个）';
    }
    markActiveConv();
    refreshRowStars();
  } finally {
    browse.busy = false;
  }
}

function markActiveConv() {
  const id = currentConv && currentConv.conversation_id;
  $$('#browseList .conv').forEach((n) => n.classList.toggle('active', !!id && n.dataset.id === id));
}

function applyBrowse() {
  browse.q = $('#browseQ').value.trim();
  browse.source = $('#browseSource').value;
  browse.from = $('#browseFrom').value;
  browse.to = $('#browseTo').value;
  browse.sort = $('#browseSort').value;
  loadBrowse(false).catch((e) => toast('加载失败: ' + e.message));
}

/* ---------------- 搜索 ---------------- */
const search = { q: '', role: '', from: '', to: '', offset: 0, limit: 30 };

async function loadSearch() {
  const d = await api('/api/search', search);
  const box = $('#searchList');
  box.innerHTML = '';
  if (d.error) { box.appendChild(el('div', 'empty', d.error)); }
  else if (!d.items.length) { box.appendChild(el('div', 'empty', '没有找到结果')); }
  d.items.forEach((h) => {
    const n = el('div', 'hit');
    const top = el('div', 'h-top');
    top.appendChild(el('span', 'role-pill ' + h.role, h.role === 'user' ? '我' : 'AI'));
    top.appendChild(el('span', 'h-title', h.title || h.conversation_id));
    top.appendChild(el('span', 'src ' + h.source, srcLabel(h.source)));
    top.appendChild(el('span', null, fmtTs(h.timestamp)));
    n.appendChild(top);
    const body = el('div', 'h-body');
    body.innerHTML = h.snip || '';
    n.appendChild(body);
    n.onclick = () => openConversation(h.conversation_id, { index: h.message_index, query: search.q });
    box.appendChild(n);
  });
  $('#searchCount').textContent = '共 ' + fmtNum(d.total || 0) + ' 条命中';
  const page = Math.floor(d.offset / d.limit) + 1;
  const pages = Math.max(1, Math.ceil((d.total || 0) / d.limit));
  $('#searchPage').textContent = page + ' / ' + pages;
  $('#searchPrev').disabled = d.offset <= 0;
  $('#searchNext').disabled = d.offset + d.limit >= (d.total || 0);
}

function applySearch(reset) {
  search.q = $('#searchQ').value.trim();
  search.role = $('#searchRole').value;
  search.from = $('#searchFrom').value;
  search.to = $('#searchTo').value;
  if (reset) search.offset = 0;
  if (!search.q) { toast('请输入搜索词'); return; }
  loadSearch().catch((e) => toast('搜索失败: ' + e.message));
}

/* ---------------- 时间线 ---------------- */
const tl = { bucket: 'day', from: '', to: '' };

async function loadTimeline() {
  const d = await api('/api/timeline', tl);
  const total = d.items.reduce((a, b) => a + b.n, 0);
  $('#tlCount').textContent = d.items.length + ' 个' + (tl.bucket === 'day' ? '天' : '月') +
    ' · ' + fmtNum(total) + ' 条';

  let items = d.items;
  let xlabel;
  if (tl.bucket === 'day') {
    items = fillDays(items);
    let prev = '';
    xlabel = (it) => {
      const m = it.k.slice(0, 7);
      if (m === prev) return null;
      prev = m;
      return m.slice(2);
    };
  } else {
    xlabel = (it) => it.k.slice(2);
  }

  renderTimeSeries($('#tlChart'), items, {
    xlabel,
    tip: (i) => i.k + ' · ' + fmtNum(i.n) + ' 条 / ' + fmtNum(i.c) + ' 个对话',
    onClick: (i) => {
      if (tl.bucket === 'day') loadDay(i.k);
      else { $('#tlFrom').value = i.k + '-01'; $('#tlTo').value = i.k + '-31'; applyTimeline(); }
    },
  });
}

/* 把缺失的日期补成 0，横轴才是真实时间轴 */
function fillDays(items) {
  if (!items.length) return items;
  const m = new Map(items.map((i) => [i.k, i]));
  const out = [];
  const d = new Date(items[0].k + 'T00:00:00Z');
  const end = new Date(items[items.length - 1].k + 'T00:00:00Z');
  while (d <= end) {
    const k = d.toISOString().slice(0, 10);
    out.push(m.get(k) || { k, n: 0, c: 0 });
    d.setUTCDate(d.getUTCDate() + 1);
  }
  return out;
}

async function loadDay(date) {
  const d = await api('/api/day', { date });
  const box = $('#tlDayList');
  box.innerHTML = '';
  const head = el('div', 'count', date + ' · ' + fmtNum(d.messages) + ' 条消息 / ' + fmtNum(d.conversations) + ' 个对话');
  head.style.marginBottom = '8px';
  head.style.gridColumn = '1 / -1';
  box.appendChild(head);
  if (!d.items.length) { box.appendChild(el('div', 'empty', '这天没有记录')); return; }
  d.items.forEach((c) => {
    const n = el('div', 'conv');
    n.dataset.id = c.conversation_id;
    const t = el('div', 't');
    t.appendChild(el('span', 'src ' + c.source, srcLabel(c.source)));
    t.appendChild(el('span', 'title', c.title || c.conversation_id));
    t.appendChild(el('span', 'n', c.hits + ' 条'));
    n.appendChild(t);
    const m = el('div', 'meta');
    m.appendChild(el('span', null, fmtTs(c.first_ts) + ' → ' + fmtTs(c.last_ts)));
    n.appendChild(m);
    n.onclick = () => openConversation(c.conversation_id);
    box.appendChild(n);
  });
}

function applyTimeline() {
  tl.bucket = $('#tlBucket').value;
  tl.from = $('#tlFrom').value;
  tl.to = $('#tlTo').value;
  $('#tlDayList').innerHTML = '';
  loadTimeline().catch((e) => toast('加载失败: ' + e.message));
}

/* ---------------- 选集 ---------------- */
const sel = { collection: 'default', items: [], busy: false };

function selHas(id) {
  return !!id && sel.items.some((c) => c.conversation_id === id);
}

async function postSelection(conversationId, action) {
  const r = await fetch('/api/selection', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ conversation_id: conversationId, action, collection: sel.collection }),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok || d.error) throw new Error(d.error || ('HTTP ' + r.status));
  return d;
}

async function loadSelection() {
  if (sel.busy) return;
  sel.busy = true;
  try {
    const d = await api('/api/selection', { collection: sel.collection });
    sel.items = d.items || [];
    renderSelection(d);
    refreshStar();
    refreshRowStars();
  } finally {
    sel.busy = false;
  }
}

function renderSelection(d) {
  const box = $('#selList');
  box.innerHTML = '';
  $('#selCount').textContent = '共 ' + fmtNum((d && d.total) || 0) + ' 个对话';
  if (!sel.items.length) {
    box.appendChild(el('div', 'empty', '选集「' + sel.collection + '」还是空的：到「浏览」里点 ☆ 加入'));
    return;
  }
  sel.items.forEach((c) => box.appendChild(convRow(c)));
}

function refreshRowStars() {
  $$('.conv').forEach((n) => {
    const b = n.querySelector('.conv-star');
    if (!b) return;
    const on = selHas(n.dataset.id);
    b.textContent = on ? '★' : '☆';
    b.classList.toggle('on', on);
    b.title = on ? '移出选集' : '加入选集';
  });
}

function refreshStar() {
  const b = $('#readerStar');
  if (!b) return;
  const on = !!(currentConv && selHas(currentConv.conversation_id));
  b.textContent = on ? '★' : '☆';
  b.classList.toggle('on', on);
  b.title = on ? '移出选集' : '加入选集';
}

async function toggleStar(id, forceRemove) {
  const action = (forceRemove || selHas(id)) ? 'remove' : 'add';
  await postSelection(id, action);
  toast(action === 'add' ? ('已加入选集 · ' + sel.collection) : ('已移出选集 · ' + sel.collection));
  await loadSelection();
}

function exportSelection(fmt) {
  if (!sel.items.length) { toast('选集为空，先加几个对话吧'); return; }
  const p = new URLSearchParams({ collection: sel.collection, format: fmt });
  if ($('#selWithThinking').checked) p.set('with_thinking', '1');
  location.href = '/api/export?' + p.toString();
}

/* ---------------- 阅读器 ---------------- */
let currentConv = null;
let currentMarks = [];
let markIdx = -1;
let currentQuery = '';
let openToken = 0;

function setMarkIdx(i) {
  if (!currentMarks.length) return;
  if (i < 0) i = currentMarks.length - 1;
  if (i >= currentMarks.length) i = 0;
  if (markIdx >= 0 && currentMarks[markIdx]) currentMarks[markIdx].classList.remove('current');
  markIdx = i;
  const mk = currentMarks[markIdx];
  mk.classList.add('current');
  mk.scrollIntoView({ block: 'center', behavior: 'smooth' });
  $('#hlCount').textContent = (markIdx + 1) + ' / ' + currentMarks.length;
}

function clearHighlight() {
  currentMarks.forEach((m) => {
    const p = m.parentNode;
    if (!p) return;
    p.replaceChild(document.createTextNode(m.textContent), m);
    p.normalize();
  });
  currentMarks = [];
  markIdx = -1;
  currentQuery = '';
  $('#hlBar').classList.add('hidden');
}

async function openConversation(id, opts) {
  opts = opts || {};
  const token = ++openToken;
  switchTab('browse');
  $('#readerEmpty').classList.add('hidden');
  $('#reader').classList.remove('hidden');
  $('#browseSplit').classList.add('reading');
  try {
    const d = await api('/api/conversation', { id });
    if (token !== openToken) return;
    currentConv = d.conversation;
    $('#readerName').textContent = currentConv.title || currentConv.conversation_id;

    const chars = d.messages.reduce((a, m) => a + (m.text ? m.text.length : 0), 0);
    const meta = $('#readerMeta');
    meta.innerHTML = '';
    meta.appendChild(el('span', 'src ' + currentConv.source, srcLabel(currentConv.source)));
    meta.appendChild(el('span', null, fmtNum(currentConv.message_count) + ' 条消息'));
    meta.appendChild(el('span', null, fmtSize(chars)));
    meta.appendChild(el('span', null, '创建 ' + fmtTs(currentConv.created_at)));
    meta.appendChild(el('span', null, '更新 ' + fmtTs(currentConv.updated_at)));
    meta.appendChild(el('span', null, currentConv.conversation_id));

    const body = $('#readerBody');
    body.innerHTML = '';
    let focusNode = null;
    d.messages.forEach((m) => {
      const n = el('div', 'msg ' + m.role);
      n.dataset.mi = m.message_index;
      const h = el('div', 'm-head');
      h.appendChild(el('span', 'm-role', m.role === 'user' ? '我' : (m.role === 'assistant' ? 'AI' : m.role)));
      h.appendChild(el('span', null, '#' + m.message_index));
      h.appendChild(el('span', null, fmtTs(m.timestamp)));
      if (m.timestamp_source && m.timestamp_source !== 'native') h.appendChild(el('span', null, '(' + m.timestamp_source + ')'));
      if (m.model) h.appendChild(el('span', null, m.model));
      if (m.text) h.appendChild(el('span', null, fmtSize(m.text.length)));
      n.appendChild(h);
      const txt = el('div', 'm-text');
      txt.innerHTML = renderRich(m.text || '');
      n.appendChild(txt);
      if (m.thinking) {
        const det = el('details');
        det.appendChild(el('summary', null, 'thinking · ' + fmtSize(m.thinking.length)));
        det.appendChild(el('pre', null, m.thinking));
        n.appendChild(det);
      }
      if (m.attachments && m.attachments.length) {
        const a = el('div', 'atts');
        a.appendChild(el('span', null, '附件: '));
        m.attachments.forEach((url, i) => {
          const link = el('a', null, '[' + (i + 1) + ']');
          link.href = safeUrl(url);
          link.target = '_blank';
          link.rel = 'noreferrer';
          a.appendChild(link);
          a.appendChild(document.createTextNode(' '));
        });
        n.appendChild(a);
      }
      body.appendChild(n);
      if (opts.index !== undefined && opts.index !== null && m.message_index === opts.index) focusNode = n;
    });

    readerScroller().scrollTop = 0;
    clearHighlight();
    $('#toTop').classList.add('hidden');

    if (opts.query) {
      currentQuery = opts.query;
      const terms = queryTerms(opts.query);
      if (terms.length) {
        currentMarks = highlightIn(body, terms);
        $('#hlQuery').textContent = terms.join(' / ');
        if (currentMarks.length) {
          $('#hlBar').classList.remove('hidden');
          let start = 0;
          if (opts.index !== undefined && opts.index !== null) {
            for (let i = 0; i < currentMarks.length; i++) {
              const host = currentMarks[i].closest('.msg');
              if (host && +host.dataset.mi >= opts.index) { start = i; break; }
            }
          }
          markIdx = -1;
          setMarkIdx(start);
        } else {
          toast('这个对话里没有出现搜索词');
        }
      }
    }

    if (!currentMarks.length && focusNode) {
      focusNode.classList.add('focus');
      focusNode.scrollIntoView({ block: 'center' });
    }

    markActiveConv();
    refreshStar();
    setHash(currentConv.conversation_id, opts.index, opts.query);
  } catch (e) {
    if (token === openToken) toast('打开失败: ' + e.message);
  }
}

function closeReader() {
  openToken++;
  currentConv = null;
  $('#reader').classList.add('hidden');
  $('#readerEmpty').classList.remove('hidden');
  $('#browseSplit').classList.remove('reading');
  markActiveConv();
  if (location.hash) history.replaceState(null, '', location.pathname + location.search);
}

/* ---------------- hash 路由 ---------------- */
function setHash(id, index, query) {
  const p = new URLSearchParams();
  p.set('c', id);
  if (index !== undefined && index !== null) p.set('i', index);
  if (query) p.set('q', query);
  const h = '#' + p.toString();
  if (location.hash !== h) history.replaceState(null, '', h);
}
function readHash() {
  const h = location.hash.replace(/^#/, '');
  if (!h) return null;
  const p = new URLSearchParams(h);
  const id = p.get('c');
  if (!id) return null;
  const i = p.get('i');
  return { id, index: i != null ? Number(i) : undefined, query: p.get('q') || '' };
}
function routeFromHash() {
  const r = readHash();
  if (!r) { if (currentConv) closeReader(); return; }
  if (currentConv && currentConv.conversation_id === r.id && !r.query) return;
  openConversation(r.id, { index: r.index, query: r.query });
}

/* ---------------- 按时间跳转 ---------------- */
async function doJump() {
  const v = $('#jumpInput').value;
  if (!v) { toast('请选择日期和时间'); return; }
  try {
    const d = await api('/api/jump', { ts: v });
    if (d.error) { toast(d.error); return; }
    const target = d.after || d.before;
    if (!target) { toast('归档里没有消息'); return; }
    toast((d.after ? '定位到之后最近一条: ' : '定位到之前最近一条: ') + fmtTs(target.timestamp));
    await openConversation(target.conversation_id, { index: target.message_index });
  } catch (e) {
    toast('跳转失败: ' + e.message);
  }
}

/* ---------------- Tab ---------------- */
function switchTab(name) {
  $$('.tab').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
  $$('.panel').forEach((p) => p.classList.toggle('active', p.id === 'tab-' + name));
  if (name === 'browse' && !browse.loaded) { browse.loaded = true; applyBrowse(); }
  if (name === 'timeline' && !tl.loaded) { tl.loaded = true; applyTimeline(); }
  if (name === 'selection') loadSelection().catch((e) => toast('选集加载失败: ' + e.message));
  if (name === 'search') setTimeout(() => $('#searchQ').focus(), 0);
}

/* ---------------- 初始化 ---------------- */
function init() {
  $$('.tab').forEach((b) => { b.onclick = () => switchTab(b.dataset.tab); });

  setupFolds();
  loadStats().catch((e) => toast('统计加载失败: ' + e.message));

  // 浏览
  const dq = debounce(applyBrowse, 250);
  $('#browseQ').addEventListener('input', dq);
  $('#browseSource').onchange = applyBrowse;
  $('#browseSort').onchange = applyBrowse;
  $('#browseFrom').onchange = applyBrowse;
  $('#browseTo').onchange = applyBrowse;
  $('#browseReset').onclick = () => {
    ['browseQ', 'browseFrom', 'browseTo'].forEach((i) => ($('#' + i).value = ''));
    $('#browseSource').value = '';
    $('#browseSort').value = 'updated';
    applyBrowse();
  };
  $('#browseMore').onclick = () => loadBrowse(true).catch((e) => toast('加载失败: ' + e.message));
  $('#browseList').addEventListener('scroll', (e) => {
    const b = e.target;
    if (b.scrollTop + b.clientHeight >= b.scrollHeight - 320 && browse.offset < browse.total && !browse.busy) {
      loadBrowse(true).catch(() => {});
    }
  });

  // 搜索
  $('#searchBtn').onclick = () => applySearch(true);
  $('#searchQ').addEventListener('keydown', (e) => { if (e.key === 'Enter') applySearch(true); });
  $('#searchPrev').onclick = () => { search.offset = Math.max(0, search.offset - search.limit); loadSearch(); };
  $('#searchNext').onclick = () => { search.offset += search.limit; loadSearch(); };

  // 时间线
  $('#tlApply').onclick = applyTimeline;
  $('#tlAll').onclick = () => { $('#tlFrom').value = ''; $('#tlTo').value = ''; applyTimeline(); };
  $('#tlBucket').onchange = applyTimeline;
  window.addEventListener('resize', debounce(() => {
    if (tl.loaded && $('#tab-timeline').classList.contains('active')) applyTimeline();
  }, 250));

  // 选集
  $('#selColl').addEventListener('change', () => {
    sel.collection = ($('#selColl').value || 'default').trim() || 'default';
    $('#selColl').value = sel.collection;
    loadSelection().catch((e) => toast('选集加载失败: ' + e.message));
  });
  $('#selRefresh').onclick = () => loadSelection().catch((e) => toast('选集加载失败: ' + e.message));
  $('#selExportMd').onclick = () => exportSelection('md');
  $('#selExportHtml').onclick = () => exportSelection('html');
  $('#selExportJsonl').onclick = () => exportSelection('jsonl');

  // 阅读器
  $('#readerClose').onclick = closeReader;
  $('#readerStar').onclick = () => {
    if (currentConv) toggleStar(currentConv.conversation_id).catch((e) => toast('选集失败: ' + e.message));
  };
  $('#readerMd').onclick = () => {
    if (currentConv) location.href = '/api/conversation?id=' +
      encodeURIComponent(currentConv.conversation_id) + '&format=md';
  };
  $('#hlPrev').onclick = () => setMarkIdx(markIdx - 1);
  $('#hlNext').onclick = () => setMarkIdx(markIdx + 1);
  $('#hlClear').onclick = clearHighlight;
  const onReaderScroll = () => {
    $('#toTop').classList.toggle('hidden', readerScroller().scrollTop < 400);
  };
  $('#readerBody').addEventListener('scroll', onReaderScroll);
  $('#reader').addEventListener('scroll', onReaderScroll);
  $('#toTop').onclick = () => readerScroller().scrollTo({ top: 0, behavior: 'smooth' });

  document.addEventListener('keydown', (e) => {
    const inField = /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName);
    if (e.key === 'Escape') { if (currentConv) closeReader(); return; }
    if (e.key === '/' && !inField) { e.preventDefault(); switchTab('search'); return; }
    if (currentConv && currentMarks.length && (e.key === 'Enter') && !inField) {
      e.preventDefault(); setMarkIdx(e.shiftKey ? markIdx - 1 : markIdx + 1);
    }
  });

  // 跳转
  const now = new Date(Date.now() + TZ_OFFSET * 3600 * 1000);
  const p = (n) => String(n).padStart(2, '0');
  $('#jumpInput').value = now.getUTCFullYear() + '-' + p(now.getUTCMonth() + 1) + '-' +
    p(now.getUTCDate()) + 'T' + p(now.getUTCHours()) + ':' + p(now.getUTCMinutes());
  $('#jumpBtn').onclick = doJump;
  $('#jumpInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') doJump(); });

  window.addEventListener('hashchange', routeFromHash);
  loadSelection().catch(() => {});
  routeFromHash();
}

document.addEventListener('DOMContentLoaded', init);
