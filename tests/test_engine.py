"""引擎单元测试：直接驱动 Replayer 验证各类公司行动与资金行为。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from engine import RebuildError, Replayer  # noqa: E402
from util import D, dec_str  # noqa: E402


def ev(seq: int, kind: str, **payload) -> dict:
    return {"seq": seq, "message_id": f"M{seq:03d}", "kind": kind,
            "payload": payload, "received_at": "2026-01-01T00:00:00+00:00"}


def base_events() -> list[dict]:
    return [
        ev(1, "account", account_id="A1", base_currency="CNY"),
        ev(2, "account", account_id="A2", base_currency="CNY"),
        ev(3, "security", security_id="S1", currency="HKD", market="HKEX"),
        ev(4, "fx_rate", base="HKD", quote="CNY", date="2026-01-01",
           rate="0.90", source="test"),
        ev(5, "cash_movement", account_id="A1", currency="HKD",
           amount="1000000", date="2026-01-02"),
        ev(6, "cash_movement", account_id="A2", currency="HKD",
           amount="100000", date="2026-01-02"),
    ]


def buy(seq: int, account: str, qty: str, price: str, day: str,
        trade_id: str | None = None, fees=None) -> dict:
    return ev(seq, "trade", trade_id=trade_id or f"T{seq:03d}", account_id=account,
              security_id="S1", side="buy", quantity=qty, price=price,
              currency="HKD", trade_date=day, fees=fees or [])


def replay(events: list[dict]) -> dict:
    return Replayer(events).run()


def open_lots(state: dict, account: str, security: str = "S1") -> list[dict]:
    last = max(state["changes"])
    return [l for l in state["changes"][last]["lots"].get(account, {}).get(security, [])
            if not l["closed"] and D(l["quantity"]) > 0]


def cash_of(state: dict, account: str, ccy: str) -> D:
    last = max(state["changes"])
    return D(state["changes"][last]["cash"].get(account, {}).get(ccy, {})
             .get("balance", "0"))


class SplitTest(unittest.TestCase):
    def test_split_conserves_cost_and_scales_quantity(self):
        events = base_events() + [
            buy(10, "A1", "100", "10.00", "2026-01-05"),
            ev(11, "corporate_action", action_id="CA1", security_id="S1",
               type="split", version=1, status="confirmed",
               ex_date="2026-02-01", terms={"from": 1, "to": 3}),
        ]
        state = replay(events)
        (lot,) = open_lots(state, "A1")
        self.assertEqual("300.000000", dec_str(lot["quantity"]))
        self.assertEqual("1000.000000", dec_str(lot["cost_total"]))  # 成本守恒
        self.assertEqual("3.333333", dec_str(lot["cost_per_share"]))
        kinds = [h["kind"] for h in lot["history"]]
        self.assertEqual(["acquire", "split"], kinds)
        self.assertEqual(1, lot["history"][1]["detail"]["version"])

    def test_reverse_split_fraction_cash_in_lieu(self):
        events = base_events() + [
            buy(10, "A1", "103", "10.00", "2026-01-05"),
            ev(11, "corporate_action", action_id="CA1", security_id="S1",
               type="split", version=1, status="confirmed",
               ex_date="2026-02-01",
               terms={"from": 5, "to": 1, "cash_in_lieu_price": "9.50"}),
        ]
        state = replay(events)
        (lot,) = open_lots(state, "A1")
        self.assertEqual("20.000000", dec_str(lot["quantity"]))  # 103/5=20.6 -> 20 股
        # 0.6 股碎股按 9.50 现金替代，去向明确
        fracs = [d for d in state["dispositions"]
                 if d["kind"] == "fraction_cash_in_lieu"]
        self.assertEqual(1, len(fracs))
        self.assertEqual("0.600000", dec_str(fracs[0]["quantity"]))
        self.assertEqual("5.700000", fracs[0]["amount"])


class RightsTest(unittest.TestCase):
    def _rights_events(self, extra=None, terms_extra=None, qty="103"):
        terms = {"ratio_base": 5, "ratio_rights": 1, "subscription_price": "8.00",
                 "currency": "HKD", "cash_option_price": "0.50",
                 "cash_in_lieu_price": "7.50", "listing_date": "2026-03-30",
                 "fee": {"amount": "20", "currency": "HKD"}}
        terms.update(terms_extra or {})
        events = base_events() + [
            buy(10, "A1", qty, "10.00", "2026-01-05"),
            ev(11, "corporate_action", action_id="CA2", security_id="S1",
               type="rights_issue", version=1, status="confirmed",
               ex_date="2026-03-01", record_date="2026-03-03",
               pay_date="2026-03-20", terms=terms),
            ev(13, "fx_rate", base="HKD", quote="CNY", date="2026-03-20",
               rate="0.90", source="test"),
        ]
        return events + (extra or [])

    def test_entitlement_fraction_and_delivery(self):
        state = replay(self._rights_events())
        lots = open_lots(state, "A1")
        self.assertEqual(2, len(lots))
        rights = [l for l in lots if l["origin"]["kind"] == "rights_issue"][0]
        # 103 股 -> 20.6 配股权 -> 20 股，0.6 碎股现金替代
        self.assertEqual("20.000000", dec_str(rights["quantity"]))
        # 成本 = 20*8 + 费用 20 = 180
        self.assertEqual("180.000000", dec_str(rights["cost_total"]))
        self.assertEqual("162.000000", dec_str(rights["base_cost_total"]))  # 180*0.9
        fracs = [d for d in state["dispositions"]
                 if d["kind"] == "fraction_cash_in_lieu"]
        self.assertEqual("0.600000", dec_str(fracs[0]["quantity"]))
        self.assertEqual("4.500000", fracs[0]["amount"])  # 0.6*7.5
        # 上市日解冻后无冻结
        self.assertEqual("0.000000", dec_str(rights["frozen"]))
        unfreezes = [h for h in rights["history"] if h["kind"] == "unfreeze"]
        self.assertEqual(1, len(unfreezes))

    def test_rights_lot_frozen_before_listing(self):
        state = replay(self._rights_events())
        # 2026-03-20 到账、2026-03-30 上市：中间时点应处于冻结
        mid = state["changes"]["2026-03-20"]
        rights = [l for l in mid["lots"]["A1"]["S1"]
                  if l["origin"]["kind"] == "rights_issue"][0]
        self.assertEqual(rights["quantity"], rights["frozen"])

    def test_cash_election(self):
        extra = [ev(12, "election", action_id="CA2", account_id="A1", mode="cash")]
        state = replay(self._rights_events(extra=extra))
        lots = open_lots(state, "A1")
        self.assertEqual(1, len(lots))  # 无配股批次
        elections = [d for d in state["dispositions"] if d["kind"] == "cash_election"]
        self.assertEqual(1, len(elections))
        self.assertEqual(20, elections[0]["shares"])
        self.assertEqual("10.000000", elections[0]["amount"])  # 20*0.5

    def test_fraction_residual_without_cash_in_lieu(self):
        state = replay(self._rights_events(terms_extra={"cash_in_lieu_price": None}))
        residuals = [d for d in state["dispositions"]
                     if d["kind"] == "fraction_residual"]
        self.assertEqual(1, len(residuals))
        pendings = [p for p in state["pending"] if p["kind"] == "fractional_residual"]
        self.assertEqual(1, len(pendings))
        self.assertIsNone(pendings[0]["resolved_date"])

    def test_cash_shortfall_is_pending_not_failure(self):
        events = base_events() + [
            buy(10, "A2", "100", "10.00", "2026-01-05"),  # A2 现金 100k-1000
            ev(11, "corporate_action", action_id="CA2", security_id="S1",
               type="rights_issue", version=1, status="confirmed",
               ex_date="2026-03-01", pay_date="2026-03-20",
               terms={"ratio_base": 1, "ratio_rights": 1,
                      "subscription_price": "200000.00", "currency": "HKD"}),
        ]
        state = replay(events)  # 缴款 20 万超出余额 -> 现金缺口待确认而非重算失败
        shortfalls = [p for p in state["pending"] if p["kind"] == "cash_shortfall"]
        self.assertEqual(1, len(shortfalls))
        self.assertEqual("A2", shortfalls[0]["account_id"])


class DividendTest(unittest.TestCase):
    def _dividend_events(self, terms_extra=None, extra=None):
        terms = {"amount_per_share": "0.50", "currency": "HKD",
                 "tax_rate": "0.10", "fee": {"amount": "20", "currency": "HKD"}}
        terms.update(terms_extra or {})
        events = base_events() + [
            buy(10, "A1", "1000", "10.00", "2026-01-05"),
            ev(11, "corporate_action", action_id="CA3", security_id="S1",
               type="cash_dividend", version=1, status="confirmed",
               ex_date="2026-04-01", record_date="2026-04-02",
               pay_date="2026-04-15", terms=terms),
        ]
        return events + (extra or [])

    def test_dividend_net_of_tax_and_fee(self):
        state = replay(self._dividend_events())
        # 毛 500，税 50，费 20 -> 净 430
        self.assertEqual(D("430"), cash_of(state, "A1", "HKD") - D("990000"))
        taxes = [d for d in state["dispositions"] if d["kind"] == "tax"]
        self.assertEqual("50.00", taxes[0]["amount"])

    def test_cross_currency_fee_not_netted(self):
        terms = {"amount_per_share": "1.00", "currency": "HKD", "tax_rate": "0",
                 "fee": {"amount": "5", "currency": "USD"}}
        extra = [ev(12, "cash_movement", account_id="A1", currency="USD",
                    amount="50", date="2026-01-02")]
        state = replay(self._dividend_events(terms_extra=terms, extra=extra))
        # 红利净额 = 1000（费用是美元，不从港币红利中扣）
        self.assertEqual(D("1000"), cash_of(state, "A1", "HKD") - D("990000"))
        self.assertEqual(D("45"), cash_of(state, "A1", "USD"))
        fees = [d for d in state["dispositions"]
                if d["kind"] == "fee" and d["currency"] == "USD"]
        self.assertEqual(1, len(fees))

    def test_eligibility_excludes_ex_date_buy(self):
        events = self._dividend_events() + [
            buy(12, "A1", "500", "10.00", "2026-04-01"),  # 除权日当日买入不含权
        ]
        state = replay(events)
        credits = [e for e in state["ledger"] if e["kind"] == "dividend_pay"]
        self.assertEqual(1, len(credits))
        # 仍按 1000 股确权
        self.assertEqual("430.000000",
                         credits[0]["cash_movements"][-1]["amount"])

    def test_reinvestment_with_residual(self):
        extra = [
            ev(12, "corporate_action", action_id="CA4", security_id="S1",
               type="reinvestment", version=1, status="confirmed",
               pay_date="2026-04-15",
               terms={"dividend_action_id": "CA3", "price": "7.00",
                      "currency": "HKD"}),
        ]
        state = replay(self._dividend_events(extra=extra))
        lots = open_lots(state, "A1")
        drip = [l for l in lots if l["origin"]["kind"] == "reinvestment"]
        self.assertEqual(1, len(drip))
        # 净红利 430 / 7 = 61.428571(向下) -> 成本 429.999997，残余 0.000003
        self.assertEqual("61.428571", dec_str(drip[0]["quantity"]))
        residuals = [d for d in state["dispositions"]
                     if d["kind"] == "reinvest_residual_cash"]
        self.assertEqual(1, len(residuals))
        self.assertEqual(D("430") - D("429.999997"), D(residuals[0]["amount"]))

    def test_reinvestment_opt_out_keeps_cash(self):
        extra = [
            ev(12, "corporate_action", action_id="CA4", security_id="S1",
               type="reinvestment", version=1, status="confirmed",
               pay_date="2026-04-15",
               terms={"dividend_action_id": "CA3", "price": "7.00",
                      "currency": "HKD"}),
            ev(13, "election", action_id="CA4", account_id="A1", mode="cash"),
        ]
        state = replay(self._dividend_events(extra=extra))
        lots = open_lots(state, "A1")
        self.assertEqual(1, len(lots))
        self.assertEqual(D("430"), cash_of(state, "A1", "HKD") - D("990000"))


class OrderingTest(unittest.TestCase):
    def test_same_day_split_before_dividend_and_arrival_independence(self):
        """同日拆股+红利：红利按拆股后数量确权；与消息到达顺序无关。"""
        config = base_events() + [buy(10, "A1", "100", "30.00", "2026-01-05")]
        split = ev(11, "corporate_action", action_id="CA-S", security_id="S1",
                   type="split", version=1, status="confirmed",
                   ex_date="2026-02-01", terms={"from": 1, "to": 3})
        dividend = ev(12, "corporate_action", action_id="CA-D", security_id="S1",
                      type="cash_dividend", version=1, status="confirmed",
                      ex_date="2026-02-01", pay_date="2026-02-10",
                      terms={"amount_per_share": "1.00", "currency": "HKD"})
        state_a = replay(config + [split, dividend])
        state_b = replay(config + [dividend, split])  # 红利消息先到
        self.assertEqual(state_a, state_b)  # 重放结果与到达顺序无关
        # 300 股 * 1.00 = 300 红利
        self.assertEqual(D("300"), cash_of(state_a, "A1", "HKD") - D("997000"))

    def test_correction_uses_latest_version(self):
        events = base_events() + [
            buy(10, "A1", "100", "10.00", "2026-01-05"),
            ev(11, "corporate_action", action_id="CA1", security_id="S1",
               type="cash_dividend", version=1, status="confirmed",
               ex_date="2026-02-01", pay_date="2026-02-10",
               terms={"amount_per_share": "1.00", "currency": "HKD"}),
            ev(12, "corporate_action", action_id="CA1", security_id="S1",
               type="cash_dividend", version=2, status="confirmed",
               ex_date="2026-02-01", pay_date="2026-02-10",
               terms={"amount_per_share": "2.00", "currency": "HKD"}),
        ]
        state = replay(events)
        self.assertEqual(D("200"), cash_of(state, "A1", "HKD") - D("999000"))
        ca = state["corporate_actions"]["CA1"]
        self.assertEqual(2, ca["version_used"])
        self.assertEqual([1, 2], ca["versions_seen"])

    def test_cancellation_removes_effects(self):
        events = base_events() + [
            buy(10, "A1", "100", "10.00", "2026-01-05"),
            ev(11, "corporate_action", action_id="CA1", security_id="S1",
               type="cash_dividend", version=1, status="confirmed",
               ex_date="2026-02-01", pay_date="2026-02-10",
               terms={"amount_per_share": "1.00", "currency": "HKD"}),
            ev(12, "corporate_action", action_id="CA1", security_id="S1",
               type="cash_dividend", version=2, status="cancelled",
               ex_date="2026-02-01", pay_date="2026-02-10", terms={}),
        ]
        state = replay(events)
        self.assertEqual(D("0"), cash_of(state, "A1", "HKD") - D("999000"))
        self.assertEqual("cancelled", state["corporate_actions"]["CA1"]["status"])

    def test_preliminary_action_flagged_pending(self):
        events = base_events() + [
            buy(10, "A1", "100", "10.00", "2026-01-05"),
            ev(11, "corporate_action", action_id="CA1", security_id="S1",
               type="cash_dividend", version=1, status="preliminary",
               ex_date="2026-02-01", pay_date="2026-02-10",
               terms={"amount_per_share": "1.00", "currency": "HKD"}),
        ]
        state = replay(events)
        kinds = [p["kind"] for p in state["pending"] if p["resolved_date"] is None]
        self.assertIn("unconfirmed_terms", kinds)


class TransferTest(unittest.TestCase):
    def test_external_transfer_in_preserves_basis(self):
        events = base_events() + [
            ev(10, "transfer", transfer_id="TR1", security_id="S1",
               from_account="EXT-1", to_account="A1", quantity="500",
               transfer_date="2026-03-01",
               cost_basis={"cost_per_share": "21.00", "currency": "HKD",
                           "acquired_date": "2025-12-01",
                           "base_cost_per_share": "18.90"}),
        ]
        state = replay(events)
        (lot,) = open_lots(state, "A1")
        self.assertEqual("2025-12-01", lot["acquired_date"])  # 保留原始取得日
        self.assertEqual("10500.000000", dec_str(lot["cost_total"]))
        self.assertEqual("9450.000000", dec_str(lot["base_cost_total"]))
        self.assertTrue(lot["origin"]["external"])

    def test_external_transfer_requires_basis(self):
        events = base_events() + [
            ev(10, "transfer", transfer_id="TR1", security_id="S1",
               from_account="EXT-1", to_account="A1", quantity="500",
               transfer_date="2026-03-01"),
        ]
        with self.assertRaises(RebuildError):
            replay(events)

    def test_internal_transfer_moves_fifo_with_basis(self):
        events = base_events() + [
            buy(10, "A1", "100", "10.00", "2026-01-05"),
            buy(11, "A1", "100", "20.00", "2026-01-06"),
            ev(12, "transfer", transfer_id="TR1", security_id="S1",
               from_account="A1", to_account="A2", quantity="150",
               transfer_date="2026-02-01"),
        ]
        state = replay(events)
        lots_a2 = open_lots(state, "A2")
        # FIFO：先入的 100 股(成本1000)整批转移，再加第二批 50 股(成本1000)
        self.assertEqual(2, len(lots_a2))
        self.assertEqual("1000.000000", dec_str(lots_a2[0]["cost_total"]))
        self.assertEqual("2026-01-05", lots_a2[0]["acquired_date"])
        (lot_a1,) = open_lots(state, "A1")
        self.assertEqual("50.000000", dec_str(lot_a1["quantity"]))

    def test_transfer_out_external_closes_lots(self):
        events = base_events() + [
            buy(10, "A1", "100", "10.00", "2026-01-05"),
            ev(11, "transfer", transfer_id="TR1", security_id="S1",
               from_account="A1", to_account="EXT-9", quantity="100",
               transfer_date="2026-02-01"),
        ]
        state = replay(events)
        self.assertEqual([], open_lots(state, "A1"))
        outs = [d for d in state["dispositions"]
                if d["kind"] == "transfer_out_external"]
        self.assertEqual(1, len(outs))


class SellAndFreezeTest(unittest.TestCase):
    def test_sell_fifo_realized_fx_decomposition(self):
        events = base_events() + [
            buy(10, "A1", "100", "10.00", "2026-01-05"),   # 成本 1000 HKD @0.90
            ev(11, "fx_rate", base="HKD", quote="CNY", date="2026-02-01",
               rate="1.00", source="test"),
            ev(12, "trade", trade_id="T-sell", account_id="A1", security_id="S1",
               side="sell", quantity="100", price="12.00", currency="HKD",
               trade_date="2026-02-01", fees=[]),
        ]
        state = replay(events)
        (rec,) = state["realized"]
        # 交易损益 (1200-1000)*1.0 = 200；汇兑损益 1000*(1.0-0.9) = 100
        self.assertEqual("200.000000", rec["realized_trading_base"])
        self.assertEqual("100.000000", rec["realized_fx_base"])
        self.assertEqual("300.000000", rec["realized_total_base"])

    def test_frozen_shares_cannot_be_sold(self):
        events = base_events() + [
            buy(10, "A1", "100", "10.00", "2026-01-05"),
            ev(11, "freeze", account_id="A1", security_id="S1",
               quantity="60", date="2026-01-10", reason="抵押"),
            ev(12, "trade", trade_id="T-sell", account_id="A1", security_id="S1",
               side="sell", quantity="50", price="12.00", currency="HKD",
               trade_date="2026-01-11", fees=[]),
        ]
        with self.assertRaises(RebuildError):
            replay(events)

    def test_freeze_then_unfreeze(self):
        events = base_events() + [
            buy(10, "A1", "100", "10.00", "2026-01-05"),
            ev(11, "freeze", account_id="A1", security_id="S1",
               quantity="60", date="2026-01-10", reason="抵押"),
            ev(12, "unfreeze", account_id="A1", security_id="S1",
               quantity="60", date="2026-01-12"),
            ev(13, "trade", trade_id="T-sell", account_id="A1", security_id="S1",
               side="sell", quantity="100", price="12.00", currency="HKD",
               trade_date="2026-01-13", fees=[]),
        ]
        state = replay(events)
        self.assertEqual([], open_lots(state, "A1"))


class FxAndCheckTest(unittest.TestCase):
    def test_missing_fx_marks_pending(self):
        events = [
            ev(1, "account", account_id="A1", base_currency="CNY"),
            ev(2, "security", security_id="S1", currency="HKD", market=""),
            ev(3, "cash_movement", account_id="A1", currency="HKD",
               amount="10000", date="2026-01-02"),
            buy(4, "A1", "100", "10.00", "2026-01-05"),
        ]
        state = replay(events)
        pendings = [p for p in state["pending"] if p["kind"] == "awaiting_fx"]
        self.assertEqual(1, len(pendings))
        (lot,) = open_lots(state, "A1")
        self.assertIsNone(lot["base_cost_total"])

    def test_fx_lookback_uses_prior_rate(self):
        events = base_events() + [buy(10, "A1", "100", "10.00", "2026-01-05")]
        state = replay(events)
        (lot,) = open_lots(state, "A1")
        self.assertEqual("900.000000", dec_str(lot["base_cost_total"]))  # 用 1-01 的 0.90
        entry = [e for e in state["ledger"] if e["kind"] == "trade_buy"][0]
        conv = entry["fx_conversions"][0]
        self.assertEqual("2026-01-01", conv["rate_date"])  # 记录实际采用的汇率日期

    def test_position_snapshot_mismatch_fails(self):
        events = base_events() + [
            buy(10, "A1", "100", "10.00", "2026-01-05"),
            ev(11, "position_snapshot", account_id="A1", security_id="S1",
               date="2026-01-06", quantity="99"),
        ]
        with self.assertRaises(RebuildError):
            replay(events)

    def test_position_snapshot_passes(self):
        events = base_events() + [
            buy(10, "A1", "100", "10.00", "2026-01-05"),
            ev(11, "position_snapshot", account_id="A1", security_id="S1",
               date="2026-01-06", quantity="100"),
        ]
        state = replay(events)
        self.assertIn("position_checks_passed:1", state["validations"])


if __name__ == "__main__":
    unittest.main()
