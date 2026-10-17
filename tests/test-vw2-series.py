#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""② 長期序列（data/vw2-series.json）的守門測試。

規則（2026-10-18 使用者定案）：
  - `ts` 和 `v`（② 幣值）是主要資料 → **每筆都一定要有**，不會因為缺 `s` 就跳過整筆。
  - `s`（市場總掛單量）可以是 `null`：缺就存 null（不沿用上一筆），tooltip 那一點不顯示掛單量，
    但**寫入時一定要在 log 留警告**。
  - 連續 4 次（輕掃約 1 小時）沒有 `s` → 告警一次（可能掛單解析壞了）。

這支測試確認：
  1. 寫入永遠帶 ts/v；有 stock 就一起存 s
  2. 沒有 stock → 照樣寫入、s 存 null、而且 stderr 有警告（不會靜默）
  3. 不會沿用上一筆的 s（沿用＝tooltip 顯示錯時點的數字）
  4. 同一分鐘只留一筆、上限裁切、缺值計數、連續缺值判斷
  5. 輕掃那條寫入路徑（dd373-fm-light.py 的 record）端到端：有 s、缺 s 存 null、第 4 次才告警一次
  6. 真正的資料檔每筆都有 ts/v（s 可為 null），且兩條產線沒有「自己組 row」的漏寫路徑

用法：
  python3 tests/test-vw2-series.py              # 全部
  python3 tests/test-vw2-series.py --data-only   # 只驗真正的資料檔（publish 前用，快）
