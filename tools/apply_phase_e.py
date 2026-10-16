#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E 階段：把預覽產線併回正式產線（單一 build-page.py）
   1) build-page.py 接手新設計（--preview 產 preview.html，其餘產 index.html）
   2) 正式版不帶 DD373_PREVIEW（?now / ?poll 不生效）
   3) 加上 DD373_FAKE_AGE_MIN（builder 端測試用：假裝資料是 N 分鐘前，驗停更橫幅）
   4) 清掉死碼（svg_line）、移除預覽專用的 raise 守門
每一步都有 assert（改不到就大聲失敗）。"""
import io
import re

PROD = io.open("build-page.py", encoding="utf-8").read()        # 目前正式版（資料層＋舊 render）
FORK = io.open("build-page-next.py", encoding="utf-8").read()   # 新設計
s = FORK

# ---------- 1) 用正式版的 fetch_sweep（有 ts 快取 + 真的掃描）取代預覽版的 raise ----------
m_prod = re.search(r"def fetch_sweep\(force=False\):.*?\n(?=\ndef )", PROD, re.S)
assert m_prod, "正式版找不到 fetch_sweep"
prod_fetch = m_prod.group(0)
m_fork = re.search(r"def fetch_sweep\(force=False\):.*?\n(?=\ndef )", s, re.S)
assert m_fork, "預覽版找不到 fetch_sweep"
s = s[:m_fork.start()] + prod_fetch + s[m_fork.end():]

# ---------- 2) append_vw2_series：拿掉「唯讀」守門（正式版要寫 ② 長期序列） ----------
old_guard = '''def append_vw2_series(ts_iso, value):
    raise RuntimeError("build-page-next.py 對 data/ 唯讀：② 長期序列由 build-page.py 寫入")
'''
assert old_guard in s
s = s.replace(old_guard, "def append_vw2_series(ts_iso, value):\n")

# ---------- 3) 移除死碼 svg_line（舊手刻 SVG，B 階段後已沒人用） ----------
m_svg = re.search(r"# ---------- 繪圖（純 SVG，無 JS） ----------\ndef svg_line\(.*?\n(?=\n# ---------- )", s, re.S)
assert m_svg, "找不到 svg_line"
assert "svg_line(" not in s.replace(m_svg.group(0), ""), "還有地方在用 svg_line"
s = s[:m_svg.start()] + s[m_svg.end():]

# ---------- 4) 常數：PREVIEW / OUT_HTML / 測試用 FAKE_AGE_MIN ----------
old_const = '''OUT_HTML = "preview.html"      # 改版期間：預覽版（正式版 docs/index.html 由 build-page.py 產，兩者互不影響）
WRITE_VERSION = True           # 產 docs/version.json（前端每 5 分鐘用它判斷有沒有新資料）
WRITE_PAGE_DATA = False        # 預覽版不覆寫 data/page-data.json（那是正式版發佈在追蹤的檔案）'''
assert old_const in s
s = s.replace(old_const, '''PREVIEW = False                # main() 依 --preview 設定（預覽版：preview.html＋noindex＋前端測試參數）
OUT_HTML = "index.html"        # 產出檔名（--preview 會改成 preview.html）
WRITE_VERSION = True           # 產 docs/version.json（前端每 5 分鐘用它判斷有沒有新資料）
WRITE_PAGE_DATA = True         # 產 data/page-data.json（--preview 時不寫，免得動到正式版在追蹤的檔）
FAKE_AGE_MIN = int(os.environ.get("DD373_FAKE_AGE_MIN", "0"))   # 測試用：把資料時間往前推 N 分鐘''')

# ---------- 5) 預覽專屬橫幅／noindex／title／旗標，全部用 PREVIEW 開關 ----------
old_banner = '''<div class="preview-banner">預覽版・資料停在 {me_ts:%m/%d %H:%M}（<span data-ts="{ep(me_ts)}" data-rel="1">{me_age} 分鐘前</span>）　正式版：<a href="./index.html">index.html</a></div>
'''
assert old_banner in s
s = s.replace(old_banner, '''{preview_banner}
''')

old_head = '''<meta name="robots" content="noindex">
<title>飛馬珀伽索斯 天幣幣值（DD373）— 預覽版</title>'''
assert old_head in s
s = s.replace(old_head, '''{preview_head}
<title>飛馬珀伽索斯 天幣幣值（DD373）</title>''')

old_flag = "    preview_flag = ('<script>window.DD373_PREVIEW=true;'\n                    'window.DD373_BUILT=\"' + now.isoformat(timespec=\"seconds\") + '\";</script>\\n')"
assert old_flag in s, "找不到 preview_flag"
s = s.replace(old_flag, '''    # 產頁時間一律帶（前端據此判斷有沒有新資料）；DD373_PREVIEW 只有預覽版才帶
    preview_flag = ('<script>window.DD373_BUILT="' + now.isoformat(timespec="seconds") + '";'
                    + ('window.DD373_PREVIEW=true;' if PREVIEW else '') + '</script>\\n')''')

# preview_banner / preview_head 變數（在 html_doc 之前組）
old_doc = "    html_doc = f\"\"\"<!DOCTYPE html>"
assert old_doc in s
s = s.replace(old_doc, '''    if PREVIEW:
        preview_banner = (f'<div class="preview-banner">預覽版・資料停在 {me_ts:%m/%d %H:%M}'
                          f'（<span data-ts="{ep(me_ts)}" data-rel="1">{me_age} 分鐘前</span>）'
                          f'　正式版：<a href="./index.html">index.html</a></div>')
        preview_head = '<meta name="robots" content="noindex">'
    else:
        preview_banner = ""
        preview_head = ""

''' + old_doc)

# ---------- 6) FAKE_AGE_MIN（測試用）套在 me_ts ----------
old_ts = '''    me_ts = dt.datetime.fromisoformat(fm_light["ts"]) if fm_light else ts
    me_age = int((now - me_ts).total_seconds() // 60)'''
assert old_ts in s
s = s.replace(old_ts, '''    me_ts = dt.datetime.fromisoformat(fm_light["ts"]) if fm_light else ts
    if FAKE_AGE_MIN:                 # 測試用：假裝資料是 N 分鐘前（驗「已停更」橫幅）
        me_ts = me_ts - dt.timedelta(minutes=FAKE_AGE_MIN)
    me_age = int((now - me_ts).total_seconds() // 60)''')

# ---------- 7) main()：--preview 切換輸出與唯讀 ----------
m_main = re.search(r"def main\(\):.*?\n(?=\nif __name__)", s, re.S)
assert m_main, "找不到 main"
s = s[:m_main.start()] + '''def main():
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


''' + s[m_main.end():]

# ---------- 8) 檔頭說明 ----------
s = s.replace('"""DD373 天幣幣價網頁 builder（預覽版產線，改版期間用）',
              '"""DD373 天幣幣價網頁 builder（蘋果風改版後的單一產線）')
