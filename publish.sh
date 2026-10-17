#!/bin/bash
# 建頁 → commit → push（GitHub Pages 自動發佈）
#
# 用法：
#   publish.sh                 整頁發佈：重掃全服（22 台 × 1 排序）→ 產頁 → push（含 0~6 分隨機抖動）
#   publish.sh --light         輕量發佈：不重掃，只把最新 15 分鐘輕掃的飛馬數字套進頁面 → push
#   publish.sh --no-jitter     不抖動（除錯用）
#   publish.sh --quiet         不輸出（給 cron 當 no_agent 用，避免污染推播訊息）
set -euo pipefail
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

MODE="full"; QUIET=1; JITTER_ON=1
for a in "$@"; do
  case "$a" in
    --light) MODE="light" ;;
    --quiet) QUIET=0 ;;
    --no-jitter) JITTER_ON=0 ;;
  esac
done

LOG=logs/publish.log
LOCK=data/.publish.lock
mkdir -p logs data

# 抖動先睡（不佔鎖），避免擋住下一個排程
JITTER=0
if [ "$MODE" = "full" ] && [ "$JITTER_ON" = "1" ]; then
  JITTER=$(python3 -c "import random;print(random.randint(0,360))")
  if [ "$JITTER" -gt 0 ]; then sleep "$JITTER"; fi
fi

# 互斥鎖：等最多 ~75 秒；仍被佔用就放棄（避免兩個 cron 互相覆蓋）
busy() { [ -d "$LOCK" ] && [ -n "$(find "$LOCK" -maxdepth 0 -mmin -8 2>/dev/null)" ]; }
if [ -d "$LOCK" ] && ! busy; then
  rm -rf "$LOCK"                     # 清掉崩潰留下的舊鎖
fi
for i in 1 2 3; do
  if busy; then sleep 25; else break; fi
done
if busy; then
  echo "===== $(date '+%H:%M:%S') [skip] 另一個發佈進行中（等 75s 仍被佔用）" >> "$LOG"
  exit 0
fi
mkdir -p "$LOCK"
trap 'rm -rf "$LOCK"' EXIT

{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S')（$MODE${JITTER:+, 抖動 ${JITTER}s}）====="
  if [ "$MODE" = "light" ]; then
    python3 build-page.py --light
  else
    python3 backfill-vw2.py || true          # 刷新「估算 ②」長歷史（由舊的實買序列換算）
    python3 build-page.py
  fi
  # ② 長期序列：每筆一定要有 ts/v；s（掛單量）可以是 null，缺值由寫入端印警告（不擋發佈）
  if ! python3 tests/test-vw2-series.py --data-only >/dev/null 2>&1; then
    echo "[publish] ⚠️ data/vw2-series.json 有筆數缺 ts/v → python3 tests/test-vw2-series.py 看明細"
  fi
  if [ -z "$(git status --porcelain docs/index.html data/page-data.json docs/version.json data/vw2-series.json)" ]; then
    echo "[publish] 頁面無變化，略過 push"
  else
    git add docs/index.html data/page-data.json data/vw2-series.json
    [ -f docs/version.json ] && git add docs/version.json || true     # 正式版切換後才有這個檔
    git commit -q -m "update $(date '+%Y-%m-%d %H:%M') 幣價" || true
    git push -q origin main
    echo "[publish] pushed"
  fi
} >> "$LOG" 2>&1

if [ "$QUIET" = "1" ]; then
  tail -3 "$LOG"
fi
