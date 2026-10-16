/* ============================================================================
   飛馬走勢圖 — 手刻 SVG 渲染器（A 版：含 P0 遺留三修正）
   原則：
     1) X 軸＝真實時間線性比例（絕不用資料點索引）
     2) X 刻度級距從 [1h,3h,6h,12h,1d,2d,7d,14d] 挑「刻度數 5~8」的最小級距，
        但**標籤不重疊優先**：會重疊就往上跳級（允許掉到 3~4 個刻度），
        最後還有 DOM 實測保險（真的擠就再隔一個標）。
     3) 對齊台北時間：整點 tick = floor((t + 8h) / step) × step − 8h；
        日以上（1d/2d/7d/14d）錨在「台北時間週一 00:00」
     4) Y 軸 nice ticks：級距 1/2/2.5/5 × 10^n，上下各留 5% padding
     5) 以 ResizeObserver 依實際像素寬高重繪（不用 viewBox 拉伸）；
        刻度字 11px、最新值標籤 12px、實測線 2px（固定像素，不隨寬度縮放）
     6) 面積填色只在實測資料本身範圍（第一筆→最後一筆），不從圖表左緣起畫
     7) 十字線＋tooltip 用 pointer events（滑鼠／觸控共用同一套）
     8) 最新值標籤貼在右側 Y 軸上；數字一律四捨五入成整數（與頁面其他處一致）
   ========================================================================== */
