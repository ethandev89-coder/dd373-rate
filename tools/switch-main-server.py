#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""換主伺服器（使用者移民時用）：網頁、15 分輕掃、全服掃描抓全頁、告警、幣商報價的「自己那台」一起換。

用法（在兩次 cron 之間跑：避開 :07/:22/:37/:52 輕掃與 :20~:28 整頁發佈）：
  python3 tools/switch-main-server.py 天神宙斯 天神 --dry-run   # 先看會做什麼（什麼都不改）
  python3 tools/switch-main-server.py 天神宙斯 天神             # 正式

做的事：
  1) 舊主伺服器專屬的資料檔搬進 data/archive/<舊服名>-<YYYYMMDD>/：
       fm-light.json（15 分輕掃）、vw2-series.json（② 長期序列）、vw2-estimated.json（估算 ②）
  2) 新主伺服器的 ② 長期序列用歷史重建（約每小時一點）→ data/vw2-series.json：
       目前的 sweep-history.json ＋ git 裡每一版 data/page-data.json（每次發佈都存了 22 台當下的數字）
       ＋ 最早那版 sweep-history.json；同一分鐘只留一筆。
       掛單量 s 一律 null：換服前這台每小時只抓第 1 頁，掛單量偏低約一半，不能跟之後的全頁數字接在一起。
  3) 寫 main-server.json（server／short／tok／since＝現在；舊的記進 previous）
  4) 重跑 backfill-vw2.py（估算 ② 改成新伺服器）
  5) cron「DD373…輕掃＋頁面更新」改名＋改說明（hermes cron edit；--no-cron 跳過）
