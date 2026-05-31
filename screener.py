"""
选股模块 - Stock Screener
=========================
基于多因子评分系统全市场筛选短线标的。
v2: 并行K线获取 + 智能预筛选 + 量价+资金多维度
"""
import math
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import List, Dict, Optional, Iterator

import pandas as pd
import numpy as np

from config import SCREEN, INDICATORS, STRATEGY
from data_feed import DataFeed


def _score_batch(codes: list, weights: dict, threshold: float) -> list:
    """线程安全：独立DataFeed实例，批量评分"""
    df = DataFeed()
    results = []
    for code in codes:
        try:
            kline = df.get_kline(code, count=60)
            if kline.empty or len(kline) < 20:
                continue
            latest = kline.iloc[-1]
            pre = kline.iloc[-2] if len(kline) > 1 else latest

            vol_ratio = latest.get("volume", 0) / max(latest.get("VOL_MA5", 1), 1)

            vol_score = 0
            if vol_ratio >= INDICATORS["volume_ratio"] and latest["close"] > latest["open"]:
                vol_score = min(100, (vol_ratio / 3) * 100)
                if latest["close"] > latest.get("MA20", 99999) and pre.get("close", 0) <= pre.get("MA20", 0):
                    vol_score = min(100, vol_score + 20)

            ma_score = 0
            if (not pd.isna(latest.get("MA5")) and not pd.isna(latest.get("MA10"))
                    and not pd.isna(pre.get("MA5")) and not pd.isna(pre.get("MA10"))):
                if latest["MA5"] > latest["MA10"] and pre["MA5"] <= pre["MA10"]:
                    ma_score = 90
                elif latest["MA5"] > latest["MA10"]:
                    ma_score = 60
                elif latest["MA5"] > latest["MA10"] and latest["MA10"] > latest.get("MA20", 0):
                    ma_score = 75

            macd_score = 0
            if (not pd.isna(latest.get("MACD_DIF")) and not pd.isna(latest.get("MACD_DEA"))
                    and not pd.isna(pre.get("MACD_DIF")) and not pd.isna(pre.get("MACD_DEA"))):
                if latest["MACD_DIF"] > latest["MACD_DEA"] and pre["MACD_DIF"] <= pre["MACD_DEA"]:
                    macd_score = 90
                elif latest["MACD_DIF"] > latest["MACD_DEA"]:
                    macd_score = 60
                if latest["MACD_DIF"] > 0 and latest["MACD_DEA"] > 0 and macd_score >= 60:
                    macd_score = min(100, macd_score + 10)

            kdj_score = 0
            if (not pd.isna(latest.get("KDJ_K")) and not pd.isna(latest.get("KDJ_D"))
                    and not pd.isna(pre.get("KDJ_K")) and not pd.isna(pre.get("KDJ_D"))):
                if latest["KDJ_K"] > latest["KDJ_D"] and pre["KDJ_K"] <= pre["KDJ_D"]:
                    kdj_score = 85
                elif latest["KDJ_K"] > latest["KDJ_D"]:
                    kdj_score = 55
                if latest["KDJ_K"] < 40 and kdj_score >= 50:
                    kdj_score = min(100, kdj_score + 15)

            vp_score = 0
            if latest["close"] > latest["open"]:
                vp_score += 40
            if latest["close"] > pre["close"]:
                vp_score += 20
            if latest.get("volume", 0) > pre.get("volume", 0):
                vp_score += 20
            if latest["close"] > max(latest.get("MA5", 0), latest.get("MA10", 0)):
                vp_score += 20

            total = (vol_score * weights["volume_breakout"] +
                     ma_score * weights["ma_golden_cross"] +
                     macd_score * weights["macd_signal"] +
                     kdj_score * weights["kdj_signal"] +
                     min(100, vp_score) * weights["volume_price"])

            if total < threshold:
                continue

            rsi = latest.get("RSI")
            rsi_val = round(float(rsi), 1) if not pd.isna(rsi) and rsi is not None else None

            results.append({
                "code": code,
                "score": round(total),
                "vol_breakout": round(vol_score),
                "ma_cross": round(ma_score),
                "macd": round(macd_score),
                "kdj": round(kdj_score),
                "vp": round(min(100, vp_score)),
                "rsi": rsi_val,
                "close": round(float(latest["close"]), 2),
                "vol_ratio": round(vol_ratio, 2),
            })
        except Exception:
            continue
    return results


