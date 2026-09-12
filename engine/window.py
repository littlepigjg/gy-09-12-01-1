"""精确滑动窗口聚合。

使用按 key 维护的 deque 保存窗口内每个事件的 (时间戳, 值)，
新事件到达时先剔除过期元素再聚合，得到精确的滑动窗口结果
（区别于按固定桶切分的“滚动窗口”近似）。
"""
from collections import deque
from typing import Any, Deque, Dict, Tuple


class SlidingWindow:
    def __init__(self, seconds: int, agg: str, field: str, op: str, value: float):
        self.seconds = seconds
        self.agg = agg
        self.field = field
        self.op = op
        self.value = value
        # key -> deque[(ts, record_value)]
        self.buckets: Dict[str, Deque[Tuple[float, Any]]] = {}

    def observe(self, key: str, ts: float, event: Dict[str, Any]) -> float:
        """记录一条事件并返回该 key 当前窗口的聚合值。"""
        bucket = self.buckets.setdefault(key, deque())

        if self.agg == "count":
            rec = 1.0
        else:
            rec = event.get(self.field)

        bucket.append((ts, rec))

        cutoff = ts - self.seconds
        while bucket and bucket[0][0] < cutoff:
            bucket.popleft()

        return self._aggregate(bucket)

    def _aggregate(self, bucket: Deque[Tuple[float, Any]]) -> float:
        if self.agg == "count":
            return float(len(bucket))
        if not bucket:
            return 0.0
        if self.agg == "distinct":
            return float(len({v for _, v in bucket}))
        try:
            vals = [float(v) for _, v in bucket]
        except (TypeError, ValueError):
            return 0.0
        if self.agg == "sum":
            return sum(vals)
        if self.agg == "avg":
            return sum(vals) / len(vals)
        if self.agg == "max":
            return max(vals)
        if self.agg == "min":
            return min(vals)
        return float(len(bucket))
