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
SORTS = ("1",)          # 只抓「綜合」排序：DD373 頁面本身依單價升序，1 種排序就有最便宜那批
                        # （3 種排序只是多幾個樣本；要全取樣跑 dd373-all-sweep.py）
BACKOFF_HOURS = 6       # 疑似被擋時退避幾小時
ERR_RATIO_MAX = 0.4     # 錯誤台數比例超過此值 → 判定被擋

TZ = dt.timezone(dt.timedelta(hours=8))      # Asia/Taipei（台灣無日光節約，固定 +8）
TREND_JS = os.path.join(PAGE_DIR, "static", "trend.js")   # 走勢圖渲染器（build 時內嵌進單檔頁面）


def ep(t):
    """naive 台北時間 → epoch 秒（圖表一律用 epoch，時區在 JS 內以 +8h 換算）。"""
    return int(t.replace(tzinfo=TZ).timestamp())


def hampel(points, k=2, n_sig=3.0):
    """滾動中位數（5 筆 Hampel filter）：偏離視窗中位數 > 3×MAD 者視為離群。
    回傳 [(t, 平滑後值, 原始值, 是否離群)]；主線用平滑值，原始值留給 tooltip。
    MAD 依慣例乘 1.4826 換算成標準差等效值。"""
    import statistics as st
    vals = [v for _t, v in points]
    out = []
    for i, (t, v) in enumerate(points):
        win = vals[max(0, i - k): i + k + 1]
        m = st.median(win)
        mad = st.median([abs(x - m) for x in win]) * 1.4826
        is_out = bool(mad > 0 and abs(v - m) > n_sig * mad)
        out.append((t, m if is_out else v, v, is_out))
    return out

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
BACKOFF = os.path.join(DATA_DIR, "backoff.json")


def backoff_active():
    """疑似被 DD373 擋住時的退避旗標（由本檔或 15 分鐘輕掃設定）。"""
    try:
        b = json.load(open(BACKOFF, encoding="utf-8"))
        return b if dt.datetime.fromisoformat(b["until"]) > dt.datetime.now() else None
    except Exception:
        return None


def set_backoff(hours, reason):
    os.makedirs(DATA_DIR, exist_ok=True)
    json.dump({"until": (dt.datetime.now() + dt.timedelta(hours=hours)).isoformat(timespec="seconds"),
               "reason": reason, "set_at": dt.datetime.now().isoformat(timespec="seconds")},
              open(BACKOFF, "w"), ensure_ascii=False)


def fetch_sweep(force=False):
    os.makedirs(DATA_DIR, exist_ok=True)
    # 快取新鮮度一律看資料檔「內部的 ts」，不看檔案 mtime
    # （mtime 會被預覽產線／git stash 之類的操作改到：曾讓正式版誤判「1 分鐘前」而整整一小時沒重掃）
    if not force and os.path.exists(SWEEP_LATEST):
        try:
            d = json.load(open(SWEEP_LATEST, encoding="utf-8"))
            age_min = (dt.datetime.now() - dt.datetime.fromisoformat(d["ts"])).total_seconds() / 60
        except Exception as e:
            d, age_min = None, None
            print(f"[cache] {os.path.basename(SWEEP_LATEST)} 讀不到內部 ts（{type(e).__name__}）→ 直接重掃", flush=True)
        if d is not None:
            if -5 <= age_min < CACHE_MIN:
                d["cached"] = True
                print(f"[cache] 沿用 {int(max(age_min, 0))} 分鐘前的掃描結果"
                      f"（{len(d['servers'])} 台，資料 ts {d['ts']}）", flush=True)
                return d
            print(f"[sweep] 快取資料已 {int(age_min)} 分鐘（門檻 {CACHE_MIN}）→ 重掃", flush=True)

    bo = backoff_active()
    if bo and not force:
        print(f"[backoff] 退避中（到 {bo['until']}｜{bo.get('reason','')}）→ 不抓，沿用上次資料", flush=True)
        if os.path.exists(SWEEP_LATEST):
            d = json.load(open(SWEEP_LATEST, encoding="utf-8"))
            d["cached"] = True
            return d
        return {"fx": fetch_fx(), "ts": dt.datetime.now().isoformat(timespec="seconds"),
                "servers": [], "errs": [], "cached": True}

    scan = load_scan_module()
    fx = fetch_fx()
    tokens = dict(scan.TOKENS)
    tokens.update({k: v for k, v in scan.discover_servers().items() if k not in scan.SKIP})
    out, errs = [], []
    for name, tok in tokens.items():
        try:
            m = server_rate(tok, fx, sorts=SORTS)
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

    # 被擋偵測：幾乎全滅就退避（避免一直硬打）
    if not out or len(errs) >= max(3, len(tokens) * ERR_RATIO_MAX):
        set_backoff(BACKOFF_HOURS, f"{len(errs)}/{len(tokens)} 台抓取失敗（疑似被擋）")
        print(f"[BLOCKED] {len(errs)}/{len(tokens)} 台失敗 → 退避 {BACKOFF_HOURS} 小時", flush=True)
        if os.path.exists(SWEEP_LATEST):
            d = json.load(open(SWEEP_LATEST, encoding="utf-8"))
            d["cached"] = True
            return d

    d = {"fx": fx, "ts": dt.datetime.now().isoformat(timespec="seconds"),
         "servers": out, "errs": errs, "cached": False}
    me_out = next((s for s in out if s["server"] == USER_SERVER), None)
    if me_out:
        append_vw2_series(d["ts"], me_out.get("vw2_gold_per_twd"))     # 記進 ② 長期序列
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


