#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B 階段：把 build-page-next.py 的樣式換成 static/apple.css，並改走勢卡的圖例／摘要標記。
一次性轉換腳本（跑完就留著當紀錄）。每一步都有 assert，改不到就大聲失敗。"""
import io
import re
import sys

P = "build-page-next.py"
s = io.open(P, encoding="utf-8").read()
orig = s

# ---------- 1) 常數：apple.css ----------
old = 'TREND_JS = os.path.join(PAGE_DIR, "static", "trend-next.js")'
assert old in s, "找不到 TREND_JS"
s = s.replace(old, old + '\nAPPLE_CSS = os.path.join(PAGE_DIR, "static", "apple.css")   # 蘋果風設計系統（build 時內嵌）')
s = s.replace('''設）   # 蘋果風設計系統（build 時內嵌）''', '設）   # 蘋果風設計系統（build 時內嵌）')

# ---------- 2) <style>…</style> 整段換成 {apple_css} ----------
m = re.search(r"<style>\n.*?\n</style>", s, re.S)
assert m, "找不到 <style> 區塊"
s = s[:m.start()] + "<style>\n{apple_css}\n</style>" + s[m.end():]

# ---------- 3) build() 裡讀 apple.css ----------
old = "    trend_js_src = open(TREND_JS, encoding=\"utf-8\").read()"
assert old in s, "找不到 trend_js_src"
s = s.replace(old, "    apple_css = open(APPLE_CSS, encoding=\"utf-8\").read()\n" + old)

# ---------- 4) chart_windows 拿掉 color（顏色一律由 CSS 變數決定） ----------
s2 = re.sub(r', "color": "#[0-9a-fA-F]{6}"', "", s)
assert s2 != s, "chart_windows 沒有 color 可以拿掉"
s = s2

# ---------- 5) panel() 改成 wsum + chart（圖例改放分段控制下方同一行） ----------
old_panel = '''    def panel(win_id, cls, dot, legend, npts):
        if npts < 2:
            return (f'<div class="panel {cls}"><p class="muted">這個時間窗還在累積（目前 {npts} 點，'
                    f'② 口徑自 {all_vw2[0][0]:%m/%d %H:%M} 開始記錄）</p><div class="legend">{legend}</div></div>')
        return (f'<div class="panel {cls}"><div class="chart" data-win="{win_id}"></div>'
                f'<div class="legend"><span class="dot {dot}"></span>{legend}</div></div>')'''
assert old_panel in s, "找不到 panel()"
new_panel = '''    def panel(win_id, cls, npts):
        """一個時間窗＝本時窗摘要＋圖（圖例在分段控制下方同一行）"""
        if npts < 2:
            return (f'<div class="panel {cls}"><p class="muted">這個時間窗還在累積（目前 {npts} 點，'
                    f'② 口徑自 {all_vw2[0][0]:%m/%d %H:%M} 開始記錄）</p></div>')
        return (f'<div class="panel {cls}"><div class="wsum" data-win="{win_id}"></div>'
                f'<div class="chart" data-win="{win_id}"></div></div>')'''
s = s.replace(old_panel, new_panel)

# ---------- 6) 圖例列 + 四個 panel 呼叫 ----------
old_panels = re.search(r"    <div class=\"panels\">\n(?:      \{panel\(.*?\n)+    </div>", s)
assert old_panels, "找不到 panels 區塊"
s = s[:old_panels.start()] + '''    <div class="lgd">
      <span class="li"><svg width="20" height="8" viewBox="0 0 20 8" aria-hidden="true"><path class="lg-real" d="M1,4 H19"/></svg>實測</span>
      <span class="li"><svg width="20" height="8" viewBox="0 0 20 8" aria-hidden="true"><path class="lg-est" d="M1,4 H19"/></svg>估算</span>
    </div>
    <div class="panels">
      {panel("24h", "p24", n24)}
      {panel("48h", "p48", n48)}
      {panel("7d", "p7", n7d)}
      {panel("30d", "p30", n30d)}
    </div>''' + s[old_panels.end():]

# ---------- 7) 底部長說明 → 一行口徑（完整方法論留到 C 階段的「口徑說明」details） ----------
# 只認「走勢卡」那個 notes（用 📏 那行當錨），不要動到 KPI 卡／全服排行的 notes
old_notes = re.search(r'  <div class="notes">\n    <div>📏.*?\n  </div>', s, re.S)
assert old_notes, "找不到走勢卡的 notes 區塊"
s = s[:old_notes.start()] + '''  <div class="notes">
    <div class="muted">同一把尺：② 買滿 5,000 萬金加權（1 台幣 ≈ N 金，越高＝金越便宜）；虛線＝估算 ②（舊的實買序列 × 比率 {r_txt} 換算，非實測）。完整方法論見頁尾「口徑說明」。</div>
  </div>''' + s[old_notes.end():]

# ---------- 8) 頁首：狀態膠囊 ／ KPI：薄市場 tag ----------
old_h1 = '  <h1>天堂經典版 天幣幣值</h1>'
assert old_h1 in s, "找不到 h1"
s = s.replace(old_h1, '''  <div class="hrow"><h1>天堂經典版 天幣幣值</h1>
    <span class="capsule{fresh_cls}"><span class="d"></span>{fresh_txt}</span></div>''')

old_kpi = '''    <div class="kpi"><label>掛單總量</label><b>{me['stock_wj'] / 10000:.2f} 億</b><i>{me['n']} 筆／有效 {me['n_eff']} 筆</i></div>'''
assert old_kpi in s, "找不到 掛單總量 KPI"
s = s.replace(old_kpi, '''    <div class="kpi"><label>掛單總量</label><b>{me['stock_wj'] / 10000:.2f} 億</b><i>{me['n']} 筆／有效 {me['n_eff']} 筆{'　<span class="tag">薄市場</span>' if me['stock_wj'] < 20000 else ''}</i></div>''')

# ---------- 9) 計算膠囊文字（me_age 已經算好） ----------
old_age = "    me_age = int((now - me_ts).total_seconds() // 60)"
assert old_age in s, "找不到 me_age"
s = s.replace(old_age, old_age + '''
    if me_age <= 30:                     # D 階段會改成每 60 秒由 JS 重算
        fresh_cls, fresh_txt = "", f"{me_age} 分鐘前"
    elif me_age <= 60:
        fresh_cls, fresh_txt = " warn", "資料延遲"
    else:
        fresh_cls, fresh_txt = " bad", f"資料停更 {me_age} 分鐘"''')

# ---------- 10) 預覽橫幅改吃 CSS 變數（.preview-banner 已在 apple.css 定義） ----------
old_banner = '''<div class="preview-banner">預覽版・資料停在 {me_ts:%m/%d %H:%M}　（正式版：<a href="./index.html">index.html</a>）</div>'''
assert old_banner in s, "找不到 preview-banner"
s = s.replace(old_banner, '''<div class="preview-banner">預覽版・資料停在 {me_ts:%m/%d %H:%M}（<span data-ts="{ep(me_ts)}" data-rel="1">{me_age} 分鐘前</span>）　正式版：<a href="./index.html">index.html</a></div>''')

io.open(P, "w", encoding="utf-8").write(s)
print("build-page-next.py 轉換完成：", len(orig), "->", len(s), "bytes")
