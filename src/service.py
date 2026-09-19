"""服务层：消息接入（幂等）、版本重建、持仓/谱系查询、快照与导出校验。

- 同一 message_id 重传：返回首次处理结果，不产生任何副作用（幂等）；
- 同一业务标识（成交号/公告号+版本/划转号/汇率键）内容一致的重传同样幂等，
  内容冲突则拒绝并提示走更正流程（公司行动需提高公告版本）；
- 迟到事件只通过 rebuild 产生新的核算版本，已发布快照永不改写；
- rebuild 在内存中一次算完，失败时 current 指针不动，不留半套结果。
"""
from __future__ import annotations

import itertools
import threading
from datetime import datetime, timezone

from engine import EngineError, build_version
from models import ACTION_TYPES, EVENT_KINDS, hash_obj
from store import Store, StoreError

REQUIRED = {
    "cash_movement": ("movement_id", "account", "currency", "amount", "direction", "date"),
    "trade": ("trade_id", "account", "security", "side", "quantity", "price",
              "currency", "trade_date"),
    "transfer": ("transfer_id", "type", "account", "security", "quantity",
                 "currency", "date"),
    "corporate_action": ("action_id", "security", "type", "announcement_version",
                         "ex_date", "record_date", "pay_date"),
    "election": ("account", "action_id", "choice"),
    "confirmation": ("message_id",),
    "fx_rate": ("pair", "rate_date", "rate", "source_version"),
}


def _business_key(kind: str, payload: dict):
    """业务幂等键：同一业务对象的重传可被识别。"""
    if kind == "trade":
        return ("trade", payload["trade_id"])
    if kind == "transfer":
        return ("transfer", payload["transfer_id"])
    if kind == "corporate_action":
        return ("corporate_action", payload["action_id"], payload["announcement_version"])
    if kind == "fx_rate":
        return ("fx_rate", payload["pair"], payload["rate_date"], payload["source_version"])
    if kind == "cash_movement":
        return ("cash_movement", payload["movement_id"])
    if kind == "election":
        return ("election", payload["account"], payload["action_id"],
                hash_obj(payload))  # 选择允许变更，按内容去重
    if kind == "confirmation":
        return ("confirmation", payload["message_id"])
    return None


