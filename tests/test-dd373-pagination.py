#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DD373 掛單頁解析／多頁抓取／去重的守門測試。

背景（2026-10-18 使用者到 DD373 實測回報）：飛馬有 28~31 筆掛單（20 條/頁 → 2 頁），
舊版只讀「綜合排序」第 1 頁，造成：
  * 掛單量少算第 2 頁（1.36 億 vs 實際 3.17 億）→ 誤標「薄市場」
  * 最便宜那筆若在第 2 頁，①／② 都偏低
  * 去重鍵用「單價＋起售＋庫存」→ 兩筆一模一樣的掛單被併成一筆（少算 2,500 萬）

這支測試鎖住：
  1. 頁面 N（为您找到 N 条记录）／每頁條數解析
  2. 逐卡解析帶商品編號（AddCart('DB…')），欄位缺漏的卡不會讓整頁掉資料
  3. 去重改用商品編號：單價／起售／庫存完全相同的兩筆仍然是兩筆
  4. 多頁：N>20 會續抓第 2 頁；筆數校驗（過濾前卡片數 vs N）對不上要回 warning
  5. 過濾條件（起售 <50 萬金）只標記 valid，卡片仍算進「讀到幾筆」
  6. 三支產線（輕掃／整頁掃描／全服歷史）都真的用了 sort=5 或 all_pages

fixtures/fm-page1.html、fm-page2.html 是 2026-10-18 11:06 飛馬 sort=5 的兩頁（已裁掉 script/style）。

用法：python3 tests/test-dd373-pagination.py
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "..", "scripts"))
import lt_rate_v2 as L                                        # noqa: E402

FX_DIR = os.path.join(HERE, "fixtures")
P1 = open(os.path.join(FX_DIR, "fm-page1.html"), encoding="utf-8").read()
P2 = open(os.path.join(FX_DIR, "fm-page2.html"), encoding="utf-8").read()


class FakeFetch:
    """依 URL 的頁碼回傳 fixture；記錄被打了幾個請求、哪些 URL。"""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def __call__(self, url, timeout=25):
        self.calls.append(url)
        page = 1
        m = L.re.search(r"-0-0-0-7mp223-0-0-0-0-0-(\d+)-0-", url)
        if m:
            page = int(m.group(1))
        if page > len(self.pages):
            return self.pages[-1]
        return self.pages[page - 1]


class PageMetaTest(unittest.TestCase):
    def test_url_template_has_page_and_sort(self):
        url = L.PAGE.format(tok="prr508", page=2, sort="5")
        self.assertTrue(url.endswith("-0-0-0-7mp223-0-0-0-0-0-2-0-5-0.html"), url)

    def test_record_count(self):
        self.assertEqual(L.record_count(P1), 28)
        self.assertEqual(L.record_count(P2), 28)

    def test_page_size(self):
        self.assertEqual(L.page_size(P1), 20)

    def test_record_count_missing_returns_none(self):
        self.assertIsNone(L.record_count("<html><body>沒有筆數</body></html>"))


class CardParseTest(unittest.TestCase):
    def test_每張卡都有商品編號(self):
        cards = L.parse_cards_detailed(P1)
        self.assertEqual(len(cards), 20)
        self.assertTrue(all(c["id"] for c in cards), [c for c in cards if not c["id"]])
        self.assertEqual(len({c["id"] for c in cards}), 20)

    def test_欄位都解得到(self):
        for c in L.parse_cards_detailed(P1):
            self.assertIsNotNone(c["unit"], c)
            self.assertIsNotNone(c["qishou"], c)
            self.assertIsNotNone(c["kucun"], c)

    def test_起售過小的卡只被標記不從清單移除(self):
        cards = L.parse_cards_detailed(P1) + L.parse_cards_detailed(P2)
        small = [c for c in cards if c["qishou"] < L.MIN_QTY]
        self.assertEqual(len(small), 2, small)
        self.assertTrue(all(not c["valid"] for c in small))
        self.assertEqual(len(cards), 28)          # 過濾前仍是 28 筆

    def test_舊的parse_cards會把重複掛單併掉(self):
        """兩筆 (2.99, 500, 500) 與兩筆 (2.99, 2000, 2000)：舊鍵＝三欄位 → 少 2 筆。"""
        cards = L.parse_cards_detailed(P1) + L.parse_cards_detailed(P2)
        triples = [(c["unit"], c["qishou"], c["kucun"]) for c in cards if c["valid"]]
        dup = [t for t in set(triples) if triples.count(t) > 1]
        self.assertEqual(len(dup), 2, dup)                      # fixture 真的含兩組重複
        # 新：以商品編號去重 → 26 筆有效（28 筆扣掉 2 筆起售 <50）
        self.assertEqual(len(L.parse_cards(P1)) + len(L.parse_cards(P2)), 26)


