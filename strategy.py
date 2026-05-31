"""
小资金稳健策略引擎
=================
专为小资金设计，追求高胜率 + 严格风控。

核心规则：
1. 买入需要至少2类信号同时确认（如趋势+量价）
2. 卖出单信号即可退出，及时止损
3. 移动止损保护浮盈
4. 时间止损避免无限持仓
5. 波动率(ATR)调整仓位
"""
import numpy as np
import pandas as pd
from typing import Optional, Tuple
from datetime import datetime

from config import RISK


# ── 信号类别 ──
SIGNAL_CATEGORIES = {
    "volume_breakout": "volume",
    "ma_cross": "trend",
    "macd_signal": "momentum",
    "kdj_signal": "momentum",
    "volume_price": "volume",
    "rsi_signal": "mean_reversion",
}


class SignalType:
    BUY = "买入"
    STOP_LOSS = "止损"
    TAKE_PROFIT = "止盈"
    TRAILING_STOP = "移动止损"
    TIME_STOP = "时间止损"
    SELL = "卖出"


class TradeSignal:
    def __init__(self, code: str, name: str, signal: str,
                 price: float, reason: str, score: float = 0):
        self.code = code
        self.name = name
        self.signal = signal
        self.price = price
        self.reason = reason
        self.score = score
        self.timestamp = datetime.now()

    def __repr__(self):
        return f"[{self.signal}] {self.code} {self.name} @{self.price:.2f} | {self.reason}"

    def to_dict(self):
        return {
            "code": self.code, "name": self.name,
            "signal": self.signal, "price": self.price,
            "reason": self.reason, "score": self.score,
        }


# ── 策略参数（小资金专用）──
STRATEGY = {
    "name": "小资金稳健策略",
    "description": "多信号确认 + 移动止损 + ATR仓位，适合小资金",
    "signal_weights": {
        "volume_breakout": 0.20,
        "ma_cross": 0.25,        # 趋势最重要
        "macd_signal": 0.20,
        "kdj_signal": 0.10,
        "volume_price": 0.15,
        "rsi_signal": 0.10,
    },
    "stop_loss": -0.03,           # -3% 硬止损
    "take_profit": 0.06,          # +6% 止盈
    "trailing_activation": 0.03,  # 涨3%后启动移动止损
    "trailing_distance": 0.025,   # 从最高点回撤2.5%退出
    "max_hold_days": 25,          # 最长持仓25天
    "min_signals": 2,             # 至少2类信号
}

PARAMETER_BOUNDS = {
    "stop_loss": (-0.20, -0.005),
    "take_profit": (0.005, 0.50),
    "trailing_activation": (0.005, 0.50),
    "trailing_distance": (0.005, 0.20),
    "max_hold_days": (3, 120),
    "min_signals": (1, len(set(SIGNAL_CATEGORIES.values()))),
    "position_size_pct": (0.01, 0.50),
}


