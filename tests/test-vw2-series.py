#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""② 長期序列（data/vw2-series.json）的守門測試。

為什麼要有：這檔案有兩條寫入路徑（每小時整頁掃描 build-page.py、15 分鐘輕掃
dd373-fm-light.py）。輕掃那條以前自己寫 `{"ts","v"}`、漏了掛單量 s，造成 59 筆
沒有 s（tooltip 的「掛單」那行會缺）。現在兩條都走 ~/.hermes/scripts/dd373_series.py
的 append_series，這支測試確認：
  1. 走共用寫入點時，每筆都一定會帶 s
  2. 呼叫端沒給 stock 時：沿用最近一筆 s；連可沿用的都沒有就不寫（不留沒有 s 的資料）
  3. 同一分鐘只留一筆、上限裁切正常
  4. 真正的資料檔每一筆都有 ts / v / s
  5. 兩條產線的原始碼沒有「自己組 row」的漏寫路徑（回歸守門）

用法：
  python3 tests/test-vw2-series.py              # 全部
  python3 tests/test-vw2-series.py --data-only   # 只驗真正的資料檔（publish 前用，快）
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

HOME = os.path.expanduser("~")
SCRIPTS = os.path.join(HOME, ".hermes", "scripts")
PAGE = os.path.join(HOME, ".hermes", "dd373-page")
SERIES = os.path.join(PAGE, "data", "vw2-series.json")
sys.path.insert(0, SCRIPTS)

import dd373_series                                   # noqa: E402


class SeriesWriteTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="vw2ser-")
        self.path = os.path.join(self.dir, "vw2-series.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _rows(self):
        return json.load(open(self.path, encoding="utf-8"))

    def test_writes_stock_on_every_row(self):
        dd373_series.append_series(self.path, "2026-10-18T03:22:50", 946.4, 16603)
        row = self._rows()[-1]
        self.assertEqual(row["s"], 16603)
        self.assertEqual(row["v"], 946.4)
        self.assertEqual(row["ts"], "2026-10-18T03:22:50")

    def test_missing_stock_inherits_previous(self):
        dd373_series.append_series(self.path, "2026-10-18T03:00:00", 945.0, 16500)
        dd373_series.append_series(self.path, "2026-10-18T03:15:00", 946.0, None)
        rows = self._rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["s"], 16500)          # 沿用上一筆，而不是留空
        self.assertEqual(rows[-1]["v"], 946.0)

    def test_no_stock_anywhere_writes_nothing(self):
        self.assertIsNone(dd373_series.append_series(self.path, "2026-10-18T03:00:00", 945.0, None))
        self.assertFalse(os.path.exists(self.path), "不該建立沒有 s 的資料")

    def test_same_minute_is_deduped(self):
        dd373_series.append_series(self.path, "2026-10-18T03:22:50", 946.4, 16600)
        dd373_series.append_series(self.path, "2026-10-18T03:22:07", 999.9, 12345)   # 同分鐘
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["v"], 946.4)

    def test_cap(self):
        for i in range(8):
            dd373_series.append_series(self.path, f"2026-10-18T03:{i:02d}:00", 940 + i, 16000 + i, cap=5)
        self.assertEqual(len(self._rows()), 5)

    def test_missing_stock_counts(self):
        dd373_series.append_series(self.path, "2026-10-18T03:00:00", 945.0, 16500)
        self.assertEqual(dd373_series.missing_stock(self.path), 0)
        rows = self._rows()
        rows.append({"ts": "2026-10-18T03:15:00", "v": 946.0})       # 模擬舊版漏寫
        json.dump(rows, open(self.path, "w", encoding="utf-8"), ensure_ascii=False)
        self.assertEqual(dd373_series.missing_stock(self.path), 1)


class LightScanPathTest(unittest.TestCase):
    """15 分鐘輕掃那條寫入路徑（dd373-fm-light.py 的 record）：實際造成的漏寫來源。"""

    def setUp(self):
        import importlib.util
        self.dir = tempfile.mkdtemp(prefix="vw2light-")
        spec = importlib.util.spec_from_file_location("dd373_fm_light", os.path.join(SCRIPTS, "dd373-fm-light.py"))
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)              # 只載入函式，不會打 DD373（__main__ 才有 main）
        self._data, self._light = self.mod.DATA, self.mod.LIGHT
        self.mod.DATA = self.dir
        self.mod.LIGHT = os.path.join(self.dir, "fm-light.json")

    def tearDown(self):
        self.mod.DATA, self.mod.LIGHT = self._data, self._light
        shutil.rmtree(self.dir, ignore_errors=True)

    def _series(self):
        return json.load(open(os.path.join(self.dir, "vw2-series.json"), encoding="utf-8"))

    def test_record_writes_stock(self):
        self.mod.record({"ts": "2026-10-18T03:22:50", "vw2_gold_per_twd": 946.4, "stock_wj": 16603})
        rows = self._series()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["s"], 16603, "輕掃寫入必須帶掛單量 s")
        light = json.load(open(self.mod.LIGHT, encoding="utf-8"))
        self.assertEqual(light["entries"][-1]["stock_wj"], 16603)

    def test_record_without_stock_still_has_s(self):
        self.mod.record({"ts": "2026-10-18T03:22:50", "vw2_gold_per_twd": 946.4, "stock_wj": 16000})
        self.mod.record({"ts": "2026-10-18T03:37:50", "vw2_gold_per_twd": 947.0})   # 這筆沒掛單量
        rows = self._series()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["s"], 16000, "沿用上一筆，不可以留空")


class RealDataTest(unittest.TestCase):
    def test_every_row_has_ts_v_stock(self):
        if not os.path.exists(SERIES):
            self.skipTest("沒有 data/vw2-series.json")
        rows = json.load(open(SERIES, encoding="utf-8"))
        self.assertTrue(rows, "序列不該是空的")
        bad = [e.get("ts") for e in rows if not e.get("ts") or not e.get("v") or not e.get("s")]
        self.assertEqual(bad, [], f"{len(bad)} 筆缺 ts/v/s（前 5 筆：{bad[:5]}）")

    def test_rows_are_time_ordered(self):
        if not os.path.exists(SERIES):
            self.skipTest("沒有 data/vw2-series.json")
        ts = [e["ts"] for e in json.load(open(SERIES, encoding="utf-8"))]
        self.assertEqual(ts, sorted(ts), "序列必須依時間排序")


class SourceGuardTest(unittest.TestCase):
    """確保兩條產線都沒有「自己組 row」的漏寫路徑。"""

    def test_light_scan_uses_shared_writer(self):
        src = open(os.path.join(SCRIPTS, "dd373-fm-light.py"), encoding="utf-8").read()
        self.assertIn("append_series(", src, "輕掃必須呼叫共用寫入點")
        self.assertIn("dd373_series", src)
        self.assertNotIn('ser.append({"ts": cur["ts"], "v": cur["vw2_gold_per_twd"]})', src,
                         "舊的漏寫路徑（只寫 ts/v）不可以回來")

    def test_build_page_delegates(self):
        src = open(os.path.join(PAGE, "build-page.py"), encoding="utf-8").read()
        self.assertIn("_series_append(", src, "整頁掃描必須走共用寫入點")
        self.assertNotIn('row = {"ts": ts_iso, "v": value}', src, "舊的漏寫路徑不可以回來")


def main():
    data_only = "--data-only" in sys.argv
    loader = unittest.TestLoader()
    if data_only:
        suite = loader.loadTestsFromTestCase(RealDataTest)
    else:
        suite = loader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
