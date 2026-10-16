#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C 階段：頁面結構改版（導覽列／大標／Hero／群組列表／全服排行 details／口徑說明／頁尾）
一次性轉換腳本，每一步都有 assert（改不到就大聲失敗）。"""
import io
import re

P = "build-page-next.py"
s = io.open(P, encoding="utf-8").read()
orig = s

# ============================================================================
# 1) 把舊的「全服表格 + me_block」整段換成：排行列 + 漲跌 helper + 近一月清單
#    （Hero／群組列表／details 需要 now_vw2、all_vw2，所以留到後面才組）
# ============================================================================
new_top = '''    # ---------- 漲跌／差值（一律文字＋▲▼–，不用 emoji） ----------
    def delta_pct(cur, old):
        """回傳百分比（float）或 None"""
        if not cur or not old:
            return None
        return (cur / old - 1) * 100

    def arrow_of(pct):
        """(css class, 文字) —— 幣值上升＝1 台幣買到更多金＝金變便宜"""
        if pct is None:
            return "flat", "—"
        if pct > 0.05:
            return "up", f"▲ +{pct:.1f}%"
        if pct < -0.05:
            return "down", f"▼ −{abs(pct):.1f}%"
        return "flat", "– 0.0%"

    def gold_dir(pct):
        """方向用文字講（不用紅綠）：金變便宜／金變貴／持平"""
        if pct is None:
            return "—"
        if pct > 0.05:
            return "金變便宜"
        if pct < -0.05:
            return "金變貴"
        return "持平"

    prev_by_name = {x["server"]: x for x in (prev["servers"] if prev else [])}

    # ---------- 全服排行列（飛馬置頂、其餘依 ② 由大到小；第 2 行放細節） ----------
    rank_of = {srv["server"]: i for i, srv in enumerate(by_vw, 1)}
    n_servers = len(by_vw)
    me = next((x for x in by_vw if x["server"] == USER_SERVER), None)
    order = ([me] if me else []) + [x for x in by_vw if x is not me]
    rank_rows = []
    for srv in order:
        o = prev_by_name.get(srv["server"])
        pct = delta_pct(srv["vw2_gold_per_twd"], o["vw2_gold_per_twd"] if o else None)
        _cls, dl_txt = arrow_of(pct)
        hi = " hi" if (pct is not None and abs(pct) >= 2) else ""      # 只有 |Δ|≥2% 才強調
        is_me = (srv is me)
        rank_rows.append(
            f'<div class="rrow{" me" if is_me else ""}">'
            f'<div class="rline1"><span class="rk">{rank_of[srv["server"]]}</span>'
            f'<span class="nm">{html.escape(srv["server"])}</span>'
            f'<span class="vw">{srv["vw2_gold_per_twd"]:,.0f}</span>'
            f'<span class="dl{hi}">{dl_txt}</span></div>'
            f'<div class="rline2">① {srv["low_gold_per_twd"]:,.0f} ・ 變現 {srv["sell_gold_per_twd"] or "—"}'
            f' ・ 吃 {srv["used"]} 筆 ・ 掛單 {srv["stock_wj"] / 10000:.2f} 億'
            f' ・ 買 5,000 萬金 ≈ NT${nt5000(srv):,}</div></div>')

    by_sell = sorted([x for x in ok if x.get("sell_gold_per_twd")], key=lambda x: x["sell_gold_per_twd"])
    movers_up = [x for x in deltas if x[3] > 0][-4:][::-1]      # 幣值上升＝金變便宜
    movers_dn = deltas[:4]                                      # 幣值下降＝金變貴

    def mover_list(items, sign):
        out = []
        for nm, e, l, p in items:
            out.append(f"<li><b>{html.escape(nm)}</b> {p:+.1f}%"
                       f"<span class='muted'>（1 台幣 {e:,.0f} → {l:,.0f} 金/元）</span></li>")
        return f"<ul class='movers {sign}'>{''.join(out)}</ul>"

'''
m = re.search(r"    # 全服表格\n.*?\n(?=    s_now = load_vw2_series\(24,)", s, re.S)
assert m, "找不到舊的『全服表格 + me_block』段落"
s = s[:m.start()] + new_top + s[m.end():]

# 舊程式裡 me／delta/arrow 的重複定義要拿掉（新版已自帶）
old_dup = '''    me = next((s for s in ok if s["server"] == USER_SERVER), None)
    pm = None
    if prev:
        pm = next((s for s in prev["servers"] if s["server"] == USER_SERVER), None)

    def delta(cur, old):
        if not cur or not old:
            return "—"
        return f"{(cur / old - 1) * 100:+.1f}%"

    def arrow(cur, old, invert=False):
        """invert=True 代表數字越小越好（例如台幣成本）→ 下降顯示綠色。"""
        if not cur or not old:
            return "flat", "—"
        v = (cur / old - 1) * 100
        good = (v < 0) if invert else (v > 0)
        mark = "⬆" if v > 0.15 else ("⬇" if v < -0.15 else "➖")
        cls = "flat" if abs(v) <= 0.15 else ("up" if good else "down")
        return cls, f"{mark} {v:+.1f}%"

'''
assert old_dup in s, "找不到舊的 me/delta/arrow 定義"
s = s.replace(old_dup, "")

# （舊的 by_sell 重複排序已在上面第一段替換時一起移除，不必再處理）

# ============================================================================
# 2) trend_block：去掉 h2 標題與底部口徑行；近一月拆成獨立 movers_block
# ============================================================================
old = '''    trend_block = f"""
<section class="card">
  <h2>飛馬走勢<span>四個時間窗（點標籤切換）｜同一把尺＝② 買滿 5,000 萬金加權幣值</span></h2>
  <div class="tabs">'''
assert old in s, "找不到 trend_block 開頭"
s = s.replace(old, '''    trend_block = f"""
<section class="card lg">
  <div class="tabs">''')

old = '''  <div class="notes">
    <div class="muted">同一把尺：② 買滿 5,000 萬金加權（1 台幣 ≈ N 金，越高＝金越便宜）；虛線＝估算 ②（舊的實買序列 × 比率 {r_txt} 換算，非實測）。完整方法論見頁尾「口徑說明」。</div>
  </div>
</section>

<section class="card">
  <h2>近一個月各服 ② 幣值變化<span>9/17~9/29 均（估算）vs 最近 3 天均（實測優先、不足處以估算補）｜同一把尺＝② 買滿 5,000 萬金加權</span></h2>
  <div class="split">
    <div><h3 class="up">金變便宜（1 元買到更多金）</h3>{mover_list(movers_up, 'up')}</div>
    <div><h3 class="down">金變貴（1 元買到更少金）</h3>{mover_list(movers_dn, 'down')}</div>
  </div>
</section>"""'''
assert old in s, "找不到 trend_block 尾端與近一月段落"
s = s.replace(old, '''</section>"""

    movers_block = f"""
