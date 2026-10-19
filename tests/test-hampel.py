#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hampel() 的門檻規則守門：偏離 > max(3×MAD, 中位數×2%) 才算離群。

跑法：python3 tests/test-hampel.py
（為什麼要有這支：MAD 在行情平穩時趨近 0，只比 3×MAD 會把 0.1% 的雜訊全標成離群；
實測 574 筆標了 15 個、13 個偏離不到 1% → 使用者要求加絕對下限。）

做法：從 build-page.py 原始碼把 hampel() 抽出來 exec（build-page.py 檔名有減號、
import 會連帶載入抓取模組與 sweep 工具，直接 import 不方便也不安全）。
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "build-page.py")


def load_hampel():
    code = open(SRC, encoding="utf-8").read()
    tree = ast.parse(code)
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "hampel")
    ns = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), SRC, "exec"), ns)
    return ns["hampel"]


def main():
    hampel = load_hampel()
    ok = fail = 0

    def check(name, got, want):
        nonlocal ok, fail
        if got == want:
            ok += 1
            print(f"PASS {name}")
        else:
            fail += 1
            print(f"FAIL {name}   got={got!r} want={want!r}")

    # 1) 平穩行情 + 0.2% 雜訊 → 不該標離群（舊規則會標）
    flat = [(i * 900.0, 1000.0 + (0.5 if i % 2 else -0.5)) for i in range(9)]
    flat[4] = (4 * 900.0, 1002.0)
    check("平穩序列的 0.2% 雜訊不算離群", sum(1 for r in hampel(flat) if r[3]), 0)

    # 2) 真的跳 4% → 要標（且主線用中位數）
    spike = [(i * 900.0, 900.0 + i) for i in range(9)]
    spike[4] = (4 * 900.0, 863.0)
    res = hampel(spike)
    check("4% 的跳價要被標成離群", sum(1 for r in res if r[3]), 1)
    check("離群點的主線值＝窗內中位數（[902,903,863,905,906] → 903）", round(res[4][1], 1), 903.0)

    # 3) 邊界：剛好 2%（不超過）不標、2.05% 標
    base = [(i * 900.0, 1000.0) for i in range(9)]
    j2 = list(base); j2[4] = (4 * 900.0, 1020.0)
    check("剛好 2.00% 不算離群（要大於）", sum(1 for r in hampel(j2) if r[3]), 0)
    j3 = list(base); j3[4] = (4 * 900.0, 1020.5)
    check("2.05% 算離群", sum(1 for r in hampel(j3) if r[3]), 1)

    # 4) 門檻取 max：MAD 大時用 3×MAD（不會被 2% 拉低）
    wide = [(0.0, 1000.0), (900.0, 970.0), (1800.0, 1030.0), (2700.0, 968.0),
            (3600.0, 1032.0), (4500.0, 1000.0)]
    check("MAD 主導時門檻仍是 3×MAD（雜訊不標）", sum(1 for r in hampel(wide) if r[3]), 0)

    # 5) 真實案例：10/15 15:24 的 863 vs 中位數 899.2（−4.03%）要標、19 日 965 那類 0.2% 不標
    check("−4.03% 必標", abs(863.0 - 899.2) / 899.2 > 0.02, True)
    check("+0.23% 不標", abs(965.0 - 962.8) / 962.8 > 0.02, False)

    print(f"\n{'✅ 全部通過' if fail == 0 else '❌ 有失敗'}：{ok} passed, {fail} failed")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
