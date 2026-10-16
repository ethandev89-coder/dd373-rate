#!/bin/bash
# 改版預覽版發佈：docs/preview.html + docs/version.json → commit → push
#
# 與 publish.sh 的關係：
#   - 兩者共用同一把互斥鎖 data/.publish.lock → 不會跟 15 分鐘的自動發佈撞在一起
#   - 本腳本只碰 docs/preview.html 與 docs/version.json，絕不碰 docs/index.html
#
# 用法：
#   publish-preview.sh              重建預覽頁（--light，不重掃）→ push
#   publish-preview.sh --no-jitter  不抖動（除錯用；預設本來就不抖動）
#   publish-preview.sh --quiet      不輸出
set -euo pipefail
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

QUIET=1
for a in "$@"; do
  case "$a" in
    --quiet) QUIET=0 ;;
  esac
done

LOG=logs/publish-preview.log
LOCK=data/.publish.lock
mkdir -p logs data

# 互斥鎖：等最多 ~75 秒；仍被佔用就放棄（讓給自動發佈，下一輪再試）
busy() { [ -d "$LOCK" ] && [ -n "$(find "$LOCK" -maxdepth 0 -mmin -8 2>/dev/null)" ]; }
if [ -d "$LOCK" ] && ! busy; then
  rm -rf "$LOCK"
fi
for i in 1 2 3; do
  if busy; then sleep 25; else break; fi
done
if busy; then
  echo "===== $(date '+%H:%M:%S') [skip] 發佈鎖被佔用（等 75s），下一輪再試" >> "$LOG"
  [ "$QUIET" = "1" ] && tail -1 "$LOG"
  exit 0
fi
mkdir -p "$LOCK"
trap 'rm -rf "$LOCK"' EXIT

{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') 預覽版發佈 ====="
  python3 build-page-next.py --light
  if [ -z "$(git status --porcelain docs/preview.html docs/version.json)" ]; then
    echo "[preview] 頁面無變化，略過 push"
  else
    git add docs/preview.html docs/version.json
    git commit -q -m "preview $(date '+%Y-%m-%d %H:%M') 改版預覽" || true
    # 不做 pull／rebase：這個 repo 只有本機在 push；而 `pull --rebase --autostash` 會把工作目錄裡
    # 未 commit 的資料檔 stash 再放回 → 改到 mtime（曾讓正式版快取誤判「1 分鐘前」而少掃一小時）。
    # push 被拒就保留本地 commit、記 log，下次再推。
    if ! git push -q origin main; then
      echo "[preview] push 失敗（遠端已更新？）→ 保留本地 commit，下次再推"
    fi
    echo "[preview] pushed"
  fi
} >> "$LOG" 2>&1

if [ "$QUIET" = "1" ]; then
  tail -3 "$LOG"
fi
