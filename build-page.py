#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DD373 天幣幣價網頁 builder（蘋果風改版後的單一產線）

產線（單一檔案）：
  python3 build-page.py            正式版 → docs/index.html + docs/version.json + data/page-data.json
  python3 build-page.py --light    不重掃，套用最新 15 分鐘輕掃的飛馬數字（每 15 分鐘的 cron）
  python3 build-page.py --preview  預覽版 → docs/preview.html（noindex＋橫幅＋前端測試參數、對 data/ 唯讀）
  DD373_FAKE_AGE_MIN=70 python3 build-page.py --light
                                   測試用：假裝資料是 70 分鐘前 → 驗「已停更」橫幅

流程：
  1) 抓取：22 台 x 1 排序（走 dd373-rate-query skill 的 lt_rate_v2.server_rate）→ ①/②/成本/變現
  2) 讀歷史：~/.hermes/data/dd373-multiserver-history.json → 趨勢圖
  3) 產出：docs/index.html（單檔、內嵌 CSS/JS）+ docs/version.json
  4) 發佈：publish.sh（拿 data/.publish.lock → commit → push）

用法：
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
PREVIEW = False                # main() 依 --preview 設定（預覽版：preview.html＋noindex＋前端測試參數）
OUT_HTML = "index.html"        # 產出檔名（--preview 會改成 preview.html）
WRITE_VERSION = True           # 產 docs/version.json（前端每 5 分鐘用它判斷有沒有新資料）
WRITE_PAGE_DATA = True         # 產 data/page-data.json（--preview 時不寫，免得動到正式版在追蹤的檔）
FAKE_AGE_MIN = int(os.environ.get("DD373_FAKE_AGE_MIN", "0"))   # 測試用：把資料時間往前推 N 分鐘
HISTORY = os.path.join(HOME, ".hermes", "data", "dd373-multiserver-history.json")
SWEEP_LATEST = os.path.join(DATA_DIR, "sweep-latest.json")
SWEEP_HISTORY = os.path.join(DATA_DIR, "sweep-history.json")
CACHE_MIN = 45          # 快取新鮮度（分鐘）：build 比 sweep 跑得頻繁時不重抓
USER_SERVER = "飛馬珀伽索斯"
TARGET_WJ = 5000.0      # ② 口徑固定：買滿 5,000 萬天幣
SORTS = ("1",)          # 只抓「綜合」排序：DD373 頁面本身依單價升序，1 種排序就有最便宜那批
                        # （3 種排序只是多幾個樣本；要全取樣跑 dd373-all-sweep.py）
BACKOFF_HOURS = 6       # 疑似被擋時退避幾小時
ERR_RATIO_MAX = 0.4     # 錯誤台數比例超過此值 → 判定被擋

TZ = dt.timezone(dt.timedelta(hours=8))      # Asia/Taipei（台灣無日光節約，固定 +8）
TREND_JS = os.path.join(PAGE_DIR, "static", "trend.js")       # 走勢圖渲染器（單一檔案）
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



# ---------- 產頁 ----------
def pct(v):
    if v is None:
        return "—"
    return f"{v:+.1f}%"


# ---- 台幣口徑（頁面主數字一律用「買 5,000 萬天幣要多少台幣」）----
def nt5000(s):
    """買滿 5,000 萬天幣的台幣成本（② 口徑換算；越低＝金越便宜）"""
    if s.get("cost_twd_5000w"):
        return s["cost_twd_5000w"]
    v = s.get("vw2_gold_per_twd")
    return round(50_000_000 / v) if v else None


def nt_per_wj(gold_per_twd):
    """最低一筆的單價：每萬天幣多少台幣"""
    return round(10000 / gold_per_twd, 2) if gold_per_twd else None


def nt_sell_5000(s):
    """把 5,000 萬天幣賣給 DD373 收購區可拿到的台幣（越高越好）"""
    v = s.get("sell_gold_per_twd")
    return round(50_000_000 / v) if v else None


