#!/usr/bin/env python3
"""A股研究与交易系统 - Flask 兼容可视化入口。"""
import os, sys, json, traceback
from datetime import datetime
import pandas as pd
from flask import Flask, jsonify, render_template, request
from config import SCREEN, INDICATORS, STRATEGY, RISK, OUTPUT, BACKTEST
from data_feed import DataFeed
from screener import StockScreener
from strategy import StrategyEngine, SignalType
from backtest import BacktestEngine
from analysis import Analyzer
from watchlist import watchlist
from screener_tail import TailEndScreener

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True

df = DataFeed()
strategy_engine = StrategyEngine()
analyzer = Analyzer()
tail_screener = TailEndScreener(data_feed=df)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/market-overview")
def api_market_overview():
    try:
        stocks = df.get_stock_list()
        if stocks.empty:
            return jsonify({"error": "获取数据失败"})
        stats = {
            "total": len(stocks),
            "up": int((stocks["change_pct"] > 0).sum()),
            "down": int((stocks["change_pct"] < 0).sum()),
            "flat": int((stocks["change_pct"] == 0).sum()),
            "limit_up": int((stocks["change_pct"] >= 9.8).sum()),
            "limit_down": int((stocks["change_pct"] <= -9.8).sum()),
            "avg_change": round(stocks["change_pct"].mean(), 2),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        top_gainers = stocks.nlargest(15, "change_pct")[
            ["code","name","price","change_pct","turnover_rate","market_cap"]
        ].to_dict("records")
        top_losers = stocks.nsmallest(15, "change_pct")[
            ["code","name","price","change_pct","turnover_rate","market_cap"]
        ].to_dict("records")
        for l in [top_gainers, top_losers]:
            for s in l:
                s["price"] = round(s["price"], 2) if s["price"] else 0
                s["change_pct"] = round(s["change_pct"], 2) if s["change_pct"] else 0
                s["market_cap"] = round(s["market_cap"], 1) if s["market_cap"] else 0
                s["turnover_rate"] = round(s["turnover_rate"], 2) if s["turnover_rate"] else 0
        return jsonify({"stats": stats, "gainers": top_gainers, "losers": top_losers,
                        "data_source": df.get_source_status()["market_snapshot"]})
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/sector-flow")
def api_sector_flow():
    try:
        sector = df.get_sector_fund_flow(30)
        if sector.empty:
            return jsonify({"sectors": []})
        concept = df.get_concept_fund_flow(20)
        return jsonify({
            "sectors": sector.to_dict("records") if not sector.empty else [],
            "concepts": concept.to_dict("records") if not concept.empty else [],
        })
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/etf-list")
def api_etf_list():
    try:
        etf = df.get_etf_list()
        if etf.empty:
            return jsonify({"etfs": []})
        return jsonify({"etfs": etf.to_dict("records")})
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/market-mood")
def api_market_mood():
    try:
        return jsonify(df.get_market_metrics())
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/fund-flow/<code>")
def api_fund_flow(code):
    return jsonify(df.get_fund_flow(code))


@app.route("/api/screen")
def api_screen():
    try:
        for k in ["market_cap_min","market_cap_max","turnover_min","turnover_max"]:
            v = request.args.get(k)
            if v: SCREEN[k] = float(v)
        v = request.args.get("score_threshold")
        if v: STRATEGY["score_threshold"] = float(v)

        screener = StockScreener()
        result = screener.screen()
        if result.empty:
            return jsonify({"stocks": [], "count": 0, "message": "无符合条件标的"})
        return jsonify({
            "stocks": result.to_dict("records"),
            "count": len(result),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/tail-end-screen", methods=["POST"])
def api_tail_end_screen():
    try:
        return jsonify(tail_screener.screen())
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/screen-config")
def api_screen_config():
    return jsonify({
        "market_cap_min": SCREEN["market_cap_min"],
        "market_cap_max": SCREEN["market_cap_max"],
        "price_min": SCREEN["price_min"],
        "price_max": SCREEN["price_max"],
        "turnover_min": SCREEN["turnover_min"],
        "turnover_max": SCREEN["turnover_max"],
        "avg_amount_min": SCREEN["avg_amount_min"],
        "score_threshold": STRATEGY["score_threshold"],
        "max_stocks": STRATEGY["max_stocks"],
        "stop_loss": RISK["stop_loss"],
        "take_profit": RISK["take_profit"],
        "max_positions": RISK["max_positions"],
    })


@app.route("/api/kline/<code>")
def api_kline(code):
    try:
        period = request.args.get("period", "day")
        count = int(request.args.get("count", 120))
        kline = df.get_kline(code, period=period, count=count)
        if kline.empty:
            return jsonify({"error": "获取K线失败"})
        stock_info = {}
        quotes = df.get_realtime_quotes([code])
        if not quotes.empty:
            s = quotes.iloc[0]
            stock_info = {
                "code": code, "name": s.get("name", ""),
                "price": round(s.get("price") or 0, 2),
                "change_pct": round(s.get("change_pct") or 0, 2),
                "market_cap": 0,
                "turnover_rate": round(s.get("turnover_rate") or 0, 2),
                "pe": round(s.get("pe") or 0, 2),
            }
        stocks = df._stock_list_cache
        if stocks is not None and not stocks.empty:
            m = stocks[stocks["code"] == code]
            if not m.empty:
                s = m.iloc[0]
                if not stock_info:
                    stock_info = {
                        "code": code, "name": s.get("name", ""),
                        "price": round(s.get("price") or 0, 2),
                        "change_pct": round(s.get("change_pct") or 0, 2),
                        "market_cap": round(s.get("market_cap") or 0, 1),
                        "turnover_rate": round(s.get("turnover_rate") or 0, 2),
                        "pe": round(s.get("pe") or 0, 2),
                    }
                else:
                    stock_info["name"] = stock_info.get("name") or s.get("name", "")
                    stock_info["market_cap"] = round(s.get("market_cap") or 0, 1)
                    stock_info["turnover_rate"] = stock_info.get("turnover_rate") or round(s.get("turnover_rate") or 0, 2)
                    stock_info["pe"] = stock_info.get("pe") or round(s.get("pe") or 0, 2)
        kline_data = []
        for _, row in kline.iterrows():
            item = {
                "date": row["date"].strftime("%Y-%m-%d") if hasattr(row["date"],"strftime") else str(row["date"]),
                "open": round(float(row["open"]),2), "close": round(float(row["close"]),2),
                "high": round(float(row["high"]),2), "low": round(float(row["low"]),2),
                "volume": float(row["volume"]),
            }
            for col in ["MA5","MA10","MA20","MA60","MACD_DIF","MACD_DEA","MACD_BAR",
                        "RSI","KDJ_K","KDJ_D","KDJ_J","BOLL_UP","BOLL_MID","BOLL_DN"]:
                if col in kline.columns and not pd.isna(row.get(col)):
                    item[col] = round(float(row[col]), 2)
            kline_data.append(item)

        sig = strategy_engine.generate_buy_signals(kline, stock_info or {"code":code,"price":0})
        fund_flow = df.get_fund_flow(code)
        return jsonify({
            "stock": stock_info, "kline": kline_data, "fund_flow": fund_flow,
            "signal": {
                "has_signal": sig is not None,
                "type": sig.signal if sig else "",
                "reason": sig.reason if sig else "",
                "score": sig.score if sig else 0,
                "stop_loss": round(sig.price * (1 + strategy_engine.stop_loss_pct), 2) if sig else 0,
                "take_profit": round(sig.price * (1 + strategy_engine.take_profit_pct), 2) if sig else 0,
            } if sig else {"has_signal": False},
        })
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/backtest", methods=["POST"])
def api_backtest():
    try:
        data = request.get_json() or {}
        codes = data.get("codes", [])
        days = int(data.get("days", 120))
        if not codes:
            return jsonify({"error": "请提供股票代码"})
        results = []
        for code in codes:
            result = BacktestEngine().run(str(code).strip(), days=days, with_benchmark=True)
            if result:
                results.append({
                    "代码": result["code"],
                    "总收益率%": result["total_return"],
                    "年化收益率%": result["annual_return"],
                    "最大回撤%": result["max_drawdown"],
                    "胜率%": result["win_rate"],
                    "夏普比率": result["sharpe_ratio"],
                    "交易次数": result["trade_count"],
                    "盈亏比": result["profit_loss_ratio"],
                })
        if not results:
            return jsonify({"results": [], "message": "回测无结果"})
        return jsonify({"results": results, "count": len(results)})
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/backtest/<code>")
def api_backtest_single(code):
    try:
        days = int(request.args.get("days", 120))
        result = BacktestEngine().run(code, "", days=days)
        if not result:
            return jsonify({"error": "回测失败"})
        trades = result.get("trades", [])
        for t in trades:
            for k in ["买入价","卖出价","盈亏"]:
                if k in t and t[k] is not None:
                    t[k] = round(float(t[k]), 2) if not isinstance(t[k], str) else t[k]
        return jsonify({
            "code": result["code"], "name": result.get("name",""),
            "total_return": result["total_return"], "annual_return": result["annual_return"],
            "max_drawdown": result["max_drawdown"], "win_rate": result["win_rate"],
            "sharpe_ratio": result["sharpe_ratio"], "trade_count": result["trade_count"],
            "profit_loss_ratio": result["profit_loss_ratio"],
            "trades": trades, "equity_curve": result.get("equity_curve",[]),
        })
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/search")
def api_search():
    try:
        q = request.args.get("q","").strip().upper()
        if not q:
            return jsonify({"stocks":[]})
        stocks = df.get_stock_list()
        if stocks.empty:
            return jsonify({"stocks":[]})
        result = stocks[stocks["code"].str.contains(q) | stocks["name"].str.contains(q,na=False)].head(20)
        return jsonify({"stocks": result[["code","name","price","change_pct","board"]].to_dict("records")})
    except Exception as e:
        return jsonify({"error":str(e)})


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    try:
        text = request.get_data(as_text=True)
        if not text.strip():
            return jsonify({"error":"请输入交易记录"})
        from io import StringIO
        df_t = pd.read_csv(StringIO(text))
        if df_t.empty:
            return jsonify({"error":"无有效数据"})
        analysis = analyzer.analyze_trades(df_t.to_dict("records"))
        if "error" in analysis:
            return jsonify(analysis)
        return jsonify({
            "总交易次数": analysis["总交易次数"], "胜率": analysis["胜率"],
            "总盈亏": analysis["总盈亏%"], "盈亏比": analysis["盈亏比"],
            "平均盈利": analysis["平均盈利%"], "平均亏损": analysis["平均亏损%"],
            "最大回撤": analysis["最大回撤%"],
            "策略分析": analysis.get("策略分析", {}),
        })
    except Exception as e:
        return jsonify({"error": f"解析失败: {str(e)}"})


# === 自选股 ===

@app.route("/api/watchlist")
def api_get_watchlist():
    return jsonify({"codes": watchlist.codes})


@app.route("/api/watchlist/add", methods=["POST"])
def api_add_watchlist():
    watchlist.add(request.get_json()["code"].strip())
    return jsonify({"success": True})


@app.route("/api/watchlist/remove", methods=["POST"])
def api_remove_watchlist():
    watchlist.remove(request.get_json()["code"].strip())
    return jsonify({"success": True})


@app.route("/api/watchlist/quotes")
def api_watchlist_quotes():
    codes = watchlist.codes
    if not codes:
        return jsonify({"quotes": []})
    quotes = df.get_realtime_quotes(codes)
    return jsonify({"quotes": quotes.to_dict("records") if not quotes.empty else []})


@app.route("/api/realtime")
def api_realtime():
    codes_str = request.args.get("codes","")
    if not codes_str:
        return jsonify({"error":"请提供股票代码"})
    quotes = df.get_realtime_quotes([c.strip() for c in codes_str.split(",")])
    if quotes.empty:
        return jsonify({"error":"获取行情失败","quotes":[]})
    return jsonify({"quotes": quotes.to_dict("records")})


@app.route("/api/status")
def api_status():
    return jsonify({"status":"ok","time":datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "watchlist":len(watchlist.codes),
                    "data_sources":df.get_source_status()})


if __name__ == "__main__":
    print("\\n  A股研究与交易系统  http://localhost:5000\\n")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
