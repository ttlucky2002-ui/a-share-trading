"""Streamlit Cloud entry point for the A-share research system."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Callable

import pandas as pd
import streamlit as st

from data_feed import DataFeed
from expectation_planner import ExpectationPlanner
from portfolio_manager import PortfolioManager
from screener_tail import TailEndScreener
from watchlist import watchlist


st.set_page_config(
    page_title="A股研究与预期计划",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_resource(show_spinner=False)
def services():
    data_feed = DataFeed()
    portfolio_manager = PortfolioManager(data_feed)
    tail_screener = TailEndScreener(data_feed)
    expectation_planner = ExpectationPlanner(data_feed)
    return data_feed, portfolio_manager, tail_screener, expectation_planner


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


def run_or_warn(label: str, fallback: Any, fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as exc:  # Streamlit page should degrade instead of crashing.
        st.warning(f"{label}暂不可用：{exc}")
        return fallback


def valid_code(code: str) -> bool:
    return bool(re.fullmatch(r"\d{6}", str(code).strip()))


def as_frame(rows: list[dict], columns: list[str], names: dict[str, str]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    present = [column for column in columns if column in frame.columns]
    return frame[present].rename(columns=names)


def quote_frame(codes: list[str], data_feed: DataFeed) -> pd.DataFrame:
    quotes = data_feed.get_realtime_quotes(codes)
    if quotes.empty:
        return pd.DataFrame({"code": codes})
    columns = ["code", "name", "price", "change_pct", "amount", "volume"]
    return as_frame(quotes.to_dict("records"), columns, {
        "code": "代码",
        "name": "名称",
        "price": "现价",
        "change_pct": "涨跌幅%",
        "amount": "成交额",
        "volume": "成交量",
    })


def load_portfolio(portfolio_manager: PortfolioManager) -> dict:
    raw = run_or_warn("持仓文件", {"capital": 100000, "positions": []}, portfolio_manager.load)
    return run_or_warn(
        "持仓诊断",
        {"capital": raw.get("capital", 100000), "positions": [], "summary": {}},
        lambda: portfolio_manager.enrich(raw),
    )


def metric_row(summary: dict) -> None:
    cols = st.columns(5)
    cols[0].metric("计划数", summary.get("plan_count", 0))
    cols[1].metric("可执行", summary.get("executable_count", 0))
    cols[2].metric("已失效", summary.get("invalid_count", 0))
    cols[3].metric("已兑现", summary.get("fulfilled_count", 0))
    cols[4].metric("平均分", summary.get("avg_score", 0))


def source_candidates(sources: dict) -> list[dict]:
    labels = {
        "watchlist": "自选股",
        "positions": "持仓",
        "tail_end": "尾盘潜伏",
    }
    items = []
    for key, rows in (sources or {}).items():
        for row in rows:
            code = str(row.get("code", "")).zfill(6)
            if not valid_code(code):
                continue
            items.append({
                **row,
                "code": code,
                "source": row.get("source") or labels.get(key, key),
                "label": f"{code} {row.get('name', '')} - {row.get('source') or labels.get(key, key)}",
            })
    return items


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
    st.caption("先写交易预期，再让系统用行情、K线、资金和来源匹配校验计划。")

    portfolio = load_portfolio(portfolio_manager)
    tail_result = st.session_state.get("tail_result") or tail_screener.last_result or {}
    with st.spinner("正在评估预期计划..."):
        data = run_or_warn(
            "预期计划",
            {"plans": [], "summary": {}, "sources": {}},
            lambda: planner.evaluate_all(watchlist.codes, portfolio, tail_result),
        )

    metric_row(data.get("summary", {}))

    candidates = source_candidates(data.get("sources", {}))
    selected_label = st.selectbox(
        "从系统候选导入",
        ["手动输入"] + [item["label"] for item in candidates],
        index=0,
    )
    selected = next((item for item in candidates if item["label"] == selected_label), None)

    st.subheader("新建计划")
    default_price = safe_float((selected or {}).get("price") or (selected or {}).get("today_price"))
    with st.form("expectation_form", clear_on_submit=False):
        left, mid, right = st.columns(3)
        code = left.text_input("股票代码", value=(selected or {}).get("code", ""))
        name = mid.text_input("名称", value=(selected or {}).get("name", ""))
        source = right.text_input("来源", value=(selected or {}).get("source", "手动"))

        thesis_type = st.selectbox(
            "预期类型",
            ["技术形态", "资金回流", "板块轮动", "基本面修复", "尾盘潜伏", "其他"],
        )
        thesis = st.text_area(
            "预期逻辑",
            value="只在系统候选内观察，等待价格接近计划位且资金/技术信号配合",
            height=80,
        )
        trigger = st.text_area(
            "触发条件",
            value="当前价接近计划买入价；K线结构不破坏；主力资金不明显流出",
            height=75,
        )
        invalidation = st.text_area(
            "失效条件",
            value="跌破止损价；当前价较计划价上偏超过3%；板块资金转弱",
            height=75,
        )

        p1, p2, p3, p4 = st.columns(4)
        planned_price = p1.number_input("计划买入价", min_value=0.0, value=float(default_price or 0), step=0.01)
        stop_loss = p2.number_input("止损价", min_value=0.0, value=0.0, step=0.01)
        take_profit = p3.number_input("止盈价", min_value=0.0, value=0.0, step=0.01)
        position_pct = p4.number_input("计划仓位%", min_value=0.0, max_value=100.0, value=5.0, step=0.5)

        c1, c2 = st.columns([1, 2])
        beginner_defaults = c1.checkbox("新手默认边界", value=True)
        horizon = c2.selectbox("时间窗口", ["T+1至5日", "当日不买只观察", "1至3日", "1至2周", "自定义"])
        review_note = st.text_area(
            "复盘记录",
            value="复盘是否按计划执行，是否出现追涨、重仓或无视失效条件",
            height=70,
        )
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
            run_or_warn("保存计划", {}, lambda: planner.save_plan(payload))
            st.success("已保存预期计划。")
            st.rerun()

    with st.expander("系统候选来源", expanded=False):
        for key, title in [("watchlist", "自选股"), ("positions", "持仓"), ("tail_end", "尾盘潜伏")]:
            rows = data.get("sources", {}).get(key, [])
            st.markdown(f"**{title}**")
            if rows:
                st.dataframe(as_frame(rows, ["code", "name", "price", "change_pct", "score", "reason"], {
                    "code": "代码", "name": "名称", "price": "价格", "change_pct": "涨跌幅%",
                    "score": "来源分", "reason": "理由",
                }), use_container_width=True, hide_index=True)
            else:
                st.caption("暂无数据")

    st.subheader("已有计划")
    plans = data.get("plans", [])
    if not plans:
        st.info("还没有计划。先从自选、持仓或尾盘潜伏结果导入一个标的。")
        return

    for plan in plans:
        title = f"{plan.get('code')} {plan.get('name', '')} · {plan.get('status', '待评估')} · {plan.get('final_score', 0)}分"
        with st.expander(title, expanded=False):
            top = st.columns(5)
            top[0].metric("现价", plan.get("quote", {}).get("price", 0), f"{plan.get('quote', {}).get('change_pct', 0)}%")
            top[1].metric("计划价", plan.get("planned_price", 0))
            top[2].metric("止损", plan.get("stop_loss", 0))
            top[3].metric("止盈", plan.get("take_profit", 0))
            top[4].metric("仓位", f"{plan.get('position_pct', 0)}%")

            st.write(plan.get("thesis", ""))
            cols = st.columns(3)
            cols[0].caption(f"计划完整度：{plan.get('plan_quality', {}).get('score', 0)}")
            cols[1].caption(f"赚钱效应：{plan.get('earning_effect_score', 0)}")
            cols[2].caption(f"执行纪律：{plan.get('execution', {}).get('score', 0)}")

            warnings = plan.get("warnings") or []
            if warnings:
                st.warning("；".join(warnings[:4]))
            questions = plan.get("review_questions") or []
            if questions:
                st.info(" / ".join(questions))
            if st.button("删除计划", key=f"delete-{plan.get('id')}"):
                run_or_warn("删除计划", {}, lambda plan_id=plan.get("id"): planner.delete_plan(plan_id))
                st.rerun()


def page_market(data_feed: DataFeed) -> None:
    st.title("市场概览")
    metrics = run_or_warn("市场情绪", {}, data_feed.get_market_metrics)
    cols = st.columns(6)
    cols[0].metric("股票数", metrics.get("total", 0))
    cols[1].metric("上涨", metrics.get("up", 0))
    cols[2].metric("下跌", metrics.get("down", 0))
    cols[3].metric("涨跌比", metrics.get("advance_ratio", 0))
    cols[4].metric("涨停/跌停", f"{metrics.get('limit_up', 0)}/{metrics.get('limit_down', 0)}")
    cols[5].metric("情绪", metrics.get("mood", "-"))

    with st.spinner("加载市场快照..."):
        stocks = run_or_warn("市场快照", pd.DataFrame(), data_feed.get_stock_list)
    if stocks.empty:
        st.info("暂无市场快照，可能是数据源暂时不可用。")
        return

    display_cols = {
        "code": "代码", "name": "名称", "price": "现价", "change_pct": "涨跌幅%",
        "turnover": "换手率%", "amount": "成交额(亿)", "market_cap": "市值(亿)",
        "main_net": "主力净额", "main_net_pct": "主力净占比%",
    }
    top = stocks.sort_values("change_pct", ascending=False).head(20)
    weak = stocks.sort_values("change_pct", ascending=True).head(20)
    left, right = st.columns(2)
    left.subheader("涨幅前列")
    left.dataframe(as_frame(top.to_dict("records"), list(display_cols), display_cols), use_container_width=True, hide_index=True)
    right.subheader("跌幅前列")
    right.dataframe(as_frame(weak.to_dict("records"), list(display_cols), display_cols), use_container_width=True, hide_index=True)

    status = data_feed.get_source_status()
    with st.expander("数据源状态", expanded=False):
        st.json(status, expanded=False)


def page_watchlist(data_feed: DataFeed) -> None:
    st.title("自选股")
    col_add, col_btn = st.columns([3, 1])
    new_code = col_add.text_input("添加股票代码", placeholder="例如 600519")
    if col_btn.button("添加", use_container_width=True):
        if valid_code(new_code):
            run_or_warn("添加自选", None, lambda: watchlist.add(str(new_code).strip()))
            st.rerun()
        else:
            st.error("股票代码必须是6位数字。")

    codes = list(watchlist.codes)
    if not codes:
        st.info("暂无自选股。")
        return

    with st.spinner("加载自选行情..."):
        st.dataframe(quote_frame(codes, data_feed), use_container_width=True, hide_index=True)
    st.divider()
    for code in codes:
        row = st.columns([2, 1])
        row[0].write(code)
        if row[1].button("移除", key=f"rm-watch-{code}", use_container_width=True):
            run_or_warn("移除自选", None, lambda c=code: watchlist.remove(c))
            st.rerun()


def page_portfolio(portfolio_manager: PortfolioManager) -> None:
    st.title("持仓诊断")
    raw = run_or_warn("持仓文件", {"capital": 100000, "positions": []}, portfolio_manager.load)
    portfolio = load_portfolio(portfolio_manager)
    summary = portfolio.get("summary", {})

    cols = st.columns(5)
    cols[0].metric("总资产", summary.get("total_asset", 0))
    cols[1].metric("持仓市值", summary.get("market_value", 0))
    cols[2].metric("可用现金", summary.get("available_cash", 0))
    cols[3].metric("总盈亏", summary.get("total_pnl", 0), f"{summary.get('total_pnl_pct', 0)}%")
    cols[4].metric("仓位", f"{summary.get('position_ratio', 0)}%")

    with st.form("portfolio_form"):
        st.subheader("新增持仓")
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
                    "code": str(code).strip().zfill(6),
                    "name": name,
                    "quantity": quantity,
                    "cost_price": cost_price,
                    "notes": notes,
                })
            run_or_warn("保存持仓", {}, lambda: portfolio_manager.save({"capital": capital, "positions": positions}))
            st.success("已保存持仓。")
            st.rerun()

    positions = portfolio.get("positions", [])
    if not positions:
        st.info("暂无持仓。云端版仅用于研究和计划记录，不建议连接真实下单。")
        return
    table = as_frame(positions, [
        "code", "name", "quantity", "cost_price", "current_price", "change_pct",
        "market_value", "pnl", "pnl_pct", "weight", "rule_action",
    ], {
        "code": "代码", "name": "名称", "quantity": "数量", "cost_price": "成本价",
        "current_price": "现价", "change_pct": "涨跌幅%", "market_value": "市值",
        "pnl": "盈亏", "pnl_pct": "盈亏%", "weight": "仓位%", "rule_action": "规则提示",
    })
    st.dataframe(table, use_container_width=True, hide_index=True)
    for item in positions:
        if st.button(f"删除 {item.get('code')} {item.get('name', '')}", key=f"rm-pos-{item.get('id')}"):
            kept = [p for p in raw.get("positions", []) if p.get("id") != item.get("id")]
            run_or_warn("删除持仓", {}, lambda: portfolio_manager.save({"capital": raw.get("capital", 100000), "positions": kept}))
            st.rerun()


def page_tail(tail_screener: TailEndScreener) -> None:
    st.title("尾盘潜伏")
    st.caption("运行后，结果会进入预期计划的候选来源。")
    if st.button("执行尾盘筛选", type="primary", use_container_width=True):
        with st.spinner("正在执行三阶段筛选，首次运行会较慢..."):
            st.session_state["tail_result"] = run_or_warn("尾盘筛选", {}, tail_screener.screen)

    result = st.session_state.get("tail_result") or tail_screener.last_result or {}
    summary = result.get("summary", {})
    cols = st.columns(4)
    cols[0].metric("状态", summary.get("message", "未运行"))
    cols[1].metric("初筛", summary.get("stage1_count", 0))
    cols[2].metric("盘中验证", summary.get("stage2_count", 0))
    cols[3].metric("入选", summary.get("selected_count", 0))

    rows = result.get("recommendations", [])
    if not rows:
        st.info("暂无筛选结果。")
        return
    table = as_frame(rows, [
        "rank", "code", "name", "today_price", "today_change", "stage1_score",
        "stage2_score", "final_score", "today_confirmation", "entry_plan", "risk_note",
    ], {
        "rank": "排名", "code": "代码", "name": "名称", "today_price": "现价",
        "today_change": "今日涨幅%", "stage1_score": "昨日分", "stage2_score": "盘中分",
        "final_score": "综合分", "today_confirmation": "盘中确认",
        "entry_plan": "执行计划", "risk_note": "风险",
    })
    st.dataframe(table, use_container_width=True, hide_index=True)


def page_deploy() -> None:
    st.title("部署说明")
    st.markdown(
        """