def nt5000_from_gold(gold_per_twd):
    """由『1 台幣≈N 金』換算買 5,000 萬天幣的台幣成本（走勢圖用）"""
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
    if FAKE_AGE_MIN:                 # 測試用：假裝資料是 N 分鐘前（驗「已停更」橫幅）
        me_ts = me_ts - dt.timedelta(minutes=FAKE_AGE_MIN)
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
    thin = sorted(ok, key=lambda s: s["stock_wj"])[:3]
    deep = sorted(ok, key=lambda s: -s["stock_wj"])[:3]

    # ---------- 漲跌／差值（一律文字＋▲▼–，不用 emoji） ----------
    def delta_pct(cur, old):
        """回傳百分比（float）或 None"""
        if not cur or not old:
            return None
        return (cur / old - 1) * 100

    def arrow_of(pct):
        """(css class, 文字) —— 幣值上升＝1 台幣買到更多金＝天幣變便宜"""
        if pct is None:
            return "flat", "—"
        if pct > 0.05:
            return "up", f"▲ +{pct:.1f}%"
        if pct < -0.05:
            return "down", f"▼ −{abs(pct):.1f}%"
        return "flat", "– 0.0%"

    def gold_dir(pct):
        """方向用文字講（不用紅綠）：天幣變便宜／天幣變貴／持平"""
        if pct is None:
            return "—"
        if pct > 0.05:
            return "天幣變便宜"
        if pct < -0.05:
            return "天幣變貴"
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
            f'<div class="rline2">最低一筆 {srv["low_gold_per_twd"]:,.0f}'
            f' ・ 掛單 {srv["stock_wj"] / 10000:.2f} 億天幣'
            f'</div></div>')


    s_now = load_vw2_series(24, "vw2_gold_per_twd")     # ② 買滿 5,000 萬天幣加權（15 分輕掃 + 每小時全服）
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
    vw2_note = (f"② 買滿 5,000 萬天幣加權幣值（15 分輕掃 + 每小時全服）｜{n24} 點｜{latest_txt}")
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
      <span class="li" data-tip="10/14 19:01 以前沒有實測，用舊的實買序列 × {r_txt} 換算；看趨勢可以，單點數字別當真"><svg width="20" height="8" viewBox="0 0 20 8" aria-hidden="true"><path class="lg-est" d="M1,4 H19"/></svg>估算</span>
      <span class="li" data-tip="單次掃描偏離前後中位數太多，主線用中位數，圈圈標原始值"><svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><circle class="lg-out" cx="6" cy="6" r="3.6"/></svg>離群點</span>
    </div>
    <div class="panels">
      {panel("24h", "p24", n24)}
      {panel("48h", "p48", n48)}
      {panel("7d", "p7", n7d)}
      {panel("30d", "p30", n30d)}
    </div>
  </div>
</section>"""


    # ---- 走勢圖：資料 JSON + 視窗設定 + 渲染器（全部內嵌，維持單檔、無外部依賴）----
    apple_css = open(APPLE_CSS, encoding="utf-8").read()
    page_js_src = open(PAGE_JS, encoding="utf-8").read()
    trend_js_src = open(TREND_JS, encoding="utf-8").read()
    # 產頁時間一律帶（前端據此判斷有沒有新資料）；DD373_PREVIEW 只有預覽版才帶
    preview_flag = ('<script>window.DD373_BUILT="' + now.isoformat(timespec="seconds") + '";'
                    + ('window.DD373_PREVIEW=true;' if PREVIEW else '') + '</script>\n')
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
    thin_tag = ('<span class="tag" data-tip="掛單總量不到 2 億天幣，單一賣家就能推動數字，看趨勢不要看單點">薄市場</span>'
                if (me and me["stock_wj"] < 20000) else "")
    hero_block = ""
    ranking_block = ""
    if me:
        rank = rank_of[me["server"]]
        hero_block = f"""
