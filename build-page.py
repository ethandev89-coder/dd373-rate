#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DD373 天幣幣價網頁 builder（純腳本、0 token）

流程：
  1) 抓取：22 台 x 3 排序（走 dd373-rate-query skill 的 lt_rate_v2.server_rate）→ ①/②/成本/變現
  2) 讀歷史：~/.hermes/data/dd373-multiserver-history.json（cron 每 5 小時寫入）→ 趨勢圖（實買口徑）
  3) 產出：public/index.html（單檔、內嵌 CSS/SVG、手機優先）+ data/page-data.json
  4) 發佈：publish.sh（git commit + push → GitHub Pages）

用法：
  python3 build-page.py            # 正常：抓新的（若快取 <CACHE_MIN 分鐘則沿用）
  python3 build-page.py --no-fetch # 只用快取／歷史（不連 DD373，離線測試用）
"""
import argparse
import html
import json
import os
import sys
import time
import datetime as dt

HOME = os.path.expanduser("~")
PAGE_DIR = os.path.join(HOME, ".hermes", "dd373-page")
DATA_DIR = os.path.join(PAGE_DIR, "data")
PUBLIC_DIR = os.path.join(PAGE_DIR, "docs")
HISTORY = os.path.join(HOME, ".hermes", "data", "dd373-multiserver-history.json")
SWEEP_LATEST = os.path.join(DATA_DIR, "sweep-latest.json")
SWEEP_HISTORY = os.path.join(DATA_DIR, "sweep-history.json")
CACHE_MIN = 45          # 快取新鮮度（分鐘）：build 比 sweep 跑得頻繁時不重抓
USER_SERVER = "飛馬珀伽索斯"
TARGET_WJ = 5000.0      # ② 口徑固定：買滿 5,000 萬金

sys.path.insert(0, os.path.join(HOME, ".hermes", "scripts"))
from lt_rate_v2 import server_rate       # noqa: E402
from lt_cashrate import fetch_fx         # noqa: E402

# 檔名帶減號，用 importlib 載入 skill 內的 sweep 工具
import importlib.util                    # noqa: E402
_SWEEP_PY = os.path.join(HOME, ".hermes", "skills", "gaming", "dd373-rate-query", "scripts",
                         "dd373-all-sweep.py")
_spec = importlib.util.spec_from_file_location("dd373_all_sweep", _SWEEP_PY)
_sweep_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_sweep_mod)
load_scan_module, sweep = _sweep_mod.load_scan_module, _sweep_mod.sweep


# ---------- 抓取 ----------
def fetch_sweep(force=False):
    os.makedirs(DATA_DIR, exist_ok=True)
    if not force and os.path.exists(SWEEP_LATEST):
        age = time.time() - os.path.getmtime(SWEEP_LATEST)
        if age < CACHE_MIN * 60:
            d = json.load(open(SWEEP_LATEST, encoding="utf-8"))
            d["cached"] = True
            print(f"[cache] 沿用 {int(age/60)} 分鐘前的掃描結果（{len(d['servers'])} 台）", flush=True)
            return d

    scan = load_scan_module()
    fx = fetch_fx()
    tokens = dict(scan.TOKENS)
    tokens.update({k: v for k, v in scan.discover_servers().items() if k not in scan.SKIP})
    out, errs = [], []
    for name, tok in tokens.items():
        try:
            m = server_rate(tok, fx)
            if m.get("err"):
                errs.append([name, m["err"]])
                continue
            rows = m["rows"]
            sw = sweep(rows, TARGET_WJ)
            out.append({
                "server": name, "tok": tok, "fx": fx,
                "low_rmb": rows[0][0],
                "low_gold_per_twd": round(10000 / (rows[0][0] * fx), 1),
                "low_wj": rows[0][1],
                "vw2_gold_per_twd": round(10000 / (sw["vw_rmb"] * fx), 1) if sw else None,
                "cost_twd_5000w": round(sw["cost_rmb"] * fx) if sw else None,
                "used": sw["used"] if sw else None,
                "sell_gold_per_twd": m.get("sell_gold_per_twd"),
                "stock_wj": m["stock_wj"],
                "n": m["n"], "n_eff": m["n_effective"],
                "floor_eff_gold": m["gold_floor"],
                "top": rows[:6],
            })
            print(f"[ok] {name}: ①{out[-1]['low_gold_per_twd']} ②{out[-1]['vw2_gold_per_twd']} used={out[-1]['used']}", flush=True)
        except Exception as e:                                    # 單台失敗不致命
            errs.append([name, f"{type(e).__name__}:{e}"[:80]])
            print(f"[err] {name}: {e}", flush=True)
        time.sleep(1.0)                                           # 對來源禮貌

    d = {"fx": fx, "ts": dt.datetime.now().isoformat(timespec="seconds"),
         "servers": out, "errs": errs, "cached": False}
    json.dump(d, open(SWEEP_LATEST, "w"), ensure_ascii=False, indent=1)
    hist = json.load(open(SWEEP_HISTORY, encoding="utf-8")) if os.path.exists(SWEEP_HISTORY) else []
    hist.append({k: d[k] for k in ("ts", "fx", "servers")})
    json.dump(hist[-400:], open(SWEEP_HISTORY, "w"), ensure_ascii=False)
    print(f"[sweep] {len(out)}/{len(tokens)} 台，errs={len(errs)}，寫入 {SWEEP_LATEST}", flush=True)
    return d


def prev_sweep():
    """回傳上一次掃描（給「vs 上次」用）。"""
    if not os.path.exists(SWEEP_HISTORY):
        return None
    hist = json.load(open(SWEEP_HISTORY, encoding="utf-8"))
    return hist[-2] if len(hist) >= 2 else None


# ---------- 歷史序列（cron 實買口徑，趨勢用） ----------
def load_series(server=USER_SERVER, days=30):
    d = json.load(open(HISTORY, encoding="utf-8"))
    pts = []
    for r in d["runs"]:
        s = r["servers"].get(server)
        if not s or not s.get("gold_floor"):
            continue
        pts.append((dt.datetime.fromisoformat(r["ts"]), s["gold_floor"],
                    s.get("sell_gold_per_twd"), round(s.get("stock_wj", 0))))
    pts.sort()
    cut = dt.datetime.now().astimezone() - dt.timedelta(days=days)
    return [p for p in pts if p[0] >= cut]


def all_server_delta():
    """各服 30 天變化（實買口徑）：近期均 vs 前期均。"""
    d = json.load(open(HISTORY, encoding="utf-8"))
    by = {}
    for r in d["runs"]:
        day = r["ts"][:10]
        for nm, s in r["servers"].items():
            if not s.get("gold_floor"):
                continue
            a, b = by.setdefault(nm, ([], []))
            (a if "2026-09-17" <= day <= "2026-09-29" else b if day >= "2026-10-10" else []).append(s["gold_floor"])
    res = []
    for nm, (early, late) in by.items():
        if len(early) >= 5 and len(late) >= 5:
            e, l = sum(early) / len(early), sum(late) / len(late)
            res.append((nm, e, l, (l / e - 1) * 100))
    return sorted(res, key=lambda x: x[3])


# ---------- 繪圖（純 SVG，無 JS） ----------
def svg_line(points, w=640, h=190, pad=(38, 12, 22, 12), color="#e74c3c", label=""):
    """points = [(datetime, value)]；回傳 inline SVG 字串。"""
    if len(points) < 2:
        return "<p class='muted'>樣本不足</p>"
    xs = [p[0].timestamp() for p in points]
    ys = [p[1] for p in points]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    if x1 == x0:
        x1 += 1
    span = (y1 - y0) or 1
    y0 -= span * 0.12
    y1 += span * 0.12
    l, r, t, b = pad
    iw, ih = w - l - r, h - t - b

    def X(ts):
        return l + (ts - x0) / (x1 - x0) * iw

    def Y(v):
        return t + (y1 - v) / (y1 - y0) * ih

    line = " ".join(("M" if i == 0 else "L") + f"{X(p[0].timestamp()):.1f},{Y(p[1]):.1f}"
                    for i, p in enumerate(points))
    area = line + f" L{X(x1):.1f},{t+ih:.1f} L{X(x0):.1f},{t+ih:.1f} Z"
    grid, labels = [], []
    for i in range(4):
        yy = t + ih * i / 3
        val = y1 - (y1 - y0) * i / 3
        grid.append(f"<line x1='{l}' y1='{yy:.1f}' x2='{l+iw}' y2='{yy:.1f}' stroke='#2a3441' stroke-width='1'/>")
        labels.append(f"<text x='{l-4}' y='{yy+3.5:.1f}' font-size='10' fill='#7f8c9b' text-anchor='end'>{val:,.0f}</text>")
    xlab = []
    for i in range(4):
        p = points[min(len(points) - 1, round(i * (len(points) - 1) / 3))]
        xlab.append(f"<text x='{X(p[0].timestamp()):.1f}' y='{h-6}' font-size='10' fill='#7f8c9b' text-anchor='middle'>{p[0]:%m/%d}</text>")
    last = points[-1]
    return f"""<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="{html.escape(label)}">
