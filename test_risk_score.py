"""用户风险评分模型的单元测试（不依赖数据库 / Flask）。

直接覆盖核心更新逻辑：
- update_profile        评分加减、边界、时间衰减、频率信号
- behavioral_decision   剔除风险等级规则的行为决策
- update_from_result    生产路径入口（引擎结果 -> 画像更新）
- 锁死回归              活跃用户被自动拦截时等级必须能降下来

运行：python3 test_risk_score.py
"""
import json
import sys
import types
import unittest

import risk_score as rs


def make_event(amount=100, hour=12):
    return {"amount": amount, "hour": hour}


def risk_rule_hit(action="REJECT"):
    """一条引用风险等级的规则命中（如「极高风险用户拦截」）。"""
    return {"action": action, "uses_risk_level": True}


def behavior_rule_hit(action="REJECT"):
    """一条普通行为规则命中（如「单笔金额超限」）。"""
    return {"action": action, "uses_risk_level": False}


class TestLevelMapping(unittest.TestCase):
    def test_boundaries(self):
        self.assertEqual(rs.level_of(0), "LOW")
        self.assertEqual(rs.level_of(29.9), "LOW")
        self.assertEqual(rs.level_of(30), "MEDIUM")
        self.assertEqual(rs.level_of(59.9), "MEDIUM")
        self.assertEqual(rs.level_of(60), "HIGH")
        self.assertEqual(rs.level_of(84.9), "HIGH")
        self.assertEqual(rs.level_of(85), "CRITICAL")
        self.assertEqual(rs.level_of(100), "CRITICAL")


class TestNewUser(unittest.TestCase):
    def test_neutral_conservative_start(self):
        """新用户：中性偏保守起点（MEDIUM），不一棒子打死，也不盲目信任。"""
        p = rs.default_profile("u_new")
        self.assertEqual(p["score"], 40.0)
        self.assertEqual(p["level"], "MEDIUM")
        self.assertTrue(p["is_new"])
        self.assertEqual(p["tx_count"], 0)

    def test_first_clean_tx_earns_trust(self):
        """新用户连续正常交易，分数稳步下降并跌入 LOW。"""
        p = rs.default_profile("u_good")
        for _ in range(6):
            p = rs.update_profile(p, make_event(amount=100), "PASS", 1000)
        self.assertEqual(p["score"], 40.0 - 6 * 2.0)  # 28
        self.assertEqual(p["level"], "LOW")
        self.assertEqual(p["tx_count"], 6)
        self.assertFalse(p["is_new"])

    def test_first_bad_tx_not_lethal(self):
        """新用户首笔即被拦：升到 HIGH 但不直接 CRITICAL（不一棒子打死）。"""
        p = rs.default_profile("u_bad")
        p = rs.update_profile(p, make_event(amount=12000, hour=3), "REJECT", 1000)
        # 40 + 20(REJECT) + 6(>=10000) + 2(深夜) = 68
        self.assertAlmostEqual(p["score"], 68.0)
        self.assertEqual(p["level"], "HIGH")
        self.assertEqual(p["reject_count"], 1)
        self.assertEqual(p["night_count"], 1)
        self.assertEqual(p["total_amount"], 12000.0)


