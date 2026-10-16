#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DD373 天幣幣價網頁 builder（預覽版產線，改版期間用）

與 build-page.py 的關係：
  - build-page.py      → 產 docs/index.html（正式版，15 分鐘的 cron 在發佈）
  - build-page-next.py → 產 docs/preview.html + docs/version.json（改版預覽，只手動發佈）
  兩份互不影響：預覽版不覆寫 docs/index.html，也不覆寫 data/page-data.json。

流程：
  1) 抓取：22 台 x 1 排序（走 dd373-rate-query skill 的 lt_rate_v2.server_rate）→ ①/②/成本/變現
  2) 讀歷史：~/.hermes/data/dd373-multiserver-history.json → 趨勢圖
  3) 產出：docs/preview.html（單檔、內嵌 CSS/JS）+ docs/version.json
  4) 發佈：publish-preview.sh（拿 data/.publish.lock → commit → push）

用法：
  python3 build-page-next.py            # 正常：抓新的（若快取 <CACHE_MIN 分鐘則沿用）
  python3 build-page-next.py --light    # 不重掃：套用最新 15 分鐘輕掃的飛馬數字（截圖用）
  python3 build-page-next.py --no-fetch # 只用快取／歷史（離線測試）
"""
import argparse
import hashlib
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
OUT_HTML = "preview.html"      # 改版期間：預覽版（正式版 docs/index.html 由 build-page.py 產，兩者互不影響）
WRITE_VERSION = True           # 產 docs/version.json（前端每 5 分鐘用它判斷有沒有新資料）
WRITE_PAGE_DATA = False        # 預覽版不覆寫 data/page-data.json（那是正式版發佈在追蹤的檔案）
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
TREND_JS = os.path.join(PAGE_DIR, "static", "trend-next.js")
APPLE_CSS = os.path.join(PAGE_DIR, "static", "apple.css")
PAGE_JS = os.path.join(PAGE_DIR, "static", "page.js")     # 行為層（時間／輪詢／狀態還原）   # 蘋果風設計系統（build 時內嵌）   # 預覽版專用渲染器（改版期間不動正式版的 trend.js）   # 走勢圖渲染器（build 時內嵌進單檔頁面）


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
    """預覽產線不得掃描／不得寫入 data/：資料一律沿用正式版產線的結果。"""
    raise RuntimeError("build-page-next.py 對 data/ 唯讀：請用 --light（沿用正式版掃描結果）")

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


def agg_series(points, bucket_sec):
    """降採樣：把實測點聚成固定長度的時段（台北時間對齊），每段取中位數＋min／max＋筆數。
    points = [(naive_t, v)]；回傳 [(naive_t(段起點), 中位數, min, max, 筆數)]。"""
    import statistics as st
    buckets = {}
    for t, v in points:
        k = int((t.replace(tzinfo=TZ).timestamp() + 8 * 3600) // bucket_sec)
        buckets.setdefault(k, []).append(v)
    out = []
    for k in sorted(buckets):
        vs = sorted(buckets[k])
        start = k * bucket_sec - 8 * 3600
        out.append((dt.datetime.fromtimestamp(start, TZ).replace(tzinfo=None),
                    st.median(vs), min(vs), max(vs), len(vs)))
    return out


def append_vw2_series(ts_iso, value):
    raise RuntimeError("build-page-next.py 對 data/ 唯讀：② 長期序列由 build-page.py 寫入")

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
    if me_age <= 30:                     # D 階段會改成每 60 秒由 JS 重算
        fresh_cls, fresh_txt = "", f"{me_age} 分鐘前"
    elif me_age <= 60:
        fresh_cls, fresh_txt = " warn", "資料延遲"
    else:
        fresh_cls, fresh_txt = " bad", f"資料停更 {me_age} 分鐘"
    fx = d["fx"]
    ok = [s for s in d["servers"] if s.get("vw2_gold_per_twd")]
    by_vw = sorted(ok, key=lambda s: -s["vw2_gold_per_twd"])
    by_sell = sorted([s for s in ok if s.get("sell_gold_per_twd")],
                     key=lambda s: s["sell_gold_per_twd"])
    thin = sorted(ok, key=lambda s: s["stock_wj"])[:3]
    deep = sorted(ok, key=lambda s: -s["stock_wj"])[:3]
    series = load_series()
    ser_g = [(t, g) for t, g, _s, _k in series]
    deltas = all_server_delta()

    # ---------- 漲跌／差值（一律文字＋▲▼–，不用 emoji） ----------
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
                       f"<span class='muted'>（{e:,.0f} → {l:,.0f} 金/元）</span></li>")
        return f"<ul class='movers {sign}'>{''.join(out)}</ul>"

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
    # 降採樣：7 天窗＝1 小時中位數（＋min–max 帶）；30 天窗＝4 小時
    agg7 = agg_series([(t, v) for t, v in all_vw2], 3600)
    agg30 = agg_series([(t, v) for t, v in all_vw2], 4 * 3600)
    # 聚合後的離群標記（5 筆 Hampel，跟原始序列同一套規則）
    chart_agg = {}
    for key, rows in (("7d", agg7), ("30d", agg30)):
        h = hampel([(t, med) for t, med, _lo, _hi, _n in rows])
        chart_agg[key] = [[ep(t), round(pv, 1), round(raw, 1), 1 if o else 0,
                           round(rows[i][2], 1), round(rows[i][3], 1), rows[i][4]]
                          for i, (t, pv, raw, o) in enumerate(h)]

    # 圖上註記：第 2 次移民期色帶、開始實測直線（時間一律台北）
    chart_notes = [
        {"type": "band", "from": ep(dt.datetime(2026, 9, 16, 0, 0)),
         "to": ep(dt.datetime(2026, 9, 30, 0, 0)), "label": "第 2 次移民 9/16–9/29"},
        {"type": "vline", "t": ep(dt.datetime(2026, 10, 14, 19, 1)), "label": "開始實測 10/14 19:01"},
    ]

    chart_payload = {
        "now": ep(t_now),
        "r": r_used,
        # [epoch, 平滑值, 原始值, 是否離群]
        "real": [[ep(t), round(v, 1), round(raw, 1), 1 if o else 0] for t, v, raw, o in chart_real],
        "est": [[ep(t), round(v, 1)] for t, v in est_all],
        # 降採樣：[段起點, 中位數(畫線用), 原始中位數, 是否離群, min, max, 筆數]
        "agg": chart_agg,
        "notes": chart_notes,
    }
    chart_windows = [
        # gapReal／gapEst：相鄰兩筆超過此秒數就斷線（實測 30 分；估算抽樣稀疏 → 用 12 小時）
        # agg：這個窗要用哪一組降採樣序列（沒有就畫原始點）
        {"id": "24h", "label": "24 小時", "span": 86400, "gapReal": 1800, "gapEst": 43200},
        {"id": "48h", "label": "48 小時", "span": 172800, "gapReal": 1800, "gapEst": 43200},
        {"id": "7d", "label": "7 天", "span": 604800, "gapReal": 1800, "gapEst": 43200,
         "agg": "7d", "gapAgg": 3 * 3600},
        {"id": "30d", "label": "30 天", "span": 2592000, "gapReal": 1800, "gapEst": 43200,
         "agg": "30d", "gapAgg": 3 * 4 * 3600},
    ]

    def panel(win_id, cls, npts):
        """一個時間窗＝本時窗摘要＋圖（圖例在分段控制下方同一行）"""
        if npts < 2:
            return (f'<div class="panel {cls}"><p class="muted">這個時間窗還在累積（目前 {npts} 點，'
                    f'② 口徑自 {all_vw2[0][0]:%m/%d %H:%M} 開始記錄）</p></div>')
        return (f'<div class="panel {cls}"><div class="wsum" data-win="{win_id}"></div>'
                f'<div class="chart" data-win="{win_id}"></div></div>')

    acc_note = (f"② 口徑自 {all_vw2[0][0]:%m/%d %H:%M} 開始記錄"
                + (f"（目前累積 {span_h:.0f} 小時）" if span_h else "")
                + "，7 天／30 天窗會逐日長出來")
    trend_block = f"""
