import unittest

import pandas as pd

from portfolio_manager import PortfolioManager


class FakePortfolioFeed:
    def get_realtime_quotes(self, codes):
        return pd.DataFrame([{
            "code": "000001",
            "name": "平安银行",
            "price": 10.6,
            "change_pct": 1.2,
        }])

    def get_kline(self, code, count=80):
        return pd.DataFrame([
            {
                "date": pd.Timestamp("2026-05-28"),
                "close": 10.0,
                "volume": 1000,
                "VOL_MA5": 900,
                "MA5": 9.8,
                "MA10": 9.7,
                "MA20": 9.5,
                "MA60": 9.2,
                "MACD_DIF": 0.12,
                "MACD_DEA": 0.08,
                "RSI": 55,
            },
            {
                "date": pd.Timestamp("2026-05-29"),
                "close": 10.6,
                "volume": 1200,
                "VOL_MA5": 1000,
                "MA5": 10.1,
                "MA10": 9.9,
                "MA20": 9.7,
                "MA60": 9.3,
                "MACD_DIF": 0.18,
                "MACD_DEA": 0.10,
                "RSI": 58,
            },
        ])

    def get_fund_flow(self, code):
        return {"main_net": 1200000, "main_net_pct": 2.1}


class PortfolioManagerTest(unittest.TestCase):
    def test_enrich_adds_risk_levels_and_market_context(self):
        manager = PortfolioManager(data_feed=FakePortfolioFeed())
        result = manager.enrich({
            "capital": 100000,
            "positions": [{"code": "000001", "quantity": 1000, "cost_price": 10.0}],
        })

        position = result["positions"][0]
        self.assertEqual(position["name"], "平安银行")
        self.assertAlmostEqual(position["pnl"], 600.0)
        self.assertGreater(position["weight"], 0)
        self.assertIn("stop_loss_price", position)
        self.assertIn("take_profit_price", position)
        self.assertTrue(position["technical"]["available"])
        self.assertEqual(position["fund_flow"]["main_net_pct"], 2.1)


if __name__ == "__main__":
    unittest.main()
