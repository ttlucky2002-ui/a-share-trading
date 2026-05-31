"""回测引擎（小资金策略版）"""
import numpy as np
import pandas as pd
from typing import Optional, List

from config import BACKTEST, RISK
from data_feed import DataFeed
from strategy import StrategyEngine, SignalType


class BacktestEngine:
    """回测引擎"""

    def __init__(self, initial_capital: float = None,
                 strategy: StrategyEngine = None,
                 data_feed: DataFeed = None):
        self.df = data_feed if data_feed else DataFeed()
        self.strategy = strategy if strategy else StrategyEngine()
        self.initial_capital = initial_capital or BACKTEST["initial_capital"]
        self.commission = BACKTEST["commission"]
        self.stamp_tax = BACKTEST["stamp_tax"]
        self.slippage = BACKTEST["slippage"]
        self.trade_log: List[dict] = []

    def run(self, code: str, name: str = "", days: int = 365,
            with_benchmark: bool = True,
            kline: pd.DataFrame = None) -> Optional[dict]:
        kline = (
            kline.copy().reset_index(drop=True)
            if kline is not None else
            self.df.get_kline(code, count=min(days, 800))
        )
        if kline.empty or len(kline) < 30:
            return None

        capital = self.initial_capital
        available = capital
        self.trade_log = []
        equity_curve = []

        qty = 0
        buy_price = 0
        entry_date = None
        highest_price = 0
        trailing_stop = None
        entry_signal = ""
        peak_equity = capital
        circuit_breaker = False

        benchmark_curve = self._get_buy_hold_curve(kline.iloc[20:]) if with_benchmark else None

        for i in range(20, len(kline)):
            cur = kline.iloc[i]
            date = cur["date"]
            close = cur["close"]
            k_up = kline.iloc[:i+1].reset_index(drop=True)

            # 卖出
            if qty > 0:
                if close > highest_price:
                    highest_price = close
                sell = self.strategy.generate_sell_signals(k_up, {
                    "code": code, "name": name,
                    "buy_price": buy_price,
                    "current_price": close,
                    "highest_price": highest_price,
                    "trailing_stop": trailing_stop,
                    "entry_date": entry_date,
                })
                if sell:
                    sp = close * (1 - self.slippage)
                    amount = qty * sp
                    fee = amount * self.commission
                    tax = amount * self.stamp_tax
                    pnl = amount - qty * buy_price - fee - tax
                    available += amount - fee - tax
                    self.trade_log.append({
                        "买入日期": str(entry_date)[:10] if entry_date else "",
                        "卖出日期": str(date)[:10],
                        "代码": code, "名称": name,
                        "方向": sell.signal,
                        "买入价": round(buy_price, 2),
                        "卖出价": round(sp, 2),
                        "数量": qty,
                        "盈亏": round(pnl, 2),
                        "盈亏%": round((sp - buy_price) / buy_price * 100, 2),
                        "策略": sell.reason,
                        "入场信号": entry_signal[:60],
                    })
                    qty = 0
                    trailing_stop = None

            # 买入
            if qty == 0 and not circuit_breaker:
                buy = self.strategy.generate_buy_signals(k_up, {
                    "code": code, "name": name, "price": close,
                })
                if buy and buy.signal == SignalType.BUY:
                    bp = close * (1 + self.slippage)
                    max_qty, _ = self.strategy.calc_position_size(
                        available, bp, buy.score, k_up
                    )
                    if max_qty >= 100:
                        qty = max_qty
                        buy_price = bp
                        entry_date = date
                        highest_price = bp
                        trailing_stop = None
                        entry_signal = buy.reason
                        cost = qty * bp
                        available -= cost + cost * self.commission

            ev = available + (qty * close if qty > 0 else 0)
            equity_curve.append({
                "date": str(date)[:10], "equity": round(ev, 2),
                "position": qty > 0, "price": float(close),
            })
            if ev > peak_equity:
                peak_equity = ev
            if peak_equity > 0 and (ev - peak_equity) / peak_equity <= RISK["max_drawdown"]:
                circuit_breaker = True

        # 期末平仓
        if qty > 0 and len(kline) > 0:
            cp = kline.iloc[-1]["close"]
            sp = cp * (1 - self.slippage)
            amount = qty * sp
            fee = amount * self.commission
            tax = amount * self.stamp_tax
            available += amount - fee - tax
            self.trade_log.append({
                "买入日期": str(entry_date)[:10] if entry_date else "",
                "卖出日期": str(kline.iloc[-1]["date"])[:10],
                "代码": code, "名称": name,
                "方向": "期末平仓",
                "买入价": round(buy_price, 2),
                "卖出价": round(sp, 2),
                "数量": qty,
                "盈亏": round(amount - qty * buy_price - fee - tax, 2),
                "盈亏%": round((sp - buy_price) / buy_price * 100, 2),
                "策略": "期末强制平仓",
                "入场信号": entry_signal[:60],
            })
            equity_curve[-1]["equity"] = round(available, 2)
            equity_curve[-1]["position"] = False

        perf = self._calc_performance(equity_curve, capital)
        perf["code"] = code
        perf["name"] = name
        perf["trades"] = self.trade_log
        perf["equity_curve"] = equity_curve
        perf["initial_capital"] = capital
        perf["strategy_config"] = self.strategy.get_config()

        if benchmark_curve:
            perf["benchmark_curve"] = benchmark_curve
            perf["benchmark_name"] = f"{name or code} 买入持有"
            perf["benchmark_return"] = round(benchmark_curve[-1]["value"] - 100, 2)
            perf["excess_return"] = round(perf["total_return"] - perf["benchmark_return"], 2)

        return perf

    @staticmethod
    def _get_buy_hold_curve(period: pd.DataFrame) -> list:
        if period.empty:
            return []
        base = float(period.iloc[0]["close"])
        if base <= 0:
            return []
        return [{
            "date": str(row["date"])[:10],
            "value": round(float(row["close"]) / base * 100, 2),
        } for _, row in period.iterrows()]

    def _calc_performance(self, curve: List[dict], initial: float) -> dict:
        if not curve:
            return {}
        df = pd.DataFrame(curve)
        final = df["equity"].iloc[-1]
        total = (final / initial - 1) * 100
        annual = ((final / initial) ** (252 / max(len(df), 1)) - 1) * 100
        df["peak"] = df["equity"].cummax()
        df["dd"] = (df["equity"] - df["peak"]) / df["peak"] * 100
        mdd = df["dd"].min()

        wins = sum(1 for t in self.trade_log if t.get("盈亏%", 0) > 0)
        total_trades = len(self.trade_log)
        wr = wins / max(total_trades, 1) * 100

        df["ret"] = df["equity"].pct_change().fillna(0)
        sharpe = (df["ret"].mean() / max(df["ret"].std(), 0.001)) * np.sqrt(252)

        win_pcts = [t["盈亏%"] for t in self.trade_log if t["盈亏%"] > 0]
        loss_pcts = [t["盈亏%"] for t in self.trade_log if t["盈亏%"] <= 0]
        aw = np.mean(win_pcts) if win_pcts else 0
        al = abs(np.mean(loss_pcts)) if loss_pcts else 0
        pl = round(aw / al, 2) if al > 0 else 0

        exit_stats = {}
        for t in self.trade_log:
            d = t.get("方向", "其他")
            exit_stats[d] = exit_stats.get(d, 0) + 1

        return {
            "total_return": round(total, 2),
            "annual_return": round(annual, 2),
            "max_drawdown": round(mdd, 2),
            "win_rate": round(wr, 1),
            "sharpe_ratio": round(sharpe, 2),
            "trade_count": total_trades,
            "profit_loss_ratio": pl,
            "avg_win_pct": round(aw, 2),
            "avg_loss_pct": round(-al, 2),
            "exit_stats": exit_stats,
        }