之後：跑 tests/ 全過 → git commit；下一次 15 分輕掃就會掃新伺服器並更新網頁。
2026-11-08 第一次用（飛馬珀伽索斯 → 天神宙斯）。
"""
import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys

HOME = os.path.expanduser("~")
PAGE = os.path.join(HOME, ".hermes", "dd373-page")
DATA = os.path.join(PAGE, "data")
CFG = os.path.join(PAGE, "main-server.json")
JOBS = os.path.join(HOME, ".hermes", "cron", "jobs.json")
FIRST_SWEEP_COMMIT = "70fddae8"    # 最早一版 data/sweep-history.json（2026-10-14 19:01 ~ 10-17 03:23）
MOVE = ("fm-light.json", "vw2-series.json", "vw2-estimated.json")
# 還沒有 main-server.json 的年代（2026-11-08 以前）主伺服器是飛馬
LEGACY = {"server": "飛馬珀伽索斯", "short": "飛馬", "tok": "prr508", "since": "2026-09-28T19:00:00"}
LIGHT_CRON_SCRIPT = "dd373-page-light.sh"


def git(*args, inp=None):
    return subprocess.run(["git", "-C", PAGE, *args], input=inp, capture_output=True, check=True).stdout


def rebuild_series(name):
    """新主伺服器的 ② 歷史（每小時）→ [{"ts","v","s":None}]（時間遞增）。"""
    pts = {}

    def add(ts, v):
        if ts and v:
            pts.setdefault(ts[:16], (ts[:19], v))

    def from_runs(runs):
        for run in runs:
            s = next((x for x in run.get("servers", []) if x.get("server") == name), None)
            if s:
                add(run.get("ts"), s.get("vw2_gold_per_twd"))

    try:
        from_runs(json.load(open(os.path.join(DATA, "sweep-history.json"), encoding="utf-8")))
    except Exception as e:
        print(f"[warn] 讀不到 sweep-history.json：{e}")
    try:
        from_runs(json.loads(git("show", f"{FIRST_SWEEP_COMMIT}:data/sweep-history.json")))
    except Exception as e:
        print(f"[warn] 讀不到 {FIRST_SWEEP_COMMIT} 那版 sweep-history.json：{e}")
    # git 裡每一版 page-data.json：ts＝那次整頁掃描的時間、servers＝那次 22 台的數字
    revs = git("rev-list", "HEAD", "--", "data/page-data.json").decode().split()
    out = git("cat-file", "--batch", inp="".join(f"{r}:data/page-data.json\n" for r in revs).encode())
    i = n = 0
    while i < len(out):
        nl = out.index(b"\n", i)
        head = out[i:nl].split()
        i = nl + 1
        if len(head) < 3 or head[1] == b"missing":
            continue
        size = int(head[2])
        blob, i = out[i:i + size], i + size + 1
        try:
            d = json.loads(blob)
        except Exception:
            continue
        s = next((x for x in d.get("servers", []) if x.get("server") == name), None)
        if s and s.get("src") != "light":       # 只收整頁掃描的數字（src=light 是當時主伺服器的輕掃覆寫）
            add(d.get("ts"), s.get("vw2_gold_per_twd"))
            n += 1
    print(f"[seed] git 裡 page-data.json {len(revs)} 版，其中 {n} 版有 {name}")
    return [{"ts": ts, "v": v, "s": None} for ts, v in sorted(pts.values())]


def rename_cron(short):
    """把 15 分輕掃那支 cron 的名稱／說明換成新服（用 hermes CLI，不直接改 jobs.json）。"""
    try:
        d = json.load(open(JOBS, encoding="utf-8"))
    except Exception as e:
        print(f"[cron] 讀不到 {JOBS}：{e} → 請手動改名")
        return
    jobs = d.get("jobs", d) if isinstance(d, dict) else d
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    job = next((j for j in jobs if isinstance(j, dict) and j.get("script") == LIGHT_CRON_SCRIPT), None)
    if not job:
        print(f"[cron] 找不到 script={LIGHT_CRON_SCRIPT} 的 job → 請手動改名")
        return
    name = f"DD373{short}輕掃＋頁面更新"
    desc = (f"{short}（主伺服器，設定在 ~/.hermes/dd373-page/main-server.json）單台輕掃（15 分）："
            "sort=5（比例最佳＝單價升冪）＋自動抓完所有頁 → ② 變動 ≥5% → 私訊；抓不到連續 2 次 → 退避 6 小時並告警。"
            "筆數校驗（過濾前讀到的卡片數要等於頁面「为您找到 N 条记录」）對不上 → 寫 log 警告（stderr，不私訊）。"
            "移民換服：python3 ~/.hermes/dd373-page/tools/switch-main-server.py <全名> <短名>。")
    exe = os.path.join(HOME, ".local", "bin", "hermes")
    cmd = [exe if os.path.exists(exe) else "hermes", "cron", "edit", job["id"], "--name", name, "--prompt", desc]
    r = subprocess.run(cmd, capture_output=True, text=True)
    print(f"[cron] {job.get('name')} → {name}（rc={r.returncode}）" + (f"：{(r.stdout + r.stderr).strip()[:300]}" if r.returncode else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("server", help="新主伺服器全名（要跟掃描結果的 server 欄位一字不差，例：天神宙斯）")
    ap.add_argument("short", help="網頁／私訊用的短名（例：天神）")
    ap.add_argument("--dry-run", action="store_true", help="只印計畫，什麼都不改")
    ap.add_argument("--no-cron", action="store_true", help="不改 cron 名稱")
    a = ap.parse_args()

    old = json.load(open(CFG, encoding="utf-8")) if os.path.exists(CFG) else dict(LEGACY)
    if old["server"] == a.server:
        sys.exit(f"主伺服器已經是 {a.server}，不用換")
    latest = json.load(open(os.path.join(DATA, "sweep-latest.json"), encoding="utf-8"))
    tok = next((s["tok"] for s in latest.get("servers", []) if s.get("server") == a.server and s.get("tok")), None)
    if not tok:
        names = "、".join(s["server"] for s in latest.get("servers", []))
        sys.exit(f"sweep-latest.json 找不到「{a.server}」（要用全名）。現有：{names}")

    now = dt.datetime.now().replace(microsecond=0)
    arch = os.path.join(DATA, "archive", f"{old['server']}-{now:%Y%m%d}")
    series = rebuild_series(a.server)
    if not series:
        sys.exit(f"重建不出 {a.server} 的 ② 歷史 → 不換（請先查 sweep-history／git）")
    print(f"[plan] 主伺服器 {old['server']}（{old.get('tok')}）→ {a.server}（{tok}），since {now.isoformat()}")
    print(f"[plan] 搬進 {os.path.relpath(arch, PAGE)}/："
          + "、".join(f for f in MOVE if os.path.exists(os.path.join(DATA, f))))
    print(f"[plan] 新 vw2-series.json：{len(series)} 點，{series[0]['ts']} → {series[-1]['ts']}（掛單量留空）")
    if a.dry_run:
        print("[dry-run] 什麼都沒改")
        return

    os.makedirs(arch, exist_ok=True)
    for f in MOVE:
        src = os.path.join(DATA, f)
        if os.path.exists(src):
            shutil.move(src, os.path.join(arch, f))
    tmp = os.path.join(DATA, f"vw2-series.json.tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(series, fh, ensure_ascii=False)
    os.replace(tmp, os.path.join(DATA, "vw2-series.json"))

    prev = list(old.get("previous", []))
    prev.append({"server": old["server"], "short": old.get("short"), "tok": old.get("tok"),
                 "since": old.get("since"), "until": now.isoformat(), "archive": os.path.relpath(arch, PAGE)})
    cfg = {"server": a.server, "short": a.short, "tok": tok, "since": now.isoformat(), "previous": prev}
    tmp = f"{CFG}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    os.replace(tmp, CFG)
    print(f"[ok] 寫入 {os.path.relpath(CFG, PAGE)}")

    r = subprocess.run([sys.executable, os.path.join(PAGE, "backfill-vw2.py")], capture_output=True, text=True)
    print("[backfill] rc=%d %s" % (r.returncode, (r.stdout + r.stderr).strip().replace("\n", " ｜ ")))

    if not a.no_cron:
        rename_cron(a.short)
    print("[next] python3 tests/test-*.py 全過 → git commit；下一次 15 分輕掃（:07/:22/:37/:52）就會掃新伺服器")


if __name__ == "__main__":
    main()