<section class="card">
  <h2>移民期均值 vs 近 3 天<span>基準期 9/17~9/29＝第 2 次移民期（估算）／近 3 天（實測為主）｜同一把尺＝② 買滿 5,000 萬金加權</span></h2>
  <div class="split">
    <div><h3>金變便宜（1 元買到更多金）</h3>{mover_list(movers_up, 'up')}</div>
    <div><h3>金變貴（1 元買到更少金）</h3>{mover_list(movers_dn, 'down')}</div>
  </div>
</section>"""''')

# ============================================================================
# 3) 新增頁面區塊（Hero／群組列表／排行 details／口徑說明）——放在 chart_block 之後
# ============================================================================
anchor = "        + ';</script>\\n<script>\\n' + trend_js_src + '\\n</script>')\n"
assert anchor in s, "找不到 chart_block 結尾"
blocks = '''

    # ---------- Hero 卡 ----------
    chg24 = None
    if me and now_vw2:
        cut = t_now - dt.timedelta(hours=24)
        prev_pt = next((p for p in reversed(all_vw2) if p[0] <= cut), None)
        if prev_pt and prev_pt[1]:
            chg24 = (now_vw2 / prev_pt[1] - 1) * 100
    chg_line = (f"{gold_dir(chg24)} {abs(chg24):.1f}%（24 小時）" if chg24 is not None else "24 小時變化：資料不足")
    thin_tag = '<span class="tag">薄市場</span>' if (me and me["stock_wj"] < 20000) else ""
    hero_block = ""
    glist_block = ""
    ranking_block = ""
    if me:
        rank = rank_of[me["server"]]
        hero_block = f"""