class TestScoreMovement(unittest.TestCase):
    def test_persistent_reject_reaches_critical(self):
        """持续被拦会升到 CRITICAL。"""
        p = rs.default_profile("u_fraud")
        p = rs.update_profile(p, make_event(amount=12000, hour=3), "REJECT", 1000)  # 68
        p = rs.update_profile(p, make_event(amount=12000, hour=3), "REJECT", 1000)  # 96
        self.assertEqual(p["level"], "CRITICAL")
        self.assertEqual(p["reject_count"], 2)

    def test_score_capped_at_100(self):
        p = rs.default_profile("u_cap")
        for i in range(10):
            p = rs.update_profile(p, make_event(amount=12000, hour=3), "REJECT", 1000 + i)
        self.assertEqual(p["score"], 100.0)

    def test_score_floored_at_0(self):
        # 每小时 1 笔的正常节奏不会触发高频加分（频率半衰期 1h，平衡值约 2）
        p = rs.default_profile("u_floor")
        for i in range(50):
            p = rs.update_profile(p, make_event(amount=10), "PASS", 1000 + i * 3600)
        self.assertEqual(p["score"], 0.0)
        self.assertEqual(p["level"], "LOW")

    def test_review_adds_less_than_reject(self):
        p1 = rs.update_profile(rs.default_profile("a"), make_event(), "REVIEW", 1000)
        p2 = rs.update_profile(rs.default_profile("b"), make_event(), "REJECT", 1000)
        self.assertLess(p1["score"] - 40, p2["score"] - 40)
        self.assertEqual(p1["review_count"], 1)

    def test_high_frequency_bonus(self):
        """短时间密集交易（窗口内 > 10 笔）触发高频附加分。"""
        p = rs.default_profile("u_freq")
        for _ in range(10):
            p = rs.update_profile(p, make_event(), "PASS", 1000)
        score_before = p["score"]  # 40 - 20 = 20
        p = rs.update_profile(p, make_event(), "PASS", 1000)
        # 第 11 笔：-2(PASS) + 5(高频) = +3
        self.assertAlmostEqual(p["score"], score_before + 3.0)

    def test_normal_pace_not_high_frequency(self):
        """每小时 1 笔的正常节奏不触发高频加分（频率是短期信号）。"""
        p = rs.default_profile("u_normal")
        for i in range(20):
            p = rs.update_profile(p, make_event(), "PASS", 1000 + i * 3600)
        # 只含 PASS 减分与衰减，不含高频加分：分数应单调走低
        self.assertLess(p["score"], 40.0)


class TestDecay(unittest.TestCase):
    def test_time_decay_pulls_score_down(self):
        """半衰期 24h：静置 48h 后评分衰减为 1/4，等级随之下调。"""
        p = rs.default_profile("u_decay")
        p = rs.update_profile(p, make_event(amount=12000, hour=3), "REJECT", 1000)   # 68
        p = rs.update_profile(p, make_event(amount=12000, hour=3), "REJECT", 1000)   # 96
        self.assertEqual(p["level"], "CRITICAL")
        # 静置 48 小时后来一笔正常小额交易
        p = rs.update_profile(p, make_event(amount=50, hour=12), "PASS", 1000 + 48 * 3600)
        # 96 * 0.25 - 2 = 22
        self.assertAlmostEqual(p["score"], 22.0)
        self.assertEqual(p["level"], "LOW")

    def test_reform_path_high_to_low(self):
        """完整路径：坏用户升到 HIGH 后，靠良好行为 + 时间衰减降回 LOW。"""
        p = rs.default_profile("u_reform")
        p = rs.update_profile(p, make_event(amount=12000, hour=3), "REJECT", 0)      # 68 HIGH
        self.assertEqual(p["level"], "HIGH")
        # 接下来一周，每小时 1 笔正常交易
        ts = 3600.0
        for _ in range(7 * 24):
            p = rs.update_profile(p, make_event(amount=80, hour=14), "PASS", ts)
            ts += 3600.0
        self.assertEqual(p["level"], "LOW")
        self.assertLess(p["score"], 30.0)

    def test_no_decay_without_elapsed_time(self):
        """同一时刻（无时间流逝）不发生衰减。"""
        p = rs.default_profile("u_same_ts")
        p = rs.update_profile(p, make_event(), "REVIEW", 5000)   # 48
        p2 = rs.update_profile(p, make_event(), "PASS", 5000)    # 48 - 2
        self.assertAlmostEqual(p2["score"], 46.0)


class TestBehavioralDecision(unittest.TestCase):
    """行为决策：剔除引用风险画像字段的规则，打破自强化回路。"""

    def test_empty_matched_is_pass(self):
        self.assertEqual(rs.behavioral_decision([]), "PASS")

    def test_risk_rule_only_is_pass(self):
        """仅命中风险等级规则（自动拦截）时，行为决策为 PASS。"""
        self.assertEqual(rs.behavioral_decision([risk_rule_hit("REJECT")]), "PASS")
        self.assertEqual(rs.behavioral_decision([risk_rule_hit("REVIEW")]), "PASS")

    def test_behavior_rule_reject(self):
        self.assertEqual(rs.behavioral_decision([behavior_rule_hit("REJECT")]), "REJECT")

    def test_behavior_review_with_risk_reject(self):
        """行为 REVIEW + 风险 REJECT → 行为决策 REVIEW（风险规则不参与）。"""
        matched = [behavior_rule_hit("REVIEW"), risk_rule_hit("REJECT")]
        self.assertEqual(rs.behavioral_decision(matched), "REVIEW")

    def test_behavior_reject_wins_over_review(self):
        matched = [behavior_rule_hit("REVIEW"), behavior_rule_hit("REJECT")]
        self.assertEqual(rs.behavioral_decision(matched), "REJECT")

    def test_missing_flag_treated_as_behavioral(self):
        """缺少标记的命中按行为规则处理（保守计入）。"""
        self.assertEqual(rs.behavioral_decision([{"action": "REVIEW"}]), "REVIEW")