def append_vw2_series(ts_iso, value):
    """② 長期序列（只存飛馬一個數字一筆）——獨立檔案，不受 sweep-history / fm-light 的 400 筆上限影響，
    7 天／30 天窗才不會因為裁切而缺歷史。"""
    if not value:
        return
    p = os.path.join(DATA_DIR, "vw2-series.json")
    try:
        ser = json.load(open(p, encoding="utf-8"))
    except Exception:
        ser = []
    key = ts_iso[:16]
    if not any(x.get("ts", "")[:16] == key for x in ser[-3:]):
        ser.append({"ts": ts_iso, "v": value})
    json.dump(ser[-9000:], open(p, "w"), ensure_ascii=False)          # 9000 筆 ≈ 93 天（15 分一筆）


def load_vw2_series(hours=24, key="vw2_gold_per_twd"):
    """② 口徑序列：優先讀長期檔 vw2-series.json，再補上 sweep-history／fm-light（去重）。"""
    pts = {}
    try:
        for e in json.load(open(os.path.join(DATA_DIR, "vw2-series.json"), encoding="utf-8")):
            if e.get("v"):
                pts[dt.datetime.fromisoformat(e["ts"]).replace(second=0, microsecond=0)] = e["v"]
    except Exception:
        pass
    try:
        for run in json.load(open(SWEEP_HISTORY, encoding="utf-8")):
            s = next((x for x in run.get("servers", []) if x.get("server") == USER_SERVER), None)
            if not s:
                continue
            v = s.get(key) or (s.get("floor_eff_gold") if key == "floor_gold" else None)
            if v:
                pts.setdefault(dt.datetime.fromisoformat(run["ts"]).replace(second=0, microsecond=0), v)
    except Exception:
        pass
    try:
        for e in json.load(open(os.path.join(DATA_DIR, "fm-light.json"), encoding="utf-8"))["entries"]:
            if e.get(key):
                pts.setdefault(dt.datetime.fromisoformat(e["ts"]).replace(second=0, microsecond=0), e[key])
    except Exception:
        pass
    cut = dt.datetime.now() - dt.timedelta(hours=hours)
    return sorted((t, v) for t, v in pts.items() if t >= cut)


def vw2_all_points():
    """{伺服器: {ts: (datetime, ②, is_est)}}：估算（9/15 起）＋實測（我們自己的掃描，同分鐘優先覆蓋）。"""
    pts = {}
    try:
        d = json.load(open(os.path.join(DATA_DIR, "vw2-estimated-all.json"), encoding="utf-8"))
        for nm, rows in (d.get("series") or {}).items():
            for e in rows:
                t = dt.datetime.fromisoformat(e["ts"]).replace(tzinfo=None)
                pts.setdefault(nm, {})[t.strftime("%Y-%m-%dT%H:%M")] = (t, e["v"], True)
    except Exception:
        pass
    try:
        for run in json.load(open(SWEEP_HISTORY, encoding="utf-8")):
            t = dt.datetime.fromisoformat(run["ts"]).replace(second=0, microsecond=0)
            for s in run.get("servers", []):
                if s.get("vw2_gold_per_twd"):
                    pts.setdefault(s["server"], {})[t.strftime("%Y-%m-%dT%H:%M")] = (t, s["vw2_gold_per_twd"], False)
    except Exception:
        pass
    return pts


def all_server_delta(days=3):
    """各服 ② 幣值變化：最近 N 天均 vs 前期（9/17~9/29）均。實測優先、不足處以估算補。"""
    pts = vw2_all_points()
    now = dt.datetime.now()
    res = []
    for nm, d in pts.items():
        vals = sorted(d.values())
        early = [v for t, v, _e in vals if "2026-09-17" <= t.strftime("%Y-%m-%d") <= "2026-09-29"]
        late = [v for t, v, _e in vals if t >= now - dt.timedelta(days=days)]
        if len(early) >= 5 and len(late) >= 5:
            e, l = sum(early) / len(early), sum(late) / len(late)
            res.append((nm, e, l, (l / e - 1) * 100))
    return sorted(res, key=lambda x: x[3])


