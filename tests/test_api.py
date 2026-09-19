"""HTTP 接口端到端测试：真实起服务、走 JSON 接口。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from app import create_server  # noqa: E402
from service import Service  # noqa: E402
from store import Store  # noqa: E402


class ApiCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.server = create_server("127.0.0.1", 0,
                                   Service(Store(cls.tmp.name)))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def call(self, method: str, path: str, body: dict | None = None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = None
        headers = {"Content-Type": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=headers)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def post_message(self, mid: str, kind: str, **payload):
        return self.call("POST", "/v1/messages",
                         {"message_id": mid, "kind": kind, "payload": payload})


class ApiTest(ApiCase):
    def test_00_health(self):
        status, body = self.call("GET", "/health")
        self.assertEqual(200, status)
        self.assertEqual("ok", body["status"])

    def test_01_unknown_route_404(self):
        status, body = self.call("GET", "/nope")
        self.assertEqual(404, status)
        self.assertEqual("not_found", body["error"]["code"])

    def test_02_full_flow(self):
        for mid, kind, payload in [
            ("A01", "account", {"account_id": "ACC", "base_currency": "CNY"}),
            ("A02", "security", {"security_id": "S1", "currency": "HKD",
                                 "market": "HKEX"}),
            ("A03", "fx_rate", {"base": "HKD", "quote": "CNY",
                                "date": "2026-01-01", "rate": "0.9"}),
            ("A04", "cash_movement", {"account_id": "ACC", "currency": "HKD",
                                      "amount": "50000", "date": "2026-01-02"}),
            ("A05", "trade", {"trade_id": "T1", "account_id": "ACC",
                              "security_id": "S1", "side": "buy",
                              "quantity": "1000", "price": "10",
                              "currency": "HKD", "trade_date": "2026-01-05"}),
            ("A06", "corporate_action",
             {"action_id": "CA1", "security_id": "S1", "type": "split",
              "version": 1, "status": "confirmed", "ex_date": "2026-02-01",
              "terms": {"from": 1, "to": 2}}),
        ]:
            status, body = self.post_message(mid, kind, **payload)
            self.assertEqual(200, status, body)
        # 重传幂等
        status, body = self.post_message("A05", "trade",
                                         trade_id="T1", account_id="ACC",
                                         security_id="S1", side="buy",
                                         quantity="1000", price="10",
                                         currency="HKD",
                                         trade_date="2026-01-05")
        self.assertEqual(200, status)
        self.assertTrue(body["idempotent_replay"])
        # 持仓查询
        status, pos = self.call("GET",
                                "/v1/accounts/ACC/positions?date=2026-02-02")
        self.assertEqual(200, status)
        self.assertEqual("2000.000000", pos["positions"][0]["quantity"])
        # 版本列表
        status, versions = self.call("GET", "/v1/versions")
        self.assertEqual(200, status)
        self.assertEqual(6, versions["current_version"])
        # 快照出具与查询
        status, snap = self.call("POST", "/v1/snapshots",
                                 {"account_id": "ACC", "date": "2026-02-02"})
        self.assertEqual(201, status)
        snap_id = snap["snapshot_id"]
        status, snap2 = self.call("GET", f"/v1/snapshots/{snap_id}")
        self.assertEqual(snap, snap2)
        # 沿革导出与校验
        status, export = self.call(
            "GET", "/v1/accounts/ACC/lineage/export?security_id=S1&date=2026-02-02")
        self.assertEqual(200, status)
        status, verify = self.call("POST", "/v1/lineage/verify", export)
        self.assertTrue(verify["valid"])
        # 公司行动版本信息
        status, actions = self.call("GET", "/v1/corporate-actions")
        self.assertEqual("confirmed", actions["corporate_actions"][0]["status"])
        # 汇率查询
        status, fx = self.call(
            "GET", "/v1/fx-rates/lookup?base=HKD&quote=CNY&date=2026-01-05")
        self.assertEqual("0.9", fx["rate"])
        self.assertEqual("2026-01-01", fx["rate_date"])

    def test_03_validation_error_400(self):
        status, body = self.call("POST", "/v1/messages",
                                 {"message_id": "B01", "kind": "trade",
                                  "payload": {"trade_id": "X"}})
        self.assertEqual(400, status)
        self.assertEqual("validation_error", body["error"]["code"])

    def test_04_missing_date_param(self):
        status, body = self.call("GET", "/v1/accounts/ACC/positions")
        self.assertEqual(400, status)


if __name__ == "__main__":
    unittest.main()
