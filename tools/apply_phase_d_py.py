#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D 階段（Python 端）：降採樣聚合、圖上註記、version.json hash、預覽測試參數。"""
import io
import re

P = "build-page-next.py"
s = io.open(P, encoding="utf-8").read()
orig = s

# ---------- 1) hashlib（version.json 內容 hash） ----------
assert "import html\nimport json" in s
s = s.replace("import argparse\nimport html\nimport json",
              "import argparse\nimport hashlib\nimport html\nimport json", 1)

# ---------- 2) 聚合函式（降採樣：中位數＋min–max） ----------
anchor = '''def append_vw2_series(ts_iso, value):'''
assert anchor in s
agg_fn = '''def agg_series(points, bucket_sec):
    """降採樣：把實測點聚成固定長度的時段（台北時間對齊），每段取中位數＋min／max＋筆數。
    points = [(naive_t, v)]；回傳 [(naive_t(段起點), 中位數, min, max, 筆數)]。"""
    import statistics as st
    buckets = {}
    for t, v in points:
        k = int((t.replace(tzinfo=TZ).timestamp() + 8 * 3600) // bucket_sec)
        buckets.setdefault(k, []).append(v)
    out = []
    for k in sorted(buckets):
        vs = sorted(buckets[k])
        start = k * bucket_sec - 8 * 3600
        out.append((dt.datetime.fromtimestamp(start, TZ).replace(tzinfo=None),
                    st.median(vs), min(vs), max(vs), len(vs)))
    return out


'''
s = s.replace(anchor, agg_fn + anchor, 1)

# ---------- 3) payload：聚合序列 + 註記 + 每窗設定 ----------
old = '''    chart_windows = [
        # gapReal／gapEst：相鄰兩筆超過此秒數就斷線（實測 30 分；估算抽樣稀疏 → 用 12 小時）
        {"id": "24h", "label": "24 小時", "span": 86400, "color": "#e74c3c", "gapReal": 1800, "gapEst": 43200},
        {"id": "48h", "label": "48 小時", "span": 172800, "color": "#d35400", "gapReal": 1800, "gapEst": 43200},
        {"id": "7d", "label": "7 天", "span": 604800, "color": "#e67e22", "gapReal": 1800, "gapEst": 43200},
        {"id": "30d", "label": "30 天", "span": 2592000, "color": "#c0392b", "gapReal": 1800, "gapEst": 43200},
    ]'''
old = re.search(r'    chart_windows = \[\n.*?\n    \]', s, re.S)
assert old, "找不到 chart_windows"
new_windows = '''    # 降採樣：7 天窗＝1 小時中位數（＋min–max 帶）；30 天窗＝4 小時
    agg7 = agg_series([(t, v) for t, v in all_vw2], 3600)
    agg30 = agg_series([(t, v) for t, v in all_vw2], 4 * 3600)
    # 聚合後的離群標記（5 筆 Hampel，跟原始序列同一套規則）
    chart_agg = {}
    for key, rows in (("7d", agg7), ("30d", agg30)):
        h = hampel([(t, med) for t, med, _lo, _hi, _n in rows])
        chart_agg[key] = [[ep(t), round(pv, 1), round(raw, 1), 1 if o else 0,
                           round(rows[i][2], 1), round(rows[i][3], 1), rows[i][4]]
                          for i, (t, pv, raw, o) in enumerate(h)]

    # 圖上註記：第 2 次移民期色帶、開始實測直線（時間一律台北）
    chart_notes = [
        {"type": "band", "from": ep(dt.datetime(2026, 9, 16, 0, 0)),
         "to": ep(dt.datetime(2026, 9, 30, 0, 0)), "label": "第 2 次移民 9/16–9/29"},
        {"type": "vline", "t": ep(dt.datetime(2026, 10, 14, 19, 1)), "label": "開始實測 10/14 19:01"},
    ]

    chart_windows = [
        # gapReal／gapEst：相鄰兩筆超過此秒數就斷線（實測 30 分；估算抽樣稀疏 → 用 12 小時）
        # agg：這個窗要用哪一組降採樣序列（沒有就畫原始點）
        {"id": "24h", "label": "24 小時", "span": 86400, "gapReal": 1800, "gapEst": 43200},
        {"id": "48h", "label": "48 小時", "span": 172800, "gapReal": 1800, "gapEst": 43200},
        {"id": "7d", "label": "7 天", "span": 604800, "gapReal": 1800, "gapEst": 43200,
         "agg": "7d", "gapAgg": 3 * 3600},
        {"id": "30d", "label": "30 天", "span": 2592000, "gapReal": 1800, "gapEst": 43200,
         "agg": "30d", "gapAgg": 3 * 4 * 3600},
    ]'''
