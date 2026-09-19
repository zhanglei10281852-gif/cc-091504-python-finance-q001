"""服务层测试：幂等接入、版本演进、快照不可变、重算原子性、导出校验。"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from engine import EngineError  # noqa: E402
from service import Service  # noqa: E402
from store import Store  # noqa: E402


def msg(mid, kind, **payload):
    return {"message_id": mid, "kind": kind, "payload": payload}


def buy_msg(mid, tid, qty, price, date, account="A", security="S"):
    return msg(mid, "trade", trade_id=tid, account=account, security=security,
               side="BUY", quantity=qty, price=price, currency="HKD", fee="0",
               fee_currency="HKD", trade_date=date)


def dividend_msg(mid, action_id, version, dps, status="confirmed"):
    return msg(mid, "corporate_action", action_id=action_id, security="S",
               type="cash_dividend", announcement_version=version,
               ex_date="2026-01-06", record_date="2026-01-08", pay_date="2026-01-20",
               dividend_per_share=dps, currency="HKD", status=status)


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="ledger-test-"))
        self.svc = Service(Store(self.dir))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class IdempotencyTest(ServiceTestBase):
    def test_same_message_retransmitted(self):
        m = buy_msg("m1", "T1", "100", "10", "2026-01-05")
        r1 = self.svc.ingest([m])
        r2 = self.svc.ingest([m])
        self.assertEqual("applied", r1["results"][0]["status"])
        self.assertEqual("duplicate", r2["results"][0]["status"])
        self.assertEqual(1, len(self.svc.store.load_events()))  # 只入账一次

    def test_same_business_object_retransmitted(self):
        r1 = self.svc.ingest([buy_msg("m1", "T1", "100", "10", "2026-01-05")])
        r2 = self.svc.ingest([buy_msg("m2", "T1", "100", "10", "2026-01-05")])
        self.assertEqual("applied", r1["results"][0]["status"])
        self.assertEqual("duplicate", r2["results"][0]["status"])
        self.assertEqual(1, len(self.svc.store.load_events()))

    def test_conflicting_business_object_rejected(self):
        self.svc.ingest([buy_msg("m1", "T1", "100", "10", "2026-01-05")])
        r = self.svc.ingest([buy_msg("m2", "T1", "100", "11", "2026-01-05")])
        self.assertEqual("rejected", r["results"][0]["status"])
        self.assertEqual(1, len(self.svc.store.load_events()))

    def test_conflicting_message_id_rejected(self):
        self.svc.ingest([buy_msg("m1", "T1", "100", "10", "2026-01-05")])
        r = self.svc.ingest([buy_msg("m1", "T2", "100", "10", "2026-01-05")])
        self.assertEqual("rejected", r["results"][0]["status"])

    def test_same_announcement_version_conflict_rejected(self):
        self.svc.ingest([dividend_msg("m1", "D1", 1, "1.0")])
        r = self.svc.ingest([dividend_msg("m2", "D1", 1, "1.5")])
        self.assertEqual("rejected", r["results"][0]["status"])
        # 提高公告版本则接受（更正流程）
        r = self.svc.ingest([dividend_msg("m3", "D1", 2, "1.5")])
        self.assertEqual("applied", r["results"][0]["status"])

    def test_malformed_rejected(self):
        r = self.svc.ingest([{"message_id": "m1", "kind": "trade", "payload": {}}])
        self.assertEqual("rejected", r["results"][0]["status"])
        r = self.svc.ingest([{"kind": "trade", "payload": {}}])
        self.assertEqual("rejected", r["results"][0]["status"])


class VersioningTest(ServiceTestBase):
    def _seed(self):
        self.svc.ingest([
            buy_msg("m1", "T1", "100", "10", "2026-01-05"),
            dividend_msg("m2", "D1", 1, "1.0"),
        ])
        return self.svc.rebuild()

    def test_late_event_creates_new_version_snapshot_untouched(self):
        v1 = self._seed()
        snap = self.svc.publish_snapshot("A", "2026-01-25")
        snap_before = self.svc.get_snapshot(snap["snapshot_id"])
        # 迟到更正：红利 1.0 -> 1.5
        self.svc.ingest([dividend_msg("m3", "D1", 2, "1.5")])
        v2 = self.svc.rebuild()
        self.assertEqual(v1["version"] + 1, v2["version"])
        self.assertNotEqual(v1["version_hash"], v2["version_hash"])
        snap_after = self.svc.get_snapshot(snap["snapshot_id"])
        self.assertEqual(snap_before, snap_after)  # 快照不被改写
        self.assertEqual(v1["version"], snap_after["version"])
        # 当前版本反映更正后的结果：0 - 1000 + 150 = -850
        self.assertEqual("-850.00", self.svc.cash("A", "2026-01-25")["HKD"])
        # 快照仍保留更正前：0 - 1000 + 100 = -900
        self.assertEqual("-900.00", snap_after["cash"]["HKD"])

    def test_rebuild_failure_leaves_no_partial_result(self):
        v1 = self._seed()
        # 超卖事件导致整批重放失败
        self.svc.ingest([msg("m9", "trade", trade_id="T9", account="A", security="S",
                             side="SELL", quantity="9999", price="1", currency="HKD",
                             fee="0", fee_currency="HKD", trade_date="2026-01-09")])
        with self.assertRaises(EngineError):
            self.svc.rebuild()
        cur = self.svc.store.current()
        self.assertEqual(v1["version"], cur["version"])  # 指针未动
        versions = list(self.dir.glob("versions/*.json"))
        self.assertEqual(1, len(versions))  # 没有留下半套版本文件
        self.assertEqual([], list(self.dir.glob("versions/*.tmp")))

    def test_versions_listed(self):
        self._seed()
        self.svc.ingest([dividend_msg("m3", "D1", 2, "1.5")])
        self.svc.rebuild()
        versions = self.svc.versions()
        self.assertEqual([1, 2], [v["version"] for v in versions])


class LineageExportTest(ServiceTestBase):
    def _seed(self):
        self.svc.ingest([
            buy_msg("m1", "T1", "100", "10", "2026-01-05"),
            msg("m2", "corporate_action", action_id="SPL", security="S", type="split",
                announcement_version=1, ex_date="2026-01-06", record_date="2026-01-06",
                pay_date="2026-01-06", numerator="2", denominator="1", currency="HKD"),
            dividend_msg("m3", "D1", 1, "1.0"),
        ])
        self.svc.rebuild()

    def test_lineage_traces_totals_to_original_lots(self):
        self._seed()
        lin = self.svc.lineage("A", "S", "2026-01-20")
        self.assertEqual("200.000000", lin["totals"]["quantity"])
        self.assertEqual(1, len(lin["roots"]))
        root = lin["roots"][0]
        self.assertEqual("trade", root["origin"])
        changes = [h["change"] for h in root["lots"][0]["history"]]
        self.assertEqual(["open", "split"], changes)  # 演变链完整

    def test_export_verifies_and_detects_tamper(self):
        self._seed()
        export = self.svc.export_lineage("A", "S", "2026-01-20")
        result = Service.verify_export(export)
        self.assertTrue(result["valid"], result)
        tampered = json.loads(json.dumps(export))
        tampered["version_doc"]["positions"]["2026-01-06"]["A"]["S"]["quantity"] = "999"
        result = Service.verify_export(tampered)
        self.assertFalse(result["valid"])

    def test_pending_listed_but_not_applied(self):
        self._seed()
        self.svc.ingest([dividend_msg("m4", "D2", 1, "5.0", status="pending")])
        self.svc.rebuild()
        pending = self.svc.pending()
        self.assertEqual(1, len(pending))
        self.assertEqual("D2", pending[0]["payload"]["action_id"])
        # 未确认公告不生效：现金只含 D1 按拆股后 200 股派发的 200（-1000 + 200）
        self.assertEqual("-800.00", self.svc.cash("A", "2026-01-25")["HKD"])


if __name__ == "__main__":
    unittest.main()
