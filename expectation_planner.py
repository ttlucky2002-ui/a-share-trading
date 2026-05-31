"""
交易预期计划模块 - ExpectationPlanner
====================================

把“先做预期、再客观执行”的短线心法落成可复盘的数据结构。
计划持久化在 expectation_plans.json，评分依赖实时行情、K线和资金流。
"""

import json
import os
from datetime import datetime
from typing import Optional

import pandas as pd

from data_feed import DataFeed


PLANS_FILE = os.path.join(os.path.dirname(__file__), "expectation_plans.json")


def _load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {"plans": []}


def _save_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _safe_float(value, default=0.0) -> float:
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value, default=0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _clip(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, float(value)))


def _generate_id(plans: list) -> str:
    existing = {p.get("id") for p in plans if p.get("id")}
    n = 1
    while f"e{n}" in existing:
        n += 1
    return f"e{n}"


class ExpectationPlanner:
    """交易预期计划管理器。"""

    def __init__(self, data_feed: Optional[DataFeed] = None):
        self.df = data_feed if data_feed is not None else DataFeed()

    # ──────────── 存储 ────────────

    def load(self) -> dict:
        data = _load_json(PLANS_FILE)
        data["plans"] = [p for p in data.get("plans", []) if isinstance(p, dict)]
        return data

    def save_plan(self, payload: dict) -> dict:
        data = self.load()
        plans = data["plans"]
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        code = str(payload.get("code", "")).strip().zfill(6)
        if not code.isdigit() or len(code) != 6:
            raise ValueError("股票代码必须是 6 位数字")

        plan_id = str(payload.get("id") or "").strip()
        existing = next((p for p in plans if p.get("id") == plan_id), None)
        plan = existing if existing is not None else {"id": _generate_id(plans), "created_at": now}

        plan.update({
            "code": code,
            "name": str(payload.get("name", "")).strip(),
            "source": str(payload.get("source", "手动")).strip() or "手动",
            "thesis_type": str(payload.get("thesis_type", "技术形态")).strip() or "技术形态",
            "thesis": str(payload.get("thesis", "")).strip(),
            "trigger": str(payload.get("trigger", "")).strip(),
            "invalidation": str(payload.get("invalidation", "")).strip(),
            "planned_price": round(_safe_float(payload.get("planned_price")), 3),
            "stop_loss": round(_safe_float(payload.get("stop_loss")), 3),
            "take_profit": round(_safe_float(payload.get("take_profit")), 3),
            "position_pct": round(_safe_float(payload.get("position_pct")), 2),
            "horizon": str(payload.get("horizon", "T+1至5日")).strip() or "T+1至5日",
            "review_note": str(payload.get("review_note", "")).strip(),
            "updated_at": now,
        })

        if existing is None:
            plans.append(plan)
        _save_json(PLANS_FILE, {"plans": plans})
        return {"success": True, "plan": plan}

    def delete_plan(self, plan_id: str) -> dict:
        data = self.load()
        plans = data["plans"]
        kept = [p for p in plans if p.get("id") != plan_id]
        _save_json(PLANS_FILE, {"plans": kept})
        return {"success": True, "deleted": len(plans) - len(kept)}

    # ──────────── 联动数据 ────────────

    def evaluate_all(self, watchlist_codes=None, portfolio=None, tail_result=None) -> dict:
        plans = self.load()["plans"]
        sources = self.build_sources(watchlist_codes or [], portfolio or {}, tail_result or {})
        source_index = {
            item["code"]: item for group in sources.values() for item in group
            if item.get("code")
        }
        enriched = [self.evaluate_plan(plan, source_index.get(plan.get("code"))) for plan in plans]
        summary = self._summary(enriched, sources)
        return {"plans": enriched, "summary": summary, "sources": sources}

    def build_sources(self, watchlist_codes=None, portfolio=None, tail_result=None) -> dict:
        watchlist_codes = [str(c).strip().zfill(6) for c in (watchlist_codes or []) if str(c).strip()]
        watch_items = self._quote_candidates(watchlist_codes, "自选股")

        position_items = []
        for pos in (portfolio or {}).get("positions", []):
            code = str(pos.get("code", "")).strip().zfill(6)
            if not code:
                continue
            position_items.append({
                "code": code,
                "name": pos.get("name", ""),
                "price": _safe_float(pos.get("current_price")),
                "change_pct": _safe_float(pos.get("change_pct")),
                "source": "持仓",
                "reason": pos.get("rule_action", "持仓观察"),
                "score": round(55 + max(-20, min(20, _safe_float(pos.get("pnl_pct")))), 1),
            })

        tail_items = []
        for item in (tail_result or {}).get("recommendations", [])[:12]:
            code = str(item.get("code", "")).strip().zfill(6)
            if not code:
                continue
            tail_items.append({
                "code": code,
                "name": item.get("name", ""),
                "price": _safe_float(item.get("today_price") or item.get("price")),
                "change_pct": _safe_float(item.get("today_change")),
                "source": "尾盘潜伏",
                "reason": item.get("today_confirmation") or item.get("entry_plan", ""),
                "score": _safe_float(item.get("final_score"), 0),
            })

        return {"watchlist": watch_items, "positions": position_items, "tail_end": tail_items}

    def _quote_candidates(self, codes: list, source: str) -> list:
        if not codes:
            return []
        quotes = self.df.get_realtime_quotes(codes)
        if quotes.empty:
            return [{"code": c, "name": "", "price": 0, "change_pct": 0, "source": source, "reason": "行情不可用", "score": 50} for c in codes]
        rows = []
        for _, row in quotes.iterrows():
            rows.append({
                "code": str(row.get("code", "")).zfill(6),
                "name": row.get("name", ""),
                "price": round(_safe_float(row.get("price")), 2),
                "change_pct": round(_safe_float(row.get("change_pct")), 2),
                "source": source,
                "reason": f"今日涨跌{_safe_float(row.get('change_pct')):+.2f}%",
                "score": 55 + max(-20, min(20, _safe_float(row.get("change_pct")) * 2)),
            })
        return rows

    # ──────────── 评分 ────────────

    def evaluate_plan(self, plan: dict, source_item: Optional[dict] = None) -> dict:
        code = str(plan.get("code", "")).strip().zfill(6)
        quote = self._quote_context(code, source_item)
        technical = self._technical_context(code)
        fund = self._fund_flow_context(code)
        plan_quality = self._plan_quality(plan)
        execution = self._execution_context(plan, quote)

        source_boost = 0
        if source_item:
            if source_item.get("source") == "尾盘潜伏":
                source_boost = 9
            elif source_item.get("source") == "持仓":
                source_boost = 5
            else:
                source_boost = 3

        earning_effect = _clip(
            technical["score"] * 0.45 +
            fund["score"] * 0.32 +
            (source_item or {}).get("score", 50) * 0.15 +
            source_boost
        )
        final_score = _clip(
            plan_quality["score"] * 0.30 +
            earning_effect * 0.32 +
            execution["score"] * 0.28 +
            source_boost
        )

        warnings = []
        warnings.extend(plan_quality["warnings"])
        warnings.extend(execution["warnings"])
        if technical["overheated"]:
            warnings.append("技术面偏热，避免把追涨当成计划")
        if fund["score"] < 45:
            warnings.append("资金流偏弱，赚钱效应不足")

        status = self._status(plan_quality, execution, final_score)
        return {
            **plan,
            "name": plan.get("name") or quote.get("name") or (source_item or {}).get("name", ""),
            "quote": quote,
            "technical": technical,
            "fund_flow": fund,
            "plan_quality": plan_quality,
            "execution": execution,
            "earning_effect_score": round(earning_effect, 1),
            "final_score": round(final_score, 1),
            "status": status,
            "warnings": warnings,
            "source_match": source_item,
            "review_questions": self._review_questions(plan_quality, execution, technical, fund),
        }
    def _quote_context(self, code: str, source_item: Optional[dict] = None) -> dict:
        quote = None
        if code:
            q = self.df.get_realtime_quotes([code])
            if not q.empty:
                quote = q.iloc[0].to_dict()
        if not quote:
            quote = source_item or {}
        price = _safe_float(quote.get("price"))
        close_yest = _safe_float(quote.get("close_yest"))
        change_pct = _safe_float(quote.get("change_pct"))
        if close_yest > 0 and change_pct == 0 and price > 0:
            change_pct = (price - close_yest) / close_yest * 100
        return {
            "code": code,
            "name": quote.get("name", ""),
            "price": round(price, 2),
            "change_pct": round(change_pct, 2),
            "amount": round(_safe_float(quote.get("amount")), 2),
            "available": price > 0,
        }

    def _technical_context(self, code: str) -> dict:
        kline = self.df.get_kline(code, count=90)
        if kline.empty:
            return {"available": False, "score": 50, "summary": "K线不可用", "signals": [], "overheated": False}

        latest = kline.iloc[-1]
        close = _safe_float(latest.get("close"))
        ma5 = latest.get("MA5")
        ma10 = latest.get("MA10")
        ma20 = latest.get("MA20")
        ma60 = latest.get("MA60")
        dif = latest.get("MACD_DIF")
        dea = latest.get("MACD_DEA")
        rsi = latest.get("RSI")
        volume = _safe_float(latest.get("volume"))
        vol_ma5 = _safe_float(latest.get("VOL_MA5"))
        volume_ratio = volume / vol_ma5 if vol_ma5 > 0 else 0

        score = 35
        signals = []
        if pd.notna(ma5) and close > float(ma5):
            score += 12
            signals.append("站上MA5")
        if pd.notna(ma20) and close > float(ma20):
            score += 16
            signals.append("站上MA20")
        if pd.notna(ma5) and pd.notna(ma10) and float(ma5) > float(ma10):
            score += 12
            signals.append("短均线偏多")
        if pd.notna(ma20) and pd.notna(ma60) and float(ma20) >= float(ma60):
            score += 12
            signals.append("中期结构不弱")
        if pd.notna(dif) and pd.notna(dea) and float(dif) > float(dea):
            score += 13
            signals.append("MACD多头")
        if pd.notna(rsi):
            rsi_val = float(rsi)
            if 45 <= rsi_val <= 68:
                score += 10
                signals.append(f"RSI {rsi_val:.1f}")
            elif rsi_val > 78:
                score -= 12
                signals.append(f"RSI过热 {rsi_val:.1f}")
        if volume_ratio:
            if 1.0 <= volume_ratio <= 2.3:
                score += 10
                signals.append(f"量比{volume_ratio:.1f}")
            elif volume_ratio > 3.2:
                score -= 8
                signals.append(f"放量过猛{volume_ratio:.1f}")

        return {
            "available": True,
            "date": latest["date"].strftime("%Y-%m-%d") if hasattr(latest.get("date"), "strftime") else str(latest.get("date", "")),
            "close": round(close, 2),
            "score": round(_clip(score), 1),
            "volume_ratio": round(volume_ratio, 2),
            "rsi": round(float(rsi), 1) if pd.notna(rsi) else None,
            "signals": signals,
            "summary": "；".join(signals) if signals else "技术信号不足",
            "overheated": bool((pd.notna(rsi) and float(rsi) > 78) or volume_ratio > 3.2),
        }

    def _fund_flow_context(self, code: str) -> dict:
        flow = self.df.get_fund_flow(code) or {}
        main_pct = _safe_float(flow.get("main_net_pct"))
        main_net = _safe_float(flow.get("main_net"))
        if not flow:
            return {"available": False, "score": 50, "main_net": 0, "main_net_pct": 0, "summary": "资金流不可用"}
        if main_pct >= 3:
            score = 88
        elif main_pct >= 1:
            score = 75
        elif main_pct > 0:
            score = 62
        elif main_pct <= -2:
            score = 30
        elif main_pct < 0:
            score = 42
        else:
            score = 50
        direction = "净流入" if main_net > 0 else "净流出" if main_net < 0 else "中性"
        return {
            "available": True,
            "score": score,
            "main_net": round(main_net, 2),
            "main_net_pct": round(main_pct, 2),
            "summary": f"主力{direction} {main_pct:+.2f}%",
        }

    def _plan_quality(self, plan: dict) -> dict:
        fields = [
            ("thesis", "缺少预期逻辑"),
            ("trigger", "缺少触发条件"),
            ("invalidation", "缺少失效条件"),
            ("horizon", "缺少时间窗口"),
        ]
        score = 20
        warnings = []
        for field, warning in fields:
            if str(plan.get(field, "")).strip():
                score += 13
            else:
                warnings.append(warning)

        entry = _safe_float(plan.get("planned_price"))
        stop = _safe_float(plan.get("stop_loss"))
        take = _safe_float(plan.get("take_profit"))
        position_pct = _safe_float(plan.get("position_pct"))
        if entry > 0:
            score += 8
        else:
            warnings.append("缺少计划买入价")
        if stop > 0:
            score += 8
        else:
            warnings.append("缺少止损价")
        if take > 0:
            score += 8
        else:
            warnings.append("缺少止盈价")
        if 0 < position_pct <= 20:
            score += 8
        elif position_pct > 20:
            score += 2
            warnings.append("单票仓位偏高")
        else:
            warnings.append("缺少计划仓位")

        risk_reward = None
        if entry > 0 and stop > 0 and take > 0:
            if stop >= entry:
                warnings.append("止损价不应高于计划买入价")
                score -= 15
            if take <= entry:
                warnings.append("止盈价不应低于计划买入价")
                score -= 15
            risk = entry - stop
            reward = take - entry
            if risk > 0:
                risk_reward = reward / risk
                if risk_reward >= 2:
                    score += 7
                elif risk_reward < 1.3:
                    warnings.append("盈亏比偏低")
                    score -= 8

        return {
            "score": round(_clip(score), 1),
            "warnings": warnings,
            "risk_reward": round(risk_reward, 2) if risk_reward is not None else None,
        }

    def _execution_context(self, plan: dict, quote: dict) -> dict:
        price = _safe_float(quote.get("price"))
        entry = _safe_float(plan.get("planned_price"))
        stop = _safe_float(plan.get("stop_loss"))
        take = _safe_float(plan.get("take_profit"))
        change_pct = _safe_float(quote.get("change_pct"))

        warnings = []
        status = "等待触发"
        score = 58
        deviation_pct = None

        if price <= 0:
            return {"score": 45, "status": "行情不可用", "deviation_pct": None, "warnings": ["实时行情不可用"]}
        if stop > 0 and price <= stop:
            return {"score": 20, "status": "预期失效", "deviation_pct": None, "warnings": ["当前价已触及或跌破止损/失效区"]}
        if take > 0 and price >= take:
            return {"score": 76, "status": "预期兑现", "deviation_pct": None, "warnings": ["当前价已到止盈区，优先复盘兑现路径"]}

        if entry > 0:
            deviation_pct = (price - entry) / entry * 100
            if -1.5 <= deviation_pct <= 1.5:
                status = "接近计划价"
                score = 84
            elif deviation_pct < -1.5:
                status = "低于计划价"
                score = 68
            elif deviation_pct <= 4:
                status = "略高于计划价"
                score = 56
                warnings.append("当前价高于计划价，需确认不是临盘追涨")
            else:
                status = "偏离计划价"
                score = 35
                warnings.append("当前价明显偏离计划价")

        if change_pct >= 7:
            score -= 10
            warnings.append("日内涨幅偏高，避免情绪化追入")
        return {
            "score": round(_clip(score), 1),
            "status": status,
            "deviation_pct": round(deviation_pct, 2) if deviation_pct is not None else None,
            "warnings": warnings,
        }

    def _status(self, plan_quality: dict, execution: dict, final_score: float) -> str:
        if execution["status"] in ("预期失效", "预期兑现"):
            return execution["status"]
        if plan_quality["score"] < 65:
            return "先补计划"
        if final_score >= 76 and execution["score"] >= 65:
            return "可按计划执行"
        if final_score >= 60:
            return "等待触发"
        return "只观察"

    def _review_questions(self, plan_quality: dict, execution: dict, technical: dict, fund: dict) -> list:
        questions = []
        if plan_quality["score"] < 80:
            questions.append("这笔交易的失效条件和仓位是否已经写清楚？")
        if execution["status"] in ("偏离计划价", "略高于计划价"):
            questions.append("当前买入是否已经从盘后计划变成盘中冲动？")
        if technical["overheated"]:
            questions.append("技术面是否已经过热，明天还有没有承接资金？")
        if fund["score"] < 45:
            questions.append("资金流偏弱时，预期兑现靠什么推动？")
        return questions[:3]

    def _summary(self, plans: list, sources: dict) -> dict:
        total = len(plans)
        executable = sum(1 for p in plans if p.get("status") == "可按计划执行")
        invalid = sum(1 for p in plans if p.get("status") == "预期失效")
        fulfilled = sum(1 for p in plans if p.get("status") == "预期兑现")
        avg_score = round(sum(_safe_float(p.get("final_score")) for p in plans) / total, 1) if total else 0
        return {
            "plan_count": total,
            "executable_count": executable,
            "invalid_count": invalid,
            "fulfilled_count": fulfilled,
            "avg_score": avg_score,
            "source_count": sum(len(v) for v in sources.values()),
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