class TestUpdateFromResult(unittest.TestCase):
    """生产路径入口：评分调节用行为决策，计数值用真实决策。"""

    def test_auto_reject_does_not_feed_score(self):
        """被风险规则自动拒绝：评分按 PASS 下降，但被拦次数照常累计。"""
        p = rs.default_profile("u")
        result = {"decision": "REJECT", "matched": [risk_rule_hit("REJECT")]}
        p = rs.update_from_result(p, make_event(amount=50, hour=14), result, 1000)
        self.assertEqual(p["score"], 38.0)        # 40 - 2，按 PASS 调节
        self.assertEqual(p["reject_count"], 1)    # 真实决策仍计入被拦次数
        self.assertEqual(p["review_count"], 0)
        self.assertEqual(p["tx_count"], 1)

    def test_behavioral_reject_raises_score(self):
        """行为规则导致的拒绝正常加分（风险规则命中不影响该结论）。"""
        result = {"decision": "REJECT",
                  "matched": [risk_rule_hit("REJECT"), behavior_rule_hit("REJECT")]}
        p = rs.update_from_result(rs.default_profile("u"),
                                  make_event(amount=12000, hour=3), result, 1000)
        self.assertAlmostEqual(p["score"], 68.0)  # 40 + 20 + 6 + 2
        self.assertEqual(p["reject_count"], 1)

    def test_missing_flag_counts_as_behavioral(self):
        result = {"decision": "REVIEW", "matched": [{"action": "REVIEW"}]}
        p = rs.update_from_result(rs.default_profile("u"), make_event(), result, 1000)
        self.assertEqual(p["score"], 48.0)        # 40 + 8
        self.assertEqual(p["review_count"], 1)


class TestLockoutRegression(unittest.TestCase):
    """回归：活跃用户被系统自动拦截时，等级必须能降下来。

    故障场景：CRITICAL 用户的每笔交易都被「极高风险用户拦截」自动 REJECT，
    若把该 REJECT 回灌评分（+20），分数被持续推高，时间衰减永远追不上，
    等级锁死在 CRITICAL。修复后评分只跟随行为决策。
    """

    def _push_to_critical(self, user):
        p = rs.default_profile(user)
        for i in range(4):
            p = rs.update_profile(p, make_event(amount=12000, hour=3), "REJECT", i)
        self.assertEqual(p["level"], "CRITICAL")
        return p

    def test_active_auto_rejected_user_recovers(self):
        """CRITICAL 用户持续交易且均被自动拒绝，但行为正常 → 等级降到 LOW。"""
        p = self._push_to_critical("u_lock")
        ts = 1000.0
        for _ in range(40):
            result = {"decision": "REJECT", "matched": [risk_rule_hit("REJECT")]}
            p = rs.update_from_result(p, make_event(amount=50, hour=14), result, ts)
            ts += 600  # 每 10 分钟一笔
        self.assertEqual(p["level"], "LOW")
        self.assertLess(p["score"], 30.0)
        # 被拦次数按真实决策累计：4 次行为拒绝 + 40 次自动拒绝
        self.assertEqual(p["reject_count"], 44)
        self.assertEqual(p["tx_count"], 44)

    def test_full_feedback_loop_recovers(self):
        """端到端回路：等级 → 风险规则 → 决策 → 评分，活跃用户不再锁死。"""
        def fake_engine(profile):
            """模拟生产路径：按当前等级套用种子风险规则出决策。"""
            matched = []
            if profile["level"] == "CRITICAL":
                matched.append(risk_rule_hit("REJECT"))
            elif profile["level"] == "HIGH":
                matched.append(risk_rule_hit("REVIEW"))
            decision = "PASS"
            if any(m["action"] == "REJECT" for m in matched):
                decision = "REJECT"
            elif matched:
                decision = "REVIEW"
            return {"decision": decision, "matched": matched}

        p = self._push_to_critical("u_loop")
        ts, levels = 1000.0, []
        for _ in range(60):
            result = fake_engine(p)  # 系统按当前等级自动处置
            p = rs.update_from_result(p, make_event(amount=100, hour=14), result, ts)
            levels.append(p["level"])
            ts += 600
        self.assertEqual(levels[0], "CRITICAL")  # 初始仍被自动拦截
        self.assertEqual(p["level"], "LOW")      # 持续良好行为后等级回落

    def test_persistent_behavioral_reject_stays_critical(self):
        """行为本身持续恶劣（被行为规则拒绝）→ 等级保持 CRITICAL，不误降。"""
        p = self._push_to_critical("u_bad_actor")
        ts = 1000.0
        for _ in range(20):
            result = {"decision": "REJECT",
                      "matched": [risk_rule_hit("REJECT"), behavior_rule_hit("REJECT")]}
            p = rs.update_from_result(p, make_event(amount=12000, hour=3), result, ts)
            ts += 600
        self.assertEqual(p["level"], "CRITICAL")
        self.assertEqual(p["score"], 100.0)