class StrategyEngine:
    """小资金稳健策略引擎"""

    def __init__(self, parameters: dict = None):
        cfg = dict(STRATEGY)
        cfg["signal_weights"] = dict(STRATEGY["signal_weights"])
        cfg["position_size_pct"] = RISK["position_pct"]
        if parameters:
            for key in PARAMETER_BOUNDS:
                if key in parameters:
                    cfg[key] = parameters[key]
        cfg = self._validate_parameters(cfg)
        self.name = cfg["name"]
        self.signal_weights = dict(cfg["signal_weights"])
        self.stop_loss_pct = cfg["stop_loss"]
        self.take_profit_pct = cfg["take_profit"]
        self.trailing_activation = cfg["trailing_activation"]
        self.trailing_distance = cfg["trailing_distance"]
        self.max_hold_days = cfg["max_hold_days"]
        self.min_signals = cfg["min_signals"]
        self.position_size_pct = cfg["position_size_pct"]

    def get_config(self) -> dict:
        return {
            "name": self.name,
            "stop_loss": self.stop_loss_pct,
            "take_profit": self.take_profit_pct,
            "trailing_activation": self.trailing_activation,
            "trailing_distance": self.trailing_distance,
            "max_hold_days": self.max_hold_days,
            "min_signals": self.min_signals,
            "position_size_pct": self.position_size_pct,
        }

    @staticmethod
    def _validate_parameters(cfg: dict) -> dict:
        normalised = dict(cfg)
        try:
            for key in ("stop_loss", "take_profit", "trailing_activation",
                        "trailing_distance", "position_size_pct"):
                normalised[key] = float(normalised[key])
            for key in ("max_hold_days", "min_signals"):
                normalised[key] = int(normalised[key])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("策略参数格式不正确") from exc
        for key, (minimum, maximum) in PARAMETER_BOUNDS.items():
            if not minimum <= normalised[key] <= maximum:
                raise ValueError(f"{key} 超出允许范围 [{minimum}, {maximum}]")
        if normalised["trailing_distance"] >= normalised["take_profit"]:
            raise ValueError("移动止损回撤幅度必须小于止盈幅度")
        return normalised

    # ──────────── 买入 ────────────

    def generate_buy_signals(self, kline: pd.DataFrame,
                              stock_info: dict) -> Optional[TradeSignal]:
        if kline.empty or len(kline) < 30:
            return None

        latest = kline.iloc[-1]
        pre = kline.iloc[-2] if len(kline) > 1 else latest
        code = stock_info.get("code", "")
        name = stock_info.get("name", "")
        price = float(stock_info.get("price", latest["close"]))

        # 检查所有信号
        triggered = []
        for sig_name, weight in self.signal_weights.items():
            result = self._check_signal(sig_name, latest, pre, kline)
            if result:
                reason, confidence = result
                cat = SIGNAL_CATEGORIES.get(sig_name, "other")
                triggered.append((cat, reason, confidence, weight))

        if not triggered:
            return None

        # 至少 min_signals 个不同类别的信号
        cats = set(t[0] for t in triggered)
        if len(cats) < self.min_signals:
            return None

        # 综合评分
        score = min(100, len(cats) * 20 + int(
            sum(c * w for _, _, c, w in triggered) / max(sum(w for _, _, _, w in triggered), 0.001) * 30
        ))

        reasons = [r for _, r, _, _ in triggered]
        combined = " | ".join(reasons)

        return TradeSignal(
            code=code, name=name, signal=SignalType.BUY,
            price=price, reason=combined, score=score,
        )

    def _check_signal(self, name: str, latest, pre, kline):
        checkers = {
            "volume_breakout": self._check_volume_breakout,
            "ma_cross": self._check_ma_cross,
            "macd_signal": self._check_macd,
            "kdj_signal": self._check_kdj,
            "volume_price": self._check_volume_price,
            "rsi_signal": self._check_rsi,
        }
        fn = checkers.get(name)
        return fn(latest, pre, kline) if fn else None

    def _check_volume_breakout(self, latest, pre, kline):
        """放量突破MA20"""
        vol_ma5 = latest.get("VOL_MA5", 0)
        if pd.isna(vol_ma5) or vol_ma5 <= 0:
            return None
        vol_ratio = latest["volume"] / vol_ma5
        if vol_ratio < 1.3 or latest["close"] <= latest["open"]:
            return None
        ma20 = latest.get("MA20", 0)
        if pd.isna(ma20):
            return None
        if latest["close"] > ma20 and pre.get("close", 0) <= pre.get("MA20", 0):
            return (f"突破MA20量比{vol_ratio:.1f}", min(1.0, 0.6 + (vol_ratio - 1.3) * 0.2))
        if latest["close"] > ma20 and vol_ratio >= 1.5:
            return (f"放量站上MA20", min(0.7, 0.4 + (vol_ratio - 1.5) * 0.15))
        if vol_ratio >= 2.0:
            return (f"放量{vol_ratio:.1f}倍", 0.3)
        return None

    def _check_ma_cross(self, latest, pre, kline):
        """均线金叉或多头排列"""
        if any(pd.isna(latest.get(m)) for m in ["MA5", "MA10"]):
            return None
        if latest["MA5"] > latest["MA10"] and pre["MA5"] <= pre["MA10"]:
            return ("MA5金叉MA10", 0.85)
        if (latest["MA5"] > latest["MA10"] and
                latest["MA10"] > latest.get("MA20", 0)):
            return ("均线多头排列", 0.7)
        if latest["MA5"] > latest["MA10"]:
            return ("短期均线向上", 0.35)
        return None

    def _check_macd(self, latest, pre, kline):
        """MACD信号"""
        if any(pd.isna(latest.get(k)) for k in ["MACD_DIF", "MACD_DEA"]):
            return None
        d, de = latest["MACD_DIF"], latest["MACD_DEA"]
        pd_ = pre["MACD_DIF"]
        if d > de and pd_ <= pre["MACD_DEA"]:
            return ("MACD零上金叉", 0.9) if d > 0 else ("MACD零下金叉", 0.55)
        if d > de and d > pd_:
            return ("MACD多头增强", 0.45)
        if d > de:
            return ("MACD多头", 0.3)
        return None

    def _check_kdj(self, latest, pre, kline):
        """KDJ金叉"""
        if any(pd.isna(latest.get(k)) for k in ["KDJ_K", "KDJ_D"]):
            return None
        k, d = latest["KDJ_K"], latest["KDJ_D"]
        if k > d and pre["KDJ_K"] <= pre["KDJ_D"]:
            return ("KDJ超卖金叉", 0.8) if k < 30 else ("KDJ金叉", 0.6) if k < 50 else ("KDJ金叉", 0.4)
        if k > d:
            return ("KDJ多头", 0.25)
        return None

    def _check_volume_price(self, latest, pre, kline):
        """量价配合"""
        if len(kline) < 5:
            return None
        recent = kline.tail(5)
        up = recent[recent["close"] > recent["open"]]
        dn = recent[recent["close"] < recent["open"]]
        if len(up) == 0 or len(dn) == 0:
            return None
        ratio = up["volume"].mean() / max(dn["volume"].mean(), 0.001)
        change = (latest["close"] - recent.iloc[0]["close"]) / recent.iloc[0]["close"]
        if ratio >= 1.5 and change > 0.02:
            return (f"上涨放量{ratio:.1f}倍", 0.7)
        if ratio >= 1.3 and change > 0.01:
            return (f"量价健康", 0.45)
        return None

    def _check_rsi(self, latest, pre, kline):
        """RSI超卖反弹"""
        if pd.isna(latest.get("RSI")):
            return None
        r, pr = latest["RSI"], pre["RSI"]
        if r > pr and pr < 30 and r < 50:
            return (f"RSI超卖反弹({r:.0f})", 0.75)
        if r < 30 and len(kline) >= 3:
            p3 = kline.iloc[-3].get("RSI")
            if p3 is not None and not pd.isna(p3) and r > pr and pr < 30 and p3 < 30:
                return (f"RSI底背离({r:.0f})", 0.8)
        if r > pr and pr < 35:
            return (f"RSI回升({r:.0f})", 0.4)
        return None

    # ──────────── 卖出（单信号触发） ────────────

    def generate_sell_signals(self, kline: pd.DataFrame,
                               position: dict) -> Optional[TradeSignal]:
        current_price = position["current_price"]
        buy_price = position["buy_price"]
        pnl = (current_price - buy_price) / buy_price
        code = position.get("code", "")
        name = position.get("name", "")
        highest = max(position.get("highest_price", buy_price), current_price)
        trailing = position.get("trailing_stop")

        # 1. 硬止损
        if pnl <= self.stop_loss_pct:
            return TradeSignal(code, name, SignalType.STOP_LOSS, current_price,
                               f"止损 {pnl*100:.1f}%")

        # 2. 移动止损：一旦最高价达到启动线，回落后仍持续生效。
        peak_pnl = (highest - buy_price) / buy_price
        if peak_pnl >= self.trailing_activation:
            tp = highest * (1 - self.trailing_distance)
            trailing = max(trailing, tp) if trailing else tp
            if current_price <= trailing:
                return TradeSignal(code, name, SignalType.TRAILING_STOP, current_price,
                                   f"移动止损 回撤{self.trailing_distance*100:.0f}%",
                                   score=round((current_price - buy_price) / buy_price * 100, 1))

        # 3. 止盈
        if pnl >= self.take_profit_pct:
            return TradeSignal(code, name, SignalType.TAKE_PROFIT, current_price,
                               f"止盈 {pnl*100:.1f}%")

        # 4. 时间止损，按交易日计算最长持仓周期。
        entry_date = position.get("entry_date")
        if entry_date is not None and not kline.empty:
            dates = pd.to_datetime(kline["date"], errors="coerce")
            held_days = int((dates >= pd.to_datetime(entry_date)).sum()) - 1
            if held_days >= self.max_hold_days:
                return TradeSignal(code, name, SignalType.TIME_STOP, current_price,
                                   f"持仓达到{held_days}个交易日")

        # 5. 技术卖出（任意一个）
        if kline.empty or len(kline) < 10:
            return None

        latest = kline.iloc[-1]
        pre = kline.iloc[-2] if len(kline) > 1 else latest
        sell = self._check_sell(latest, pre)
        if sell:
            return TradeSignal(code, name, SignalType.SELL, current_price, sell)

        return None

    def _check_sell(self, latest, pre) -> Optional[str]:
        reasons = []
        # MACD死叉
        if (not pd.isna(latest.get("MACD_DIF")) and not pd.isna(latest.get("MACD_DEA"))
                and not pd.isna(pre.get("MACD_DIF")) and not pd.isna(pre.get("MACD_DEA"))
                and latest["MACD_DIF"] < latest["MACD_DEA"]
                and pre["MACD_DIF"] >= pre["MACD_DEA"]):
            reasons.append("MACD死叉")
        # 跌破MA10
        if (not pd.isna(latest.get("MA10"))
                and latest["close"] < latest["MA10"]
                and pre["close"] >= pre["MA10"]):
            reasons.append("跌破MA10")
        # 跌破MA20
        if (not pd.isna(latest.get("MA20"))
                and latest["close"] < latest["MA20"]
                and pre["close"] >= pre["MA20"]):
            reasons.append("跌破MA20")
        # MACD顶背离
        if (not pd.isna(latest.get("MACD_DIF")) and not pd.isna(pre.get("MACD_DIF"))
                and latest["close"] > pre["close"]
                and latest["MACD_DIF"] < pre["MACD_DIF"]):
            reasons.append("MACD顶背离")
        # RSI超买
        if not pd.isna(latest.get("RSI")) and latest["RSI"] > 70:
            reasons.append(f"RSI超买({latest['RSI']:.0f})")
        # 缩量下跌
        if (latest["close"] < pre["close"]
                and latest.get("volume", 0) < pre.get("volume", 0) * 0.8):
            reasons.append("缩量下跌")
        return "; ".join(reasons) if reasons else None

    # ──────────── 仓位 ────────────

    def calc_position_size(self, capital: float, price: float,
                           score: float, kline: pd.DataFrame = None) -> Tuple[int, float]:
        ratio = self.position_size_pct * min(1.0, score / 70)
        # ATR调整
        if kline is not None and len(kline) >= 14:
            atr = self._calc_atr(kline)
            if atr > 0 and price > 0:
                vol_factor = max(0.5, min(2.0, 0.02 / max(atr / price, 0.005)))
                ratio *= vol_factor
        ratio = min(ratio, self.position_size_pct * 1.5)

        target = capital * ratio
        qty = max(100, int(target / price / 100) * 100)
        amount = qty * price
        if amount > capital:
            qty = int(capital / price / 100) * 100
        return qty, qty * price

    @staticmethod
    def _calc_atr(kline: pd.DataFrame, period: int = 14) -> float:
        if len(kline) < period + 1:
            return 0
        h, l, c = kline["high"].values, kline["low"].values, kline["close"].values
        tr = np.maximum(h[1:] - l[1:],
                        np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
        return float(np.mean(tr[-period:]))
