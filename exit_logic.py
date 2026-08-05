# -*- coding: utf-8 -*-
"""
exit_logic.py — 通用出場邏輯引擎
═══════════════════════════════════════════════════════════
與「哪個策略選股」完全解耦，只負責：
    給定進場日 + 進場價 + 之後的價格序列 → 算出出場日/出場價/出場原因

三個出場條件（任一先觸發就出場）：
    1) 順勢停損：收盤跌破 MA10 或 MA20
    2) 支撐位跌破：跌破「進場日前 N 日」的最低點
    3) 常數% 停損：從進場價回檔超過 X%

可同時套用在多個策略的「進場清單」上，各自獨立跑一輪，
再用 compare_strategies() 把結果放在一起比較勝率 / 期望值 / 平均持有天數。
"""

from __future__ import annotations
import pandas as pd
import numpy as np


# ────────────────────────────────────────────────────────────
# 1. 單筆交易的出場模擬
# ────────────────────────────────────────────────────────────
def simulate_exit(
    entry_date,
    entry_price: float,
    close: pd.Series,
    low: pd.Series | None = None,
    ma_type: str = "MA20",          # "MA10" or "MA20"
    support_lookback: int = 5,      # 進場日前 N 日低點
    stop_pct: float = 0.08,         # 常數% 停損
    max_hold_days: int | None = 120,
    intraday_stop: bool = True,     # True: %停損用「最低價」判斷(較貼近真實停損單)；False: 用收盤價
) -> dict:
    """
    close / low: 以日期為 index 的 pd.Series，需涵蓋「進場日前 support_lookback 天」
                 一直到「你想回測到的最後一天」，且已依日期排序。
    回傳: dict(entry_date, entry_price, exit_date, exit_price, exit_reason,
               ret_pct, hold_days, hit_MA, hit_support, hit_stop)
    """
    close = close.sort_index()
    entry_date = pd.Timestamp(entry_date)

    if entry_date not in close.index:
        # 用進場日之後最近一個交易日對齊（例如當天停牌）
        after = close.index[close.index >= entry_date]
        if len(after) == 0:
            return _fail_result(entry_date, entry_price, "找不到進場日之後的資料")
        entry_date = after[0]

    ma_window = 10 if ma_type.upper() == "MA10" else 20
    ma_series = close.rolling(ma_window).mean()

    # 支撐位：進場日「前」N 日（不含進場當天）的最低點
    pre_entry = close.index[close.index < entry_date]
    if low is not None:
        low = low.sort_index()
        support_src = low.reindex(close.index)
    else:
        support_src = close
    if len(pre_entry) >= 1:
        window_idx = pre_entry[-support_lookback:]
        support_price = float(support_src.loc[window_idx].min())
    else:
        support_price = float("nan")

    stop_price = entry_price * (1 - stop_pct)

    future_dates = close.index[close.index > entry_date]
    if max_hold_days is not None:
        future_dates = future_dates[:max_hold_days]

    for d in future_dates:
        c = float(close.loc[d])
        l = float(low.loc[d]) if (low is not None and d in low.index and not pd.isna(low.loc[d])) else c

        hit_ma      = c < float(ma_series.loc[d]) if not pd.isna(ma_series.loc[d]) else False
        hit_support = (not pd.isna(support_price)) and (c < support_price)
        hit_stop    = (l <= stop_price) if intraday_stop else (c <= stop_price)

        if hit_ma or hit_support or hit_stop:
            reasons = []
            if hit_stop:    reasons.append(f"常數%停損(-{stop_pct*100:.0f}%)")
            if hit_support: reasons.append(f"跌破{support_lookback}日低點支撐")
            if hit_ma:      reasons.append(f"收盤跌破{ma_type}")

            # 出場價：%停損優先用停損價成交（較貼近真實停損單），否則用當日收盤
            exit_price = stop_price if (hit_stop and intraday_stop and l <= stop_price) else c
            ret_pct = (exit_price / entry_price - 1) * 100
            hold_days = int((d - entry_date).days)

            return {
                "entry_date": entry_date.strftime("%Y-%m-%d"),
                "entry_price": round(entry_price, 2),
                "exit_date": d.strftime("%Y-%m-%d"),
                "exit_price": round(exit_price, 2),
                "exit_reason": " + ".join(reasons),
                "ret_pct": round(ret_pct, 2),
                "hold_days": hold_days,
                "hit_MA": hit_ma,
                "hit_support": hit_support,
                "hit_stop": hit_stop,
                "support_price": round(support_price, 2) if not pd.isna(support_price) else None,
                "stop_price": round(stop_price, 2),
                "status": "exited",
            }

    # 期間內都沒觸發任何出場條件 → 仍持有 / 資料用完
    if len(future_dates) == 0:
        return _fail_result(entry_date, entry_price, "無後續資料")

    last_d = future_dates[-1]
    last_c = float(close.loc[last_d])
    ret_pct = (last_c / entry_price - 1) * 100
    return {
        "entry_date": entry_date.strftime("%Y-%m-%d"),
        "entry_price": round(entry_price, 2),
        "exit_date": last_d.strftime("%Y-%m-%d"),
        "exit_price": round(last_c, 2),
        "exit_reason": "未觸發出場（資料結束/仍持有）",
        "ret_pct": round(ret_pct, 2),
        "hold_days": int((last_d - entry_date).days),
        "hit_MA": False, "hit_support": False, "hit_stop": False,
        "support_price": round(support_price, 2) if not pd.isna(support_price) else None,
        "stop_price": round(stop_price, 2),
        "status": "still_holding",
    }