<section class="card lg hero">
  <div class="hero-n"><b>{me['vw2_gold_per_twd']:,.0f}</b><span>金/元</span></div>
  <p class="hero-l">1 台幣買 {me['vw2_gold_per_twd']:,.0f} 金/元 · {chg_line}</p>
  <div class="hero-3">
    <div><label>最低一筆</label><b>{me['low_gold_per_twd']:,.0f}</b></div>
    <div><label>變現</label><b>{me['sell_gold_per_twd'] or '—'}</b></div>
    <div><label>掛單</label><b>{me['stock_wj'] / 10000:.2f} 億</b> {thin_tag}</div>
  </div>
</section>"""
        glist_block = f"""
<section class="card">
  <div class="glist">
    <div class="grow"><span>買 5,000 萬金</span><span class="gv"><b>≈ NT${nt5000(me):,}</b></span></div>
    <div class="grow"><span>掛單深度</span><span class="gv">總量 <b>{me['stock_wj'] / 10000:.2f} 億</b>／買滿 5,000 萬金要吃 <b>{me['used']}</b> 筆</span></div>
    <div class="grow"><span>全服排名</span><span class="gv"><b>第 {rank}／{n_servers} 名</b> <span class="tag">移民參考</span></span></div>
  </div>
</section>"""
        ranking_block = f"""
<section class="card">
  <details class="dsc">
    <summary><span>全服排行</span><span class="dsub">飛馬第 {rank}／{n_servers} 名</span></summary>
    <div class="dbody glist">{''.join(rank_rows)}</div>
  </details>
</section>"""

    # ---------- 口徑說明（全頁方法論只在這裡出現一次） ----------
    method_block = f"""
<section class="card">
  <details class="dsc">
    <summary><span>口徑說明</span><span class="dsub">方法論</span></summary>
    <div class="dbody method">
      <p class="mt">同一把尺：② 買滿 5,000 萬金加權</p>
      <p><b>② 買滿 5,000 萬金加權（本頁主數字）</b>＝從最便宜的掛單往上吃、累積到 5,000 萬金為止的加權平均單價，換算成 <b>1 台幣 ≈ N 金/元</b>（數字越大＝天幣越便宜）。</p>
      <p><b>① 最低一筆</b>＝全檔位最便宜的單筆單價（小額真的買得到的價，單位同為金/元）。</p>
      <p><b>變現</b>＝DD373 收購區掛單（數字越小＝賣金換到越多台幣）。</p>
      <p class="mt">掛單深度：筆數定義</p>
      <p><b>n 筆</b>＝抓到的掛單則數（已去重，並濾掉起售 &lt;50 萬金、單價 &lt;0.5 或 &gt;8 元/萬金的異常卡）。</p>
      <p><b>有效 n 筆</b>＝其中庫存 ≥1,000 萬金的現貨筆數（大額買得到的部分）。</p>
      <p><b>吃筆</b>＝從最便宜的掛單往上吃到 5,000 萬金為止、總共吃掉的筆數（最後一筆可能只吃一部分）。</p>
      <p class="mt">取樣與更新</p>
      <p>全服 22 台每小時掃 1 次；飛馬另外每 15 分鐘輕掃 1 次（只影響飛馬數字與告警）。時間一律 Asia/Taipei。</p>
      <p>台幣金額＝② 加權幣值換算（買 5,000 萬金的成本），匯率 CNY→TWD {fx:.4f}（產生時）。</p>
      <p class="mt">估算段（灰色虛線）</p>
      <p>實測 ② 從 10/14 19:01 開始記錄；更早的資料用舊的「實買」序列 × 比率 {r_txt} 換算（比率取自兩者同時段的實測配對，誤差 &lt;1%）。估算段的趨勢可信，但<b>絕對值只能當參考</b>，不要與實測逐點比大小。</p>
      <p class="mt">薄市場</p>
      <p>掛單總量 &lt;2 億金時，單一賣家就能推動數字 → 一律標「薄市場」，看趨勢不要看單點。</p>
      <p class="mt">排名的用途</p>
      <p>平時天幣不能跨服，全服排名只在<b>移民時</b>有意義（移民參考）。</p>
      <p class="mt">資料來源</p>
      <p>DD373 天幣掛單（天堂經典版台服），本次掃到 {len(ok)}／{len(d['servers']) + len(d.get('errs', []))} 台。</p>
    </div>
  </details>
