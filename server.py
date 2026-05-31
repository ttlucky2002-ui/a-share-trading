#!/usr/bin/env python3
"""A股量化交易系统 - 内置HTTP服务器 (无需Flask)"""
import math
import mimetypes
import os, sys, json, traceback, threading, time
from datetime import date, datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from io import StringIO
from urllib.parse import urlparse, parse_qs, unquote
import pandas as pd

from config import SCREEN, STRATEGY, RISK, BACKTEST, LONG_TERM
from data_feed import DataFeed
from screener import StockScreener
from fundamental import LongTermFundamentalScreener
from backtest import BacktestEngine
from strategy import StrategyEngine
from analysis import Analyzer
from watchlist import watchlist
from ai_advisor import AIAdvisor
from broker import BrokerError, guosen_client
from risk_control import RealAccountRiskAnalyzer
from screener_tail import TailEndScreener
from portfolio_manager import PortfolioManager
from expectation_planner import ExpectationPlanner

df = DataFeed()
screener = StockScreener(data_feed=df)
strategy = StrategyEngine()
analyzer = Analyzer()
ai_advisor = AIAdvisor()
fundamental_screener = LongTermFundamentalScreener(data_feed=df)
risk_analyzer = RealAccountRiskAnalyzer(guosen_client)
tail_screener = TailEndScreener(data_feed=df)
portfolio_manager = PortfolioManager(data_feed=df)
expectation_planner = ExpectationPlanner(data_feed=df)

TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), 'templates')
STATIC_DIR = os.path.join(os.path.dirname(__file__), 'static')

_screen_task = None
_screen_result = None
_fundamental_task = None
_fundamental_result = None
_optimization_task = None
_optimization_lock = threading.RLock()
_optimization_state = {
    "state": "idle",
    "logs": [],
    "trials": [],
}


def _json_safe(value):
    """Convert pandas/numpy values into strict JSON-safe primitives."""
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.strftime("%Y-%m-%d %H:%M:%S") if isinstance(value, datetime) else value.strftime("%Y-%m-%d")

    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass

    if hasattr(value, "item"):
        try:
            value = value.item()
        except (TypeError, ValueError):
            pass

    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value


def _normalise_days(value, default=250):
    try:
        return min(800, max(60, int(value if value is not None else default)))
    except (TypeError, ValueError) as exc:
        raise ValueError("回测周期必须为整数") from exc


def _new_strategy(parameters=None):
    config = strategy.get_config()
    if parameters:
        if not isinstance(parameters, dict):
            raise ValueError("策略参数必须为对象")
        config.update(parameters)
    return StrategyEngine(config)


def _selected_core_stocks():
    return [{
        "code": str(item.get("code", "")).strip(),
        "name": str(item.get("name", "")),
        "rank": item.get("recommendation_rank"),
        "selection_score": item.get("selection_score"),
    } for item in fundamental_screener.recommendations
        if str(item.get("code", "")).strip()]


def _run_case(code, name, days, execution_strategy, kline=None):
    engine = BacktestEngine(strategy=execution_strategy, data_feed=df)
    result = engine.run(
        code, name=name, days=days, with_benchmark=True, kline=kline
    )
    if result:
        result["strategy_name"] = execution_strategy.name
    return result


def _batch_backtest(candidates, days, parameters=None, histories=None):
    execution_strategy = _new_strategy(parameters)
    rows = []
    missing = []
    for candidate in candidates:
        code = candidate["code"]
        history = histories.get(code) if histories is not None else None
        result = _run_case(code, candidate.get("name", ""), days, execution_strategy, history)
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
        "strategy_name": execution_strategy.name,
        "parameters": execution_strategy.get_config(),
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


def _optimization_log(message, level="info"):
    entry = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "level": level,
        "message": message,
    }
    with _optimization_lock:
        _optimization_state["logs"].append(entry)


def _set_optimization_state(**updates):
    with _optimization_lock:
        _optimization_state.update(updates)


