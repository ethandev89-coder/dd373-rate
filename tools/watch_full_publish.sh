#!/bin/bash
# 盯 03:20 的自動 full 發佈（切換正式版後第一次），完成後印出 log 供驗收
cd "$(dirname "$0")/.." || exit 1
while [ "$(date +%H%M)" \< "0322" ]; do sleep 20; done
sleep 30
echo "=== 03:15~03:29 的自動發佈 ==="
awk '/^===== 2026-10-17 03:(1[5-9]|2[0-9])/{p=1} p' logs/publish.log | head -60
echo "=== log 最後 6 行 ==="
tail -6 logs/publish.log
echo "=== 檢查：full 是重掃還是吃快取 ==="
awk '/^===== 2026-10-17 03:2/{p=1} p' logs/publish.log | grep -E "\[sweep\]|\[cache\]|\[build\]|pushed" | head -8