# ---------- 繪圖（純 SVG，無 JS） ----------
def svg_line(points, w=640, h=200, pad=(50, 18, 34, 18), color="#e74c3c", label="", xfmt=None, extra=None):
    """points = [(datetime, value)]；回傳 inline SVG 字串。
    xfmt=None 時自動選：跨度 ≤30 小時用時:分，否則用月/日。
    extra = (points, color)：同一張圖上再畫一條虛線（與主線共用座標軸）。
    版面：左邊留 50 給 y 軸數字、下面留 34 給 x 軸標籤（避免左下角兩個軸的文字疊在一起）；
    x 軸第一/最後標籤改成靠左/靠右對齊，不會超出邊界或被切掉。"""
    if len(points) < 2:
        return "<p class='muted'>樣本不足</p>"
    ex = [p for p in (extra[0] if extra else []) if p[1]]
    xs = [p[0].timestamp() for p in points] + [p[0].timestamp() for p in ex]
    ys = [p[1] for p in points] + [p[1] for p in ex]
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
        labels.append(f"<text x='{l-7}' y='{yy+4:.1f}' font-size='11' fill='#7f8c9b' text-anchor='end'>{val:,.0f}</text>")
    xlab = []
    if xfmt is None:
        span_h_x = (x1 - x0) / 3600
        xfmt = "%H:%M" if span_h_x <= 30 else ("%m/%d %H:%M" if span_h_x <= 72 else "%m/%d")
    # x 軸標籤：依「時間範圍」平均取 5 個刻度（不是抓資料點，否則同一時段的點會標出重複日期）
    nx = 5
    approx_w = len(dt.datetime.fromtimestamp(x0).strftime(xfmt)) * 6.5
    while nx > 3 and nx * approx_w > iw * 0.92:
        nx -= 1
    for i in range(nx):
        ts = x0 + (x1 - x0) * i / (nx - 1)
        tt = dt.datetime.fromtimestamp(ts)          # 注意：不要用 t（t 是上方留白）
        anchor = "start" if i == 0 else ("end" if i == nx - 1 else "middle")
        xlab.append(f"<text x='{X(ts):.1f}' y='{h-12}' font-size='11' fill='#7f8c9b' "
                    f"text-anchor='{anchor}'>{tt.strftime(xfmt)}</text>")
    last = points[-1]
    lx, ly = X(last[0].timestamp()), max(Y(last[1]) - 10, t + 12)
    anchor = "end" if lx > l + iw * 0.75 else ("start" if lx < l + iw * 0.15 else "middle")
    extra_svg = ""
    if len(ex) >= 2:
        eline = " ".join(("M" if i == 0 else "L") + f"{X(p[0].timestamp()):.1f},{Y(p[1]):.1f}"
                         for i, p in enumerate(ex))
        extra_svg = (f"<path d='{eline}' fill='none' stroke='{extra[1]}' stroke-width='1.8' "
                     f"stroke-dasharray='6 4' opacity='0.75'/>")
    return f"""<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="{html.escape(label)}">
<path d="{area}" fill="{color}" opacity="0.12"/>
<path d="{line}" fill="none" stroke="{color}" stroke-width="2.2" stroke-linejoin="round"/>
{extra_svg}
{''.join(grid)}{''.join(labels)}{''.join(xlab)}
<circle cx="{lx:.1f}" cy="{Y(last[1]):.1f}" r="3.5" fill="{color}"/>
<text x="{lx:.1f}" y="{ly:.1f}" font-size="12" fill="#ecf0f1" text-anchor="{anchor}">{last[1]:,.0f}</text>
</svg>"""


# ---------- 產頁 ----------
def pct(v):
    if v is None:
        return "—"
    return f"{v:+.1f}%"


# ---- 台幣口徑（頁面主數字一律用「買 5,000 萬金要多少台幣」）----
def nt5000(s):
    """買滿 5,000 萬金的台幣成本（② 口徑換算；越低＝金越便宜）"""
    if s.get("cost_twd_5000w"):
        return s["cost_twd_5000w"]
    v = s.get("vw2_gold_per_twd")
    return round(50_000_000 / v) if v else None


def nt_per_wj(gold_per_twd):
    """最低一筆的單價：每萬金多少台幣"""
    return round(10000 / gold_per_twd, 2) if gold_per_twd else None


def nt_sell_5000(s):
    """把 5,000 萬金賣給 DD373 收購區可拿到的台幣（越高越好）"""
    v = s.get("sell_gold_per_twd")
    return round(50_000_000 / v) if v else None