class StockScreener:
    """A股多因子选股器 v2"""

    def __init__(self, data_feed=None):
        self.df = data_feed if data_feed is not None else DataFeed()
        self._progress = {"done": 0, "total": 0, "state": "idle"}

    @property
    def progress(self) -> dict:
        return dict(self._progress)

    def screen(self, max_workers: int = 6) -> pd.DataFrame:
        """全市场选股主流程（并行版）"""
        stocks = self.df.get_stock_list()
        if stocks.empty:
            return pd.DataFrame()

        filtered = self._smart_filter(stocks)
        if filtered.empty:
            return pd.DataFrame()

        codes = filtered["code"].tolist()
        self._progress = {"done": 0, "total": len(codes), "state": "scoring"}

        batch_size = max(30, len(codes) // (max_workers * 2))
        batches = [codes[i:i + batch_size] for i in range(0, len(codes), batch_size)]

        weights = STRATEGY["strategy_weights"]
        threshold = STRATEGY["score_threshold"]
        all_results = []

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_score_batch, batch, weights, threshold): i
                for i, batch in enumerate(batches)
            }
            for future in as_completed(futures):
                batch_results = future.result()
                all_results.extend(batch_results)
                self._progress["done"] += len(batches[futures[future]])

        self._progress["state"] = "merging"

        if not all_results:
            self._progress["state"] = "done"
            return pd.DataFrame()

        scored_df = pd.DataFrame(all_results).sort_values("score", ascending=False)
        top_codes = scored_df.head(STRATEGY["max_stocks"])["code"].tolist()

        result = filtered[filtered["code"].isin(top_codes)].copy()
        score_map = {r["code"]: r for r in all_results}
        for col in ["score", "vol_breakout", "ma_cross", "macd", "kdj", "vp", "rsi", "vol_ratio"]:
            result[col] = result["code"].map(lambda c: score_map.get(c, {}).get(col, 0))

        result = result.rename(columns={
            "score": "综合评分", "vol_breakout": "放量突破",
            "ma_cross": "均线金叉", "macd": "MACD", "kdj": "KDJ",
            "vp": "量价配合", "rsi": "RSI", "vol_ratio": "量比",
        })

        cols = ["code", "name", "price", "change_pct", "turnover_rate",
                "market_cap", "board", "pe", "volume_ratio", "main_net",
                "综合评分", "放量突破", "均线金叉", "MACD", "KDJ", "量价配合", "RSI", "量比"]

        result = result[[c for c in cols if c in result.columns]]
        self._progress["state"] = "done"
        return result

    def _smart_filter(self, stocks: pd.DataFrame) -> pd.DataFrame:
        """智能预筛选：基础条件 + 活跃度 + 主力资金"""
        df = stocks.copy()

        conditions = (
            (df["price"] >= SCREEN["price_min"]) &
            (df["price"] <= SCREEN["price_max"]) &
            (df["market_cap"] >= SCREEN["market_cap_min"]) &
            (df["market_cap"] <= SCREEN["market_cap_max"]) &
            (df["amount"] >= SCREEN["avg_amount_min"] * 1e8) &
            (df["turnover_rate"] >= SCREEN["turnover_min"]) &
            (df["turnover_rate"] <= SCREEN["turnover_max"])
        )

        if SCREEN["exclude_st"]:
            conditions &= ~df["is_st"]
        if SCREEN["exclude_kcb"]:
            conditions &= (df["board"] != "科创板")
        if SCREEN["exclude_bj"]:
            conditions &= (df["board"] != "北交所")

        filtered = df[conditions].copy()

        if ("volume_ratio" in filtered.columns
                and filtered["volume_ratio"].notna().any()):
            filtered = filtered[filtered["volume_ratio"] >= 0.5]

        if len(filtered) > 500:
            filtered = filtered.nlargest(500, "amount")

        return filtered.copy()

    def screen_with_detail(self, code: str) -> Optional[dict]:
        """单股详细评分"""
        stocks = self.df.get_stock_list()
        match = stocks[stocks["code"] == code]
        if match.empty:
            return None

        stock = match.iloc[0]
        kline = self.df.get_kline(code, count=60)
        if kline.empty:
            return None

        latest = kline.iloc[-1]
        pre = kline.iloc[-2] if len(kline) > 1 else latest

        vol_ratio = latest.get("volume", 0) / max(latest.get("VOL_MA5", 1), 1)

        signals = []
        if vol_ratio >= 1.5:
            signals.append(f"量比{vol_ratio:.1f}，显著放量")
        if latest["close"] > latest["open"]:
            signals.append("收阳线")
        if not pd.isna(latest.get("MA5")) and latest["close"] > latest["MA5"]:
            signals.append("站上MA5")
        if (not pd.isna(latest.get("MACD_DIF")) and not pd.isna(latest.get("MACD_DEA"))
                and latest["MACD_DIF"] > latest["MACD_DEA"]):
            signals.append("MACD多头")
        if (not pd.isna(latest.get("KDJ_K")) and not pd.isna(latest.get("KDJ_D"))
                and latest["KDJ_K"] > latest["KDJ_D"]):
            signals.append("KDJ金叉")
        if (not pd.isna(latest.get("MA5")) and not pd.isna(latest.get("MA10"))
                and not pd.isna(pre.get("MA5")) and not pd.isna(pre.get("MA10"))
                and latest["MA5"] > latest["MA10"] and pre["MA5"] <= pre["MA10"]):
            signals.append("MA5上穿MA10 ✅")

        return {
            "code": code,
            "name": stock.get("name", ""),
            "price": stock.get("price", 0),
            "change_pct": stock.get("change_pct", 0),
            "board": stock.get("board", ""),
            "signals": signals,
            "close": round(float(latest["close"]), 2),
            "MA5": round(float(latest.get("MA5", 0)), 2) if not pd.isna(latest.get("MA5")) else None,
            "MA10": round(float(latest.get("MA10", 0)), 2) if not pd.isna(latest.get("MA10")) else None,
            "MA20": round(float(latest.get("MA20", 0)), 2) if not pd.isna(latest.get("MA20")) else None,
            "MACD": round(float(latest.get("MACD_DIF", 0)), 3) if not pd.isna(latest.get("MACD_DIF")) else None,
            "vol_ratio": round(vol_ratio, 2),
        }