old_doc2 = """與 build-page.py 的關係：
  - build-page.py      → 產 docs/index.html（正式版，15 分鐘的 cron 在發佈）
  - build-page-next.py → 產 docs/preview.html + docs/version.json（改版預覽，只手動發佈）
  兩份互不影響：預覽版不覆寫 docs/index.html，也不覆寫 data/page-data.json。

流程："""
assert old_doc2 in s
s = s.replace(old_doc2, """產線（單一檔案）：
  python3 build-page.py            正式版 → docs/index.html + docs/version.json + data/page-data.json
  python3 build-page.py --light    不重掃，套用最新 15 分鐘輕掃的飛馬數字（每 15 分鐘的 cron）
  python3 build-page.py --preview  預覽版 → docs/preview.html（noindex＋橫幅＋前端測試參數、對 data/ 唯讀）
  DD373_FAKE_AGE_MIN=70 python3 build-page.py --light
                                   測試用：假裝資料是 70 分鐘前 → 驗「已停更」橫幅

流程：""")
s = s.replace("  3) 產出：docs/preview.html（單檔、內嵌 CSS/JS）+ docs/version.json\n  4) 發佈：publish-preview.sh（拿 data/.publish.lock → commit → push）",
              "  3) 產出：docs/index.html（單檔、內嵌 CSS/JS）+ docs/version.json\n  4) 發佈：publish.sh（拿 data/.publish.lock → commit → push）")
s = s.replace('  python3 build-page-next.py            # 正常：抓新的（若快取 <CACHE_MIN 分鐘則沿用）\n  python3 build-page-next.py --light    # 不重掃：套用最新 15 分鐘輕掃的飛馬數字（截圖用）\n  python3 build-page-next.py --no-fetch # 只用快取／歷史（離線測試）\n', "")

io.open("build-page.py", "w", encoding="utf-8").write(s)
print("build-page.py 合併完成：", len(PROD), "->", len(s), "bytes")
