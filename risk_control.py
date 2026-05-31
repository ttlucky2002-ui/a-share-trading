"""Risk diagnostics based exclusively on authenticated brokerage account data."""

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from broker import BrokerError, guosen_client
from config import RISK


ACCOUNT_FIELDS = {
    "total_asset": ["total_asset", "totalAsset", "asset", "总资产", "资产总值"],
    "available": ["available", "available_cash", "enableBalance", "可用资金", "可用余额"],
    "position_value": ["position_value", "marketValue", "stockValue", "证券市值", "持仓市值"],
    "total_pnl": ["total_pnl", "profitLoss", "floatingProfit", "总盈亏", "浮动盈亏"],
    "total_pnl_pct": ["total_pnl_pct", "profitLossRatio", "yieldRate", "盈亏比例", "收益率"],
}
POSITION_FIELDS = {
    "code": ["code", "symbol", "securityCode", "stock_code", "证券代码"],
    "name": ["name", "securityName", "stock_name", "证券名称"],
    "quantity": ["quantity", "currentQty", "positionQty", "持仓数量", "股份余额"],
    "available_quantity": ["available_quantity", "availableQty", "enableQty", "可卖数量", "可用股份"],
    "cost_price": ["cost_price", "costPrice", "avgPrice", "成本价"],
    "current_price": ["current_price", "lastPrice", "price", "最新价", "当前价"],
    "market_value": ["market_value", "marketValue", "positionValue", "证券市值", "持仓市值"],
    "pnl": ["pnl", "profitLoss", "floatingProfit", "浮动盈亏"],
    "pnl_pct": ["pnl_pct", "profitLossRatio", "yieldRate", "盈亏比例", "收益率"],
}
TRADE_FIELDS = {
    "timestamp": ["timestamp", "time", "tradeTime", "businessTime", "成交时间", "发生时间"],
    "date": ["date", "tradeDate", "businessDate", "成交日期", "发生日期"],
    "code": ["code", "symbol", "securityCode", "stock_code", "证券代码"],
    "side": ["side", "direction", "businessFlag", "买卖方向", "委托方向"],
    "price": ["price", "tradePrice", "businessPrice", "成交价格", "成交均价"],
    "quantity": ["quantity", "tradeQty", "businessQty", "成交数量", "成交股数"],
    "amount": ["amount", "tradeAmount", "businessBalance", "成交金额", "发生金额"],
}


def _key_equal(key: str, alias: str) -> bool:
    return str(key).lower() == str(alias).lower()


def _lookup(payload: Any, aliases: Iterable[str]) -> Any:
    if isinstance(payload, dict):
        for alias in aliases:
            for key, value in payload.items():
                if _key_equal(key, alias) and not isinstance(value, (dict, list)):
                    return value
        for value in payload.values():
            found = _lookup(value, aliases)
            if found is not None:
                return found
    return None


