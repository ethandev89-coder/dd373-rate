#!/bin/bash
# 建頁 → commit → push（GitHub Pages 自動發佈）
set -euo pipefail
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

LOG=logs/publish.log
mkdir -p logs
{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') ====="
  python3 build-page.py
  if [ -z "$(git status --porcelain docs/index.html data/page-data.json)" ]; then
    echo "[publish] 頁面無變化，略過 push"
  else
    git add docs/index.html data/page-data.json
    git commit -q -m "update $(date '+%Y-%m-%d %H:%M') 幣價" || true
    git push -q origin main
    echo "[publish] pushed"
  fi
} >> "$LOG" 2>&1
tail -3 "$LOG"