<path d="{area}" fill="{color}" opacity="0.12"/>
<path d="{line}" fill="none" stroke="{color}" stroke-width="2.2" stroke-linejoin="round"/>
{''.join(grid)}{''.join(labels)}{''.join(xlab)}
<circle cx="{X(last[0].timestamp()):.1f}" cy="{Y(last[1]):.1f}" r="3.5" fill="{color}"/>
<text x="{min(X(last[0].timestamp()), w-52):.1f}" y="{max(Y(last[1])-8, 12):.1f}" font-size="11" fill="#ecf0f1" text-anchor="middle">{last[1]:,.0f}</text>
</svg>"""


# ---------- 產頁 ----------
def pct(v):
    if v is None:
        return "—"
    return f"{v:+.1f}%"


def build(d, prev):
    now = dt.datetime.now()
    ts = dt.datetime.fromisoformat(d["ts"])
    age_min = int((now - ts).total_seconds() // 60)
    fx = d["fx"]
    ok = [s for s in d["servers"] if s.get("vw2_gold_per_twd")]
    by_vw = sorted(ok, key=lambda s: -s["vw2_gold_per_twd"])
    by_sell = sorted([s for s in ok if s.get("sell_gold_per_twd")],
                     key=lambda s: s["sell_gold_per_twd"])
    thin = sorted(ok, key=lambda s: s["stock_wj"])[:3]
    deep = sorted(ok, key=lambda s: -s["stock_wj"])[:3]
    me = next((s for s in ok if s["server"] == USER_SERVER), None)
    pm = None
    if prev:
        pm = next((s for s in prev["servers"] if s["server"] == USER_SERVER), None)

    def delta(cur, old):
        if not cur or not old:
            return "—"
        return f"{(cur / old - 1) * 100:+.1f}%"

    def arrow(cur, old):
        if not cur or not old:
            return "flat", "—"
        v = (cur / old - 1) * 100
        if v > 0.15:
            return "up", f"⬆ {v:+.1f}%"
        if v < -0.15:
            return "down", f"⬇ {v:+.1f}%"
        return "flat", f"➖ {v:+.1f}%"

    series = load_series()
    ser_g = [(t, g) for t, g, _s, _k in series]
    deltas = all_server_delta()

    # 全服表格
    rows = []
    for i, s in enumerate(by_vw, 1):
        o = next((x for x in (prev["servers"] if prev else []) if x["server"] == s["server"]), None)
        cls, txt = arrow(s["vw2_gold_per_twd"], o["vw2_gold_per_twd"] if o else None)
        me_cls = " me" if s["server"] == USER_SERVER else ""
        tag = " <span class='tag'>你服</span>" if s["server"] == USER_SERVER else ""
        rows.append(
            f"<tr class='{cls}{me_cls}'><td class='rk'>{i}</td>"
            f"<td class='nm'>{html.escape(s['server'])}{tag}</td>"
            f"<td class='num hi'>{s['vw2_gold_per_twd']:,.0f}</td>"
            f"<td class='num'>{s['low_gold_per_twd']:,.0f}</td>"
            f"<td class='num'>{s['cost_twd_5000w']:,}</td>"
            f"<td class='num sm'>{s['used']}</td>"
            f"<td class='num'>{s['sell_gold_per_twd'] or '—'}</td>"
            f"<td class='num sm'>{s['stock_wj'] / 10000:.2f}億</td>"
            f"<td class='num sm {cls}'>{txt}</td></tr>")

    movers_up = [x for x in deltas if x[3] > 0][-4:][::-1]
    movers_dn = deltas[:4]

    def mover_list(items, sign):
        out = []
        for nm, e, l, p in items:
            out.append(f"<li><b>{html.escape(nm)}</b> {p:+.1f}%"
                       f"<span class='muted'>（實買 {e:,.0f} → {l:,.0f}）</span></li>")
        return f"<ul class='movers {sign}'>{''.join(out)}</ul>"

    me_block = ""
    if me:
        c1, t1 = arrow(me["vw2_gold_per_twd"], pm["vw2_gold_per_twd"] if pm else None)
        c2, t2 = arrow(me["sell_gold_per_twd"], pm["sell_gold_per_twd"] if pm else None)
        rank = by_vw.index(me) + 1
        best = by_vw[0]
        gap_cost = me["cost_twd_5000w"] / best["cost_twd_5000w"] * 100 - 100
        sell_rank = by_sell.index(me) + 1 if me in by_sell else "—"
        me_block = f"""