class Service:
    def __init__(self, store: Store, holidays: set[str] | None = None):
        self.store = store
        self.holidays = set(holidays or set())
        self._lock = threading.Lock()
        self._snapshot_seq = itertools.count(1)

    # ------------------------------------------------------------------ ingest
    def ingest(self, messages: list[dict]) -> dict:
        """接入一批消息。逐条返回 applied / duplicate / rejected。"""
        results = []
        with self._lock:
            known = self.store.load_messages()
            events = self.store.load_events()
            biz_keys = {}
            for env in events:
                key = _business_key(env["kind"], env["payload"])
                if key is not None:
                    biz_keys.setdefault(key, hash_obj(env["payload"]))
            seq = self.store.next_seq()
            new_events = []
            for msg in messages:
                seq += 1
                result, envelope = self._ingest_one(msg, known, biz_keys, seq)
                if envelope is not None:
                    new_events.append(envelope)
                    key = _business_key(envelope["kind"], envelope["payload"])
                    if key is not None:
                        biz_keys[key] = hash_obj(envelope["payload"])
                else:
                    seq -= 1  # 未入库的消息不消耗序号
                results.append(result)
            if new_events:
                self.store.append_events(new_events)
            self.store.save_messages(known)
        return {"results": results, "accepted": len(new_events)}

    def _ingest_one(self, msg: dict, known: dict, biz_keys: dict, seq: int):
        message_id = msg.get("message_id")
        kind = msg.get("kind")
        payload = msg.get("payload")
        if not message_id or kind not in EVENT_KINDS or not isinstance(payload, dict):
            return {"message_id": message_id, "status": "rejected",
                    "detail": "缺少 message_id/kind/payload 或 kind 非法"}, None
        content_hash = hash_obj({"kind": kind, "payload": payload})
        if message_id in known:  # 同一消息重传：幂等
            first = known[message_id]
            if first["content_hash"] == content_hash:
                return {"message_id": message_id, "status": "duplicate",
                        "detail": "相同消息已处理，返回首次结果",
                        "first_result": first["result"]}, None
            return {"message_id": message_id, "status": "rejected",
                    "detail": "message_id 冲突：同标识不同内容"}, None
        missing = [f for f in REQUIRED[kind] if f not in payload]
        if missing:
            return {"message_id": message_id, "status": "rejected",
                    "detail": f"缺少字段: {', '.join(missing)}"}, None
        if kind == "corporate_action" and payload["type"] not in ACTION_TYPES:
            return {"message_id": message_id, "status": "rejected",
                    "detail": f"未知公司行动类型: {payload['type']}"}, None
        key = _business_key(kind, payload)
        if key is not None and key in biz_keys:
            if biz_keys[key] == hash_obj(payload):
                result = {"message_id": message_id, "status": "duplicate",
                          "detail": "相同业务对象已入账"}
                known[message_id] = {"content_hash": content_hash, "result": result}
                return result, None
            return {"message_id": message_id, "status": "rejected",
                    "detail": "业务标识冲突：内容不一致，请走更正流程"
                              "（公司行动需提高 announcement_version）"}, None
        envelope = {
            "seq": seq,
            "message_id": message_id,
            "kind": kind,
            "payload": payload,
            "received_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        result = {"message_id": message_id, "status": "applied", "seq": seq}
        known[message_id] = {"content_hash": content_hash, "result": result}
        return result, envelope

    # ----------------------------------------------------------------- rebuild
    def rebuild(self) -> dict:
        """全量重放并原子发布新版本；失败时当前版本保持不变。"""
        with self._lock:
            events = self.store.load_events()
            version_doc = build_version(events, self.holidays)  # 失败则抛错，不落盘
            n = self.store.save_version(version_doc)
        return {"version": n, "version_hash": version_doc["version_hash"],
                "state_hash": version_doc["state_hash"],
                "events_applied": len(version_doc["events_applied"]),
                "pending": len(version_doc["pending"])}

    # ----------------------------------------------------------------- queries
    def _current_version(self) -> dict:
        cur = self.store.current()
        if not cur:
            raise StoreError("尚无核算版本，请先重建")
        return self.store.load_version(cur["version"])

    @staticmethod
    def _resolve_day(version_doc: dict, day: str) -> str | None:
        days = [d for d in version_doc["positions"] if d <= day]
        return max(days) if days else None

    def positions(self, account: str, day: str) -> dict:
        v = self._current_version()
        resolved = self._resolve_day(v, day)
        positions = (v["positions"].get(resolved, {}).get(account, {}) if resolved else {})
        cash = {}
        if resolved:
            for key, amount in v["cash"].get(resolved, {}).items():
                acc, ccy = key.split("|", 1)
                if acc == account:
                    cash[ccy] = amount
        return {"account": account, "requested_date": day, "as_of": resolved,
                "version": v["version"], "positions": positions, "cash": cash}

    def cash(self, account: str, day: str) -> dict:
        return self.positions(account, day)["cash"]

    def realized(self, account: str) -> list[dict]:
        v = self._current_version()
        return [r for r in v["realized"] if r["account"] == account]

    def pending(self) -> list[dict]:
        return self._current_version()["pending"]

    def corporate_actions(self, security: str | None = None) -> list[dict]:
        v = self._current_version()
        actions = [a for a in v["actions_used"]
                   if security is None or a["security"] == security]
        return actions

    def fx(self, pair: str, day: str) -> dict:
        events = self.store.load_events()
        best = None
        for env in events:
            if env["kind"] != "fx_rate":
                continue
            p = env["payload"]
            if p["pair"] == pair and p["rate_date"] <= day:
                if best is None or (p["rate_date"], p["source_version"]) > \
                                   (best["rate_date"], best["source_version"]):
                    best = p
        if best is None:
            raise StoreError(f"无可用汇率 {pair}（{day} 或之前）")
        return best

    def versions(self) -> list[dict]:
        return self.store.list_versions()

    # ----------------------------------------------------------------- lineage
    def lineage(self, account: str, security: str, day: str) -> dict:
        """成本沿革：任一持仓日的总额如何由原始批次演变而来。"""
        v = self._current_version()
        resolved = self._resolve_day(v, day)
        if not resolved:
            return {"account": account, "security": security, "requested_date": day,
                    "as_of": None, "version": v["version"], "roots": [], "totals": None}
        pos = v["positions"].get(resolved, {}).get(account, {}).get(security)
        roots: dict[str, dict] = {}
        if pos:
            for lot_view in pos["lots"]:
                lot = v["lot_index"][lot_view["lot_id"]]
                root_id = lot["root_id"]
                root = roots.setdefault(root_id, {
                    "root_lot_id": root_id,
                    "origin": v["lot_index"][root_id]["origin"],
                    "opened_date": v["lot_index"][root_id]["opened_date"],
                    "lots": [],
                })
                history = [h for h in v["lot_histories"].get(lot_view["lot_id"], [])
                           if h["date"] <= resolved]
                root["lots"].append({**lot_view, "derived_from": lot["derived_from"],
                                     "history": history})
        actions = [a for a in v["actions_used"] if a["security"] == security]
        fx_usages = [u for u in v["fx_usages"]
                     if u.get("account") == account and u.get("security") == security
                     and u["date"] <= resolved]
        pending = [p for p in v["pending"]
                   if p["payload"].get("account") in (None, account)
                   and p["payload"].get("security") in (None, security)]
        return {
            "account": account, "security": security, "requested_date": day,
            "as_of": resolved, "version": v["version"],
            "version_hash": v["version_hash"],
            "totals": pos, "roots": list(roots.values()),
            "announcements": actions,
            "fx_usages": fx_usages,
            "pending": pending,
        }

    # --------------------------------------------------------------- snapshots
    def publish_snapshot(self, account: str, date: str) -> dict:
        """出具客户快照。快照不可变；之后的迟到事件只产生新版本。"""
        with self._lock:
            v = self._current_version()
            resolved = self._resolve_day(v, date)
            positions = v["positions"].get(resolved, {}).get(account, {}) if resolved else {}
            cash = {}
            if resolved:
                for key, amount in v["cash"].get(resolved, {}).items():
                    acc, ccy = key.split("|", 1)
                    if acc == account:
                        cash[ccy] = amount
            snapshot_id = f"SNAP-{account}-{date}-{next(self._snapshot_seq):04d}"
            snapshot = {
                "snapshot_id": snapshot_id,
                "account": account,
                "date": date,
                "as_of": resolved,
                "version": v["version"],
                "version_hash": v["version_hash"],
                "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "positions": positions,
                "cash": cash,
            }
            snapshot["snapshot_hash"] = hash_obj({
                "account": account, "date": date, "version": v["version"],
                "positions": positions, "cash": cash,
            })
            self.store.save_snapshot(snapshot)
            return {k: snapshot[k] for k in
                    ("snapshot_id", "account", "date", "as_of", "version",
                     "version_hash", "snapshot_hash", "published_at")}

    def get_snapshot(self, snapshot_id: str) -> dict:
        return self.store.load_snapshot(snapshot_id)

    def list_snapshots(self) -> list[dict]:
        return self.store.list_snapshots()

    # ------------------------------------------------------------------ export
    def export_lineage(self, account: str, security: str, day: str) -> dict:
        """导出可校验的成本沿革：内含完整版本文档，第三方可独立重算哈希。"""
        v = self._current_version()
        return {
            "type": "cost-lineage-export",
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "focus": {"account": account, "security": security, "date": day},
            "version": v["version"],
            "version_hash": v["version_hash"],
            "state_hash": v["state_hash"],
            "lineage": self.lineage(account, security, day),
            "version_doc": v,
        }

    @staticmethod
    def verify_export(export: dict) -> dict:
        """独立校验导出件：重算状态哈希与版本哈希，并抽查谱系总额。"""
        checks = []
        doc = export.get("version_doc", {})
        state_hash = hash_obj({
            "positions": doc.get("positions"), "cash": doc.get("cash"),
            "lot_index": doc.get("lot_index"), "realized": doc.get("realized"),
        })
        checks.append({"check": "state_hash", "ok": state_hash == doc.get("state_hash")
                       == export.get("state_hash")})
        version_hash = hash_obj({
            "events": [a["hash"] for a in doc.get("events_applied", [])],
            "reference": [r["hash"] for r in doc.get("reference_events", [])],
            "state": doc.get("state_hash"),
        })
        checks.append({"check": "version_hash", "ok": version_hash == doc.get("version_hash")
                       == export.get("version_hash")})
        focus = export.get("focus", {})
        lineage = export.get("lineage", {})
        day = lineage.get("as_of")
        pos = doc.get("positions", {}).get(day, {}) \
            .get(focus.get("account"), {}).get(focus.get("security"))
        checks.append({"check": "lineage_totals",
                       "ok": (lineage.get("totals") or None) == (pos or None)})
        return {"valid": all(c["ok"] for c in checks), "checks": checks}
