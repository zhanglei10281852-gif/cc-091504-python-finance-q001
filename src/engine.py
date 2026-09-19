"""公司行动成本重建引擎。

从不可变事件日志出发，按业务真实顺序（除权日/登记日/到账日、公告版本、
撤销更正）重放账户变化，产出一份完整的核算版本状态。

设计要点：
- 重放结果只与事件内容有关，与消息到达顺序无关（见 ordering.py）；
- 同一公司行动的多版公告：取最高版本；撤销（cancelled）则整体剔除；
- 任何校验失败抛出 RebuildError，调用方不得提交版本（不留半套结果）；
- 缺失汇率、碎股残余、现金缺口等不阻断重放，而是登记为待确认事项，
  保证"每一分钱、每一股都有明确去向"。
"""
from __future__ import annotations

import copy
from collections import defaultdict
from typing import Any

from ordering import (
    CASH,
    DELIVER,
    ENTITLE,
    FREEZE,
    POSITION_CHECK,
    REINVEST,
    SPLIT,
    TRADE,
    TRANSFER,
    sort_key,
)
from util import D, ZERO, dec_str, floor_int, floor6, money, parse_date, q6

FX_LOOKBACK_DAYS = 7
TOL = D("0.000001")


class RebuildError(Exception):
    """重算失败：版本不会提交，当前核算版本保持不变。"""

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class FxBook:
    """汇率簿：按 (基础币, 报价币) 存放带日期与来源的汇率。

    查询取不晚于用汇日的最近一期（最多回溯 FX_LOOKBACK_DAYS 天），
    支持反向汇率倒数。返回值记录实际采用的日期与来源，便于审计。
    """

    def __init__(self) -> None:
        self._rates: dict[tuple[str, str], list[dict]] = defaultdict(list)

    def add(self, base: str, quote: str, day: str, rate: Any, source: str, ref: str) -> None:
        self._rates[(base, quote)].append(
            {"date": str(day), "rate": D(rate), "source": source, "ref": ref}
        )

    def lookup(self, base: str, quote: str, day: str) -> dict | None:
        if base == quote:
            return {"rate": D(1), "date": day, "source": "parity", "ref": None, "inverted": False}
        direct = self._lookup_one(base, quote, day)
        if direct is not None:
            return direct
        inv = self._lookup_one(quote, base, day)
        if inv is not None:
            return {
                "rate": D(1) / inv["rate"],
                "date": inv["date"],
                "source": inv["source"],
                "ref": inv["ref"],
                "inverted": True,
            }
        return None

    def _lookup_one(self, base: str, quote: str, day: str) -> dict | None:
        best = None
        for r in self._rates.get((base, quote), []):
            if r["date"] <= day and (best is None or r["date"] > best["date"]):
                best = r
        if best is None:
            return None
        if (parse_date(day) - parse_date(best["date"])).days > FX_LOOKBACK_DAYS:
            return None
        return {
            "rate": best["rate"],
            "date": best["date"],
            "source": best["source"],
            "ref": best["ref"],
            "inverted": False,
        }


def allocate(total: Any, weights: list[Any]) -> list:
    """把 total 按权重分摊到 6 位小数，舍入差归入权重最大的一项。"""
    total = q6(total)
    weights = [D(w) for w in weights]
    s = sum(weights, ZERO)
    if s <= 0:
        return [ZERO for _ in weights]
    raw = [q6(total * w / s) for w in weights]
    diff = total - sum(raw, ZERO)
    if diff != 0 and weights:
        idx = max(range(len(weights)), key=lambda i: weights[i])
        raw[idx] += diff
    return raw


