# DD373 天幣幣值（天堂經典版）

自動抓取 DD373 台服天幣掛單 → 產出單頁靜態網址，手機優先。

- 網址：https://ethandev89-coder.github.io/dd373-rate/
- 全服 22 台：①最低一筆、②買滿 5,000 萬金加權、5,000 萬金成本(NT$)、變現、掛單量
- 資料：`data/sweep-history.json`（每次掃描的原始 JSON，可回推趨勢）
- 頁面：`docs/index.html`（唯一發佈檔，GitHub Pages 來源＝main /docs）

## 檔案

| 檔案 | 用途 |
|---|---|
| `build-page.py` | 抓 DD373（22 台 × 3 排序）→ 算 ①/②/成本/變現 → render `docs/index.html` |
| `publish.sh` | build → git commit → push（Pages 自動更新）；log 在 `logs/publish.log` |
| `data/sweep-latest.json` | 最近一次掃描原始資料（build 45 分鐘內沿用快取，不重抓） |
| `data/page-data.json` | 頁面用的精簡資料 |

## 口徑

- **① 最低一筆** = 全檔位最低單價（小額實際買得到的價）
- **② 買滿 5,000 萬金** = 從最便宜往上吃到 5,000 萬金為止的量加權平均（大額實際成本）＝長期基準
- **變現** = DD373 收購區掛單，數字越低＝賣金換到越多台幣
- 趨勢圖用的是 cron 另一支掃描的「實買口徑」，**與 ①② 不能直接比大小**（頁面上有標）

## 手動操作

```bash
python3 build-page.py            # 抓新的並產頁
python3 build-page.py --no-fetch # 用快取產頁（離線測版面）
bash publish.sh                  # 產頁 + 發佈到 GitHub Pages
```

純腳本產頁，0 token。
