from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from app import health_payload


class HealthTest(unittest.TestCase):
    def test_health_payload(self) -> None:
        payload = health_payload()
        self.assertEqual("ok", payload["status"])
        self.assertTrue(payload["service"])


if __name__ == "__main__":
    unittest.main()
