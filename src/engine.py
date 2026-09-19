"""核算引擎：从事件日志全量重放，生成一个不可变的核算版本。

设计要点：
- 事件溯源：版本状态完全由事件日志推导，迟到事件只产生新版本；
- 真实业务顺序：按 (生效日, 阶段, 业务标识, 公告版本, 接收序号) 排序，
  与消息到达顺序无关；
- 公司行动按 action_id 归并，最高公告版本生效，旧版本记为 superseded，
  撤销记为 cancelled，均保留审计痕迹；
- 任何换算（跨币种费用、红利币种）都记录 fx_usage（汇率、日期、版本），
  使汇兑损益可解释；
- 重放在内存中一次完成，失败时不产生任何版本文件（由 store 保证原子落盘）。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from models import (
    ACTION_TYPES,
    D,
    Lot,
    dec_str,
    floor_int,
    hash_obj,
    q_money,
    q_qty,
)
from ordering import adjust_business_day, sort_steps


class EngineError(Exception):
    """重放过程中的业务错误（如超卖、缺汇率）。整批重放失败，不留半套结果。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _Replay:
    def __init__(self, holidays: set[str]):
        self.holidays = holidays
        self.lots: dict[str, Lot] = {}
        self.open_lots: dict[tuple, list[str]] = defaultdict(list)  # (account, security) -> FIFO
        self.cash: dict[tuple, Decimal] = defaultdict(lambda: D("0"))
        self.fx: dict[tuple, tuple] = {}          # (pair, rate_date) -> (rate, source_version)
        self.elections: dict[tuple, dict] = {}    # (account, action_id) -> payload
        self.entitlements: dict[tuple, dict] = {} # (account, action_id) -> 权益暂存
        self.freezes: dict[str, list] = {}        # transfer_id -> [(lot_id, qty)]
        self.realized: list[dict] = []
        self.fx_usages: list[dict] = []
        self.cash_movements: list[dict] = []
        self.notes: list[dict] = []
        self.pending: list[dict] = []
        self.daily_positions: dict[str, dict] = {}
        self.daily_cash: dict[str, dict] = {}
        self.applied: list[dict] = []
        self._lot_seq = 0

    # ------------------------------------------------------------------ utils
    def _new_lot(self, *, account, security, quantity, cost_total, currency,
                 date, origin, derived_from=(), root_id=None) -> Lot:
        self._lot_seq += 1
        lot = Lot(
            lot_id=f"L{self._lot_seq:05d}",
            account=account,
            security=security,
            quantity=q_qty(quantity),
            cost_total=q_money(cost_total),
            currency=currency,
            opened_date=date,
            origin=origin,
            root_id=root_id or f"L{self._lot_seq:05d}",
            derived_from=list(derived_from),
        )
        self.lots[lot.lot_id] = lot
        self.open_lots[(account, security)].append(lot.lot_id)
        return lot

    def _fifo_lots(self, account: str, security: str) -> list[Lot]:
        ids = self.open_lots.get((account, security), [])
        return [self.lots[i] for i in ids if not self.lots[i].closed]

    def _held(self, account: str, security: str) -> Decimal:
        return sum((l.quantity for l in self._fifo_lots(account, security)), D("0"))

    def _move_cash(self, *, date, account, currency, amount, reason, message_id,
                   action_id=None, note=""):
        amount = q_money(amount)
        self.cash[(account, currency)] += amount
        self.cash_movements.append({
            "date": date, "account": account, "currency": currency,
            "amount": dec_str(amount), "reason": reason,
            "message_id": message_id, "action_id": action_id, "note": note,
        })

    def _convert(self, *, amount, from_ccy, to_ccy, date, message_id, purpose,
                 account=None, security=None, action_id=None) -> Decimal:
        """跨币种换算；记录所用汇率与版本，汇兑损益由此可解释。"""
        amount = D(amount)
        if from_ccy == to_ccy or amount == 0:
            return q_money(amount)
        direct = (f"{from_ccy}/{to_ccy}", date)
        inverse = (f"{to_ccy}/{from_ccy}", date)
        if direct in self.fx:
            rate, ver = self.fx[direct]
            out = q_money(amount * D(rate))
            usage = {"pair": direct[0], "rate": rate, "rate_date": date, "direction": "direct"}
        elif inverse in self.fx:
            rate, ver = self.fx[inverse]
            out = q_money(amount / D(rate))
            usage = {"pair": inverse[0], "rate": rate, "rate_date": date, "direction": "inverse"}
        else:
            # 允许使用此前最近一个交易日的汇率，并在记录中注明实际取值日
            usage = None
            for day in sorted({d for (p, d) in self.fx if p == direct[0]}, reverse=True):
                if day < date:
                    rate, ver = self.fx[(direct[0], day)]
                    out = q_money(amount * D(rate))
                    usage = {"pair": direct[0], "rate": rate, "rate_date": day,
                             "direction": "direct", "note": f"按最近可得汇率日 {day} 换算"}
                    break
            if usage is None:
                raise EngineError(f"缺少汇率 {from_ccy}/{to_ccy}（{date} 或之前）")
        usage.update({
            "date": date, "from_currency": from_ccy, "to_currency": to_ccy,
            "amount_from": dec_str(q_money(amount)), "amount_to": dec_str(out),
            "source_version": ver, "purpose": purpose, "message_id": message_id,
            "account": account, "security": security, "action_id": action_id,
        })
        self.fx_usages.append(usage)
        return out

    def _note(self, *, date, kind, message_id, action_id=None, account=None,
              security=None, detail=""):
        self.notes.append({
            "date": date, "kind": kind, "message_id": message_id,
            "action_id": action_id, "account": account, "security": security,
            "detail": detail,
        })

    # ------------------------------------------------------------- 快照与汇总
    def snapshot_day(self, day: str):
        view: dict = {}
        for (account, security), ids in sorted(self.open_lots.items()):
            lots = [self.lots[i] for i in ids
                    if not self.lots[i].closed and self.lots[i].quantity > 0]
            if not lots:
                continue
            qty = sum((l.quantity for l in lots), D("0"))
            frozen = sum((l.frozen for l in lots), D("0"))
            cost = sum((l.cost_total for l in lots), D("0"))
            view.setdefault(account, {})[security] = {
                "quantity": dec_str(q_qty(qty)),
                "frozen": dec_str(q_qty(frozen)),
                "sellable": dec_str(q_qty(qty - frozen)),
                "cost_total": dec_str(q_money(cost)),
                "currency": lots[0].currency,
                "lots": [l.view() for l in lots],
            }
        self.daily_positions[day] = view
        self.daily_cash[day] = {
            f"{acc}|{ccy}": dec_str(q_money(amt))
            for (acc, ccy), amt in sorted(self.cash.items()) if amt != 0
        }

    # ---------------------------------------------------------------- 步骤处理
    def do_cash_movement(self, step):
        p = step["payload"]
        amount = q_money(D(p["amount"]))
        if p["direction"] == "withdraw":
            amount = -amount
        self._move_cash(date=step["date"], account=p["account"], currency=p["currency"],
                        amount=amount, reason="cash_" + p["direction"],
                        message_id=step["message_id"], note=p.get("note", ""))

    def do_trade(self, step):
        p = step["payload"]
        account, security = p["account"], p["security"]
        ccy = p["currency"]
        qty = q_qty(D(p["quantity"]))
        fee = self._convert(amount=D(p.get("fee", "0")), from_ccy=p.get("fee_currency", ccy),
                            to_ccy=ccy, date=step["date"], message_id=step["message_id"],
                            purpose="trade_fee", account=account, security=security)
        if p["side"] == "BUY":
            gross = q_money(qty * D(p["price"]))
            lot = self._new_lot(account=account, security=security, quantity=qty,
                                cost_total=gross + fee, currency=ccy,
                                date=step["date"], origin="trade")
            lot.note(date=step["date"], seq=step["seq"], message_id=step["message_id"],
                     action_id=None, change="open",
                     note=f"买入 {dec_str(qty)} @{p['price']}，费用 {dec_str(fee)} {ccy} 资本化")
            self._move_cash(date=step["date"], account=account, currency=ccy,
                            amount=-(gross + fee), reason="trade_buy",
                            message_id=step["message_id"], note=f"{security} 买入")
        else:  # SELL
            if sum((l.sellable for l in self._fifo_lots(account, security)), D("0")) < qty:
                raise EngineError(
                    f"{account}/{security} 可售数量不足：卖出 {dec_str(qty)}，"
                    f"含冻结在内的可售不足")
            cost_removed = self._reduce_fifo(account=account, security=security, qty=qty,
                                             date=step["date"], seq=step["seq"],
                                             message_id=step["message_id"], change="sell")
            gross = q_money(qty * D(p["price"]))
            proceeds = q_money(gross - fee)
            self._move_cash(date=step["date"], account=account, currency=ccy,
                            amount=proceeds, reason="trade_sell",
                            message_id=step["message_id"], note=f"{security} 卖出")
            self.realized.append({
                "date": step["date"], "account": account, "security": security,
                "quantity": dec_str(qty), "proceeds": dec_str(proceeds),
                "cost_removed": dec_str(cost_removed),
                "pnl": dec_str(q_money(proceeds - cost_removed)), "currency": ccy,
                "message_id": step["message_id"],
            })

    def _reduce_fifo(self, *, account, security, qty, date, seq, message_id,
                     change, action_id=None) -> Decimal:
        """FIFO 扣减批次，返回结转成本。最后一份取批次剩余成本，避免尾差。"""
        remaining = q_qty(qty)
        cost_removed = D("0")
        for lot in self._fifo_lots(account, security):
            if remaining <= 0:
                break
            take = min(lot.quantity, remaining)
            if take == lot.quantity:
                part = lot.cost_total
                lot.closed = True
            else:
                part = q_money(lot.cost_total * take / lot.quantity)
            lot.cost_total = q_money(lot.cost_total - part)
            lot.quantity = q_qty(lot.quantity - take)
            lot.frozen = min(lot.frozen, lot.quantity)
            cost_removed += part
            remaining = q_qty(remaining - take)
            lot.note(date=date, seq=seq, message_id=message_id, action_id=action_id,
                     change=change, note=f"扣减 {dec_str(q_qty(take))}，结转成本 {dec_str(q_money(part))}")
        if remaining > 0:
            raise EngineError(f"{account}/{security} 持仓不足，无法扣减 {dec_str(q_qty(qty))}")
        return q_money(cost_removed)

    def do_transfer(self, step):
        p = step["payload"]
        account, security = p["account"], p["security"]
        qty = q_qty(D(p["quantity"]))
        if p["type"] == "transfer_in":
            if step["status"] != "confirmed":
                return  # 未确认的转入不入账，仅在 pending 清单中可见
            cost = q_money(D(p["cost_total"]))
            lot = self._new_lot(account=account, security=security, quantity=qty,
                                cost_total=cost, currency=p["currency"],
                                date=step["date"], origin="transfer_in")
            lot.note(date=step["date"], seq=step["seq"], message_id=step["message_id"],
                     action_id=None, change="open",
                     note=f"自 {p.get('counter_account', '外部')} 转入，成本随券结转")
        else:  # transfer_out
            if step["status"] != "confirmed":
                # 未确认：只冻结，去向明确到具体批次
                self._freeze(account, security, qty, step)
                return
            self._unfreeze(p["transfer_id"])
            cost_out = self._reduce_fifo(account=account, security=security, qty=qty,
                                         date=step["date"], seq=step["seq"],
                                         message_id=step["message_id"], change="transfer_out")
            self._note(date=step["date"], kind="transfer_out", message_id=step["message_id"],
                       account=account, security=security,
                       detail=f"转出 {dec_str(qty)} 至 {p.get('counter_account', '外部')}，"
                              f"结转成本 {dec_str(cost_out)} {p['currency']}")

    def _freeze(self, account, security, qty, step):
        remaining = qty
        frozen_parts = []
        for lot in self._fifo_lots(account, security):
            if remaining <= 0:
                break
            take = min(lot.sellable, remaining)
            if take <= 0:
                continue
            lot.frozen = q_qty(lot.frozen + take)
            remaining = q_qty(remaining - take)
            frozen_parts.append((lot.lot_id, take))
            lot.note(date=step["date"], seq=step["seq"], message_id=step["message_id"],
                     action_id=None, change="freeze",
                     note=f"待确认转出冻结 {dec_str(q_qty(take))}")
        self.freezes[step["payload"]["transfer_id"]] = frozen_parts
        detail = f"冻结 {dec_str(q_qty(qty - remaining))}（去向：{[f'{i}:{dec_str(q)}' for i, q in frozen_parts]}）"
        if remaining > 0:
            detail += f"，缺口 {dec_str(remaining)} 待持仓到位后补足"
        self._note(date=step["date"], kind="freeze", message_id=step["message_id"],
                   account=account, security=security, detail=detail)

    def _unfreeze(self, transfer_id):
        for lot_id, qty in self.freezes.pop(transfer_id, []):
            lot = self.lots[lot_id]
            lot.frozen = q_qty(max(D("0"), lot.frozen - qty))

    # -------------------------------------------------------------- 公司行动
    def do_split(self, step):
        p = step["payload"]
        security = p["security"]
        num, den = D(p["numerator"]), D(p["denominator"])
        price = p.get("cash_in_lieu_price")
        fractions: dict[str, Decimal] = defaultdict(lambda: D("0"))
        for lot in list(self.lots.values()):
            if lot.security != security or lot.closed or lot.quantity <= 0:
                continue
            raw = lot.quantity * num / den
            whole = D(floor_int(raw))
            frac = q_qty(raw - whole)
            if frac > 0 and price:
                lot.quantity = whole
                fractions[lot.account] += frac
                note = f"拆股 {num}:{den}，碎股 {dec_str(frac)} 折现"
            else:
                lot.quantity = q_qty(raw)
                note = f"拆股 {num}:{den}，总成本不变、单位成本摊薄"
            # 冻结数量随拆股同比例缩放，并同步冻结台账，保证确认后解冻数量一致
            lot.frozen = q_qty(min(lot.frozen * num / den, lot.quantity))
            lot.note(date=step["date"], seq=step["seq"], message_id=step["message_id"],
                     action_id=p["action_id"], change="split", note=note)
        for transfer_id, parts in self.freezes.items():
            self.freezes[transfer_id] = [
                (lid, q_qty(q * num / den)) if self.lots[lid].security == security else (lid, q)
                for lid, q in parts
            ]
        for account, frac in sorted(fractions.items()):
            cash = q_money(frac * D(price))
            self._move_cash(date=step["date"], account=account, currency=p["currency"],
                            amount=cash, reason="split_fraction_cash_in_lieu",
                            message_id=step["message_id"], action_id=p["action_id"],
                            note=f"碎股 {dec_str(frac)} 股按 {price} 折现")

    def do_rights_entitle(self, step):
        p = step["payload"]
        for account in self._holders(p["security"]):
            held = self._held(account, p["security"])
            if held <= 0:
                continue
            raw = held * D(p["ratio"])
            whole, frac = floor_int(raw), q_qty(raw - floor_int(raw))
            lots = [l.lot_id for l in self._fifo_lots(account, p["security"])]
            self.entitlements[(account, p["action_id"])] = {
                "whole": whole, "frac": frac, "held": held, "lots": lots,
            }
            self._note(date=step["date"], kind="rights_entitle",
                       message_id=step["message_id"], action_id=p["action_id"],
                       account=account, security=p["security"],
                       detail=f"登记持仓 {dec_str(q_qty(held))}，配股权益 {whole} 股"
                              f"（碎股 {dec_str(frac)} 股待处置）")

    def do_rights_settle(self, step):
        p = step["payload"]
        ccy = p["currency"]
        for (account, action_id), ent in sorted(self.entitlements.items()):
            if action_id != p["action_id"]:
                continue
            whole, frac = ent["whole"], ent["frac"]
            election = self.elections.get((account, action_id), {})
            choice = election.get("choice", p.get("default_election", "subscribe"))
            if choice == "subscribe" and whole > 0:
                gross = q_money(D(whole) * D(p["subscription_price"]))
                fee = self._rights_fee(p, account, ccy, step)
                lot = self._new_lot(account=account, security=p["security"], quantity=whole,
                                    cost_total=gross + fee, currency=ccy, date=step["date"],
                                    origin="rights_issue", derived_from=ent["lots"])
                lot.note(date=step["date"], seq=step["seq"], message_id=step["message_id"],
                         action_id=action_id, change="open",
                         note=f"配股认购 {whole} 股 @{p['subscription_price']}，"
                              f"费用 {dec_str(fee)} {ccy} 资本化")
                self._move_cash(date=step["date"], account=account, currency=ccy,
                                amount=-(gross + fee), reason="rights_subscribe",
                                message_id=step["message_id"], action_id=action_id,
                                note=f"配股缴款 {whole} 股")
            elif choice == "cash" and whole > 0:
                credit = q_money(D(whole) * D(p["cash_in_lieu_price"]))
                self._move_cash(date=step["date"], account=account, currency=ccy,
                                amount=credit, reason="rights_cash_election",
                                message_id=step["message_id"], action_id=action_id,
                                note=f"现金选择权：{whole} 股按 {p['cash_in_lieu_price']} 折现")
            elif choice == "lapse":
                self._note(date=step["date"], kind="rights_lapse",
                           message_id=step["message_id"], action_id=action_id,
                           account=account, security=p["security"],
                           detail=f"放弃认购 {whole} 股")
            if frac > 0:
                if p.get("fraction_policy", "cash_in_lieu") == "cash_in_lieu":
                    cash = q_money(frac * D(p["cash_in_lieu_price"]))
                    self._move_cash(date=step["date"], account=account, currency=ccy,
                                    amount=cash, reason="rights_fraction_cash_in_lieu",
                                    message_id=step["message_id"], action_id=action_id,
                                    note=f"不足一股碎股 {dec_str(frac)} 股按 {p['cash_in_lieu_price']} 折现")
                else:
                    self._note(date=step["date"], kind="rights_fraction_lapse",
                               message_id=step["message_id"], action_id=action_id,
                               account=account, security=p["security"],
                               detail=f"碎股 {dec_str(frac)} 股按公告作废")

    def _rights_fee(self, p, account, ccy, step) -> Decimal:
        fee = p.get("fee")
        if not fee:
            return D("0")
        return self._convert(amount=D(fee["amount"]), from_ccy=fee["currency"], to_ccy=ccy,
                             date=step["date"], message_id=step["message_id"],
                             purpose="rights_fee", account=account, security=p["security"],
                             action_id=p["action_id"])

    def do_dividend_entitle(self, step):
        p = step["payload"]
        for account in self._holders(p["security"]):
            held = self._held(account, p["security"])
            if held <= 0:
                continue
            lots = [l.lot_id for l in self._fifo_lots(account, p["security"])]
            self.entitlements[(account, p["action_id"])] = {"held": held, "lots": lots}
            self._note(date=step["date"], kind="dividend_entitle",
                       message_id=step["message_id"], action_id=p["action_id"],
                       account=account, security=p["security"],
                       detail=f"登记持仓 {dec_str(q_qty(held))}，每股派 {p['dividend_per_share']}")

    def do_dividend_pay(self, step):
        p = step["payload"]
        ccy = p["currency"]
        reinvest = step.get("reinvest_action")  # 关联的再投资行动（如有）
        for (account, action_id), ent in sorted(self.entitlements.items()):
            if action_id != p["action_id"]:
                continue
            gross = q_money(ent["held"] * D(p["dividend_per_share"]))
            fee = D("0")
            f = p.get("fee")
            if f:
                fee = self._convert(amount=D(f["amount"]), from_ccy=f["currency"], to_ccy=ccy,
                                    date=step["date"], message_id=step["message_id"],
                                    purpose="dividend_fee", account=account,
                                    security=p["security"], action_id=action_id)
            net = q_money(gross - fee)
            do_reinvest = False
            if reinvest:
                election = self.elections.get((account, reinvest["action_id"]), {})
                do_reinvest = election.get("choice", reinvest.get("default_election", "cash")) == "reinvest"
            if do_reinvest:
                price = D(reinvest["reinvest_price"])
                shares = floor_int(net / price)
                used = q_money(D(shares) * price)
                residual = q_money(net - used)
                if shares > 0:
                    lot = self._new_lot(account=account, security=p["security"], quantity=shares,
                                        cost_total=used, currency=ccy, date=step["date"],
                                        origin="reinvestment", derived_from=ent["lots"])
                    lot.note(date=step["date"], seq=step["seq"], message_id=step["message_id"],
                             action_id=reinvest["action_id"], change="open",
                             note=f"红利再投资 {shares} 股 @{reinvest['reinvest_price']}")
                self._move_cash(date=step["date"], account=account, currency=ccy,
                                amount=residual, reason="dividend_reinvest_residual",
                                message_id=step["message_id"], action_id=action_id,
                                note=f"红利净额 {dec_str(net)}，再投资 {dec_str(used)}，余款留现金")
            else:
                self._move_cash(date=step["date"], account=account, currency=ccy,
                                amount=net, reason="dividend",
                                message_id=step["message_id"], action_id=action_id,
                                note=f"红利 {dec_str(gross)} 减费用 {dec_str(fee)}")

    def _holders(self, security: str) -> list[str]:
        return sorted({acc for (acc, sec) in self.open_lots
                       if sec == security and any(not self.lots[i].closed for i in self.open_lots[(acc, sec)])})


