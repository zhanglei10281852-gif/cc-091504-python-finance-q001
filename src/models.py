"""领域基础模型与确定性序列化工具。

所有金额、数量一律使用 Decimal，序列化为字符串，保证版本哈希可校验、
重放结果可复现。数量精度取自 reference/domain.json 的 quantity_precision(6)。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP

QTY_PLACES = Decimal("0.000001")
MONEY_PLACES = Decimal("0.01")


def D(value) -> Decimal:
    """把输入安全地转成 Decimal（不接受 float 隐式转换）。"""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        raise TypeError("禁止使用 float 表示金额或数量，请用字符串")
    return Decimal(str(value))


def q_qty(value) -> Decimal:
    """数量精度：6 位小数。"""
    return D(value).quantize(QTY_PLACES, rounding=ROUND_HALF_UP)


def q_money(value) -> Decimal:
    """金额精度：2 位小数。"""
    return D(value).quantize(MONEY_PLACES, rounding=ROUND_HALF_UP)


def floor_int(value) -> int:
    """向下取整（用于不足一股的碎股切分）。"""
    return int(D(value).to_integral_value(rounding=ROUND_FLOOR))


def dec_str(value) -> str:
    return format(D(value), "f")


def canonical(obj) -> str:
    """确定性 JSON：键排序、无空白，用于哈希与校验。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def hash_obj(obj) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 事件类型（与 reference/domain.json 对齐）
# ---------------------------------------------------------------------------

EVENT_KINDS = (
    "cash_movement",     # 现金存入/支取
    "trade",             # 成交
    "transfer",          # 跨账户转入/转出
    "corporate_action",  # 公司行动公告（含更正、撤销）
    "election",          # 账户层选择（配股认购/现金选择权、红利再投资）
    "confirmation",      # 确认此前处于 pending 的消息
    "fx_rate",           # 汇率（带 source_version）
)

ACTION_TYPES = ("split", "rights_issue", "cash_dividend", "reinvestment")


@dataclass
class Lot:
    """持仓批次。cost_total 为批次总成本（持仓币种），历史逐笔可追溯。"""

    lot_id: str
    account: str
    security: str
    quantity: Decimal
    cost_total: Decimal
    currency: str
    opened_date: str
    origin: str                      # trade / transfer_in / rights_issue / reinvestment
    root_id: str                     # 谱系根批次（拆股不变根，新股自根）
    frozen: Decimal = Decimal("0")
    derived_from: list = field(default_factory=list)  # 派生来源批次（如配股源自哪些老批次）
    closed: bool = False
    history: list = field(default_factory=list)       # [{date, seq, message_id, action_id, change, quantity_after, cost_after, note}]

    @property
    def sellable(self) -> Decimal:
        return self.quantity - self.frozen

    def note(self, *, date, seq, message_id, action_id, change, note=""):
        self.history.append({
            "date": date,
            "seq": seq,
            "message_id": message_id,
            "action_id": action_id,
            "change": change,
            "quantity_after": dec_str(q_qty(self.quantity)),
            "cost_after": dec_str(q_money(self.cost_total)),
            "note": note,
        })

    def to_dict(self) -> dict:
        return {
            "lot_id": self.lot_id,
            "account": self.account,
            "security": self.security,
            "quantity": dec_str(q_qty(self.quantity)),
            "frozen": dec_str(q_qty(self.frozen)),
            "sellable": dec_str(q_qty(self.sellable)),
            "cost_total": dec_str(q_money(self.cost_total)),
            "currency": self.currency,
            "opened_date": self.opened_date,
            "origin": self.origin,
            "root_id": self.root_id,
            "derived_from": list(self.derived_from),
            "closed": self.closed,
        }

    def view(self) -> dict:
        """按日快照中的精简视图。"""
        return {
            "lot_id": self.lot_id,
            "quantity": dec_str(q_qty(self.quantity)),
            "frozen": dec_str(q_qty(self.frozen)),
            "cost_total": dec_str(q_money(self.cost_total)),
            "root_id": self.root_id,
            "origin": self.origin,
            "opened_date": self.opened_date,
        }