<section class="card accent">
  <h2>飛馬珀伽索斯（你服）<span>即時掃描 {ts:%m/%d %H:%M}（{age_min} 分鐘前）</span></h2>
  <div class="kpis">
    <div class="kpi"><label>① 最低一筆</label><b>{me['low_gold_per_twd']:,.0f}</b><i>1 台幣 ≈ N 金</i></div>
    <div class="kpi"><label>② 買滿 5,000 萬金</label><b>{me['vw2_gold_per_twd']:,.0f}</b><i>長期基準 <span class="{c1}">{t1}</span></i></div>
    <div class="kpi"><label>5,000 萬金成本</label><b>NT${me['cost_twd_5000w']:,}</b><i>吃 {me['used']} 筆掛單</i></div>
    <div class="kpi"><label>變現（賣金）</label><b>{me['sell_gold_per_twd'] or '—'}</b><i>1 台幣可賣 N 金 <span class="{c2}">{t2}</span></i></div>
  </div>
  <div class="notes">
    <div>📍 全服 22 台排名：買金划算度 <b>第 {rank} 名</b>／變現划算度 <b>第 {sell_rank} 名</b></div>
    <div>🧮 同樣買 5,000 萬金，最划算的 <b>{html.escape(best['server'])}</b>（NT${best['cost_twd_5000w']:,}）比飛馬便宜 <b>{gap_cost:,.0f}%</b></div>
    <div>📦 飛馬掛單總量 <b>{me['stock_wj'] / 10000:.2f} 億金</b>（{me['n']} 筆／有效 {me['n_eff']} 筆）{'　⚠️ 薄市場，數字易被單一賣家左右' if me['stock_wj'] < 20000 else ''}</div>
    <div>💡 台幣換算：1 萬金 ≈ NT${10000 / me['vw2_gold_per_twd']:.2f}（以 ② 計）</div>
  </div>
