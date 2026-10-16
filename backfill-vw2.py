#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把舊的 DD373 歷史（9/15 起、cron 的「實買」口徑）換算成「大約的 ② 買滿 5,000 萬金加權幣值」。

為什麼可以這樣換算：
  同一時刻的實測配對（② 和實買）只從今天 19:01 起才有（我們自己的每小時掃描兩者都存）。
  用這些配對算出比率 r = ② ÷ 實買（**逐服**算，樣本不足才退回全服中位），
  再把 9/15 起的實買序列乘上 r，就得到「大約的 ②」。＝估算值，不是實測值 → 頁面會標明。

輸出：
  data/vw2-estimated.json      飛馬單服（走勢卡用）
  data/vw2-estimated-all.json  22 台（全服比較卡用）：
      {"updated","global_r","r":{服:比率},"series":{服:[{"ts","v"}...]}}

用法：python3 backfill-vw2.py [--days 35]
"""
import argparse
import datetime as dt
import json
import os
import statistics as st

HOME = os.path.expanduser("~")
DATA = os.path.join(HOME, ".hermes", "dd373-page", "data")
SWEEP_HISTORY = os.path.join(DATA, "sweep-history.json")
CRON_HISTORY = os.path.join(HOME, ".hermes", "data", "dd373-multiserver-history.json")
OUT_ONE = os.path.join(DATA, "vw2-estimated.json")
OUT_ALL = os.path.join(DATA, "vw2-estimated-all.json")
USER_SERVER = "飛馬珀伽索斯"


def ratios():
    """逐服算 ②÷實買 的中位比率（配對只在我們自己的掃描裡有）；回傳 ({服:r}, 全部樣本, 全服中位)。"""
    try:
        runs = json.load(open(SWEEP_HISTORY, encoding="utf-8"))
    except Exception:
        return {}, [], None
    per, allr = {}, []
    for run in runs:
        for s in run.get("servers", []):
            v, f = s.get("vw2_gold_per_twd"), s.get("floor_eff_gold")
            if v and f:
                per.setdefault(s["server"], []).append(v / f)
                allr.append(v / f)
    global_r = st.median(allr) if len(allr) >= 3 else None
    out = {}
    for nm, rs in per.items():
        out[nm] = st.median(rs) if len(rs) >= 3 else (global_r or 1.0)
    return out, allr, global_r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=35)
    a = ap.parse_args()

    per_r, allr, global_r = ratios()
    if not global_r:
        print("沒有可用的配對樣本（sweep-history 還沒有同時含 ② 與實買的資料）→ 不產生估算檔")
        return
    spread = f"{min(allr):.4f}~{max(allr):.4f}" if len(allr) > 1 else "—"

    hist = json.load(open(CRON_HISTORY, encoding="utf-8"))
    cut = dt.datetime.now().astimezone() - dt.timedelta(days=a.days)
    series, counts = {}, {}
    for run in hist.get("runs", []):
        try:
            t = dt.datetime.fromisoformat(run["ts"])
        except Exception:
            continue
        if t < cut:
            continue
        for nm, s in (run.get("servers") or {}).items():
            if not s.get("gold_floor"):
                continue
            r = per_r.get(nm, global_r)
            key = t.isoformat(timespec="seconds")[:16]
            series.setdefault(nm, {})[key] = round(s["gold_floor"] * r, 1)
            counts[nm] = counts.get(nm, 0) + 1

    out_all = {"updated": dt.datetime.now().isoformat(timespec="seconds"),
               "global_r": round(global_r, 5),
               "r": {k: round(v, 5) for k, v in per_r.items()},
               "series": {nm: [{"ts": k, "v": v} for k, v in sorted(d.items())]
                          for nm, d in series.items()}}
    json.dump(out_all, open(OUT_ALL, "w", encoding="utf-8"), ensure_ascii=False)

    if USER_SERVER in series:
        r = per_r.get(USER_SERVER, global_r)
        json.dump([{"ts": k, "v": v, "r": round(r, 5), "src": "est"}
                   for k, v in sorted(series[USER_SERVER].items())],
                  open(OUT_ONE, "w", encoding="utf-8"), ensure_ascii=False)

    print(f"全服中位比率 r = ② ÷ 實買 = {global_r:.5f}（配對 {len(allr)} 組，區間 {spread}）")
    print(f"估算 {len(series)} 台；{USER_SERVER} {counts.get(USER_SERVER, 0)} 筆"
          f"（r={per_r.get(USER_SERVER, global_r):.5f}）")
    print(f"→ {OUT_ALL} / {OUT_ONE}")


if __name__ == "__main__":
    main()