<section class="card lg">
  <div class="tabs">
    <input type="radio" name="tr" id="tr24" checked>
    <input type="radio" name="tr" id="tr48">
    <input type="radio" name="tr" id="tr7">
    <input type="radio" name="tr" id="tr30">
    <div class="tabbar">
      <label for="tr24">24 小時</label><label for="tr48">48 小時</label><label for="tr7">7 天</label><label for="tr30">30 天</label>
    </div>
    <div class="lgd">
      <span class="li"><svg width="20" height="8" viewBox="0 0 20 8" aria-hidden="true"><path class="lg-real" d="M1,4 H19"/></svg>實測</span>
      <span class="li"><svg width="20" height="8" viewBox="0 0 20 8" aria-hidden="true"><path class="lg-est" d="M1,4 H19"/></svg>估算</span>
    </div>
    <div class="panels">
      {panel("24h", "p24", n24)}
      {panel("48h", "p48", n48)}
      {panel("7d", "p7", n7d)}
      {panel("30d", "p30", n30d)}
    </div>
  </div>
</section>"""

    movers_block = f"""
<section class="card">
  <h2>移民期均值 vs 近 3 天<span>基準期 9/17~9/29＝第 2 次移民期（估算）／近 3 天（實測為主）｜同一把尺＝② 買滿 5,000 萬金加權</span></h2>
  <div class="split">
    <div><h3>金變便宜（1 元買到更多金）</h3>{mover_list(movers_up, 'up')}</div>
    <div><h3>金變貴（1 元買到更少金）</h3>{mover_list(movers_dn, 'down')}</div>
  </div>
