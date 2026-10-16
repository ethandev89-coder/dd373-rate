/* ============================================================================
   飛馬走勢圖 — 手刻 SVG 渲染器
   原則（依驗收條件）：
     1) X 軸＝真實時間線性比例（絕不用資料點索引）
     2) X 刻度級距從 [1h,3h,6h,12h,1d,2d,7d] 挑「刻度數落在 5~8 個」的那一個，
        並對齊台北時間：tick = floor((t + 8h) / step) * step − 8h
     3) Y 軸 nice ticks：級距 1/2/2.5/5 × 10^n，上下各留 5% padding
     4) 以 ResizeObserver 依實際像素寬高重繪（不用 viewBox 拉伸）；
        刻度字 11px、最新值標籤 12px、主線 2px（固定像素，不隨寬度縮放）
     5) 面積填色只在實測資料本身範圍（第一筆→最後一筆），不從圖表左緣起畫
     6) 十字線＋tooltip 用 pointer events（滑鼠／觸控共用同一套）
     7) 最新值標籤貼在右側 Y 軸上
   ========================================================================== */
(function () {
  'use strict';

  var TZOFF = 8 * 3600;                                   // Asia/Taipei（台灣無日光節約，固定 +8）
  var STEPS = [3600, 10800, 21600, 43200, 86400, 172800, 604800];  // 1h 3h 6h 12h 1d 2d 7d
  var FONT = 11, FONT_BADGE = 12, LW = 2, LW_EST = 1.6;
  var MIN_LABEL_PX = 44;                                  // 相鄰刻度標籤最小可讀間距

  /* ---------- 時間（台北） ---------- */
  function tp(t) {
    var d = new Date((t + TZOFF) * 1000);
    return { Y: d.getUTCFullYear(), M: d.getUTCMonth() + 1, D: d.getUTCDate(),
             h: d.getUTCHours(), m: d.getUTCMinutes() };
  }
  function p2(n) { return (n < 10 ? '0' : '') + n; }
  function fmtDate(t) { var p = tp(t); return p2(p.M) + '/' + p2(p.D); }
  function fmtHM(t) { var p = tp(t); return p2(p.h) + ':' + p2(p.m); }
  function fmtFull(t) { var p = tp(t); return p2(p.M) + '/' + p2(p.D) + ' ' + p2(p.h) + ':' + p2(p.m); }
  function isMidnight(t) { return tp(t).h === 0 && tp(t).m === 0; }

  /* ---------- X 刻度 ---------- */
  function tickIdx(t, step) { return Math.floor((t + TZOFF) / step); }

  // 窗內（含端點）的對齊刻度
  function ticksFor(x0, x1, step) {
    var out = [], k0 = Math.ceil((x0 + TZOFF) / step), k1 = Math.floor((x1 + TZOFF) / step);
    for (var k = k0; k <= k1; k++) out.push(k * step - TZOFF);
    return out;
  }

  // 先挑「刻度數 5~8」的最小級距；標籤會擠在一起時才往上一級（下一級仍需 >=5 個才升）
  function chooseStep(x0, x1, pxWidth) {
    var i, step, best = null;
    for (i = 0; i < STEPS.length; i++) {
      var n = ticksFor(x0, x1, STEPS[i]).length;
      if (n >= 5 && n <= 8) { best = i; break; }
    }
    if (best === null) return STEPS[STEPS.length - 1];
    var inner = pxWidth - 48 - 46;
    for (i = best; i < STEPS.length - 1; i++) {
      var ticks = ticksFor(x0, x1, STEPS[i]);
      var need = ticks.length * MIN_LABEL_PX;
      if (inner >= need || ticksFor(x0, x1, STEPS[i + 1]).length < 5) break;
    }
    return STEPS[i];
  }

  // 標籤文字：整日 → M/D，其餘 → HH:MM
  function tickLabel(t, step) {
    if (step >= 86400) return fmtDate(t);
    return isMidnight(t) ? fmtDate(t) : fmtHM(t);
  }

  /* ---------- Y 軸 nice ticks（級距 1/2/2.5/5 × 10^n） ---------- */
  function niceStep(raw) {
    if (!(raw > 0)) return 1;
    var exp = Math.floor(Math.log(raw) / Math.LN10);
    var f = raw / Math.pow(10, exp);
    var nf = f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10;
    return nf * Math.pow(10, exp);
  }
  function niceScale(lo, hi, target, padFrac) {
    if (padFrac === undefined) padFrac = 0.05;
    if (target === undefined) target = 5;
    if (!(hi > lo)) { var c = (hi + lo) / 2 || 0; lo = c - 1; hi = c + 1; }
    var pad = (hi - lo) * padFrac;
    lo -= pad; hi += pad;
    var step = niceStep((hi - lo) / (target - 1));
    var nlo = Math.floor(lo / step) * step, nhi = Math.ceil(hi / step) * step;
    var ticks = [], v;
    for (v = nlo; v <= nhi + step * 1e-9; v += step) ticks.push(Math.round(v * 1e6) / 1e6);
    return { min: nlo, max: nhi, step: step, ticks: ticks };
  }

  /* ---------- 資料輔助 ---------- */
  // 相鄰兩筆間隔 > gap 就斷線（回傳一串段）
  function segments(pts, gap) {
    var segs = [], cur = [], i;
    for (i = 0; i < pts.length; i++) {
      if (cur.length && pts[i].t - cur[cur.length - 1].t > gap) { segs.push(cur); cur = []; }
      cur.push(pts[i]);
    }
    if (cur.length) segs.push(cur);
    return segs;
  }

  function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function num(v) { return Number(v).toLocaleString('en-US'); }

  /* ---------- 幾何 ---------- */
  function pad(w) { return w >= 560 ? { l: 48, r: 50, t: 12, b: 28 } : { l: 46, r: 46, t: 12, b: 28 }; }
  function height(w) { return w >= 560 ? 240 : 200; }
  function bounds(vals) {
    var lo = Infinity, hi = -Infinity;
    for (var i = 0; i < vals.length; i++) {
      var v = vals[i];
      if (v === null || v === undefined || isNaN(v)) continue;
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
    return lo === Infinity ? [0, 1] : [lo, hi];
  }

  /* ---------- 主渲染 ---------- */
  function render(win) {
    var host = document.querySelector('.chart[data-win="' + win.id + '"]');
    if (!host) return;
    var w = Math.max(240, Math.round(host.clientWidth || 320));
    var h = height(w), pd = pad(w);
    var iw = w - pd.l - pd.r, ih = h - pd.t - pd.b;

    var now = Math.floor(Date.now() / 1000);
    var x1 = now, x0 = now - win.span;

    var real = DATA.real.filter(function (p) { return p.t >= x0 && p.t <= now; })
                       .map(function (p) { return { t: p.t, v: p.v, raw: p.raw === undefined ? p.v : p.raw, o: p.o }; });
    var est = DATA.est.filter(function (p) { return p.t >= x0 && p.t <= now; })
                      .map(function (p) { return { t: p.t, v: p.v }; });

    if (real.length < 2 && est.length < 2) {
      host.innerHTML = '<p class="muted" style="font-size:12px;margin:6px 0">這個時間窗還沒有資料</p>';
      return;
    }

    // Y 範圍：實測（含離群原始值）＋估算
    var vals = real.map(function (p) { return p.v; }).concat(real.map(function (p) { return p.raw; }))
                   .concat(est.map(function (p) { return p.v; }));
    var yb = bounds(vals), ys = niceScale(yb[0], yb[1], 5, 0.05);
    var y0 = ys.min, y1 = ys.max;

    function X(t) { return pd.l + (t - x0) / (x1 - x0) * iw; }
    function Y(v) { return pd.t + (y1 - v) / (y1 - y0) * ih; }

    var step = chooseStep(x0, x1, w);
    var ticks = ticksFor(x0, x1, step);

    var s = [];
    s.push('<svg width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '" ' +
           'font-family="-apple-system,PingFang TC,Microsoft JhengHei,sans-serif" role="img" ' +
           'aria-label="' + esc(win.label + ' 走勢') + '">');

    // 面積填色漸層（由線往下淡出到透明）
    s.push('<defs><linearGradient id="g_' + win.id + '" gradientUnits="userSpaceOnUse" ' +
           'x1="0" y1="' + pd.t + '" x2="0" y2="' + (pd.t + ih) + '">' +
           '<stop offset="0%" stop-color="' + win.color + '" stop-opacity="0.26"/>' +
           '<stop offset="100%" stop-color="' + win.color + '" stop-opacity="0"/></linearGradient></defs>');

    // 格線＋Y 標籤
    ys.ticks.forEach(function (v) {
      var yy = Y(v);
      s.push('<line x1="' + pd.l + '" y1="' + yy.toFixed(1) + '" x2="' + (pd.l + iw) + '" y2="' + yy.toFixed(1) +
             '" stroke="#2a3441" stroke-width="1"/>');
      s.push('<text x="' + (pd.l - 7) + '" y="' + (yy + 4).toFixed(1) + '" font-size="' + FONT +
             '" fill="#7f8c9b" text-anchor="end">' + num(v) + '</text>');
    });

    // 面積填色：只涵蓋實測資料本身（第一筆→最後一筆），不從左緣起畫
    if (real.length >= 2) {
      segments(real, win.gapReal).forEach(function (seg) {
        if (seg.length < 2) return;
        var d = seg.map(function (p, i) { return (i ? 'L' : 'M') + X(p.t).toFixed(1) + ',' + Y(p.v).toFixed(1); }).join(' ');
        d += ' L' + X(seg[seg.length - 1].t).toFixed(1) + ',' + (pd.t + ih).toFixed(1) +
             ' L' + X(seg[0].t).toFixed(1) + ',' + (pd.t + ih).toFixed(1) + ' Z';
        s.push('<path d="' + d + '" fill="url(#g_' + win.id + ')" stroke="none"/>');
      });
    }

    // 估算線（灰虛線，無填色）
    segments(est, win.gapEst).forEach(function (seg) {
      if (seg.length < 2) return;
      var d = seg.map(function (p, i) { return (i ? 'L' : 'M') + X(p.t).toFixed(1) + ',' + Y(p.v).toFixed(1); }).join(' ');
      s.push('<path d="' + d + '" fill="none" stroke="#7f8c9b" stroke-width="' + LW_EST + '" ' +
             'stroke-dasharray="5 4" opacity="0.8"/>');
    });

    // 實測線
    segments(real, win.gapReal).forEach(function (seg) {
      var d = seg.map(function (p, i) { return (i ? 'L' : 'M') + X(p.t).toFixed(1) + ',' + Y(p.v).toFixed(1); }).join(' ');
      s.push('<path d="' + d + '" fill="none" stroke="' + win.color + '" stroke-width="' + LW +
             '" stroke-linejoin="round" stroke-linecap="round"/>');
    });

    // 離群點：空心小點（線用平滑值，點顯示原始值位置）
    real.forEach(function (p) {
      if (!p.o) return;
      s.push('<circle cx="' + X(p.t).toFixed(1) + '" cy="' + Y(p.raw).toFixed(1) + '" r="3.4" fill="none" ' +
             'stroke="' + win.color + '" stroke-width="1.6"/>');
    });

    // X 標籤（對齊台北整點／日界；首尾靠邊對齊避免被切掉）
    ticks.forEach(function (t, i) {
      var anchor = i === 0 ? 'start' : (i === ticks.length - 1 ? 'end' : 'middle');
      s.push('<text x="' + X(t).toFixed(1) + '" y="' + (h - 10) + '" font-size="' + FONT +
             '" fill="#7f8c9b" text-anchor="' + anchor + '">' + tickLabel(t, step) + '</text>');
    });

    // 最新值標籤：貼在右側 Y 軸上
    var last = real.length ? real[real.length - 1] : null;
    if (last) {
      var ly = Y(last.v), bx = w - pd.r + 6, bw = pd.r - 12, bh = 18;
      ly = Math.min(Math.max(ly, pd.t + bh / 2), pd.t + ih - bh / 2);
      s.push('<rect x="' + bx + '" y="' + (ly - bh / 2).toFixed(1) + '" width="' + bw + '" height="' + bh +
             '" rx="4" fill="' + win.color + '"/>');
      s.push('<text x="' + (bx + bw / 2) + '" y="' + (ly + 4.2).toFixed(1) + '" font-size="' + FONT_BADGE +
             '" font-weight="700" fill="#0f1419" text-anchor="middle">' + num(last.v) + '</text>');
    }

    s.push('</svg>');
    host.innerHTML = s.join('');

    CHARTS[win.id] = { host: host, w: w, h: h, cw: host.clientWidth, pd: pd, iw: iw, ih: ih, x0: x0, x1: x1,
                       y0: y0, y1: y1, X: X, Y: Y, real: real, est: est, step: step };
    if (window.DD373AfterRender) window.DD373AfterRender(win, CHARTS[win.id]);
  }

  /* ---------- 載入 / 重繪 ---------- */
  var DATA = null, CHARTS = {};
  function renderAll() { WINCFG.forEach(render); }

  // 內嵌資料為求精簡用陣列：[t, 平滑值, 原始值, 是否離群] / [t, v]
  function normalize(d) {
    d.real = d.real.map(function (r) { return { t: r[0], v: r[1], raw: r[2] === undefined ? r[1] : r[2], o: !!r[3] }; });
    d.est = d.est.map(function (r) { return { t: r[0], v: r[1] }; });
    return d;
  }

  function boot() {
    var el = document.getElementById('chart-data');
    if (!el) return;
    DATA = normalize(JSON.parse(el.textContent));
    renderAll();
    if (window.ResizeObserver) {
      var ro = new ResizeObserver(function () {
        var seen = {};
        WINCFG.forEach(function (win) {
          var host = document.querySelector('.chart[data-win="' + win.id + '"]');
          if (host && host.clientWidth) seen[win.id] = Math.round(host.clientWidth);
        });
        var changed = Object.keys(seen).some(function (k) { return CHARTS[k] && CHARTS[k].cw !== seen[k]; });
        if (changed) renderAll();
      });
      WINCFG.forEach(function (win) {
        var host = document.querySelector('.chart[data-win="' + win.id + '"]');
        if (host) ro.observe(host);
      });
    } else {
      window.addEventListener('resize', renderAll);
    }
    // 保險 1：視窗尺寸變化也重繪（ResizeObserver 在部分嵌入環境／無頭瀏覽器不一定會通知）
    window.addEventListener('resize', function () { setTimeout(renderAll, 0); });
    // 保險 2：切換時間窗時面板由 display:none 變可見 → 立即用正確寬度重繪
    Array.prototype.forEach.call(document.querySelectorAll('input[name=tr]'), function (r) {
      r.addEventListener('change', function () { setTimeout(renderAll, 0); });
    });
  }

  window.DD373TREND = { boot: boot, renderAll: renderAll, charts: CHARTS,
                        // 供測試用
                        ticksFor: ticksFor, chooseStep: chooseStep, niceScale: niceScale, niceStep: niceStep,
                        tickLabel: tickLabel, fmtFull: fmtFull, tickIdx: tickIdx, TZOFF: TZOFF };

  var WINCFG = window.DD373WINDOWS || [];
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