<section class="card lg hero">
  <div class="hero-n"><b>{me['vw2_gold_per_twd']:,.0f}</b><span>天幣/元</span></div>
  <p class="hero-l">等於 1:{me['vw2_gold_per_twd']:,.0f} · {chg_line}</p>
  <div class="hero-3">
    <div><label data-tip="全部掛單裡最便宜的一筆，小額才買得到這個價">最低一筆</label><b>{me['low_gold_per_twd']:,.0f}</b></div>
    <div><label>掛單（天幣）</label><b>{me['stock_wj'] / 10000:.2f} 億</b> {thin_tag}</div>
  </div>
</section>"""
        ranking_block = f"""
<section class="card">
  <details class="dsc">
    <summary><span>全服排行</span><span class="dsub">飛馬第 {rank}／{n_servers} 名</span></summary>
    <div class="dbody glist">{''.join(rank_rows)}</div>
  </details>
</section>"""

    bo_block = (f'<div class="notice">掃描暫時退避中（疑似被 DD373 擋）：'
                f'{bo["until"][:16].replace("T", " ")} 之後自動恢復，頁面先顯示上次資料</div>' if bo else '')

    if PREVIEW:
        preview_banner = (f'<div class="preview-banner">預覽版・資料停在 {me_ts:%m/%d %H:%M}'
                          f'（<span data-ts="{ep(me_ts)}" data-rel="1">{me_age} 分鐘前</span>）'
                          f'　正式版：<a href="./index.html">index.html</a></div>')
        preview_head = '<meta name="robots" content="noindex">'
    else:
        preview_banner = ""
        preview_head = ""

    html_doc = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{preview_head}
<title>飛馬珀伽索斯 天幣幣值（DD373）</title>
<style>
{apple_css}
.notice {{
  margin: 0 0 16px; padding: 12px; border-radius: 12px;
  background: var(--warn-soft); color: var(--warn); font-size: 13px;
}}
</style>
</head>
<body>
{preview_banner}
<nav class="nav">
  <span class="nav-t">天幣幣值</span>
  <span class="capsule{fresh_cls}" data-fresh><span class="d"></span><span data-ts="{ep(me_ts)}" data-rel="1">{fresh_txt}</span></span>
</nav>
{bo_block}
<div class="notice bad" id="stale-banner" hidden></div>
<header class="page-head">
  <h1>飛馬珀伽索斯</h1>
  <p class="sub" data-tip="從最便宜的掛單一路往上買，湊滿 5,000 萬天幣的平均價；數字越大＝天幣越便宜">DD373 · 買滿 5,000 萬天幣加權</p>
</header>
{hero_block}
{trend_block}
{ranking_block}
<footer>
  產生時間 {now:%Y/%m/%d %H:%M:%S} ｜ 資料來源：DD373 天幣掛單（天堂經典版台服）｜ 匯率 CNY→TWD {fx:.4f}<br>
  飛馬每 15 分鐘、全服每小時更新。平台掛單價格隨時變動，本頁為取樣時點的快照；短時間內個別服可跳 ±10%，單一快照別當趨勢。
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
    global PREVIEW, OUT_HTML, WRITE_PAGE_DATA
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true", help="只用快取／歷史，不連 DD373")
    ap.add_argument("--light", action="store_true",
                    help="每 15 分鐘的輕量發佈：不重掃全服，只把最新輕掃的飛馬數字套進頁面")
    ap.add_argument("--preview", action="store_true",
                    help="產 docs/preview.html（預覽版：加 noindex＋橫幅、帶前端測試參數、不寫 page-data.json、不掃描）")
    a = ap.parse_args()
    if a.preview:
        PREVIEW, OUT_HTML, WRITE_PAGE_DATA = True, "preview.html", False
        a.no_fetch = True            # 預覽版對 data/ 唯讀：資料一律沿用正式版產線
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