# ---------------------------------------------------------------------------
# 事件归并与步骤展开
# ---------------------------------------------------------------------------

def _resolve_actions(events: list[dict]) -> tuple[dict, list, list]:
    """按 action_id 归并公告：最高版本生效；返回 (生效, 被取代, 已撤销)。"""
    by_id: dict[str, list[dict]] = defaultdict(list)
    for env in events:
        if env["kind"] == "corporate_action":
            by_id[env["payload"]["action_id"]].append(env)
    effective, superseded, cancelled = {}, [], []
    for action_id, envs in sorted(by_id.items()):
        envs.sort(key=lambda e: (e["payload"]["announcement_version"], e["seq"]))
        top = envs[-1]
        superseded.extend({"action_id": action_id,
                           "announcement_version": e["payload"]["announcement_version"],
                           "message_id": e["message_id"]} for e in envs[:-1])
        if top["payload"].get("status") == "cancelled":
            cancelled.append({"action_id": action_id,
                              "announcement_version": top["payload"]["announcement_version"],
                              "message_id": top["message_id"]})
        else:
            effective[action_id] = top
    return effective, superseded, cancelled


def _resolve_statuses(events: list[dict]) -> dict[str, str]:
    """消息状态：默认按载荷，confirmation 事件可确认此前的 pending 消息。"""
    status = {}
    for env in events:
        if env["kind"] == "confirmation":
            status[env["payload"]["message_id"]] = env["payload"].get("status", "confirmed")
        else:
            status.setdefault(env["message_id"], env["payload"].get("status", "confirmed"))
    return status


