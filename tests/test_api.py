"""HTTP 接口端到端测试：真实起服务、走完整流程。"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from app import build_service, create_server  # noqa: E402


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp(prefix="ledger-api-"))
        service = build_service(str(cls.dir), holidays={"2026-06-24"})
        cls.server = create_server("127.0.0.1", 0, service)
        cls.port = cls.server.server_address[1]
        import threading
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def _req(self, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_full_flow(self):
        code, body = self._req("GET", "/health")
        self.assertEqual(200, code)
        self.assertEqual("ok", body["status"])

        messages = [
            {"message_id": "a1", "kind": "cash_movement", "payload": {
                "movement_id": "CM1", "account": "ACC", "currency": "HKD",
                "amount": "50000", "direction": "deposit", "date": "2026-01-01"}},
            {"message_id": "a2", "kind": "trade", "payload": {
                "trade_id": "T1", "account": "ACC", "security": "S", "side": "BUY",
                "quantity": "100", "price": "10", "currency": "HKD", "fee": "0",
                "fee_currency": "HKD", "trade_date": "2026-01-05"}},
            {"message_id": "a3", "kind": "corporate_action", "payload": {
                "action_id": "D1", "security": "S", "type": "cash_dividend",
                "announcement_version": 1, "ex_date": "2026-06-08",
                "record_date": "2026-06-10", "pay_date": "2026-06-24",
                "dividend_per_share": "2", "currency": "HKD"}},
        ]
        code, body = self._req("POST", "/ingest", {"messages": messages})
        self.assertEqual(200, code)
        self.assertEqual(3, body["accepted"])
        # 重传同一批：全部幂等
        code, body = self._req("POST", "/ingest", {"messages": messages})
        self.assertTrue(all(r["status"] == "duplicate" for r in body["results"]))

        code, body = self._req("POST", "/rebuild")
        self.assertEqual(200, code)
        self.assertEqual(1, body["version"])

        code, body = self._req("GET", "/positions?account=ACC&date=2026-06-30")
        self.assertEqual(200, code)
        self.assertEqual("100.000000", body["positions"]["S"]["quantity"])
        # 到账日 06-24 是假日，红利 06-25 到账：50000 - 1000 + 200
        self.assertEqual("49200.00", body["cash"]["HKD"])

        code, body = self._req("GET", "/lineage?account=ACC&security=S&date=2026-06-30")
        self.assertEqual(200, code)
        self.assertEqual("100.000000", body["totals"]["quantity"])
        self.assertEqual(1, len(body["roots"]))

        code, body = self._req("POST", "/snapshots", {"account": "ACC", "date": "2026-06-30"})
        self.assertEqual(201, code)
        snap_id = body["snapshot_id"]

        # 迟到更正：红利 2 -> 3，只能产生新版本
        self._req("POST", "/ingest", {"messages": [
            {"message_id": "a4", "kind": "corporate_action", "payload": {
                "action_id": "D1", "security": "S", "type": "cash_dividend",
                "announcement_version": 2, "ex_date": "2026-06-08",
                "record_date": "2026-06-10", "pay_date": "2026-06-24",
                "dividend_per_share": "3", "currency": "HKD"}}]})
        self._req("POST", "/rebuild")
        code, snap = self._req("GET", f"/snapshots/{snap_id}")
        self.assertEqual(200, code)
        self.assertEqual(1, snap["version"])  # 快照仍属旧版本
        self.assertEqual("49200.00", snap["cash"]["HKD"])
        code, body = self._req("GET", "/positions?account=ACC&date=2026-06-30")
        self.assertEqual("49300.00", body["cash"]["HKD"])  # 当前版本已更正

        code, body = self._req("GET", "/export/lineage?account=ACC&security=S&date=2026-06-30")
        self.assertEqual(200, code)
        code, result = self._req("POST", "/verify", body)
        self.assertTrue(result["valid"], result)

        code, body = self._req("GET", "/versions")
        self.assertEqual([1, 2], [v["version"] for v in body["versions"]])

    def test_bad_request_and_404(self):
        code, _ = self._req("GET", "/nope")
        self.assertEqual(404, code)
        code, _ = self._req("GET", "/positions?account=ACC")
        self.assertEqual(400, code)


if __name__ == "__main__":
    unittest.main()