</section>"""

    # ---- 走勢圖：資料 JSON + 視窗設定 + 渲染器（全部內嵌，維持單檔、無外部依賴）----
    apple_css = open(APPLE_CSS, encoding="utf-8").read()
    page_js_src = open(PAGE_JS, encoding="utf-8").read()
    trend_js_src = open(TREND_JS, encoding="utf-8").read()
    preview_flag = ('<script>window.DD373_PREVIEW=true;'
                    'window.DD373_BUILT="' + now.isoformat(timespec="seconds") + '";</script>\n')
    chart_block = (
        '<script type="application/json" id="chart-data">'
        + json.dumps(chart_payload, separators=(",", ":"))
        + '</script>\n' + preview_flag + '<script>window.DD373WINDOWS='
        + json.dumps(chart_windows, ensure_ascii=False, separators=(",", ":"))
        + ';</script>\n<script>\n' + trend_js_src + '\n</script>'
        + '<script>\n' + page_js_src + '\n</script>')


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
  <p class="hero-l">1 台幣買 {me['vw2_gold_per_twd']:,.0f} 金 · {chg_line}</p>
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
      <p><b>② 買滿 5,000 萬金加權（本頁主數字）</b>＝從最便宜的掛單往上吃、累積到 5,000 萬金為止的加權平均單價，換算成金／元（<b>數字越大＝天幣越便宜</b>）。</p>
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
      <p class="mt">降採樣與圖上註記</p>
      <p>7 天窗把實測聚合成 <b>1 小時中位數</b>、30 天窗聚合成 <b>4 小時中位數</b>；淡色帶是該時段的 <b>min–max</b> 區間。滑過去看得到該時段的中位數、區間與筆數；某個時段完全沒資料就斷線，不會硬連。</p>
      <p>圖上的淡色背景帶＝<b>第 2 次移民期（9/16–9/29）</b>；虛線直線＝實測 ② 開始記錄的時間（<b>10/14 19:01</b>）。</p>
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

    html_doc = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
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
  <span class="capsule{fresh_cls}" data-fresh><span class="d"></span><span data-ts="{ep(me_ts)}" data-rel="1">{fresh_txt}</span></span>
</nav>
{bo_block}
<div class="notice bad" id="stale-banner" hidden></div>
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
</html>"""
    os.makedirs(PUBLIC_DIR, exist_ok=True)
    with open(os.path.join(PUBLIC_DIR, OUT_HTML), "w", encoding="utf-8") as f:
        f.write(html_doc)
    if WRITE_VERSION:
        # built＝每次發佈都會變（前端用它判斷有沒有新資料）；hash＝內容指紋（保險）
        json.dump({"built": now.isoformat(timespec="seconds"),
                   "hash": hashlib.sha256(html_doc.encode("utf-8")).hexdigest()[:16],
                   "out": OUT_HTML, "ts": d["ts"]},
                  open(os.path.join(PUBLIC_DIR, "version.json"), "w"), ensure_ascii=False)
    if WRITE_PAGE_DATA:
        json.dump({"ts": d["ts"], "fx": fx, "servers": ok}, open(os.path.join(DATA_DIR, "page-data.json"), "w"),
                  ensure_ascii=False)
    print(f"[build] {os.path.relpath(os.path.join(PUBLIC_DIR, OUT_HTML), PAGE_DIR)}"
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
    a.no_fetch = True          # 預覽產線對 data/ 唯讀：永遠不掃描、不寫資料檔
    if not a.light:
        print("[preview] 提醒：本產線不掃描，資料沿用正式版（如需最新輕掃數字請加 --light）", flush=True)
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