1. GitHub 仓库推送后，在 Streamlit Community Cloud 创建新应用。
2. Repository 选择本项目，Branch 选择 `main`，Main file path 填 `streamlit_app.py`。
3. 需要 DeepSeek 时，在 Cloud 的 Secrets 中配置 `DEEPSEEK_API_KEY="你的key"`。
4. 国信 AK/SK 不建议放在公开云端；云端版默认只做研究、预期计划和复盘。
"""
    )
    st.code(
        """DEEPSEEK_API_KEY="your-key"
# 可选，不建议云端启用真实下单
GUOSEN_API_BASE_URL=""
GUOSEN_API_AK=""
GUOSEN_API_SK=""
""",
        language="toml",
    )


def main() -> None:
    data_feed, portfolio_manager, tail_screener, planner = services()
    st.sidebar.title("A股研究系统")
    st.sidebar.caption(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    page = st.sidebar.radio(
        "模块",
        ["预期计划", "市场概览", "自选股", "持仓诊断", "尾盘潜伏", "部署说明"],
        index=0,
    )

    if page == "预期计划":
        page_expectation(data_feed, portfolio_manager, tail_screener, planner)
    elif page == "市场概览":
        page_market(data_feed)
    elif page == "自选股":
        page_watchlist(data_feed)
    elif page == "持仓诊断":
        page_portfolio(portfolio_manager)
    elif page == "尾盘潜伏":
        page_tail(tail_screener)
    else:
        page_deploy()


if __name__ == "__main__":
    main()
