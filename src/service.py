"""服务层：消息摄入（幂等）、版本重算（原子）、查询与快照。"""
from __future__ import annotations

import threading
from typing import Any

from engine import FxBook, RebuildError, Replayer
from store import Store
from util import D, GENESIS, chain_hash, dec_str, hash_obj, now_iso, parse_date


class ServiceError(Exception):
    status = 500
    code = "internal_error"

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ValidationError(ServiceError):
    status = 400
    code = "validation_error"


class NotFoundError(ServiceError):
    status = 404
    code = "not_found"


class ConflictError(ServiceError):
    status = 409
    code = "conflict"


# 各类消息的必填字段；公司行动按类型补充校验
PAYLOAD_SPECS: dict[str, list[str]] = {
    "account": ["account_id", "base_currency"],
    "security": ["security_id", "currency"],
    "fx_rate": ["base", "quote", "date", "rate"],
    "calendar": ["market", "days"],
    "cash_movement": ["account_id", "currency", "amount", "date"],
    "trade": ["trade_id", "account_id", "security_id", "side",
              "quantity", "price", "currency", "trade_date"],
    "transfer": ["transfer_id", "security_id", "from_account", "to_account",
                 "quantity", "transfer_date"],
    "corporate_action": ["action_id", "security_id", "type", "version", "status"],
    "election": ["action_id", "account_id", "mode"],
    "freeze": ["account_id", "security_id", "quantity", "date"],
    "unfreeze": ["account_id", "security_id", "quantity", "date"],
    "position_snapshot": ["account_id", "security_id", "date", "quantity"],
}

ACTION_TYPES = {"split", "rights_issue", "cash_dividend", "reinvestment"}
ACTION_STATUSES = {"preliminary", "confirmed", "cancelled"}
DATE_FIELDS = ("ex_date", "record_date", "pay_date")


def validate_payload(kind: str, payload: dict) -> None:
    spec = PAYLOAD_SPECS.get(kind)
    if spec is None:
        raise ValidationError(f"未知消息类型 {kind}", {"kind": kind})
    missing = [f for f in spec if payload.get(f) is None]
    if missing:
        raise ValidationError(f"消息缺少必填字段 {missing}", {"missing": missing})
    if kind == "trade" and payload["side"] not in ("buy", "sell"):
        raise ValidationError("side 必须是 buy 或 sell")
    if kind == "corporate_action":
        if payload["type"] not in ACTION_TYPES:
            raise ValidationError(f"未知公司行动类型 {payload['type']}")
        if payload["status"] not in ACTION_STATUSES:
            raise ValidationError(f"未知公告状态 {payload['status']}")
        if int(payload["version"]) < 1:
            raise ValidationError("公告版本必须为正整数")
        if payload["status"] != "cancelled":
            atype = payload["type"]
            if atype in ("split", "rights_issue", "cash_dividend") \
                    and not payload.get("ex_date"):
                raise ValidationError(f"{atype} 必须提供除权日 ex_date")
            if atype in ("rights_issue", "cash_dividend", "reinvestment") \
                    and not payload.get("pay_date"):
                raise ValidationError(f"{atype} 必须提供到账日 pay_date")
            terms = payload.get("terms") or {}
            if atype == "split" and ("from" not in terms or "to" not in terms):
                raise ValidationError("拆股条款必须包含 from/to 比例")
            if atype == "rights_issue":
                for f in ("ratio_base", "ratio_rights", "subscription_price"):
                    if f not in terms:
                        raise ValidationError(f"配股条款缺少 {f}")
            if atype == "cash_dividend" and "amount_per_share" not in terms:
                raise ValidationError("红利条款缺少 amount_per_share")
            if atype == "reinvestment" and (
                    "dividend_action_id" not in terms or "price" not in terms):
                raise ValidationError("再投资条款缺少 dividend_action_id 或 price")
    for field in DATE_FIELDS + ("date", "trade_date", "transfer_date"):
        if payload.get(field) is not None:
            try:
                parse_date(payload[field])
            except ValueError:
                raise ValidationError(f"日期格式非法: {field}={payload[field]}")