def _fail_result(entry_date, entry_price, msg):
    return {
        "entry_date": pd.Timestamp(entry_date).strftime("%Y-%m-%d"),
        "entry_price": entry_price, "exit_date": None, "exit_price": None,
        "exit_reason": msg, "ret_pct": None, "hold_days": None,
        "hit_MA": False, "hit_support": False, "hit_stop": False,
        "support_price": None, "stop_price": None, "status": "error",
    }


# ────────────────────────────────────────────────────────────
# 2. 批次回測：一份「進場清單」→ 一批交易結果
# ────────────────────────────────────────────────────────────
def run_backtest(
    entries: pd.DataFrame,          # columns: date, code, [entry_price], [strategy]
    price_data: dict,               # {code: {"close": pd.Series, "low": pd.Series}}
    ma_type: str = "MA20",
    support_lookback: int = 5,
    stop_pct: float = 0.08,
    max_hold_days: int | None = 120,
    intraday_stop: bool = True,
) -> pd.DataFrame:
    """
    entries 每一列是一筆「進場訊號」。若沒有 entry_price 欄位，
    會自動用該檔股票在 date 當天的收盤價作為進場價。
    回傳每一筆交易的出場結果 DataFrame。
    """
    rows = []
    for _, r in entries.iterrows():
        code = str(r["code"]).zfill(4) if str(r["code"]).isdigit() else str(r["code"])
        d = price_data.get(code)
        if d is None or "close" not in d:
            rows.append({**r.to_dict(), "exit_reason": "無價格資料", "status": "error"})
            continue

        close = d["close"]
        low = d.get("low")
        entry_date = pd.Timestamp(r["date"])

        if "entry_price" in r and not pd.isna(r["entry_price"]):
            entry_price = float(r["entry_price"])
        else:
            aligned = close.index[close.index >= entry_date]
            if len(aligned) == 0:
                rows.append({**r.to_dict(), "exit_reason": "無進場日資料", "status": "error"})
                continue
            entry_price = float(close.loc[aligned[0]])

        res = simulate_exit(
            entry_date, entry_price, close, low,
            ma_type=ma_type, support_lookback=support_lookback,
            stop_pct=stop_pct, max_hold_days=max_hold_days,
            intraday_stop=intraday_stop,
        )
        res["code"] = code
        res["strategy"] = r.get("strategy", "未分類")
        rows.append(res)

    return pd.DataFrame(rows)


# ────────────────────────────────────────────────────────────
# 3. 多策略比較
# ────────────────────────────────────────────────────────────
def summarize(results: pd.DataFrame, group_col: str = "strategy") -> pd.DataFrame:
    """輸出每個策略的勝率 / 平均報酬 / 期望值 / 平均持有天數 / 出場原因分布"""
    ok = results[results["status"] != "error"].copy()
    ok["win"] = ok["ret_pct"] > 0

    def _agg(g):
        n = len(g)
        win_n = int(g["win"].sum())
        reason_dist = g["exit_reason"].value_counts(normalize=True).round(3).to_dict()
        return pd.Series({
            "交易數": n,
            "勝率%": round(win_n / n * 100, 1) if n else None,
            "平均報酬%": round(g["ret_pct"].mean(), 2) if n else None,
            "中位數報酬%": round(g["ret_pct"].median(), 2) if n else None,
            "最大獲利%": round(g["ret_pct"].max(), 2) if n else None,
            "最大虧損%": round(g["ret_pct"].min(), 2) if n else None,
            "平均持有天數": round(g["hold_days"].mean(), 1) if n else None,
            "出場原因分布": reason_dist,
        })

    return ok.groupby(group_col).apply(_agg).reset_index()
