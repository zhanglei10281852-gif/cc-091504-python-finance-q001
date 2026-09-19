"""服务层测试：幂等、版本化、快照不可变、原子提交、沿革校验。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from service import ConflictError, NotFoundError, Service  # noqa: E402
from store import Store  # noqa: E402
from util import D  # noqa: E402


def msg(mid: str, kind: str, **payload) -> dict:
    return {"message_id": mid, "kind": kind, "payload": payload}


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = Service(Store(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def ingest(self, mid: str, kind: str, auto: bool = True, **payload):
        message = {"message_id": mid, "kind": kind, "payload": payload,
                   "auto_recalculate": auto}
        return self.service.ingest(message)

    def seed_basic(self):
        self.ingest("M01", "account", account_id="A1", base_currency="CNY")
        self.ingest("M02", "security", security_id="S1", currency="HKD",
                    market="HKEX")
        self.ingest("M03", "fx_rate", base="HKD", quote="CNY",
                    date="2026-01-01", rate="0.90", source="t")
        self.ingest("M04", "cash_movement", account_id="A1", currency="HKD",
                    amount="100000", date="2026-01-02")
        self.ingest("M05", "trade", trade_id="T1", account_id="A1",
                    security_id="S1", side="buy", quantity="100", price="10.00",
                    currency="HKD", trade_date="2026-01-05", fees=[])


class IdempotencyTest(ServiceCase):
    def test_same_message_replay_is_idempotent(self):
        self.seed_basic()
        versions_before = self.service.list_versions()["versions"]
        again, status = self.ingest("M05", "trade", trade_id="T1",
                                    account_id="A1", security_id="S1",
                                    side="buy", quantity="100", price="10.00",
                                    currency="HKD", trade_date="2026-01-05",
                                    fees=[])
        self.assertEqual(200, status)
        self.assertTrue(again["idempotent_replay"])
        # 不产生新事件、不产生新版本
        self.assertEqual(5, self.service.list_events()["total"])
        self.assertEqual(versions_before, self.service.list_versions()["versions"])
        pos = self.service.positions("A1", "2026-01-06")
        self.assertEqual("100.000000", pos["positions"][0]["quantity"])

    def test_same_id_different_payload_conflicts(self):
        self.seed_basic()
        with self.assertRaises(ConflictError):
            self.ingest("M05", "trade", trade_id="T1", account_id="A1",
                        security_id="S1", side="buy", quantity="200",
                        price="10.00", currency="HKD", trade_date="2026-01-05",
                        fees=[])

    def test_invalid_message_rejected_and_replayed_consistently(self):
        resp, status = self.ingest("M10", "trade", trade_id="T1")
        self.assertEqual(400, status)
        self.assertEqual("rejected", resp["status"])
        resp2, status2 = self.ingest("M10", "trade", trade_id="T1")
        self.assertEqual(400, status2)
        self.assertTrue(resp2["idempotent_replay"])
        self.assertEqual(0, self.service.list_events()["total"])  # 未入事件日志


class VersioningTest(ServiceCase):
    def test_late_event_creates_new_version_snapshot_untouched(self):
        self.seed_basic()
        self.ingest("M06", "corporate_action", action_id="CA1", security_id="S1",
                    type="cash_dividend", version=1, status="confirmed",
                    ex_date="2026-02-01", pay_date="2026-02-10",
                    terms={"amount_per_share": "1.00", "currency": "HKD"})
        snap = self.service.issue_snapshot("A1", "2026-02-15", "月结单")
        snap_cash = {c["currency"]: c["balance"] for c in snap["cash"]}
        self.assertEqual("99100.000000", snap_cash["HKD"])  # 100000-1000+100
        # 迟到更正：每股 1.00 -> 1.50
        self.ingest("M07", "corporate_action", action_id="CA1", security_id="S1",
                    type="cash_dividend", version=2, status="confirmed",
                    ex_date="2026-02-01", pay_date="2026-02-10",
                    terms={"amount_per_share": "1.50", "currency": "HKD"})
        versions = self.service.list_versions()
        self.assertEqual(7, versions["current_version"])
        # 快照保持出具时口径
        again = self.service.get_snapshot(snap["snapshot_id"])
        self.assertEqual(snap["version_seq"], again["version_seq"])
        self.assertEqual({c["currency"]: c["balance"] for c in snap["cash"]},
                         {c["currency"]: c["balance"] for c in again["cash"]})
        # 对比显示重述差异
        cmp_ = self.service.compare_snapshot(snap["snapshot_id"])
        self.assertTrue(cmp_["restated"])
        # 当前版本现金为 99150
        pos_now = self.service.positions("A1", "2026-02-15")
        cash_now = {c["currency"]: c["balance"] for c in pos_now["cash"]}
        self.assertEqual("99150.000000", cash_now["HKD"])

    def test_version_query_pinning(self):
        self.seed_basic()
        self.ingest("M06", "corporate_action", action_id="CA1", security_id="S1",
                    type="split", version=1, status="confirmed",
                    ex_date="2026-02-01", terms={"from": 1, "to": 2})
        v5 = self.service.positions("A1", "2026-02-02", version=5)
        v6 = self.service.positions("A1", "2026-02-02", version=6)
        self.assertEqual("100.000000", v5["positions"][0]["quantity"])
        self.assertEqual("200.000000", v6["positions"][0]["quantity"])
        with self.assertRaises(NotFoundError):
            self.service.positions("A1", "2026-02-02", version=99)


class AtomicityTest(ServiceCase):
    def test_failed_recalc_leaves_no_partial_result(self):
        self.seed_basic()
        before = self.service.list_versions()["current_version"]
        # 持仓核对与重放结果不符 -> 重算失败
        resp, status = self.ingest(
            "M06", "position_snapshot", account_id="A1", security_id="S1",
            date="2026-01-06", quantity="99")
        self.assertEqual(422, status)
        self.assertEqual("failed", resp["recalculation"]["status"])
        # 当前版本不变、没有新版本文件
        self.assertEqual(before, self.service.list_versions()["current_version"])
        self.assertEqual(before, len(self.service.list_versions()["versions"]))
        # 旧版本查询仍然可用
        pos = self.service.positions("A1", "2026-01-06")
        self.assertEqual("100.000000", pos["positions"][0]["quantity"])
        # 修正数据（同一日期的新核对覆盖旧核对）后重算成功
        self.ingest("M07", "position_snapshot", False, account_id="A1",
                    security_id="S1", date="2026-01-06", quantity="100")
        result = self.service.recalculate()
        self.assertEqual("committed", result["status"])
        self.assertEqual(before + 1, result["version_seq"])
        # 事件日志未变时重算空转，不产生新版本
        again = self.service.recalculate()
        self.assertEqual("no_change", again["status"])
        self.assertEqual(before + 1, again["version_seq"])

    def test_failed_recalc_replay_returns_same_422(self):
        self.seed_basic()
        payload = dict(account_id="A1", security_id="S1", date="2026-01-06",
                       quantity="99")
        resp1, status1 = self.ingest("M06", "position_snapshot", **payload)
        self.assertEqual(422, status1)
        resp2, status2 = self.ingest("M06", "position_snapshot", **payload)
        self.assertEqual(422, status2)
        self.assertTrue(resp2["idempotent_replay"])


class LineageTest(ServiceCase):
    def test_export_and_verify(self):
        self.seed_basic()
        self.ingest("M06", "corporate_action", action_id="CA1", security_id="S1",
                    type="split", version=1, status="confirmed",
                    ex_date="2026-02-01", terms={"from": 1, "to": 2})
        export = self.service.export_lineage("A1", "S1", "2026-02-02")
        result = self.service.verify_lineage(export)
        self.assertTrue(result["valid"], result)
        # 篡改一步流水后校验失败
        import copy
        tampered = copy.deepcopy(export)
        tampered["steps"][0]["notes"] = ["篡改"]
        result2 = self.service.verify_lineage(tampered)
        self.assertFalse(result2["valid"])

    def test_lineage_shows_version_and_fx_used(self):
        self.seed_basic()
        self.ingest("M06", "corporate_action", action_id="CA1", security_id="S1",
                    type="split", version=1, status="confirmed",
                    ex_date="2026-02-01", terms={"from": 1, "to": 2})
        lin = self.service.lineage("A1", "S1", "2026-02-02")
        lot = lin["lots"][0]
        split_step = [h for h in lot["history"] if h["kind"] == "split"][0]
        self.assertEqual(1, split_step["detail"]["version"])  # 采用的公告版本
        export = self.service.export_lineage("A1", "S1", "2026-02-02")
        buy_step = [s for s in export["steps"] if s["kind"] == "trade_buy"][0]
        conv = buy_step["fx_conversions"][0]
        self.assertEqual("0.90", conv["rate"])  # 采用的汇率
        self.assertEqual("2026-01-01", conv["rate_date"])


class PendingTest(ServiceCase):
    def test_pending_as_of_filters_by_date(self):
        self.seed_basic()
        self.ingest("M07", "fx_rate", base="HKD", quote="CNY",
                    date="2026-02-10", rate="0.91", source="t")
        self.ingest("M06", "corporate_action", action_id="CA1", security_id="S1",
                    type="cash_dividend", version=1, status="confirmed",
                    ex_date="2026-02-01", pay_date="2026-02-10",
                    terms={"amount_per_share": "1.00", "currency": "HKD"})
        mid = self.service.pending("A1", "2026-02-05")
        self.assertEqual(1, len(mid["pending"]))  # 除权后、到账前：待到账
        after = self.service.pending("A1", "2026-02-15")
        self.assertEqual(0, len(after["pending"]))  # 到账后：已解决
        before = self.service.pending("A1", "2026-01-20")
        self.assertEqual(0, len(before["pending"]))  # 除权前：尚未产生


if __name__ == "__main__":
    unittest.main()