class TestRuleRiskFlag(unittest.TestCase):
    """规则对象正确标记是否引用风险画像字段。"""

    def _rule(self, conditions):
        from engine.rules import Rule
        return Rule({"id": 1, "name": "t", "enabled": 1, "definition": json.dumps({
            "action": "REJECT", "conditions": conditions})})

    def test_risk_level_condition_flagged(self):
        r = self._rule([{"field": "user_risk_level", "op": "eq", "value": "CRITICAL"}])
        self.assertTrue(r.uses_risk_level)

    def test_risk_score_condition_flagged(self):
        r = self._rule([{"field": "user_risk_score", "op": "gte", "value": 60}])
        self.assertTrue(r.uses_risk_level)

    def test_behavior_condition_not_flagged(self):
        r = self._rule([{"field": "amount", "op": "gt", "value": 10000}])
        self.assertFalse(r.uses_risk_level)

    def test_no_condition_not_flagged(self):
        self.assertFalse(self._rule([]).uses_risk_level)


class TestEngineOutputContract(unittest.TestCase):
    """引擎输出契约：命中规则携带 uses_risk_level 标记。

    该标记是评分回路剔除风险规则的依据；若丢失，
    behavioral_decision 会把自动拦截回灌评分，重新引入锁死。
    （用 fake db 模块替代 MySQL，纯内存验证引擎行为。）
    """

    def _make_engine(self, rows):
        fake_db = types.ModuleType("db")
        fake_db.fetch_rules = lambda: rows
        fake_db.rules_version = lambda: 1
        saved_db = sys.modules.get("db")
        saved_eng = sys.modules.pop("engine.engine", None)
        sys.modules["db"] = fake_db
        try:
            import engine.engine as eng_mod
            return eng_mod.RiskEngine()
        finally:
            sys.modules.pop("db", None)
            if saved_db is not None:
                sys.modules["db"] = saved_db
            if saved_eng is not None:
                sys.modules["engine.engine"] = saved_eng

    def test_evaluate_marks_risk_rules(self):
        rows = [
            {"id": 1, "name": "极高风险用户拦截", "enabled": 1, "definition": json.dumps({
                "priority": 95, "weight": 10, "action": "REJECT",
                "conditions": [{"field": "user_risk_level", "op": "eq", "value": "CRITICAL"}]})},
            {"id": 2, "name": "单笔金额超限", "enabled": 1, "definition": json.dumps({
                "priority": 100, "weight": 10, "action": "REJECT",
                "conditions": [{"field": "amount", "op": "gt", "value": 10000}]})},
        ]
        eng = self._make_engine(rows)
        result = eng.evaluate({"user_id": "u", "amount": 20000, "time": 1000.0,
                               "user_risk_level": "CRITICAL", "user_risk_score": 99})
        self.assertEqual(result["decision"], "REJECT")
        flags = {m["name"]: m["uses_risk_level"] for m in result["matched"]}
        self.assertTrue(flags["极高风险用户拦截"])
        self.assertFalse(flags["单笔金额超限"])
        # 剔除风险规则后行为决策仍为 REJECT（金额超限是行为规则）
        self.assertEqual(rs.behavioral_decision(result["matched"]), "REJECT")


if __name__ == "__main__":
    unittest.main(verbosity=2)