def build_version(events: list[dict], holidays: set[str]) -> dict:
    """全量重放事件日志，返回一个完整的核算版本文档。"""
    rep = _Replay(holidays)
    effective, superseded, cancelled = _resolve_actions(events)
    statuses = _resolve_statuses(events)

    for env in events:
        if env["kind"] == "fx_rate":
            p = env["payload"]
            key = (p["pair"], p["rate_date"])
            cur = rep.fx.get(key)
            if cur is None or p["source_version"] > cur[1]:
                rep.fx[key] = (p["rate"], p["source_version"])
        elif env["kind"] == "election":
            p = env["payload"]
            rep.elections[(p["account"], p["action_id"])] = p

    # 关联：红利 -> 再投资行动
    reinvest_by_dividend = {
        p["source_action_id"]: p
        for p in (e["payload"] for e in effective.values())
        if p["type"] == "reinvestment"
    }

    steps: list[dict] = []
    for env in events:
        p, kind = env["payload"], env["kind"]
        status = statuses.get(env["message_id"], "confirmed")
        if status == "pending":
            rep.pending.append({"message_id": env["message_id"], "kind": kind,
                                "status": status, "payload": p})
            if kind != "transfer":  # 未确认消息一律不生效；转出除外（先冻结）
                continue
        if kind == "cash_movement" and status == "confirmed":
            steps.append({"date": p["date"], "phase": "cash_movement", "kind": kind,
                          "seq": env["seq"], "message_id": env["message_id"],
                          "payload": p, "status": status, "tie": env["message_id"]})
        elif kind == "trade":
            steps.append({"date": p["trade_date"], "phase": "trade", "kind": kind,
                          "seq": env["seq"], "message_id": env["message_id"],
                          "payload": p, "status": status, "tie": p["trade_id"]})
        elif kind == "transfer":
            phase = "transfer_in" if p["type"] == "transfer_in" else "transfer_out"
            steps.append({"date": p["date"], "phase": phase, "kind": kind,
                          "seq": env["seq"], "message_id": env["message_id"],
                          "payload": p, "status": status, "tie": p["transfer_id"]})
        elif kind == "corporate_action":
            action_id = p["action_id"]
            if action_id not in effective or effective[action_id] is not env:
                continue  # 旧版本或已撤销的公告不产生步骤
            steps.extend(_action_steps(env, reinvest_by_dividend, rep.holidays))

    handlers = {
        "cash_movement": rep.do_cash_movement,
        "trade": rep.do_trade,
        "transfer_in": rep.do_transfer,
        "transfer_out": rep.do_transfer,
        "split": rep.do_split,
        "rights_entitle": rep.do_rights_entitle,
        "rights_settle": rep.do_rights_settle,
        "dividend_entitle": rep.do_dividend_entitle,
        "dividend_pay": rep.do_dividend_pay,
    }
    for step in sort_steps(steps):
        handlers[step["phase"]](step)
        rep.applied.append({"seq": step["seq"], "message_id": step["message_id"],
                            "kind": step["kind"], "phase": step["phase"],
                            "date": step["date"], "hash": hash_obj(step["payload"])})
        rep.snapshot_day(step["date"])

    lot_index = {lid: lot.to_dict() for lid, lot in sorted(rep.lots.items())}
    lot_histories = {lid: lot.history for lid, lot in sorted(rep.lots.items())}
    # 参考事件（汇率、选择、确认）不产生步骤，但必须计入版本，保证每条入库事件去向完整
    reference_events = [
        {"seq": env["seq"], "message_id": env["message_id"], "kind": env["kind"],
         "hash": hash_obj(env["payload"])}
        for env in events if env["kind"] in ("fx_rate", "election", "confirmation")
    ]
    state_hash = hash_obj({
        "positions": rep.daily_positions, "cash": rep.daily_cash,
        "lot_index": lot_index, "realized": rep.realized,
    })
    version_hash = hash_obj({
        "events": [a["hash"] for a in rep.applied],
        "reference": [r["hash"] for r in reference_events],
        "state": state_hash,
    })
    return {
        "created_at": _now_iso(),
        "events_applied": rep.applied,
        "reference_events": reference_events,
        "pending": rep.pending,
        "superseded_actions": superseded,
        "cancelled_actions": cancelled,
        "actions_used": [
            {k: env["payload"][k] for k in
             ("action_id", "type", "security", "announcement_version",
              "ex_date", "record_date", "pay_date")}
            for env in sorted(effective.values(), key=lambda e: e["payload"]["action_id"])
        ],
        "positions": rep.daily_positions,
        "cash": rep.daily_cash,
        "realized": rep.realized,
        "fx_usages": rep.fx_usages,
        "cash_movements": rep.cash_movements,
        "notes": rep.notes,
        "lot_index": lot_index,
        "lot_histories": lot_histories,
        "state_hash": state_hash,
        "version_hash": version_hash,
    }


