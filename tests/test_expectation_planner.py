import os
import tempfile
import unittest

import pandas as pd

import expectation_planner as planner_module
from expectation_planner import ExpectationPlanner


class FakeExpectationFeed:
    def __init__(self, price=10.2):
        self.price = price

    def get_realtime_quotes(self, codes):
        return pd.DataFrame([{
            "code": codes[0],
            "name": "平安银行",
            "price": self.price,
            "close_yest": 10.0,
            "change_pct": round((self.price - 10.0) / 10.0 * 100, 2),
            "amount": 120000000,
        }])

    def get_kline(self, code, count=90):
        return pd.DataFrame([{
            "date": pd.Timestamp("2026-05-29"),
            "close": self.price,
            "volume": 1200,
            "VOL_MA5": 1000,
            "MA5": 10.0,
            "MA10": 9.9,
            "MA20": 9.8,
            "MA60": 9.6,
            "MACD_DIF": 0.18,
            "MACD_DEA": 0.10,
            "RSI": 55,
        }])

    def get_fund_flow(self, code):
        return {"main_net": 3_000_000, "main_net_pct": 2.5}


class ExpectationPlannerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_file = planner_module.PLANS_FILE
        planner_module.PLANS_FILE = os.path.join(self.tmp.name, "plans.json")

    def tearDown(self):
        planner_module.PLANS_FILE = self.old_file
        self.tmp.cleanup()

    def test_complete_plan_can_be_marked_executable(self):
        planner = ExpectationPlanner(data_feed=FakeExpectationFeed())
        saved = planner.save_plan({
            "code": "1",
            "name": "平安银行",
            "thesis_type": "技术形态",
            "thesis": "指数放量转强后，银行低位缩量回踩等待资金回流",
            "trigger": "站回MA5且资金保持净流入",
            "invalidation": "跌破止损价或板块资金转流出",
            "planned_price": 10.2,
            "stop_loss": 9.6,
            "take_profit": 11.5,
            "position_pct": 10,
            "horizon": "T+1至5日",
        })["plan"]

        evaluated = planner.evaluate_plan(saved)

        self.assertEqual(evaluated["code"], "000001")
        self.assertEqual(evaluated["status"], "可按计划执行")
        self.assertGreaterEqual(evaluated["plan_quality"]["score"], 90)
        self.assertGreaterEqual(evaluated["earning_effect_score"], 70)

    def test_stop_loss_turns_plan_invalid(self):
        planner = ExpectationPlanner(data_feed=FakeExpectationFeed(price=9.4))
        plan = {
            "code": "000001",
            "thesis": "资金回流预期",
            "trigger": "重新站上均线",
            "invalidation": "跌破止损",
            "planned_price": 10.2,
            "stop_loss": 9.6,
            "take_profit": 11.5,
            "position_pct": 8,
            "horizon": "T+1",
        }

        evaluated = planner.evaluate_plan(plan)

        self.assertEqual(evaluated["status"], "预期失效")
        self.assertLess(evaluated["execution"]["score"], 30)


if __name__ == "__main__":
    unittest.main()
