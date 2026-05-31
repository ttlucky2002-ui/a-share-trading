"""
尾盘潜伏选股器 - TailEndScreener
=================================

三阶段流程：
  Stage 1 (14:00-14:10): 基于昨日收盘的技术面/量能初筛 -> 200只
  Stage 2 (14:10-14:25): 基于今日分时/资金/量价盘中验证 -> 排序
  Stage 3 (14:25-14:30): 行业分散 + 综合排名精选 -> Top 10-15

设计文档: docs/TAIL_END_SCREENER.md
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pandas as pd

from config import TAIL_END
from data_feed import DataFeed


def _clip(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, float(value)))


def _as_float(value, default: float = 0.0) -> float:
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _last_completed_pair(kline: pd.DataFrame) -> tuple:
    """Return the latest completed daily bar before today and its prior bar."""
    if kline.empty or "date" not in kline.columns:
        return None, None
    frame = kline.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame[frame["date"].notna()].sort_values("date").reset_index(drop=True)
    if len(frame) < 2:
        return None, None

    today = pd.Timestamp(datetime.now().date())
    completed = frame[frame["date"].dt.normalize() < today]
    if len(completed) >= 2:
        return completed.iloc[-1], completed.iloc[-2]
    return frame.iloc[-1], frame.iloc[-2]


def _is_trading_hours() -> bool:
    """检查当前是否在 A 股交易时段内（9:30-15:00）。"""
    now = datetime.now()
    if now.weekday() >= 5:  # 周末
        return False
    t = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= t <= 15 * 60


def _json_safe(value):
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except (TypeError, ValueError):
            pass
    if isinstance(value, float):
        return value if __import__("math").isfinite(value) else None
    return value


class TailEndScreener:
    """尾盘潜伏选股器：三阶段流水线。"""

    def __init__(self, data_feed=None):
        self.df = data_feed if data_feed is not None else DataFeed()
        self._summary = {}
        self._candidates = []
        self._rotation_boards = []
        self._market_flow_available = False
        self.last_result = {}

    # ────────────── Stage 1: 昨日初筛 ──────────────

    def _stage1_pool(self) -> list:
        """从全市场快照建立基础池；真正的初筛只看昨日K线。"""
        stocks = self.df.get_stock_list()
        if stocks.empty:
            return []

        cfg = TAIL_END
        conditions = (
            (stocks["price"] >= cfg["price_min"]) &
            (stocks["price"] <= cfg["price_max"]) &
            (stocks["market_cap"] >= cfg["market_cap_min"])
        )
        if cfg["exclude_st"]:
            conditions &= ~stocks["is_st"]
        if cfg["exclude_bj"]:
            conditions &= stocks["board"] != "北交所"

        pool = stocks[conditions].copy()
        # 不按当日成交额/涨幅截断，避免开盘后资金已进入的股票挤占昨日候选池。
        # 候选请求量过大时，优先处理中等市值，最终流动性由昨日K线成交量验证。
        preferred_cap = cfg.get("stage1_preferred_market_cap", 180)
        pool["_cap_distance"] = (
            pd.to_numeric(pool["market_cap"], errors="coerce").fillna(0) - preferred_cap
        ).abs()
        pool = pool.sort_values(
            ["_cap_distance", "code"], ascending=[True, True]
        ).head(
            cfg.get("stage1_max_candidates", 1000)
        )
        pool = pool.drop(columns=["_cap_distance"], errors="ignore")
        return pool.to_dict("records")

    def _score_yesterday_kline(self, code: str) -> dict:
        """基于昨日K线数据计算Stage 1评分。"""
        kline = self.df.get_kline(code, count=TAIL_END.get("stage1_kline_count", 80))
        if kline.empty or len(kline) < 3:
            return {"available": False, "score": 0, "reason": "K线数据不足"}

        yesterday, prev_day = _last_completed_pair(kline)
        if yesterday is None or prev_day is None:
            return {"available": False, "score": 0, "reason": "缺少昨日完整K线"}

        cfg = TAIL_END
        w = TAIL_END["stage1_weights"]

        close = _as_float(yesterday.get("close"))
        prev_close = _as_float(prev_day.get("close"))
        open_price = _as_float(yesterday.get("open"), close)
        volume = _as_float(yesterday.get("volume"))
        vol_ma5 = _as_float(yesterday.get("VOL_MA5"), volume)
        if close <= 0 or prev_close <= 0 or volume <= 0:
            return {"available": False, "score": 0, "reason": "昨日价格或成交量无效"}

        yesterday_change = (close - prev_close) / prev_close * 100
        if not (cfg["yesterday_change_min"] <= yesterday_change <= cfg["yesterday_change_max"]):
            return {
                "available": False,
                "score": 0,
                "reason": (
                    f"昨日涨跌幅{yesterday_change:+.1f}%不在"
                    f"[{cfg['yesterday_change_min']}%,{cfg['yesterday_change_max']}%]"
                ),
            }

        # ── 量比评分：昨日量 / 5日均量 ──
        vol_ratio = volume / max(vol_ma5, 1)
        if vol_ratio < cfg["volume_ratio_min"]:
            return {
                "available": False,
                "score": 0,
                "reason": f"昨日量比{vol_ratio:.2f}低于{cfg['volume_ratio_min']}",
            }
        if vol_ratio <= 1.5:
            vol_score = 55 + (vol_ratio - cfg["volume_ratio_min"]) / max(0.1, 1.5 - cfg["volume_ratio_min"]) * 35
        elif vol_ratio <= 2.5:
            vol_score = 90 - (vol_ratio - 1.5) * 12
        else:
            vol_score = 78 - min(35, (vol_ratio - 2.5) * 18)
        vol_score = _clip(vol_score)

        # ── 均线结构评分 ──
        ma5 = yesterday.get("MA5")
        ma10 = yesterday.get("MA10")
        ma20 = yesterday.get("MA20")
        ma60 = yesterday.get("MA60")
        ma_score = 0
        ma_reasons = []
        if pd.notna(ma5) and close > float(ma5):
            ma_score += 25
            ma_reasons.append("收盘站上MA5")
        if pd.notna(ma5) and pd.notna(ma10) and float(ma5) > float(ma10):
            ma_score += 25
            ma_reasons.append("MA5高于MA10")
            prev_ma5 = prev_day.get("MA5")
            prev_ma10 = prev_day.get("MA10")
            if (pd.notna(prev_ma5) and pd.notna(prev_ma10)
                    and float(prev_ma5) <= float(prev_ma10)):
                ma_score += 10
                ma_reasons.append("MA5金叉MA10")
        if pd.notna(ma20) and close > ma20:
            ma_score += 20
            ma_reasons.append("站上MA20")
        if pd.notna(ma20) and pd.notna(ma60) and float(ma20) >= float(ma60):
            ma_score += 20
            ma_reasons.append("MA20不弱于MA60")
        ma_score = _clip(ma_score)

        # ── MACD评分 ──
        dif = yesterday.get("MACD_DIF")
        dea = yesterday.get("MACD_DEA")
        macd_score = 0
        macd_reasons = []
        if pd.notna(dif) and pd.notna(dea):
            if float(dif) > float(dea):
                macd_score += 60
                macd_reasons.append("DIF>DEA")
                if float(dif) > 0:
                    macd_score += 25
                    macd_reasons.append("DIF>0")
                prev_dif = prev_day.get("MACD_DIF")
                if pd.notna(prev_dif) and float(dif) >= float(prev_dif):
                    macd_score += 15
                    macd_reasons.append("DIF抬升")
        macd_score = _clip(macd_score)

        # ── 价格位置评分：昨日收盘在近10日区间的位置 ──
        completed = kline.copy()
        completed["date"] = pd.to_datetime(completed["date"], errors="coerce")
        completed = completed[completed["date"].dt.normalize() <= pd.Timestamp(yesterday["date"]).normalize()]
        recent_10 = completed.tail(10)
        if len(recent_10) >= 5:
            high_10 = float(recent_10["high"].max())
            low_10 = float(recent_10["low"].min())
            range_10 = high_10 - low_10
            if range_10 > 0.01:
                position = (close - low_10) / range_10
                # 中高位但未贴近区间极值，避免尾盘再去追过热标的。
                if 0.45 <= position <= 0.85:
                    pos_score = 100
                elif 0.35 <= position < 0.45 or 0.85 < position <= 0.95:
                    pos_score = 72
                elif position > 0.95:
                    pos_score = 45
                else:
                    pos_score = 38
            else:
                position = 0.5
                pos_score = 60
        else:
            position = 0.5
            pos_score = 60

        # ── RSI评分 ──
        rsi = yesterday.get("RSI")
        rsi_score = 55
        if pd.notna(rsi):
            rsi_val = float(rsi)
            if not (cfg["rsi_min"] <= rsi_val <= cfg["rsi_max"]):
                return {
                    "available": False,
                    "score": 0,
                    "reason": f"昨日RSI {rsi_val:.1f}不在[{cfg['rsi_min']},{cfg['rsi_max']}]",
                }
            if 45 <= rsi_val <= 58:
                rsi_score = 100
            elif 40 <= rsi_val <= 62:
                rsi_score = 82
            else:
                rsi_score = 62

        total = round(
            vol_score * w["volume"]
            + ma_score * w["ma_structure"]
            + macd_score * w["macd"]
            + pos_score * w["price_position"]
            + rsi_score * w["rsi"]
        )

        reasons = []
        if ma_reasons:
            reasons.extend(ma_reasons)
        if macd_reasons:
            reasons.extend(macd_reasons)
        if vol_ratio >= 1.2:
            reasons.append(f"量比{vol_ratio:.1f}")
        if yesterday_change >= 0:
            reasons.append(f"昨日温和上涨{yesterday_change:+.1f}%")
        else:
            reasons.append(f"昨日回踩{yesterday_change:+.1f}%")
        if close >= open_price:
            reasons.append("昨日收阳")

        return {
            "available": True,
            "score": total,
            "yesterday_date": pd.Timestamp(yesterday["date"]).strftime("%Y-%m-%d"),
            "yesterday_close": round(close, 2),
            "yesterday_change": round(yesterday_change, 2),
            "yesterday_volume_ratio": round(vol_ratio, 2),
            "ma_score": round(ma_score),
            "macd_score": round(macd_score),
            "volume_score": round(vol_score),
            "position_score": round(pos_score),
            "rsi": round(float(rsi if pd.notna(rsi) else 50), 1),
            "reason": "；".join(reasons[:4]) if reasons else "技术信号偏弱",
            "ma5": round(float(ma5), 2) if pd.notna(ma5) else None,
            "ma10": round(float(ma10), 2) if pd.notna(ma10) else None,
            "ma20": round(float(ma20), 2) if pd.notna(ma20) else None,
        }

    def _stage1_screen(self, pool: list) -> list:
        """对初筛池逐股获取K线数据并评分，返回前200只。"""
        if not pool:
            return []

        codes = [item["code"] for item in pool]
        pool_by_code = {str(item["code"]).zfill(6): item for item in pool}
        results = []
        limit = TAIL_END["stage1_pool_size"]

        with ThreadPoolExecutor(max_workers=min(12, len(pool) or 1)) as executor:
            futures = {executor.submit(self._score_yesterday_kline, code): code
                       for code in codes}
            for future in as_completed(futures):
                code = futures[future]
                try:
                    score_result = future.result()
                except Exception:
                    continue
                if not score_result.get("available"):
                    continue

                quote = pool_by_code.get(str(code).zfill(6), {})
                results.append({
                    "code": str(code).zfill(6),
                    "name": quote.get("name", ""),
                    "board": quote.get("board", ""),
                    "market_cap": round(_as_float(quote.get("market_cap")), 2),
                    **score_result,
                })

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:limit]

    # ────────────── Stage 2: 今日盘中验证 ──────────────

    def _verify_today(self, item: dict) -> dict:
        """对单只候选股进行今日盘中验证评分。"""
        code = item["code"]
        w = TAIL_END["stage2_weights"]

        # 1. 今日实时行情
        quotes_df = self.df.get_realtime_quotes([code])
        if quotes_df.empty:
            item["stage2"] = {
                "available": False,
                "score": 0,
                "reason": "实时行情不可用",
            }
            return item

        q = quotes_df.iloc[0]
        today_change = float(q.get("change_pct") or 0)
        today_amount = float(q.get("amount") or 0)
        today_price = float(q.get("price") or 0)
        volume_ratio_today = float(q.get("volume_ratio") or 0)

        # 基本条件过滤
        change_min = TAIL_END["today_change_min"]
        change_max = TAIL_END["today_change_max"]
        vr_min = TAIL_END["today_volume_ratio_min"]
        vr_max = TAIL_END["today_volume_ratio_max"]

        if today_change < change_min or today_change > change_max:
            item["stage2"] = {
                "available": False,
                "score": 0,
                "reason": f"今日涨幅{today_change:+.1f}%不在区间[{change_min}%,{change_max}%]",
            }
            return item
        if volume_ratio_today > 0 and (volume_ratio_today < vr_min or volume_ratio_today > vr_max):
            item["stage2"] = {
                "available": False,
                "score": 0,
                "reason": f"今日量比{volume_ratio_today:.1f}不在区间[{vr_min},{vr_max}]",
            }
            return item

        # ── 量价评分 (30%) ──
        # 涨幅在中段(0~5%) 线性得分
        change_score = _clip(today_change / max(change_max, 1) * 60, 0, 40)
        # 量比评分: 1.0-2.0 最优
        vol_score = _clip(
            (20 - abs(volume_ratio_today - 1.5) * 15) if volume_ratio_today > 0 else 0,
            0, 30
        )
        # 当前价在今日区间位置（分时数据可细化）
        intraday_data = self.df.get_intraday_minute(code)
        if intraday_data.get("available"):
            pos_in_day = intraday_data.get("position_in_day", 0.5)
            # 在今日区间上2/3为佳
            pos_score = _clip(pos_in_day * 30, 0, 30)
            intraday_trend_pct = intraday_data.get("change_pct", 0)
        else:
            pos_score = 15
            intraday_trend_pct = 0

        volume_price_score = _clip(change_score + vol_score + pos_score, 0, 100)

        # ── 资金流入评分 (30%) ──
        fund = self.df.get_intraday_stock_fund_flow(code)
        if fund.get("available"):
            main_net = _as_float(fund.get("main_net"))
            super_large = _as_float(fund.get("super_large_net"))
            large = _as_float(fund.get("large_net"))
            main_pct = _as_float(fund.get("main_net_pct"))

            fund_score = 45 if main_net > 0 else 15
            fund_score += min(30, max(0, main_pct) * 5)
            if super_large > 0:
                fund_score += 15
            if large > 0:
                fund_score += 10
            if super_large + large > 0 and main_net > 0:
                fund_score += min(15, (super_large + large) / max(abs(main_net), 1) * 15)
            # 主力净流入占比加分
            if main_pct > 3:
                fund_score += 10
            elif main_pct > 1:
                fund_score += 5
            fund_score = _clip(fund_score, 0, 100)
        else:
            # 回退到快照资金数据
            snapshot_flow = self.df.get_fund_flow(code)
            main_net = _as_float(snapshot_flow.get("main_net"))
            main_pct = _as_float(snapshot_flow.get("main_net_pct"))
            fund_score = _clip(35 + max(0, main_pct) * 4, 0, 65)

        main_net_min = TAIL_END.get("today_main_net_min", 0)
        main_pct_min = TAIL_END.get("today_main_net_pct_min", 0)
        main_pct_max = TAIL_END.get("today_main_net_pct_max", 100)
        if main_net <= main_net_min:
            item["stage2"] = {
                "available": False,
                "score": 0,
                "reason": f"主力净流入{main_net / 1e4:+.0f}万未转正",
            }
            return item
        if main_pct < main_pct_min:
            item["stage2"] = {
                "available": False,
                "score": 0,
                "reason": f"主力净流入占比{main_pct:.1f}%低于{main_pct_min:.1f}%",
            }
            return item
        if main_pct > main_pct_max:
            item["stage2"] = {
                "available": False,
                "score": 0,
                "reason": f"主力净流入占比{main_pct:.1f}%过高，避免追已过热标的",
            }
            return item

        # ── 分时趋势评分 (25%) ──
        if intraday_data.get("available"):
            trend_score = 0
            trend_reasons = []
            # 上涨分钟占比
            up_ratio = intraday_data.get("up_minute_ratio", 0.5)
            trend_score += _clip(up_ratio * 30, 0, 30)
            # 尾盘趋势
            tail = intraday_data.get("tail_trend", "flat")
            if tail == "up":
                trend_score += 25
                trend_reasons.append("尾盘走强")
            elif tail == "flat":
                trend_score += 12
            elif TAIL_END.get("reject_tail_down", True):
                item["stage2"] = {
                    "available": False,
                    "score": 0,
                    "reason": "分时尾段转弱，尾盘交易放弃",
                }
                return item
            # 价格在均线上方时间占比
            above_ratio = intraday_data.get("above_avg_ratio", 0.5)
            trend_score += _clip(above_ratio * 25, 0, 25)
            # 量能集中度：尾盘量相对于早盘
            vol_conc = intraday_data.get("volume_concentration", 0.5)
            if vol_conc >= 0.8:
                trend_score += 20
            elif vol_conc >= 0.5:
                trend_score += 10
            trend_score = _clip(trend_score, 0, 100)
            trend_reason = "；".join(trend_reasons) if trend_reasons else "分时信号中性"
        else:
            trend_score = 50
            trend_reason = "分时数据不可用"

        # ── 板块资金评分 (15%) ──
        sector_score = 0
        sector_names = []
        board_matches = sorted(
            item.get("board_matches", []),
            key=lambda board: board.get("flow_score", 0),
            reverse=True,
        )
        if board_matches:
            top_board = board_matches[0]
            sector_score = _clip(float(top_board.get("flow_score") or 0))
            for board in board_matches[:3]:
                inflow = _as_float(board.get("main_net_inflow"))
                sector_names.append(
                    f"{board.get('name', '')}({board.get('type', '')},净流入{inflow:+.1f}亿)"
                )
        elif self._market_flow_available and TAIL_END.get("require_sector_match", True):
            item["stage2"] = {
                "available": False,
                "score": 0,
                "reason": "未匹配当日资金领先板块",
            }
            return item
        else:
            sector_score = 45
        sector_score = _clip(sector_score, 0, 100)

        # ── 综合Stage 2评分 ──
        stage2_score = round(
            volume_price_score * w["volume_price"]
            + fund_score * w["fund_flow"]
            + trend_score * w["intraday_trend"]
            + sector_score * w["sector_flow"]
        )

        parts = []
        if volume_price_score >= 50:
            parts.append(f"量价配合({volume_price_score}分)")
        if fund_score >= 50:
            parts.append(f"资金流入({fund_score}分)")
        if trend_score >= 60:
            parts.append(f"分时走强({trend_score}分)")
        if sector_score >= 50:
            parts.append(f"板块助攻({sector_score}分)")
        if not parts:
            parts.append("信号平淡")

        item["stage2"] = {
            "available": True,
            "score": stage2_score,
            "today_price": round(today_price, 2),
            "today_change": round(today_change, 2),
            "volume_ratio": round(volume_ratio_today, 2),
            "today_amount": round(today_amount / 1e8, 2),
            "volume_price_score": round(volume_price_score),
            "fund_score": round(fund_score),
            "trend_score": round(trend_score),
            "sector_score": round(sector_score),
            "main_net_today": round(main_net / 1e4, 0),
            "main_net_pct": round(main_pct, 2),
            "reason": "；".join(parts),
            "intraday_reason": trend_reason,
            "sector_matches": sector_names,
            "tomorrow_boards": [
                board.get("name", "") for board in board_matches[:3]
                if board.get("name")
            ],
            "intraday_change_pct": round(intraday_trend_pct, 2),
        }
        return item

    def _stage2_verify(self, candidates: list) -> list:
        """对Stage 1候选逐只进行今日盘中验证。"""
        if not candidates:
            return []

        limit = TAIL_END.get("stage2_candidate_limit", 50)
        candidates = candidates[:limit]
        codes = [str(item["code"]).zfill(6) for item in candidates]
        matches = {}
        self._rotation_boards = []
        self._market_flow_available = False
        try:
            rotation = self.df.get_rotation_matches(
                codes, top_n=TAIL_END.get("rotation_board_limit", 10)
            )
            matches = rotation.get("matches", {}) or {}
            self._rotation_boards = rotation.get("boards", []) or []
            self._market_flow_available = bool(self._rotation_boards)
        except Exception:
            matches = {}
            self._rotation_boards = []
            self._market_flow_available = False
        for item in candidates:
            code = str(item["code"]).zfill(6)
            item["board_matches"] = matches.get(code, [])

        with ThreadPoolExecutor(max_workers=min(8, len(candidates) or 1)) as executor:
            futures = {executor.submit(self._verify_today, item): item["code"]
                       for item in candidates}
            results = []
            for future in as_completed(futures):
                try:
                    result = future.result()
                except Exception:
                    continue
                if result.get("stage2", {}).get("available"):
                    results.append(result)

        results.sort(
            key=lambda x: x["stage2"]["score"],
            reverse=True,
        )
        return results

    # ────────────── Stage 3: 尾盘精选 ──────────────

    def _stage3_select(self, verified: list) -> list:
        """行业分散 + 综合排名精选 Top N。"""
        if not verified:
            return []

        w = TAIL_END["composite_weights"]
        n = TAIL_END["recommendation_count"]
        max_per_ind = TAIL_END["max_per_industry"]
        max_per_concept = TAIL_END["max_per_concept"]

        for item in verified:
            s1 = float(item.get("score", 0))
            s2 = float(item.get("stage2", {}).get("score", 0))
            item["final_score"] = round(s1 * w["stage1"] + s2 * w["stage2"])

        # 先按综合分排序
        verified.sort(key=lambda x: x["final_score"], reverse=True)

        # 行业分散选择
        selected = []
        industry_count = {}
        concept_count = {}

        for item in verified:
            if len(selected) >= n:
                break
            board_matches = item.get("board_matches", [])
            industry = next(
                (board.get("name", "") for board in board_matches
                 if board.get("type") == "行业"),
                item.get("industry") or self._get_stock_industry(item["code"]),
            )
            concepts = [
                board.get("name", "") for board in board_matches
                if board.get("type") == "概念" and board.get("name")
            ]
            item["industry"] = industry
            item["concepts"] = concepts[:3]
            if industry and industry_count.get(industry, 0) >= max_per_ind:
                continue
            if any(concept_count.get(concept, 0) >= max_per_concept for concept in concepts[:2]):
                continue
            if industry:
                industry_count[industry] = industry_count.get(industry, 0) + 1
            for concept in concepts[:2]:
                concept_count[concept] = concept_count.get(concept, 0) + 1
            selected.append(item)

        # 如果分散后不足n只，从剩余按分补齐
        if len(selected) < n:
            for item in verified:
                if len(selected) >= n:
                    break
                if item not in selected:
                    selected.append(item)

        # 排名
        for rank, item in enumerate(selected, start=1):
            item["rank"] = rank

        return selected

    def _get_stock_industry(self, code: str) -> str:
        """获取股票所属行业（从快照缓存中读取）。"""
        stocks = self.df._stock_list_cache
        if stocks is not None and not stocks.empty and "industry" in stocks.columns:
            match = stocks[stocks["code"] == code]
            if not match.empty:
                ind = match.iloc[0].get("industry", "")
                if ind:
                    return str(ind)
        return ""

    def _get_stock_concepts(self, code: str) -> list:
        """获取股票所属概念板块（缓存未命中时返回空）。"""
        return []

    def _check_trading_restrictions(self, item: dict) -> list:
        """检查交易限制：涨跌停、停牌等。"""
        warnings = []
        code = item["code"]
        # 从实时行情获取
        quotes_df = self.df.get_realtime_quotes([code])
        if not quotes_df.empty:
            q = quotes_df.iloc[0]
            change_pct = float(q.get("change_pct") or 0)
            price = float(q.get("price") or 0)
            # 涨停检查
            if change_pct >= 9.5 and price == q.get("high", price):
                warnings.append("当日涨停")
            # 跌停检查
            if change_pct <= -9.5 and price == q.get("low", price):
                warnings.append("当日跌停")
        return warnings

    # ────────────── 主入口 ──────────────

    def screen(self) -> dict:
        """执行完整的尾盘潜伏三阶段筛选。"""
        self._rotation_boards = []
        self._market_flow_available = False
        self._summary = {
            "state": "stage1",
            "message": "Stage 1: 昨日数据初筛",
            "strategy": "昨日初筛 + 今日分时/资金验证 + 尾盘执行",
        }
        pool = self._stage1_pool()
        self._summary.update({"pool_count": len(pool)})
        if not pool:
            return self._result("初筛池为空", [])

        candidates = self._stage1_screen(pool)
        self._summary.update({
            "state": "stage2",
            "message": "Stage 2: 今日盘中验证",
            "stage1_count": len(candidates),
        })
        if not candidates:
            return self._result("无满足昨日条件的标的", [])

        verified = self._stage2_verify(candidates)
        self._summary.update({
            "state": "stage3",
            "message": "Stage 3: 尾盘精选",
            "stage2_count": len(verified),
            "rotation_board_count": len(self._rotation_boards),
        })
        if not verified:
            return self._result("今日盘中验证无合格标的", [])

        selected = self._stage3_select(verified)

        # 构建输出
        recommendations = []
        for item in selected:
            s1 = item.get("score", 0)
            s2_info = item.get("stage2", {})
            s2 = s2_info.get("score", 0)
            warnings = self._check_trading_restrictions(item)

            recommendations.append({
                "rank": item.get("rank"),
                "code": item["code"],
                "name": item.get("name", ""),
                "price": item.get("yesterday_close", 0),
                "today_price": s2_info.get("today_price"),
                "yesterday_date": item.get("yesterday_date"),
                "yesterday_change": item.get("yesterday_change"),
                "yesterday_volume_ratio": item.get("yesterday_volume_ratio"),
                "stage1_score": s1,
                "stage2_score": s2,
                "final_score": item.get("final_score", 0),
                "today_change": s2_info.get("today_change"),
                "volume_ratio": s2_info.get("volume_ratio"),
                "main_net_today": s2_info.get("main_net_today"),
                "main_net_pct": s2_info.get("main_net_pct"),
                "yesterday_setup": item.get("reason", ""),
                "today_confirmation": s2_info.get("reason", ""),
                "intraday_trend": s2_info.get("intraday_change_pct"),
                "sector_matches": s2_info.get("sector_matches", []),
                "tomorrow_boards": s2_info.get("tomorrow_boards", []),
                "industry": item.get("industry", ""),
                "concepts": item.get("concepts", []),
                "warnings": warnings,
                "ma5": item.get("ma5"),
                "ma10": item.get("ma10"),
                "ma20": item.get("ma20"),
                "entry_plan": "14:25后观察，尾盘不跌破分时均价且主力仍为净流入再考虑",
                "risk_note": "若尾盘跳水、主力转净流出或次日板块资金断档，放弃或降低仓位",
            })

        # 市场上下文
        market_ctx = {}
        try:
            market_ctx = self.df.get_market_metrics()
            top_sectors = self.df.get_sector_fund_flow(5)
            if not top_sectors.empty:
                market_ctx["top_sectors"] = [
                    {
                        "name": r["name"],
                        "main_net_inflow": round(float(r.get("main_net_inflow", 0)), 2),
                        "change_pct": round(float(r.get("change_pct", 0)), 2),
                    }
                    for _, r in top_sectors.iterrows()
                ]
            if self._rotation_boards:
                market_ctx["tomorrow_focus_boards"] = [
                    {
                        "name": board.get("name", ""),
                        "type": board.get("type", ""),
                        "flow_score": board.get("flow_score", 0),
                        "main_net_inflow": board.get("main_net_inflow", 0),
                        "recent_main_net_inflow": board.get("recent_main_net_inflow"),
                    }
                    for board in self._rotation_boards[:8]
                ]
        except Exception:
            pass

        in_session = _is_trading_hours()
        self._summary.update({
            "state": "done",
            "message": "筛选完成",
            "selected_count": len(recommendations),
            "in_trading_hours": in_session,
            "rotation_board_count": len(self._rotation_boards),
            "trade_window": "14:25-14:55尾盘确认",
        })

        result = {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "market_status": "交易中" if in_session else "已收盘",
            "summary": _json_safe(self._summary),
            "recommendations": _json_safe(recommendations),
            "market_context": _json_safe(market_ctx),
        }
        self.last_result = result
        return result

    def _result(self, message: str, recommendations: list) -> dict:
        self._summary.update({
            "state": "done",
            "message": message,
            "selected_count": len(recommendations),
        })
        result = {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "market_status": "交易中" if _is_trading_hours() else "已收盘",
            "summary": _json_safe(self._summary),
            "recommendations": [],
            "market_context": {},
        }
        self.last_result = result
        return result
