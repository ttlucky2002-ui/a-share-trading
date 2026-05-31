"""
数据获取模块 - Data Feed
========================
通过东方财富/新浪财经获取全市场快照，通过腾讯财经获取即时行情和K线。
所有在线源失败时使用最近一次成功的本地快照，避免页面被单一数据源拖垮。
"""

import math
import os
import time
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import pandas as pd
import numpy as np
from datetime import datetime
from typing import Optional, List

from config import REQUEST_TIMEOUT, REQUEST_RETRIES, REQUEST_INTERVAL


EASTMONEY_LIVE = "https://push2.eastmoney.com"
EASTMONEY_DELAY = "https://push2delay.eastmoney.com"
EASTMONEY_FINANCIAL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
SINA_MARKET_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
)
SINA_MARKET_COUNT_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeStockCount"
)
MARKET_CACHE_DIR = os.path.join(os.path.dirname(__file__), ".cache")
MARKET_CACHE_FILE = os.path.join(MARKET_CACHE_DIR, "market_snapshot.json")
MARKET_CACHE_META_FILE = os.path.join(MARKET_CACHE_DIR, "market_snapshot.meta.json")


def _safe_float(val, default=0.0):
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


class DataFeed:
    """A股行情接口，带多源回退和最近成功快照缓存。"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36"),
            "Referer": "https://quote.eastmoney.com/",
        })
        self._stock_list_cache = None
        self._stock_list_cache_time = 0
        self._last_request_time = 0
        self._cache_lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._eastmoney_live_unavailable_until = 0
        self._board_history_unavailable_until = 0
        self._financial_cache = {}
        self._financial_cache_ttl = 12 * 60 * 60
        self._source_state = {
            "market_snapshot": {"source": "尚未加载", "ok": False, "stale": False},
            "realtime": {"source": "尚未请求", "ok": False},
            "kline": {"source": "尚未请求", "ok": False},
            "fundamentals": {"source": "尚未请求", "ok": False},
        }
        self._load_local_snapshot()

    def _rate_limit(self):
        elapsed = time.time() - self._last_request_time
        if elapsed < REQUEST_INTERVAL:
            time.sleep(REQUEST_INTERVAL - elapsed)
        self._last_request_time = time.time()

    @staticmethod
    def _safe_div(val, divisor, default=0.0):
        try:
            return float(val) / divisor
        except (ValueError, TypeError):
            return default

    def _request(self, url: str, params: dict = None, *,
                 timeout=None, retries: int = None,
                 headers: dict = None) -> Optional[requests.Response]:
        timeout = timeout or (3, REQUEST_TIMEOUT)
        retries = REQUEST_RETRIES if retries is None else retries
        for attempt in range(retries):
            try:
                self._rate_limit()
                resp = self.session.get(url, params=params, timeout=timeout,
                                        headers=headers)
                resp.encoding = "utf-8"
                if resp.status_code == 200:
                    return resp
            except requests.RequestException:
                if attempt < retries - 1:
                    time.sleep(1)
        return None

    def _request_eastmoney(self, path: str, params: dict,
                           prefer_delay: bool = False) -> tuple:
        use_delay_first = (
            prefer_delay or time.time() < self._eastmoney_live_unavailable_until
        )
        bases = (EASTMONEY_DELAY, EASTMONEY_LIVE) if use_delay_first else (
            EASTMONEY_LIVE, EASTMONEY_DELAY
        )
        for base in bases:
            resp = self._request(
                base + path, params, timeout=(3, 10), retries=1
            )
            if resp is not None:
                source = "东方财富延时行情" if base == EASTMONEY_DELAY else "东方财富实时行情"
                return resp, source
            if base == EASTMONEY_LIVE:
                self._eastmoney_live_unavailable_until = time.time() + 300
        return None, ""

    def _set_source_state(self, key: str, source: str, ok: bool,
                          stale: bool = False, detail: str = ""):
        with self._cache_lock:
            self._source_state[key] = {
                "source": source,
                "ok": ok,
                "stale": stale,
                "detail": detail,
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

    def get_source_status(self) -> dict:
        with self._cache_lock:
            status = {key: dict(value) for key, value in self._source_state.items()}
            if self._stock_list_cache_time:
                status["market_snapshot"]["age_seconds"] = int(
                    max(0, time.time() - self._stock_list_cache_time)
                )
            status["market_snapshot"]["rows"] = (
                len(self._stock_list_cache) if self._stock_list_cache is not None else 0
            )
        return status

    def _load_local_snapshot(self):
        if not os.path.exists(MARKET_CACHE_FILE):
            return
        try:
            # 读取并清理尾部的空字节（系统异常截断可能导致）
            import io
            with open(MARKET_CACHE_FILE, "rb") as handle:
                raw = handle.read().rstrip(b"\x00")
            cached = pd.read_json(io.BytesIO(raw), orient="records", dtype={"code": str})
            if cached.empty:
                return
            cached["code"] = cached["code"].astype(str).str.zfill(6)
            generated_at = os.path.getmtime(MARKET_CACHE_FILE)
            source = "本地缓存"
            if os.path.exists(MARKET_CACHE_META_FILE):
                with open(MARKET_CACHE_META_FILE, "rb") as handle:
                    meta_raw = handle.read().rstrip(b"\x00")
                meta = json.loads(meta_raw)
                generated_at = float(meta.get("timestamp", generated_at))
                source = f"本地缓存({meta.get('source', '未知源')})"
            self._stock_list_cache = cached
            self._stock_list_cache_time = generated_at
            self._set_source_state("market_snapshot", source, True, stale=True,
                                   detail="已读取最近一次成功快照")
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return

    def _store_local_snapshot(self, frame: pd.DataFrame, source: str):
        try:
            os.makedirs(MARKET_CACHE_DIR, exist_ok=True)
            snapshot_tmp = MARKET_CACHE_FILE + ".tmp"
            meta_tmp = MARKET_CACHE_META_FILE + ".tmp"
            frame.to_json(snapshot_tmp, orient="records", force_ascii=False)
            with open(meta_tmp, "w", encoding="utf-8") as handle:
                json.dump({"source": source, "timestamp": time.time()}, handle,
                          ensure_ascii=False)
            os.replace(snapshot_tmp, MARKET_CACHE_FILE)
            os.replace(meta_tmp, MARKET_CACHE_META_FILE)
        except OSError:
            pass

    # ──────────── 股票列表 ────────────

    def get_stock_list(self, force_refresh: bool = False) -> pd.DataFrame:
        """
        获取全A股股票列表 + 实时行情快照
        数据源: 东方财富实时/延时行情，失败后使用新浪财经或最近成功快照。
        返回: DataFrame, 缓存5分钟
        """
        with self._cache_lock:
            cache_age = time.time() - self._stock_list_cache_time
            if self._stock_list_cache is not None and not force_refresh and cache_age < 300:
                return self._stock_list_cache

        if not self._refresh_lock.acquire(blocking=False):
            with self._cache_lock:
                if self._stock_list_cache is not None:
                    return self._stock_list_cache
            if not self._refresh_lock.acquire(timeout=35):
                return pd.DataFrame()
            self._refresh_lock.release()
            with self._cache_lock:
                return self._stock_list_cache if self._stock_list_cache is not None else pd.DataFrame()

        try:
            result, source, detail = self._fetch_eastmoney_snapshot()
            if result.empty:
                result, source, detail = self._fetch_sina_snapshot()
            if not result.empty:
                with self._cache_lock:
                    self._stock_list_cache = result
                    self._stock_list_cache_time = time.time()
                self._store_local_snapshot(result, source)
                self._set_source_state("market_snapshot", source, True, False, detail)
                print(f"[DataFeed] 市场快照加载完成: {len(result)} 只 ({source})")
                return result

            with self._cache_lock:
                if self._stock_list_cache is not None:
                    self._set_source_state("market_snapshot", "本地缓存", True, True,
                                           "在线数据源均不可用")
                    return self._stock_list_cache
            self._set_source_state("market_snapshot", "无可用数据源", False, False,
                                   "在线数据源均不可用且无本地缓存")
            return pd.DataFrame()
        finally:
            self._refresh_lock.release()

    def _fetch_eastmoney_snapshot(self) -> tuple:
        path = "/api/qt/clist/get"
        params = {
            "pn": 1, "pz": 100, "po": 1, "np": 1,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": 2, "invt": 2, "fid": "f3",
            "fs": ("m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,"
                   "m:0+t:81+s:2048,m:0+t:83+s:2048"),
            "fields": ("f12,f14,f2,f3,f4,f5,f6,f7,f8,f9,f10,"
                       "f15,f16,f20,f21,f23,f62,f184"),
        }
        resp, source = self._request_eastmoney(path, params)
        if resp is None:
            return pd.DataFrame(), "", "东方财富首页请求失败"
        try:
            first_data = resp.json().get("data") or {}
            total = int(first_data.get("total") or 0)
            pages = max(1, math.ceil(total / 100))
            rows = list(first_data.get("diff") or [])
        except (ValueError, TypeError, json.JSONDecodeError, AttributeError):
            return pd.DataFrame(), "", "东方财富响应无法解析"
        if not rows:
            return pd.DataFrame(), "", "东方财富未返回行情"

        base = EASTMONEY_DELAY if "延时" in source else EASTMONEY_LIVE

        def fetch_page(page):
            page_params = dict(params)
            page_params["pn"] = page
            for _ in range(2):
                page_resp = self._request(
                    base + path, page_params, timeout=(3, 10), retries=1
                )
                if page_resp is not None:
                    try:
                        return page_resp.json().get("data", {}).get("diff", []) or []
                    except (ValueError, TypeError, json.JSONDecodeError, AttributeError):
                        pass
            return []

        if pages > 1:
            with ThreadPoolExecutor(max_workers=20) as executor:
                futures = [executor.submit(fetch_page, page)
                           for page in range(2, pages + 1)]
                for future in as_completed(futures):
                    rows.extend(future.result())
        result = self._frame_from_eastmoney(rows)
        coverage = len(result) / total if total else 0
        if coverage < 0.95:
            return pd.DataFrame(), "", f"东方财富快照覆盖率不足: {coverage:.1%}"
        detail = f"共{len(result)}只，覆盖率{coverage:.1%}"
        return result, source, detail

    def _fetch_sina_snapshot(self) -> tuple:
        params = {"node": "hs_a"}
        sina_headers = {"Referer": "https://finance.sina.com.cn"}
        count_resp = self._request(SINA_MARKET_COUNT_URL, params,
                                   timeout=(3, 10), retries=2, headers=sina_headers)
        if count_resp is None:
            return pd.DataFrame(), "", "新浪市场数量请求失败"
        try:
            total = int(count_resp.json())
        except (ValueError, TypeError, json.JSONDecodeError):
            return pd.DataFrame(), "", "新浪市场数量无法解析"

        base_params = {
            "num": 100, "sort": "symbol", "asc": 1, "node": "hs_a",
            "symbol": "", "_s_r_a": "page",
        }

        def fetch_page(page):
            page_params = dict(base_params)
            page_params["page"] = page
            response = self._request(
                SINA_MARKET_URL, page_params, timeout=(3, 10), retries=2,
                headers=sina_headers
            )
            if response is None:
                return []
            try:
                return response.json() or []
            except (ValueError, TypeError, json.JSONDecodeError):
                return []

        rows = []
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(fetch_page, page)
                       for page in range(1, math.ceil(total / 100) + 1)]
            for future in as_completed(futures):
                rows.extend(future.result())
        result = self._frame_from_sina(rows)
        coverage = len(result) / total if total else 0
        if coverage < 0.95:
            return pd.DataFrame(), "", f"新浪快照覆盖率不足: {coverage:.1%}"
        return result, "新浪财经行情", f"共{len(result)}只，覆盖率{coverage:.1%}"

    def _frame_from_eastmoney(self, rows: list) -> pd.DataFrame:
        values = [{
            "code": str(item.get("f12", "")), "name": item.get("f14", ""),
            "price": item.get("f2", 0) or 0, "change_pct": item.get("f3", 0) or 0,
            "change_amt": item.get("f4", 0) or 0, "high": item.get("f15", 0) or 0,
            "low": item.get("f16", 0) or 0, "volume": item.get("f5", 0) or 0,
            "amount": item.get("f6", 0) or 0, "turnover_rate": item.get("f8", 0) or 0,
            "amplitude": item.get("f7", 0) or 0,
            "market_cap": _safe_float(item.get("f20")) / 1e8,
            "total_market_cap": _safe_float(item.get("f20")) / 1e8,
            "pe": item.get("f9", 0) or 0, "pb": item.get("f23", 0) or 0,
            "volume_ratio": item.get("f10", 0) or 0,
            "main_net": _safe_float(item.get("f62")),
            "main_net_pct": _safe_float(item.get("f184")), "industry": "",
        } for item in rows if item.get("f12")]
        return self._normalize_stock_frame(pd.DataFrame(values))

    def _frame_from_sina(self, rows: list) -> pd.DataFrame:
        values = [{
            "code": str(item.get("code", "")), "name": item.get("name", ""),
            "price": item.get("trade", 0) or 0,
            "change_pct": item.get("changepercent", 0) or 0,
            "change_amt": item.get("pricechange", 0) or 0,
            "high": item.get("high", 0) or 0, "low": item.get("low", 0) or 0,
            "volume": item.get("volume", 0) or 0, "amount": item.get("amount", 0) or 0,
            "turnover_rate": item.get("turnoverratio", 0) or 0, "amplitude": 0,
            "market_cap": _safe_float(item.get("mktcap")) / 10000,
            "total_market_cap": _safe_float(item.get("mktcap")) / 10000,
            "pe": item.get("per", 0) or 0, "pb": item.get("pb", 0) or 0,
            "volume_ratio": np.nan, "main_net": 0, "main_net_pct": 0, "industry": "",
        } for item in rows if item.get("code")]
        return self._normalize_stock_frame(pd.DataFrame(values))

    def _normalize_stock_frame(self, result: pd.DataFrame) -> pd.DataFrame:
        if result.empty:
            return result
        result = result.drop_duplicates("code").copy()
        result["code"] = result["code"].astype(str).str.zfill(6)
        numeric_cols = [
            "price", "change_pct", "change_amt", "high", "low", "volume",
            "amount", "turnover_rate", "amplitude", "market_cap",
            "total_market_cap", "pe", "pb", "volume_ratio", "main_net",
            "main_net_pct",
        ]
        for col in numeric_cols:
            result[col] = pd.to_numeric(result[col], errors="coerce")
        result["board"] = result["code"].apply(self._get_board)
        result["is_st"] = result["name"].str.contains("ST|退", na=False)
        return result

    @staticmethod
    def _get_board(code):
        if code.startswith(("8", "4")):
            return "北交所"
        if code.startswith("3"):
            return "创业板"
        if code.startswith("68"):
            return "科创板"
        if code.startswith(("0", "1")):
            return "深圳主板"
        if code.startswith("6"):
            return "上海主板"
        return "其他"

    def _fetch_tencent_snapshot(self, codes: list, batch_size: int = 60) -> dict:
        """批量从腾讯财经获取实时行情快照"""
        result = {}

        def _code_to_tencent(code):
            if code.startswith(("4", "8", "92")):
                return f"bj{code}"
            if code.startswith(("6", "9")):
                return f"sh{code}"
            return f"sz{code}"

        tc_codes = [_code_to_tencent(c) for c in codes]
        batches = [tc_codes[i:i + batch_size] for i in range(0, len(tc_codes), batch_size)]

        def _fetch_batch(batch):
            url = "https://qt.gtimg.cn/q=" + ",".join(batch)
            try:
                r = self._request(url, timeout=(3, 8), retries=2)
                if r is None:
                    return {}
                r.encoding = "gbk"
                batch_result = {}
                for line in r.text.strip().split("\n"):
                    m = re.search(r'v_(\w+)="(.+?)"', line)
                    if not m:
                        continue
                    raw_code = m.group(1)
                    code = raw_code.replace("sh", "").replace("sz", "").replace("bj", "")
                    fields = m.group(2).split("~")
                    if len(fields) < 40:
                        continue
                    price = _safe_float(fields[3])
                    batch_result[code] = {
                        "name": fields[1],
                        "price": price,
                        "close_yest": _safe_float(fields[4]),
                        "open": _safe_float(fields[5]),
                        "change_pct": _safe_float(fields[32]),
                        "high": _safe_float(fields[33]),
                        "low": _safe_float(fields[34]),
                        "volume": _safe_float(fields[6]),
                        "amount": _safe_float(fields[37]) * 10000,
                        "turnover_rate": _safe_float(fields[38]),
                        "pe": _safe_float(fields[39]),
                        "pb": _safe_float(fields[43]),
                        "market_cap": _safe_float(fields[44]),
                        "volume_ratio": _safe_float(fields[49]),
                        "main_net": _safe_float(fields[50]) * 10000,
                    }
                return batch_result
            except Exception:
                return {}

        with ThreadPoolExecutor(max_workers=min(6, len(batches) or 1)) as executor:
            futures = [executor.submit(_fetch_batch, b) for b in batches]
            for future in as_completed(futures):
                result.update(future.result())

        return result

    # ──────────── 历史K线 ────────────

    def get_kline(self, code: str, period: str = "day",
                  count: int = 120) -> pd.DataFrame:
        """
        获取日K/周K/月K线数据（腾讯财经优先 + 东方财富后备）
        返回包含 MA5, MA10, MA20, MA60, MACD, RSI, KDJ, BOLL
        """
        df = self._get_kline_tencent(code, period, count)
        if not df.empty:
            self._set_source_state("kline", "腾讯财经", True)
            return df
        df = self._get_kline_eastmoney(code, period, count)
        if not df.empty:
            self._set_source_state("kline", "东方财富历史行情", True)
            return df
        self._set_source_state("kline", "腾讯财经/东方财富", False,
                               detail=f"{code} K线请求失败")
        return pd.DataFrame()

    def _get_kline_eastmoney(self, code: str, period: str = "day",
                             count: int = 120) -> pd.DataFrame:
        secid = self._get_secid(code)
        if not secid:
            return pd.DataFrame()
        period_map = {"day": "101", "week": "102", "month": "103"}
        params = {
            "secid": secid,
            "ut": "fa5fd1943c7b386f172d6893dbfd32bb",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": period_map.get(period, "101"),
            "fqt": "1",
            "end": "20500101",
            "lmt": count,
        }
        resp = self._request(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            params, timeout=(3, 10), retries=2
        )
        if resp is None:
            return pd.DataFrame()
        try:
            klines = (resp.json().get("data") or {}).get("klines", [])
        except (ValueError, TypeError, json.JSONDecodeError, AttributeError):
            return pd.DataFrame()
        rows = []
        for line in klines:
            parts = line.split(",")
            if len(parts) < 11:
                continue
            rows.append({
                "date": parts[0], "open": _safe_float(parts[1]),
                "close": _safe_float(parts[2]), "high": _safe_float(parts[3]),
                "low": _safe_float(parts[4]), "volume": _safe_float(parts[5]),
                "amount": _safe_float(parts[6]), "amplitude": _safe_float(parts[7]),
                "change_pct": _safe_float(parts[8]), "change_amt": _safe_float(parts[9]),
                "turnover": _safe_float(parts[10]),
            })
        df = pd.DataFrame(rows)
        if df.empty:
            return df
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        return self._calc_indicators(df)

    def _get_kline_tencent(self, code: str, period: str = "day",
                           count: int = 120) -> pd.DataFrame:
        if code == "000300":
            symbol = "sh000300"
        elif code.startswith(("6", "9")):
            symbol = f"sh{code}"
        elif code.startswith(("4", "8")):
            symbol = f"bj{code}"
        else:
            symbol = f"sz{code}"

        period_map = {"day": "day", "week": "week", "month": "month"}
        qt_period = period_map.get(period, "day")
        url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        params = {"param": f"{symbol},{qt_period},,,{count},qfq"}
        resp = self._request(url, params)
        if not resp:
            return pd.DataFrame()

        try:
            payload = resp.json().get("data", {}).get(symbol, {}) or {}
            rows_raw = payload.get(f"qfq{qt_period}") or payload.get(qt_period) or []
            rows = []
            for item in rows_raw:
                if len(item) < 6:
                    continue
                rows.append({
                    "date": item[0],
                    "open": float(item[1]),
                    "close": float(item[2]),
                    "high": float(item[3]),
                    "low": float(item[4]),
                    "volume": float(item[5]),
                    "amount": 0.0,
                    "amplitude": 0.0,
                    "change_pct": 0.0,
                    "change_amt": 0.0,
                    "turnover": 0.0,
                })
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        if df.empty:
            return df
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        return self._calc_indicators(df)

    def _calc_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """计算常用技术指标"""
        close = df["close"].values
        high = df["high"].values
        low = df["low"].values
        volume = df["volume"].values

        # ── 均线 ──
        for period in [5, 10, 20, 60]:
            if len(close) >= period:
                df[f"MA{period}"] = pd.Series(
                    np.convolve(close, np.ones(period)/period, mode="valid"),
                    index=df.index[period-1:]
                )
            else:
                df[f"MA{period}"] = np.nan

        # ── MACD ──
        ema_fast = self._ema(close, 12)
        ema_slow = self._ema(close, 26)
        dif = ema_fast - ema_slow
        dea = self._ema(dif, 9)
        macd_bar = 2 * (dif - dea)
        df["MACD_DIF"] = dif
        df["MACD_DEA"] = dea
        df["MACD_BAR"] = macd_bar

        # ── RSI ──
        period = 14
        if len(close) > period:
            delta = np.diff(close)
            gains = np.where(delta > 0, delta, 0)
            losses = np.where(delta < 0, -delta, 0)
            avg_gain = self._ema(gains, period)
            avg_loss = self._ema(losses, period)
            rs = avg_gain / np.maximum(avg_loss, 1e-10)
            rsi = 100 - (100 / (1 + rs))
            df["RSI"] = np.concatenate([np.full(1, np.nan), rsi])
        else:
            df["RSI"] = np.nan

        # ── KDJ ──
        period_k = 9
        if len(close) >= period_k:
            low_n = pd.Series(low).rolling(period_k).min().values
            high_n = pd.Series(high).rolling(period_k).max().values
            rsv = np.where(
                (high_n - low_n) != 0,
                (close - low_n) / (high_n - low_n) * 100,
                50
            )
            k = self._ema(rsv, 3)
            d = self._ema(k, 3)
            j = 3 * k - 2 * d
            df["KDJ_K"] = k
            df["KDJ_D"] = d
            df["KDJ_J"] = j
        else:
            df["KDJ_K"] = np.nan
            df["KDJ_D"] = np.nan
            df["KDJ_J"] = np.nan

        # ── 成交量均线 ──
        if len(volume) >= 5:
            df["VOL_MA5"] = pd.Series(
                np.convolve(volume, np.ones(5)/5, mode="valid"),
                index=df.index[4:]
            )
        else:
            df["VOL_MA5"] = np.nan
        if len(volume) >= 10:
            df["VOL_MA10"] = pd.Series(
                np.convolve(volume, np.ones(10)/10, mode="valid"),
                index=df.index[9:]
            )
        else:
            df["VOL_MA10"] = np.nan

        # ── BOLL ──
        period_boll = 20
        if len(close) >= period_boll:
            mid = pd.Series(close).rolling(period_boll).mean().values
            std = pd.Series(close).rolling(period_boll).std().values
            df["BOLL_MID"] = mid
            df["BOLL_UP"] = mid + 2 * std
            df["BOLL_DN"] = mid - 2 * std
        else:
            df["BOLL_MID"] = df["BOLL_UP"] = df["BOLL_DN"] = np.nan

        return df

    @staticmethod
    def _ema(values: np.ndarray, period: int) -> np.ndarray:
        """指数移动平均"""
        alpha = 2 / (period + 1)
        result = np.full_like(values, np.nan, dtype=float)
        if len(values) == 0:
            return result
        result[0] = values[0]
        for i in range(1, len(values)):
            if np.isnan(result[i-1]):
                result[i] = values[i]
            else:
                result[i] = alpha * values[i] + (1 - alpha) * result[i-1]
        return result

    # ──────────── 实时行情 ────────────

    def get_realtime_quotes(self, codes: List[str]) -> pd.DataFrame:
        """
        获取实时行情（腾讯财经优先，新浪财经后备）
        codes: 股票代码列表，如 ["600519", "000858"]
        """
        if not codes:
            return pd.DataFrame()

        snapshot = self._fetch_tencent_snapshot(codes)
        if snapshot:
            rows = [{"code": code, **snapshot[code]} for code in codes if code in snapshot]
            frame = pd.DataFrame(rows)
            if not frame.empty:
                self._set_source_state("realtime", "腾讯财经", True)
                return frame

        frame = self._get_realtime_quotes_sina(codes)
        if not frame.empty:
            self._set_source_state("realtime", "新浪财经", True,
                                   detail="腾讯行情不可用，已回退")
            return frame
        self._set_source_state("realtime", "腾讯财经/新浪财经", False,
                               detail="即时行情请求失败")
        return pd.DataFrame()

    def _get_realtime_quotes_sina(self, codes: List[str]) -> pd.DataFrame:
        # 构建新浪格式代码
        sina_codes = []
        code_map = {}
        for code in codes:
            prefix = "sh" if code.startswith(("6", "9")) else "sz"
            s_code = f"{prefix}{code}"
            sina_codes.append(s_code)
            code_map[s_code] = code

        url = "https://hq.sinajs.cn/list=" + ",".join(sina_codes)
        resp = self._request(
            url, timeout=(3, 8), retries=2,
            headers={"Referer": "https://finance.sina.com.cn"}
        )
        if not resp:
            return pd.DataFrame()
        resp.encoding = "gbk"

        rows = []
        for line in resp.text.strip().split("\n"):
            if not line:
                continue
            try:
                match = re.search(r'hq_str_(\w+)="(.+)"', line)
                if not match:
                    continue
                s_code = match.group(1)
                values = match.group(2).split(",")
                if len(values) < 32:
                    continue
                rows.append({
                    "code": code_map.get(s_code, s_code),
                    "name": values[0],
                    "open": float(values[1]) if values[1] else 0,
                    "close_yest": float(values[2]) if values[2] else 0,
                    "price": float(values[3]) if values[3] else 0,
                    "high": float(values[4]) if values[4] else 0,
                    "low": float(values[5]) if values[5] else 0,
                    "volume": float(values[8]) if values[8] else 0,
                    "amount": float(values[9]) if values[9] else 0,
                })
            except (ValueError, IndexError):
                continue

        df = pd.DataFrame(rows)
        if not df.empty:
            df["change_pct"] = ((df["price"] - df["close_yest"])
                                / df["close_yest"].replace(0, np.nan) * 100)
        return df

    # ──────────── 辅助方法 ────────────

    def _get_secid(self, code: str) -> Optional[str]:
        """获取东方财富secid格式"""
        if code == "000300":
            return "1.000300"
        if code.startswith(("6", "9")):
            return f"1.{code}"
        elif code.startswith(("0", "3", "2")):
            return f"0.{code}"
        elif code.startswith(("4", "8")):
            return f"0.{code}"
        return None

    def get_stock_info(self, code: str) -> dict:
        """获取单只股票基本信息"""
        secid = self._get_secid(code)
        if not secid:
            return {}
        params = {
            "secid": secid,
            "ut": "fa5fd1943c7b386f172d6893dbfd32bb",
            "fields": "f43,f44,f45,f46,f47,f48,f49,f50,f51,f52,f55,f57,f58,f84,f85",
        }
        resp, _ = self._request_eastmoney("/api/qt/stock/get", params)
        if not resp:
            return {}
        try:
            d = resp.json().get("data", {}) or {}
            return {
                "code": code,
                "open": _safe_float(d.get("f44")),
                "close": _safe_float(d.get("f43")),
                "high": _safe_float(d.get("f45")),
                "low": _safe_float(d.get("f46")),
                "volume": _safe_float(d.get("f47")),
                "amount": _safe_float(d.get("f48")),
                "pe": _safe_float(d.get("f57")),
                "amplitude": _safe_float(d.get("f43")),
            }
        except (json.JSONDecodeError, KeyError, TypeError):
            return {}

    def get_concept_board(self) -> pd.DataFrame:
        """获取概念板块列表（用于板块热度分析）"""
        params = {
            "pn": 1, "pz": 500, "po": 1, "np": 1,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": 2, "invt": 2, "fid": "f3", "fs": "m:90+t:3",
            "fields": "f12,f14,f2,f3,f4,f8,f20",
        }
        resp, _ = self._request_eastmoney("/api/qt/clist/get", params)
        if not resp:
            return pd.DataFrame()
        try:
            items = (resp.json().get("data") or {}).get("diff", [])
            rows = [{
                "code": i.get("f12"),
                "name": i.get("f14"),
                "price": i.get("f2"),
                "change_pct": i.get("f3"),
                "rise_count": i.get("f8"),
                "total_market_cap": i.get("f20", 0),
            } for i in items if i.get("f12")]
            return pd.DataFrame(rows)
        except (json.JSONDecodeError, KeyError, TypeError):
            return pd.DataFrame()


    # ──────────── 资金流向 ────────────
    def get_sector_fund_flow(self, top_n: int = 20) -> pd.DataFrame:
        """获取行业板块资金流向排行"""
        params = {
            "pn": 1, "pz": top_n, "po": 1, "np": 1,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": 2, "invt": 2, "fid": "f62",
            "fs": "m:90+t:2",
            "fields": "f12,f14,f2,f3,f4,f62,f184,f204,f205,f62",
        }
        resp, _ = self._request_eastmoney("/api/qt/clist/get", params)
        if not resp:
            return pd.DataFrame()
        try:
            items = (resp.json().get("data") or {}).get("diff", [])
            rows = []
            for i in items:
                if not i.get("f12"):
                    continue
                main_net = _safe_float(i.get("f62")) / 1e8
                rows.append({
                    "code": i["f12"],
                    "name": i.get("f14", ""),
                    "change_pct": round(_safe_float(i.get("f3")), 2),
                    "price": round(_safe_float(i.get("f2")), 2),
                    "main_net_inflow": round(main_net, 2),
                    "main_net_pct": round(_safe_float(i.get("f184")), 2),
                    "rise_count": _safe_float(i.get("f204")),
                    "fall_count": _safe_float(i.get("f205")),
                })
            return pd.DataFrame(rows)
        except (json.JSONDecodeError, KeyError, TypeError):
            return pd.DataFrame()

    def get_concept_fund_flow(self, top_n: int = 20) -> pd.DataFrame:
        """获取概念板块资金流向排行"""
        params = {
            "pn": 1, "pz": top_n, "po": 1, "np": 1,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": 2, "invt": 2, "fid": "f62",
            "fs": "m:90+t:3",
            "fields": "f12,f14,f2,f3,f4,f62,f184,f204,f205",
        }
        resp, _ = self._request_eastmoney("/api/qt/clist/get", params)
        if not resp:
            return pd.DataFrame()
        try:
            items = (resp.json().get("data") or {}).get("diff", [])
            rows = []
            for i in items:
                if not i.get("f12"):
                    continue
                main_net = _safe_float(i.get("f62")) / 1e8
                rows.append({
                    "code": i["f12"],
                    "name": i.get("f14", ""),
                    "change_pct": round(_safe_float(i.get("f3")), 2),
                    "main_net_inflow": round(main_net, 2),
                    "main_net_pct": round(_safe_float(i.get("f184")), 2),
                })
            return pd.DataFrame(rows)
        except (json.JSONDecodeError, KeyError, TypeError):
            return pd.DataFrame()

    def get_board_constituents(self, board_code: str, limit: int = 1000) -> set:
        """获取行业或概念板块成分股代码，用于将资金热点映射至候选股。"""
        params = {
            "pn": 1, "pz": limit, "po": 1, "np": 1,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": 2, "invt": 2, "fid": "f3",
            "fs": f"b:{board_code}",
            "fields": "f12,f14",
        }
        resp, _ = self._request_eastmoney(
            "/api/qt/clist/get", params, prefer_delay=True
        )
        if resp is None:
            return set()
        try:
            items = (resp.json().get("data") or {}).get("diff", [])
            return {str(item.get("f12", "")).zfill(6) for item in items if item.get("f12")}
        except (ValueError, TypeError, json.JSONDecodeError, AttributeError):
            return set()

    def get_board_flow_history(self, board_code: str, days: int = 5) -> dict:
        """获取板块近期主力资金轨迹，数值单位标准化为亿元。"""
        unavailable = {
            "recent_main_net_inflow": None,
            "positive_days": None,
            "days": 0,
        }
        if time.time() < self._board_history_unavailable_until:
            return unavailable
        params = {
            "secid": f"90.{board_code}",
            "ut": "b2884a393a59ad64002292a3e90d46a5",
            "lmt": max(1, days),
            "klt": 101,
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55",
        }
        resp = self._request(
            "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
            params, timeout=(2, 4), retries=1
        )
        if resp is None:
            self._board_history_unavailable_until = time.time() + 300
            return unavailable
        try:
            lines = (resp.json().get("data") or {}).get("klines", [])
        except (ValueError, TypeError, json.JSONDecodeError, AttributeError):
            lines = []
        if not lines:
            return unavailable
        main_flows = []
        for line in lines:
            parts = str(line).split(",")
            if len(parts) >= 2:
                main_flows.append(_safe_float(parts[1]) / 1e8)
        return {
            "recent_main_net_inflow": round(sum(main_flows), 2),
            "positive_days": sum(flow > 0 for flow in main_flows),
            "days": len(main_flows),
        }

    def get_rotation_matches(self, candidate_codes: list, top_n: int = 8) -> dict:
        """将资金领先的行业/概念板块与综合候选股对应起来。"""
        target_codes = {str(code).zfill(6) for code in candidate_codes}
        matches = {code: [] for code in target_codes}
        board_specs = []
        sources = (
            ("行业", self.get_sector_fund_flow(top_n)),
            ("概念", self.get_concept_fund_flow(top_n)),
        )
        for board_type, frame in sources:
            if frame.empty:
                continue
            for rank, (_, row) in enumerate(frame.head(top_n).iterrows(), start=1):
                board_specs.append((board_type, rank, row))
        if not board_specs:
            return {"boards": [], "matches": matches}

        first_code = str(board_specs[0][2].get("code", ""))
        history_probe = self.get_board_flow_history(first_code)

        def build_board(spec_index, board_type, rank, row):
            flow = float(row.get("main_net_inflow") or 0)
            change_pct = float(row.get("change_pct") or 0)
            if spec_index == 0:
                history = history_probe
            elif history_probe["days"]:
                history = self.get_board_flow_history(str(row.get("code", "")))
            else:
                history = {
                    "recent_main_net_inflow": None,
                    "positive_days": None,
                    "days": 0,
                }
            score = 35 + max(0, (top_n - rank + 1) * 3)
            score += min(20, max(0, flow))
            if history["positive_days"] is not None:
                score += min(15, history["positive_days"] * 3)
            if (history["recent_main_net_inflow"] is not None
                    and history["recent_main_net_inflow"] > 0):
                score += 10
            if change_pct > 0:
                score += min(5, change_pct)
            board = {
                "type": board_type,
                "code": str(row.get("code", "")),
                "name": str(row.get("name", "")),
                "change_pct": round(change_pct, 2),
                "main_net_inflow": round(flow, 2),
                "recent_main_net_inflow": history["recent_main_net_inflow"],
                "positive_days": history["positive_days"],
                "flow_score": round(max(0.0, min(100.0, score))),
            }
            members = self.get_board_constituents(board["code"])
            return board, members

        boards = []
        with ThreadPoolExecutor(max_workers=min(6, len(board_specs))) as executor:
            futures = [
                executor.submit(build_board, index, board_type, rank, row)
                for index, (board_type, rank, row) in enumerate(board_specs)
            ]
            for future in as_completed(futures):
                board, members = future.result()
                boards.append(board)
                for code in target_codes.intersection(members):
                    matches[code].append(board)
        boards.sort(
            key=lambda board: (board["type"] != "行业", -board["flow_score"])
        )
        return {"boards": boards, "matches": matches}

    def get_market_metrics(self) -> dict:
        """获取大盘情绪指标（从股票列表缓存计算）"""
        stocks = self.get_stock_list()
        if stocks.empty:
            return {}
        up = int((stocks["change_pct"] > 0).sum())
        down = int((stocks["change_pct"] < 0).sum())
        flat = int((stocks["change_pct"] == 0).sum())
        limit_up = int((stocks["change_pct"] >= 9.8).sum())
        limit_down = int((stocks["change_pct"] <= -9.8).sum())
        advance_ratio = round(up / max(down, 1), 2)
        if advance_ratio >= 1.2 and limit_up > limit_down * 2:
            mood = "偏强"
        elif advance_ratio <= 0.8:
            mood = "偏弱"
        else:
            mood = "震荡"
        return {
            "total": len(stocks), "up": up, "down": down, "flat": flat,
            "limit_up": limit_up, "limit_down": limit_down,
            "advance_ratio": advance_ratio, "mood": mood,
        }

    def get_etf_list(self) -> pd.DataFrame:
        """获取ETF基金列表"""
        params = {
            "pn": 1, "pz": 100, "po": 1, "np": 1,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": 2, "invt": 2, "fid": "f3",
            "fs": "b:MK0021,b:MK0022,b:MK0023,b:MK0024",
            "fields": "f12,f14,f2,f3,f6,f7,f8",
        }
        resp, _ = self._request_eastmoney("/api/qt/clist/get", params)
        if not resp:
            return pd.DataFrame()
        try:
            items = (resp.json().get("data") or {}).get("diff", [])
            rows = []
            for i in items:
                amp = _safe_float(i.get("f7"))
                if amp > 15:
                    continue
                rows.append({
                    "code": i.get("f12", ""),
                    "name": i.get("f14", ""),
                    "price": round(_safe_float(i.get("f2")), 3),
                    "change_pct": round(_safe_float(i.get("f3")), 2),
                    "amount": round(_safe_float(i.get("f6")) / 1e8, 2),
                    "turnover": _safe_float(i.get("f8")),
                })
            return pd.DataFrame(rows)
        except (json.JSONDecodeError, KeyError, TypeError):
            return pd.DataFrame()



    def get_fund_flow(self, code: str) -> dict:
        """获取个股资金流向"""
        secid = self._get_secid(code)
        if not secid:
            return {}

        stocks = self._stock_list_cache
        if stocks is not None and not stocks.empty and "main_net" in stocks.columns:
            match = stocks[stocks["code"] == code]
            if not match.empty:
                row = match.iloc[0]
                return {
                    "code": code,
                    "name": row.get("name", ""),
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "main_net": float(row.get("main_net") or 0),
                    "main_net_pct": float(row.get("main_net_pct") or 0),
                }

        return {}

    def get_intraday_minute(self, code: str) -> dict:
        """获取今日分时分钟数据（腾讯财经），返回分时趋势摘要。"""
        code = str(code).zfill(6)
        if code.startswith(("6", "9")):
            symbol = f"sh{code}"
        elif code.startswith(("4", "8")):
            symbol = f"bj{code}"
        else:
            symbol = f"sz{code}"
        resp = self._request(
            "https://web.ifzq.gtimg.cn/appstock/app/minute/query",
            {"param": symbol}, timeout=(3, 8), retries=2,
        )
        if not resp:
            return {"available": False, "error": "分时数据请求失败"}

        try:
            payload = resp.json()
            data_section = (payload.get("data") or {}).get(symbol) or {}
            mins_raw = data_section.get("MINS") or data_section.get("data") or []
            if not mins_raw or not isinstance(mins_raw, list):
                return {"available": False, "error": "无分时数据（可能未开市）"}
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
            return {"available": False, "error": "分时响应解析失败"}

        rows = []
        for entry in mins_raw:
            parts = str(entry).split()
            if len(parts) < 3:
                continue
            try:
                rows.append({
                    "time": parts[0],
                    "price": float(parts[1]),
                    "volume": float(parts[2]),
                    "avg_price": float(parts[3]) if len(parts) > 3 else float(parts[1]),
                })
            except (ValueError, IndexError):
                continue

        if not rows:
            return {"available": False, "error": "分时数据解析后为空"}

        # 计算分时趋势指标
        prices = [r["price"] for r in rows]
        volumes = [r["volume"] for r in rows]
        first_price = prices[0]
        last_price = prices[-1]
        avg_price = sum(prices) / len(prices)

        # 上涨分钟占比
        up_minutes = sum(1 for i in range(1, len(prices)) if prices[i] >= prices[i - 1])
        up_ratio = round(up_minutes / max(1, len(prices) - 1), 4)

        # 价格相对开盘位置
        max_price = max(prices)
        min_price = min(prices)
        price_range = max_price - min_price
        position_in_day = round(
            (last_price - min_price) / max(price_range, 0.01), 2
        ) if price_range > 0.01 else 0.5

        # 尾盘30分钟趋势（最后10个数据点，约30分钟）
        tail = rows[-10:] if len(rows) >= 10 else rows
        tail_start_price = tail[0]["price"]
        tail_end_price = tail[-1]["price"]
        tail_trend = "up" if tail_end_price > tail_start_price else (
            "down" if tail_end_price < tail_start_price else "flat"
        )

        # 量能集中度：前30分钟 vs 最后30分钟
        first_third = int(len(rows) * 0.3)
        last_third = rows[-first_third:] if first_third > 0 else rows
        first_vol = sum(volumes[:first_third]) if first_third > 0 else 0
        last_vol = sum(r["volume"] for r in last_third) if first_third > 0 else 0
        volume_concentration = round(
            last_vol / max(first_vol, 1), 2
        ) if first_vol > 0 else 0
        # >1 表示尾盘量能更强（好信号），<0.5 表示早盘放量后衰竭

        # 价格在均线上方的时间占比
        above_avg = sum(1 for r in rows if r["price"] >= r["avg_price"])
        above_avg_ratio = round(above_avg / max(1, len(rows)), 4)

        return {
            "available": True,
            "minute_count": len(rows),
            "open_price": round(first_price, 2),
            "close_price": round(last_price, 2),
            "avg_price": round(avg_price, 2),
            "high": round(max_price, 2),
            "low": round(min_price, 2),
            "change_pct": round(
                (last_price - first_price) / max(first_price, 0.01) * 100, 2
            ),
            "up_minute_ratio": up_ratio,
            "position_in_day": position_in_day,
            "tail_trend": tail_trend,
            "volume_concentration": volume_concentration,
            "above_avg_ratio": above_avg_ratio,
        }

    def get_intraday_stock_fund_flow(self, code: str) -> dict:
        """获取个股今日资金流向明细（东方财富），含大小单拆分。"""
        secid = self._get_secid(code)
        if not secid:
            return {"available": False, "error": "无效代码"}
        params = {
            "secid": secid,
            "ut": "b2884a393a59ad64002292a3e90d46a5",
            "fields": "f62,f66,f68,f72,f74,f70,f78,f76,f80,f184,f64",
        }
        resp = self._request(
            "https://push2.eastmoney.com/api/qt/stock/get",
            params, timeout=(3, 8), retries=2,
        )
        if resp is None:
            return {"available": False, "error": "资金流请求失败"}
        try:
            d = (resp.json().get("data") or {})
        except (json.JSONDecodeError, TypeError, AttributeError):
            return {"available": False, "error": "资金流响应解析失败"}

        main_net = _safe_float(d.get("f62"))
        main_net_pct = _safe_float(d.get("f184"))
        super_large_net = _safe_float(d.get("f66"))
        large_net = _safe_float(d.get("f68"))
        medium_net = _safe_float(d.get("f72"))
        small_net = _safe_float(d.get("f74"))

        return {
            "available": True,
            "code": code,
            "main_net": round(main_net, 2),
            "main_net_pct": round(main_net_pct, 2),
            "super_large_net": round(super_large_net, 2),
            "large_net": round(large_net, 2),
            "medium_net": round(medium_net, 2),
            "small_net": round(small_net, 2),
            "active_buy_ratio": round(
                (super_large_net + large_net) / max(abs(main_net), 1) * 100, 1
            ) if main_net != 0 else 0,
        }

    def get_financial_news(self, count: int = 30) -> list:
        """获取最新财经新闻（东方财富）"""
        url = "https://np-listapi.eastmoney.com/comm/web/getFastNewsListByDate"
        params = {
            "client": "web",
            "biz": "fast_news",
            "fastColumn": "102",
            "sortEnd": "",
            "pageIndex": 1,
            "pageSize": min(count, 100),
            "req_timestamp": int(time.time() * 1000),
        }
        try:
            resp = self._request(url, params)
            if not resp:
                return self._get_news_fallback(count)
            data = resp.json()
            items = data.get("data", {}).get("fastNewsList", [])
            if not items:
                return self._get_news_fallback(count)
            news_list = []
            for item in items[:count]:
                news_list.append({
                    "title": item.get("title", ""),
                    "summary": item.get("summary", ""),
                    "time": item.get("showTime", ""),
                    "source": "东方财富",
                })
            return news_list
        except Exception:
            return self._get_news_fallback(count)

    def _get_news_fallback(self, count: int) -> list:
        """备用新闻源：东方财富要闻列表"""
        params = {
            "pn": 1, "pz": min(count, 50), "po": 1, "np": 1,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": 2, "invt": 2, "fid": "f3",
            "fs": "m:0+t:1", "fields": "f12,f14,f136",
        }
        try:
            resp, _ = self._request_eastmoney("/api/qt/clist/get", params)
            if not resp:
                return []
            data = resp.json()
            data_section = data.get("data") or {}
            items = data_section.get("diff") or []
            news_list = []
            for item in items:
                code = item.get("f12", "")
                name = item.get("f14", "")
                if code and name:
                    news_list.append({
                        "title": name,
                        "summary": "",
                        "code": code,
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "source": "东方财富",
                    })
            return news_list
        except Exception:
            return []

    def get_hot_concepts(self, top_n: int = 20) -> list:
        """获取当日热门概念板块排行（综合资金+涨幅）"""
        concept_flow = self.get_concept_fund_flow(top_n)
        if concept_flow.empty:
            return []

        hot = []
        for _, row in concept_flow.iterrows():
            hot.append({
                "name": row.get("name", ""),
                "change_pct": round(float(row.get("change_pct") or 0), 2),
                "main_net_inflow": round(float(row.get("main_net_inflow") or 0), 2),
                "main_net_pct": round(float(row.get("main_net_pct") or 0), 2),
            })
        return hot

    def get_market_context(self) -> dict:
        """获取AI分析所需的完整市场上下文数据"""
        stocks = self.get_stock_list()
        context = {
            "market_stats": {
                "total": len(stocks),
                "up": int((stocks["change_pct"] > 0).sum()),
                "down": int((stocks["change_pct"] < 0).sum()),
                "limit_up": int((stocks["change_pct"] >= 9.8).sum()),
                "limit_down": int((stocks["change_pct"] <= -9.8).sum()),
                "avg_change": round(stocks["change_pct"].mean(), 2),
            },
            "top_gainers": [],
            "top_losers": [],
            "hot_concepts": self.get_hot_concepts(15),
            "sector_flow": [],
        }

        cols = ["code", "name", "price", "change_pct", "turnover_rate", "market_cap", "board"]
        for _, s in stocks.nlargest(10, "change_pct")[cols].iterrows():
            context["top_gainers"].append({
                "code": s["code"], "name": s["name"],
                "price": round(s["price"], 2),
                "change_pct": round(s["change_pct"], 2),
                "board": s.get("board", ""),
            })
        for _, s in stocks.nsmallest(10, "change_pct")[cols].iterrows():
            context["top_losers"].append({
                "code": s["code"], "name": s["name"],
                "price": round(s["price"], 2),
                "change_pct": round(s["change_pct"], 2),
                "board": s.get("board", ""),
            })

        sector = self.get_sector_fund_flow(15)
        if not sector.empty:
            for _, row in sector.iterrows():
                context["sector_flow"].append({
                    "name": row.get("name", ""),
                    "change_pct": round(float(row.get("change_pct") or 0), 2),
                    "main_net_inflow": round(float(row.get("main_net_inflow") or 0), 2),
                })

        return context

    @staticmethod
    def _eastmoney_security_code(code: str) -> str:
        suffix = "SH" if str(code).startswith(("6", "9")) else "SZ"
        if str(code).startswith(("4", "8")):
            suffix = "BJ"
        return f"{str(code).zfill(6)}.{suffix}"

    def get_financial_data(self, code: str, force_refresh: bool = False) -> dict:
        """逐股取得最近财务指标报告，并合并行情中的估值字段。

        财务指标来自东方财富数据中心 RPT_LICO_FN_CPD 报告接口；行情估值
        PE/PB 与市值来自全市场快照。接口失败时不编造财务指标。
        """
        code = str(code).zfill(6)
        cached = self._financial_cache.get(code)
        if (cached and not force_refresh
                and time.time() - cached["cached_at"] < self._financial_cache_ttl):
            return dict(cached["data"])

        quote = {}
        stocks = self._stock_list_cache
        if stocks is not None and not stocks.empty:
            match = stocks[stocks["code"] == code]
            if not match.empty:
                row = match.iloc[0]
                quote = {
                    "name": str(row.get("name", "")),
                    "pe": round(float(row.get("pe") or 0), 2),
                    "pb": round(float(row.get("pb") or 0), 2),
                    "market_cap": round(float(row.get("market_cap") or 0), 2),
                    "price": round(float(row.get("price") or 0), 2),
                    "change_pct": round(float(row.get("change_pct") or 0), 2),
                    "turnover_rate": round(float(row.get("turnover_rate") or 0), 2),
                    "board": str(row.get("board", "")),
                    "amount": round(float(row.get("amount") or 0), 2),
                    "main_net": round(float(row.get("main_net") or 0) / 1e8, 2),
                    "main_net_pct": round(float(row.get("main_net_pct") or 0), 2),
                }

        params = {
            "reportName": "RPT_LICO_FN_CPD",
            "columns": "ALL",
            "filter": f'(SECUCODE="{self._eastmoney_security_code(code)}")',
            "pageNumber": 1,
            "pageSize": 8,
            "sortColumns": "REPORTDATE",
            "sortTypes": "-1",
            "source": "WEB",
            "client": "WEB",
        }
        resp = self._request(
            EASTMONEY_FINANCIAL, params, timeout=(3, REQUEST_TIMEOUT), retries=2,
            headers={"Referer": "https://data.eastmoney.com/"}
        )
        if resp is None:
            self._set_source_state("fundamentals", "东方财富财务指标", False,
                                   detail=f"{code} 请求失败")
            return {"code": code, **quote, "available": False,
                    "data_source": "东方财富财务指标", "error": "财务接口请求失败"}

        try:
            rows = ((resp.json().get("result") or {}).get("data") or [])
        except (ValueError, TypeError, json.JSONDecodeError, AttributeError):
            rows = []
        if not rows:
            return {"code": code, **quote, "available": False,
                    "data_source": "东方财富财务指标", "error": "无财务指标报告"}

        latest = rows[0]
        report_date = str(latest.get("REPORTDATE") or "")[:10]
        quarter = int(report_date[5:7]) // 3 if len(report_date) >= 7 else 0
        result = {
            "code": code,
            "name": quote.get("name") or str(latest.get("SECURITY_NAME_ABBR") or ""),
            **{key: value for key, value in quote.items() if key != "name"},
            "available": True,
            "data_source": "东方财富财务指标 API",
            "report_date": report_date,
            "notice_date": str(latest.get("NOTICE_DATE") or latest.get("UPDATE_DATE") or "")[:10],
            "report_quarter": quarter,
            "eps": round(_safe_float(latest.get("BASIC_EPS")), 4),
            "bps": round(_safe_float(latest.get("BPS")), 4),
            "revenue": round(_safe_float(latest.get("TOTAL_OPERATE_INCOME")) / 1e8, 2),
            "net_profit": round(_safe_float(latest.get("PARENT_NETPROFIT")) / 1e8, 2),
            "roe": round(_safe_float(latest.get("WEIGHTAVG_ROE")), 2),
            "revenue_growth": round(_safe_float(latest.get("YSTZ")), 2),
            "profit_growth": round(_safe_float(latest.get("SJLTZ")), 2),
            "gross_margin": (round(_safe_float(latest.get("XSMLL")), 2)
                             if latest.get("XSMLL") is not None else None),
            "operating_cf_per_share": round(_safe_float(latest.get("MGJYXJJE")), 4),
        }
        result["annualized_roe"] = round(
            result["roe"] * (4 / quarter), 2
        ) if quarter else result["roe"]
        self._financial_cache[code] = {"cached_at": time.time(), "data": result}
        self._set_source_state("fundamentals", "东方财富财务指标 API", True,
                               detail=f"最近取得 {code} {report_date} 报告")
        return dict(result)

    def get_financials_batch(self, codes: list, progress_callback=None) -> dict:
        """以有限并发为候选池逐股取得财务指标，并持续回报完成进度。"""
        results = {}
        total = len(codes)
        if not codes:
            return results
        with ThreadPoolExecutor(max_workers=min(6, total)) as executor:
            futures = {
                executor.submit(self.get_financial_data, code): code for code in codes
            }
            for index, future in enumerate(as_completed(futures), start=1):
                code = futures[future]
                try:
                    fin = future.result()
                except Exception as exc:
                    fin = {
                        "code": str(code).zfill(6),
                        "available": False,
                        "error": str(exc),
                    }
                results[str(code).zfill(6)] = fin
                if progress_callback:
                    progress_callback(index, total, code, bool(fin.get("available")))
        return results