def _number(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return None


def _find_records(payload: Any, aliases: Iterable[str]) -> List[dict]:
    if isinstance(payload, list):
        if payload and isinstance(payload[0], dict):
            if any(_lookup(item, aliases) is not None for item in payload):
                return payload
        for item in payload:
            records = _find_records(item, aliases)
            if records:
                return records
    if isinstance(payload, dict):
        for value in payload.values():
            records = _find_records(value, aliases)
            if records:
                return records
    return []


def _success_response(result: dict) -> Any:
    if not result.get("ok"):
        raise BrokerError(f"国信接口返回 HTTP {result.get('status_code', '--')}")
    return result.get("response", {})


class RealAccountRiskAnalyzer:
    """Assess configured limits against balances, positions and fills from Guosen."""

    def __init__(self, broker=None):
        self.broker = broker or guosen_client

    @staticmethod
    def _empty_summary() -> Dict[str, Any]:
        return {
            "total_asset": None,
            "available": None,
            "position_value": None,
            "total_pnl": None,
            "total_pnl_pct": None,
            "position_count": None,
        }

    def _base_result(self, status: dict) -> dict:
        return {
            "source": "国信证券真实账户 API",
            "connected": False,
            "configured": bool(status.get("account_query_configured")),
            "summary": self._empty_summary(),
            "exposure_pct": None,
            "risk_budget_pct": round(RISK["max_exposure"] * 100, 1),
            "alerts": [],
            "positions": [],
            "recent_trades": [],
            "risk": dict(RISK),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "stages": [
                {
                    "stage": "真实账户连接",
                    "status": "待验证",
                    "detail": "资金、持仓及成交必须由已授权国信接口返回",
                },
                {
                    "stage": "风险阈值评估",
                    "status": "等待数据",
                    "detail": f"单票上限 {RISK['position_pct']:.0%}，总仓位上限 {RISK['max_exposure']:.0%}",
                },
                {
                    "stage": "实盘委托边界",
                    "status": "已解锁" if status.get("live_enabled") else "实盘关闭",
                    "detail": "下单入口默认关闭，开启后仍要求逐单确认",
                },
            ],
        }

    @staticmethod
    def _normalise_position(item: dict, total_asset: Optional[float]) -> dict:
        position = {}
        for key, aliases in POSITION_FIELDS.items():
            value = _lookup(item, aliases)
            position[key] = value if key in ("code", "name") else _number(value)
        code = str(position.get("code") or "").split(".")[0]
        position["code"] = code
        market_value = position.get("market_value")
        if market_value is None and position.get("quantity") is not None and position.get("current_price") is not None:
            market_value = position["quantity"] * position["current_price"]
            position["market_value"] = round(market_value, 2)
        position["position_pct"] = (
            round(market_value / total_asset * 100, 2)
            if market_value is not None and total_asset
            else None
        )
        available = position.get("available_quantity")
        position["constraint"] = (
            f"可卖数量 {int(available)}" if available is not None else "接口未返回可卖数量"
        )
        position["status"] = "持仓观察"
        return position

    @staticmethod
    def _normalise_trade(item: dict) -> dict:
        record = {}
        for key, aliases in TRADE_FIELDS.items():
            value = _lookup(item, aliases)
            record[key] = value if key in ("timestamp", "date", "code", "side") else _number(value)
        record["code"] = str(record.get("code") or "").split(".")[0]
        if not record.get("timestamp"):
            record["timestamp"] = record.get("date") or ""
        return record

    def analyze(self) -> dict:
        status = self.broker.public_status()
        result = self._base_result(status)
        if not status.get("account_query_configured"):
            result["alerts"].append(
                "尚未取得真实账户数据：请在“国信下单”配置资金查询和持仓查询路径后刷新。"
            )
            return result

        try:
            account_payload = _success_response(self.broker.account())
            position_payload = _success_response(self.broker.positions())
            trade_payload = None
            if status.get("trade_query_configured"):
                trade_payload = _success_response(self.broker.trades())
        except BrokerError as exc:
            result["alerts"].append(f"真实账户查询失败：{exc}")
            result["stages"][0]["status"] = "查询失败"
            return result

        summary = self._empty_summary()
        for key, aliases in ACCOUNT_FIELDS.items():
            summary[key] = _number(_lookup(account_payload, aliases))
        position_rows = _find_records(position_payload, POSITION_FIELDS["code"])
        positions = [
            self._normalise_position(item, summary["total_asset"])
            for item in position_rows
        ]
        positions = [item for item in positions if item["code"]]
        summary["position_count"] = len(positions)
        if summary["position_value"] is None and positions and all(
            item.get("market_value") is not None for item in positions
        ):
            summary["position_value"] = round(sum(item["market_value"] for item in positions), 2)
        exposure = (
            round(summary["position_value"] / summary["total_asset"] * 100, 2)
            if summary["position_value"] is not None and summary["total_asset"]
            else None
        )

        alerts = []
        for position in positions:
            pnl_pct = position.get("pnl_pct")
            value_pct = position.get("position_pct")
            if pnl_pct is not None and pnl_pct <= RISK["stop_loss"] * 100:
                position["status"] = "触及止损阈值"
                alerts.append(f"{position['name'] or position['code']} 真实持仓盈亏已低于止损阈值。")
            elif pnl_pct is not None and pnl_pct >= RISK["take_profit"] * 100:
                position["status"] = "触及止盈阈值"
            if value_pct is not None and value_pct > RISK["position_pct"] * 100:
                alerts.append(f"{position['name'] or position['code']} 真实仓位超过单票风险上限。")
        if summary["position_count"] > RISK["max_positions"]:
            alerts.append("真实持仓标的数量超过当前配置上限。")
        if exposure is not None and exposure > RISK["max_exposure"] * 100:
            alerts.append("真实账户总仓位超过当前组合风险预算。")

        recent_trades = []
        if trade_payload is not None:
            trade_rows = _find_records(trade_payload, TRADE_FIELDS["code"])
            recent_trades = [self._normalise_trade(item) for item in trade_rows[:10]]
            today = datetime.now().strftime("%Y-%m-%d")
            today_count = sum(
                1 for trade in recent_trades if str(trade.get("timestamp", "")).startswith(today)
            )
            if today_count > RISK["max_trades_per_day"]:
                alerts.append("真实账户今日成交次数超过当前配置上限。")
        else:
            alerts.append("未配置真实成交查询路径，暂不能检查今日成交频率。")
        if not positions and not position_rows:
            alerts.append("持仓接口未返回可识别持仓；如账户确有持仓，请按授权报文字段扩展映射。")
        if not alerts:
            alerts.append("根据当前已返回的真实账户数据，未触发配置的风险阈值。")

        result.update({
            "connected": True,
            "summary": summary,
            "exposure_pct": exposure,
            "positions": positions,
            "recent_trades": recent_trades,
            "alerts": alerts,
        })
        result["stages"][0].update({"status": "已连接", "detail": "资金与持仓来自国信真实账户接口"})
        result["stages"][1].update({"status": "已评估", "detail": f"已核查 {len(positions)} 只真实持仓"})
        return result