class Service:
    def __init__(self, store: Store):
        self.store = store
        self._lock = threading.RLock()

    # ------------------------------------------------------------ 摄入

    def ingest(self, message: dict) -> tuple[dict, int]:
        """摄入消息。同一 message_id 重传返回首次结果，不产生新效果。"""
        mid = message.get("message_id")
        kind = message.get("kind")
        payload = message.get("payload")
        if not mid or not isinstance(mid, str):
            raise ValidationError("消息缺少 message_id")
        if not kind or not isinstance(kind, str):
            raise ValidationError("消息缺少 kind")
        if not isinstance(payload, dict):
            raise ValidationError("消息缺少 payload 对象")
        auto = bool(message.get("auto_recalculate", True))
        req_hash = hash_obj({"kind": kind, "payload": payload})
        with self._lock:
            existing = self.store.get_message(mid)
            if existing is not None:
                if existing["request_hash"] == req_hash:
                    response = dict(existing["response"])
                    response["idempotent_replay"] = True
                    return response, existing["status_code"]
                raise ConflictError(
                    f"message_id {mid} 已存在但内容不一致",
                    {"message_id": mid},
                )
            try:
                validate_payload(kind, payload)
            except ValidationError as exc:
                response = {"message_id": mid, "status": "rejected",
                            "error": {"code": exc.code, "message": exc.message,
                                      "details": exc.details}}
                self.store.save_message(mid, {"request_hash": req_hash,
                                              "response": response, "status_code": 400})
                return response, 400
            event = {"seq": self.store.next_event_seq(), "message_id": mid,
                     "kind": kind, "payload": payload, "received_at": now_iso()}
            self.store.append_event(event)
            response: dict[str, Any] = {"message_id": mid, "event_seq": event["seq"],
                                        "status": "accepted"}
            status_code = 200
            if auto:
                try:
                    response["recalculation"] = self._recalculate_locked([mid])
                except RebuildError as exc:
                    response["recalculation"] = {"status": "failed",
                                                 "error": exc.message,
                                                 "details": exc.details}
                    status_code = 422
            self.store.save_message(mid, {"request_hash": req_hash,
                                          "response": response,
                                          "status_code": status_code})
            return response, status_code

    # ------------------------------------------------------------ 重算

    def recalculate(self, trigger_message_ids: list[str] | None = None) -> dict:
        with self._lock:
            return self._recalculate_locked(trigger_message_ids or [])

    def _recalculate_locked(self, trigger: list[str]) -> dict:
        """全量重放并原子提交新版本；失败时当前版本保持不变。

        事件日志未发生变化时不产生新版本（空转保护）。
        """
        events = self.store.load_events()
        log_hash = self.store.event_log_hash(events)
        prev = self.store.current_seq()
        if prev is not None:
            prev_doc = self.store.load_version(prev)
            if prev_doc is not None and prev_doc["event_log_hash"] == log_hash:
                return {"status": "no_change", "version_seq": prev,
                        "events_included": len(events),
                        "event_log_hash": log_hash}
        try:
            state = Replayer(events).run()
        except RebuildError as exc:
            self.store.append_failed({"at": now_iso(), "trigger": trigger,
                                      "error": exc.message, "details": exc.details})
            raise
        seq = (prev or 0) + 1
        doc = {
            "version_seq": seq,
            "created_at": now_iso(),
            "trigger_message_ids": trigger,
            "events_included": len(events),
            "event_log_hash": log_hash,
            "supersedes": prev,
            **state,
        }
        self.store.save_version(doc)   # 先写版本文件
        self.store.set_current(seq)    # 再拨动当前指针
        return {
            "status": "committed",
            "version_seq": seq,
            "supersedes": prev,
            "events_included": len(events),
            "event_log_hash": doc["event_log_hash"],
            "pending_count": len([p for p in state["pending"]
                                  if p["resolved_date"] is None]),
            "warnings": state["warnings"],
            "validations": state["validations"],
        }

    # ------------------------------------------------------------ 版本

    def list_versions(self) -> dict:
        current = self.store.current_seq()
        return {"current_version": current, "versions": self.store.list_versions()}

    def get_version(self, seq: int) -> dict:
        doc = self.store.load_version(seq)
        if doc is None:
            raise NotFoundError(f"核算版本 v{seq} 不存在")
        return doc

    def _version_doc(self, seq: int | None) -> dict:
        if seq is None:
            seq = self.store.current_seq()
            if seq is None:
                raise NotFoundError("尚无核算版本，请先摄入消息并触发重算")
        return self.get_version(seq)

    # ------------------------------------------------------------ 查询

    @staticmethod
    def _state_at(doc: dict, day: str) -> dict:
        dates = [d for d in doc["changes"] if d <= day]
        if not dates:
            return {"lots": {}, "cash": {}}
        return doc["changes"][max(dates)]

    @staticmethod
    def _public_lot(lot: dict) -> dict:
        qty, frozen = D(lot["quantity"]), D(lot["frozen"])
        return {
            "lot_id": lot["lot_id"],
            "quantity": dec_str(qty),
            "frozen": dec_str(frozen),
            "sellable": dec_str(qty - frozen),
            "currency": lot["currency"],
            "cost_total": lot["cost_total"],
            "cost_per_share": lot["cost_per_share"],
            "base_currency": lot["base_currency"],
            "base_cost_total": lot["base_cost_total"],
            "acquired_date": lot["acquired_date"],
            "origin": lot["origin"],
        }

    def positions(self, account_id: str, day: str, version: int | None = None) -> dict:
        doc = self._version_doc(version)
        state = self._state_at(doc, day)
        positions = []
        for sid, lots in sorted(state["lots"].get(account_id, {}).items()):
            open_lots = [l for l in lots
                         if not l["closed"] and D(l["quantity"]) > 0]
            if not open_lots:
                continue
            qty = sum((D(l["quantity"]) for l in open_lots), D(0))
            frozen = sum((D(l["frozen"]) for l in open_lots), D(0))
            cost = sum((D(l["cost_total"]) for l in open_lots), D(0))
            base_known = all(l["base_cost_total"] is not None for l in open_lots)
            base = sum((D(l["base_cost_total"]) for l in open_lots), D(0)) if base_known else None
            positions.append({
                "security_id": sid,
                "quantity": dec_str(qty),
                "frozen": dec_str(frozen),
                "sellable": dec_str(qty - frozen),
                "currency": open_lots[0]["currency"],
                "cost_total": dec_str(cost),
                "base_currency": open_lots[0]["base_currency"],
                "base_cost_total": dec_str(base) if base is not None else None,
                "lots": [self._public_lot(l) for l in open_lots],
            })
        cash = [
            {"currency": ccy, "balance": bal["balance"], "frozen": bal["frozen"]}
            for ccy, bal in sorted(state["cash"].get(account_id, {}).items())
        ]
        pending = self._pending_as_of(doc, day, account_id)
        return {
            "account_id": account_id,
            "date": day,
            "version_seq": doc["version_seq"],
            "positions": positions,
            "cash": cash,
            "pending_count": len(pending),
        }

    def cash(self, account_id: str, day: str, version: int | None = None) -> dict:
        return {"account_id": account_id, "date": day,
                "cash": self.positions(account_id, day, version)["cash"],
                "version_seq": self._version_doc(version)["version_seq"]}

    @staticmethod
    def _pending_as_of(doc: dict, day: str, account_id: str | None = None) -> list[dict]:
        out = []
        for item in doc["pending"]:
            if account_id is not None and item.get("account_id") not in (None, account_id):
                continue
            if item["since_date"] and item["since_date"] > day:
                continue
            if item["resolved_date"] is not None and item["resolved_date"] <= day:
                continue
            out.append(item)
        return out

    def pending(self, account_id: str, day: str, version: int | None = None) -> dict:
        doc = self._version_doc(version)
        return {"account_id": account_id, "date": day,
                "version_seq": doc["version_seq"],
                "pending": self._pending_as_of(doc, day, account_id)}

    def corporate_actions(self, version: int | None = None) -> dict:
        doc = self._version_doc(version)
        return {"version_seq": doc["version_seq"],
                "corporate_actions": list(doc["corporate_actions"].values())}

    def realized(self, account_id: str, version: int | None = None) -> dict:
        doc = self._version_doc(version)
        return {"account_id": account_id, "version_seq": doc["version_seq"],
                "realized": [r for r in doc["realized"] if r["account_id"] == account_id]}

    # ------------------------------------------------------------ 成本沿革

    def lineage(self, account_id: str, security_id: str, day: str,
                version: int | None = None) -> dict:
        doc = self._version_doc(version)
        state = self._state_at(doc, day)
        lots = [l for l in state["lots"].get(account_id, {}).get(security_id, [])
                if not l["closed"] and D(l["quantity"]) > 0]
        actions = [a for a in doc["corporate_actions"].values()
                   if a["security_id"] == security_id]
        return {
            "account_id": account_id,
            "security_id": security_id,
            "date": day,
            "version_seq": doc["version_seq"],
            "lots": lots,
            "dispositions": [d for d in doc["dispositions"]
                             if d["account_id"] == account_id
                             and d.get("security_id") == security_id
                             and d["date"] <= day],
            "realized": [r for r in doc["realized"]
                         if r["account_id"] == account_id
                         and r["security_id"] == security_id and r["date"] <= day],
            "pending": [p for p in self._pending_as_of(doc, day, account_id)
                        if p.get("security_id") in (None, security_id)],
            "corporate_actions": actions,
        }

    def export_lineage(self, account_id: str, security_id: str, day: str,
                       version: int | None = None) -> dict:
        """导出可校验的成本沿革：批次沿革 + 流水步骤 + 哈希链。"""
        doc = self._version_doc(version)
        lin = self.lineage(account_id, security_id, day, version)
        steps = [e for e in doc["ledger"]
                 if e.get("account_id") == account_id
                 and e.get("security_id") == security_id
                 and e["date"] <= day]
        chain = []
        h = GENESIS
        for step in steps:
            step_hash = hash_obj(step)
            h = chain_hash(h, step_hash)
            chain.append({"entry_id": step["entry_id"], "step_hash": step_hash,
                          "chain_hash": h})
        lots = []
        for lot in lin["lots"]:
            lots.append({**lot, "sellable": dec_str(D(lot["quantity"]) - D(lot["frozen"])),
                         "lot_checksum": hash_obj(lot)})
        return {
            "export_type": "cost_lineage",
            "account_id": account_id,
            "security_id": security_id,
            "as_of": day,
            "version_seq": doc["version_seq"],
            "event_log_hash": doc["event_log_hash"],
            "generated_at": now_iso(),
            "lots": lots,
            "pending": lin["pending"],
            "dispositions": lin["dispositions"],
            "realized": lin["realized"],
            "corporate_actions": lin["corporate_actions"],
            "steps": steps,
            "chain": chain,
            "lineage_hash": h,
            "verify": {
                "algorithm": "sha256",
                "canonical": "json(sort_keys,separators=(',',':'),ensure_ascii=False)",
                "recipe": "step_hash=sha256(canonical(step)); "
                          "chain_hash=sha256(prev_chain_hash+'|'+step_hash)，起点为64个'0'；"
                          "lot_checksum=sha256(canonical(lot 不含 lot_checksum 与派生字段 sellable))",
            },
        }

    def verify_lineage(self, export_doc: dict) -> dict:
        """独立重算导出文档的哈希链与批次校验值。"""
        checks = []
        ok_all = True
        h = GENESIS
        steps = export_doc.get("steps", [])
        chain = export_doc.get("chain", [])
        for i, step in enumerate(steps):
            step_hash = hash_obj(step)
            h = chain_hash(h, step_hash)
            expected = chain[i] if i < len(chain) else {}
            ok = expected.get("step_hash") == step_hash and expected.get("chain_hash") == h
            ok_all = ok_all and ok
            checks.append({"name": f"step:{step.get('entry_id')}", "ok": ok})
        lineage_ok = h == export_doc.get("lineage_hash")
        ok_all = ok_all and lineage_ok and len(steps) == len(chain)
        checks.append({"name": "lineage_hash", "ok": lineage_ok})
        for lot in export_doc.get("lots", []):
            claimed = lot.get("lot_checksum")
            bare = {k: v for k, v in lot.items()
                    if k not in ("lot_checksum", "sellable")}
            ok = hash_obj(bare) == claimed
            ok_all = ok_all and ok
            checks.append({"name": f"lot:{lot.get('lot_id')}", "ok": ok})
        return {"valid": ok_all, "checks": checks}

    # ------------------------------------------------------------ 快照

    def issue_snapshot(self, account_id: str, day: str, label: str = "") -> dict:
        """出具客户快照：钉住当前核算版本，之后迟到事件不得覆盖。"""
        with self._lock:
            doc = self._version_doc(None)
            pos = self.positions(account_id, day)
            pend = self._pending_as_of(doc, day, account_id)
            snap = {
                "snapshot_id": f"SNP-{self.store.next_snapshot_seq():04d}",
                "account_id": account_id,
                "date": day,
                "version_seq": doc["version_seq"],
                "issued_at": now_iso(),
                "label": label,
                "positions": pos["positions"],
                "cash": pos["cash"],
                "pending": pend,
            }
            self.store.save_snapshot(snap)
            return snap

    def get_snapshot(self, snapshot_id: str) -> dict:
        snap = self.store.get_snapshot(snapshot_id)
        if snap is None:
            raise NotFoundError(f"快照 {snapshot_id} 不存在")
        return snap

    def list_snapshots(self) -> dict:
        return {"snapshots": self.store.list_snapshots()}

    def compare_snapshot(self, snapshot_id: str) -> dict:
        """对比快照出具时的口径与当前最新版本（解释客户看到的跳变）。"""
        snap = self.get_snapshot(snapshot_id)
        current = self.positions(snap["account_id"], snap["date"])
        then = {p["security_id"]: p for p in snap["positions"]}
        now = {p["security_id"]: p for p in current["positions"]}
        diffs = []
        for sid in sorted(set(then) | set(now)):
            before, after = then.get(sid), now.get(sid)
            diffs.append({
                "security_id": sid,
                "quantity_then": before["quantity"] if before else None,
                "quantity_now": after["quantity"] if after else None,
                "cost_then": before["cost_total"] if before else None,
                "cost_now": after["cost_total"] if after else None,
                "base_cost_then": before["base_cost_total"] if before else None,
                "base_cost_now": after["base_cost_total"] if after else None,
                "quantity_delta": dec_str(D(after["quantity"]) - D(before["quantity"]))
                if before and after else None,
            })
        return {
            "snapshot_id": snapshot_id,
            "account_id": snap["account_id"],
            "date": snap["date"],
            "snapshot_version": snap["version_seq"],
            "current_version": current["version_seq"],
            "restated": snap["version_seq"] != current["version_seq"],
            "diffs": diffs,
        }

    # ------------------------------------------------------------ 其他

    def fx_lookup(self, base: str, quote: str, day: str) -> dict:
        book = FxBook()
        for ev in self.store.load_events():
            if ev["kind"] == "fx_rate":
                p = ev["payload"]
                book.add(p["base"], p["quote"], p["date"], p["rate"],
                         p.get("source", ""), ev["message_id"])
        found = book.lookup(base, quote, day)
        if found is None:
            raise NotFoundError(f"找不到汇率 {base}->{quote}@{day}")
        return {"base": base, "quote": quote, "date": day,
                "rate": dec_str(found["rate"]), "rate_date": found["date"],
                "source": found["source"], "inverted": found["inverted"]}

    def list_events(self, kind: str | None = None, limit: int = 200) -> dict:
        events = self.store.load_events()
        if kind:
            events = [e for e in events if e["kind"] == kind]
        return {"total": len(events), "events": events[-limit:]}
