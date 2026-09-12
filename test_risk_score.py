"""用户风险评分模型的单元测试（不依赖数据库 / Flask）。

运行：python3 test_risk_score.py
"""
import unittest

import risk_score as rs


def make_event(amount=100, hour=12):
    return {"amount": amount, "hour": hour}


def play(profile, event, decision, ts):
    """便捷函数：用同一时间轴推进画像。"""
    return rs.update_profile(profile, event, decision, ts)


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
            p = play(p, make_event(amount=100), "PASS", ts=1000)
        self.assertEqual(p["score"], 40.0 - 6 * 2.0)  # 28
        self.assertEqual(p["level"], "LOW")
        self.assertEqual(p["tx_count"], 6)
        self.assertFalse(p["is_new"])

    def test_first_bad_tx_not_lethal(self):
        """新用户首笔即被拦：升到 HIGH 但不直接 CRITICAL（不一棒子打死）。"""
        p = rs.default_profile("u_bad")
        p = play(p, make_event(amount=12000, hour=3), "REJECT", ts=1000)
        # 40 + 20(REJECT) + 6(>=10000) + 2(深夜) = 68
        self.assertAlmostEqual(p["score"], 68.0)
        self.assertEqual(p["level"], "HIGH")
        self.assertNotEqual(p["level"], "CRITICAL")
        self.assertEqual(p["reject_count"], 1)
        self.assertEqual(p["night_count"], 1)
        self.assertEqual(p["total_amount"], 12000.0)


class TestScoreMovement(unittest.TestCase):
    def test_persistent_reject_reaches_critical(self):
        """持续被拦会升到 CRITICAL。"""
        p = rs.default_profile("u_fraud")
        p = play(p, make_event(amount=12000, hour=3), "REJECT", ts=1000)   # 68
        p = play(p, make_event(amount=12000, hour=3), "REJECT", ts=1000)   # 96
        self.assertEqual(p["level"], "CRITICAL")
        self.assertEqual(p["reject_count"], 2)

    def test_score_capped_at_100(self):
        p = rs.default_profile("u_cap")
        for i in range(10):
            p = play(p, make_event(amount=12000, hour=3), "REJECT", ts=1000 + i)
        self.assertEqual(p["score"], 100.0)

    def test_score_floored_at_0(self):
        # 每小时 1 笔的正常节奏不会触发高频加分（频率半衰期 1h，平衡值约 2）
        p = rs.default_profile("u_floor")
        for i in range(50):
            p = play(p, make_event(amount=10), "PASS", ts=1000 + i * 3600)
        self.assertEqual(p["score"], 0.0)
        self.assertEqual(p["level"], "LOW")

    def test_review_adds_less_than_reject(self):
        p1 = play(rs.default_profile("a"), make_event(), "REVIEW", ts=1000)
        p2 = play(rs.default_profile("b"), make_event(), "REJECT", ts=1000)
        self.assertLess(p1["score"] - 40, p2["score"] - 40)
        self.assertEqual(p1["review_count"], 1)

    def test_high_frequency_bonus(self):
        """短时间密集交易（窗口内 > 10 笔）触发高频附加分。"""
        p = rs.default_profile("u_freq")
        for _ in range(10):
            p = play(p, make_event(), "PASS", ts=1000)
        score_before = p["score"]  # 40 - 20 = 20
        p = play(p, make_event(), "PASS", ts=1000)
        # 第 11 笔：-2(PASS) + 5(高频) = +3
        self.assertAlmostEqual(p["score"], score_before + 3.0)

    def test_normal_pace_not_high_frequency(self):
        """每小时 1 笔的正常节奏不触发高频加分（频率是短期信号）。"""
        p = rs.default_profile("u_normal")
        for i in range(20):
            p = play(p, make_event(), "PASS", ts=1000 + i * 3600)
        # 只含 PASS 减分与衰减，不含高频加分：分数应单调走低
        self.assertLess(p["score"], 40.0)


class TestDecay(unittest.TestCase):
    def test_time_decay_pulls_score_down(self):
        """半衰期 24h：静置 48h 后评分衰减为 1/4，等级随之下调。"""
        p = rs.default_profile("u_decay")
        p = play(p, make_event(amount=12000, hour=3), "REJECT", ts=1000)   # 68
        p = play(p, make_event(amount=12000, hour=3), "REJECT", ts=1000)   # 96
        self.assertEqual(p["level"], "CRITICAL")
        # 静置 48 小时后来一笔正常小额交易
        p = play(p, make_event(amount=50, hour=12), "PASS", ts=1000 + 48 * 3600)
        # 96 * 0.25 - 2 = 22
        self.assertAlmostEqual(p["score"], 22.0)
        self.assertEqual(p["level"], "LOW")

    def test_reform_path_high_to_low(self):
        """完整路径：坏用户升到 HIGH 后，靠良好行为 + 时间衰减降回 LOW。"""
        p = rs.default_profile("u_reform")
        p = play(p, make_event(amount=12000, hour=3), "REJECT", ts=0)      # 68 HIGH
        self.assertEqual(p["level"], "HIGH")
        # 接下来一周，每天 5 笔正常交易
        ts = 3600.0
        for _ in range(7 * 5):
            p = play(p, make_event(amount=80, hour=14), "PASS", ts=ts)
            ts += 3600.0
        self.assertEqual(p["level"], "LOW")
        self.assertLess(p["score"], 30.0)

    def test_no_decay_without_elapsed_time(self):
        """同一时刻（无时间流逝）不发生衰减。"""
        p = rs.default_profile("u_same_ts")
        p = play(p, make_event(), "REVIEW", ts=5000)   # 48
        p2 = play(p, make_event(), "PASS", ts=5000)    # 48 - 2
        self.assertAlmostEqual(p2["score"], 46.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
