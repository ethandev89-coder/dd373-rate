#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 data/vw2-series.json 裡缺 `s`（市場總掛單量，萬天幣）的筆數補回來 —— **手動工具**。

什麼時候用：`s` 現在允許是 `null`（缺就存 null，tooltip 那一點不顯示掛單量，這是刻意設計）。
只有在「確定掛單解析已經修好／想把歷史補齊」時才手動跑這支；**不要放進 cron 或 publish**
（自動回填會蓋掉「當時真的抓不到」的事實）。

背景：這檔案有兩條寫入路徑（每小時整頁掃描、15 分鐘輕掃），輕掃那條以前只寫 ts/v，
造成 59 筆沒有 s。現在兩條都走 `~/.hermes/scripts/dd373_series.py` 的 append_series，
缺 s 會存 null（不沿用上一筆），連續 4 次會告警一次。

用法：
  python3 tools/backfill-series-stock.py --dry-run     # 只看會補幾筆
  python3 tools/backfill-series-stock.py               # 備份 → 補 → 驗證 v 完全沒變
  python3 tools/backfill-series-stock.py --tol 45      # 掛單量取樣容許誤差（分鐘）
  python3 tools/backfill-series-stock.py --quiet       # 沒東西要補就不輸出

保證：
  * 動筆前先備份成 data/vw2-series.json.bak-YYYYmmdd-HHMMSS
  * 只新增 `s`，`ts`／`v`／筆數／順序一律不動；補完會逐筆比對 v 並印出結果
"""
import argparse
import datetime as dt
import json
import os
import shutil
import sys

HOME = os.path.expanduser("~")
PAGE = os.path.join(HOME, ".hermes", "dd373-page")
DATA = os.path.join(PAGE, "data")
SERIES = os.path.join(DATA, "vw2-series.json")
FM_LIGHT = os.path.join(DATA, "fm-light.json")
SWEEP_HISTORY = os.path.join(DATA, "sweep-history.json")
HISTORY = os.path.join(HOME, ".hermes", "data", "dd373-multiserver-history.json")
USER_SERVER = "飛馬珀伽索斯"
TZ = dt.timezone(dt.timedelta(hours=8))


def local_naive(iso):
    d = dt.datetime.fromisoformat(iso)
    if d.tzinfo is not None:
        d = d.astimezone(TZ).replace(tzinfo=None)
    return d.replace(second=0, microsecond=0)


def load_stock_series():
    """掛單量樣本（萬天幣）：fm-light → sweep-history → 舊全服紀錄；同一分鐘取第一個。"""
    pts = {}
    if os.path.exists(FM_LIGHT):
        try:
            for e in json.load(open(FM_LIGHT, encoding="utf-8")).get("entries", []):
                if e.get("stock_wj"):
                    pts.setdefault(local_naive(e["ts"]), e["stock_wj"])
        except Exception as e:
            print(f"[warn] fm-light 讀取失敗：{e}", file=sys.stderr)
    if os.path.exists(SWEEP_HISTORY):
        try:
            for run in json.load(open(SWEEP_HISTORY, encoding="utf-8")):
                s = next((x for x in run.get("servers", []) if x.get("server") == USER_SERVER), None)
                if s and s.get("stock_wj"):
                    pts.setdefault(local_naive(run["ts"]), s["stock_wj"])
        except Exception as e:
            print(f"[warn] sweep-history 讀取失敗：{e}", file=sys.stderr)
    if os.path.exists(HISTORY):
        try:
            for run in json.load(open(HISTORY, encoding="utf-8")).get("runs", []):
                s = (run.get("servers") or {}).get(USER_SERVER)
                if s and s.get("stock_wj"):
                    pts.setdefault(local_naive(run["ts"]), s["stock_wj"])
        except Exception as e:
            print(f"[warn] 舊全服紀錄讀取失敗：{e}", file=sys.stderr)
    return sorted(pts.items())


def nearest(series, t, tol_min):
    import bisect
    if not series:
        return None
    times = [p[0] for p in series]
    i = bisect.bisect_left(times, t)
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(series):
            d = abs((series[j][0] - t).total_seconds())
            if best is None or d < best[0]:
                best = (d, series[j][1])
    if best and best[0] <= tol_min * 60:
        return best[1]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--tol", type=int, default=45, help="掛單量取樣容許誤差（分鐘）")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(SERIES):
        print(f"[backfill] 找不到 {SERIES}", file=sys.stderr)
        return 1
    ser = json.load(open(SERIES, encoding="utf-8"))
    before = [(e.get("ts"), e.get("v")) for e in ser]          # 驗證用：ts/v 快照
    missing = [e for e in ser if not e.get("s")]

    if not missing:
        if not a.quiet:
            print(f"[backfill] {len(ser)} 筆全部都有 s，不用補")
        return 0

    stocks = load_stock_series()
    filled, still = 0, 0
    for e in missing:
        s = nearest(stocks, local_naive(e["ts"]), a.tol)
        if s is None:
            still += 1
            continue
        if not a.dry_run:
            e["s"] = s
        filled += 1

    if a.dry_run:
        print(f"[backfill][dry-run] {len(ser)} 筆，缺 s {len(missing)} 筆 → 可補 {filled}、補不到 {still}")
        return 0

    if filled:
        bak = f"{SERIES}.bak-{dt.datetime.now():%Y%m%d-%H%M%S}"
        shutil.copy2(SERIES, bak)
        # 只改 s：把填好的寫回（順序、筆數、ts、v 都不動）
        tmp = SERIES + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(ser, f, ensure_ascii=False)
        os.replace(tmp, SERIES)

        after = [(e.get("ts"), e.get("v")) for e in json.load(open(SERIES, encoding="utf-8"))]
        same = before == after
        left = sum(1 for e in json.load(open(SERIES, encoding="utf-8")) if not e.get("s"))
        print(f"[backfill] 備份 {os.path.basename(bak)}")
        print(f"[backfill] 補了 {filled} 筆 s；仍缺 {left} 筆（{still} 筆找不到 {a.tol} 分內的掛單量樣本）")
        print(f"[backfill] ts/v/筆數完全沒變：{'✅ 是' if same else '❌ 否'}")
        if not same:
            print("[backfill] ❌ 資料被動到，請用備份還原：" + bak, file=sys.stderr)
            return 2
    else:
        print(f"[backfill] 缺 {len(missing)} 筆，但都找不到 {a.tol} 分內的掛單量樣本 → 沒動檔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
