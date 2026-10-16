/* ============================================================================
   天幣幣值頁 — 行為層（D 階段）
     1) 時間會動：所有 [data-ts] 每 60 秒重算「N 分鐘前」；膠囊依新鮮度轉 --warn／--bad，
        超過 60 分鐘另外在頁首顯示「資料已停更」橫幅
     2) 分頁可見時每 5 分鐘檢查 version.json：有新資料才整頁重新載入
        三個保護：① built 真的變新 ② 最近 60 秒有操作就不打斷 ③ tooltip 開著就不打斷
     3) 記住上次選的時間窗（localStorage）；details 展開狀態與捲動位置（sessionStorage）
     4) 預覽版專用測試參數（正式版沒有 window.DD373_PREVIEW，一律忽略）
          ?now=2026-10-17T04:00   假裝現在時間（驗「資料延遲」膠囊與「停更」橫幅）
          ?poll=15                把 5 分鐘檢查改成 15 秒
   ========================================================================== */
(function () {
  'use strict';

  var PREVIEW = !!window.DD373_PREVIEW;
  var Q = {};
  try { Q = new URLSearchParams(location.search); } catch (e) { }
  function qget(k) { try { return Q.get(k); } catch (e) { return null; } }

  // ---------- 現在時間（預覽可用 ?now= 假造） ----------
  function previewNowMs() {
    if (!PREVIEW) return null;
    var v = qget('now');
    if (!v) return null;
    var ms = Date.parse(v.length <= 16 ? v + '+08:00' : v);       // 沒帶時區就當台北時間
    return isNaN(ms) ? null : ms;
  }
  var FAKE_NOW = previewNowMs();
  function nowMs() { return FAKE_NOW || Date.now(); }
  function pollMs() {
    var s = PREVIEW ? parseInt(qget('poll') || '0', 10) : 0;
    return s > 0 ? s * 1000 : 5 * 60 * 1000;
  }

  /* ---------- 1) 相對時間／新鮮度 ---------- */
  function p2(n) { return (n < 10 ? '0' : '') + n; }
  function tp(ms) {
    var d = new Date(ms + 8 * 3600 * 1000);                       // 台北時間
    return { M: d.getUTCMonth() + 1, D: d.getUTCDate(), h: d.getUTCHours(), m: d.getUTCMinutes() };
  }
  function fmtAbs(tsSec) { var p = tp(tsSec * 1000); return p2(p.M) + '/' + p2(p.D) + ' ' + p2(p.h) + ':' + p2(p.m); }
  function ageMin(tsSec) { return Math.floor((nowMs() - tsSec * 1000) / 60000); }
  function relText(min) {
    if (min < 1) return '剛剛';
    if (min < 60) return min + ' 分鐘前';
    var h = Math.floor(min / 60), m = min % 60;
    return h + ' 小時' + (m ? ' ' + m + ' 分鐘' : '') + '前';
  }
  function durText(min) {                                          // 經過時間（不帶「前」）
    if (min < 1) return '不到 1 分鐘';
    if (min < 60) return min + ' 分鐘';
    var h = Math.floor(min / 60), m = min % 60;
    return h + ' 小時' + (m ? ' ' + m + ' 分鐘' : '');
  }
  function freshState(min) {                                       // 資料新鮮度：ok／warn／bad
    if (min <= 30) return { cls: '', txt: relText(min) };
    if (min <= 60) return { cls: 'warn', txt: '資料延遲' };
    return { cls: 'bad', txt: '已停更 ' + durText(min) };
  }

  function updateTimes() {
    var nodes = document.querySelectorAll('[data-ts][data-rel="1"]'), i, el, min;
    for (i = 0; i < nodes.length; i++) {
      el = nodes[i];
      el.textContent = relText(ageMin(parseInt(el.getAttribute('data-ts'), 10)));
    }
    var cap = document.querySelector('[data-fresh]');
    if (!cap) return;
    var src = cap.querySelector('[data-ts]');
    if (!src) return;
    var ts = parseInt(src.getAttribute('data-ts'), 10);
    min = ageMin(ts);
    var st = freshState(min);
    cap.className = 'capsule' + (st.cls ? ' ' + st.cls : '');
    src.textContent = st.txt;
    var ban = document.getElementById('stale-banner');
    if (ban) {
      if (st.cls === 'bad') {
        ban.textContent = '已停更 ' + durText(min) + '（最後一筆 ' + fmtAbs(ts) + '）';
        ban.hidden = false;
      } else {
        ban.hidden = true;
      }
    }
  }

  /* ---------- 2) 自動更新（有新資料才 reload，且不打斷使用者） ---------- */
  var MY_BUILT = (function () {
    var b = window.DD373_BUILT;
    if (!b) return 0;
    var ms = Date.parse(b.length <= 16 ? b + '+08:00' : b);
    return isNaN(ms) ? 0 : ms;
  })();
  var lastInteract = Date.now();
  ['pointerdown', 'pointermove', 'pointerup', 'keydown', 'wheel', 'touchstart', 'scroll'].forEach(function (ev) {
    document.addEventListener(ev, function () { lastInteract = Date.now(); }, { passive: true });
  });
  function tooltipOpen() { return !!document.querySelector('.tt.on'); }
  function shouldReload(newBuiltMs, now, lastInt, ttOpen) {
    if (!newBuiltMs || !MY_BUILT) return false;
    if (newBuiltMs <= MY_BUILT) return false;        // 沒有新資料 → 不 reload
    if (ttOpen) return false;                        // tooltip 開著 → 不打斷
    if (now - lastInt < 60000) return false;         // 最近 60 秒有操作 → 延到下一輪
    return true;
  }
  function checkVersion() {
    return fetch('version.json?t=' + Date.now(), { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        if (!j || !j.built) return false;
        var nb = Date.parse(j.built.length <= 16 ? j.built + '+08:00' : j.built);
        if (shouldReload(nb, Date.now(), lastInteract, tooltipOpen())) { location.reload(); return true; }
        return false;
      })
      .catch(function () { return false; });
  }

  /* ---------- 3) 狀態記住／還原 ---------- */
  var K_TAB = 'dd373.tab', K_DET = 'dd373.details', K_SCR = 'dd373.scroll';
  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { } }
  function ssGet(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } }
  function ssSet(k, v) { try { sessionStorage.setItem(k, v); } catch (e) { } }

  function winOfTabId(id) { return id.replace(/^tr/, '') + (id === 'tr7' || id === 'tr30' ? 'd' : 'h'); }
  function restoreTab() {
    var v = lsGet(K_TAB) || '24h';                                 // 讀不到就用 24 小時
    var el = document.getElementById('tr' + v.replace(/[hd]$/, ''));
    if (!el) el = document.getElementById('tr24');
    if (el) el.checked = true;
  }
  function bindTab() {
    Array.prototype.forEach.call(document.querySelectorAll('input[name=tr]'), function (r) {
      r.addEventListener('change', function () { if (r.checked) lsSet(K_TAB, winOfTabId(r.id)); });
    });
  }
  function bindDetails() {
    var ds = document.querySelectorAll('details.dsc');
    var saved = ssGet(K_DET);
    if (saved) {
      try {
        var arr = JSON.parse(saved) || [];
        Array.prototype.forEach.call(ds, function (d, i) { if (arr[i]) d.open = true; });
      } catch (e) { }
    }
    Array.prototype.forEach.call(ds, function (d) {
      d.addEventListener('toggle', function () {
        var cur = [];
        Array.prototype.forEach.call(ds, function (x) { cur.push(x.open ? 1 : 0); });
        ssSet(K_DET, JSON.stringify(cur));
      });
    });
  }
  function bindScroll() {
    var t = null;
    window.addEventListener('scroll', function () {
      if (t) return;
      t = setTimeout(function () {
        ssSet(K_SCR, String(Math.round(window.scrollY || 0)));
        t = null;
      }, 250);
    }, { passive: true });
  }
  function restoreScroll() {
    var v = ssGet(K_SCR);
    if (v === null) return;
    var y = parseInt(v, 10);
    if (!isNaN(y) && y > 0) setTimeout(function () { window.scrollTo(0, y); }, 0);
  }

  /* ---------- 圖例說明泡泡（滑鼠 hover／手機點一下） ---------- */
  function bindTips() {
    var tip = null, cur = null, shownAt = 0;
    function hide() {
      if (tip && tip.parentNode) tip.parentNode.removeChild(tip);
      tip = null; cur = null;
    }
    function show(el) {
      if (cur === el) return;
      hide();
      cur = el;
      tip = document.createElement('div');
      tip.className = 'lgtip';
      tip.textContent = el.getAttribute('data-tip');
      document.body.appendChild(tip);
      var r = el.getBoundingClientRect(), w = tip.offsetWidth;
      tip.style.left = Math.max(8, Math.min(r.left, document.documentElement.clientWidth - w - 8)) + 'px';
      tip.style.top = (r.bottom + (window.scrollY || 0) + 8) + 'px';
      shownAt = Date.now();
    }
    function target(e) {
      return (e.target && e.target.closest) ? e.target.closest('[data-tip]') : null;
    }
    document.addEventListener('mouseover', function (e) {          // 滑鼠：移上去顯示、移開收起
      var el = target(e);
      if (el) show(el); else hide();
    }, true);
    document.addEventListener('click', function (e) {              // 手機：點一下開／關
      var el = target(e);
      if (!el) { hide(); return; }
      if (el.closest && el.closest('summary')) {                    // 泡泡在 <summary> 裡時，別讓 details 跟著開合
        e.preventDefault();
        e.stopPropagation();
      }
      // 手機 tap 會先來一個合成 mouseover（已顯示）→ 350ms 內不再 toggle，避免一閃就消失
      if (cur === el && Date.now() - shownAt > 350) hide(); else show(el);
    });
    window.addEventListener('scroll', hide, { passive: true });
  }

  /* ---------- 啟動 ---------- */
  function boot() {
    restoreTab();
    bindTab();
    bindDetails();
    bindScroll();
    updateTimes();
    bindTips();
    if (window.DD373TREND && window.DD373TREND.renderAll) window.DD373TREND.renderAll();   // 還原頁籤後用正確寬度重繪
    restoreScroll();
    setInterval(updateTimes, 60 * 1000);                            // 相對時間每 60 秒重算
    setInterval(function () {                                       // 分頁可見時才檢查
      if (document.visibilityState === 'visible') checkVersion();
    }, pollMs());
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'visible') { updateTimes(); checkVersion(); }
    });
  }

  window.DD373PAGE = { restoreTab: restoreTab, bindTips: bindTips, shouldReload: shouldReload, checkVersion: checkVersion, updateTimes: updateTimes,
                       relText: relText, durText: durText, freshState: freshState, ageMin: ageMin, nowMs: nowMs,
                       pollMs: pollMs, builtMs: function () { return MY_BUILT; },
                       setBuilt: function (v) { MY_BUILT = v; },
                       preview: PREVIEW, fakeNow: FAKE_NOW };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