(function () {
  'use strict';

  var TZOFF = 8 * 3600;                                   // Asia/Taipei（台灣無日光節約，固定 +8）
  var HOUR = 3600, DAY = 86400;
  var STEPS = [HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR, DAY, 2 * DAY, 7 * DAY, 14 * DAY];
  var FONT = 11, FONT_BADGE = 12, LW = 2, LW_EST = 1.6;
  var CH_LABEL = 6.2;                                     // 11px 數字估計字寬（第一輪排版的估計值）
  var MIN_GAP_PAD = 6;                                    // 標籤之間至少留的空白

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
  function isMidnight(t) { var p = tp(t); return p.h === 0 && p.m === 0; }
  function num(v) { return Math.round(v).toLocaleString('en-US'); }   // 整數＋千分位
  /* 漲跌方向（台股慣例）：數字變大＝紅 ▲、數字變小＝綠 ▼；
     四捨五入（digits 位）後為 0 就不加箭頭、用灰色。回傳 { cls, arrow, a }，a 已四捨五入。 */
  function dirOf(v, digits) {
    var f = Math.pow(10, digits === undefined ? 1 : digits);
    var a = Math.round(Math.abs(v) * f) / f;
    if (a === 0 || v === 0) return { cls: 'flat', arrow: '', a: 0 };
    var up = v > 0;
    return { cls: up ? 'up' : 'down', arrow: up ? '▲' : '▼', a: a };
  }
  function dirPct(v) {                      // 百分比字串（不含正負號）
    var d = dirOf(v);
    return { cls: d.cls, txt: d.cls === 'flat' ? '0.0%' : d.arrow + ' ' + d.a.toFixed(1) + '%' };
  }

  /* ---------- X 刻度 ---------- */
  // 台北「週一 00:00」的日序（day 0 = 1970-01-01 台北 = 週四 → 週一 ≡ 4 (mod 7)）
  function mondayAnchorDay(dayIdx) { return dayIdx - ((((dayIdx - 4) % 7) + 7) % 7); }

  function ticksFor(x0, x1, step) {
    var out = [], i;
    if (step % DAY === 0) {                                // 日以上：以週一為錨
      var n = step / DAY;
      var d0 = Math.floor((x0 + TZOFF) / DAY), d1 = Math.floor((x1 + TZOFF) / DAY);
      var anchor = mondayAnchorDay(d0);
      var k = Math.floor((d0 - anchor) / n);
      for (i = anchor + k * n; i <= d1; i += n) {
        var t = i * DAY - TZOFF;
        if (t >= x0 && t <= x1) out.push(t);
      }
      return out;
    }
    var k0 = Math.ceil((x0 + TZOFF) / step), k1 = Math.floor((x1 + TZOFF) / step);
    for (i = k0; i <= k1; i++) out.push(i * step - TZOFF);
    return out;
  }

  // 標籤文字：整日 → M/D，其餘 → HH:MM（跨日那一格顯示日期）
  function tickLabel(t, step) {
    if (step >= DAY) return fmtDate(t);
    return isMidnight(t) ? fmtDate(t) : fmtHM(t);
  }

  function maxLabelPx(ticks, step) {
    var m = 0;
    for (var i = 0; i < ticks.length; i++) {
      var w = tickLabel(ticks[i], step).length * CH_LABEL + MIN_GAP_PAD;
      if (w > m) m = w;
    }
    return m || 40;
  }

  // 相鄰刻度在畫面上的 px 間距（時間等距 → 像素也等距）
  function tickGapPx(pxInner, step, x0, x1) { return pxInner * step / (x1 - x0); }

  /* 選級距（三段式）：
     1) 首選：刻度數 5~8 且標籤不重疊 → 取最小級距
     2) 次選：沒有 5~8 時，取「刻度數最接近 5」且不重疊、且 >=3 個刻度的級距
        （例如 30 天窗：2d 會擠出 16 個刻度、7d 剛好 4 個 → 取 7d 週刻度）
     3) 都不行 → 用最大級距，由 labelEvery／DOM 實測隔一個標收拾 */
  function chooseStep(x0, x1, pxInner) {
    var i, tk, gap, need, n, best = null, fallback = null, bestDist = Infinity;
    for (i = 0; i < STEPS.length; i++) {
      tk = ticksFor(x0, x1, STEPS[i]);
      n = tk.length;
      if (n < 3) continue;
      gap = tickGapPx(pxInner, STEPS[i], x0, x1);
      need = maxLabelPx(tk, STEPS[i]);
      if (gap < need) continue;                                  // 會重疊 → 不採用
      if (n >= 5 && n <= 8 && best === null) best = STEPS[i];     // 首選
      if (fallback === null && n < 5) fallback = STEPS[i];        // 次選：第一個 <5 但不重疊的
      if (n < 5 && Math.abs(n - 5) < bestDist) { bestDist = Math.abs(n - 5); fallback = STEPS[i]; }
    }
    if (best !== null) return best;
    if (fallback !== null) return fallback;
    return STEPS[STEPS.length - 1];
  }

  // 完整排版計劃：級距、所有刻度、以及實際要標的刻度（隔 labelEvery 個標一次）
  function labelPlan(x0, x1, pxInner) {
    var step = chooseStep(x0, x1, pxInner);
    var ticks = ticksFor(x0, x1, step);
    var need = maxLabelPx(ticks, step);
    var gap = tickGapPx(pxInner, step, x0, x1);
    var every = gap > 0 ? Math.max(1, Math.ceil(need / gap)) : 1;
    var labeled = [];
    for (var i = 0; i < ticks.length; i++) if (i % every === 0) labeled.push(ticks[i]);
    return { step: step, ticks: ticks, labeled: labeled, every: every };
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
  function segments(pts, gap) {                            // 相鄰兩筆間隔 > gap 就斷線
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
  function render(win, depth) {
    var host = document.querySelector('.chart[data-win="' + win.id + '"]');
    if (!host) return;
    var w = Math.max(240, Math.round(host.clientWidth || 320));
    var h = height(w), pd = pad(w);
    var iw = w - pd.l - pd.r, ih = h - pd.t - pd.b;

    var now = Math.floor(Date.now() / 1000);
    var x1 = now, x0 = now - win.span;

    // 這個窗要用原始點還是降採樣序列？（7 天＝1 小時中位數＋min–max 帶；30 天＝4 小時）
    var useAgg = !!(win.agg && DATA.agg && DATA.agg[win.agg]);
    var real, gapReal;
    if (useAgg) {
      real = DATA.agg[win.agg].filter(function (r) { return r[0] >= x0 && r[0] <= now; })
                              .map(function (r) { return { t: r[0], v: r[1], raw: r[2], o: !!r[3],
                                                           lo: r[4], hi: r[5], n: r[6] }; });
      gapReal = win.gapAgg || win.gapReal;              // 聚合後：連續幾個時段沒資料才斷線
    } else {
      real = DATA.real.filter(function (p) { return p.t >= x0 && p.t <= now; })
                       .map(function (p) { return { t: p.t, v: p.v, raw: p.raw, o: p.o }; });
      gapReal = win.gapReal;
    }
    var est = DATA.est.filter(function (p) { return p.t >= x0 && p.t <= now; })
                      .map(function (p) { return { t: p.t, v: p.v }; });

    if (real.length < 2 && est.length < 2) {
      host.innerHTML = '<p class="muted" style="font-size:12px;margin:6px 0">這個時間窗還沒有資料</p>';
      return;
    }

    // Y 範圍：實測（含離群原始值、min–max 帶）＋估算
    var vals = real.map(function (p) { return p.v; }).concat(real.map(function (p) { return p.raw; }))
                   .concat(real.map(function (p) { return p.lo === undefined ? p.v : p.lo; }))
                   .concat(real.map(function (p) { return p.hi === undefined ? p.v : p.hi; }))
                   .concat(est.map(function (p) { return p.v; }));
    var yb = bounds(vals), ys = niceScale(yb[0], yb[1], 5, 0.05);
    var y0 = ys.min, y1 = ys.max;

    function X(t) { return pd.l + (t - x0) / (x1 - x0) * iw; }
    function Y(v) { return pd.t + (y1 - v) / (y1 - y0) * ih; }

    var plan = labelPlan(x0, x1, iw);
    if (win.__everyW === w && win.__labelEvery && win.__labelEvery > plan.every) {   // DOM 實測後的修正值
      plan.every = win.__labelEvery;
      plan.labeled = plan.ticks.filter(function (_t, i) { return i % plan.every === 0; });
    }
    var step = plan.step;

    var s = [];
    s.push('<svg width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '" ' +
           'font-family="-apple-system,PingFang TC,Microsoft JhengHei,sans-serif" role="img" ' +
           'aria-label="' + esc(win.label + ' 走勢') + '">');

    // 面積填色漸層（由線往下淡出到透明；顏色/透明度由 CSS 的 .stop-top/.stop-bot 決定）
    s.push('<defs><linearGradient id="g_' + win.id + '" gradientUnits="userSpaceOnUse" ' +
           'x1="0" y1="' + pd.t + '" x2="0" y2="' + (pd.t + ih) + '">' +
           '<stop class="stop-top" offset="0%"/><stop class="stop-bot" offset="100%"/></linearGradient></defs>');

    // 圖上註記：背景色帶（例：第 2 次移民期）先畫，格線與資料線疊在上面
    (DATA.notes || []).forEach(function (nt) {
      if (nt.type !== 'band' || nt.to <= x0 || nt.from >= x1) return;
      var bx0 = X(Math.max(nt.from, x0)), bx1 = X(Math.min(nt.to, x1));
      s.push('<rect class="note-band" x="' + bx0.toFixed(1) + '" y="' + pd.t +
             '" width="' + Math.max(bx1 - bx0, 1).toFixed(1) + '" height="' + ih + '"/>');
      if (bx1 - bx0 > 60) {
        s.push('<text class="note-txt" x="' + (bx0 + 4).toFixed(1) + '" y="' + (pd.t + 11) +
               '" text-anchor="start">' + nt.label + '</text>');
      }
    });

    // 格線＋Y 標籤
    ys.ticks.forEach(function (v) {
      var yy = Y(v);
      s.push('<line class="grid" x1="' + pd.l + '" y1="' + yy.toFixed(1) + '" x2="' + (pd.l + iw) + '" y2="' + yy.toFixed(1) + '"/>');
      s.push('<text class="ytick" x="' + (pd.l - 7) + '" y="' + (yy + 4).toFixed(1) +
             '" text-anchor="end">' + num(v) + '</text>');
    });

    // 圖上註記：垂直虛線標記（例：開始實測）
    (DATA.notes || []).forEach(function (nt) {
      if (nt.type !== 'vline' || nt.t < x0 || nt.t > x1) return;
      var vx = X(nt.t), near = vx > pd.l + iw - 60;
      s.push('<line class="note-line" x1="' + vx.toFixed(1) + '" y1="' + pd.t +
             '" x2="' + vx.toFixed(1) + '" y2="' + (pd.t + ih) + '"/>');
      s.push('<text class="note-txt" x="' + (near ? vx - 4 : vx + 4).toFixed(1) + '" y="' + (pd.t + 23) +
             '" text-anchor="' + (near ? 'end' : 'start') + '">' + nt.label + '</text>');
    });

    // min–max 淡色帶（只有降採樣的窗有）
    if (useAgg && real.length >= 2) {
      segments(real, gapReal).forEach(function (seg) {
        if (seg.length < 2) return;
        var up = seg.map(function (p, i) {
          return (i ? 'L' : 'M') + X(p.t).toFixed(1) + ',' + Y(p.hi).toFixed(1);
        }).join(' ');
        var dn = seg.slice().reverse().map(function (p) {
          return 'L' + X(p.t).toFixed(1) + ',' + Y(p.lo).toFixed(1);
        }).join(' ');
        s.push('<path class="band" d="' + up + ' ' + dn + ' Z"/>');
      });
    }

    // 面積填色：只涵蓋實測資料本身（第一筆→最後一筆），不從左緣起畫
    if (real.length >= 2) {
      segments(real, gapReal).forEach(function (seg) {
        if (seg.length < 2) return;
        var d = seg.map(function (p, i) { return (i ? 'L' : 'M') + X(p.t).toFixed(1) + ',' + Y(p.v).toFixed(1); }).join(' ');
        d += ' L' + X(seg[seg.length - 1].t).toFixed(1) + ',' + (pd.t + ih).toFixed(1) +
             ' L' + X(seg[0].t).toFixed(1) + ',' + (pd.t + ih).toFixed(1) + ' Z';
        s.push('<path class="area" d="' + d + '" fill="url(#g_' + win.id + ')" stroke="none"/>');
      });
    }

    // 估算線（灰虛線，無填色）
    segments(est, win.gapEst).forEach(function (seg) {
      if (seg.length < 2) return;
      var d = seg.map(function (p, i) { return (i ? 'L' : 'M') + X(p.t).toFixed(1) + ',' + Y(p.v).toFixed(1); }).join(' ');
      s.push('<path class="line-est" d="' + d + '"/>');
    });

    // 實測線
    segments(real, gapReal).forEach(function (seg) {
      var d = seg.map(function (p, i) { return (i ? 'L' : 'M') + X(p.t).toFixed(1) + ',' + Y(p.v).toFixed(1); }).join(' ');
      s.push('<path class="line-real" d="' + d + '"/>');
    });

    // 離群點：空心小點（原始值位置）
    real.forEach(function (p) {
      if (!p.o) return;
      s.push('<circle class="outlier" cx="' + X(p.t).toFixed(1) + '" cy="' + Y(p.raw).toFixed(1) + '" r="3.4"/>');
    });

    // X 標籤（台北整點／週一對齊）
    // 對齊方式：一律置中（貼齊刻度）；只有置中文字會超出 SVG 邊界時才靠左／靠右。
    // 註：不要「首尾一律靠邊」——第一個標籤整段往右伸會跟下一個標籤重疊。
    plan.labeled.forEach(function (t) {
      var txt = tickLabel(t, step), est = txt.length * CH_LABEL, cx = X(t);
      var anchor = 'middle';
      if (cx - est / 2 < 2) anchor = 'start';
      else if (cx + est / 2 > w - 2) anchor = 'end';
      s.push('<text class="xtick" x="' + cx.toFixed(1) + '" y="' + (h - 10) +
             '" text-anchor="' + anchor + '">' + txt + '</text>');
    });

    // 最新值標籤：貼在右側 Y 軸上（一律用最新的「原始實測點」，四個窗才會跟 Hero 一致）
    var latest = null, li;
    for (li = DATA.real.length - 1; li >= 0; li--) {
      if (DATA.real[li].t <= now && DATA.real[li].t >= x0) { latest = DATA.real[li]; break; }
    }
    if (!latest && real.length) latest = real[real.length - 1];
    if (latest) {
      var ly = Y(latest.v), bx = w - pd.r + 6, bw = pd.r - 12, bh = 18;
      ly = Math.min(Math.max(ly, pd.t + bh / 2), pd.t + ih - bh / 2);
      s.push('<rect class="badge" x="' + bx + '" y="' + (ly - bh / 2).toFixed(1) + '" width="' + bw +
             '" height="' + bh + '" rx="6"/>');
      s.push('<text class="badge-t" x="' + (bx + bw / 2) + '" y="' + (ly + 4.2).toFixed(1) +
             '" text-anchor="middle">' + num(latest.v) + '</text>');
    }

    // 十字線圖層（hover 時只更新這一層，不重繪整張圖）
    s.push('<g class="cross-g" visibility="hidden">' +
           '<line class="cross" x1="0" y1="' + pd.t + '" x2="0" y2="' + (pd.t + ih) + '"/>' +
           '<circle class="cross-dot" cx="0" cy="0" r="4"/></g>');

    s.push('</svg>');
    host.innerHTML = s.join('');

    // hover 用的合併序列（實測＋估算，依時間排序；估算本來就在實測之前）
    var hoverPts = est.map(function (p) { return { t: p.t, v: p.v, raw: p.v, o: false, src: 'est' }; })
                      .concat(real.map(function (p) {
                        return { t: p.t, v: p.v, raw: p.raw, o: p.o, src: 'real',
                                 lo: p.lo, hi: p.hi, n: p.n };
                      }))
                      .sort(function (a, b) { return a.t - b.t; });

    CHARTS[win.id] = { win: win, host: host, w: w, h: h, cw: host.clientWidth, pd: pd, iw: iw, ih: ih,
                       x0: x0, x1: x1, y0: y0, y1: y1, X: X, Y: Y, real: real, est: est,
                       hoverPts: hoverPts, step: step, plan: plan, useAgg: useAgg,
                       aggLabel: useAgg ? (win.agg === '7d' ? '1 小時' : '4 小時') : null };

    updateSummary(win, CHARTS[win.id]);

    // 標籤量測保險：實際文字框若還重疊 → 隔一個標重畫（最多 3 輪）
    if (fitLabels(win, CHARTS[win.id], depth || 0)) return;
    attachHover(win, CHARTS[win.id]);
    if (window.DD373AfterRender) window.DD373AfterRender(win, CHARTS[win.id]);
  }

  /* ---------- 本時窗摘要（圖上方一行） ---------- */
  function updateSummary(win, st) {
    var el = document.querySelector('.wsum[data-win="' + win.id + '"]');
    if (!el) return;
    var pts = st.hoverPts;
    if (pts.length < 2) { el.innerHTML = ''; return; }
    var first = pts[0], last = pts[pts.length - 1];
    var vals = pts.map(function (p) { return p.v; });
    var hi = Math.max.apply(null, vals), lo = Math.min.apply(null, vals);
    var chg = (last.v / first.v - 1) * 100;
    // 台股慣例：數字變大＝紅 ▲、變小＝綠 ▼、0.0% 灰不加箭頭（與 Hero 膠囊同一套寫法）
    var d = dirPct(chg);
    var hasEst = pts.some(function (p) { return p.src === 'est'; });
    el.innerHTML = '本時窗 <span class="' + d.cls + '">' + d.txt + '</span>' +
                   '　高 <b>' + num(hi) + '</b>　低 <b>' + num(lo) + '</b>' +
                   (hasEst ? '　<span class="tag">含估算</span>' : '');
  }

  /* ---------- 十字線＋tooltip（pointer events：滑鼠／觸控共用） ---------- */
  function attachHover(win, st) {
    var host = st.host;
    // 注意：render() 會用 innerHTML 重畫整張圖，tooltip 元素會被清掉 → 每次都要確認它還掛在 DOM 上
    if (!host.__tt) {
      host.__tt = document.createElement('div');
      host.__tt.className = 'tt';
    }
    if (host.__tt.parentNode !== host) host.appendChild(host.__tt);
    if (!host.__bound) {
      host.__bound = true;
      host.addEventListener('pointerdown', function (e) {
        host.__pressed = true;
        hoverAt(win, e);
      });
      host.addEventListener('pointermove', function (e) {
        if (e.pointerType === 'mouse' || host.__pressed) hoverAt(win, e);
      });
      host.addEventListener('pointerup', function () { host.__pressed = false; });   // 觸控：放開後保留
      host.addEventListener('pointerleave', function (e) {
        if (e.pointerType === 'mouse') hideHover(win);
      });
      document.addEventListener('pointerdown', function (e) {                        // 點別處才收起來
        if (!host.contains(e.target)) hideHover(win);
      }, true);
    }
  }

  function hideHover(win) {
    var st = CHARTS[win.id];
    if (!st) return;
    var g = st.host.querySelector('g.cross-g');
    if (g) g.setAttribute('visibility', 'hidden');
    if (st.host.__tt) st.host.__tt.classList.remove('on');
  }

  function hoverAt(win, ev) {
    var st = CHARTS[win.id];
    if (!st || !st.hoverPts.length) return;
    var host = st.host, tt = host.__tt;
    var rect = host.getBoundingClientRect();
    var px = ev.clientX - rect.left, py = ev.clientY - rect.top;

    // 找最近的點；若附近 4px 內有離群點則優先吸附到它（空心圈半徑 3.4px，手指點得到，
    // 這樣「10/15 的 863 原始值」在手機上也查得到 —— 否則要精準命中 0.1px 內才看得到）
    var best = null, bestD = Infinity, outBest = null, outD = Infinity, i, d;
    for (i = 0; i < st.hoverPts.length; i++) {
      d = Math.abs(st.X(st.hoverPts[i].t) - px);
      if (d < bestD) { bestD = d; best = i; }
      if (st.hoverPts[i].o && d < outD) { outD = d; outBest = i; }
    }
    if (outBest !== null && outD <= 4) best = outBest;
    if (!best && best !== 0) return;
    var p = st.hoverPts[best], prev = best > 0 ? st.hoverPts[best - 1] : null;

    var g = host.querySelector('g.cross-g');
    var cx = st.X(p.t), cy = st.Y(p.v);
    if (g) {
      g.setAttribute('visibility', 'visible');
      var ln = g.querySelector('line.cross');
      ln.setAttribute('x1', cx.toFixed(1)); ln.setAttribute('x2', cx.toFixed(1));
      var dot = g.querySelector('circle.cross-dot');
      dot.setAttribute('cx', cx.toFixed(1)); dot.setAttribute('cy', cy.toFixed(1));
    }

    // 顯示的數值：離群點顯示原始值（主線畫的是平滑後的中位數）
    var shown = p.o ? p.raw : p.v;
    var prevV = prev ? (prev.o ? prev.raw : prev.v) : null;
    var diff = prev ? shown - prevV : null;
    var pct = prevV ? diff / prevV * 100 : null;
    var difftxt;
    if (diff === null) {
      difftxt = '';
    } else {
      // 台股慣例：▲ 紅／▼ 綠、四捨五入為 0 就不加箭頭；只上色，不寫正負號
      var dd = dirOf(diff, 0), dp = (pct === null || prevV === null) ? null : dirPct(pct);
      var dtxt = dd.cls === 'flat' ? '0' : dd.arrow + ' ' + num(dd.a);
      difftxt = '較前一筆 <span class="' + dd.cls + '">' + dtxt + '</span>' +
                (dp ? '（<span class="' + dp.cls + '">' + dp.txt + '</span>）' : '');
    }
    tt.innerHTML =
      '<div class="t1">' + fmtFull(p.t) + '</div>' +
      '<div class="v">' + num(shown) + ' 天幣/元</div>' +
      (difftxt ? '<div>' + difftxt + '</div>' : '') +
      '<div class="src">來源：' + (p.src === 'real' ? '實測' : '估算') +
      (p.n ? '（' + (st.aggLabel || '') + '中位數・' + p.n + ' 筆；區間 ' + num(p.lo) + '–' + num(p.hi) + '）'
           : (p.o ? '（離群點・原始值；主線為中位數 ' + num(p.v) + '）' : '')) + '</div>';
    tt.classList.add('on');

    var tw = tt.offsetWidth, th = tt.offsetHeight;
    var left = cx + 12;
    if (left + tw > st.w - 4) left = cx - tw - 12;
    if (left < 4) left = 4;
    var top = Math.min(Math.max(py - th - 10, 4), st.h - th - 4);
    tt.style.left = left.toFixed(1) + 'px';
    tt.style.top = top.toFixed(1) + 'px';
  }

  function fitLabels(win, st, depth) {
    var labels = st.host.querySelectorAll('text.xtick');
    if (labels.length < 3 || !st.host.clientWidth) return false;      // 面板隱藏時量不到，切換頁籤時再量
    var rects = [], i;
    for (i = 0; i < labels.length; i++) rects.push(labels[i].getBoundingClientRect());
    for (i = 1; i < rects.length; i++) {
      if (rects[i].left < rects[i - 1].right + 2) {                   // 有重疊
        if (depth >= 3) return false;
        win.__labelEvery = (st.plan.every || 1) * 2;
        win.__everyW = st.w;
        render(win, depth + 1);
        return true;
      }
    }
    return false;
  }

  /* ---------- 載入 / 重繪 ---------- */
  var DATA = null, CHARTS = {};
  function renderAll() { WINCFG.forEach(function (win) { render(win, 0); }); }

  function normalize(d) {                                 // 內嵌資料用精簡陣列
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
    // 保險 1：視窗尺寸變化也重繪（ResizeObserver 在部分環境不一定會通知）
    window.addEventListener('resize', function () { setTimeout(renderAll, 0); });
    // 保險 2：切換時間窗時面板由 display:none 變可見 → 立即用正確寬度重繪
    Array.prototype.forEach.call(document.querySelectorAll('input[name=tr]'), function (r) {
      r.addEventListener('change', function () { setTimeout(renderAll, 0); });
    });
    // 系統深／淺色切換 → 重繪（顏色雖然都走 CSS 變數，重繪可確保 tooltip 量測與尺寸一致）
    if (window.matchMedia) {
      var mq = window.matchMedia('(prefers-color-scheme: dark)');
      var onScheme = function () { setTimeout(renderAll, 0); };
      if (mq.addEventListener) mq.addEventListener('change', onScheme);
      else if (mq.addListener) mq.addListener(onScheme);
    }
  }

  window.DD373TREND = { boot: boot, renderAll: renderAll, render: render, charts: CHARTS,
                        // 供測試用
                        ticksFor: ticksFor, chooseStep: chooseStep, labelPlan: labelPlan,
                        maxLabelPx: maxLabelPx, tickGapPx: tickGapPx, mondayAnchorDay: mondayAnchorDay,
                        niceScale: niceScale, niceStep: niceStep, tickLabel: tickLabel,
                        fmtFull: fmtFull, num: num, TZOFF: TZOFF, STEPS: STEPS,
                        dirOf: dirOf, dirPct: dirPct };

  var WINCFG = window.DD373WINDOWS || [];
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
