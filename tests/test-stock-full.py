#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全服排行「掛單量」的守門測試。

背景（2026-10-18）：每小時全服掃描只讀第 1 頁（20 筆），各服實際 23~49 筆 →
排行上的掛單量只算到 41~87%、比例每服不同 → 不能互相比。改成：
  - 每天一次 dd373-stock-full.py 掃完所有頁 → data/stock-full.json（22 台同口徑）
  - 排行的掛單量優先用這個檔；飛馬那列用即時值（它本來就抓全頁，15 分鐘更新）
  - 檔案缺／該服抓失敗 → 退回 sweep 的第 1 頁值（不要空白）

用法：python3 tests/test-stock-full.py
"""
import importlib.util
import json
import os
import shutil
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE_DIR = os.path.dirname(HERE)


def load_build_page():
    spec = importlib.util.spec_from_file_location("bp", os.path.join(PAGE_DIR, "build-page.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # 只載入常數／函式（main 在 __main__ 才跑）
    return mod


class StockFullTest(unittest.TestCase):
    def setUp(self):
        self.bp = load_build_page()
        self.tmp = tempfile.mkdtemp()
        self.orig = self.bp.STOCK_FULL
        self.bp.STOCK_FULL = os.path.join(self.tmp, "stock-full.json")

    def tearDown(self):
        self.bp.STOCK_FULL = self.orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, obj):
        json.dump(obj, open(self.bp.STOCK_FULL, "w", encoding="utf-8"), ensure_ascii=False)

    def test_讀得到全頁掛單量(self):
        self.write({"ts": "2026-10-18T11:47:00", "servers": {
            "海神波塞頓": {"stock_wj": 64000, "n": 45, "count": 45, "pages": 3, "ok": True},
            "飛馬珀伽索斯": {"stock_wj": 33600, "n": 29, "count": 31, "pages": 2, "ok": True}}})
        m = self.bp.load_stock_full()
        self.assertEqual(m["海神波塞頓"], 64000)
        self.assertEqual(m["飛馬珀伽索斯"], 33600)

    def test_失敗的服不採用(self):
        self.write({"ts": "x", "servers": {
            "A": {"stock_wj": 100, "ok": False},
            "B": {"stock_wj": 200, "ok": True},
            "C": {"stock_wj": None, "ok": True}}})
        m = self.bp.load_stock_full()
        self.assertEqual(m, {"B": 200})

    def test_缺檔回空字典(self):
        self.assertEqual(self.bp.load_stock_full(), {})

    def test_壞檔不炸(self):
        open(self.bp.STOCK_FULL, "w").write("{not json")
        self.assertEqual(self.bp.load_stock_full(), {})

    def test_排行原始碼有用到全頁檔且飛馬例外(self):
        src = open(os.path.join(PAGE_DIR, "build-page.py"), encoding="utf-8").read()
        self.assertIn("full_stock = load_stock_full()", src)
        self.assertIn('full_stock.get(srv["server"], srv["stock_wj"])', src)
        self.assertIn('srv["stock_wj"] if is_me else', src)   # 飛馬用即時值
        self.assertIn("每日一次全頁掃描", src)                # tooltip 要寫清楚口徑

    def test_每日掃描腳本存在且抓全頁(self):
        p = os.path.expanduser("~/.hermes/scripts/dd373-stock-full.py")
        src = open(p, encoding="utf-8").read()
        self.assertIn("all_pages=True", src)
        self.assertIn("stock-full.json", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