s = s[:old.start()] + new_windows + s[old.end():]

# payload 加 agg / notes
old_payload = '''        "est": [[ep(t), round(v, 1)] for t, v in est_all],
    }'''
assert old_payload in s
s = s.replace(old_payload, '''        "est": [[ep(t), round(v, 1)] for t, v in est_all],
        # 降採樣：[段起點, 中位數(畫線用), 原始中位數, 是否離群, min, max, 筆數]
        "agg": chart_agg,
        "notes": chart_notes,
    }''')

# ---------- 4) version.json 加內容 hash ----------
old_v = '''        json.dump({"built": now.isoformat(timespec="seconds"), "out": OUT_HTML, "ts": d["ts"]},
                  open(os.path.join(PUBLIC_DIR, "version.json"), "w"), ensure_ascii=False)'''
assert old_v in s
s = s.replace(old_v, '''        # built＝每次發佈都會變（前端用它判斷有沒有新資料）；hash＝內容指紋（保險）
        json.dump({"built": now.isoformat(timespec="seconds"),
                   "hash": hashlib.sha256(html_doc.encode("utf-8")).hexdigest()[:16],
                   "out": OUT_HTML, "ts": d["ts"]},
                  open(os.path.join(PUBLIC_DIR, "version.json"), "w"), ensure_ascii=False)''')

# ---------- 5) 頁面：預覽旗標＋產頁時間（前端判斷新舊用）＋預覽專用測試參數說明 ----------
old_flag = '''<script>window.DD373WINDOWS='''
assert old_flag in s
s = s.replace(old_flag, '''<script>window.DD373_PREVIEW=true;window.DD373_BUILT="{now.isoformat(timespec="seconds")}";</script>
<script>window.DD373WINDOWS=''', 1)

# ---------- 6) 停更橫幅（預設隱藏；時間由 JS 依規則顯示）＋ page.js 內嵌 ----------
old_bo = '''{bo_block}
<header class="page-head">'''
assert old_bo in s
s = s.replace(old_bo, '''{bo_block}
<div class="notice bad" id="stale-banner" hidden></div>
<header class="page-head">''')

# 內嵌 page.js（行為層）
old_inline = """        + ';</script>\\n<script>\\n' + trend_js_src + '\\n</script>')"""
assert old_inline in s
s = s.replace(old_inline, """        + ';</script>\\n<script>\\n' + trend_js_src + '\\n</script>'
        + '<script>\\n' + page_js_src + '\\n</script>')""")

old_read = '    apple_css = open(APPLE_CSS, encoding="utf-8").read()'
assert old_read in s
s = s.replace(old_read, old_read + '\n    page_js_src = open(PAGE_JS, encoding="utf-8").read()')

old_const = 'APPLE_CSS = os.path.join(PAGE_DIR, "static", "apple.css")'
assert old_const in s
s = s.replace(old_const, old_const + '\nPAGE_JS = os.path.join(PAGE_DIR, "static", "page.js")     # 行為層（時間／輪詢／狀態還原）')


# ---------- 7) 膠囊加上 data-fresh（讓 JS 接管新鮮度狀態） ----------
old_cap = '<span class="capsule{fresh_cls}"><span class="d"></span><span data-ts="{ep(me_ts)}" data-rel="1">{fresh_txt}</span></span>'
assert old_cap in s, "找不到膠囊標記"
s = s.replace(old_cap, '<span class="capsule{fresh_cls}" data-fresh><span class="d"></span><span data-ts="{ep(me_ts)}" data-rel="1">{fresh_txt}</span></span>')

io.open(P, "w", encoding="utf-8").write(s)
print("D（Python 端）轉換完成：", len(orig), "->", len(s), "bytes")
