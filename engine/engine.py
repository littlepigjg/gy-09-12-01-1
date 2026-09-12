"""风控引擎编排：加载规则、评估事件、决策、滑动窗口、热更新。"""
import logging
import threading
import time
from typing import Any, Dict, List, Tuple

import db
from config import HOT_RELOAD_INTERVAL
from engine.matcher import RuleMatcher
from engine.rules import Rule, _compare
from engine.window import SlidingWindow

log = logging.getLogger("risk.engine")


class RiskEngine:
    def __init__(self):
        self._lock = threading.RLock()
        self.rules: List[Rule] = []
        self.rules_by_id: Dict[int, Rule] = {}
        self.matcher: RuleMatcher = None
        self._windows: Dict[int, SlidingWindow] = {}
        self._version = -1
        self.reload()

    # ---------- 规则加载 / 热更新 ----------
    def reload(self):
        rows = db.fetch_rules()
        rules = [Rule(r) for r in rows if r["enabled"]]
        matcher = RuleMatcher(rules)
        with self._lock:
            self.rules = rules
            self.rules_by_id = {r.id: r for r in rules}
            self.matcher = matcher
            # 规则变化后重建窗口状态，避免携带旧规则残留
            self._windows = {}
            self._version = db.rules_version()
        log.info("规则加载完成：%d 条启用，决策树节点 %d / 叶子 %d / 共享条件 %d",
                 len(rules), matcher.node_count, matcher.leaf_count, len(matcher.condition_keys))

    def hot_reload_loop(self):
        """后台守护线程：轮询版本号，变化则热更新（无需重启进程）。"""
        while True:
            time.sleep(HOT_RELOAD_INTERVAL)
            try:
                if db.rules_version() != self._version:
                    self.reload()
            except Exception:  # noqa: BLE001
                log.exception("规则热更新检查失败")

    # ---------- 核心评估 ----------
    def evaluate(self, event: Dict[str, Any]) -> Dict[str, Any]:
        start = time.perf_counter()
        with self._lock:
            matched_ids = self.matcher.match(event)
            matched = [self.rules_by_id[i] for i in matched_ids if i in self.rules_by_id]
            matched.sort(key=lambda r: r.priority, reverse=True)

            triggered: List[Tuple[Rule, float]] = []
            for rule in matched:
                if rule.window:
                    agg_val = self._check_window(rule, event)
                    if _compare(rule.window["op"], agg_val, float(rule.window["value"])):
                        triggered.append((rule, agg_val))
                else:
                    triggered.append((rule, 0.0))

            score = sum(r.weight for r, _ in triggered)
            if any(r.action == "REJECT" for r, _ in triggered):
                decision = "REJECT"
            elif any(r.action == "REVIEW" for r, _ in triggered):
                decision = "REVIEW"
            else:
                decision = "PASS"

            latency_ms = (time.perf_counter() - start) * 1000

        return {
            "decision": decision,
            "score": score,
            "latency_ms": round(latency_ms, 3),
            "matched": [
                {
                    "id": r.id,
                    "name": r.name,
                    "action": r.action,
                    "priority": r.priority,
                    "weight": r.weight,
                    "dedup_seconds": r.dedup_seconds,
                    "agg_value": round(agg, 2) if r.window else None,
                    # 评分回路据此剔除风险等级规则，避免自强化锁死
                    "uses_risk_level": r.uses_risk_level,
                }
                for r, agg in triggered
            ],
        }

    def _check_window(self, rule: Rule, event: Dict[str, Any]) -> float:
        w = rule.window
        group_by = w.get("group_by", "user_id")
        key = str(event.get(group_by, ""))
        win = self._windows.get(rule.id)
        if win is None:
            win = SlidingWindow(
                seconds=int(w.get("seconds", 60)),
                agg=w.get("agg", "count"),
                field=w.get("field", "amount"),
                op=w.get("op", "gt"),
                value=float(w.get("value", 0)),
            )
            self._windows[rule.id] = win
        return win.observe(key, float(event["time"]), event)

    # ---------- 元信息 ----------
    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "rule_count": len(self.rules),
                "node_count": self.matcher.node_count,
                "leaf_count": self.matcher.leaf_count,
                "shared_conditions": len(self.matcher.condition_keys),
                "active_windows": sum(len(w.buckets) for w in self._windows.values()),
                "version": self._version,
            }