def _run_ai_optimization(candidates, days, parameters, rounds):
    try:
        _optimization_log(f"开始优化：{len(candidates)} 只核心股，{days} 个交易日；对照为每只股票同期买入持有。")
        histories = {}
        for index, candidate in enumerate(candidates, start=1):
            code = candidate["code"]
            history = df.get_kline(code, count=min(days, 800))
            if not history.empty:
                histories[code] = history
            _optimization_log(f"加载行情 {index}/{len(candidates)}：{code} {candidate.get('name', '')}")
        available = [candidate for candidate in candidates if candidate["code"] in histories]
        if not available:
            raise ValueError("核心股历史K线均不可用")

        baseline = _batch_backtest(available, days, parameters, histories)
        trials = [{
            "round": 0,
            "label": "当前参数",
            "parameters": baseline["parameters"],
            "summary": baseline["summary"],
        }]
        best = baseline
        _optimization_log(
            "基线完成：平均超额 {:+.2f}%，平均回撤 {:.2f}%，目标分 {:.2f}。".format(
                baseline["summary"]["avg_excess_return"],
                baseline["summary"]["avg_max_drawdown"],
                baseline["summary"]["objective_score"],
            )
        )

        for round_number in range(1, rounds + 1):
            _optimization_log(f"第 {round_number}/{rounds} 轮：请求 DeepSeek 提出参数候选。")
            proposal = ai_advisor.suggest_backtest_parameters(
                best["parameters"], best["summary"], trials
            )
            candidate_parameters = proposal["parameters"]
            _optimization_log(
                f"DeepSeek 建议：{proposal.get('reason', '未提供理由')}；开始实测候选参数。"
            )
            tested = _batch_backtest(available, days, candidate_parameters, histories)
            trial = {
                "round": round_number,
                "label": f"AI候选 {round_number}",
                "parameters": tested["parameters"],
                "summary": tested["summary"],
                "reason": proposal.get("reason", ""),
            }
            trials.append(trial)
            _optimization_log(
                "第 {} 轮结果：平均超额 {:+.2f}%，平均回撤 {:.2f}%，目标分 {:.2f}。".format(
                    round_number,
                    tested["summary"]["avg_excess_return"],
                    tested["summary"]["avg_max_drawdown"],
                    tested["summary"]["objective_score"],
                )
            )
            if tested["summary"]["objective_score"] > best["summary"]["objective_score"]:
                best = tested
                _optimization_log(f"第 {round_number} 轮成为当前最优方案。", "success")

        _optimization_log(
            "优化完成：最优平均超额 {:+.2f}%，目标分 {:.2f}。结果仅反映该历史区间，需样本外复核。".format(
                best["summary"]["avg_excess_return"],
                best["summary"]["objective_score"],
            ),
            "success",
        )
        _set_optimization_state(
            state="done",
            trials=trials,
            best=best,
            baseline=baseline,
            completed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
    except Exception as exc:
        _optimization_log(f"优化失败：{exc}", "error")
        _set_optimization_state(state="error", error=str(exc))


class ApiHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # 静默日志

    def _send_json(self, data, status=200):
        body = json.dumps(_json_safe(data), ensure_ascii=False, default=str,
                          allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html, status=200):
        body = html.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'public, max-age=86400')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, msg, status=500):
        self._send_json({"error": msg}, status)

    def _read_body(self):
        length = int(self.headers.get('Content-Length', 0))
        if length > 0:
            return self.rfile.read(length).decode('utf-8')
        return ''

    def _parse_path(self):
        parsed = urlparse(self.path)
        return parsed.path, parse_qs(parsed.query)

    def do_GET(self):
        path, params = self._parse_path()
        try:
            if path == '/' or path == '/index.html':
                self._serve_template()
            elif path.startswith('/static/'):
                self._serve_static(path)
            elif path == '/api/market-overview':
                self._market_overview()
            elif path == '/api/screen':
                self._screen_async(params)
            elif path == '/api/screen/status':
                self._screen_status()
            elif path == '/api/screen-config':
                self._screen_config()
            elif path == '/api/long-term-screen/status':
                self._long_term_screen_status()
            elif path == '/api/sector-flow':
                self._sector_flow()
            elif path == '/api/etf-list':
                self._etf_list()
            elif path == '/api/market-mood':
                self._market_mood()
            elif path == '/api/risk-dashboard':
                self._risk_dashboard()
            elif path == '/api/strategy-config':
                self._strategy_config()
            elif path == '/api/backtest/core-stocks':
                self._core_stocks()
            elif path == '/api/backtest/optimize/status':
                self._optimization_status()
            elif path.startswith('/api/backtest/'):
                code = path.replace('/api/backtest/', '')
                self._run_backtest(code, params)
            elif path == '/api/watchlist':
                self._send_json({"codes": watchlist.codes})
            elif path == '/api/watchlist/quotes':
                self._watchlist_quotes()
            elif path == '/api/portfolio':
                data = portfolio_manager.load()
                enriched = portfolio_manager.enrich(data)
                self._send_json(enriched)
            elif path == '/api/expectation-plans':
                self._expectation_plans()
            elif path == '/api/search':
                self._search(params)
            elif path.startswith('/api/kline/'):
                code = path.replace('/api/kline/', '')
                self._kline(code, params)
            elif path.startswith('/api/fund-flow/'):
                code = path.replace('/api/fund-flow/', '')
                self._send_json(df.get_fund_flow(code))
            elif path == '/api/realtime':
                self._realtime(params)
            elif path == '/api/status':
                self._send_json({"status": "ok",
                                 "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                 "watchlist": len(watchlist.codes),
                                 "broker_account_configured": guosen_client.public_status()["account_query_configured"],
                                 "data_sources": df.get_source_status()})
            elif path == '/api/ai/status':
                self._send_json({"configured": ai_advisor.is_configured,
                                 "model": ai_advisor.model})
            elif path == '/api/broker/status':
                self._send_json(guosen_client.public_status())
            elif path == '/api/broker/account':
                self._broker_query("account")
            elif path == '/api/broker/positions':
                self._broker_query("positions")
            elif path == '/api/broker/trades':
                self._broker_query("trades")
            elif path == '/api/broker/orders':
                self._broker_query("orders")
            else:
                self._send_error("Not found", 404)
        except Exception as e:
            self._send_error(str(e))

    def do_POST(self):
        path, _ = self._parse_path()
        body = self._read_body()
        try:
            if path == '/api/backtest/run':
                self._run_configured_backtest(body)
            elif path == '/api/backtest/core':
                self._run_core_backtest(body)
            elif path == '/api/backtest/optimize':
                self._start_optimization(body)
            elif path == '/api/investment-advice':
                self._investment_advice(body)
            elif path == '/api/settings':
                self._update_settings(body)
            elif path == '/api/analyze':
                self._analyze(body)
            elif path == '/api/watchlist/add':
                data = json.loads(body)
                watchlist.add(data['code'].strip())
                self._send_json({"success": True})
            elif path == '/api/watchlist/remove':
                data = json.loads(body)
                watchlist.remove(data['code'].strip())
                self._send_json({"success": True})
            elif path == '/api/ai/market-analysis':
                data = json.loads(body) if body else {}
                ai_advisor.api_key = data.get("api_key") or ai_advisor.api_key
                result = ai_advisor.analyze_market()
                self._send_json(result)
            elif path == '/api/ai/chat':
                data = json.loads(body) if body else {}
                message = data.get("message", "").strip()
                if not message:
                    self._send_error("请输入问题", 400)
                    return
                ai_advisor.api_key = data.get("api_key") or ai_advisor.api_key
                result = ai_advisor.chat_with_context(message)
                self._send_json(result)
            elif path == '/api/ai/analyze-stock':
                data = json.loads(body) if body else {}
                code = data.get("code", "").strip()
                if not code:
                    self._send_error("请输入股票代码", 400)
                    return
                ai_advisor.api_key = data.get("api_key") or ai_advisor.api_key
                result = ai_advisor.analyze_stock(code)
                self._send_json(result)
            elif path == '/api/ai/enhanced-screen':
                data = json.loads(body) if body else {}
                ai_advisor.api_key = data.get("api_key") or ai_advisor.api_key
                self._ai_enhanced_screen(data)
            elif path == '/api/ai/fundamental-screen':
                data = json.loads(body) if body else {}
                ai_advisor.api_key = data.get("api_key") or ai_advisor.api_key
                self._fundamental_screen(data)
            elif path == '/api/long-term-screen':
                data = json.loads(body) if body else {}
                ai_advisor.api_key = data.get("api_key") or ai_advisor.api_key
                self._long_term_screen_start(data)
            elif path == '/api/ai/config':
                data = json.loads(body) if body else {}
                key = data.get("api_key", "").strip()
                if key:
                    ai_advisor.api_key = key
                self._send_json({"success": True, "configured": ai_advisor.is_configured})
            elif path == '/api/portfolio/save':
                data = json.loads(body) if body else {}
                result = portfolio_manager.save(data)
                self._send_json(result)
            elif path == '/api/portfolio/analyze':
                req = json.loads(body) if body else {}
                portfolio_data = portfolio_manager.load()
                enriched = portfolio_manager.enrich(portfolio_data)
                api_key = req.get("api_key", "").strip()
                if api_key:
                    ai_advisor.api_key = api_key
                # 如果用户刚运行过尾盘潜伏，一并传给 AI 作为新增买入观察池。
                tail_recs = (tail_screener.last_result or {}).get("recommendations", [])
                if not tail_recs and hasattr(fundamental_screener, "recommendations"):
                    tail_recs = fundamental_screener.recommendations
                if tail_recs:
                    tail_data = [{
                        "code": r.get("code"), "name": r.get("name"),
                        "stage1_score": r.get("stage1_score", r.get("fundamental_score")),
                        "stage2_score": r.get("stage2_score", r.get("technical_score")),
                        "final_score": r.get("final_score", r.get("selection_score")),
                        "today_confirmation": r.get("today_confirmation", r.get("technical_reason", "")),
                    } for r in tail_recs[:10]]
                else:
                    tail_data = None
                result = ai_advisor.analyze_portfolio(enriched, tail_data)
                self._send_json(result)
            elif path == '/api/expectation-plan/save':
                self._expectation_plan_save(body)
            elif path == '/api/expectation-plan/delete':
                self._expectation_plan_delete(body)
            elif path == '/api/broker/config':
                self._broker_config(body)
            elif path == '/api/broker/test':
                self._broker_test()
            elif path == '/api/broker/order/preview':
                self._broker_order_preview(body)
            elif path == '/api/broker/order':
                self._broker_order_submit(body)
            elif path == '/api/tail-end-screen':
                result = tail_screener.screen()
                self._send_json(result)
            else:
                self._send_error("Not found", 404)
        except Exception as e:
            self._send_error(str(e))

    # ── 模板 ──
    def _serve_template(self):
        tmpl = os.path.join(TEMPLATE_DIR, 'index.html')
        if os.path.exists(tmpl):
            with open(tmpl, 'r', encoding='utf-8') as f:
                self._send_html(f.read())
        else:
            self._send_error("Template not found", 404)

    def _serve_static(self, path):
        rel = unquote(path[len('/static/'):]).replace('/', os.sep)
        static_root = os.path.abspath(STATIC_DIR)
        file_path = os.path.abspath(os.path.join(static_root, rel))
        if not file_path.startswith(static_root + os.sep) or not os.path.isfile(file_path):
            return self._send_error("Static file not found", 404)
        content_type = mimetypes.guess_type(file_path)[0] or 'application/octet-stream'
        with open(file_path, 'rb') as f:
            self._send_bytes(f.read(), content_type)

    # ── API 实现 ──
    def _market_overview(self):
        stocks = df.get_stock_list()
        if stocks is None or stocks.empty:
            return self._send_error("获取全市场数据失败")
        stats = {
            "total": len(stocks), "up": int((stocks["change_pct"] > 0).sum()),
            "down": int((stocks["change_pct"] < 0).sum()),
            "limit_up": int((stocks["change_pct"] >= 9.8).sum()),
            "limit_down": int((stocks["change_pct"] <= -9.8).sum()),
            "avg_change": round(stocks["change_pct"].mean(), 2),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        cols = ["code","name","price","change_pct","turnover_rate","market_cap"]
        gainers = stocks.nlargest(15, "change_pct")[cols].to_dict("records")
        losers = stocks.nsmallest(15, "change_pct")[cols].to_dict("records")
        for l in [gainers, losers]:
            for s in l:
                s["price"] = round(s["price"], 2) if s["price"] else 0
                s["change_pct"] = round(s["change_pct"], 2) if s["change_pct"] else 0
                s["market_cap"] = round(s["market_cap"], 1) if s["market_cap"] else 0
                s["turnover_rate"] = round(s["turnover_rate"], 2) if s["turnover_rate"] else 0
        self._send_json({"stats": stats, "gainers": gainers, "losers": losers,
                         "data_source": df.get_source_status()["market_snapshot"]})

    def _screen_async(self, params):
        global _screen_task, _screen_result
        for k in ["market_cap_min","market_cap_max","turnover_min","turnover_max"]:
            v = params.get(k, [None])[0]
            if v: SCREEN[k] = float(v)
        v = params.get("score_threshold", [None])[0]
        if v: STRATEGY["score_threshold"] = float(v)

        _screen_result = None
        def _run():
            global _screen_result
            try:
                _screen_result = screener.screen()
            except Exception as e:
                _screen_result = pd.DataFrame({"error": [str(e)]})

        _screen_task = threading.Thread(target=_run, daemon=True)
        _screen_task.start()
        self._send_json({"started": True, "status": "started"})

    def _screen_status(self):
        global _screen_task, _screen_result
        if _screen_task is None:
            return self._send_json({"state": "idle", "done": 0, "total": 0})

        if _screen_task.is_alive():
            progress = screener.progress
            progress_state = progress.get("state", "idle")
            if progress_state in ("scoring", "merging"):
                return self._send_json({**progress, "state": progress_state})
            return self._send_json({**progress, "state": "running"})

        if _screen_result is not None:
            if isinstance(_screen_result, pd.DataFrame) and not _screen_result.empty:
                if "error" in _screen_result.columns:
                    return self._send_json({"state": "error", "message": str(_screen_result.iloc[0]["error"])})
                return self._send_json({
                    "state": "done",
                    "stocks": _screen_result.to_dict("records"),
                    "count": len(_screen_result),
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                })
            return self._send_json({"state": "done", "stocks": [], "count": 0,
                                     "message": "无符合条件标的",
                                     "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})

        self._send_json({"state": "done", "stocks": [], "count": 0})

    def _ai_enhanced_screen(self, data):
        global _screen_result
        stocks = df.get_stock_list()
        if stocks.empty:
            return self._send_error("获取市场数据失败")

        codes = data.get("codes", [])
        if codes:
            candidates = stocks[stocks["code"].isin(codes)]
        else:
            candidates = stocks.nlargest(30, "amount")[
                (stocks["change_pct"] > 0) &
                (stocks["turnover_rate"] >= SCREEN["turnover_min"]) &
                (stocks["turnover_rate"] <= SCREEN["turnover_max"])
            ]
            if SCREEN["exclude_st"]:
                candidates = candidates[~candidates["is_st"]]

        if candidates.empty:
            return self._send_error("无符合条件的候选标的")

        top = candidates.head(20)
        candidate_list = []
        for _, s in top.iterrows():
            candidate_list.append(
                f"{s['code']} {s['name']} 价格{s['price']} 涨跌幅{s['change_pct']:+.2f}% "
                f"换手率{s['turnover_rate']:.1f}% 市值{s['market_cap']:.0f}亿 [{s['board']}]"
            )

        concepts = df.get_hot_concepts(10)
        concept_text = "热门概念: " + ", ".join(
            f"{c['name']}({c['change_pct']:+.1f}%)" for c in concepts
        ) if concepts else ""

        prompt = f"""当前市场数据：

候选标的（按成交额排序）：
{chr(10).join(candidate_list[:15])}

{concept_text}

请从以上候选标的中，结合以下维度进行精选分析：
1. 哪些标的所处板块与当前热点概念最匹配？
2. 哪些标的从价格、换手率、市值角度看最具短线爆发潜力？
3. 按优先级推荐 5 只股票，每只给出：
   - 股票代码和名称
   - 推荐理由（结合板块+资金面）
   - 预期收益空间
   - 止损建议

用中文回复，格式清晰，具体到每只股票。"""

        messages = [
            {"role": "system", "content": ai_advisor.system_prompt},
            {"role": "user", "content": prompt},
        ]
        response = ai_advisor._call_ai(messages)
        self._send_json({"response": response, "candidates": len(candidate_list)})

    def _fundamental_screen(self, data):
        """兼容旧入口：立即开始新的中长期异步任务。"""
        self._long_term_screen_start(data)

    def _long_term_screen_start(self, data):
        global _fundamental_task, _fundamental_result
        if _fundamental_task is not None and _fundamental_task.is_alive():
            return self._send_json({"started": False, "state": "running"})

        requested_limit = data.get("universe_limit")
        try:
            universe_limit = max(1, int(requested_limit)) if requested_limit else None
        except (ValueError, TypeError):
            universe_limit = None
        _fundamental_result = None
        fundamental_screener.recommendations = []

        def _run():
            global _fundamental_result
            try:
                _fundamental_result = fundamental_screener.screen(
                    universe_limit=universe_limit
                )
            except Exception as exc:
                _fundamental_result = {"error": str(exc)}

        _fundamental_task = threading.Thread(target=_run, daemon=True)
        _fundamental_task.start()
        self._send_json({
            "started": True,
            "state": "started",
            "universe_limit": universe_limit or 0,
            "scan_scope": "全部初筛股票" if universe_limit is None else f"诊断限制 {universe_limit} 只",
            "holding_horizon": LONG_TERM["holding_horizon"],
        })

    def _long_term_screen_status(self):
        global _fundamental_task, _fundamental_result
        if _fundamental_task is None:
            return self._send_json({"state": "idle", "done": 0, "total": 0})
        if _fundamental_task.is_alive():
            return self._send_json(fundamental_screener.progress)
        if isinstance(_fundamental_result, dict) and _fundamental_result.get("error"):
            return self._send_json({
                "state": "error", "message": _fundamental_result["error"]
            })
        results = _fundamental_result or []
        self._send_json({
            "state": "done",
            "results": results,
            "count": len(results),
            "recommendations": fundamental_screener.recommendations,
            "summary": fundamental_screener.summary,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

    def _screen_config(self):
        self._send_json({
            "market_cap_min": SCREEN["market_cap_min"], "market_cap_max": SCREEN["market_cap_max"],
            "price_min": SCREEN["price_min"], "price_max": SCREEN["price_max"],
            "turnover_min": SCREEN["turnover_min"], "turnover_max": SCREEN["turnover_max"],
            "score_threshold": STRATEGY["score_threshold"], "max_stocks": STRATEGY["max_stocks"],
            "stop_loss": RISK["stop_loss"], "take_profit": RISK["take_profit"],
            "holding_horizon": LONG_TERM["holding_horizon"],
            "fundamental_universe_limit": LONG_TERM["universe_limit"],
            "fundamental_minimum_score": LONG_TERM["minimum_score"],
        })

    def _sector_flow(self):
        sector = df.get_sector_fund_flow(30)
        concept = df.get_concept_fund_flow(20)
        self._send_json({
            "sectors": sector.to_dict("records") if not sector.empty else [],
            "concepts": concept.to_dict("records") if not concept.empty else [],
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

    def _etf_list(self):
        etf = df.get_etf_list()
        self._send_json({"etfs": etf.to_dict("records") if not etf.empty else []})

    def _market_mood(self):
        self._send_json(df.get_market_metrics())

    def _strategy_config(self):
        self._send_json({
            "strategy": strategy.get_config(),
            "name": "小资金稳健策略",
            "risk": dict(RISK),
            "backtest": dict(BACKTEST),
            "screen": dict(SCREEN),
            "market_rules": [
                "沪深股票买入按100股整数倍校验",
                "风控只根据国信真实账户返回的资金、持仓和成交评估",
                "真实委托必须执行授权接口校验，并遵守涨跌停与T+1规则",
            ],
        })

    def _risk_dashboard(self):
        self._send_json(risk_analyzer.analyze())

    def _expectation_plans(self):
        portfolio_data = portfolio_manager.load()
        enriched_portfolio = portfolio_manager.enrich(portfolio_data)
        tail_result = tail_screener.last_result or {}
        self._send_json(expectation_planner.evaluate_all(
            watchlist_codes=watchlist.codes,
            portfolio=enriched_portfolio,
            tail_result=tail_result,
        ))

    def _expectation_plan_save(self, body):
        data = json.loads(body) if body else {}
        result = expectation_planner.save_plan(data)
        portfolio_data = portfolio_manager.load()
        enriched_portfolio = portfolio_manager.enrich(portfolio_data)
        evaluated = expectation_planner.evaluate_plan(
            result["plan"],
            self._expectation_source_match(result["plan"].get("code"), enriched_portfolio),
        )
        self._send_json({"success": True, "plan": evaluated})

    def _expectation_plan_delete(self, body):
        data = json.loads(body) if body else {}
        plan_id = str(data.get("id", "")).strip()
        if not plan_id:
            return self._send_error("缺少计划ID", 400)
        self._send_json(expectation_planner.delete_plan(plan_id))

    def _expectation_source_match(self, code, enriched_portfolio):
        sources = expectation_planner.build_sources(
            watchlist_codes=watchlist.codes,
            portfolio=enriched_portfolio,
            tail_result=tail_screener.last_result or {},
        )
        code = str(code or "").zfill(6)
        for group in sources.values():
            for item in group:
                if item.get("code") == code:
                    return item
        return None

    def _watchlist_quotes(self):
        if not watchlist.codes:
            return self._send_json({"quotes": []})
        quotes = df.get_realtime_quotes(watchlist.codes)
        self._send_json({
            "quotes": quotes.to_dict("records") if not quotes.empty else [],
        })

    def _search(self, params):
        query = self._get_param(params, "q").strip().upper()
        if not query:
            return self._send_json({"stocks": []})
        stocks = df.get_stock_list()
        if stocks.empty:
            return self._send_json({"stocks": []})
        code_match = stocks["code"].astype(str).str.contains(query, regex=False)
        name_match = stocks["name"].astype(str).str.contains(query, case=False, na=False, regex=False)
        columns = ["code", "name", "price", "change_pct", "board"]
        self._send_json({
            "stocks": stocks[code_match | name_match].head(20)[columns].to_dict("records"),
        })

    def _kline(self, code, params):
        if not (len(code) == 6 and code.isdigit()):
            return self._send_error("股票代码必须为6位数字", 400)
        period = self._get_param(params, "period", "day")
        if period not in ("day", "week", "month"):
            return self._send_error("K线周期只支持 day、week 或 month", 400)
        try:
            count = min(800, max(30, int(self._get_param(params, "count", "120"))))
        except ValueError:
            return self._send_error("K线条数必须为整数", 400)

        kline = df.get_kline(code, period=period, count=count)
        if kline.empty:
            return self._send_error("获取K线失败")

        stock_info = {}
        quotes = df.get_realtime_quotes([code])
        if not quotes.empty:
            quote = quotes.iloc[0]
            stock_info = {
                "code": code,
                "name": quote.get("name", ""),
                "price": round(float(quote.get("price") or 0), 2),
                "change_pct": round(float(quote.get("change_pct") or 0), 2),
                "market_cap": round(float(quote.get("market_cap") or 0), 1),
                "turnover_rate": round(float(quote.get("turnover_rate") or 0), 2),
                "pe": round(float(quote.get("pe") or 0), 2),
            }
        stocks = df.get_stock_list()
        if not stocks.empty:
            matched = stocks[stocks["code"] == code]
            if not matched.empty:
                quote = matched.iloc[0]
                stock_info.setdefault("code", code)
                stock_info["name"] = stock_info.get("name") or quote.get("name", "")
                stock_info["price"] = stock_info.get("price") or round(float(quote.get("price") or 0), 2)
                stock_info["change_pct"] = stock_info.get("change_pct") or round(float(quote.get("change_pct") or 0), 2)
                stock_info["market_cap"] = stock_info.get("market_cap") or round(float(quote.get("market_cap") or 0), 1)
                stock_info["turnover_rate"] = stock_info.get("turnover_rate") or round(float(quote.get("turnover_rate") or 0), 2)
                stock_info["pe"] = stock_info.get("pe") or round(float(quote.get("pe") or 0), 2)

        bars = []
        indicators = [
            "MA5", "MA10", "MA20", "MA60", "MACD_DIF", "MACD_DEA", "MACD_BAR",
            "RSI", "KDJ_K", "KDJ_D", "KDJ_J", "BOLL_UP", "BOLL_MID", "BOLL_DN",
        ]
        for _, row in kline.iterrows():
            item = {
                "date": row["date"].strftime("%Y-%m-%d") if hasattr(row["date"], "strftime") else str(row["date"])[:10],
                "open": round(float(row["open"]), 2),
                "close": round(float(row["close"]), 2),
                "high": round(float(row["high"]), 2),
                "low": round(float(row["low"]), 2),
                "volume": float(row["volume"]),
            }
            for column in indicators:
                if column in kline.columns and not pd.isna(row.get(column)):
                    item[column] = round(float(row[column]), 2)
            bars.append(item)

        fallback_price = float(kline.iloc[-1]["close"])
        signal = strategy.generate_buy_signals(
            kline, stock_info or {"code": code, "price": fallback_price}
        )
        signal_data = {"has_signal": False}
        if signal:
            signal_data = {
                "has_signal": True,
                "type": signal.signal,
                "reason": signal.reason,
                "score": signal.score,
                "stop_loss": round(signal.price * (1 + strategy.stop_loss_pct), 2),
                "take_profit": round(signal.price * (1 + strategy.take_profit_pct), 2),
            }
        self._send_json({
            "stock": stock_info,
            "kline": bars,
            "fund_flow": df.get_fund_flow(code),
            "signal": signal_data,
        })

    def _realtime(self, params):
        codes = [
            code.strip() for code in self._get_param(params, "codes").split(",")
            if code.strip()
        ]
        if not codes:
            return self._send_error("请提供股票代码", 400)
        quotes = df.get_realtime_quotes(codes)
        self._send_json({
            "quotes": quotes.to_dict("records") if not quotes.empty else [],
        })

    def _update_settings(self, body):
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return self._send_error("参数不是有效 JSON", 400)
        risk_data = data.get("risk", {})
        backtest_data = data.get("backtest", {})
        updated_risk = dict(RISK)
        updated_backtest = dict(BACKTEST)
        try:
            for key in ("position_pct", "max_exposure", "stop_loss", "take_profit", "max_drawdown"):
                if key in risk_data:
                    updated_risk[key] = float(risk_data[key])
            for key in ("max_positions", "max_trades_per_day"):
                if key in risk_data:
                    updated_risk[key] = int(risk_data[key])
            for key in ("initial_capital", "commission", "stamp_tax", "slippage"):
                if key in backtest_data:
                    updated_backtest[key] = float(backtest_data[key])
        except (TypeError, ValueError):
            return self._send_error("运行参数包含无效数字", 400)

        if not 0 < updated_risk["position_pct"] <= updated_risk["max_exposure"] <= 1:
            return self._send_error("仓位比例必须大于0，且单票仓位不得超过组合仓位", 400)
        if not -1 < updated_risk["stop_loss"] < 0 or not 0 < updated_risk["take_profit"] <= 1:
            return self._send_error("止损必须为负值，止盈必须为正值", 400)
        if not -1 < updated_risk["max_drawdown"] < 0:
            return self._send_error("组合最大回撤必须为负值", 400)
        if updated_risk["max_positions"] < 1 or updated_risk["max_trades_per_day"] < 1:
            return self._send_error("持仓数和每日交易数必须至少为1", 400)
        if updated_backtest["initial_capital"] <= 0:
            return self._send_error("初始资金必须大于0", 400)
        if any(not 0 <= updated_backtest[key] < 0.1 for key in ("commission", "stamp_tax", "slippage")):
            return self._send_error("成交成本比例超出有效范围", 400)
        try:
            validated_strategy = StrategyEngine({
                **strategy.get_config(),
                "position_size_pct": updated_risk["position_pct"],
                "stop_loss": updated_risk["stop_loss"],
                "take_profit": updated_risk["take_profit"],
            })
        except ValueError as exc:
            return self._send_error(str(exc), 400)

        RISK.update(updated_risk)
        BACKTEST.update(updated_backtest)
        strategy.position_size_pct = validated_strategy.position_size_pct
        strategy.stop_loss_pct = validated_strategy.stop_loss_pct
        strategy.take_profit_pct = validated_strategy.take_profit_pct
        self._send_json({
            "success": True,
            "message": "运行参数已更新；回测仅使用小资金稳健策略。",
        })

    def _analyze(self, body):
        if not body.strip():
            return self._send_error("请输入交易记录", 400)
        try:
            records = pd.read_csv(StringIO(body)).to_dict("records")
        except Exception as exc:
            return self._send_error(f"解析失败: {exc}", 400)
        if not records:
            return self._send_error("无有效数据", 400)
        self._send_json(analyzer.analyze_trades(records))

    def _broker_query(self, resource):
        try:
            result = getattr(guosen_client, resource)()
            self._send_json(result)
        except (AttributeError, BrokerError) as exc:
            self._send_error(str(exc), 400)

    def _broker_config(self, body):
        try:
            data = json.loads(body) if body else {}
            self._send_json(guosen_client.configure(data))
        except (json.JSONDecodeError, BrokerError) as exc:
            self._send_error(str(exc), 400)

    def _broker_test(self):
        try:
            self._send_json(guosen_client.test_connection())
        except BrokerError as exc:
            self._send_error(str(exc), 400)

    def _broker_order_preview(self, body):
        try:
            data = json.loads(body) if body else {}
            self._send_json(guosen_client.preview_order(data))
        except (json.JSONDecodeError, BrokerError) as exc:
            self._send_error(str(exc), 400)

    def _broker_order_submit(self, body):
        try:
            data = json.loads(body) if body else {}
            self._send_json(guosen_client.submit_order(data))
        except (json.JSONDecodeError, BrokerError) as exc:
            self._send_error(str(exc), 400)

    def _investment_advice(self, body):
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return self._send_error("请求不是有效 JSON", 400)
        key = str(data.get("api_key") or "").strip()
        if key:
            ai_advisor.api_key = key
        candidates = [dict(item) for item in fundamental_screener.recommendations]
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if not candidates:
            return self._send_json({
                "summary": "请先运行全面选股，产生精选标的后再生成建议。",
                "market_mood": "待选股",
                "sectors": [],
                "stocks": [],
                "risk_alerts": ["未生成精选标的，不能形成仓位建议。"],
                "total_position": 0,
                "analysis_mode": "等待选股",
                "timestamp": timestamp,
            })

        mode = "规则精选建议"
        if data.get("use_ai", True) and ai_advisor.is_configured:
            candidates = ai_advisor.explain_long_term_candidates(candidates)
            mode = "DeepSeek摘要 + 规则精选"

        position = round(min(
            RISK["position_pct"] * 100,
            RISK["max_exposure"] * 100 / max(len(candidates), 1),
        ), 1)
        stocks = []
        risk_alerts = []
        for item in candidates:
            reason = "；".join(part for part in (
                item.get("fundamental_reason", ""),
                item.get("technical_reason", ""),
                "资金匹配: " + "、".join(item.get("matched_themes", []))
                if item.get("matched_themes") else "",
                item.get("ai_summary", ""),
            ) if part)
            stocks.append({
                "code": item.get("code", ""),
                "name": item.get("name", ""),
                "signal": "小资金策略观察",
                "score": item.get("selection_score", item.get("composite_score", 0)),
                "weight": position,
                "reason": reason or "通过综合规则筛选，等待入场信号确认。",
            })
            risk = item.get("ai_risk_note") or item.get("risk")
            if risk and risk not in risk_alerts:
                risk_alerts.append(f"{item.get('name') or item.get('code')}: {risk}")

        sectors = []
        for board in fundamental_screener.summary.get("rotation_boards", [])[:3]:
            net = board.get("recent_main_net_inflow", board.get("main_net_inflow"))
            flow = f"，净流入{float(net):+.2f}亿" if net is not None else ""
            sectors.append({
                "name": board.get("name", "-"),
                "weight": round(position * len(candidates) / 3, 1),
                "reason": f"资金热点匹配{flow}",
            })
        self._send_json({
            "summary": f"基于全面选股结果，列出 {len(stocks)} 只小资金策略观察标的；入场仍须满足单一策略信号。",
            "market_mood": "以资金与风险阈值为准",
            "sectors": sectors,
            "stocks": stocks,
            "risk_alerts": risk_alerts[:10] or ["仍需核验公告、成交容量与止损执行条件。"],
            "total_position": round(position * len(stocks), 1),
            "analysis_mode": mode,
            "timestamp": timestamp,
        })

    def _core_stocks(self):
        candidates = _selected_core_stocks()
        self._send_json({
            "stocks": candidates,
            "count": len(candidates),
            "message": "请先在选股页运行全面选股以生成核心股。"
            if not candidates else "已加载选股页产生的核心股。",
        })

    def _run_configured_backtest(self, body):
        try:
            data = json.loads(body) if body else {}
            code = str(data.get("code", "")).strip()
            if not (len(code) == 6 and code.isdigit()):
                return self._send_error("股票代码必须为6位数字", 400)
            days = _normalise_days(data.get("days"))
            execution_strategy = _new_strategy(data.get("parameters"))
        except (json.JSONDecodeError, ValueError) as exc:
            return self._send_error(str(exc), 400)
        name = ""
        stocks = df.get_stock_list()
        if not stocks.empty:
            match = stocks[stocks["code"] == code]
            if not match.empty:
                name = str(match.iloc[0].get("name", ""))
        result = _run_case(code, name, days, execution_strategy)
        if not result:
            return self._send_error("回测失败：历史K线不足或数据源不可用")
        self._send_json(result)

    def _run_core_backtest(self, body):
        candidates = _selected_core_stocks()
        if not candidates:
            return self._send_error("请先在选股页运行全面选股，生成核心股后再进行一键回测", 409)
        try:
            data = json.loads(body) if body else {}
            days = _normalise_days(data.get("days"))
            result = _batch_backtest(candidates, days, data.get("parameters"))
        except (json.JSONDecodeError, ValueError) as exc:
            return self._send_error(str(exc), 400)
        self._send_json(result)

    def _start_optimization(self, body):
        global _optimization_task, _optimization_state
        candidates = _selected_core_stocks()
        if not candidates:
            return self._send_error("请先在选股页运行全面选股，生成核心股后再启动优化", 409)
        try:
            data = json.loads(body) if body else {}
            days = _normalise_days(data.get("days"))
            parameters = _new_strategy(data.get("parameters")).get_config()
            rounds = min(5, max(1, int(data.get("rounds", 3))))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            return self._send_error(str(exc), 400)
        key = str(data.get("api_key") or "").strip()
        if key:
            ai_advisor.api_key = key
        if not ai_advisor.is_configured:
            return self._send_error("请先配置 DeepSeek API Key 后再启动 AI 优化", 400)
        with _optimization_lock:
            if _optimization_task is not None and _optimization_task.is_alive():
                return self._send_error("AI 优化任务正在运行", 409)
            _optimization_state = {
                "state": "running",
                "logs": [],
                "trials": [],
                "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "rounds": rounds,
            }
            _optimization_task = threading.Thread(
                target=_run_ai_optimization,
                args=(candidates, days, parameters, rounds),
                daemon=True,
            )
            _optimization_task.start()
        self._send_json({"started": True, "state": "running", "rounds": rounds})

    def _optimization_status(self):
        with _optimization_lock:
            state = dict(_optimization_state)
            state["logs"] = list(_optimization_state.get("logs", []))
            state["trials"] = list(_optimization_state.get("trials", []))
        self._send_json(state)

    def _run_backtest(self, code, params):
        if not (len(code) == 6 and code.isdigit()):
            return self._send_error("股票代码必须为6位数字", 400)
        try:
            days = min(800, max(60, int(self._get_param(params, "days", "250"))))
        except ValueError:
            return self._send_error("回测周期必须为整数", 400)
        name = ""
        stocks = df.get_stock_list()
        if not stocks.empty:
            match = stocks[stocks["code"] == code]
            if not match.empty:
                name = str(match.iloc[0].get("name", ""))

        execution_strategy = _new_strategy()
        result = _run_case(code, name, days, execution_strategy)
        if not result:
            return self._send_error("回测失败：历史K线不足或数据源不可用")

        self._send_json(result)

    @staticmethod
    def _get_param(params: dict, key: str, default: str = "") -> str:
        values = params.get(key)
        return values[0] if values else default

def main():
    port = int(os.environ.get("PORT", 5000))
    server = ThreadingHTTPServer(("0.0.0.0", port), ApiHandler)
    print("A股中长期研究与交易系统")
    print(f"   http://localhost:{port}")
    print(f"   数据源: 东方财富 / 腾讯财经 / 新浪财经 (失败时降级)")
    live_status = "已启用" if guosen_client.live_enabled else "默认关闭(需设置 GUOSEN_ENABLE_LIVE_TRADING=YES)"
    ai_status = "已配置" if ai_advisor.is_configured else "未配置"
    print(f"   实盘交易: {live_status}")
    print(f"   DeepSeek AI: {ai_status}")
    print("   运行状态: /api/status")
    print("=" * 50)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