class PaginationTest(unittest.TestCase):
    def test_抓完兩頁且筆數對得上(self):
        f = FakeFetch([P1, P2])
        cards, meta, html1 = L.fetch_pages("prr508", "5", fetch_fn=f)
        self.assertEqual(len(f.calls), 2, f.calls)               # 第 1 頁 + 第 2 頁
        self.assertIn("-0-0-0-7mp223-0-0-0-0-0-2-0-5-0.html", f.calls[1])
        self.assertEqual(meta["count"], 28)
        self.assertEqual(meta["cards_read"], 28)
        self.assertEqual(meta["unique"], 28)
        self.assertEqual(meta["pages"], 2)
        self.assertTrue(meta["match"])
        self.assertEqual(meta["warnings"], [])
        self.assertEqual(html1, P1)

    def test_單頁伺服器只打一個請求(self):
        # 頁面說只有 8 筆 → 不需要第 2 頁
        small = P2.replace('<span class="colorFF5">28</span>条记录',
                           '<span class="colorFF5">8</span>条记录', 1)
        self.assertEqual(L.record_count(small), 8)
        f = FakeFetch([small])
        cards, meta, _ = L.fetch_pages("prr508", "5", fetch_fn=f)
        self.assertEqual(len(f.calls), 1)
        self.assertEqual(meta["pages"], 1)
        self.assertTrue(meta["match"])

    def test_筆數對不上要警告(self):
        broken = P1.replace('<span class="colorFF5">28</span>条记录',
                            '<span class="colorFF5">31</span>条记录', 1)
        self.assertEqual(L.record_count(broken), 31)
        f = FakeFetch([broken, P2])
        _cards, meta, _ = L.fetch_pages("prr508", "5", fetch_fn=f)
        self.assertFalse(meta["match"])
        self.assertEqual(len(meta["warnings"]), 1)
        self.assertIn("31", meta["warnings"][0])
        self.assertIn("28", meta["warnings"][0])

    def test_跨頁同一筆只算一次(self):
        # 模擬分頁在抓取途中位移：第 2 頁重複出現第 1 頁的第一張卡
        first = L.re.split(r'(?=<div class="goods-list-item)', P1)[1]
        f = FakeFetch([P1, P2 + first])
        _cards, meta, _ = L.fetch_pages("prr508", "5", fetch_fn=f)
        self.assertEqual(meta["unique"], 28)                     # 編號去重
        self.assertEqual(meta["cards_read"], 29)                 # 過濾前真的讀到 29 筆
        self.assertFalse(meta["match"])                          # 29 != N=28 → 警告
        self.assertTrue(meta["warnings"])


class StockTest(unittest.TestCase):
    def test_全頁掛單量等於全部卡片庫存(self):
        f = FakeFetch([P1, P2])
        cards, _meta, _ = L.fetch_pages("prr508", "5", fetch_fn=f)
        valid = [c for c in cards if c["valid"]]
        self.assertGreater(sum(c["kucun"] for c in valid), 3.0e4)   # >3 億天幣
        # 第 1 頁只有約 2.0 億 → 只看第 1 頁會誤判薄市場
        p1 = [c for c in L.parse_cards_detailed(P1) if c["valid"]]
        self.assertLess(sum(c["kucun"] for c in p1), sum(c["kucun"] for c in valid))

    def test_server_rate帶出頁碼資訊(self):
        f = FakeFetch([P1, P2])
        m = L.server_rate("prr508", 4.84, sorts=("5",), fetch_fn=f, all_pages=True)
        self.assertEqual(m["page"]["pages"], 2)
        self.assertTrue(m["page"]["match"])
        self.assertEqual(m["n"], 26)

    def test_all_pages_False維持單頁(self):
        f = FakeFetch([P1, P2])
        L.server_rate("prr508", 4.84, sorts=("5",), fetch_fn=f, all_pages=False)
        self.assertEqual(len(f.calls), 1)


class SourceGuardTest(unittest.TestCase):
    """三支產線都要記得「飛馬全頁 / sort=5」，不然下次改版又會漏。"""

    def read(self, *p):
        return open(os.path.join(os.path.expanduser("~"), *p), encoding="utf-8").read()

    def test_輕掃用sort5且抓全頁(self):
        s = self.read(".hermes", "scripts", "dd373-fm-light.py")
        self.assertIn('sorts=("5",), all_pages=True', s)

    def test_整頁掃描飛馬抓全頁(self):
        s = self.read(".hermes", "dd373-page", "build-page.py")
        self.assertIn("paged = (name == USER_SERVER)", s)
        self.assertIn("all_pages=paged", s)

    def test_全服歷史飛馬抓全頁(self):
        s = self.read(".hermes", "scripts", "dd373-scan-all.py")
        self.assertIn('all_pages=(name == "飛馬珀伽索斯")', s)


if __name__ == "__main__":
    unittest.main(verbosity=2)
