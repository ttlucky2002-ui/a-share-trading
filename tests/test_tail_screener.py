import unittest
from datetime import datetime

import pandas as pd

from screener_tail import TailEndScreener


class YesterdayKlineFeed:
    def __init__(self):
        today = pd.Timestamp(datetime.now().date())
        self.kline = pd.DataFrame([
            {
                "date": today - pd.Timedelta(days=3),
                "open": 10.0,
                "close": 10.4,
                "high": 10.5,
                "low": 9.9,
                "volume": 900,
                "VOL_MA5": 900,
                "MA5": 10.0,
                "MA10": 9.9,
                "MA20": 9.8,
                "MA60": 9.7,
                "MACD_DIF": 0.08,
                "MACD_DEA": 0.05,
                "RSI": 50,
            },
            {
                "date": today - pd.Timedelta(days=2),
                "open": 10.4,
                "close": 10.8,
                "high": 10.9,
                "low": 10.3,
                "volume": 1000,
                "VOL_MA5": 950,
                "MA5": 10.3,
                "MA10": 10.1,
                "MA20": 9.9,
                "MA60": 9.8,
                "MACD_DIF": 0.10,
                "MACD_DEA": 0.07,
                "RSI": 51,
            },
            {
                "date": today - pd.Timedelta(days=1),
                "open": 10.8,
                "close": 11.0,
                "high": 11.1,
                "low": 10.7,
                "volume": 1200,
                "VOL_MA5": 1000,
                "MA5": 10.7,
                "MA10": 10.5,
                "MA20": 10.2,
                "MA60": 9.9,
                "MACD_DIF": 0.18,
                "MACD_DEA": 0.09,
                "RSI": 53,
            },
            {
                "date": today,
                "open": 11.5,
                "close": 12.5,
                "high": 12.6,
                "low": 11.4,
                "volume": 5000,
                "VOL_MA5": 1800,
                "MA5": 11.2,
                "MA10": 10.7,
                "MA20": 10.3,
                "MA60": 10.0,
                "MACD_DIF": 0.55,
                "MACD_DEA": 0.20,
                "RSI": 78,
            },
        ])

    def get_kline(self, code, count=80):
        return self.kline.copy()


class IntradayFlowFeed:
    def get_rotation_matches(self, codes, top_n=10):
        board = {
            "type": "概念",
            "code": "BK0001",
            "name": "低空经济",
            "main_net_inflow": 8.5,
            "flow_score": 86,
        }
        return {"boards": [board], "matches": {codes[0]: [board]}}

    def get_realtime_quotes(self, codes):
        return pd.DataFrame([{
            "code": codes[0],
            "price": 11.25,
            "close_yest": 11.0,
            "change_pct": 2.2,
            "volume": 200000,
            "amount": 22500000,
            "volume_ratio": 1.3,
            "high": 11.4,
            "low": 10.95,
        }])

    def get_intraday_minute(self, code):
        return {
            "available": True,
            "position_in_day": 0.78,
            "change_pct": 1.4,
            "up_minute_ratio": 0.62,
            "tail_trend": "up",
            "above_avg_ratio": 0.72,
            "volume_concentration": 0.9,
        }

    def get_intraday_stock_fund_flow(self, code):
        return {
            "available": True,
            "main_net": 8_000_000,
            "main_net_pct": 2.3,
            "super_large_net": 2_000_000,
            "large_net": 2_500_000,
        }


class TailEndScreenerTest(unittest.TestCase):
    def test_stage1_uses_completed_yesterday_not_today(self):
        screener = TailEndScreener(data_feed=YesterdayKlineFeed())

        result = screener._score_yesterday_kline("000001")

        today_text = pd.Timestamp(datetime.now().date()).strftime("%Y-%m-%d")
        self.assertTrue(result["available"])
        self.assertNotEqual(result["yesterday_date"], today_text)
        self.assertAlmostEqual(result["yesterday_close"], 11.0)
        self.assertAlmostEqual(result["yesterday_change"], round((11.0 - 10.8) / 10.8 * 100, 2))

    def test_stage2_attaches_fund_flow_board_matches(self):
        screener = TailEndScreener(data_feed=IntradayFlowFeed())
        candidates = [{
            "code": "000001",
            "name": "测试股份",
            "score": 82,
            "reason": "昨日结构良好",
        }]

        verified = screener._stage2_verify(candidates)

        self.assertEqual(len(verified), 1)
        stage2 = verified[0]["stage2"]
        self.assertTrue(stage2["available"])
        self.assertIn("低空经济", stage2["tomorrow_boards"])
        self.assertGreaterEqual(stage2["sector_score"], 80)


if __name__ == "__main__":
    unittest.main()
