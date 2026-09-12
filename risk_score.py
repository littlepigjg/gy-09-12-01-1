"""用户风险画像评分：可升可降的信用分模型。

设计要点：
- 评分 0~100，映射 LOW / MEDIUM / HIGH / CRITICAL 四档等级。
- 新用户没有历史记录，给中性偏保守的起点分（默认 40，MEDIUM），
  既不盲目信任，也不一棒子打死。
- 每笔交易按风控决策加减分：被拦（REJECT）大幅加分、转人工（REVIEW）
  小幅加分、正常通过（PASS）减分 —— 用户变老实，分数就会往下掉。
- 评分与近期频率计数随时间按半衰期指数衰减（默认 24h），
  历史污点会被时间冲淡，保证等级不会只升不降。
- 大额、深夜（0-6 点）、高频交易作为附加调节项，全生命周期统计
  （交易数 / 被拦次数 / 审核次数 / 深夜笔数 / 累计金额）供运营查看。

本模块不依赖数据库，可独立测试。
"""
from config import RISK_FREQ_HALF_LIFE_HOURS, RISK_HALF_LIFE_HOURS, RISK_NEW_USER_SCORE

LEVELS = ("LOW", "MEDIUM", "HIGH", "CRITICAL")

# 行为加减分：决策结果 -> 分数调节
_DELTA = {
    "REJECT": 20.0,   # 交易被拒绝：强风险信号
    "REVIEW": 8.0,    # 转人工审核：中等风险信号
    "PASS": -2.0,     # 正常通过：表现良好，降分
}

_AMOUNT_TIERS = ((10000.0, 6.0), (5000.0, 3.0))  # 大额附加分（达到高一档即不再累加低一档）
_NIGHT_BONUS = 2.0       # 深夜（0-6 点）交易附加分
_FREQ_THRESHOLD = 10.0   # 衰减窗口内交易数超过该值视为高频
_FREQ_BONUS = 5.0        # 高频附加分

_HALF_LIFE_SECONDS = RISK_HALF_LIFE_HOURS * 3600.0
# 频率是短期信号（类似窗口规则的 1 小时尺度），用独立的短半衰期，
# 否则正常节奏的老用户也会被累积成"高频"
_FREQ_HALF_LIFE_SECONDS = RISK_FREQ_HALF_LIFE_HOURS * 3600.0


def level_of(score: float) -> str:
    """评分 -> 等级。区间：[0,30) LOW，[30,60) MEDIUM，[60,85) HIGH，[85,100] CRITICAL。"""
    if score < 30:
        return "LOW"
    if score < 60:
        return "MEDIUM"
    if score < 85:
        return "HIGH"
    return "CRITICAL"


def default_profile(user_id: str) -> dict:
    """新用户画像：中性偏保守的起点，无任何历史。"""
    return {
        "user_id": user_id,
        "score": RISK_NEW_USER_SCORE,
        "level": level_of(RISK_NEW_USER_SCORE),
        "tx_count": 0,
        "reject_count": 0,
        "review_count": 0,
        "night_count": 0,
        "recent_count": 0.0,   # 带衰减的近期交易计数（频率信号）
        "total_amount": 0.0,
        "first_seen_ts": None,
        "last_tx_ts": None,
        "is_new": True,
    }


def _is_night(hour) -> bool:
    return hour is not None and 0 <= int(hour) < 6


def update_profile(profile: dict, event: dict, decision: str, now_ts: float) -> dict:
    """根据一笔交易及其风控决策更新用户画像，返回更新后的画像（不修改入参）。

    分三步：
    1. 时间衰减 —— 距上一笔交易越久，历史评分与近期频率衰减越多（半衰期）。
    2. 行为调节 —— 按本次决策结果、金额、时段、频率加减分。
    3. 统计累计 —— 全生命周期计数，供运营面板展示。
    """
    p = dict(profile)

    # 1. 时间衰减（半衰期）：历史评分向 0 衰减，近期频率按更短的半衰期衰减
    last = p.get("last_tx_ts")
    if last:
        elapsed = max(float(now_ts) - float(last), 0.0)
        p["score"] = float(p["score"]) * 0.5 ** (elapsed / _HALF_LIFE_SECONDS)
        p["recent_count"] = float(p.get("recent_count", 0.0)) * 0.5 ** (elapsed / _FREQ_HALF_LIFE_SECONDS)
    else:
        p["recent_count"] = float(p.get("recent_count", 0.0))

    # 2. 本次行为调节
    p["score"] += _DELTA.get(decision, 0.0)

    amount = float(event.get("amount", 0) or 0)
    for threshold, bonus in _AMOUNT_TIERS:
        if amount >= threshold:
            p["score"] += bonus
            break

    night = _is_night(event.get("hour"))
    if night:
        p["score"] += _NIGHT_BONUS

    p["recent_count"] += 1.0
    if p["recent_count"] > _FREQ_THRESHOLD:
        p["score"] += _FREQ_BONUS

    # 3. 统计累计（不随时间衰减，是"被拦了几次"这类事实记录）
    p["tx_count"] = int(p.get("tx_count", 0)) + 1
    if decision == "REJECT":
        p["reject_count"] = int(p.get("reject_count", 0)) + 1
    elif decision == "REVIEW":
        p["review_count"] = int(p.get("review_count", 0)) + 1
    if night:
        p["night_count"] = int(p.get("night_count", 0)) + 1
    p["total_amount"] = float(p.get("total_amount", 0.0)) + amount

    # 边界约束与时间戳
    p["score"] = min(100.0, max(0.0, p["score"]))
    p["level"] = level_of(p["score"])
    if not p.get("first_seen_ts"):
        p["first_seen_ts"] = now_ts
    p["last_tx_ts"] = now_ts
    p["is_new"] = False
    return p
