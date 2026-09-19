"""引擎核心核算逻辑测试：拆股、配股、红利再投资、跨账户划转、冻结与更正。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from engine import EngineError, build_version  # noqa: E402

FUND = "100000"


def env(seq, kind, **payload):
    return {"seq": seq, "message_id": f"m-{seq}", "kind": kind, "payload": payload}


def fund(seq, account="A", ccy="HKD", amount=FUND, date="2026-01-01"):
    return env(seq, "cash_movement", movement_id=f"CM{seq}", account=account,
               currency=ccy, amount=amount, direction="deposit", date=date)


def buy(seq, qty, price, date, account="A", security="S", fee="0", ccy="HKD"):
    return env(seq, "trade", trade_id=f"T{seq}", account=account, security=security,
               side="BUY", quantity=qty, price=price, currency=ccy, fee=fee,
               fee_currency=ccy, trade_date=date)


def pos(version, account, security, day):
    days = [d for d in version["positions"] if d <= day]
    return version["positions"][max(days)][account][security]


def cash(version, account, ccy, day):
    days = [d for d in version["cash"] if d <= day]
    return version["cash"][max(days)].get(f"{account}|{ccy}", "0")


class SplitTest(unittest.TestCase):
    def test_split_scales_quantity_keeps_cost(self):
        v = build_version([
            fund(0),
            buy(1, "100", "10", "2026-01-05", fee="5"),
            env(2, "corporate_action", action_id="CA1", security="S", type="split",
                announcement_version=1, ex_date="2026-01-10", record_date="2026-01-10",
                pay_date="2026-01-10", numerator="5", denominator="1", currency="HKD"),
        ], set())
        p = pos(v, "A", "S", "2026-01-10")
        self.assertEqual("500.000000", p["quantity"])
        self.assertEqual("1005.00", p["cost_total"])  # 总成本不变

    def test_reverse_split_fraction_cash_in_lieu(self):
        v = build_version([
            fund(0),
            buy(1, "100", "10", "2026-01-05"),
            env(2, "corporate_action", action_id="CA1", security="S", type="split",
                announcement_version=1, ex_date="2026-01-10", record_date="2026-01-10",
                pay_date="2026-01-10", numerator="1", denominator="3", currency="HKD",
                cash_in_lieu_price="30"),
        ], set())
        p = pos(v, "A", "S", "2026-01-10")
        self.assertEqual("33.000000", p["quantity"])  # 100/3 向下取整
        # 碎股 0.333333 股按 30 折现 10.00，去向为现金
        self.assertEqual("99010.00", cash(v, "A", "HKD", "2026-01-10"))
        reasons = [m["reason"] for m in v["cash_movements"]]
        self.assertIn("split_fraction_cash_in_lieu", reasons)


class RightsTest(unittest.TestCase):
    def _events(self, election=None):
        events = [
            fund(0),
            buy(1, "1000", "10", "2026-01-05"),
            env(2, "corporate_action", action_id="R1", security="S", type="rights_issue",
                announcement_version=1, ex_date="2026-01-06", record_date="2026-01-08",
                pay_date="2026-01-20", ratio="0.3333", subscription_price="8",
                currency="HKD", fee={"amount": "10", "currency": "USD"},
                fraction_policy="cash_in_lieu", cash_in_lieu_price="12"),
            env(3, "fx_rate", pair="USD/HKD", rate_date="2026-01-20", rate="7.8",
                source_version=1),
        ]
        if election:
            events.append(env(4, "election", account="A", action_id="R1", choice=election))
        return events

    def test_subscribe_with_fraction_and_cross_currency_fee(self):
        v = build_version(self._events(election="subscribe"), set())
        p = pos(v, "A", "S", "2026-01-20")
        # 1000 * 0.3333 = 333.3 -> 认购 333 股，碎股 0.3 股折现
        self.assertEqual("1333.000000", p["quantity"])
        # 新批次成本 = 333*8 + 10 USD*7.8 = 2664 + 78 = 2742
        new_lot = [l for l in p["lots"] if l["origin"] == "rights_issue"][0]
        self.assertEqual("2742.00", new_lot["cost_total"])
        # 100000 - 10000 - 2742 + 0.3*12 = 87261.60
        self.assertEqual("87261.60", cash(v, "A", "HKD", "2026-01-20"))
        usage = [u for u in v["fx_usages"] if u["purpose"] == "rights_fee"][0]
        self.assertEqual("USD/HKD", usage["pair"])
        self.assertEqual(1, usage["source_version"])

    def test_cash_election(self):
        v = build_version(self._events(election="cash"), set())
        p = pos(v, "A", "S", "2026-01-20")
        self.assertEqual("1000.000000", p["quantity"])  # 不产生新批次
        # 333 股按 12 折现 + 碎股 0.3*12；现金选择权去向明确
        self.assertEqual("93999.60", cash(v, "A", "HKD", "2026-01-20"))
        reasons = [m["reason"] for m in v["cash_movements"]]
        self.assertIn("rights_cash_election", reasons)

    def test_lapse(self):
        v = build_version(self._events(election="lapse"), set())
        self.assertEqual("1000.000000", pos(v, "A", "S", "2026-01-20")["quantity"])
        self.assertEqual("90003.60", cash(v, "A", "HKD", "2026-01-20"))  # 仅碎股折现


class DividendReinvestTest(unittest.TestCase):
    def test_reinvest_net_of_fee_with_residual(self):
        v = build_version([
            fund(0),
            buy(1, "1000", "10", "2026-01-05"),
            env(2, "corporate_action", action_id="D1", security="S", type="cash_dividend",
                announcement_version=1, ex_date="2026-01-06", record_date="2026-01-08",
                pay_date="2026-01-20", dividend_per_share="2", currency="HKD",
                fee={"amount": "10", "currency": "USD"}),
            env(3, "corporate_action", action_id="DRIP", security="S", type="reinvestment",
                announcement_version=1, ex_date="2026-01-06", record_date="2026-01-08",
                pay_date="2026-01-20", source_action_id="D1", reinvest_price="15",
                currency="HKD"),
            env(4, "election", account="A", action_id="DRIP", choice="reinvest"),
            env(5, "fx_rate", pair="USD/HKD", rate_date="2026-01-20", rate="7.8",
                source_version=1),
        ], set())
        p = pos(v, "A", "S", "2026-01-20")
        # 红利 2000 - 费用 78 = 1922；1922/15 = 128.13 -> 128 股，余 2 元现金
        self.assertEqual("1128.000000", p["quantity"])
        lot = [l for l in p["lots"] if l["origin"] == "reinvestment"][0]
        self.assertEqual("1920.00", lot["cost_total"])
        self.assertEqual("90002.00", cash(v, "A", "HKD", "2026-01-20"))

    def test_dividend_cash_default(self):
        v = build_version([
            fund(0),
            buy(1, "100", "10", "2026-01-05"),
            env(2, "corporate_action", action_id="D1", security="S", type="cash_dividend",
                announcement_version=1, ex_date="2026-01-06", record_date="2026-01-08",
                pay_date="2026-01-20", dividend_per_share="1.5", currency="HKD"),
        ], set())
        self.assertEqual("99150.00", cash(v, "A", "HKD", "2026-01-20"))


class TransferFreezeTest(unittest.TestCase):
    def test_pending_transfer_freezes_then_confirmation_settles(self):
        events = [
            buy(1, "100", "10", "2026-01-05"),
            env(2, "transfer", transfer_id="TR1", type="transfer_out", account="A",
                security="S", quantity="40", currency="HKD", date="2026-01-10",
                status="pending"),
        ]
        v1 = build_version(events, set())
        p = pos(v1, "A", "S", "2026-01-10")
        self.assertEqual("40.000000", p["frozen"])
        self.assertEqual("60.000000", p["sellable"])
        self.assertEqual(1, len(v1["pending"]))
        v2 = build_version(events + [
            env(3, "confirmation", message_id="m-2", status="confirmed"),
        ], set())
        p = pos(v2, "A", "S", "2026-01-10")
        self.assertEqual("60.000000", p["quantity"])
        self.assertEqual("0.000000", p["frozen"])
        self.assertEqual(0, len(v2["pending"]))

    def test_sell_beyond_sellable_fails(self):
        with self.assertRaises(EngineError):
            build_version([
                buy(1, "100", "10", "2026-01-05"),
                env(2, "transfer", transfer_id="TR1", type="transfer_out", account="A",
                    security="S", quantity="40", currency="HKD", date="2026-01-10",
                    status="pending"),
                env(3, "trade", trade_id="T9", account="A", security="S", side="SELL",
                    quantity="70", price="12", currency="HKD", fee="0",
                    fee_currency="HKD", trade_date="2026-01-11"),
            ], set())

    def test_transfer_in_carries_cost(self):
        v = build_version([
            env(1, "transfer", transfer_id="TR1", type="transfer_in", account="A",
                counter_account="B", security="S", quantity="50", cost_total="600",
                currency="HKD", date="2026-01-05"),
        ], set())
        p = pos(v, "A", "S", "2026-01-05")
        self.assertEqual("600.00", p["cost_total"])
        self.assertEqual("transfer_in", p["lots"][0]["origin"])


class CorrectionTest(unittest.TestCase):
    def _dividend(self, seq, version, dps, status="confirmed"):
        return env(seq, "corporate_action", action_id="D1", security="S",
                   type="cash_dividend", announcement_version=version,
                   ex_date="2026-01-06", record_date="2026-01-08", pay_date="2026-01-20",
                   dividend_per_share=dps, currency="HKD", status=status)

    def test_higher_announcement_version_supersedes(self):
        v = build_version([
            fund(0),
            buy(1, "100", "10", "2026-01-05"),
            self._dividend(2, 1, "1.0"),
            self._dividend(3, 2, "1.5"),
        ], set())
        self.assertEqual("99150.00", cash(v, "A", "HKD", "2026-01-20"))
        self.assertEqual(1, len(v["superseded_actions"]))
        used = [a for a in v["actions_used"] if a["action_id"] == "D1"][0]
        self.assertEqual(2, used["announcement_version"])

    def test_cancel_voids_action(self):
        v = build_version([
            fund(0),
            buy(1, "100", "10", "2026-01-05"),
            self._dividend(2, 1, "1.0"),
            self._dividend(3, 2, "1.0", status="cancelled"),
        ], set())
        self.assertEqual("99000.00", cash(v, "A", "HKD", "2026-01-20"))
        self.assertEqual(1, len(v["cancelled_actions"]))
        self.assertEqual([], v["actions_used"])

    def test_late_fx_version_wins(self):
        v = build_version([
            fund(0),
            buy(1, "100", "10", "2026-01-05"),
            env(2, "corporate_action", action_id="D1", security="S", type="cash_dividend",
                announcement_version=1, ex_date="2026-01-06", record_date="2026-01-08",
                pay_date="2026-01-20", dividend_per_share="1", currency="HKD",
                fee={"amount": "10", "currency": "USD"}),
            env(3, "fx_rate", pair="USD/HKD", rate_date="2026-01-20", rate="7.8",
                source_version=1),
            env(4, "fx_rate", pair="USD/HKD", rate_date="2026-01-20", rate="8.0",
                source_version=2),
        ], set())
        # 费用按 v2 汇率 8.0 折算：100 - 80 = 20
        self.assertEqual("99020.00", cash(v, "A", "HKD", "2026-01-20"))
        usage = v["fx_usages"][0]
        self.assertEqual(2, usage["source_version"])


class RealizedTest(unittest.TestCase):
    def test_fifo_sell_realizes_pnl(self):
        v = build_version([
            buy(1, "100", "10", "2026-01-05"),
            buy(2, "100", "20", "2026-01-06"),
            env(3, "trade", trade_id="T3", account="A", security="S", side="SELL",
                quantity="150", price="30", currency="HKD", fee="0",
                fee_currency="HKD", trade_date="2026-01-10"),
        ], set())
        r = v["realized"][0]
        # FIFO：先出 100@10 的批次，再出 50@20；成本 1000+1000=2000，收入 4500
        self.assertEqual("2000.00", r["cost_removed"])
        self.assertEqual("2500.00", r["pnl"])
        p = pos(v, "A", "S", "2026-01-10")
        self.assertEqual("50.000000", p["quantity"])
        self.assertEqual("1000.00", p["cost_total"])


if __name__ == "__main__":
    unittest.main()
