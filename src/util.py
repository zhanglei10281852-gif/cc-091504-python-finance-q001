"""通用工具：Decimal 精度、规范 JSON、哈希链、日期处理。

所有金额与数量在内存中一律使用 Decimal，序列化为字符串，
保证版本文件、快照与导出文档的哈希可重复校验。
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Any

QTY_PLACES = 6
QTY_QUANT = Decimal("0.000001")
MONEY_QUANT = Decimal("0.01")
ZERO = Decimal("0")

# 哈希链起点（全零），用于成本沿革与事件日志的链式校验
GENESIS = "0" * 64


def D(value: Any) -> Decimal:
    """把字符串/数字安全转换为 Decimal。"""
    if isinstance(value, Decimal):
        return value
    if value is None:
        raise ValueError("无法把 None 转换为 Decimal")
    if isinstance(value, bool):
        raise ValueError("无法把 bool 转换为 Decimal")
    return Decimal(str(value))


def q6(value: Any) -> Decimal:
    """数量精度：6 位小数，四舍五入。"""
    return D(value).quantize(QTY_QUANT, rounding=ROUND_HALF_UP)


def floor6(value: Any) -> Decimal:
    """数量精度：6 位小数，向下取整（用于可交付数量）。"""
    return D(value).quantize(QTY_QUANT, rounding=ROUND_DOWN)


def money(value: Any) -> Decimal:
    """现金精度：2 位小数，四舍五入。"""
    return D(value).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def floor_int(value: Any) -> int:
    """向下取整到整数股（配股不足一股的部分单独处理）。"""
    return int(D(value).to_integral_value(rounding=ROUND_DOWN))


def dec_str(value: Any) -> str:
    """Decimal 的稳定字符串形式（保留小数位，便于哈希一致）。"""
    return format(D(value), "f")


def to_jsonable(obj: Any) -> Any:
    """递归转换为可 JSON 序列化结构：Decimal -> str，日期 -> ISO 字符串。"""
    if isinstance(obj, Decimal):
        return dec_str(obj)
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    return obj


def canonical_json(obj: Any) -> str:
    """规范 JSON：键排序、紧凑分隔符，用于哈希。"""
    return json.dumps(to_jsonable(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_obj(obj: Any) -> str:
    return sha256_text(canonical_json(obj))


def chain_hash(prev: str, step_hash: str) -> str:
    """哈希链：H(n) = sha256(H(n-1) | step(n))。"""
    return sha256_text(f"{prev}|{step_hash}")


def parse_date(value: str) -> date:
    return date.fromisoformat(str(value))


def date_str(value: date) -> str:
    return value.isoformat()


def today_str() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