class Replayer:
    """单次重放器：输入事件列表，输出核算版本状态（字典）。"""

    def __init__(self, events: list[dict]):
        self.events = events
        self.fx = FxBook()
        self.accounts: dict[str, dict] = {}
        self.securities: dict[str, dict] = {}
        self.calendars: dict[str, set] = defaultdict(set)
        self.elections: dict[tuple[str, str], str] = {}
        self.action_versions: dict[str, list[dict]] = defaultdict(list)
        self.warnings: list[str] = []

    # ------------------------------------------------------------------ 入口

    def run(self) -> dict:
        self._load_configs()
        actions = self._resolve_actions()
        effects = self._build_effects(actions)
        self._init_state()
        by_date: dict[str, list[dict]] = defaultdict(list)
        for eff in effects:
            by_date[eff["date"]].append(eff)
        for day in sorted(by_date):
            for eff in sorted(by_date[day], key=sort_key):
                self._apply(eff)
            self._snapshot(day)
        self._final_validations()
        return self._version_doc(actions)

    # ---------------------------------------------------------- 配置与解析

    def _load_configs(self) -> None:
        for ev in self.events:
            kind, p = ev["kind"], ev["payload"]
            if kind == "account":
                self.accounts[p["account_id"]] = {
                    "base_currency": p["base_currency"],
                    "name": p.get("name", ""),
                }
            elif kind == "security":
                self.securities[p["security_id"]] = {
                    "currency": p["currency"],
                    "market": p.get("market", ""),
                    "name": p.get("name", ""),
                }
            elif kind == "fx_rate":
                self.fx.add(p["base"], p["quote"], p["date"], p["rate"],
                            p.get("source", ""), ev["message_id"])
            elif kind == "calendar":
                self.calendars[p["market"]] |= {str(d) for d in p.get("days", [])}
            elif kind == "election":
                self.elections[(p["action_id"], p["account_id"])] = p["mode"]
            elif kind == "corporate_action":
                self.action_versions[p["action_id"]].append(
                    {**p, "_seq": ev["seq"], "_message_id": ev["message_id"]}
                )

    def _resolve_actions(self) -> dict[str, dict]:
        """同一行动按公告版本解析：最高版本生效；撤销则整体剔除。"""
        resolved: dict[str, dict] = {}
        for action_id, versions in self.action_versions.items():
            ordered = sorted(versions, key=lambda v: (int(v["version"]), v["_seq"]))
            winner = ordered[-1]
            resolved[action_id] = {
                "action_id": action_id,
                "security_id": winner["security_id"],
                "type": winner["type"],
                "status": winner["status"],
                "version_used": int(winner["version"]),
                "versions_seen": [int(v["version"]) for v in ordered],
                "ex_date": winner.get("ex_date"),
                "record_date": winner.get("record_date"),
                "pay_date": winner.get("pay_date"),
                "terms": winner.get("terms", {}),
                "announced_at": winner.get("announced_at", ""),
            }
            self._check_calendar(winner)
        return resolved

    def _check_calendar(self, action: dict) -> None:
        sec = self.securities.get(action["security_id"], {})
        market = sec.get("market")
        days = self.calendars.get(market)
        if not market or not days:
            return
        for field in ("ex_date", "record_date", "pay_date"):
            day = action.get(field)
            if day and day not in days:
                self.warnings.append(
                    f"公司行动 {action['action_id']} 的 {field}={day} 不是 {market} 交易日"
                )

    # ------------------------------------------------------------ 效果构建

    def _build_effects(self, actions: dict[str, dict]) -> list[dict]:
        effects: list[dict] = []

        def add(date: str, priority: int, key: str, seq: int, kind: str, **data: Any) -> None:
            effects.append({"date": date, "priority": priority, "key": key,
                            "seq": seq, "kind": kind, **data})

        # 持仓核对：同一 (账户, 证券, 日期) 以最新收到的为准（纠错重发是常态）
        snapshots: dict[tuple, dict] = {}
        for ev in self.events:
            if ev["kind"] == "position_snapshot":
                p = ev["payload"]
                key = (p["account_id"], p["security_id"], p["date"])
                if key not in snapshots or ev["seq"] > snapshots[key]["seq"]:
                    snapshots[key] = ev

        for ev in self.events:
            kind, p, seq, mid = ev["kind"], ev["payload"], ev["seq"], ev["message_id"]
            if kind == "trade":
                add(p["trade_date"], TRADE, p["trade_id"], seq, "trade", payload=p, message_id=mid)
            elif kind == "cash_movement":
                add(p["date"], CASH, mid, seq, "cash", payload=p, message_id=mid)
            elif kind == "transfer":
                add(p["transfer_date"], TRANSFER, p["transfer_id"], seq, "transfer",
                    payload=p, message_id=mid)
            elif kind in ("freeze", "unfreeze"):
                add(p["date"], FREEZE, mid, seq, kind, payload=p, message_id=mid)
            elif kind == "position_snapshot":
                continue  # 已在上方去重，下面统一登记

        for snap_ev in snapshots.values():
            p = snap_ev["payload"]
            add(p["date"], POSITION_CHECK, snap_ev["message_id"], snap_ev["seq"],
                "check", payload=p, message_id=snap_ev["message_id"])

        for action_id, act in actions.items():
            if act["status"] == "cancelled":
                continue
            atype = act["type"]
            terms = act["terms"]
            if atype == "split":
                add(act["ex_date"], SPLIT, action_id, 0, "split", action=act)
            elif atype == "rights_issue":
                add(act["ex_date"], ENTITLE, action_id, 0, "entitle_rights", action=act)
                add(act["pay_date"], DELIVER, action_id, 0, "deliver_rights", action=act)
                if terms.get("listing_date"):
                    add(terms["listing_date"], FREEZE, action_id, 0, "unfreeze_listing", action=act)
            elif atype == "cash_dividend":
                add(act["ex_date"], ENTITLE, action_id, 0, "entitle_dividend", action=act)
                add(act["pay_date"], DELIVER, action_id, 0, "pay_dividend", action=act)
            elif atype == "reinvestment":
                add(act["pay_date"], REINVEST, action_id, 0, "reinvest", action=act)
            else:
                raise RebuildError(f"未知公司行动类型 {atype}", {"action_id": action_id})
        return effects

    # ------------------------------------------------------------ 重放状态

    def _init_state(self) -> None:
        self.lots: dict[tuple[str, str], list[dict]] = defaultdict(list)
        self.cash: dict[tuple[str, str], dict] = defaultdict(
            lambda: {"balance": q6(ZERO), "frozen": q6(ZERO)}
        )
        self.entitlements: dict[str, dict] = {}
        self.dividend_credits: dict[tuple[str, str], dict] = {}
        self.ledger: list[dict] = []
        self.pending: list[dict] = []
        self.dispositions: list[dict] = []
        self.realized: list[dict] = []
        self.changes: dict[str, dict] = {}
        self._lot_n = 0
        self._ledger_n = 0
        self._pending_n = 0
        self._checks_passed = 0
        self._shortfall_pending: dict[tuple[str, str], str] = {}

    def _apply(self, eff: dict) -> None:
        handler = {
            "trade": self._apply_trade,
            "cash": self._apply_cash,
            "split": self._apply_split,
            "entitle_rights": self._apply_entitle_rights,
            "deliver_rights": self._apply_deliver_rights,
            "entitle_dividend": self._apply_entitle_dividend,
            "pay_dividend": self._apply_pay_dividend,
            "reinvest": self._apply_reinvest,
            "transfer": self._apply_transfer,
            "freeze": self._apply_freeze,
            "unfreeze": self._apply_unfreeze,
            "unfreeze_listing": self._apply_unfreeze_listing,
            "check": self._apply_check,
        }[eff["kind"]]
        handler(eff)

    # ------------------------------------------------------------ 基础工具

    def _err(self, message: str, details: dict | None = None) -> None:
        raise RebuildError(message, details)

    def _security(self, security_id: str) -> dict:
        sec = self.securities.get(security_id)
        if sec is None:
            self._err(f"未知证券 {security_id}", {"security_id": security_id})
        return sec

    def _account(self, account_id: str) -> dict:
        acc = self.accounts.get(account_id)
        if acc is None:
            self._err(f"未知账户 {account_id}", {"account_id": account_id})
        return acc

    def _new_entry(self, day: str, kind: str, account_id: str | None,
                   security_id: str | None, ref: dict) -> dict:
        self._ledger_n += 1
        return {
            "entry_id": f"L{self._ledger_n:06d}",
            "date": day,
            "kind": kind,
            "account_id": account_id,
            "security_id": security_id,
            "ref": ref,
            "lots_affected": [],
            "cash_movements": [],
            "fx_conversions": [],
            "dispositions": [],
            "notes": [],
        }

    def _convert(self, amount: Any, frm: str, to: str, day: str,
                 entry: dict | None, purpose: str) -> Any:
        """币种转换；缺汇率时登记 awaiting_fx 待确认并返回 None。"""
        amount = D(amount)
        if frm == to:
            return amount
        r = self.fx.lookup(frm, to, day)
        if r is None:
            self._add_pending(
                "awaiting_fx", since=day,
                account=entry.get("account_id") if entry else None,
                security=entry.get("security_id") if entry else None,
                ref=(entry or {}).get("entry_id"),
                detail={"amount": dec_str(amount), "from": frm, "to": to, "purpose": purpose},
            )
            if entry is not None:
                entry["notes"].append(f"缺少汇率 {frm}->{to}@{day}，{purpose} 待确认")
            return None
        converted = q6(amount * r["rate"])
        if entry is not None:
            entry["fx_conversions"].append({
                "amount": dec_str(amount), "from": frm, "to": to,
                "rate": dec_str(r["rate"]), "rate_date": r["date"],
                "rate_source": r["source"], "inverted": r["inverted"],
                "converted": dec_str(converted), "purpose": purpose,
            })
        return converted

    def _debit(self, entry: dict, account_id: str, ccy: str, amount: Any, reason: str) -> None:
        amount = q6(amount)
        bal = self.cash[(account_id, ccy)]
        bal["balance"] -= amount
        entry["cash_movements"].append({
            "account_id": account_id, "currency": ccy,
            "amount": dec_str(-amount), "reason": reason,
        })
        key = (account_id, ccy)
        if bal["balance"] < 0 and key not in self._shortfall_pending:
            pid = self._add_pending(
                "cash_shortfall", since=entry["date"], account=account_id,
                detail={"currency": ccy, "balance": dec_str(bal["balance"]), "reason": reason},
            )
            self._shortfall_pending[key] = pid

    def _credit(self, entry: dict, account_id: str, ccy: str, amount: Any, reason: str) -> None:
        amount = q6(amount)
        bal = self.cash[(account_id, ccy)]
        bal["balance"] += amount
        entry["cash_movements"].append({
            "account_id": account_id, "currency": ccy,
            "amount": dec_str(amount), "reason": reason,
        })
        key = (account_id, ccy)
        pid = self._shortfall_pending.pop(key, None)
        if pid is not None and bal["balance"] >= 0:
            self._resolve_pending(pid, entry["date"])

    def _add_pending(self, kind: str, since: str, account: str | None = None,
                     security: str | None = None, ref: str | None = None,
                     due: str | None = None, detail: dict | None = None) -> str:
        self._pending_n += 1
        pid = f"P{self._pending_n:04d}"
        self.pending.append({
            "pending_id": pid, "kind": kind, "account_id": account,
            "security_id": security, "since_date": since, "due_date": due,
            "resolved_date": None, "ref": ref, "detail": detail or {},
        })
        return pid

    def _resolve_pending(self, pending_id: str, day: str) -> None:
        for item in self.pending:
            if item["pending_id"] == pending_id and item["resolved_date"] is None:
                item["resolved_date"] = day

    def _disposition(self, entry: dict | None, day: str, kind: str, account_id: str,
                     security_id: str | None, **data: Any) -> None:
        rec = {"date": day, "kind": kind, "account_id": account_id,
               "security_id": security_id, **data}
        self.dispositions.append(rec)
        if entry is not None:
            entry["dispositions"].append(rec)

    def _next_lot_id(self, account_id: str, security_id: str) -> str:
        self._lot_n += 1
        return f"LOT-{self._lot_n:06d}"

    def _new_lot(self, account_id: str, security_id: str, day: str, quantity: Any,
                 cost_total: Any, ccy: str, base_cost: Any, base_ccy: str,
                 origin: dict, history_kind: str, history_detail: dict) -> dict:
        quantity = q6(quantity)
        cost_total = q6(cost_total)
        lot = {
            "lot_id": self._next_lot_id(account_id, security_id),
            "account_id": account_id,
            "security_id": security_id,
            "quantity": quantity,
            "frozen": q6(ZERO),
            "currency": ccy,
            "cost_total": cost_total,
            "cost_per_share": q6(cost_total / quantity) if quantity > 0 else ZERO,
            "base_currency": base_ccy,
            "base_cost_total": q6(base_cost) if base_cost is not None else None,
            "acquired_date": day,
            "origin": origin,
            "closed": False,
            "history": [],
        }
        lot["history"].append({
            "date": day, "kind": history_kind, "ref": origin.get("ref"),
            "detail": {"quantity": dec_str(quantity), "cost_total": dec_str(cost_total),
                       **history_detail},
        })
        self.lots[(account_id, security_id)].append(lot)
        return lot

    def _open_lots(self, account_id: str, security_id: str) -> list[dict]:
        return [l for l in self.lots.get((account_id, security_id), [])
                if not l["closed"] and l["quantity"] > 0]

    def _consume_fifo(self, account_id: str, security_id: str, quantity: Any,
                      respect_frozen: bool, purpose: str) -> list[tuple[dict, Any]]:
        """按买入日先进先出消耗批次；respect_frozen 时冻结部分不可用。"""
        need = D(quantity)
        segments: list[tuple[dict, Any]] = []
        ordered = sorted(self._open_lots(account_id, security_id),
                         key=lambda l: (l["acquired_date"], l["lot_id"]))
        for lot in ordered:
            avail = lot["quantity"] - lot["frozen"] if respect_frozen else lot["quantity"]
            if avail <= 0:
                continue
            take = min(avail, need)
            segments.append((lot, take))
            need -= take
            if need <= 0:
                break
        if need > 0:
            self._err(
                f"{purpose}：{account_id}/{security_id} 可用数量不足，缺口 {dec_str(need)}",
                {"account_id": account_id, "security_id": security_id,
                 "requested": dec_str(quantity), "shortfall": dec_str(need)},
            )
        return segments

    def _reduce_lot(self, lot: dict, take: Any, day: str, kind: str, ref: str) -> tuple:
        """从批次扣减 take 数量，返回 (证券币种成本, 基准币成本) 的对应份额。"""
        take = q6(take)
        if take == lot["quantity"]:
            cost_sec, cost_base = lot["cost_total"], lot["base_cost_total"]
            lot["quantity"] = q6(ZERO)
            lot["frozen"] = q6(ZERO)
            lot["cost_total"] = q6(ZERO)
            lot["base_cost_total"] = q6(ZERO) if cost_base is not None else None
            lot["closed"] = True
        else:
            ratio = take / lot["quantity"]
            cost_sec = q6(lot["cost_total"] * ratio)
            cost_base = q6(lot["base_cost_total"] * ratio) if lot["base_cost_total"] is not None else None
            lot["quantity"] = q6(lot["quantity"] - take)
            lot["cost_total"] = q6(lot["cost_total"] - cost_sec)
            if cost_base is not None:
                lot["base_cost_total"] = q6(lot["base_cost_total"] - cost_base)
            lot["cost_per_share"] = q6(lot["cost_total"] / lot["quantity"]) if lot["quantity"] > 0 else ZERO
        lot["history"].append({
            "date": day, "kind": kind, "ref": ref,
            "detail": {"quantity_delta": dec_str(-take),
                       "quantity_after": dec_str(lot["quantity"]),
                       "cost_delta": dec_str(-cost_sec),
                       "cost_after": dec_str(lot["cost_total"])},
        })
        return cost_sec, cost_base

    # ------------------------------------------------------------ 各类效果

    def _apply_cash(self, eff: dict) -> None:
        p = eff["payload"]
        self._account(p["account_id"])
        entry = self._new_entry(eff["date"], "cash_movement", p["account_id"], None,
                                {"message_id": eff["message_id"]})
        amount = D(p["amount"])
        note = p.get("note", "")
        if amount >= 0:
            self._credit(entry, p["account_id"], p["currency"], amount, note or "存入")
        else:
            self._debit(entry, p["account_id"], p["currency"], -amount, note or "取出")
        self.ledger.append(entry)

    def _apply_trade(self, eff: dict) -> None:
        p = eff["payload"]
        if p["side"] == "buy":
            self._trade_buy(eff, p)
        else:
            self._trade_sell(eff, p)

    def _trade_buy(self, eff: dict, p: dict) -> None:
        day = eff["date"]
        sec = self._security(p["security_id"])
        acc = self._account(p["account_id"])
        aid, sid = p["account_id"], p["security_id"]
        ccy = p["currency"]
        if ccy != sec["currency"]:
            self._err(f"成交币种 {ccy} 与证券币种 {sec['currency']} 不一致",
                      {"trade_id": p["trade_id"]})
        qty, price = D(p["quantity"]), D(p["price"])
        gross = q6(qty * price)
        entry = self._new_entry(day, "trade_buy", aid, sid,
                                {"message_id": eff["message_id"], "trade_id": p["trade_id"]})
        fee_sec_total = ZERO
        for fee in p.get("fees", []):
            famt, fccy = q6(D(fee["amount"])), fee["currency"]
            self._debit(entry, aid, fccy, famt, f"费用:{fee.get('kind', 'fee')}")
            self._disposition(entry, day, "fee", aid, sid,
                              amount=dec_str(famt), currency=fccy,
                              fee_kind=fee.get("kind", "fee"), ref=p["trade_id"])
            fee_sec = self._convert(famt, fccy, ccy, day, entry, "费用资本化")
            if fee_sec is not None:
                fee_sec_total += fee_sec
            else:
                entry["notes"].append(f"费用 {dec_str(famt)} {fccy} 因缺汇率暂未资本化")
        cost_sec = q6(gross + fee_sec_total)
        self._debit(entry, aid, ccy, gross, "买入成交")
        base_cost = self._convert(cost_sec, ccy, acc["base_currency"], day, entry, "成本基准折算")
        lot = self._new_lot(aid, sid, day, qty, cost_sec, ccy, base_cost,
                            acc["base_currency"],
                            {"kind": "trade", "ref": p["trade_id"]}, "acquire",
                            {"trade_id": p["trade_id"], "price": dec_str(price)})
        entry["lots_affected"].append({
            "lot_id": lot["lot_id"], "quantity_delta": dec_str(qty),
            "cost_delta": dec_str(cost_sec),
            "base_cost_delta": dec_str(base_cost) if base_cost is not None else None,
        })
        self.ledger.append(entry)

    def _trade_sell(self, eff: dict, p: dict) -> None:
        day = eff["date"]
        sec = self._security(p["security_id"])
        acc = self._account(p["account_id"])
        aid, sid = p["account_id"], p["security_id"]
        ccy = p["currency"]
        qty, price = D(p["quantity"]), D(p["price"])
        proceeds = q6(qty * price)
        entry = self._new_entry(day, "trade_sell", aid, sid,
                                {"message_id": eff["message_id"], "trade_id": p["trade_id"]})
        fee_sec_total = ZERO
        for fee in p.get("fees", []):
            famt, fccy = q6(D(fee["amount"])), fee["currency"]
            self._debit(entry, aid, fccy, famt, f"费用:{fee.get('kind', 'fee')}")
            self._disposition(entry, day, "fee", aid, sid,
                              amount=dec_str(famt), currency=fccy,
                              fee_kind=fee.get("kind", "fee"), ref=p["trade_id"])
            fee_sec = self._convert(famt, fccy, ccy, day, entry, "费用折算")
            if fee_sec is not None:
                fee_sec_total += fee_sec
        net_sec = q6(proceeds - fee_sec_total)
        segments = self._consume_fifo(aid, sid, qty, respect_frozen=True, purpose="卖出")
        cost_sec_removed, cost_base_removed = ZERO, ZERO
        base_known = True
        for lot, take in segments:
            c_sec, c_base = self._reduce_lot(lot, take, day, "sell", p["trade_id"])
            cost_sec_removed += c_sec
            if c_base is None:
                base_known = False
            else:
                cost_base_removed += c_base
            entry["lots_affected"].append({
                "lot_id": lot["lot_id"], "quantity_delta": dec_str(-take),
                "cost_delta": dec_str(-c_sec),
                "base_cost_delta": dec_str(-c_base) if c_base is not None else None,
            })
        self._credit(entry, aid, ccy, proceeds, "卖出成交")
        base_ccy = acc["base_currency"]
        sale = self.fx.lookup(ccy, base_ccy, day)
        realized = {
            "date": day, "account_id": aid, "security_id": sid,
            "quantity": dec_str(qty), "currency": ccy,
            "proceeds_net": dec_str(net_sec),
            "cost": dec_str(cost_sec_removed),
            "realized_pnl": dec_str(q6(net_sec - cost_sec_removed)),
            "base_currency": base_ccy,
            "ref": p["trade_id"],
        }
        if sale is None or not base_known:
            realized.update({"proceeds_net_base": None, "cost_base": None,
                             "realized_trading_base": None, "realized_fx_base": None,
                             "realized_total_base": None})
            if sale is None:
                self._convert(cost_sec_removed, ccy, base_ccy, day, entry, "卖出损益折算")
        else:
            rate = sale["rate"]
            proceeds_base = q6(net_sec * rate)
            trading_base = q6((net_sec - cost_sec_removed) * rate)
            fx_base = q6(cost_sec_removed * rate - cost_base_removed)
            realized.update({
                "proceeds_net_base": dec_str(proceeds_base),
                "cost_base": dec_str(cost_base_removed),
                "realized_trading_base": dec_str(trading_base),
                "realized_fx_base": dec_str(fx_base),
                "realized_total_base": dec_str(q6(trading_base + fx_base)),
                "fx": {"sale_rate": dec_str(rate), "sale_rate_date": sale["date"],
                       "avg_acquisition_rate": dec_str(cost_base_removed / cost_sec_removed)
                       if cost_sec_removed > 0 else None},
            })
            entry["fx_conversions"].append({
                "amount": dec_str(net_sec), "from": ccy, "to": base_ccy,
                "rate": dec_str(rate), "rate_date": sale["date"],
                "rate_source": sale["source"], "inverted": sale["inverted"],
                "converted": dec_str(proceeds_base), "purpose": "卖出损益折算",
            })
        self.realized.append(realized)
        self.ledger.append(entry)

    def _apply_split(self, eff: dict) -> None:
        act = eff["action"]
        day = eff["date"]
        sid = act["security_id"]
        terms = act["terms"]
        factor = D(terms["to"]) / D(terms["from"])
        cil_price = terms.get("cash_in_lieu_price")
        for aid in sorted(self.accounts):
            lots = self._open_lots(aid, sid)
            if not lots:
                continue
            entry = self._new_entry(day, "split", aid, sid,
                                    {"action_id": act["action_id"],
                                     "version": act["version_used"]})
            cost_before = sum((l["cost_total"] for l in lots), ZERO)
            for lot in lots:
                qty_before = lot["quantity"]
                exact = qty_before * factor
                if cil_price:
                    whole = D(floor_int(exact))
                    frac = q6(exact - whole)
                    new_qty = q6(whole)
                    if frac > 0:
                        cash_lieu = q6(frac * D(cil_price))
                        self._credit(entry, aid, lot["currency"], cash_lieu, "碎股现金替代")
                        self._disposition(entry, day, "fraction_cash_in_lieu", aid, sid,
                                          quantity=dec_str(frac), amount=dec_str(cash_lieu),
                                          currency=lot["currency"],
                                          ref=act["action_id"])
                else:
                    new_qty = q6(exact)
                new_frozen = min(q6(lot["frozen"] * factor), new_qty)
                lot["quantity"] = new_qty
                lot["frozen"] = new_frozen
                lot["cost_per_share"] = q6(lot["cost_total"] / new_qty) if new_qty > 0 else ZERO
                lot["history"].append({
                    "date": day, "kind": "split", "ref": act["action_id"],
                    "detail": {"version": act["version_used"],
                               "ratio": f"{terms['from']}:{terms['to']}",
                               "quantity_before": dec_str(qty_before),
                               "quantity_after": dec_str(new_qty),
                               "cost_before": dec_str(lot["cost_total"]),
                               "cost_after": dec_str(lot["cost_total"])},
                })
                entry["lots_affected"].append({
                    "lot_id": lot["lot_id"],
                    "quantity_delta": dec_str(new_qty - qty_before),
                    "cost_delta": "0", "base_cost_delta": "0",
                })
            cost_after = sum((l["cost_total"] for l in lots), ZERO)
            if abs(cost_after - cost_before) > TOL:
                self._err("拆股前后成本不守恒",
                          {"action_id": act["action_id"], "before": dec_str(cost_before),
                           "after": dec_str(cost_after)})
            self.ledger.append(entry)

    # ------------------------------------------------------------ 确权

    def _eligible_lots(self, account_id: str, security_id: str, ex_date: str) -> list[dict]:
        """资格 = 除权日重放时点持有且买入日早于除权日的批次。"""
        return [l for l in self._open_lots(account_id, security_id)
                if l["acquired_date"] < ex_date]

    def _apply_entitle_rights(self, eff: dict) -> None:
        act = eff["action"]
        day = eff["date"]
        sid = act["security_id"]
        terms = act["terms"]
        ratio = D(terms["ratio_rights"]) / D(terms["ratio_base"])
        for aid in sorted(self.accounts):
            per_lot = []
            for lot in self._eligible_lots(aid, sid, day):
                raw = q6(lot["quantity"] * ratio)
                shares = floor_int(raw)
                per_lot.append({
                    "lot_id": lot["lot_id"],
                    "eligible_qty": dec_str(lot["quantity"]),
                    "raw_entitlement": dec_str(raw),
                    "shares": shares,
                    "fraction": dec_str(q6(raw - shares)),
                })
            if not per_lot:
                continue
            ent_id = f"ENT-{act['action_id']}-{aid}"
            self.entitlements[ent_id] = {
                "entitlement_id": ent_id, "action_id": act["action_id"],
                "account_id": aid, "security_id": sid, "kind": "rights_issue",
                "per_lot": per_lot, "status": "pending",
                "ex_date": day, "pay_date": act["pay_date"],
                "version": act["version_used"],
            }
            self._add_pending("awaiting_payment", since=day, account=aid, security=sid,
                              ref=ent_id, due=act["pay_date"],
                              detail={"action_id": act["action_id"], "kind": "rights_issue"})
            entry = self._new_entry(day, "rights_entitle", aid, sid,
                                    {"action_id": act["action_id"],
                                     "version": act["version_used"]})
            entry["notes"].append(
                f"配股确权：{sum(x['shares'] for x in per_lot)} 股，"
                f"零碎 {dec_str(sum(D(x['fraction']) for x in per_lot))} 股"
            )
            self.ledger.append(entry)

    def _apply_entitle_dividend(self, eff: dict) -> None:
        act = eff["action"]
        day = eff["date"]
        sid = act["security_id"]
        for aid in sorted(self.accounts):
            per_lot = [{"lot_id": l["lot_id"], "eligible_qty": dec_str(l["quantity"])}
                       for l in self._eligible_lots(aid, sid, day)]
            if not per_lot:
                continue
            total = sum((D(x["eligible_qty"]) for x in per_lot), ZERO)
            ent_id = f"ENT-{act['action_id']}-{aid}"
            self.entitlements[ent_id] = {
                "entitlement_id": ent_id, "action_id": act["action_id"],
                "account_id": aid, "security_id": sid, "kind": "cash_dividend",
                "per_lot": per_lot, "total_eligible": dec_str(total), "status": "pending",
                "ex_date": day, "pay_date": act["pay_date"],
                "version": act["version_used"],
            }
            self._add_pending("awaiting_payment", since=day, account=aid, security=sid,
                              ref=ent_id, due=act["pay_date"],
                              detail={"action_id": act["action_id"], "kind": "cash_dividend"})
            entry = self._new_entry(day, "dividend_entitle", aid, sid,
                                    {"action_id": act["action_id"],
                                     "version": act["version_used"]})
            entry["notes"].append(f"红利确权：合资格 {dec_str(total)} 股")
            self.ledger.append(entry)

    # ------------------------------------------------------------ 交付

    def _apply_deliver_rights(self, eff: dict) -> None:
        act = eff["action"]
        day = eff["date"]
        sid = act["security_id"]
        terms = act["terms"]
        action_id = act["action_id"]
        for ent_id in sorted(self.entitlements):
            ent = self.entitlements[ent_id]
            if ent["action_id"] != action_id or ent["status"] != "pending":
                continue
            aid = ent["account_id"]
            acc = self._account(aid)
            sec = self._security(sid)
            entry = self._new_entry(day, "rights_deliver", aid, sid,
                                    {"action_id": action_id, "version": act["version_used"]})
            mode = self.elections.get((action_id, aid), terms.get("default_mode", "shares"))
            total_shares = sum(x["shares"] for x in ent["per_lot"])
            total_fraction = sum((D(x["fraction"]) for x in ent["per_lot"]), ZERO)
            if mode == "cash":
                price = terms.get("cash_option_price")
                if price is None:
                    self._err("现金选择权缺少 cash_option_price", {"action_id": action_id})
                amount = q6(D(total_shares) * D(price))
                ccy = terms.get("currency", sec["currency"])
                self._credit(entry, aid, ccy, amount, "配股现金选择权")
                self._disposition(entry, day, "cash_election", aid, sid,
                                  shares=total_shares, price=dec_str(D(price)),
                                  amount=dec_str(amount), currency=ccy, ref=action_id)
            else:
                self._deliver_rights_shares(act, ent, entry, terms, sec, acc, day)
            if total_fraction > 0:
                cil = terms.get("cash_in_lieu_price")
                if cil:
                    amount = q6(total_fraction * D(cil))
                    ccy = terms.get("currency", sec["currency"])
                    self._credit(entry, aid, ccy, amount, "配股碎股现金替代")
                    self._disposition(entry, day, "fraction_cash_in_lieu", aid, sid,
                                      quantity=dec_str(total_fraction),
                                      amount=dec_str(amount), currency=ccy, ref=action_id)
                else:
                    self._disposition(entry, day, "fraction_residual", aid, sid,
                                      quantity=dec_str(total_fraction), ref=action_id)
                    self._add_pending("fractional_residual", since=day, account=aid,
                                      security=sid, ref=action_id,
                                      detail={"quantity": dec_str(total_fraction),
                                              "note": "不足一股残余，等待发行人处理"})
            ent["status"] = "settled"
            ent["settled_date"] = day
            for item in self.pending:
                if item["ref"] == ent_id and item["resolved_date"] is None:
                    item["resolved_date"] = day
            self.ledger.append(entry)

    def _deliver_rights_shares(self, act: dict, ent: dict, entry: dict,
                               terms: dict, sec: dict, acc: dict, day: str) -> None:
        aid = ent["account_id"]
        sid = act["security_id"]
        action_id = act["action_id"]
        sub_ccy = terms.get("currency", sec["currency"])
        price = D(terms["subscription_price"])
        per_lot = ent["per_lot"]
        shares_list = [D(x["shares"]) for x in per_lot]
        fee_sub = ZERO
        fee = terms.get("fee")
        if fee:
            famt, fccy = q6(D(fee["amount"])), fee["currency"]
            self._debit(entry, aid, fccy, famt, "配股费用")
            self._disposition(entry, day, "fee", aid, sid,
                              amount=dec_str(famt), currency=fccy,
                              fee_kind="rights_fee", ref=action_id)
            converted = self._convert(famt, fccy, sub_ccy, day, entry, "配股费用折算")
            if converted is not None:
                fee_sub = converted
            else:
                entry["notes"].append("配股费用因缺汇率暂未计入成本")
        fee_allocs = allocate(fee_sub, shares_list)
        total_cost = ZERO
        for item, shares, fee_alloc in zip(per_lot, shares_list, fee_allocs):
            if shares <= 0:
                continue
            cost = q6(q6(shares * price) + fee_alloc)
            total_cost += cost
            base_cost = self._convert(cost, sub_ccy, acc["base_currency"], day, entry,
                                      "配股成本基准折算")
            lot = self._new_lot(aid, sid, day, shares, cost, sub_ccy, base_cost,
                                acc["base_currency"],
                                {"kind": "rights_issue", "ref": action_id,
                                 "source_lot": item["lot_id"]},
                                "rights_deliver",
                                {"action_id": action_id, "version": act["version_used"],
                                 "subscription_price": dec_str(price),
                                 "source_lot": item["lot_id"],
                                 "fee_allocated": dec_str(fee_alloc)})
            if terms.get("listing_date") and terms["listing_date"] > day:
                lot["frozen"] = lot["quantity"]
                lot["history"].append({
                    "date": day, "kind": "freeze", "ref": action_id,
                    "detail": {"quantity": dec_str(lot["quantity"]),
                               "reason": "awaiting_listing",
                               "until": terms["listing_date"]},
                })
            entry["lots_affected"].append({
                "lot_id": lot["lot_id"], "quantity_delta": dec_str(shares),
                "cost_delta": dec_str(cost),
                "base_cost_delta": dec_str(base_cost) if base_cost is not None else None,
            })
        sub_total = q6(sum(shares_list, ZERO) * price)
        self._debit(entry, aid, sub_ccy, sub_total, "配股缴款")

    def _apply_pay_dividend(self, eff: dict) -> None:
        act = eff["action"]
        day = eff["date"]
        sid = act["security_id"]
        terms = act["terms"]
        action_id = act["action_id"]
        sec = self._security(sid)
        div_ccy = terms.get("currency", sec["currency"])
        dps = D(terms["amount_per_share"])
        tax_rate = D(terms.get("tax_rate", "0"))
        for ent_id in sorted(self.entitlements):
            ent = self.entitlements[ent_id]
            if ent["action_id"] != action_id or ent["status"] != "pending":
                continue
            aid = ent["account_id"]
            acc = self._account(aid)
            entry = self._new_entry(day, "dividend_pay", aid, sid,
                                    {"action_id": action_id, "version": act["version_used"]})
            gross = money(D(ent["total_eligible"]) * dps)
            tax = money(gross * tax_rate) if tax_rate > 0 else ZERO
            if tax > 0:
                self._disposition(entry, day, "tax", aid, sid,
                                  amount=dec_str(tax), currency=div_ccy, ref=action_id)
            fee_div = ZERO
            fee = terms.get("fee")
            if fee:
                famt, fccy = money(D(fee["amount"])), fee["currency"]
                self._disposition(entry, day, "fee", aid, sid,
                                  amount=dec_str(famt), currency=fccy,
                                  fee_kind="dividend_fee", ref=action_id)
                if fccy == div_ccy:
                    # 与红利同币种：直接从红利中净扣，不再单独动账
                    fee_div = famt
                else:
                    # 跨币种费用：在其自身币种单独扣款，保留明确去向
                    self._debit(entry, aid, fccy, famt, "红利费用")
            net = money(gross - tax - fee_div)
            self._credit(entry, aid, div_ccy, net, "现金红利")
            self._convert(gross, div_ccy, acc["base_currency"], day, entry, "红利基准折算(备查)")
            self.dividend_credits[(action_id, aid)] = {
                "amount": net, "currency": div_ccy, "gross": gross,
                "account_id": aid, "action_id": action_id, "date": day,
            }
            ent["status"] = "settled"
            ent["settled_date"] = day
            for item in self.pending:
                if item["ref"] == ent_id and item["resolved_date"] is None:
                    item["resolved_date"] = day
            self.ledger.append(entry)

    def _apply_reinvest(self, eff: dict) -> None:
        act = eff["action"]
        day = eff["date"]
        sid = act["security_id"]
        terms = act["terms"]
        action_id = act["action_id"]
        sec = self._security(sid)
        div_action = terms["dividend_action_id"]
        price = D(terms["price"])
        pccy = terms.get("currency", sec["currency"])
        if pccy != sec["currency"]:
            self._err("再投资币种须与证券币种一致", {"action_id": action_id})
        for key in sorted(list(self.dividend_credits)):
            daction, aid = key
            if daction != div_action:
                continue
            if self.elections.get((action_id, aid)) == "cash":
                continue
            credit = self.dividend_credits.pop(key)
            acc = self._account(aid)
            entry = self._new_entry(day, "reinvest", aid, sid,
                                    {"action_id": action_id, "version": act["version_used"],
                                     "dividend_action_id": div_action})
            avail, cccy = credit["amount"], credit["currency"]
            rate = D(1)
            avail_p = avail
            if cccy != pccy:
                r = self.fx.lookup(cccy, pccy, day)
                if r is None:
                    self._convert(avail, cccy, pccy, day, entry, "再投资折算")
                    self.dividend_credits[key] = credit
                    self.ledger.append(entry)
                    continue
                rate = r["rate"]
                avail_p = q6(avail * rate)
                entry["fx_conversions"].append({
                    "amount": dec_str(avail), "from": cccy, "to": pccy,
                    "rate": dec_str(rate), "rate_date": r["date"],
                    "rate_source": r["source"], "inverted": r["inverted"],
                    "converted": dec_str(avail_p), "purpose": "再投资折算",
                })
            shares = floor6(avail_p / price)
            if shares <= 0:
                self._disposition(entry, day, "reinvest_residual_cash", aid, sid,
                                  amount=dec_str(avail), currency=cccy, ref=action_id)
                self.ledger.append(entry)
                continue
            used_p = q6(shares * price)
            used_c = q6(used_p / rate)
            residual = avail - used_c
            if residual < 0:
                used_c, residual = avail, ZERO
            self._debit(entry, aid, cccy, used_c, "红利再投资")
            if residual > 0:
                self._disposition(entry, day, "reinvest_residual_cash", aid, sid,
                                  amount=dec_str(residual), currency=cccy, ref=action_id)
            base_cost = self._convert(used_p, pccy, acc["base_currency"], day, entry,
                                      "再投资成本基准折算")
            lot = self._new_lot(aid, sid, day, shares, used_p, pccy, base_cost,
                                acc["base_currency"],
                                {"kind": "reinvestment", "ref": action_id,
                                 "dividend_action_id": div_action},
                                "reinvest",
                                {"action_id": action_id, "price": dec_str(price),
                                 "dividend_action_id": div_action})
            entry["lots_affected"].append({
                "lot_id": lot["lot_id"], "quantity_delta": dec_str(shares),
                "cost_delta": dec_str(used_p),
                "base_cost_delta": dec_str(base_cost) if base_cost is not None else None,
            })
            self.ledger.append(entry)

    # ------------------------------------------------------------ 转入转出

    def _apply_transfer(self, eff: dict) -> None:
        p = eff["payload"]
        day = eff["date"]
        sid = p["security_id"]
        sec = self._security(sid)
        qty = D(p["quantity"])
        src, dst = p["from_account"], p["to_account"]
        src_managed, dst_managed = src in self.accounts, dst in self.accounts
        if not src_managed and not dst_managed:
            self._err("转入转出双方均为外部账户", {"transfer_id": p["transfer_id"]})
        if dst_managed and not src_managed:
            self._transfer_in_external(eff, p, sec, day)
            return
        entry = self._new_entry(day, "transfer", dst if dst_managed else src, sid,
                                {"message_id": eff["message_id"],
                                 "transfer_id": p["transfer_id"]})
        segments = self._consume_fifo(src, sid, qty, respect_frozen=True, purpose="转出")
        for lot, take in segments:
            cost_sec, cost_base = self._reduce_lot(lot, take, day, "transfer_out",
                                                   p["transfer_id"])
            entry["lots_affected"].append({
                "lot_id": lot["lot_id"], "quantity_delta": dec_str(-take),
                "cost_delta": dec_str(-cost_sec),
                "base_cost_delta": dec_str(-cost_base) if cost_base is not None else None,
            })
            if dst_managed:
                acc = self.accounts[dst]
                new_lot = self._new_lot(
                    dst, sid, day, take, cost_sec, lot["currency"], cost_base,
                    acc["base_currency"],
                    {"kind": "transfer_in", "ref": p["transfer_id"],
                     "from_account": src, "source_lot": lot["lot_id"]},
                    "transfer_in",
                    {"transfer_id": p["transfer_id"], "from_account": src,
                     "source_lot": lot["lot_id"],
                     "original_acquired_date": lot["acquired_date"]})
                new_lot["acquired_date"] = lot["acquired_date"]
                entry["lots_affected"].append({
                    "lot_id": new_lot["lot_id"], "quantity_delta": dec_str(take),
                    "cost_delta": dec_str(cost_sec),
                    "base_cost_delta": dec_str(cost_base) if cost_base is not None else None,
                })
            else:
                self._disposition(entry, day, "transfer_out_external", src, sid,
                                  quantity=dec_str(take), cost=dec_str(cost_sec),
                                  currency=lot["currency"], to_account=dst,
                                  ref=p["transfer_id"])
        self.ledger.append(entry)

    def _transfer_in_external(self, eff: dict, p: dict, sec: dict, day: str) -> None:
        """外部跨账户转入：必须自带原始成本与取得日，保持成本沿革连续。"""
        sid = p["security_id"]
        dst = p["to_account"]
        acc = self.accounts[dst]
        basis = p.get("cost_basis")
        if not basis:
            self._err("外部转入必须提供 cost_basis（原始成本与取得日）",
                      {"transfer_id": p["transfer_id"]})
        qty = D(p["quantity"])
        cost_ps = D(basis["cost_per_share"])
        cost_total = q6(cost_ps * qty)
        entry = self._new_entry(day, "transfer_in", dst, sid,
                                {"message_id": eff["message_id"],
                                 "transfer_id": p["transfer_id"]})
        if basis.get("base_cost_per_share") is not None:
            base_total = q6(D(basis["base_cost_per_share"]) * qty)
            entry["notes"].append("基准币成本采用随转入提供的原始值")
        else:
            base_total = self._convert(cost_total, sec["currency"], acc["base_currency"],
                                       day, entry, "转入成本基准折算")
        lot = self._new_lot(dst, sid, day, qty, cost_total, sec["currency"], base_total,
                            acc["base_currency"],
                            {"kind": "transfer_in", "ref": p["transfer_id"],
                             "from_account": p["from_account"], "external": True},
                            "transfer_in",
                            {"transfer_id": p["transfer_id"],
                             "from_account": p["from_account"],
                             "original_acquired_date": basis["acquired_date"]})
        lot["acquired_date"] = basis["acquired_date"]
        entry["lots_affected"].append({
            "lot_id": lot["lot_id"], "quantity_delta": dec_str(qty),
            "cost_delta": dec_str(cost_total),
            "base_cost_delta": dec_str(base_total) if base_total is not None else None,
        })
        self.ledger.append(entry)

    # ------------------------------------------------------------ 冻结

    def _apply_freeze(self, eff: dict) -> None:
        self._freeze_impl(eff, direction=1)

    def _apply_unfreeze(self, eff: dict) -> None:
        self._freeze_impl(eff, direction=-1)

    def _freeze_impl(self, eff: dict, direction: int) -> None:
        p = eff["payload"]
        day = eff["date"]
        aid, sid = p["account_id"], p["security_id"]
        self._account(aid)
        need = D(p["quantity"])
        kind = "freeze" if direction > 0 else "unfreeze"
        entry = self._new_entry(day, kind, aid, sid, {"message_id": eff["message_id"]})
        ordered = sorted(self._open_lots(aid, sid),
                         key=lambda l: (l["acquired_date"], l["lot_id"]))
        for lot in ordered:
            avail = (lot["quantity"] - lot["frozen"]) if direction > 0 else lot["frozen"]
            if avail <= 0:
                continue
            take = min(avail, need)
            lot["frozen"] = q6(lot["frozen"] + direction * take)
            lot["history"].append({
                "date": day, "kind": kind, "ref": eff["message_id"],
                "detail": {"quantity": dec_str(take),
                           "frozen_after": dec_str(lot["frozen"]),
                           "reason": p.get("reason", "")},
            })
            need -= take
            if need <= 0:
                break
        if need > 0:
            self._err(f"{'冻结' if direction > 0 else '解冻'}数量不足，缺口 {dec_str(need)}",
                      {"account_id": aid, "security_id": sid})
        entry["notes"].append(f"{'冻结' if direction > 0 else '解冻'} {dec_str(D(p['quantity']))} 股")
        self.ledger.append(entry)

    def _apply_unfreeze_listing(self, eff: dict) -> None:
        act = eff["action"]
        day = eff["date"]
        sid = act["security_id"]
        action_id = act["action_id"]
        for aid in sorted(self.accounts):
            for lot in self._open_lots(aid, sid):
                if lot["origin"].get("kind") == "rights_issue" \
                        and lot["origin"].get("ref") == action_id and lot["frozen"] > 0:
                    entry = self._new_entry(day, "unfreeze", aid, sid,
                                            {"action_id": action_id})
                    qty = lot["frozen"]
                    lot["frozen"] = q6(ZERO)
                    lot["history"].append({
                        "date": day, "kind": "unfreeze", "ref": action_id,
                        "detail": {"quantity": dec_str(qty), "reason": "listing"},
                    })
                    entry["notes"].append(f"配股上市解冻 {dec_str(qty)} 股")
                    self.ledger.append(entry)

    # ------------------------------------------------------------ 核对

    def _apply_check(self, eff: dict) -> None:
        p = eff["payload"]
        aid, sid = p["account_id"], p["security_id"]
        actual = sum((l["quantity"] for l in self._open_lots(aid, sid)), ZERO)
        expected = D(p["quantity"])
        if actual != expected:
            self._err(
                f"持仓核对失败 {aid}/{sid}@{eff['date']}：期望 {dec_str(expected)}，"
                f"实际 {dec_str(actual)}",
                {"account_id": aid, "security_id": sid, "date": eff["date"],
                 "expected": dec_str(expected), "actual": dec_str(actual)},
            )
        self._checks_passed += 1

    # ------------------------------------------------------------ 收尾

    def _snapshot(self, day: str) -> None:
        lots_view: dict[str, dict[str, list]] = defaultdict(dict)
        for (aid, sid), lots in self.lots.items():
            lots_view[aid][sid] = copy.deepcopy(lots)
        cash_view: dict[str, dict[str, dict]] = defaultdict(dict)
        for (aid, ccy), bal in self.cash.items():
            cash_view[aid][ccy] = dict(bal)
        self.changes[day] = {"lots": dict(lots_view), "cash": dict(cash_view)}

    def _final_validations(self) -> None:
        for (aid, sid), lots in self.lots.items():
            for lot in lots:
                if lot["quantity"] < -TOL:
                    self._err("批次数量为负", {"lot_id": lot["lot_id"]})
                if lot["frozen"] < -TOL or lot["frozen"] - lot["quantity"] > TOL:
                    self._err("冻结数量超出批次数量", {"lot_id": lot["lot_id"]})

    def _version_doc(self, actions: dict[str, dict]) -> dict:
        ca_summary = {}
        for action_id, act in sorted(actions.items()):
            ca_summary[action_id] = {
                "action_id": action_id,
                "security_id": act["security_id"],
                "type": act["type"],
                "status": act["status"],
                "version_used": act["version_used"],
                "versions_seen": act["versions_seen"],
                "ex_date": act["ex_date"],
                "record_date": act["record_date"],
                "pay_date": act["pay_date"],
            }
            if act["status"] == "preliminary":
                self._add_pending("unconfirmed_terms", since=act["ex_date"] or "",
                                  security=act["security_id"], ref=action_id,
                                  detail={"note": "公告条款仍为初步状态，等待正式版本"})
        return {
            "accounts": self.accounts,
            "securities": self.securities,
            "changes": self.changes,
            "ledger": self.ledger,
            "entitlements": sorted(self.entitlements.values(),
                                   key=lambda e: e["entitlement_id"]),
            "pending": self.pending,
            "dispositions": self.dispositions,
            "realized": self.realized,
            "corporate_actions": ca_summary,
            "warnings": sorted(self.warnings),
            "validations": [
                "lots_non_negative",
                "frozen_within_quantity",
                f"position_checks_passed:{self._checks_passed}",
                f"events_replayed:{len(self.events)}",
            ],
        }
