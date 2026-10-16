# DD373 天幣幣值（天堂經典版）

自動抓取 DD373 台服天幣掛單 → 產出單頁靜態網址，手機優先。

- 網址：https://ethandev89-coder.github.io/dd373-rate/
- 全服 22 台：①最低一筆、②買滿 5,000 萬金加權、5,000 萬金成本(NT$)、變現、掛單量
- 資料：`data/sweep-history.json`（每次掃描的原始 JSON，可回推趨勢）
- 頁面：`docs/index.html`（唯一發佈檔，GitHub Pages 來源＝main /docs）


## ② 口徑與「估算段」

- 頁面主數字＝**② 買滿 5,000 萬金加權幣值**（1 台幣≈N 金）。
- ② 的實測只從 2026-10-14 19:01 起（我們自己的每小時掃描＋15 分輕掃才同時存 ② 與實買）。
- 更早的歷史用 `backfill-vw2.py` 換算：**r = ② ÷ 實買**（逐服取中位，用同時段實測配對算），舊的實買序列 × r → `data/vw2-estimated-all.json`（22 台）與 `vw2-estimated.json`（飛馬）。
- 頁面上 **實線＝實測 ②、灰虛線＝估算 ②**，legend 標明比率與「非實測」。每次整頁發佈都會刷新估算段。
- ② 長期序列獨立存 `data/vw2-series.json`（上限 9,000 筆≈93 天），避免 30 天窗因 400 筆裁切而缺歷史。

## 檔案

| 檔案 | 用途 |
|---|---|
| `build-page.py` | 抓 DD373（22 台 × 1 排序）→ 算 ①/②/變現 → render `docs/index.html`；`--light` 則只把最新輕掃的飛馬數字套進頁面 |
| `backfill-vw2.py` | 舊實買歷史 → 估算 ②（逐服比率換算） |
| `publish.sh` | build（full 時先跑 backfill）→ commit → push；`--light`＝15 分鐘輕量發佈、`--no-jitter`＝除錯 |


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

## 改版（蘋果風）與退回方式

2026-10-17 改版：整頁改成蘋果風（CSS 變數＋深淺色）、走勢圖改手刻 SVG 渲染器（`static/trend.js`）、
加了 `static/page.js`（時間會動／5 分鐘檢查更新／記住時間軸）與 `static/apple.css`（設計系統）。
**單一產線**：`build-page.py` 同時負責正式版與預覽版。

```
python3 build-page.py            正式版 → docs/index.html + docs/version.json + data/page-data.json
python3 build-page.py --light    不重掃，套用最新 15 分鐘輕掃的飛馬數字（15 分鐘的 cron 用）
python3 build-page.py --preview  預覽版 → docs/preview.html（noindex＋橫幅＋前端測試參數、對 data/ 唯讀）
./publish.sh [--light] [--no-jitter]   重建（拿 data/.publish.lock）→ commit → push
DD373_FAKE_AGE_MIN=70 python3 build-page.py --light   測試用：假裝資料是 70 分鐘前（驗「已停更」橫幅）
```

預覽版專用前端參數（正式版沒有 `DD373_PREVIEW` 旗標，加了也不生效）：
`?now=2026-10-17T04:00`（假造現在時間）、`?poll=15`（把 5 分鐘檢查改成 15 秒）。

### 退回改版前（一行）

```bash
cd ~/.hermes/dd373-page && git checkout pre-apple -- build-page.py static/trend.js && git commit -m "rollback to pre-apple" && ./publish.sh --light --no-jitter
```

標籤 `pre-apple` = 改版前的正式版（舊 build-page.py ＋ 舊 static/trend.js）。
`build-page.py --preview` 與 `static/page.js` 留在新版是刻意的（預覽產線與行為層都還要），退回後不受影響。