"""
import contextlib
import io
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


def _append(path, ts, v, s):
    """寫一筆，回傳 (row, stderr 文字)。"""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        row = dd373_series.append_series(path, ts, v, s)
    return row, err.getvalue()


class SeriesWriteTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="vw2ser-")
        self.path = os.path.join(self.dir, "vw2-series.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _rows(self):
        return json.load(open(self.path, encoding="utf-8"))

    def test_writes_ts_v_and_stock(self):
        row, err = _append(self.path, "2026-10-18T03:22:50", 946.4, 16603)
        self.assertEqual(row["s"], 16603)
        self.assertEqual(row["v"], 946.4)
        self.assertEqual(row["ts"], "2026-10-18T03:22:50")
        self.assertEqual(err, "", "有掛單量時不該有警告")

    def test_missing_stock_writes_null_and_warns(self):
        _append(self.path, "2026-10-18T03:00:00", 945.0, 16500)
        row, err = _append(self.path, "2026-10-18T03:15:00", 946.0, None)
        rows = self._rows()
        self.assertEqual(len(rows), 2, "v 是主要資料：不能因為缺 s 就跳過整筆")
        self.assertEqual(rows[-1]["v"], 946.0)
        self.assertIsNone(rows[-1]["s"], "缺掛單量要存 null，不是沿用上一筆")
        self.assertEqual(rows[0]["s"], 16500, "前一筆不能被改")
        self.assertIn("沒有掛單量", err, "缺 s 一定要在 log 留警告")

    def test_missing_stock_on_empty_file_still_writes(self):
        row, err = _append(self.path, "2026-10-18T03:00:00", 945.0, None)
        self.assertEqual(len(self._rows()), 1)
        self.assertIsNone(self._rows()[0]["s"])
        self.assertIn("沒有掛單量", err)

    def test_no_value_writes_nothing(self):
        row, err = _append(self.path, "2026-10-18T03:00:00", None, 16500)
        self.assertIsNone(row)
        self.assertFalse(os.path.exists(self.path), "沒有 v 就不該寫（整筆沒有主要資料）")

    def test_same_minute_is_deduped(self):
        _append(self.path, "2026-10-18T03:22:50", 946.4, 16600)
        _append(self.path, "2026-10-18T03:22:07", 999.9, 12345)   # 同分鐘
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["v"], 946.4)

    def test_cap(self):
        for i in range(8):
            dd373_series.append_series(self.path, f"2026-10-18T03:{i:02d}:00", 940 + i, 16000 + i, cap=5)
        self.assertEqual(len(self._rows()), 5)

    def test_missing_stock_counts_nulls(self):
        _append(self.path, "2026-10-18T03:00:00", 945.0, 16500)
        self.assertEqual(dd373_series.missing_stock(self.path), 0)
        _append(self.path, "2026-10-18T03:15:00", 946.0, None)
        self.assertEqual(dd373_series.missing_stock(self.path), 1)

    def test_missing_streak_and_threshold(self):
        for i in range(3):
            _append(self.path, f"2026-10-18T03:{i:02d}:00", 940 + i, None)
        self.assertEqual(dd373_series.missing_streak(self.path), 3)
        self.assertFalse(dd373_series.trailing_missing_stock(self.path, 4))
        _append(self.path, "2026-10-18T03:03:00", 945, None)
        self.assertEqual(dd373_series.missing_streak(self.path), 4)
        self.assertTrue(dd373_series.trailing_missing_stock(self.path, 4))
        _append(self.path, "2026-10-18T03:04:00", 946, 16500)   # 恢復
        self.assertEqual(dd373_series.missing_streak(self.path), 0)
        self.assertFalse(dd373_series.trailing_missing_stock(self.path, 4))


class LightScanPathTest(unittest.TestCase):
    """15 分鐘輕掃那條寫入路徑（dd373-fm-light.py 的 record）。"""

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

    def _record(self, ts, v, stock):
        cur = {"ts": ts, "vw2_gold_per_twd": v}
        if stock is not None:
            cur["stock_wj"] = stock
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.mod.record(cur)
        return out.getvalue(), err.getvalue()

    def test_record_writes_stock(self):
        self._record("2026-10-18T03:22:50", 946.4, 16603)
        rows = self._series()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["s"], 16603)
        light = json.load(open(self.mod.LIGHT, encoding="utf-8"))
        self.assertEqual(light["entries"][-1]["stock_wj"], 16603)

    def test_record_without_stock_writes_null(self):
        self._record("2026-10-18T03:22:50", 946.4, 16000)
        out, err = self._record("2026-10-18T03:37:50", 947.0, None)
        rows = self._series()
        self.assertEqual(len(rows), 2, "缺掛單量也要寫入價格")
        self.assertIsNone(rows[-1]["s"])
        self.assertEqual(rows[-1]["v"], 947.0)
        self.assertIn("沒有掛單量", err)
        self.assertEqual(out, "", "還沒連續 4 次，不該告警")

    def test_alert_fires_once_at_fourth_miss(self):
        outs = [self._record(f"2026-10-18T03:{15 * i:02d}:00", 946.0, None)[0] for i in range(4)]
        self.assertEqual(outs[:3], ["", "", ""], "第 1~3 次不告警")
        self.assertIn("連續 4 次", outs[3], "第 4 次要告警")
        out5 = self._record("2026-10-18T04:15:00", 946.0, None)[0]
        self.assertEqual(out5, "", "第 5 次之後不要重複吵")

    def test_no_alert_when_stock_recovers_before_four(self):
        for i in range(3):
            self.assertEqual(self._record(f"2026-10-18T03:{15 * i:02d}:00", 946.0, None)[0], "")
        out = self._record("2026-10-18T03:45:00", 946.0, 16500)[0]
        self.assertEqual(out, "", "恢復了就完全靜默")


class RealDataTest(unittest.TestCase):
    def test_every_row_has_ts_and_v(self):
        if not os.path.exists(SERIES):
            self.skipTest("沒有 data/vw2-series.json")
        rows = json.load(open(SERIES, encoding="utf-8"))
        self.assertTrue(rows, "序列不該是空的")
        bad = [e.get("ts") for e in rows if not e.get("ts") or e.get("v") is None]
        self.assertEqual(bad, [], f"{len(bad)} 筆缺 ts/v（前 5 筆：{bad[:5]}）")

    def test_rows_are_time_ordered(self):
        if not os.path.exists(SERIES):
            self.skipTest("沒有 data/vw2-series.json")
        ts = [e["ts"] for e in json.load(open(SERIES, encoding="utf-8"))]
        self.assertEqual(ts, sorted(ts), "序列必須依時間排序")

    def test_null_stock_is_reported(self):
        """s 可以 null，但要能被數出來（人看 log／統計用）；不算失敗。"""
        if not os.path.exists(SERIES):
            self.skipTest("沒有 data/vw2-series.json")
        rows = json.load(open(SERIES, encoding="utf-8"))
        print(f"    （統計）{len(rows)} 筆、s 為 null {dd373_series.missing_stock(SERIES)} 筆、"
              f"連續缺值 {dd373_series.missing_streak(SERIES)} 筆")


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