def _action_steps(env: dict, reinvest_by_dividend: dict, holidays: set[str]) -> list[dict]:
    """把一份生效公告展开为带日期的处理步骤（除权/登记/到账，到账日按日历顺延）。"""
    p = env["payload"]
    base = {"kind": "corporate_action", "seq": env["seq"], "message_id": env["message_id"],
            "payload": p, "status": "confirmed", "tie": p["action_id"],
            "announcement_version": p["announcement_version"]}
    steps = []
    t = p["type"]
    if t == "split":
        steps.append({**base, "date": p["ex_date"], "phase": "split"})
    elif t == "rights_issue":
        pay = adjust_business_day(p["pay_date"], holidays)
        steps.append({**base, "date": p["record_date"], "phase": "rights_entitle"})
        steps.append({**base, "date": pay, "phase": "rights_settle"})
    elif t == "cash_dividend":
        pay = adjust_business_day(p["pay_date"], holidays)
        reinvest = reinvest_by_dividend.get(p["action_id"])
        if reinvest and adjust_business_day(reinvest["pay_date"], holidays) != pay:
            raise EngineError(
                f"再投资 {reinvest['action_id']} 的到账日必须与红利 {p['action_id']} 一致")
        steps.append({**base, "date": p["record_date"], "phase": "dividend_entitle"})
        steps.append({**base, "date": pay, "phase": "dividend_pay",
                      "reinvest_action": reinvest})
    elif t == "reinvestment":
        pass  # 步骤随其关联的红利行动展开
    else:
        raise EngineError(f"未知公司行动类型: {t}")
    return steps
