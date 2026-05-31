"""
持仓管理模块 - PortfolioManager
================================
管理用户持仓数据：录入、存储、实时行情补充、盈亏计算。
持仓数据持久化在 portfolio_positions.json。
"""

import json
import os
from datetime import datetime
from typing import Optional

import pandas as pd

from config import RISK
from data_feed import DataFeed


POSITIONS_FILE = os.path.join(os.path.dirname(__file__), "portfolio_positions.json")


def _load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"capital": 100000, "positions": []}


def _save_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _safe_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _generate_id(positions: list) -> str:
    existing = {p.get("id") for p in positions if p.get("id")}
    n = 1
    while f"p{n}" in existing:
        n += 1
    return f"p{n}"


class PortfolioManager:
    """持仓数据管理器"""

    def __init__(self, data_feed: Optional[DataFeed] = None):
        self.df = data_feed if data_feed is not None else DataFeed()

    # ──────────── 存储 ────────────

    def load(self) -> dict:
        """从 JSON 加载持仓数据。"""
        return _load_json(POSITIONS_FILE)

    def save(self, data: dict) -> dict:
        """保存持仓数据到 JSON。"""
        capital = _safe_float(data.get("capital"), 100000)
        positions = []
        for item in data.get("positions", []):
            code = str(item.get("code", "")).strip().zfill(6)
            quantity = _safe_float(item.get("quantity"))
            cost_price = _safe_float(item.get("cost_price"))
            if not code.isdigit() or len(code) != 6 or quantity <= 0 or cost_price <= 0:
                continue
            positions.append({
                "id": item.get("id") or _generate_id(positions),
                "code": code,
                "name": str(item.get("name", "")).strip(),
                "quantity": quantity,
                "cost_price": round(cost_price, 3),
                "added_at": item.get("added_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "notes": str(item.get("notes", "")).strip(),
            })
        payload = {"capital": capital, "positions": positions}
        _save_json(POSITIONS_FILE, payload)
        return {"success": True, "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    # ──────────── 实时行情补充 ────────────

    def enrich(self, data: dict) -> dict:
        """补充每只持仓的实时行情和盈亏计算。"""
        capital = _safe_float(data.get("capital"), 100000)
        positions = list(data.get("positions", []))
        if not positions:
            return {"capital": capital, "positions": [], "summary": self._empty_summary()}

        codes = [p.get("code", "").strip() for p in positions]
        quotes_df = self.df.get_realtime_quotes(codes)
        quotes = {}
        if not quotes_df.empty:
            for _, row in quotes_df.iterrows():
                code = str(row.get("code", "")).zfill(6)
                quotes[code] = row

        enriched = []
        total_market_value = 0.0
        total_cost = 0.0

        for pos in positions:
            code = str(pos.get("code", "")).strip().zfill(6)
            quantity = _safe_float(pos.get("quantity"))
            cost_price = _safe_float(pos.get("cost_price"))
            q = quotes.get(code, {})

            current_price = _safe_float(q.get("price"))
            if current_price <= 0:
                current_price = cost_price
            change_pct = _safe_float(q.get("change_pct"))
            name = str(pos.get("name", "") or q.get("name", ""))
            market_value = current_price * quantity
            cost_total = cost_price * quantity
            pnl = market_value - cost_total
            pnl_pct = (pnl / cost_total * 100) if cost_total > 0 else 0.0
            weight = 0.0

            total_market_value += market_value
            total_cost += cost_total

            diagnostics = self._position_diagnostics(code, cost_price, current_price, pnl_pct)

            enriched.append({
                "id": pos.get("id", _generate_id(positions)),
                "code": code,
                "name": name,
                "quantity": quantity,
                "cost_price": round(cost_price, 2),
                "current_price": round(current_price, 2),
                "change_pct": round(change_pct, 2),
                "market_value": round(market_value, 2),
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 2),
                "weight": weight,
                "added_at": pos.get("added_at", ""),
                "notes": pos.get("notes", ""),
                **diagnostics,
            })

        summary = self._calc_summary(capital, total_market_value, total_cost, enriched)
        for item in enriched:
            item["weight"] = round(item["market_value"] / summary["total_asset"] * 100, 1) if summary["total_asset"] else 0
        return {"capital": capital, "positions": enriched, "summary": summary}

    def _position_diagnostics(self, code: str, cost_price: float,
                              current_price: float, pnl_pct: float) -> dict:
        """补充止盈止损参考、技术摘要和资金流。"""
        stop_loss_price = round(cost_price * (1 + float(RISK["stop_loss"])), 2)
        take_profit_price = round(cost_price * (1 + float(RISK["take_profit"])), 2)
        protect_profit_price = None
        if pnl_pct >= abs(float(RISK["take_profit"])) * 100 and current_price > 0:
            protect_profit_price = round(max(cost_price, current_price * 0.97), 2)

        rule_action = "持有观察"
        if current_price > 0 and current_price <= stop_loss_price:
            rule_action = "触及规则止损线"
        elif current_price > 0 and current_price >= take_profit_price:
            rule_action = "触及规则止盈线"
        elif pnl_pct <= float(RISK["stop_loss"]) * 100:
            rule_action = "亏损接近止损阈值"

        tech = self._technical_snapshot(code)
        fund = self.df.get_fund_flow(code)
        return {
            "stop_loss_price": stop_loss_price,
            "take_profit_price": take_profit_price,
            "protect_profit_price": protect_profit_price,
            "rule_action": rule_action,
            "technical": tech,
            "fund_flow": fund,
        }

    def _technical_snapshot(self, code: str) -> dict:
        kline = self.df.get_kline(code, count=80)
        if kline.empty:
            return {"available": False, "summary": "K线不可用"}
        latest = kline.iloc[-1]
        prev = kline.iloc[-2] if len(kline) > 1 else latest
        close = _safe_float(latest.get("close"))
        ma5 = latest.get("MA5")
        ma10 = latest.get("MA10")
        ma20 = latest.get("MA20")
        ma60 = latest.get("MA60")
        dif = latest.get("MACD_DIF")
        dea = latest.get("MACD_DEA")
        rsi = latest.get("RSI")
        volume = _safe_float(latest.get("volume"))
        vol_ma5 = _safe_float(latest.get("VOL_MA5"))
        volume_ratio = volume / vol_ma5 if vol_ma5 > 0 else 0

        trend_parts = []
        if pd.notna(ma5) and close > float(ma5):
            trend_parts.append("站上MA5")
        if pd.notna(ma20) and close > float(ma20):
            trend_parts.append("站上MA20")
        if pd.notna(ma20) and pd.notna(ma60) and float(ma20) > float(ma60):
            trend_parts.append("中期均线偏多")
        if pd.notna(dif) and pd.notna(dea):
            trend_parts.append("MACD多头" if float(dif) > float(dea) else "MACD偏弱")
        if volume_ratio:
            trend_parts.append(f"量比{volume_ratio:.1f}")

        return {
            "available": True,
            "date": latest["date"].strftime("%Y-%m-%d") if hasattr(latest.get("date"), "strftime") else str(latest.get("date", "")),
            "close": round(close, 2),
            "previous_close": round(_safe_float(prev.get("close")), 2),
            "ma5": round(float(ma5), 2) if pd.notna(ma5) else None,
            "ma10": round(float(ma10), 2) if pd.notna(ma10) else None,
            "ma20": round(float(ma20), 2) if pd.notna(ma20) else None,
            "ma60": round(float(ma60), 2) if pd.notna(ma60) else None,
            "macd_dif": round(float(dif), 4) if pd.notna(dif) else None,
            "macd_dea": round(float(dea), 4) if pd.notna(dea) else None,
            "rsi": round(float(rsi), 1) if pd.notna(rsi) else None,
            "volume_ratio": round(volume_ratio, 2),
            "summary": "；".join(trend_parts) if trend_parts else "技术信号不足",
        }

    def _calc_summary(self, capital: float, market_value: float, total_cost: float,
                      positions: list) -> dict:
        total_asset = capital - total_cost + market_value  # 剩余现金 + 持仓市值
        available_cash = capital - total_cost  # 剩余可用资金
        total_pnl = market_value - total_cost
        total_pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0.0
        position_ratio = (market_value / total_asset * 100) if total_asset > 0 else 0.0
        win_count = sum(1 for p in positions if p.get("pnl", 0) > 0)
        loss_count = sum(1 for p in positions if p.get("pnl", 0) < 0)

        return {
            "total_asset": round(total_asset, 2),
            "available_cash": round(available_cash, 2),
            "market_value": round(market_value, 2),
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl_pct, 2),
            "position_ratio": round(position_ratio, 1),
            "position_count": len(positions),
            "win_count": win_count,
            "loss_count": loss_count,
        }

    @staticmethod
    def _empty_summary() -> dict:
        return {
            "total_asset": 0, "available_cash": 0, "market_value": 0,
            "total_pnl": 0, "total_pnl_pct": 0, "position_ratio": 0,
            "position_count": 0, "win_count": 0, "loss_count": 0,
        }