def nt5000_from_gold(gold_per_twd):
    """由『1 台幣≈N 金』換算買 5,000 萬金的台幣成本（走勢圖用）"""
    return round(50_000_000 / gold_per_twd) if gold_per_twd else None


def build(d, prev):
    now = dt.datetime.now()
    bo = backoff_active()
    next_upd = now.replace(minute=20, second=0, microsecond=0)
    if next_upd <= now:
        next_upd += dt.timedelta(hours=1)
    light_txt = "尚無資料"
    fm_light = d.get("fm_light")
    try:
        e = json.load(open(os.path.join(DATA_DIR, "fm-light.json"), encoding="utf-8"))["entries"][-1]
        t = dt.datetime.fromisoformat(e["ts"])
        light_txt = (f'{t:%H:%M} 掃到 ② {e["vw2_gold_per_twd"]:,.0f}'
                     f'（{int((now - t).total_seconds() // 60)} 分鐘前）')
    except Exception:
        pass
    if fm_light:
        light_txt += "，已套用到本頁飛馬數字"
    ts = dt.datetime.fromisoformat(d["ts"])
    age_min = int((now - ts).total_seconds() // 60)
    me_ts = dt.datetime.fromisoformat(fm_light["ts"]) if fm_light else ts
    me_age = int((now - me_ts).total_seconds() // 60)
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

    def arrow(cur, old, invert=False):
        """invert=True 代表數字越小越好（例如台幣成本）→ 下降顯示綠色。"""
        if not cur or not old:
            return "flat", "—"
        v = (cur / old - 1) * 100
        good = (v < 0) if invert else (v > 0)
        mark = "⬆" if v > 0.15 else ("⬇" if v < -0.15 else "➖")
        cls = "flat" if abs(v) <= 0.15 else ("up" if good else "down")
        return cls, f"{mark} {v:+.1f}%"

    series = load_series()
    ser_g = [(t, g) for t, g, _s, _k in series]
    deltas = all_server_delta()

    # 全服表格
    rows = []
    mrows = []
    for i, s in enumerate(by_vw, 1):
        o = next((x for x in (prev["servers"] if prev else []) if x["server"] == s["server"]), None)
        cls, txt = arrow(s["vw2_gold_per_twd"], o["vw2_gold_per_twd"] if o else None)   # ② 上升＝金變便宜
        me_cls = " me" if s["server"] == USER_SERVER else ""
        tag = ""                                          # 不再標「主服」：改由底色較亮的列辨識飛馬
        rows.append(
            f"<tr class='{cls}{me_cls}'><td class='rk'>{i}</td>"
            f"<td class='nm'>{html.escape(s['server'])}{tag}</td>"
            f"<td class='num hi pri'>{s['vw2_gold_per_twd']:,.0f}</td>"
            f"<td class='num'>{s['low_gold_per_twd']:,.0f}</td>"
            f"<td class='num'>{s['sell_gold_per_twd'] or '—'}</td>"
            f"<td class='num sm'>{s['used']}</td>"
            f"<td class='num sm'>{s['stock_wj'] / 10000:.2f}億</td>"
            f"<td class='num sm {cls}'>{txt}</td></tr>")
        mrows.append(
            f"<div class='mrow{me_cls}'>"
            f"<div class='mline1'><span class='rk'>{i}</span><span class='nm'>{html.escape(s['server'])}{tag}</span>"
            f"<span class='vw pri'>{s['vw2_gold_per_twd']:,.0f}</span><span class='dl {cls}'>{txt}</span></div>"
            f"<div class='mline2'>① {s['low_gold_per_twd']:,.0f} ・ 變現 {s['sell_gold_per_twd'] or '—'}"
            f" ・ 掛單 {s['stock_wj'] / 10000:.2f}億 ・ 吃 {s['used']} 筆"
            f" ・ 買 5,000 萬金 ≈ NT${nt5000(s):,}</div></div>")

    by_sell = sorted([s for s in ok if s.get("sell_gold_per_twd")],
                     key=lambda s: s["sell_gold_per_twd"])

    movers_up = [x for x in deltas if x[3] > 0][-4:][::-1]      # 幣值上升＝金變便宜
    movers_dn = deltas[:4]                                      # 幣值下降＝金變貴

    def mover_list(items, sign):
        out = []
        for nm, e, l, p in items:
            out.append(f"<li><b>{html.escape(nm)}</b> {p:+.1f}%"
                       f"<span class='muted'>（1 台幣 {e:,.0f} → {l:,.0f} 金）</span></li>")
        return f"<ul class='movers {sign}'>{''.join(out)}</ul>"

    me_block = ""
    if me:
        c1, t1 = arrow(me["vw2_gold_per_twd"], pm["vw2_gold_per_twd"] if pm else None)
        c2, t2 = arrow(me["sell_gold_per_twd"], pm["sell_gold_per_twd"] if pm else None)
        rank = by_vw.index(me) + 1
        best = by_vw[0]
        gap_gold = me["vw2_gold_per_twd"] / best["vw2_gold_per_twd"] * 100 - 100
        sell_rank = by_sell.index(me) + 1 if me in by_sell else "—"
        me_block = f"""
<section class="card accent">
  <h2>飛馬珀伽索斯<span>{'15 分鐘輕掃' if fm_light else '即時掃描'} {me_ts:%m/%d %H:%M}（{me_age} 分鐘前）</span></h2>
  <div class="kpis">
    <div class="kpi"><label>★ 買滿 5,000 萬金（加權幣值）</label><b class="pri">{me['vw2_gold_per_twd']:,.0f}</b><i>1 台幣 ≈ N 金 <span class="{c1}">{t1}</span></i></div>
    <div class="kpi"><label>最低一筆（①）</label><b>{me['low_gold_per_twd']:,.0f}</b><i>1 台幣 ≈ N 金（起售 {me['low_wj']:,.0f} 萬金）</i></div>
    <div class="kpi"><label>變現（賣金）</label><b>{me['sell_gold_per_twd'] or '—'}</b><i>1 台幣可賣 N 金 <span class="{c2}">{t2}</span></i></div>
    <div class="kpi"><label>掛單總量</label><b>{me['stock_wj'] / 10000:.2f} 億</b><i>{me['n']} 筆／有效 {me['n_eff']} 筆</i></div>
  </div>
  <div class="notes">
    <div>📍 全服 22 台排名：幣值（買金划算度）<b>第 {rank} 名</b>／變現划算度 <b>第 {sell_rank} 名</b></div>
    <div>🧮 同樣買 5,000 萬金：最划算的 <b>{html.escape(best['server'])}</b>（{best['vw2_gold_per_twd']:,.0f} 金/元）比飛馬多買 <b>{abs(gap_gold):,.0f}%</b> 的金；飛馬要 NT${nt5000(me):,}、它只要 NT${nt5000(best):,}</div>
    <div>📦 飛馬掛單總量 <b>{me['stock_wj'] / 10000:.2f} 億金</b>{'　⚠️ 薄市場，數字易被單一賣家左右' if me['stock_wj'] < 20000 else ''}</div>
    <div>💡 台幣換算：買 5,000 萬金 ≈ <b>NT${nt5000(me):,}</b>（以 ② 加權計）｜匯率 CNY→TWD {fx:.4f}</div>
  </div>
</section>"""

    s_now = load_vw2_series(24, "vw2_gold_per_twd")     # ② 買滿 5,000 萬金加權（15 分輕掃 + 每小時全服）
    all_vw2 = load_vw2_series(24 * 31, "vw2_gold_per_twd")
    now_vw2 = s_now[-1][1] if s_now else None
    t_now = dt.datetime.now()
    latest_txt = (f"最新 <b>{now_vw2:,.0f}</b> @ {t_now:%H:%M}" if now_vw2 else "—")
    span_h = (all_vw2[-1][0] - all_vw2[0][0]).total_seconds() / 3600 if all_vw2 else 0

    def win_npts(hours):
        """窗內實測點數（圖例與『還在累積』判斷用）"""
        cut = t_now - dt.timedelta(hours=hours)
        return len([p for p in all_vw2 if p[0] >= cut])

    n24, n48, n7d, n30d = win_npts(24), win_npts(48), win_npts(24 * 7), win_npts(24 * 30)
    vw2_note = (f"② 買滿 5,000 萬金加權幣值（15 分輕掃 + 每小時全服）｜{n24} 點｜{latest_txt}")
    # 估算 ②（由 9/15 起的實買口徑 × 比率換算；只用在實測②開始之前）
    est_all, r_used = [], None
    try:
        for e in json.load(open(os.path.join(DATA_DIR, "vw2-estimated.json"), encoding="utf-8")):
            est_all.append((dt.datetime.fromisoformat(e["ts"]).replace(tzinfo=None), e["v"]))
            r_used = e.get("r", r_used)
    except Exception:
        est_all = []
    first_real = all_vw2[0][0] if all_vw2 else None
    est_all = [p for p in sorted(est_all) if (not first_real or p[0] < first_real)]
    r_txt = f"{r_used:.4f}" if r_used else "—"
    est_legend = (f"｜<b>虛線＝估算 ②</b>（9/15 起、由實買 × {r_txt} 換算，非實測）") if est_all else ""

    # ---- 走勢圖資料（epoch 秒；時區換算一律在 JS 端 +8h 做）----
    chart_real = hampel([(t, v) for t, v in all_vw2])       # 5 筆 Hampel：主線用平滑值、離群原始值另存
    chart_payload = {
        "now": ep(t_now),
        "r": r_used,
        # [epoch, 平滑值, 原始值, 是否離群]
        "real": [[ep(t), round(v, 1), round(raw, 1), 1 if o else 0] for t, v, raw, o in chart_real],
        "est": [[ep(t), round(v, 1)] for t, v in est_all],
    }
    chart_windows = [
        # gapReal／gapEst：相鄰兩筆超過此秒數就斷線（實測 30 分；估算抽樣稀疏 → 用 12 小時）
        {"id": "24h", "label": "24 小時", "span": 86400, "color": "#e74c3c", "gapReal": 1800, "gapEst": 43200},
        {"id": "48h", "label": "48 小時", "span": 172800, "color": "#d35400", "gapReal": 1800, "gapEst": 43200},
        {"id": "7d", "label": "7 天", "span": 604800, "color": "#e67e22", "gapReal": 1800, "gapEst": 43200},
        {"id": "30d", "label": "30 天", "span": 2592000, "color": "#c0392b", "gapReal": 1800, "gapEst": 43200},
    ]

    def panel(win_id, cls, dot, legend, npts):
        if npts < 2:
            return (f'<div class="panel {cls}"><p class="muted">這個時間窗還在累積（目前 {npts} 點，'
                    f'② 口徑自 {all_vw2[0][0]:%m/%d %H:%M} 開始記錄）</p><div class="legend">{legend}</div></div>')
        return (f'<div class="panel {cls}"><div class="chart" data-win="{win_id}"></div>'
                f'<div class="legend"><span class="dot {dot}"></span>{legend}</div></div>')

    acc_note = (f"② 口徑自 {all_vw2[0][0]:%m/%d %H:%M} 開始記錄"
                + (f"（目前累積 {span_h:.0f} 小時）" if span_h else "")
                + "，7 天／30 天窗會逐日長出來")
    trend_block = f"""
<section class="card">
  <h2>飛馬走勢<span>四個時間窗（點標籤切換）｜同一把尺＝② 買滿 5,000 萬金加權幣值</span></h2>
  <div class="tabs">
    <input type="radio" name="tr" id="tr24" checked>
    <input type="radio" name="tr" id="tr48">
    <input type="radio" name="tr" id="tr7">
    <input type="radio" name="tr" id="tr30">
    <div class="tabbar">
      <label for="tr24">24 小時</label><label for="tr48">48 小時</label><label for="tr7">7 天</label><label for="tr30">30 天</label>
    </div>
    <div class="panels">
      {panel("24h", "p24", "red", vw2_note + "<br>數字＝1 台幣買到幾金（買滿 5,000 萬金加權），<b>越高＝金越便宜</b>", n24)}
      {panel("48h", "p48", "burnt", f"實線＝實測 ②｜{n48} 點｜{latest_txt}<br>本窗全為實測（② 口徑自 {all_vw2[0][0]:%m/%d %H:%M} 起連續記錄，無估算段）；數字＝1 台幣買到幾金，<b>越高＝金越便宜</b>", n48)}
      {panel("7d", "p7", "orange", f"實線＝實測 ②｜{n7d} 點｜{latest_txt}{est_legend}<br>{acc_note}", n7d)}
      {panel("30d", "p30", "red", f"實線＝實測 ②｜{n30d} 點｜{latest_txt}{est_legend}<br>{acc_note}", n30d)}
    </div>
  </div>
  <div class="notes">
    <div>📏 四個頁籤＋上方表格／KPI <b>全部同一把尺</b>＝<b>② 買滿 5,000 萬金加權幣值</b>（1 台幣≈N 金，越高＝金越便宜），線尾都接到現在。</div>
    <div>🧮 <b>灰虛線＝估算 ②</b>：實測 ② 只從 {all_vw2[0][0]:%m/%d %H:%M} 開始記錄；更早的用舊的「實買」序列 × 比率 {r_txt} 換算（比率取自兩者同時段的實測配對，誤差 &lt;1%，但仍屬<b>估算</b>，趨勢可信、絕對值只能當參考）。48 小時窗完全落在實測範圍內，所以沒有虛線。</div>
    <div>ℹ️ {acc_note}。</div>
  </div>
</section>

<section class="card">
  <h2>近一個月各服 ② 幣值變化<span>9/17~9/29 均（估算）vs 最近 3 天均（實測優先、不足處以估算補）｜同一把尺＝② 買滿 5,000 萬金加權</span></h2>
  <div class="split">
    <div><h3 class="up">金變便宜（1 元買到更多金）</h3>{mover_list(movers_up, 'up')}</div>
    <div><h3 class="down">金變貴（1 元買到更少金）</h3>{mover_list(movers_dn, 'down')}</div>
  </div>
</section>"""

    # ---- 走勢圖：資料 JSON + 視窗設定 + 渲染器（全部內嵌，維持單檔、無外部依賴）----
    trend_js_src = open(TREND_JS, encoding="utf-8").read()
    chart_block = (
        '<script type="application/json" id="chart-data">'
        + json.dumps(chart_payload, separators=(",", ":"))
        + '</script>\n<script>window.DD373WINDOWS='
        + json.dumps(chart_windows, ensure_ascii=False, separators=(",", ":"))
        + ';</script>\n<script>\n' + trend_js_src + '\n</script>')

    rows_html = "".join(rows)
    mrows_html = "".join(mrows)
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
.pri{{color:var(--gold) !important;font-weight:700}}
td.pri{{background:rgba(241,196,15,.12);box-shadow:inset 1px 0 0 rgba(241,196,15,.35),inset -1px 0 0 rgba(241,196,15,.35)}}
th.pri{{color:var(--gold) !important;background:#20293a}}
.mob{{display:block}}
.full{{display:none}}
.mrow{{padding:7px 2px;border-bottom:1px solid var(--line)}}
.mrow.me{{background:#20304a;border-radius:6px;padding:7px 7px}}
.mline1{{display:flex;align-items:baseline;gap:6px}}
.mline1 .rk{{color:var(--muted);font-size:11px;width:16px;flex:none}}
.mline1 .nm{{flex:1;font-size:13px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.mline1 .nm.me{{font-weight:700}}
.mline1 .vw{{font-weight:700;font-size:15px;font-variant-numeric:tabular-nums;flex:none}}
.mline1 .dl{{font-size:11px;text-align:right;font-variant-numeric:tabular-nums;flex:none;min-width:58px}}
.mline2{{font-size:11px;color:#9aa7b6;margin-top:1px}}
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
.dot.orange{{background:#e67e22}}
.dot.burnt{{background:#d35400}}
.tabs input{{display:none}}
.tabbar{{display:flex;flex-wrap:wrap;gap:5px;margin:2px 0 10px}}
.tabbar label{{font-size:12px;padding:5px 10px;border:1px solid var(--line);border-radius:999px;color:var(--muted);cursor:pointer}}
#tr24:checked ~ .tabbar label[for=tr24],#tr48:checked ~ .tabbar label[for=tr48],#tr7:checked ~ .tabbar label[for=tr7],#tr30:checked ~ .tabbar label[for=tr30]{{background:#2b3b52;color:#fff;border-color:#4a5f80}}
.panels > .panel{{display:none}}
#tr24:checked ~ .panels .p24,#tr48:checked ~ .panels .p48,#tr7:checked ~ .panels .p7,#tr30:checked ~ .panels .p30{{display:block}}
.chart{{margin:6px 0 2px;width:100%;position:relative}}
.chart svg{{display:block}}
.movers{{margin:0;padding-left:18px;font-size:12.5px}}
.movers li{{margin:2px 0}}
.split{{display:grid;grid-template-columns:1fr;gap:10px}}
.muted{{color:var(--muted)}}
footer{{color:var(--muted);font-size:11px;margin-top:14px;line-height:1.7}}
a{{color:#6ab0ff}}
@media(min-width:560px){{.kpis{{grid-template-columns:repeat(4,1fr)}}.split{{grid-template-columns:1fr 1fr}}}}
@media(min-width:700px){{.mob{{display:none}}.full{{display:block}}}}
</style>
</head>
<body>
<header>
  <h1>天堂經典版 天幣幣值</h1>
  <div class="sub">資料來源：DD373 天幣掛單 ｜ 匯率 CNY→TWD {fx:.4f} ｜ 掃到 {len(ok)}/{len(d['servers']) + len(d.get('errs', []))} 台
  ｜ 全服掃描 {ts:%m/%d %H:%M}（{age_min} 分鐘前）{'　⚠️ 使用快取' if d.get('cached') and not fm_light else ''}
  ｜ 下次全服重掃 約 {next_upd:%H:%M}（±6 分）</div>
  <div class="sub">⚡ 飛馬每 15 分鐘輕掃：{light_txt}{'　✅ 已套用' if fm_light else ''}</div>
  {f'<div class="sub" style="color:#e67e22">🚫 掃描暫時退避中（疑似被 DD373 擋）：{bo["until"][:16].replace("T", " ")} 之後自動恢復，頁面先顯示上次資料</div>' if bo else ''}
</header>
{me_block}
<section class="card">
  <h2>全服 22 台排行<span>★ 金色＝買滿 5,000 萬金的加權幣值（1 台幣 ≈ N 金，越大越划算）</span></h2>
  <div class="mob">{mrows_html}</div>
  <div class="full chart" style="overflow-x:auto">
  <table>
    <thead><tr><th></th><th class="nm">伺服器</th><th class="pri">②5000萬 金/元 ★</th><th>①最低</th><th>變現</th><th>吃筆</th><th>掛單</th><th>vs上次</th></tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
  </div>
  <div class="notes">
    <div><span class="pri">★ 主要看「② 買滿 5,000 萬金」</span>＝1 台幣買到幾金（從最便宜往上吃到 5,000 萬金的加權值，越大＝金越便宜）；「①最低」＝全檔位最便宜的單筆單價（小額實際買得到）；「變現」＝DD373 收購區（數字越小＝賣金換到越多台幣）。底色較亮的那列＝飛馬。</div>
    <div>🟢 買金最划算：{', '.join(f"<b>{html.escape(s['server'])}</b>（②{s['vw2_gold_per_twd']:,.0f}）" for s in by_vw[:3])}</div>
    <div>💰 變現最划算（數字低）：{', '.join(f"<b>{html.escape(s['server'])}</b>（{s['sell_gold_per_twd']}）" for s in by_sell[:3])}</div>
    <div>🐳 深市場（大額不會被抬價）：{', '.join(f"{html.escape(s['server'])} {s['stock_wj']/10000:.1f}億" for s in deep)}</div>
    <div>⚠️ 薄市場（&lt;2 億金，數字易失真）：{', '.join(f"{html.escape(s['server'])} {s['stock_wj']/10000:.2f}億" for s in thin if s['stock_wj'] < 20000) or '無'}</div>
    <div class="muted">口徑：① ＝ 全檔位最低單價（小額實際買得到，1 台幣≈N 金）；② ＝ 從最便宜往上吃到 5,000 萬金為止的加權平均（＝買滿 5,000 萬金的實際幣值，本頁主數字）；變現 ＝ DD373 收購區掛單（數字越小＝賣金換到越多台幣）。</div>
  </div>
</section>
{trend_block}
<footer>
  {len(ok)} 台掃描 ｜ 產生時間 {now:%Y/%m/%d %H:%M:%S} ｜ 純腳本產頁（0 token）｜ 全服每小時重掃、飛馬每 15 分鐘輕掃<br>
  ⚠️ 平台掛單價格隨時變動，此頁為取樣時點的快照；短時間內個別服可跳 ±10%，單一快照別當趨勢。
</footer>
{chart_block}
</body>
</html>"""
    os.makedirs(PUBLIC_DIR, exist_ok=True)
    with open(os.path.join(PUBLIC_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(html_doc)
    json.dump({"ts": d["ts"], "fx": fx, "servers": ok}, open(os.path.join(DATA_DIR, "page-data.json"), "w"),
              ensure_ascii=False)
    print(f"[build] {os.path.relpath(os.path.join(PUBLIC_DIR, 'index.html'), PAGE_DIR)}"
          f"（{len(html_doc):,} bytes）、{len(ok)} 台", flush=True)


def apply_light_override(d):
    """把 15 分鐘輕掃的最新飛馬數字套進頁面資料（口徑與整頁掃描相同：同 1 種排序、同一個 ② 算法）。
    回傳輕掃資訊 dict 或 None。"""
    try:
        e = json.load(open(os.path.join(DATA_DIR, "fm-light.json"), encoding="utf-8"))["entries"][-1]
    except Exception:
        return None
    me = next((s for s in d.get("servers", []) if s.get("server") == USER_SERVER), None)
    if not me:
        return None
    for k in ("low_gold_per_twd", "vw2_gold_per_twd", "cost_twd_5000w", "used", "sell_gold_per_twd",
              "stock_wj", "n"):
        if e.get(k) is not None:
            me[k] = e[k]
    me["src"] = "light"
    return {"ts": e["ts"], "vw2": e.get("vw2_gold_per_twd")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true", help="只用快取／歷史，不連 DD373")
    ap.add_argument("--light", action="store_true",
                    help="每 15 分鐘的輕量發佈：不重掃全服，只把最新輕掃的飛馬數字套進頁面")
    a = ap.parse_args()
    if a.light:
        a.no_fetch = True
    d = fetch_sweep(force=False) if not a.no_fetch else (
        json.load(open(SWEEP_LATEST, encoding="utf-8")) if os.path.exists(SWEEP_LATEST) else
        {"fx": fetch_fx(), "ts": dt.datetime.now().isoformat(timespec="seconds"), "servers": [], "errs": []})
    if a.no_fetch and os.path.exists(SWEEP_LATEST):
        d["cached"] = True
    if a.light:
        info = apply_light_override(d)
        if info:
            d["fm_light"] = info
    build(d, prev_sweep())


if __name__ == "__main__":
    main()
