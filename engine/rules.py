"""规则模型：JSON 规则定义解析、条件编译与求值。"""
import json
import re
from typing import Any, Dict, List, Optional


class Condition:
    """一条原子条件，如 field='amount' op='gt' value=10000。"""

    __slots__ = ("field", "op", "value")

    def __init__(self, field: str, op: str, value: Any):
        self.field = field
        self.op = op
        self.value = value

    def key(self):
        """用于跨规则共享去重的稳定键。"""
        if isinstance(self.value, list):
            v = tuple(sorted(str(x) for x in self.value))
        else:
            v = self.value
        return (self.field, self.op, v)

    def __repr__(self):
        return f"{self.field} {self.op} {self.value}"


class Rule:
    """编译后的内存规则。"""

    def __init__(self, row: Dict[str, Any]):
        self.id = int(row["id"])
        self.name = row["name"]
        self.enabled = bool(row["enabled"])
        raw = row["definition"]
        if isinstance(raw, str):
            raw = json.loads(raw)
        self.description = raw.get("description", "")
        self.priority = int(raw.get("priority", 0))
        self.weight = int(raw.get("weight", 1))
        self.action = str(raw.get("action", "REVIEW")).upper()
        self.dedup_seconds = int(raw.get("dedup_seconds", 60))
        self.conditions: List[Condition] = [
            Condition(c["field"], c["op"], c["value"]) for c in raw.get("conditions", [])
        ]
        self.window: Optional[Dict[str, Any]] = raw.get("window")


# 数值比较运算符
_NUM_OPS = {"gt", "gte", "lt", "lte"}


def _as_number(v):
    if isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _compare(op: str, left, right) -> bool:
    """left 为实际值，right 为规则阈值，均为数值。"""
    if op == "gt":
        return left > right
    if op == "gte":
        return left >= right
    if op == "lt":
        return left < right
    if op == "lte":
        return left <= right
    if op == "eq":
        return left == right
    if op == "neq":
        return left != right
    return False


def evaluate_condition(cond: Condition, event: Dict[str, Any]) -> bool:
    """对单个事件求值一个原子条件。"""
    val = event.get(cond.field)
    if val is None:
        return False
    op = cond.op
    target = cond.value

    if op in _NUM_OPS:
        left = _as_number(val)
        right = _as_number(target)
        if left is None or right is None:
            return False
        return _compare(op, left, right)

    if op in ("eq", "neq"):
        left_n, right_n = _as_number(val), _as_number(target)
        if left_n is not None and right_n is not None:
            return _compare(op, left_n, right_n)
        return _compare(op, str(val), str(target))

    if op == "contains":
        return str(target) in str(val)

    if op in ("in", "not_in"):
        bag = target if isinstance(target, list) else [target]
        hit = val in bag
        return hit if op == "in" else not hit

    if op == "regex":
        try:
            return re.search(str(target), str(val)) is not None
        except re.error:
            return False

    return False


def evaluate_conditions(conds: List[Condition], event: Dict[str, Any]) -> bool:
    """AND 语义：所有条件满足才命中。"""
    for c in conds:
        if not evaluate_condition(c, event):
            return False
    return True