</section>"""

    trend_block = f"""
<section class="card">
  <h2>飛馬近 30 天走勢<span>DD373 實買口徑（cron 每 5 小時）｜與上表 ①② 口徑不同，只看趨勢</span></h2>
  <div class="chart">{svg_line(ser_g, color="#e74c3c", label="飛馬實買價走勢")}</div>
  <div class="legend"><span class="dot red"></span>實買價（1 台幣買到幾金，越高＝金越便宜）</div>
  <div class="notes">
    <div>📉 近 5 日均 <b>{sum(v for _t, v in ser_g[-20:]) / max(1, len(ser_g[-20:])):,.0f}</b>；全期最高 <b>{max(v for _t, v in ser_g):,.0f}</b>／最低 <b>{min(v for _t, v in ser_g):,.0f}</b></div>
    <div>🔎 樣本：{len(ser_g)} 個取樣點（{ser_g[0][0]:%m/%d} ~ {ser_g[-1][0]:%m/%d}）</div>
  </div>
</section>

<section class="card">
  <h2>近一個月各服變化<span>9/17~9/29 均 vs 10/10 起均（實買口徑）</span></h2>
  <div class="split">
    <div><h3 class="up">金變便宜（1 元買到更多金）</h3>{mover_list(movers_up, 'up')}</div>
    <div><h3 class="down">金變貴（1 元買到更少金）</h3>{mover_list(movers_dn, 'down')}</div>
  </div>
