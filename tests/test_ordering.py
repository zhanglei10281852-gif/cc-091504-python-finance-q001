"""同日多项行动的排序规则与重放确定性测试。"""
from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from engine import build_version  # noqa: E402
from ordering import adjust_business_day, sort_steps  # noqa: E402
from test_engine import buy, cash, env, fund, pos  # noqa: E402


class SameDayOrderingTest(unittest.TestCase):
    def test_split_before_entitlement_same_day(self):
        """拆股与配股登记同日：权益按拆股后的数量计算。"""
        v = build_version([
            fund(0),
            buy(1, "100", "10", "2026-01-05"),
            env(2, "corporate_action", action_id="SPL", security="S", type="split",
                announcement_version=1, ex_date="2026-01-08", record_date="2026-01-08",
                pay_date="2026-01-08", numerator="2", denominator="1", currency="HKD"),
            env(3, "corporate_action", action_id="R1", security="S", type="rights_issue",
                announcement_version=1, ex_date="2026-01-07", record_date="2026-01-08",
                pay_date="2026-01-20", ratio="0.5", subscription_price="8",
                currency="HKD", fraction_policy="cash_in_lieu",
                cash_in_lieu_price="12"),
        ], set())
        p = pos(v, "A", "S", "2026-01-20")
        # 拆股后 200 股，配股 200*0.5 = 100 股 -> 合计 300 股
        self.assertEqual("300.000000", p["quantity"])

    def test_trade_on_record_date_counts(self):
        """登记日当天的成交在权益登记之前入账（日终持仓确定权益）。"""
        v = build_version([
            fund(0),
            buy(1, "100", "10", "2026-01-05"),
            buy(2, "100", "10", "2026-01-08"),  # 登记日当天买入
            env(3, "corporate_action", action_id="D1", security="S", type="cash_dividend",
                announcement_version=1, ex_date="2026-01-06", record_date="2026-01-08",
                pay_date="2026-01-20", dividend_per_share="1", currency="HKD"),
        ], set())
        # 按 200 股派息：100000 - 1000 - 1000 + 200 = 98200
        self.assertEqual("98200.00", cash(v, "A", "HKD", "2026-01-20"))

    def test_replay_deterministic_regardless_of_arrival_order(self):
        """消息到达顺序不影响结果：乱序重放得到相同版本哈希。"""
        events = [
            fund(0),
            buy(1, "100", "10", "2026-01-05"),
            env(2, "corporate_action", action_id="SPL", security="S", type="split",
                announcement_version=1, ex_date="2026-01-06", record_date="2026-01-06",
                pay_date="2026-01-06", numerator="2", denominator="1", currency="HKD"),
            env(3, "corporate_action", action_id="D1", security="S", type="cash_dividend",
                announcement_version=1, ex_date="2026-01-07", record_date="2026-01-08",
                pay_date="2026-01-20", dividend_per_share="1", currency="HKD"),
            env(4, "transfer", transfer_id="TR1", type="transfer_in", account="A",
                counter_account="B", security="S", quantity="50", cost_total="600",
                currency="HKD", date="2026-01-07"),
        ]
        baseline = build_version(events, set())["version_hash"]
        for seed in range(5):
            shuffled = events[:]
            random.Random(seed).shuffle(shuffled)
            # 重新分配接收序号以模拟不同的到达顺序
            for i, e in enumerate(shuffled):
                e["seq"] = i
            self.assertEqual(baseline, build_version(shuffled, set())["version_hash"])

    def test_sort_steps_stable(self):
        steps = [
            {"date": "2026-01-08", "phase": "transfer_out", "seq": 1, "tie": "b"},
            {"date": "2026-01-08", "phase": "split", "seq": 2, "tie": "a"},
            {"date": "2026-01-08", "phase": "trade", "seq": 3, "tie": "c"},
            {"date": "2026-01-08", "phase": "transfer_in", "seq": 4, "tie": "d"},
            {"date": "2026-01-07", "phase": "trade", "seq": 5, "tie": "e"},
        ]
        ordered = [s["phase"] for s in sort_steps(steps)]
        self.assertEqual(["trade", "transfer_in", "split", "trade", "transfer_out"],
                         ordered)


class CalendarTest(unittest.TestCase):
    def test_pay_date_rolls_over_holiday_and_weekend(self):
        holidays = {"2026-06-24"}
        self.assertEqual("2026-06-25", adjust_business_day("2026-06-24", holidays))
        # 2026-06-27 是周六，顺延到周一 06-29
        self.assertEqual("2026-06-29", adjust_business_day("2026-06-27", holidays))

    def test_dividend_pays_on_adjusted_date(self):
        v = build_version([
            fund(0),
            buy(1, "100", "10", "2026-06-01"),
            env(2, "corporate_action", action_id="D1", security="S", type="cash_dividend",
                announcement_version=1, ex_date="2026-06-08", record_date="2026-06-10",
                pay_date="2026-06-24", dividend_per_share="1", currency="HKD"),
        ], {"2026-06-24"})
        # 06-24 为假日，红利在 06-25 到账
        self.assertEqual("99000.00", cash(v, "A", "HKD", "2026-06-24"))
        self.assertEqual("99100.00", cash(v, "A", "HKD", "2026-06-25"))


if __name__ == "__main__":
    unittest.main()
