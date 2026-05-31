"""Streamlit Cloud entry point for the A-share research system.

The cloud edition intentionally excludes Guosen live trading and real-account
query pages. Research, screening, backtesting, watchlist, manual portfolio,
tail-end screening, and expectation planning are kept in-app.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from io import StringIO
from typing import Any, Callable

import pandas as pd
import streamlit as st

from ai_advisor import AIAdvisor
from analysis import Analyzer
from backtest import BacktestEngine
from config import BACKTEST, LONG_TERM, RISK, SCREEN, STRATEGY as SCREEN_STRATEGY
from data_feed import DataFeed
from expectation_planner import ExpectationPlanner
from fundamental import LongTermFundamentalScreener
from portfolio_manager import PortfolioManager
from screener import StockScreener
from screener_tail import TailEndScreener
from strategy import StrategyEngine
from watchlist import watchlist


st.set_page_config(
    page_title="A股研究与预期计划",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)


def inject_css() -> None:
    st.markdown(
        """
<style>
  .block-container { max-width: 1540px; padding-top: 1.1rem; padding-bottom: 2rem; }
  [data-testid="stMetricValue"] { font-size: 1.45rem; }
  .codex-card {
    border: 1px solid rgba(148, 163, 184, .22);
    background: rgba(15, 23, 42, .48);
    border-radius: 8px;
    padding: 14px 16px;
    margin: 8px 0 12px;
  }
  .codex-title { font-size: 1.05rem; font-weight: 700; margin-bottom: 6px; }
  .codex-muted { color: #94a3b8; font-size: .88rem; line-height: 1.55; }
  .status-ok { color: #22c55e; font-weight: 700; }
  .status-warn { color: #f59e0b; font-weight: 700; }
  .status-bad { color: #ef4444; font-weight: 700; }
  .small-note { font-size: .84rem; color: #94a3b8; }
</style>
""",
        unsafe_allow_html=True,
    )


@st.cache_resource(show_spinner=False)
def services():
    data_feed = DataFeed()
    portfolio_manager = PortfolioManager(data_feed)
    tail_screener = TailEndScreener(data_feed)
    expectation_planner = ExpectationPlanner(data_feed)
    technical_screener = StockScreener(data_feed)
    fundamental_screener = LongTermFundamentalScreener(data_feed)
    ai_advisor = AIAdvisor()
    ai_advisor.df = data_feed
    analyzer = Analyzer()
    return {
        "data": data_feed,
        "portfolio": portfolio_manager,
        "tail": tail_screener,
        "planner": expectation_planner,
        "technical": technical_screener,
        "fundamental": fundamental_screener,
        "ai": ai_advisor,
        "analyzer": analyzer,
    }


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def valid_code(code: str) -> bool:
    return bool(re.fullmatch(r"\d{6}", str(code).strip()))


def run_or_warn(label: str, fallback: Any, fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as exc:
        st.warning(f"{label}暂不可用：{exc}")
        return fallback


def get_secret(name: str) -> str:
    try:
        return str(st.secrets.get(name, "") or "")
    except Exception:
        return ""


def configure_ai(advisor: AIAdvisor) -> None:
    key = (
        st.session_state.get("deepseek_api_key")
        or get_secret("DEEPSEEK_API_KEY")
        or os.environ.get("DEEPSEEK_API_KEY", "")
    )
    advisor.api_key = key


def clean_value(value: Any) -> Any:
    if isinstance(value, (list, tuple, set)):
        return "、".join(str(v) for v in value if v not in (None, ""))
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return value


def display_frame(rows: Any, columns: list[str], names: dict[str, str]) -> pd.DataFrame:
    if isinstance(rows, pd.DataFrame):
        frame = rows.copy()
    else:
        frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    present = [column for column in columns if column in frame.columns]
    frame = frame[present].copy()
    for column in frame.columns:
        frame[column] = frame[column].map(clean_value)
    return frame.rename(columns=names)


def render_table(rows: Any, columns: list[str], names: dict[str, str], height: int | None = None) -> pd.DataFrame:
    frame = display_frame(rows, columns, names)
    if frame.empty:
        st.info("暂无数据。")
    else:
        st.dataframe(frame, use_container_width=True, hide_index=True, height=height)
    return frame


def metric_cards(items: list[tuple[str, Any, Any | None]]) -> None:
    cols = st.columns(len(items))
    for col, (label, value, delta) in zip(cols, items):
        col.metric(label, value, delta)


def selected_core_stocks(fundamental_screener: LongTermFundamentalScreener) -> list[dict]:
    candidates = (
        st.session_state.get("fundamental_recommendations")
        or fundamental_screener.recommendations
        or []
    )
    return [{
        "code": str(item.get("code", "")).zfill(6),
        "name": str(item.get("name", "")),
        "rank": item.get("recommendation_rank") or item.get("rank"),
        "selection_score": item.get("selection_score") or item.get("composite_score"),
    } for item in candidates if valid_code(str(item.get("code", "")))]


def normalise_days(value: Any, default: int = 250) -> int:
    return min(800, max(60, safe_int(value, default)))


def strategy_params_from_controls(prefix: str, defaults: dict | None = None) -> dict:
    defaults = defaults or StrategyEngine().get_config()
    with st.expander("策略参数", expanded=False):
        c1, c2, c3 = st.columns(3)
        stop_loss = c1.slider(
            "止损%",
            min_value=-20.0,
            max_value=-0.5,
            value=float(defaults["stop_loss"] * 100),
            step=0.5,
            key=f"{prefix}_stop_loss",
        )
        take_profit = c2.slider(
            "止盈%",
            min_value=0.5,
            max_value=50.0,
            value=float(defaults["take_profit"] * 100),
            step=0.5,
            key=f"{prefix}_take_profit",
        )
        position = c3.slider(
            "单票仓位%",
            min_value=1.0,
            max_value=50.0,
            value=float(defaults["position_size_pct"] * 100),
            step=1.0,
            key=f"{prefix}_position",
        )
        c4, c5, c6 = st.columns(3)
        trail_activation = c4.slider(
            "移动止损激活%",
            min_value=0.5,
            max_value=50.0,
            value=float(defaults["trailing_activation"] * 100),
            step=0.5,
            key=f"{prefix}_trail_activation",
        )
        trail_distance = c5.slider(
            "移动回撤%",
            min_value=0.5,
            max_value=20.0,
            value=float(defaults["trailing_distance"] * 100),
            step=0.5,
            key=f"{prefix}_trail_distance",
        )
        max_hold = c6.number_input(
            "最长持仓天数",
            min_value=3,
            max_value=120,
            value=int(defaults["max_hold_days"]),
            key=f"{prefix}_max_hold",
        )
        min_signals = st.slider(
            "最少信号类别数",
            min_value=1,
            max_value=4,
            value=int(defaults["min_signals"]),
            key=f"{prefix}_min_signals",
        )
    return {
        "stop_loss": stop_loss / 100,
        "take_profit": take_profit / 100,
        "position_size_pct": position / 100,
        "trailing_activation": trail_activation / 100,
        "trailing_distance": trail_distance / 100,
        "max_hold_days": int(max_hold),
        "min_signals": int(min_signals),
    }


def run_single_backtest(data_feed: DataFeed, code: str, days: int, parameters: dict) -> dict | None:
    name = ""
    stocks = data_feed.get_stock_list()
    if not stocks.empty:
        match = stocks[stocks["code"] == code]
        if not match.empty:
            name = str(match.iloc[0].get("name", ""))
    engine = BacktestEngine(
        strategy=StrategyEngine(parameters),
        data_feed=data_feed,
    )
    return engine.run(code, name=name, days=days, with_benchmark=True)


def batch_backtest(data_feed: DataFeed, candidates: list[dict], days: int,
                   parameters: dict | None = None) -> dict:
    rows = []
    missing = []
    for candidate in candidates:
        code = str(candidate["code"]).zfill(6)
        result = run_single_backtest(data_feed, code, days, parameters or {})
        if not result:
            missing.append(code)
            continue
        rows.append({
            "code": code,
            "name": candidate.get("name", ""),
            "rank": candidate.get("rank"),
            "total_return": result["total_return"],
            "benchmark_return": result.get("benchmark_return", 0),
            "excess_return": result.get("excess_return", result["total_return"]),
            "max_drawdown": result["max_drawdown"],
            "win_rate": result["win_rate"],
            "sharpe_ratio": result["sharpe_ratio"],
            "profit_loss_ratio": result["profit_loss_ratio"],
            "trade_count": result["trade_count"],
        })
    if not rows:
        raise ValueError("核心股历史K线不足或数据源不可用")

    avg_return = round(sum(float(row["total_return"]) for row in rows) / len(rows), 2)
    avg_hold = round(sum(float(row["benchmark_return"]) for row in rows) / len(rows), 2)
    avg_excess = round(sum(float(row["excess_return"]) for row in rows) / len(rows), 2)
    avg_drawdown = round(sum(float(row["max_drawdown"]) for row in rows) / len(rows), 2)
    avg_win_rate = round(sum(float(row["win_rate"]) for row in rows) / len(rows), 1)
    total_trades = sum(int(row["trade_count"]) for row in rows)
    outperformed = sum(1 for row in rows if float(row["excess_return"]) > 0)
    objective = round(avg_excess - abs(avg_drawdown) * 0.35 + outperformed * 0.30, 2)
    return {
        "parameters": StrategyEngine(parameters or {}).get_config(),
        "days": days,
        "stock_count": len(rows),
        "missing_codes": missing,
        "summary": {
            "avg_return": avg_return,
            "avg_buy_hold_return": avg_hold,
            "avg_excess_return": avg_excess,
            "avg_max_drawdown": avg_drawdown,
            "avg_win_rate": avg_win_rate,
            "total_trades": total_trades,
            "outperformed_count": outperformed,
            "objective_score": objective,
        },
        "results": rows,
    }


def render_backtest_result(result: dict) -> None:
    if not result:
        st.info("暂无回测结果。")
        return
    metric_cards([
        ("总收益", f"{result.get('total_return', 0):.2f}%", f"持有 {result.get('benchmark_return', 0):.2f}%"),
        ("超额收益", f"{result.get('excess_return', 0):.2f}%", None),
        ("最大回撤", f"{result.get('max_drawdown', 0):.2f}%", None),
        ("胜率", f"{result.get('win_rate', 0):.1f}%", None),
        ("夏普", result.get("sharpe_ratio", 0), None),
        ("交易数", result.get("trade_count", 0), None),
    ])
    curve = pd.DataFrame(result.get("equity_curve", []))
    benchmark = pd.DataFrame(result.get("benchmark_curve", []))
    if not curve.empty:
        chart = pd.DataFrame({
            "日期": curve["date"],
            "策略净值": curve["equity"] / float(result.get("initial_capital", 1)) * 100,
        })
        if not benchmark.empty:
            chart["买入持有"] = benchmark["value"].reindex(chart.index).values
        st.line_chart(chart.set_index("日期"))

    trades = result.get("trades", [])
    if trades:
        render_table(trades, [
            "买入日期", "卖出日期", "代码", "名称", "方向", "买入价", "卖出价",
            "数量", "盈亏", "盈亏%", "策略", "入场信号",
        ], {
            "买入日期": "买入日期", "卖出日期": "卖出日期", "代码": "代码", "名称": "名称",
            "方向": "方向", "买入价": "买入价", "卖出价": "卖出价", "数量": "数量",
            "盈亏": "盈亏", "盈亏%": "盈亏%", "策略": "退出原因", "入场信号": "入场信号",
        }, height=320)


def save_expectation_from_candidate(candidate: dict, planner: ExpectationPlanner,
                                    source: str) -> None:
    code = str(candidate.get("code", "")).zfill(6)
    price = safe_float(
        candidate.get("price")
        or candidate.get("today_price")
        or candidate.get("close")
        or candidate.get("current_price")
    )
    if not valid_code(code) or price <= 0:
        st.error("候选缺少有效代码或价格，不能生成预期计划。")
        return
    planner.save_plan({
        "code": code,
        "name": candidate.get("name", ""),
        "source": source,
        "thesis_type": "系统候选",
        "thesis": "来自系统筛选结果，只在价格接近计划位且资金/技术信号配合时执行",
        "trigger": "当前价接近计划买入价；K线结构不破坏；主力资金不明显流出",
        "invalidation": "跌破止损价；当前价较计划价上偏超过3%；板块资金转弱",
        "planned_price": round(price, 2),
        "stop_loss": round(price * 0.95, 2),
        "take_profit": round(price * 1.10, 2),
        "position_pct": 5,
        "horizon": "T+1至5日",
        "review_note": "复盘是否按计划执行，是否出现追涨、重仓或无视失效条件",
    })
    st.success(f"已为 {code} 生成预期计划。")


def page_market(data_feed: DataFeed) -> None:
    st.title("市场总览")
    st.caption("行情快照、涨跌分布、资金热点和 ETF 池。")

    force_refresh = st.button("刷新全市场快照", use_container_width=False)
    stocks = run_or_warn("市场快照", pd.DataFrame(), lambda: data_feed.get_stock_list(force_refresh=force_refresh))
    metrics = run_or_warn("市场情绪", {}, data_feed.get_market_metrics)
    metric_cards([
        ("股票数", metrics.get("total", len(stocks) if not stocks.empty else 0), None),
        ("上涨", metrics.get("up", 0), None),
        ("下跌", metrics.get("down", 0), None),
        ("涨跌比", metrics.get("advance_ratio", 0), None),
        ("涨停/跌停", f"{metrics.get('limit_up', 0)}/{metrics.get('limit_down', 0)}", None),
        ("情绪", metrics.get("mood", "-"), None),
    ])

    if not stocks.empty:
        cols = ["code", "name", "price", "change_pct", "turnover_rate", "amount", "market_cap", "main_net_pct"]
        names = {
            "code": "代码", "name": "名称", "price": "现价", "change_pct": "涨跌幅%",
            "turnover_rate": "换手率%", "amount": "成交额", "market_cap": "市值(亿)",
            "main_net_pct": "主力净占比%",
        }
        left, right = st.columns(2)
        with left:
            st.subheader("涨幅前列")
            render_table(stocks.sort_values("change_pct", ascending=False).head(20), cols, names, height=420)
        with right:
            st.subheader("跌幅前列")
            render_table(stocks.sort_values("change_pct", ascending=True).head(20), cols, names, height=420)

    st.subheader("资金热点")
    sector, concept = st.columns(2)
    with sector:
        sectors = run_or_warn("行业资金", pd.DataFrame(), lambda: data_feed.get_sector_fund_flow(30))
        render_table(sectors, ["name", "change_pct", "main_net_inflow", "main_net_pct", "flow_score"], {
            "name": "行业", "change_pct": "涨跌幅%", "main_net_inflow": "主力净流入(亿)",
            "main_net_pct": "主力净占比%", "flow_score": "资金分",
        }, height=420)
    with concept:
        concepts = run_or_warn("概念资金", pd.DataFrame(), lambda: data_feed.get_concept_fund_flow(30))
        render_table(concepts, ["name", "change_pct", "main_net_inflow", "main_net_pct", "flow_score"], {
            "name": "概念", "change_pct": "涨跌幅%", "main_net_inflow": "主力净流入(亿)",
            "main_net_pct": "主力净占比%", "flow_score": "资金分",
        }, height=420)

    with st.expander("ETF 池", expanded=False):
        etfs = run_or_warn("ETF列表", pd.DataFrame(), data_feed.get_etf_list)
        render_table(etfs, ["code", "name", "price", "change_pct", "amount", "turnover"], {
            "code": "代码", "name": "名称", "price": "现价", "change_pct": "涨跌幅%",
            "amount": "成交额(亿)", "turnover": "换手率%",
        }, height=360)

    with st.expander("数据源状态", expanded=False):
        st.json(data_feed.get_source_status(), expanded=False)


def page_screening(data_feed: DataFeed, technical_screener: StockScreener,
                   fundamental_screener: LongTermFundamentalScreener,
                   planner: ExpectationPlanner) -> None:
    st.title("选股中心")
    st.caption("保留原网站的中长期全面选股与短线技术选股。筛选结果可加入自选或生成预期计划。")

    tab_fund, tab_tech, tab_actions = st.tabs(["全面选股", "技术短线", "候选操作"])

    with tab_fund:
        st.markdown('<div class="codex-card"><div class="codex-title">中长期全面选股</div><div class="codex-muted">先按流动性与交易边界建立股票池，再逐股拉取财务指标，合格后叠加技术与板块资金，最终生成核心观察股。</div></div>', unsafe_allow_html=True)
        c1, c2, c3, c4 = st.columns(4)
        scan_all = c1.checkbox("扫描全部初筛池", value=False)
        diagnostic_limit = c2.number_input("诊断扫描数量", min_value=20, max_value=1500, value=160, step=20)
        min_score = c3.slider("基本面最低分", 0, 100, int(LONG_TERM["minimum_score"]))
        use_ai_summary = c4.checkbox("AI补充摘要", value=False)
        if st.button("运行全面选股", type="primary", use_container_width=True):
            LONG_TERM["minimum_score"] = int(min_score)
            limit = None if scan_all else int(diagnostic_limit)
            with st.spinner("正在执行全面选股。全量扫描会逐股请求财务数据，耗时较长..."):
                results = run_or_warn(
                    "全面选股",
                    [],
                    lambda: fundamental_screener.screen(universe_limit=limit),
                )
                recommendations = list(fundamental_screener.recommendations)
                if use_ai_summary:
                    advisor = services()["ai"]
                    configure_ai(advisor)
                    if advisor.is_configured:
                        recommendations = advisor.explain_long_term_candidates(recommendations)
                    else:
                        st.warning("未配置 DeepSeek API Key，已跳过 AI 摘要。")
                st.session_state["fundamental_results"] = results
                st.session_state["fundamental_summary"] = dict(fundamental_screener.summary)
                st.session_state["fundamental_recommendations"] = recommendations
            st.success("全面选股完成。")

        summary = st.session_state.get("fundamental_summary") or fundamental_screener.summary or {}
        if summary:
            metric_cards([
                ("初筛池", summary.get("pool_count", 0), None),
                ("有效财报", summary.get("financial_success_count", 0), None),
                ("综合候选", summary.get("fundamental_qualified_count", 0), None),
                ("精选", summary.get("selected_count", 0), None),
                ("周期", summary.get("holding_horizon", "-"), None),
            ])
            boards = summary.get("rotation_boards") or []
            if boards:
                with st.expander("资金轮动匹配板块", expanded=False):
                    render_table(boards, ["name", "type", "change_pct", "flow_score", "main_net_inflow", "recent_main_net_inflow"], {
                        "name": "板块", "type": "类型", "change_pct": "涨跌幅%", "flow_score": "资金分",
                        "main_net_inflow": "今日净流入(亿)", "recent_main_net_inflow": "近期净流入(亿)",
                    })

        recommendations = st.session_state.get("fundamental_recommendations") or fundamental_screener.recommendations
        if recommendations:
            st.subheader("精选核心股")
            render_table(recommendations, [
                "recommendation_rank", "code", "name", "price", "selection_score", "composite_score",
                "fundamental_score", "technical_score", "market_flow_score", "annualized_roe",
                "revenue_growth", "profit_growth", "pe", "pb", "matched_themes", "fundamental_reason",
                "technical_reason", "ai_summary", "risk", "ai_risk_note",
            ], {
                "recommendation_rank": "精选排名", "code": "代码", "name": "名称", "price": "价格",
                "selection_score": "精选分", "composite_score": "综合分", "fundamental_score": "基本面分",
                "technical_score": "技术分", "market_flow_score": "资金分", "annualized_roe": "年化ROE%",
                "revenue_growth": "营收同比%", "profit_growth": "净利同比%", "pe": "PE", "pb": "PB",
                "matched_themes": "资金匹配", "fundamental_reason": "基本面依据",
                "technical_reason": "技术依据", "ai_summary": "AI摘要", "risk": "规则风险",
                "ai_risk_note": "AI风险",
            }, height=420)

        results = st.session_state.get("fundamental_results") or []
        if results:
            with st.expander("全部综合候选", expanded=False):
                render_table(results, [
                    "code", "name", "price", "market_cap", "report_date", "fundamental_score",
                    "technical_score", "composite_score", "annualized_roe", "revenue_growth",
                    "profit_growth", "operating_cf_per_share", "pe", "pb", "risk",
                ], {
                    "code": "代码", "name": "名称", "price": "价格", "market_cap": "市值(亿)",
                    "report_date": "报告期", "fundamental_score": "基本面分", "technical_score": "技术分",
                    "composite_score": "综合分", "annualized_roe": "年化ROE%", "revenue_growth": "营收同比%",
                    "profit_growth": "净利同比%", "operating_cf_per_share": "经营现金流/股",
                    "pe": "PE", "pb": "PB", "risk": "风险",
                }, height=520)

    with tab_tech:
        st.markdown('<div class="codex-card"><div class="codex-title">短线技术选股</div><div class="codex-muted">基于放量突破、均线、MACD、KDJ、量价配合进行全市场技术评分。</div></div>', unsafe_allow_html=True)
        f1, f2, f3, f4 = st.columns(4)
        SCREEN["market_cap_min"] = f1.number_input("最小市值(亿)", value=float(SCREEN["market_cap_min"]))
        SCREEN["market_cap_max"] = f2.number_input("最大市值(亿)", value=float(SCREEN["market_cap_max"]))
        SCREEN["turnover_min"] = f3.number_input("最小换手率%", value=float(SCREEN["turnover_min"]))
        SCREEN["turnover_max"] = f4.number_input("最大换手率%", value=float(SCREEN["turnover_max"]))
        f5, f6, f7 = st.columns(3)
        SCREEN_STRATEGY["score_threshold"] = f5.slider("评分阈值", 0, 100, int(SCREEN_STRATEGY["score_threshold"]))
        SCREEN_STRATEGY["max_stocks"] = f6.number_input("最多返回", min_value=5, max_value=80, value=int(SCREEN_STRATEGY["max_stocks"]))
        workers = f7.slider("并发数", 1, 10, 6)
        if st.button("运行技术选股", type="primary", use_container_width=True):
            with st.spinner("正在执行技术选股..."):
                result = run_or_warn("技术选股", pd.DataFrame(), lambda: technical_screener.screen(max_workers=workers))
                st.session_state["technical_screen_result"] = result
            st.success(f"技术选股完成，返回 {0 if result.empty else len(result)} 只。")

        tech_result = st.session_state.get("technical_screen_result", pd.DataFrame())
        if isinstance(tech_result, pd.DataFrame) and not tech_result.empty:
            render_table(tech_result, [
                "code", "name", "price", "change_pct", "turnover_rate", "market_cap", "board", "pe",
                "volume_ratio", "main_net", "综合评分", "放量突破", "均线金叉", "MACD", "KDJ",
                "量价配合", "RSI", "量比",
            ], {
                "code": "代码", "name": "名称", "price": "价格", "change_pct": "涨跌幅%",
                "turnover_rate": "换手率%", "market_cap": "市值(亿)", "board": "板块",
                "pe": "PE", "volume_ratio": "行情量比", "main_net": "主力净额",
                "综合评分": "综合评分", "放量突破": "放量突破", "均线金叉": "均线金叉",
                "MACD": "MACD", "KDJ": "KDJ", "量价配合": "量价配合", "RSI": "RSI", "量比": "K线量比",
            }, height=520)

    with tab_actions:
        st.subheader("候选操作")
        pool = []
        for item in st.session_state.get("fundamental_recommendations", []) or []:
            pool.append({"source": "全面选股", **item})
        tech_result = st.session_state.get("technical_screen_result", pd.DataFrame())
        if isinstance(tech_result, pd.DataFrame) and not tech_result.empty:
            pool.extend({"source": "技术选股", **row} for row in tech_result.to_dict("records"))
        if not pool:
            st.info("先运行全面选股或技术选股，再对候选做操作。")
            return
        labels = []
        for item in pool:
            score = item.get("selection_score")
            if score in (None, ""):
                score = item.get("综合评分")
            if score in (None, ""):
                score = item.get("composite_score", "")
            labels.append(
                f"{item.get('code')} {item.get('name', '')} - {item.get('source')} - 分数 {score}"
            )
        selected = st.selectbox("选择候选", labels)
        item = pool[labels.index(selected)]
        c1, c2, c3 = st.columns(3)
        if c1.button("加入自选", use_container_width=True):
            watchlist.add(str(item["code"]).zfill(6))
            st.success("已加入自选。")
        if c2.button("生成预期计划", use_container_width=True):
            save_expectation_from_candidate(item, planner, item.get("source", "系统候选"))
        if c3.button("加载到个股研究", use_container_width=True):
            st.session_state["research_code"] = str(item["code"]).zfill(6)
            st.success("已写入个股研究代码。")


def page_research(data_feed: DataFeed, advisor: AIAdvisor) -> None:
    st.title("个股研究")
    st.caption("查看行情、K线指标、资金流、策略信号，并可调用 AI 做单股研究摘要。")
    code = st.text_input("股票代码", value=st.session_state.get("research_code", "600519"), max_chars=6)
    c1, c2, c3 = st.columns(3)
    period = c1.selectbox("周期", ["day", "week", "month"], index=0)
    count = c2.number_input("K线数量", min_value=60, max_value=800, value=180, step=20)
    run = c3.button("加载个股", type="primary", use_container_width=True)

    if run:
        st.session_state["research_code"] = code.strip().zfill(6)
    code = st.session_state.get("research_code", code).strip().zfill(6)
    if not valid_code(code):
        st.error("请输入6位股票代码。")
        return

    with st.spinner("加载个股数据..."):
        quotes = run_or_warn("即时行情", pd.DataFrame(), lambda: data_feed.get_realtime_quotes([code]))
        kline = run_or_warn("K线", pd.DataFrame(), lambda: data_feed.get_kline(code, period=period, count=int(count)))
        flow = run_or_warn("资金流", {}, lambda: data_feed.get_fund_flow(code))
        detail = run_or_warn("技术评分", None, lambda: StockScreener(data_feed).screen_with_detail(code))

    if not quotes.empty:
        quote = quotes.iloc[0].to_dict()
        metric_cards([
            ("名称", quote.get("name", ""), None),
            ("现价", round(safe_float(quote.get("price")), 2), f"{safe_float(quote.get('change_pct')):+.2f}%"),
            ("成交额", round(safe_float(quote.get("amount")) / 1e8, 2), "亿"),
            ("昨收", quote.get("close_yest", "-"), None),
        ])

    if not kline.empty:
        chart_cols = ["close"]
        for column in ["MA5", "MA10", "MA20", "MA60"]:
            if column in kline.columns:
                chart_cols.append(column)
        chart = kline.copy()
        chart["date"] = chart["date"].astype(str).str.slice(0, 10)
        st.line_chart(chart.set_index("date")[chart_cols])
        render_table(kline.tail(30), [
            "date", "open", "high", "low", "close", "volume", "MA5", "MA10", "MA20",
            "MACD_DIF", "MACD_DEA", "RSI", "KDJ_K", "KDJ_D",
        ], {
            "date": "日期", "open": "开", "high": "高", "low": "低", "close": "收",
            "volume": "量", "MA5": "MA5", "MA10": "MA10", "MA20": "MA20",
            "MACD_DIF": "DIF", "MACD_DEA": "DEA", "RSI": "RSI", "KDJ_K": "K", "KDJ_D": "D",
        }, height=360)

    col_left, col_right = st.columns(2)
    with col_left:
        st.subheader("策略信号")
        if detail:
            st.write("；".join(detail.get("signals", [])) or "暂无明显买入信号")
            st.json({k: v for k, v in detail.items() if k not in ("signals",)}, expanded=False)
        else:
            st.info("技术评分不可用。")
    with col_right:
        st.subheader("资金流")
        st.json(flow or {"available": False}, expanded=False)

    configure_ai(advisor)
    if st.button("AI 分析这只股票", use_container_width=True):
        if not advisor.is_configured:
            st.warning("请先在侧边栏配置 DeepSeek API Key 或 Streamlit Secrets。")
        else:
            with st.spinner("AI正在分析个股..."):
                result = run_or_warn("AI个股分析", {}, lambda: advisor.analyze_stock(code))
            st.markdown(result.get("response") or result.get("error") or "无响应")


def build_investment_advice(candidates: list[dict], advisor: AIAdvisor,
                            use_ai: bool) -> dict:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not candidates:
        return {
            "summary": "请先在选股中心运行全面选股，产生精选标的后再生成建议。",
            "market_mood": "待选股",
            "stocks": [],
            "risk_alerts": ["未生成精选标的，不能形成仓位建议。"],
            "total_position": 0,
            "analysis_mode": "等待选股",
            "timestamp": timestamp,
        }
    enriched = [dict(item) for item in candidates]
    mode = "规则精选建议"
    if use_ai and advisor.is_configured:
        enriched = advisor.explain_long_term_candidates(enriched)
        mode = "DeepSeek摘要 + 规则精选"
    position = round(min(
        RISK["position_pct"] * 100,
        RISK["max_exposure"] * 100 / max(len(enriched), 1),
    ), 1)
    stocks = []
    risk_alerts = []
    for item in enriched:
        reason = "；".join(part for part in (
            item.get("fundamental_reason", ""),
            item.get("technical_reason", ""),
            "资金匹配: " + "、".join(item.get("matched_themes", [])) if item.get("matched_themes") else "",
            item.get("ai_summary", ""),
        ) if part)
        stocks.append({
            "code": item.get("code", ""),
            "name": item.get("name", ""),
            "score": item.get("selection_score", item.get("composite_score", 0)),
            "weight": position,
            "reason": reason or "通过综合规则筛选，等待入场信号确认。",
        })
        risk = item.get("ai_risk_note") or item.get("risk")
        if risk:
            risk_alerts.append(f"{item.get('name') or item.get('code')}: {risk}")
    return {
        "summary": f"基于全面选股结果，列出 {len(stocks)} 只观察标的；入场仍须满足策略信号。",
        "market_mood": "以资金与风险阈值为准",
        "stocks": stocks,
        "risk_alerts": risk_alerts[:10] or ["仍需核验公告、成交容量与止损执行条件。"],
        "total_position": round(position * len(stocks), 1),
        "analysis_mode": mode,
        "timestamp": timestamp,
    }


def page_ai(data_feed: DataFeed, advisor: AIAdvisor,
            fundamental_screener: LongTermFundamentalScreener) -> None:
    st.title("AI 研究助手")
    st.caption("AI 只做摘要和研究辅助，不替代固定规则选股评分。")
    configure_ai(advisor)
    st.info("AI状态：" + ("已配置" if advisor.is_configured else "未配置 DeepSeek API Key"))

    tab_advice, tab_market, tab_chat = st.tabs(["精选建议", "大盘分析", "自由问答"])
    with tab_advice:
        candidates = st.session_state.get("fundamental_recommendations") or fundamental_screener.recommendations
        use_ai = st.checkbox("使用 DeepSeek 补充摘要", value=advisor.is_configured)
        if st.button("生成投资研究建议", type="primary", use_container_width=True):
            with st.spinner("生成建议..."):
                advice = build_investment_advice(candidates, advisor, use_ai)
                st.session_state["investment_advice"] = advice
        advice = st.session_state.get("investment_advice")
        if advice:
            metric_cards([
                ("分析模式", advice.get("analysis_mode", "-"), None),
                ("建议总仓位", f"{advice.get('total_position', 0)}%", None),
                ("时间", advice.get("timestamp", "-"), None),
            ])
            st.write(advice.get("summary", ""))
            render_table(advice.get("stocks", []), ["code", "name", "score", "weight", "reason"], {
                "code": "代码", "name": "名称", "score": "评分", "weight": "建议仓位%", "reason": "依据",
            })
            st.warning("；".join(advice.get("risk_alerts", [])))

    with tab_market:
        if st.button("AI分析当前市场", type="primary", use_container_width=True):
            if not advisor.is_configured:
                st.warning("请先配置 DeepSeek API Key。")
            else:
                with st.spinner("正在拉取市场上下文并调用 AI..."):
                    result = run_or_warn("AI市场分析", {}, advisor.analyze_market)
                st.session_state["ai_market_result"] = result
        result = st.session_state.get("ai_market_result")
        if result:
            st.markdown(result.get("response") or result.get("error") or "无响应")
            if result.get("context_summary"):
                st.json(result["context_summary"], expanded=False)

    with tab_chat:
        default_question = "请结合当前市场资金和已披露财务数据，说明今天适合观察哪些方向，风险是什么？"
        question = st.text_area("问题", value=default_question, height=110)
        if st.button("发送给 AI", type="primary", use_container_width=True):
            if not advisor.is_configured:
                st.warning("请先配置 DeepSeek API Key。")
            else:
                with st.spinner("AI正在回答..."):
                    result = run_or_warn("AI问答", {}, lambda: advisor.chat_with_context(question))
                st.session_state["ai_chat_result"] = result
        result = st.session_state.get("ai_chat_result")
        if result:
            st.markdown(result.get("response") or result.get("error") or "无响应")


def page_backtest(data_feed: DataFeed, advisor: AIAdvisor,
                  fundamental_screener: LongTermFundamentalScreener) -> None:
    st.title("策略回测")
    st.caption("支持单股回测、全面选股核心股批量回测，以及可选 AI 参数优化。")

    tab_single, tab_core, tab_opt = st.tabs(["单股回测", "核心股批测", "AI参数优化"])
    with tab_single:
        code = st.text_input("回测股票代码", value=st.session_state.get("research_code", "600519"), max_chars=6)
        days = st.number_input("回测交易日", min_value=60, max_value=800, value=250, step=10)
        params = strategy_params_from_controls("single_bt")
        if st.button("运行单股回测", type="primary", use_container_width=True):
            code = code.strip().zfill(6)
            if not valid_code(code):
                st.error("股票代码必须是6位数字。")
            else:
                with st.spinner("正在回测..."):
                    result = run_or_warn("单股回测", None, lambda: run_single_backtest(data_feed, code, int(days), params))
                st.session_state["single_backtest_result"] = result
        render_backtest_result(st.session_state.get("single_backtest_result"))

    with tab_core:
        candidates = selected_core_stocks(fundamental_screener)
        st.write(f"当前核心股数量：{len(candidates)}")
        if candidates:
            render_table(candidates, ["rank", "code", "name", "selection_score"], {
                "rank": "排名", "code": "代码", "name": "名称", "selection_score": "精选分",
            }, height=220)
        days = st.number_input("批测交易日", min_value=60, max_value=800, value=250, step=10, key="core_days")
        params = strategy_params_from_controls("core_bt")
        if st.button("一键回测核心股", type="primary", use_container_width=True):
            if not candidates:
                st.warning("请先在选股中心运行全面选股。")
            else:
                with st.spinner("正在批量回测核心股..."):
                    result = run_or_warn("核心股批测", {}, lambda: batch_backtest(data_feed, candidates, int(days), params))
                st.session_state["core_backtest_result"] = result
        result = st.session_state.get("core_backtest_result")
        if result:
            summary = result.get("summary", {})
            metric_cards([
                ("平均策略收益", f"{summary.get('avg_return', 0):.2f}%", None),
                ("平均买入持有", f"{summary.get('avg_buy_hold_return', 0):.2f}%", None),
                ("平均超额", f"{summary.get('avg_excess_return', 0):.2f}%", None),
                ("平均回撤", f"{summary.get('avg_max_drawdown', 0):.2f}%", None),
                ("胜率", f"{summary.get('avg_win_rate', 0):.1f}%", None),
                ("目标分", summary.get("objective_score", 0), None),
            ])
            render_table(result.get("results", []), [
                "rank", "code", "name", "total_return", "benchmark_return", "excess_return",
                "max_drawdown", "win_rate", "sharpe_ratio", "trade_count",
            ], {
                "rank": "排名", "code": "代码", "name": "名称", "total_return": "策略收益%",
                "benchmark_return": "买入持有%", "excess_return": "超额%", "max_drawdown": "最大回撤%",
                "win_rate": "胜率%", "sharpe_ratio": "夏普", "trade_count": "交易数",
            }, height=360)

    with tab_opt:
        configure_ai(advisor)
        candidates = selected_core_stocks(fundamental_screener)
        rounds = st.slider("AI优化轮数", 1, 5, 2)
        days = st.number_input("优化回测交易日", min_value=60, max_value=800, value=250, step=10, key="opt_days")
        base_params = strategy_params_from_controls("opt_bt")
        if st.button("启动 AI 参数优化", type="primary", use_container_width=True):
            if not candidates:
                st.warning("请先在选股中心运行全面选股。")
            elif not advisor.is_configured:
                st.warning("请先配置 DeepSeek API Key。")
            else:
                trials = []
                with st.spinner("正在跑基线和 AI 参数候选..."):
                    best = batch_backtest(data_feed, candidates, int(days), base_params)
                    trials.append({"round": 0, "label": "当前参数", "parameters": best["parameters"], "summary": best["summary"]})
                    for round_no in range(1, int(rounds) + 1):
                        proposal = advisor.suggest_backtest_parameters(best["parameters"], best["summary"], trials)
                        candidate_params = proposal.get("parameters") or best["parameters"]
                        tested = batch_backtest(data_feed, candidates, int(days), candidate_params)
                        tested["label"] = proposal.get("rationale", f"AI候选 {round_no}")
                        trials.append({
                            "round": round_no,
                            "label": tested["label"],
                            "parameters": tested["parameters"],
                            "summary": tested["summary"],
                        })
                        if tested["summary"]["objective_score"] > best["summary"]["objective_score"]:
                            best = tested
                st.session_state["ai_optimization"] = {"best": best, "trials": trials}
        result = st.session_state.get("ai_optimization")
        if result:
            st.subheader("优化轮次")
            render_table(result.get("trials", []), ["round", "label", "parameters", "summary"], {
                "round": "轮次", "label": "说明", "parameters": "参数", "summary": "结果",
            }, height=360)
            st.subheader("当前最优")
            st.json(result["best"]["parameters"], expanded=False)
            st.json(result["best"]["summary"], expanded=False)


def page_watchlist(data_feed: DataFeed) -> None:
    st.title("自选股")
    c1, c2 = st.columns([3, 1])
    code = c1.text_input("添加股票代码", placeholder="例如 600519", max_chars=6)
    if c2.button("添加", type="primary", use_container_width=True):
        if valid_code(code):
            watchlist.add(code.strip().zfill(6))
            st.rerun()
        else:
            st.error("股票代码必须是6位数字。")
    codes = list(watchlist.codes)
    if not codes:
        st.info("暂无自选股。")
        return
    quotes = run_or_warn("自选行情", pd.DataFrame(), lambda: data_feed.get_realtime_quotes(codes))
    render_table(quotes if not quotes.empty else [{"code": c} for c in codes], [
        "code", "name", "price", "change_pct", "amount", "volume",
    ], {
        "code": "代码", "name": "名称", "price": "现价", "change_pct": "涨跌幅%",
        "amount": "成交额", "volume": "成交量",
    }, height=420)
    for item in codes:
        row = st.columns([4, 1, 1])
        row[0].write(item)
        if row[1].button("研究", key=f"research-{item}", use_container_width=True):
            st.session_state["research_code"] = item
            st.success("已写入个股研究。")
        if row[2].button("移除", key=f"remove-{item}", use_container_width=True):
            watchlist.remove(item)
            st.rerun()


def load_portfolio(portfolio_manager: PortfolioManager) -> dict:
    raw = run_or_warn("持仓文件", {"capital": 100000, "positions": []}, portfolio_manager.load)
    return run_or_warn(
        "持仓诊断",
        {"capital": raw.get("capital", 100000), "positions": [], "summary": {}},
        lambda: portfolio_manager.enrich(raw),
    )


def page_portfolio(portfolio_manager: PortfolioManager, advisor: AIAdvisor,
                   tail_screener: TailEndScreener) -> None:
    st.title("持仓诊断")
    st.caption("云端版为手动持仓记录和研究诊断，不连接真实券商账户。")
    raw = run_or_warn("持仓文件", {"capital": 100000, "positions": []}, portfolio_manager.load)
    portfolio = load_portfolio(portfolio_manager)
    summary = portfolio.get("summary", {})
    metric_cards([
        ("总资产", round(summary.get("total_asset", 0), 2), None),
        ("持仓市值", round(summary.get("market_value", 0), 2), None),
        ("可用现金", round(summary.get("available_cash", 0), 2), None),
        ("总盈亏", round(summary.get("total_pnl", 0), 2), f"{summary.get('total_pnl_pct', 0)}%"),
        ("仓位", f"{summary.get('position_ratio', 0)}%", None),
    ])

    with st.form("portfolio_form"):
        c1, c2, c3, c4 = st.columns(4)
        capital = c1.number_input("账户资金", min_value=0.0, value=float(raw.get("capital", 100000)), step=1000.0)
        code = c2.text_input("股票代码")
        quantity = c3.number_input("数量", min_value=0.0, value=0.0, step=100.0)
        cost_price = c4.number_input("成本价", min_value=0.0, value=0.0, step=0.01)
        name = st.text_input("名称")
        notes = st.text_input("备注")
        submitted = st.form_submit_button("保存持仓", use_container_width=True)
    if submitted:
        if code and not valid_code(code):
            st.error("股票代码必须是6位数字。")
        else:
            positions = list(raw.get("positions", []))
            if code and quantity > 0 and cost_price > 0:
                positions.append({
                    "code": code.strip().zfill(6),
                    "name": name,
                    "quantity": quantity,
                    "cost_price": cost_price,
                    "notes": notes,
                })
            portfolio_manager.save({"capital": capital, "positions": positions})
            st.rerun()

    positions = portfolio.get("positions", [])
    if positions:
        render_table(positions, [
            "code", "name", "quantity", "cost_price", "current_price", "change_pct",
            "market_value", "pnl", "pnl_pct", "weight", "stop_loss_price",
            "take_profit_price", "rule_action",
        ], {
            "code": "代码", "name": "名称", "quantity": "数量", "cost_price": "成本",
            "current_price": "现价", "change_pct": "今日%", "market_value": "市值",
            "pnl": "盈亏", "pnl_pct": "盈亏%", "weight": "仓位%", "stop_loss_price": "止损线",
            "take_profit_price": "止盈线", "rule_action": "规则提示",
        }, height=420)
        for item in positions:
            if st.button(f"删除 {item.get('code')} {item.get('name', '')}", key=f"delete-pos-{item.get('id')}"):
                kept = [p for p in raw.get("positions", []) if p.get("id") != item.get("id")]
                portfolio_manager.save({"capital": raw.get("capital", 100000), "positions": kept})
                st.rerun()
    else:
        st.info("暂无持仓。")

    configure_ai(advisor)
    if st.button("AI 持仓策略分析", use_container_width=True):
        if not advisor.is_configured:
            st.warning("请先配置 DeepSeek API Key。")
        else:
            tail = st.session_state.get("tail_result") or tail_screener.last_result or {}
            with st.spinner("AI正在压缩持仓策略摘要..."):
                result = run_or_warn(
                    "AI持仓分析",
                    {},
                    lambda: advisor.analyze_portfolio(portfolio, tail.get("recommendations", [])),
                )
            st.markdown(result.get("response") or result.get("error") or "无响应")


def source_candidates(sources: dict) -> list[dict]:
    labels = {"watchlist": "自选股", "positions": "持仓", "tail_end": "尾盘潜伏"}
    items = []
    for key, rows in (sources or {}).items():
        for row in rows:
            code = str(row.get("code", "")).zfill(6)
            if valid_code(code):
                items.append({
                    **row,
                    "code": code,
                    "source": row.get("source") or labels.get(key, key),
                    "label": f"{code} {row.get('name', '')} - {row.get('source') or labels.get(key, key)}",
                })
    return items


def page_tail(tail_screener: TailEndScreener) -> None:
    st.title("尾盘潜伏")
    st.caption("昨日K线初筛、今日分时/资金验证、尾盘执行计划。结果会进入预期计划候选来源。")
    if st.button("执行尾盘筛选", type="primary", use_container_width=True):
        with st.spinner("正在执行三阶段筛选，首次运行会较慢..."):
            st.session_state["tail_result"] = run_or_warn("尾盘筛选", {}, tail_screener.screen)
    result = st.session_state.get("tail_result") or tail_screener.last_result or {}
    summary = result.get("summary", {})
    metric_cards([
        ("状态", summary.get("message", "未运行"), None),
        ("初筛", summary.get("stage1_count", 0), None),
        ("盘中验证", summary.get("stage2_count", 0), None),
        ("入选", summary.get("selected_count", 0), None),
    ])
    context = result.get("market_context", {})
    if context:
        with st.expander("市场上下文", expanded=False):
            st.json(context, expanded=False)
    rows = result.get("recommendations", [])
    render_table(rows, [
        "rank", "code", "name", "today_price", "today_change", "stage1_score",
        "stage2_score", "final_score", "main_net_today", "main_net_pct",
        "today_confirmation", "entry_plan", "risk_note",
    ], {
        "rank": "排名", "code": "代码", "name": "名称", "today_price": "现价",
        "today_change": "今日涨幅%", "stage1_score": "昨日分", "stage2_score": "盘中分",
        "final_score": "综合分", "main_net_today": "主力净流", "main_net_pct": "主力占比%",
        "today_confirmation": "盘中确认", "entry_plan": "执行计划", "risk_note": "风险",
    }, height=520)


def plan_payload_from_form(candidate: dict | None, form: dict) -> dict:
    code = str(form["code"]).strip().zfill(6)
    entry = safe_float(form["planned_price"])
    stop_loss = safe_float(form["stop_loss"])
    take_profit = safe_float(form["take_profit"])
    if form["beginner_defaults"] and entry > 0:
        stop_loss = stop_loss or round(entry * 0.95, 2)
        take_profit = take_profit or round(entry * 1.10, 2)
    return {
        "code": code,
        "name": str(form["name"]).strip() or (candidate or {}).get("name", ""),
        "source": str(form["source"]).strip() or (candidate or {}).get("source", "手动"),
        "thesis_type": form["thesis_type"],
        "thesis": form["thesis"],
        "trigger": form["trigger"],
        "invalidation": form["invalidation"],
        "planned_price": entry,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "position_pct": safe_float(form["position_pct"], 5.0),
        "horizon": form["horizon"],
        "review_note": form["review_note"],
    }


def page_expectation(data_feed: DataFeed, portfolio_manager: PortfolioManager,
                     tail_screener: TailEndScreener, planner: ExpectationPlanner) -> None:
    st.title("预期计划")
    st.caption("先写交易预期，再用行情、K线、资金与来源匹配校验。")
    portfolio = load_portfolio(portfolio_manager)
    tail_result = st.session_state.get("tail_result") or tail_screener.last_result or {}
    data = run_or_warn(
        "预期计划",
        {"plans": [], "summary": {}, "sources": {}},
        lambda: planner.evaluate_all(watchlist.codes, portfolio, tail_result),
    )
    summary = data.get("summary", {})
    metric_cards([
        ("计划数", summary.get("plan_count", 0), None),
        ("可执行", summary.get("executable_count", 0), None),
        ("已失效", summary.get("invalid_count", 0), None),
        ("已兑现", summary.get("fulfilled_count", 0), None),
        ("平均分", summary.get("avg_score", 0), None),
    ])

    candidates = source_candidates(data.get("sources", {}))
    selected_label = st.selectbox("从系统候选导入", ["手动输入"] + [item["label"] for item in candidates])
    selected = next((item for item in candidates if item["label"] == selected_label), None)
    default_price = safe_float((selected or {}).get("price") or (selected or {}).get("today_price"))

    with st.form("expectation_form"):
        c1, c2, c3 = st.columns(3)
        code = c1.text_input("股票代码", value=(selected or {}).get("code", ""))
        name = c2.text_input("名称", value=(selected or {}).get("name", ""))
        source = c3.text_input("来源", value=(selected or {}).get("source", "手动"))
        thesis_type = st.selectbox("预期类型", ["技术形态", "资金回流", "板块轮动", "基本面修复", "尾盘潜伏", "其他"])
        thesis = st.text_area("预期逻辑", value="只在系统候选内观察，等待价格接近计划位且资金/技术信号配合", height=75)
        trigger = st.text_area("触发条件", value="当前价接近计划买入价；K线结构不破坏；主力资金不明显流出", height=70)
        invalidation = st.text_area("失效条件", value="跌破止损价；当前价较计划价上偏超过3%；板块资金转弱", height=70)
        p1, p2, p3, p4 = st.columns(4)
        planned_price = p1.number_input("计划买入价", min_value=0.0, value=float(default_price or 0), step=0.01)
        stop_loss = p2.number_input("止损价", min_value=0.0, value=0.0, step=0.01)
        take_profit = p3.number_input("止盈价", min_value=0.0, value=0.0, step=0.01)
        position_pct = p4.number_input("计划仓位%", min_value=0.0, max_value=100.0, value=5.0, step=0.5)
        c4, c5 = st.columns([1, 2])
        beginner_defaults = c4.checkbox("新手默认边界", value=True)
        horizon = c5.selectbox("时间窗口", ["T+1至5日", "当日不买只观察", "1至3日", "1至2周", "自定义"])
        review_note = st.text_area("复盘记录", value="复盘是否按计划执行，是否出现追涨、重仓或无视失效条件", height=70)
        submitted = st.form_submit_button("保存预期计划", use_container_width=True)
    if submitted:
        payload = plan_payload_from_form(selected, {
            "code": code, "name": name, "source": source, "thesis_type": thesis_type,
            "thesis": thesis, "trigger": trigger, "invalidation": invalidation,
            "planned_price": planned_price, "stop_loss": stop_loss, "take_profit": take_profit,
            "position_pct": position_pct, "beginner_defaults": beginner_defaults,
            "horizon": horizon, "review_note": review_note,
        })
        if not valid_code(payload["code"]):
            st.error("股票代码必须是6位数字。")
        elif payload["planned_price"] <= 0:
            st.error("请填写计划买入价。")
        else:
            planner.save_plan(payload)
            st.rerun()

    with st.expander("候选来源", expanded=False):
        for key, title in [("watchlist", "自选股"), ("positions", "持仓"), ("tail_end", "尾盘潜伏")]:
            st.markdown(f"**{title}**")
            render_table(data.get("sources", {}).get(key, []), ["code", "name", "price", "change_pct", "score", "reason"], {
                "code": "代码", "name": "名称", "price": "价格", "change_pct": "涨跌幅%",
                "score": "来源分", "reason": "理由",
            }, height=180)

    plans = data.get("plans", [])
    st.subheader("已有计划")
    if not plans:
        st.info("暂无预期计划。")
        return
    render_table(plans, [
        "id", "code", "name", "source", "planned_price", "stop_loss", "take_profit",
        "position_pct", "final_score", "earning_effect_score", "status", "warnings",
    ], {
        "id": "ID", "code": "代码", "name": "名称", "source": "来源", "planned_price": "计划价",
        "stop_loss": "止损", "take_profit": "止盈", "position_pct": "仓位%", "final_score": "总分",
        "earning_effect_score": "赚钱效应", "status": "状态", "warnings": "提示",
    }, height=420)
    plan_ids = [p.get("id") for p in plans if p.get("id")]
    if plan_ids:
        target = st.selectbox("删除计划", ["不删除"] + plan_ids)
        if target != "不删除" and st.button("确认删除", use_container_width=True):
            planner.delete_plan(target)
            st.rerun()


def page_review(analyzer: Analyzer) -> None:
    st.title("交易复盘")
    st.caption("上传交易 CSV 后统计胜率、盈亏比、月度表现和策略表现。")
    st.info("CSV 至少包含：类型、盈亏%、盈亏。若有 卖出日期、持仓天数、策略、累计盈亏，统计会更完整。")
    uploaded = st.file_uploader("上传交易记录 CSV", type=["csv"])
    text = st.text_area("或粘贴 CSV 内容", height=150)
    if st.button("开始复盘", type="primary", use_container_width=True):
        try:
            if uploaded is not None:
                frame = pd.read_csv(uploaded)
            elif text.strip():
                frame = pd.read_csv(StringIO(text))
            else:
                st.warning("请上传或粘贴 CSV。")
                return
            result = analyzer.analyze_trades(frame.to_dict("records"))
            st.session_state["review_result"] = result
        except Exception as exc:
            st.error(f"解析失败：{exc}")
    result = st.session_state.get("review_result")
    if result:
        if result.get("error"):
            st.error(result["error"])
            return
        metric_cards([
            ("交易次数", result.get("总交易次数", 0), None),
            ("胜率", f"{result.get('胜率', 0)}%", None),
            ("总盈亏", result.get("总盈亏", 0), f"{result.get('总盈亏%', 0)}%"),
            ("盈亏比", result.get("盈亏比", 0), None),
            ("最大回撤", f"{result.get('最大回撤%', 0)}%", None),
        ])
        if isinstance(result.get("月度统计"), pd.DataFrame) and not result["月度统计"].empty:
            st.subheader("月度统计")
            st.dataframe(result["月度统计"], use_container_width=True)
        if result.get("策略分析"):
            st.subheader("策略分析")
            st.dataframe(pd.DataFrame(result["策略分析"]).T, use_container_width=True)
        if isinstance(result.get("交易明细"), pd.DataFrame):
            st.subheader("交易明细")
            st.dataframe(result["交易明细"], use_container_width=True, hide_index=True)


def page_settings() -> None:
    st.title("运行设置")
    st.caption("仅影响当前 Streamlit 进程；云端重启后会回到代码默认值。")
    with st.form("settings_form"):
        c1, c2, c3, c4 = st.columns(4)
        position = c1.number_input("单票仓位%", min_value=1.0, max_value=80.0, value=float(RISK["position_pct"] * 100), step=1.0)
        exposure = c2.number_input("组合仓位上限%", min_value=1.0, max_value=100.0, value=float(RISK["max_exposure"] * 100), step=1.0)
        stop = c3.number_input("默认止损%", min_value=-50.0, max_value=-0.5, value=float(RISK["stop_loss"] * 100), step=0.5)
        take = c4.number_input("默认止盈%", min_value=0.5, max_value=100.0, value=float(RISK["take_profit"] * 100), step=0.5)
        b1, b2, b3, b4 = st.columns(4)
        capital = b1.number_input("回测初始资金", min_value=1000.0, value=float(BACKTEST["initial_capital"]), step=1000.0)
        commission = b2.number_input("佣金%", min_value=0.0, max_value=5.0, value=float(BACKTEST["commission"] * 100), step=0.01)
        stamp = b3.number_input("印花税%", min_value=0.0, max_value=5.0, value=float(BACKTEST["stamp_tax"] * 100), step=0.01)
        slippage = b4.number_input("滑点%", min_value=0.0, max_value=5.0, value=float(BACKTEST["slippage"] * 100), step=0.01)
        saved = st.form_submit_button("保存当前进程设置", use_container_width=True)
    if saved:
        if position > exposure:
            st.error("单票仓位不能超过组合仓位上限。")
        else:
            RISK.update({
                "position_pct": position / 100,
                "max_exposure": exposure / 100,
                "stop_loss": stop / 100,
                "take_profit": take / 100,
            })
            BACKTEST.update({
                "initial_capital": capital,
                "commission": commission / 100,
                "stamp_tax": stamp / 100,
                "slippage": slippage / 100,
            })
            st.success("已更新当前进程参数。")
    st.subheader("当前参数")
    st.json({"RISK": RISK, "BACKTEST": BACKTEST, "SCREEN": SCREEN, "LONG_TERM": LONG_TERM}, expanded=False)


def page_deploy() -> None:
    st.title("部署说明")
    st.markdown(
        """
Streamlit Cloud 部署参数：

1. Repository：`ttlucky2002-ui/a-share-trading`
2. Branch：`main`
3. Main file path：`streamlit_app.py`
4. 需要 AI 时，在 Secrets 中配置 `DEEPSEEK_API_KEY="你的key"`

云端版不会展示国信下单和真实账户查询页面；真实交易密钥不要放到公开云端。
"""
    )
    st.code('DEEPSEEK_API_KEY="your-key"', language="toml")


def sidebar_ai_config(advisor: AIAdvisor) -> None:
    with st.sidebar.expander("AI Key", expanded=False):
        st.caption("可使用 Streamlit Secrets，也可以临时填在这里；不会写入仓库。")
        key = st.text_input("DEEPSEEK_API_KEY", value=st.session_state.get("deepseek_api_key", ""), type="password")
        c1, c2 = st.columns(2)
        if c1.button("使用", use_container_width=True):
            st.session_state["deepseek_api_key"] = key.strip()
            configure_ai(advisor)
            st.rerun()
        if c2.button("清除", use_container_width=True):
            st.session_state["deepseek_api_key"] = ""
            configure_ai(advisor)
            st.rerun()
        configure_ai(advisor)
        st.write("状态：" + ("已配置" if advisor.is_configured else "未配置"))


def main() -> None:
    inject_css()
    svc = services()
    data_feed: DataFeed = svc["data"]
    portfolio_manager: PortfolioManager = svc["portfolio"]
    tail_screener: TailEndScreener = svc["tail"]
    planner: ExpectationPlanner = svc["planner"]
    technical_screener: StockScreener = svc["technical"]
    fundamental_screener: LongTermFundamentalScreener = svc["fundamental"]
    advisor: AIAdvisor = svc["ai"]
    analyzer: Analyzer = svc["analyzer"]

    sidebar_ai_config(advisor)
    st.sidebar.title("A股研究系统")
    st.sidebar.caption(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    page = st.sidebar.radio(
        "模块",
        [
            "市场总览",
            "选股中心",
            "个股研究",
            "AI研究助手",
            "策略回测",
            "自选股",
            "持仓诊断",
            "尾盘潜伏",
            "预期计划",
            "交易复盘",
            "运行设置",
            "部署说明",
        ],
        index=1,
    )

    if page == "市场总览":
        page_market(data_feed)
    elif page == "选股中心":
        page_screening(data_feed, technical_screener, fundamental_screener, planner)
    elif page == "个股研究":
        page_research(data_feed, advisor)
    elif page == "AI研究助手":
        page_ai(data_feed, advisor, fundamental_screener)
    elif page == "策略回测":
        page_backtest(data_feed, advisor, fundamental_screener)
    elif page == "自选股":
        page_watchlist(data_feed)
    elif page == "持仓诊断":
        page_portfolio(portfolio_manager, advisor, tail_screener)
    elif page == "尾盘潜伏":
        page_tail(tail_screener)
    elif page == "预期计划":
        page_expectation(data_feed, portfolio_manager, tail_screener, planner)
    elif page == "交易复盘":
        page_review(analyzer)
    elif page == "运行设置":
        page_settings()
    else:
        page_deploy()


if __name__ == "__main__":
    main()