</section>"""

    rows_html = "".join(rows)
    html_doc = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#12181f">
<title>天堂經典版 天幣幣值（DD373 全服）</title>
<style>
:root{{--bg:#0f1419;--card:#161d26;--card2:#1b2330;--line:#263040;--fg:#e8edf3;--muted:#8a97a6;--red:#e74c3c;--green:#2ecc71;--gold:#f1c40f}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.55 -apple-system,"PingFang TC","Hiragino Sans GB","Microsoft JhengHei",sans-serif;padding:14px max(12px,env(safe-area-inset-left)) 40px}}
header{{margin:4px 0 14px}}
h1{{font-size:19px;margin:0 0 4px}}
h2{{font-size:15px;margin:0 0 10px;display:flex;flex-wrap:wrap;gap:6px;align-items:baseline;justify-content:space-between}}
h2 span{{font-size:11px;color:var(--muted);font-weight:400}}
h3{{font-size:12.5px;margin:0 0 6px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:13px;margin:0 0 12px}}
.card.accent{{background:linear-gradient(180deg,#1b2430,#161d26);border-color:#33415a}}
.sub{{color:var(--muted);font-size:12px}}
.kpis{{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin:4px 0 10px}}
.kpi{{background:var(--card2);border-radius:9px;padding:9px 10px}}
.kpi label{{display:block;font-size:11px;color:var(--muted)}}
.kpi b{{font-size:19px;display:block;line-height:1.25}}
.kpi i{{font-size:11px;color:var(--muted);font-style:normal}}
.notes div{{font-size:12.5px;color:#c6d0da;margin:3px 0}}
.up{{color:var(--green)}} .down{{color:var(--red)}} .flat{{color:var(--muted)}}
table{{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}}
th,td{{padding:6px 4px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}}
th{{font-size:11px;color:var(--muted);font-weight:500;position:sticky;top:0;background:var(--card)}}
td.nm,th.nm{{text-align:left;font-size:12.5px}}
td.rk{{color:var(--muted);font-size:11px;width:20px}}
td.num{{font-size:12.5px}}
td.sm{{font-size:11px;color:#b7c2ce}}
td.hi{{font-weight:700}}
tr.me{{background:#20304a}}
tr.me td.nm{{font-weight:700}}
.tag{{background:#2ecc71;color:#04121f;border-radius:4px;font-size:10px;padding:1px 4px;vertical-align:1px}}
.legend{{font-size:11px;color:var(--muted);margin:4px 0 8px}}
.dot{{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px}}
.dot.red{{background:var(--red)}}
.chart{{margin:6px 0 2px}}
.movers{{margin:0;padding-left:18px;font-size:12.5px}}
.movers li{{margin:2px 0}}
.split{{display:grid;grid-template-columns:1fr;gap:10px}}
.muted{{color:var(--muted)}}
footer{{color:var(--muted);font-size:11px;margin-top:14px;line-height:1.7}}
a{{color:#6ab0ff}}
@media(min-width:560px){{.kpis{{grid-template-columns:repeat(4,1fr)}}.split{{grid-template-columns:1fr 1fr}}}}
</style>
</head>
<body>
<header>
  <h1>天堂經典版 天幣幣值</h1>
  <div class="sub">資料來源：DD373 天幣掛單 ｜ 匯率 CNY→TWD {fx:.4f} ｜ 掃到 {len(ok)}/{len(d['servers']) + len(d.get('errs', []))} 台
  ｜ 更新 {ts:%Y/%m/%d %H:%M}（{age_min} 分鐘前）{'　⚠️ 使用快取' if d.get('cached') else ''}</div>
</header>
{me_block}
<section class="card">
  <h2>全服 22 台排行<span>依 ② 買滿 5,000 萬金（數字越大＝1 元買到越多金）</span></h2>
  <div class="chart" style="overflow-x:auto">
  <table>
    <thead><tr><th></th><th class="nm">伺服器</th><th>②5000萬</th><th>①最低</th><th>成本NT$</th><th>吃筆</th><th>變現</th><th>掛單</th><th>vs上次</th></tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
  </div>
  <div class="notes">
    <div>🟢 買金最划算：{', '.join(f"<b>{html.escape(s['server'])}</b>（②{s['vw2_gold_per_twd']:,.0f}）" for s in by_vw[:3])}</div>
    <div>💰 變現最划算（數字低）：{', '.join(f"<b>{html.escape(s['server'])}</b>（{s['sell_gold_per_twd']}）" for s in by_sell[:3])}</div>
    <div>🐳 深市場（大額不會被抬價）：{', '.join(f"{html.escape(s['server'])} {s['stock_wj']/10000:.1f}億" for s in deep)}</div>
    <div>⚠️ 薄市場（&lt;2 億金，數字易失真）：{', '.join(f"{html.escape(s['server'])} {s['stock_wj']/10000:.2f}億" for s in thin if s['stock_wj'] < 20000) or '無'}</div>
    <div class="muted">口徑：① ＝ 全檔位最低單價（小額實際買得到）；② ＝ 從最便宜往上吃到 5,000 萬金為止的加權平均（大額實際成本）；變現 ＝ DD373 收購區掛單（越低＝賣金換到越多台幣）。</div>
  </div>
</section>
{trend_block}
<footer>
  {len(ok)} 台掃描 ｜ 產生時間 {now:%Y/%m/%d %H:%M:%S} ｜ 純腳本產頁（0 token）｜ 資料每 5 小時自動重掃<br>
  ⚠️ 平台掛單價格隨時變動，此頁為取樣時點的快照；短時間內個別服可跳 ±10%，單一快照別當趨勢。
</footer>
</body>
</html>"""
    os.makedirs(PUBLIC_DIR, exist_ok=True)
    with open(os.path.join(PUBLIC_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(html_doc)
    json.dump({"ts": d["ts"], "fx": fx, "servers": ok}, open(os.path.join(DATA_DIR, "page-data.json"), "w"),
              ensure_ascii=False)
    print(f"[build] public/index.html（{len(html_doc):,} bytes）、{len(ok)} 台", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true", help="只用快取／歷史，不連 DD373")
    a = ap.parse_args()
    d = fetch_sweep(force=False) if not a.no_fetch else (
        json.load(open(SWEEP_LATEST, encoding="utf-8")) if os.path.exists(SWEEP_LATEST) else
        {"fx": fetch_fx(), "ts": dt.datetime.now().isoformat(timespec="seconds"), "servers": [], "errs": []})
    if a.no_fetch and os.path.exists(SWEEP_LATEST):
        d["cached"] = True
    build(d, prev_sweep())


if __name__ == "__main__":
    main()
