"""
中长期综合选股器。

先用流动性与交易边界建立可执行股票池，并为池内全部股票请求最新财务
指标报告；基本面合格后追加技术评分，再结合近期板块资金热点精选十股。
"""
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from config import LONG_TERM, SCREEN
from data_feed import DataFeed


def _clip(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def _growth_score(value: float) -> float:
    if value <= -20:
        return 0
    if value < 0:
        return 25
    if value < 5:
        return 50
    if value < 15:
        return 70
    if value < 35:
        return 90
    return 80


class LongTermFundamentalScreener:
    """面向一周以上持仓周期的可复核综合选股流程。"""

    def __init__(self, data_feed=None):
        self.df = data_feed if data_feed is not None else DataFeed()
        self._progress = {"state": "idle", "done": 0, "total": 0}
        self.summary = {}
        self.recommendations = []

    @property
    def progress(self) -> dict:
        return dict(self._progress)

    def screen(self, universe_limit: int = None, ai_advisor=None) -> list:
        stocks = self.df.get_stock_list()
        if stocks.empty:
            self.summary = {"error": "获取市场股票池失败"}
            self.recommendations = []
            return []

        pool = self._candidate_pool(stocks)
        # 默认覆盖全部初筛股票；正数限制仅保留给命令行诊断/兼容调用。
        limit = LONG_TERM["universe_limit"] if universe_limit is None else int(universe_limit)
        if limit > 0:
            pool = pool.head(limit)

        codes = pool["code"].tolist()
        self._progress = {
            "state": "fundamentals",
            "done": 0,
            "total": len(codes),
            "message": "逐股拉取最新财务指标报告",
        }

        def on_progress(done, total, code, available):
            self._progress.update({
                "state": "fundamentals",
                "done": done,
                "total": total,
                "current_code": str(code),
                "financial_available": available,
            })

        financials = self.df.get_financials_batch(codes, progress_callback=on_progress)
        self._progress.update({"state": "scoring", "message": "计算中长期基本面评分"})

        ranked = []
        successful = 0
        for _, quote in pool.iterrows():
            financial = financials.get(str(quote["code"]), {})
            if not financial.get("available"):
                continue
            successful += 1
            ranked.append(self._evaluate(financial))

        fundamental_ranked = [
            item for item in ranked
            if item["fundamental_score"] >= LONG_TERM["minimum_score"]
        ]
        fundamental_ranked.sort(key=lambda item: item["fundamental_score"], reverse=True)

        self._progress.update({
            "state": "technical",
            "done": 0,
            "total": len(fundamental_ranked),
            "message": "在基本面合格池上计算技术评分与综合排名",
        })
        with ThreadPoolExecutor(max_workers=min(6, len(fundamental_ranked) or 1)) as executor:
            futures = {
                executor.submit(self._technical_analysis, item["code"]): item
                for item in fundamental_ranked
            }
            for index, future in enumerate(as_completed(futures), start=1):
                item = futures[future]
                try:
                    item.update(future.result())
                except Exception:
                    item.update({
                        "technical_available": False,
                        "technical_score": 0,
                        "technical_reason": "技术数据请求失败",
                        "trend_confirmation": "趋势数据不可用",
                    })
                item["composite_score"] = self._composite_score(item)
                self._progress.update({
                    "done": index,
                    "current_code": item["code"],
                    "technical_available": item.get("technical_available", False),
                })

        fundamental_ranked.sort(key=lambda item: item["composite_score"], reverse=True)
        self._progress.update({
            "state": "market",
            "done": len(fundamental_ranked),
            "total": len(fundamental_ranked),
            "message": "结合近期热点与板块资金流精选十股",
        })
        selected, rotation_boards = self._select_recommendations(fundamental_ranked)
        self.recommendations = selected[:LONG_TERM["recommendation_count"]]
        recommendation_codes = {
            item["code"]: rank for rank, item in enumerate(self.recommendations, start=1)
        }
        for item in fundamental_ranked:
            item["recommendation_rank"] = recommendation_codes.get(item["code"])

        self.summary = {
            "holding_horizon": LONG_TERM["holding_horizon"],
            "pool_count": len(pool),
            "financial_success_count": successful,
            "fundamental_qualified_count": len(fundamental_ranked),
            "selected_count": len(self.recommendations),
            "comprehensive_count": min(len(fundamental_ranked), LONG_TERM["result_limit"]),
            "scan_scope": "全部初筛股票" if not limit else f"诊断限制 {limit} 只",
            "data_source": "东方财富财务指标 API / K线指标 / 板块资金流",
            "weights": dict(LONG_TERM["weights"]),
            "composite_weights": dict(LONG_TERM["composite_weights"]),
            "selection_weights": dict(LONG_TERM["selection_weights"]),
            "rotation_boards": rotation_boards[:8],
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._progress.update({"state": "done", "done": len(codes), "total": len(codes)})
        return fundamental_ranked[:LONG_TERM["result_limit"]]

    def _candidate_pool(self, stocks: pd.DataFrame) -> pd.DataFrame:
        """过滤不可执行标的；排序只控制请求批次，不参与财务评分。"""
        conditions = (
            (stocks["price"] >= SCREEN["price_min"]) &
            (stocks["price"] <= SCREEN["price_max"]) &
            (stocks["market_cap"] >= LONG_TERM["market_cap_min"]) &
            (stocks["amount"] >= LONG_TERM["average_amount_min"] * 1e8) &
            (stocks["turnover_rate"] <= LONG_TERM["turnover_max"])
        )
        if SCREEN["exclude_st"]:
            conditions &= ~stocks["is_st"]
        if SCREEN["exclude_kcb"]:
            conditions &= stocks["board"] != "科创板"
        if SCREEN["exclude_bj"]:
            conditions &= stocks["board"] != "北交所"
        return stocks[conditions].copy().sort_values("amount", ascending=False)

    def _evaluate(self, financial: dict) -> dict:
        roe = float(financial.get("annualized_roe") or 0)
        revenue_growth = float(financial.get("revenue_growth") or 0)
        profit_growth = float(financial.get("profit_growth") or 0)
        gross_margin = financial.get("gross_margin")
        eps = float(financial.get("eps") or 0)
        cash_ps = float(financial.get("operating_cf_per_share") or 0)
        pe = float(financial.get("pe") or 0)
        pb = float(financial.get("pb") or 0)

        roe_score = _clip(roe * 5)
        margin_score = _clip(float(gross_margin) * 2) if gross_margin is not None else roe_score
        quality = round(roe_score * 0.7 + margin_score * 0.3)

        growth = round(_growth_score(revenue_growth) * 0.4 + _growth_score(profit_growth) * 0.6)

        if pe <= 0:
            pe_score = 10
        elif pe <= 12:
            pe_score = 90
        elif pe <= 25:
            pe_score = 78
        elif pe <= 40:
            pe_score = 52
        elif pe <= 70:
            pe_score = 28
        else:
            pe_score = 10
        if pb <= 0:
            pb_score = 35
        elif pb <= 2:
            pb_score = 90
        elif pb <= 5:
            pb_score = 65
        elif pb <= 10:
            pb_score = 40
        else:
            pb_score = 18
        valuation = round(pe_score * 0.65 + pb_score * 0.35)

        if eps <= 0:
            cashflow = 10 if cash_ps >= 0 else 0
        else:
            coverage = cash_ps / eps
            cashflow = round(_clip(coverage * 70))

        weights = LONG_TERM["weights"]
        score = round(
            quality * weights["quality"] +
            growth * weights["growth"] +
            valuation * weights["valuation"] +
            cashflow * weights["cashflow"]
        )
        reasons = [
            f"{financial.get('report_date', '--')} 报告期",
            f"ROE年化参考 {roe:.1f}%",
            f"营收/净利同比 {revenue_growth:+.1f}%/{profit_growth:+.1f}%",
        ]
        risks = []
        if eps <= 0:
            risks.append("每股收益非正")
        if revenue_growth < 0 or profit_growth < 0:
            risks.append("盈利增长承压")
        if pe <= 0 or pe > 50:
            risks.append("估值指标异常或偏高")
        if eps > 0 and cash_ps < eps * 0.5:
            risks.append("经营现金流覆盖偏低")
        if gross_margin is None:
            risks.append("毛利率字段不适用于或未披露")

        return {
            "code": financial["code"],
            "name": financial.get("name", ""),
            "board": financial.get("board", ""),
            "price": financial.get("price", 0),
            "market_cap": financial.get("market_cap", 0),
            "report_date": financial.get("report_date", ""),
            "notice_date": financial.get("notice_date", ""),
            "fundamental_score": score,
            "quality_score": quality,
            "growth_score": growth,
            "valuation_score": valuation,
            "cashflow_score": cashflow,
            "roe": financial.get("roe", 0),
            "annualized_roe": financial.get("annualized_roe", 0),
            "revenue_growth": revenue_growth,
            "profit_growth": profit_growth,
            "gross_margin": gross_margin,
            "eps": eps,
            "operating_cf_per_share": cash_ps,
            "pe": pe,
            "pb": pb,
            "main_net": financial.get("main_net", 0),
            "main_net_pct": financial.get("main_net_pct", 0),
            "fundamental_reason": "；".join(reasons),
            "risk": "；".join(risks) if risks else "未触发量化财务警示，仍需核验公告",
            "data_source": financial.get("data_source", ""),
        }

    def _technical_analysis(self, code: str) -> dict:
        kline = self.df.get_kline(code, count=80)
        if kline.empty:
            return {
                "technical_available": False,
                "technical_score": 0,
                "technical_reason": "技术数据不可用",
                "trend_confirmation": "趋势数据不可用",
            }
        latest = kline.iloc[-1]
        previous = kline.iloc[-2] if len(kline) > 1 else latest
        close = float(latest.get("close") or 0)
        ma20 = latest.get("MA20")
        ma60 = latest.get("MA60")
        score = 0
        reasons = []
        if pd.notna(ma20) and pd.notna(ma60) and close > ma20 > ma60:
            trend = "站上MA20/MA60，中期趋势确认"
            score += 35
            reasons.append("中期均线多头")
        elif pd.notna(ma20) and close > ma20:
            trend = "站上MA20，等待长期趋势确认"
            score += 20
            reasons.append("站上MA20")
        else:
            trend = "未站上MA20，仅保留基本面观察"

        dif = latest.get("MACD_DIF")
        dea = latest.get("MACD_DEA")
        if pd.notna(dif) and pd.notna(dea) and dif > dea:
            score += 15
            reasons.append("MACD多头")
            if dif > 0:
                score += 5

        rsi = latest.get("RSI")
        if pd.notna(rsi) and 40 <= float(rsi) <= 70:
            score += 10
            reasons.append("RSI健康区间")

        k_value = latest.get("KDJ_K")
        d_value = latest.get("KDJ_D")
        if pd.notna(k_value) and pd.notna(d_value) and k_value > d_value:
            score += 10
            reasons.append("KDJ多头")

        ma5 = latest.get("MA5")
        ma10 = latest.get("MA10")
        previous_ma5 = previous.get("MA5")
        previous_ma10 = previous.get("MA10")
        if pd.notna(ma5) and pd.notna(ma10) and ma5 > ma10:
            score += 10
            reasons.append("短期均线支持")
            if (pd.notna(previous_ma5) and pd.notna(previous_ma10)
                    and previous_ma5 <= previous_ma10):
                score += 5
                reasons.append("短期金叉")

        volume = float(latest.get("volume") or 0)
        volume_ma5 = float(latest.get("VOL_MA5") or 0)
        volume_ratio = volume / volume_ma5 if volume_ma5 > 0 else 0
        if volume_ratio >= 1.2 and close >= float(latest.get("open") or close):
            score += 10
            reasons.append("量价配合")

        return {
            "technical_available": True,
            "technical_score": round(_clip(score)),
            "technical_reason": "；".join(reasons) if reasons else "技术信号偏弱",
            "trend_confirmation": trend,
            "volume_ratio": round(volume_ratio, 2),
        }

    def _composite_score(self, item: dict) -> int:
        weights = LONG_TERM["composite_weights"]
        return round(
            float(item.get("fundamental_score", 0)) * weights["fundamental"] +
            float(item.get("technical_score", 0)) * weights["technical"]
        )

    def _select_recommendations(self, ranked: list) -> tuple:
        if not ranked:
            return [], []
        rotation = self.df.get_rotation_matches(
            [item["code"] for item in ranked],
            top_n=LONG_TERM["theme_board_limit"],
        )
        matches = rotation.get("matches", {})
        boards = rotation.get("boards", [])
        market_data_available = bool(boards)
        weights = LONG_TERM["selection_weights"]
        for item in ranked:
            stock_matches = sorted(
                matches.get(item["code"], []),
                key=lambda board: board.get("flow_score", 0),
                reverse=True,
            )
            individual_score = _clip(50 + float(item.get("main_net_pct") or 0) * 3)
            if stock_matches:
                theme_score = float(stock_matches[0].get("flow_score") or 0)
                market_score = theme_score * 0.8 + individual_score * 0.2
            elif market_data_available:
                market_score = individual_score * 0.3
            else:
                market_score = 50
            item["market_flow_score"] = round(_clip(market_score))
            item["matched_themes"] = []
            for board in stock_matches[:3]:
                recent_flow = board.get("recent_main_net_inflow")
                recent_text = (
                    f"近5日净流入{recent_flow:+.2f}亿"
                    if recent_flow is not None else
                    f"当日净流入{board.get('main_net_inflow', 0):+.2f}亿"
                )
                item["matched_themes"].append(
                    f"{board['name']}({board['type']}, {recent_text})"
                )
            item["selection_score"] = round(
                item["composite_score"] * weights["composite"] +
                item["market_flow_score"] * weights["market_flow"]
            )
        selected = sorted(
            ranked,
            key=lambda item: (item["selection_score"], item["composite_score"]),
            reverse=True,
        )
        return selected, boards
