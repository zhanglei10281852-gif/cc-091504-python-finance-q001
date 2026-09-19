"""同日多事件的稳定排序规则（规则依赖显式化）。

业务依据：
1. CASH            现金存入/取出最先，保证当日后续交收有款可用；
2. SPLIT           拆股先调整持仓数量，后续确权按调整后的数量计算；
3. ENTITLE         确权（配股权、红利资格）发生在除权日、拆股调整之后；
                   资格 = 除权日重放时点持有且买入日早于除权日的批次；
4. TRADE           当日成交在确权之后入账（除权日当日买入本就不含权）；
5. DELIVER         到账交付（配股缴款到账、红利发放）；
6. REINVEST        红利再投资依赖红利现金已到账，故排在交付之后；
7. FREEZE          冻结/解冻；
8. TRANSFER        跨账户转入转出（冻结之后，冻结份额不可转出）；
9. POSITION_CHECK  持仓核对在当日所有变动之后执行。

同一 (日期, 优先级) 内按业务标识（行动号/成交号等）字典序排序，
最后按事件到达序号兜底，保证重放结果与消息到达顺序无关。
"""
from __future__ import annotations

CASH = 5
SPLIT = 10
ENTITLE = 20
TRADE = 30
DELIVER = 40
REINVEST = 50
FREEZE = 60
TRANSFER = 70
POSITION_CHECK = 90


def sort_key(effect: dict) -> tuple:
    return (effect["date"], effect["priority"], effect["key"], effect["seq"])
