"""同一交易日内的步骤排序规则与市场日历。

排序原则（规则依赖，稳定且确定）：
  1. 现金与转入先到位，保证后续行动有完整的持仓基数；
  2. 拆股先调整单位成本与数量，再计算任何以持仓为基数的权益；
  3. 成交在权益登记之前入账（登记日按当日日终持仓确定权益）；
  4. 权益登记（配股、红利）→ 配股缴款到账 → 红利到账 → 红利再投资；
  5. 转出最后执行，避免漏算转出方在登记日仍持有的权益。
同阶段内按 (业务标识, 公告版本, 接收序号) 稳定排序，保证重放可复现。
"""
from __future__ import annotations

from datetime import date, timedelta

PHASES = {
    "cash_movement": 5,
    "transfer_in": 10,
    "split": 20,
    "trade": 30,
    "rights_entitle": 40,
    "dividend_entitle": 45,
    "rights_settle": 50,
    "dividend_pay": 55,
    "transfer_out": 70,
}


def step_sort_key(step: dict) -> tuple:
    """(日期, 阶段, 业务标识, 公告版本, 接收序号) —— 完全确定的排序键。"""
    return (
        step["date"],
        PHASES[step["phase"]],
        step.get("tie", ""),
        step.get("announcement_version", 0),
        step["seq"],
    )


def sort_steps(steps: list[dict]) -> list[dict]:
    return sorted(steps, key=step_sort_key)


def is_business_day(day: str, holidays: set[str]) -> bool:
    d = date.fromisoformat(day)
    return d.weekday() < 5 and day not in holidays


def adjust_business_day(day: str, holidays: set[str]) -> str:
    """到账日落在非交易日时顺延到下一交易日（FOLLOWING 规则）。"""
    d = date.fromisoformat(day)
    while True:
        s = d.isoformat()
        if is_business_day(s, holidays):
            return s
        d += timedelta(days=1)
