#!/usr/bin/env python3
"""Command-line research utilities without a local paper-trading account."""

import json
import sys

import pandas as pd

from analysis import Analyzer
from backtest import BacktestEngine
from config import LONG_TERM, OUTPUT
from data_feed import DataFeed
from fundamental import LongTermFundamentalScreener


def cmd_screen() -> None:
    """Run the comprehensive medium/long-term stock selection process."""
    feed = DataFeed()
    result = LongTermFundamentalScreener(data_feed=feed).screen(
        universe_limit=LONG_TERM["universe_limit"],
    )
    frame = pd.DataFrame(result)
    if frame.empty:
        print("未筛选出达到综合分析条件的标的。")
        return
    frame.to_csv(OUTPUT["screen_result_file"], index=False, encoding="utf-8-sig")
    columns = [
        column for column in (
            "code", "name", "fundamental_score", "technical_score",
            "composite_score", "selection_score", "recommendation_rank",
            "report_date", "roe", "profit_growth", "revenue_growth", "pe", "pb",
        ) if column in frame.columns
    ]
    print(frame[columns].to_string(index=False))
    print(f"\n结果已写入 {OUTPUT['screen_result_file']}。")


def cmd_backtest(code: str, days: int = 250) -> None:
    """Run historical validation; it does not represent a live account."""
    result = BacktestEngine(data_feed=DataFeed()).run(code, days=days, with_benchmark=True)
    if not result:
        print("回测失败：历史 K 线不足或数据源不可用。")
        return
    metrics = {
        key: result.get(key)
        for key in ("code", "total_return", "benchmark_return", "max_drawdown",
                    "win_rate", "sharpe_ratio", "trade_count")
    }
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


def cmd_analyze(csv_path: str) -> None:
    """Analyse a broker-exported transaction CSV or other explicit trade file."""
    trades = pd.read_csv(csv_path).to_dict("records")
    report = Analyzer().analyze_trades(trades)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


def print_usage() -> None:
    print(
        "用法:\n"
        "  python main.py screen\n"
        "  python main.py backtest <股票代码> [交易日数]\n"
        "  python main.py analyze <真实成交CSV路径>\n\n"
        "真实账户风控和国信委托请通过 python server.py 启动的网页完成。"
    )


def main() -> None:
    if len(sys.argv) < 2:
        print_usage()
        return
    command = sys.argv[1].lower()
    if command == "screen":
        cmd_screen()
    elif command == "backtest" and len(sys.argv) >= 3:
        days = int(sys.argv[3]) if len(sys.argv) >= 4 else 250
        cmd_backtest(sys.argv[2], days)
    elif command == "analyze" and len(sys.argv) >= 3:
        cmd_analyze(sys.argv[2])
    else:
        print_usage()


if __name__ == "__main__":
    main()
