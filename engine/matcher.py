"""规则匹配器：基于决策树的规则高效匹配。

思路：
- 规则的条件是 AND 连接词。把所有规则的条件集合看作样本，递归挑选
  “出现频次最高”的条件作为分裂节点，构建一棵二叉决策树（左=条件成立，
  右=条件不成立）。
- 不含某分裂条件的规则在左右两侧都可能命中，因此进入两个分支；叶子节点
  用集合去重，保证每个规则只返回一次。
- 无条件（全命中）的规则被单独提取，避免树内被无限复制。

相比朴素地逐条规则线性扫描，决策树把“共享的条件”只求值一次，命中的
规则集合沿路径直接剪枝得到，规则越多收益越明显。
"""
from collections import Counter
from typing import Any, Dict, List, Set

from engine.rules import Rule, evaluate_condition


class _Leaf:
    __slots__ = ("matched",)

    def __init__(self, matched: Set[int]):
        self.matched = matched


class _Node:
    __slots__ = ("condition", "left", "right")

    def __init__(self, condition, left, right):
        self.condition = condition
        self.left = left
        self.right = right


class RuleMatcher:
    def __init__(self, rules: List[Rule]):
        self.rules = rules
        # 无条件规则恒命中，单独缓存，避免决策树内重复复制
        self.always: Set[int] = {r.id for r in rules if not r.conditions}
        self.condition_keys: Set = set()
        self.node_count = 0
        self.leaf_count = 0
        self.root = self._build([r for r in rules if r.conditions])

    def _build(self, rules: List[Rule]):
        items = [(r, list(r.conditions)) for r in rules]
        return self._build_rec(items)

    def _build_rec(self, items):
        freq: Counter = Counter()
        for _, rem in items:
            for c in rem:
                freq[c.key()] += 1

        if not freq:
            matched = {r.id for r, rem in items if not rem}
            self.leaf_count += 1
            return _Leaf(matched)

        # 选出现频次最高的条件作为分裂节点（尽量浅、尽量少重复求值）
        best_key = freq.most_common(1)[0][0]
        self.condition_keys.add(best_key)
        best_cond = None
        for _, rem in items:
            for c in rem:
                if c.key() == best_key:
                    best_cond = c
                    break
            if best_cond is not None:
                break

        true_items, false_items = [], []
        for r, rem in items:
            has = any(c.key() == best_key for c in rem)
            if has:
                true_items.append((r, [c for c in rem if c.key() != best_key]))
            else:
                # 不含该条件，左右分支都保留
                true_items.append((r, rem))
                false_items.append((r, rem))

        self.node_count += 1
        return _Node(
            best_cond,
            self._build_rec(true_items),
            self._build_rec(false_items),
        )

    def match(self, event: Dict[str, Any]) -> Set[int]:
        """返回命中的规则 id 集合。"""
        matched: Set[int] = set(self.always)
        stack = [self.root]
        while stack:
            n = stack.pop()
            if isinstance(n, _Leaf):
                matched.update(n.matched)
            elif evaluate_condition(n.condition, event):
                stack.append(n.left)
            else:
                stack.append(n.right)
        return matched
