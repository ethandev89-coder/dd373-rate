# -*- coding: utf-8 -*-
"""離群點門檻（預覽版專用；驗收通過後併回 build-page.py 的 hampel()）。

問題：原本只比 `3×MAD`。MAD 在「行情很穩」的時段會趨近 0（例：連續幾筆只差 0.5），
於是偏離 1~2 個天幣（0.1~0.2%）也會被標成離群 → 574 筆裡 15 個離群、13 個偏離不到 1%。

新規則：偏離中位數 > max(3×MAD, 中位數×2%) 才算離群。
    只有真的跳超過 2% 的點才會被標記（例：10/15 15:24 的 863，−4%）。
原始序列與 7 天／30 天聚合都走同一條規則（build-page.py 的兩個呼叫點都換成這支）。
"""

import statistics as st


def hampel(points, k=2, n_sig=3.0, min_pct=0.02):
    """滾動中位數（5 筆 Hampel filter）＋最小門檻。

    回傳 [(t, 平滑後值, 原始值, 是否離群)]；主線用平滑值，原始值留給 tooltip。
    MAD 依慣例乘 1.4826 換算成標準差等效值；門檻再加一道「中位數×2%」的絕對下限，
    避免行情平穩（MAD≈0）時把 1~2 個天幣的雜訊誤判成離群。
    """
    vals = [v for _t, v in points]
    out = []
    for i, (t, v) in enumerate(points):
        win = vals[max(0, i - k): i + k + 1]
        m = st.median(win)
        mad = st.median([abs(x - m) for x in win]) * 1.4826
        floor = abs(m) * min_pct                      # 最小門檻：中位數的 2%
        threshold = max(n_sig * mad, floor)
        is_out = bool(threshold > 0 and abs(v - m) > threshold)
        out.append((t, m if is_out else v, v, is_out))
    return out