</section>"""
    bo_block = (f'<div class="notice">掃描暫時退避中（疑似被 DD373 擋）：'
                f'{bo["until"][:16].replace("T", " ")} 之後自動恢復，頁面先顯示上次資料</div>' if bo else '')
'''
s = s.replace(anchor, anchor + blocks)

# ============================================================================
# 4) html_doc：換成 C 階段的結構（導覽列 → 大標 → Hero → 走勢 → 群組列表 → 排行 → 近一月 → 口徑 → 頁尾）
# ============================================================================
m = re.search(r'    rows_html = "".join\(rows\).*?\n</html>"""', s, re.S)
assert m, "找不到舊的 html_doc 段落"
new_doc = '''    html_doc = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#F2F2F7" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#000000" media="(prefers-color-scheme: dark)">
<meta name="robots" content="noindex">
<title>飛馬珀伽索斯 天幣幣值（DD373）— 預覽版</title>
<style>
{apple_css}
.notice {{
  margin: 0 0 16px; padding: 12px; border-radius: 12px;
  background: var(--warn-soft); color: var(--warn); font-size: 13px;
}}
</style>
</head>
<body>
<div class="preview-banner">預覽版・資料停在 {me_ts:%m/%d %H:%M}（<span data-ts="{ep(me_ts)}" data-rel="1">{me_age} 分鐘前</span>）　正式版：<a href="./index.html">index.html</a></div>
<nav class="nav">
  <span class="nav-t">天幣幣值</span>
  <span class="capsule{fresh_cls}"><span class="d"></span><span data-ts="{ep(me_ts)}" data-rel="1">{fresh_txt}</span></span>
</nav>
{bo_block}
<header class="page-head">
  <h1>飛馬珀伽索斯</h1>
  <p class="sub">DD373 · 買滿 5,000 萬金加權</p>
</header>
{hero_block}
{trend_block}
{glist_block}
{ranking_block}
{movers_block}
{method_block}
<footer>
  產生時間 {now:%Y/%m/%d %H:%M:%S} ｜ 資料來源：DD373 天幣掛單（天堂經典版台服）｜ 匯率 CNY→TWD {fx:.4f}<br>
  平台掛單價格隨時變動，本頁為取樣時點的快照；短時間內個別服可跳 ±10%，單一快照別當趨勢。
</footer>
{chart_block}
</body>
</html>"""'''
s = s[:m.start()] + new_doc + s[m.end():]

io.open(P, "w", encoding="utf-8").write(s)
print("build-page-next.py C 階段轉換完成：", len(orig), "->", len(s), "bytes")
