"""
A股中长期研究与交易系统 - 配置文件
=========================
所有可调参数集中管理，便于策略调整。
"""
import os

# ========== 数据源配置 ==========
REQUEST_TIMEOUT = 15
REQUEST_RETRIES = 3
REQUEST_INTERVAL = 0.3

# ========== 选股过滤条件 ==========
SCREEN = {
    "market_cap_min": 30,
    "market_cap_max": 2000,
    "price_min": 3.0,
    "price_max": 200.0,
    "avg_amount_min": 0.5,
    "turnover_min": 1.0,
    "turnover_max": 20.0,
    "exclude_st": True,
    "exclude_kcb": False,
    "exclude_bj": True,
}

# ========== 技术指标参数 ==========
INDICATORS = {
    "ma_short": 5,
    "ma_medium": 10,
    "ma_long": 20,
    "ma_trend": 60,
    "macd_fast": 12,
    "macd_slow": 26,
    "macd_signal": 9,
    "rsi_period": 14,
    "rsi_overbought": 75,
    "rsi_oversold": 25,
    "kdj_k": 9,
    "kdj_d": 3,
    "volume_ratio": 1.5,
}

# ========== 兼容技术筛选接口的评分权重（不属于回测交易方案） ==========
STRATEGY = {
    "strategy_weights": {
        "volume_breakout": 0.25,
        "ma_golden_cross": 0.20,
        "macd_signal": 0.20,
        "kdj_signal": 0.15,
        "volume_price": 0.20,
    },
    "score_threshold": 60,
    "max_stocks": 15,
}

# ========== 中长期选股（目标持仓至少一周） ==========
LONG_TERM = {
    "holding_horizon": "至少1周，重点观察1至6个月",
    # 0 表示对通过初步交易性过滤的全部股票拉取财务数据，不做人为截断。
    "universe_limit": 0,
    "result_limit": 50,
    "recommendation_count": 10,
    "minimum_score": 50,
    "market_cap_min": 50,
    "average_amount_min": 0.2,
    "turnover_max": 12.0,
    "weights": {
        "quality": 0.35,
        "growth": 0.30,
        "valuation": 0.20,
        "cashflow": 0.15,
    },
    "composite_weights": {
        "fundamental": 0.70,
        "technical": 0.30,
    },
    "selection_weights": {
        "composite": 0.80,
        "market_flow": 0.20,
    },
    "theme_board_limit": 8,
}

# ========== 风险管理 ==========
RISK = {
    "position_pct": 0.2,
    "max_exposure": 0.8,
    "stop_loss": -0.03,
    "take_profit": 0.06,
    "max_positions": 5,
    "max_trades_per_day": 3,
    "max_drawdown": -0.10,
}

# ========== 交易时段 ==========
TRADING_HOURS = {
    "morning_start": "09:30",
    "morning_end": "11:30",
    "afternoon_start": "13:00",
    "afternoon_end": "15:00",
}

# ========== 回测配置 ==========
BACKTEST = {
    "initial_capital": 100000,
    "commission": 0.00025,
    "stamp_tax": 0.001,
    "slippage": 0.001,
}

# ========== 输出配置 ==========
OUTPUT = {
    "screen_result_file": "选股结果.csv",
    "trade_log_file": "交易记录.csv",
    "analysis_report_file": "复盘报告.html",
    "chart_dir": "charts",
}

# ========== 尾盘潜伏选股策略 ==========
TAIL_END = {
    "holding_horizon": "尾盘买入，目标持仓1-3日",
    # Stage 1: 昨日初筛
    "stage1_pool_size": 200,
    "stage1_max_candidates": 1000,
    "stage1_preferred_market_cap": 180,
    "market_cap_min": 30,
    "price_min": 5.0,
    "price_max": 100.0,
    "exclude_st": True,
    "exclude_bj": True,
    "yesterday_change_min": -5.0,
    "yesterday_change_max": 5.0,
    "volume_ratio_min": 0.8,
    "rsi_min": 35,
    "rsi_max": 65,
    "stage1_kline_count": 80,
    "stage1_weights": {
        "volume": 0.25,
        "ma_structure": 0.25,
        "macd": 0.20,
        "price_position": 0.15,
        "rsi": 0.15,
    },
    # Stage 2: 今日盘中验证
    "today_change_min": 0.0,
    "today_change_max": 5.0,
    "today_volume_ratio_min": 0.8,
    "today_volume_ratio_max": 2.5,
    "today_main_net_min": 0,
    "today_main_net_pct_min": 0.0,
    "today_main_net_pct_max": 10.0,
    "reject_tail_down": True,
    "require_sector_match": True,
    "rotation_board_limit": 10,
    "stage2_candidate_limit": 50,
    "stage2_weights": {
        "volume_price": 0.30,
        "fund_flow": 0.30,
        "intraday_trend": 0.25,
        "sector_flow": 0.15,
    },
    # Stage 3: 尾盘精选
    "composite_weights": {
        "stage1": 0.30,
        "stage2": 0.70,
    },
    "recommendation_count": 10,
    "max_per_industry": 2,
    "max_per_concept": 2,
}

# ========== AI 选股助手配置 ==========
# DeepSeek API: https://platform.deepseek.com/
AI_CONFIG = {
    "api_key": os.environ.get("DEEPSEEK_API_KEY", ""),
    "api_url": "https://api.deepseek.com/v1/chat/completions",
    "model": "deepseek-chat",
    "max_tokens": 4096,
    "temperature": 0.7,
    "system_prompt": "你是一位审慎的A股综合研究助理，服务于至少持仓一周的中长期观察。只能依据提供的财务API数据、技术评分、板块资金流、行情与新闻分析；不得虚构财报、公告或预测，缺少数据时必须明确说明。输出需指出股票代码、报告期、主要依据与风险。",
    "enable_news": True,
    "news_count": 30,
}
