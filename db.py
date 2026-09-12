"""MySQL 访问层：连接、建表、种子规则、查询辅助。"""
import json
import logging

import pymysql
from pymysql.cursors import DictCursor

from config import DB_CONFIG

log = logging.getLogger("risk.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    k           VARCHAR(64) PRIMARY KEY,
    v           BIGINT NOT NULL DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS rules (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    name        VARCHAR(128) NOT NULL,
    definition  JSON NOT NULL,
    enabled     TINYINT(1) NOT NULL DEFAULT 1,
    version     INT NOT NULL DEFAULT 1,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY idx_enabled (enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS events (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id     VARCHAR(64) NOT NULL,
    amount      DECIMAL(18,2) NOT NULL,
    merchant    VARCHAR(128) NOT NULL,
    event_time  DATETIME NOT NULL,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    KEY idx_user_time (user_id, event_time),
    KEY idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS decisions (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    event_id      BIGINT NULL,
    user_id       VARCHAR(64) NOT NULL,
    amount        DECIMAL(18,2) NOT NULL,
    merchant      VARCHAR(128) NOT NULL,
    decision      ENUM('PASS','REJECT','REVIEW') NOT NULL,
    score         INT NOT NULL DEFAULT 0,
    matched_rules JSON NULL,
    latency_ms    FLOAT NOT NULL DEFAULT 0,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    KEY idx_decision (decision),
    KEY idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS alarms (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    dedup_key     VARCHAR(160) NOT NULL UNIQUE,
    user_id       VARCHAR(64) NOT NULL,
    rule_id       INT NOT NULL,
    rule_name     VARCHAR(128) NOT NULL,
    level         VARCHAR(16) NOT NULL DEFAULT 'MEDIUM',
    message       VARCHAR(512) NOT NULL,
    event_id      BIGINT NULL,
    hit_count     INT NOT NULL DEFAULT 1,
    status        ENUM('OPEN','RESOLVED') NOT NULL DEFAULT 'OPEN',
    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY idx_status (status),
    KEY idx_user (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

# 初始规则（JSON 定义）。字段：user_id / amount / merchant / hour / time
_SEED_RULES = [
    {
        "name": "单笔金额超限",
        "definition": {
            "description": "单笔交易金额超过 10000 元直接拒绝",
            "priority": 100,
            "weight": 10,
            "action": "REJECT",
            "conditions": [{"field": "amount", "op": "gt", "value": 10000}],
            "dedup_seconds": 120,
        },
    },
    {
        "name": "大额交易人工审核",
        "definition": {
            "description": "单笔金额在 5000 ~ 10000 之间转人工审核",
            "priority": 50,
            "weight": 5,
            "action": "REVIEW",
            "conditions": [{"field": "amount", "op": "gte", "value": 5000}],
            "dedup_seconds": 120,
        },
    },
    {
        "name": "短时高频交易",
        "definition": {
            "description": "同一用户 60 秒内交易次数超过 5 次",
            "priority": 80,
            "weight": 8,
            "action": "REJECT",
            "window": {"seconds": 60, "group_by": "user_id", "agg": "count", "op": "gt", "value": 5},
            "dedup_seconds": 60,
        },
    },
    {
        "name": "短时累计金额异常",
        "definition": {
            "description": "同一用户 300 秒内累计金额超过 50000 元转人工审核",
            "priority": 60,
            "weight": 6,
            "action": "REVIEW",
            "window": {"seconds": 300, "group_by": "user_id", "agg": "sum", "field": "amount", "op": "gt", "value": 50000},
            "dedup_seconds": 300,
        },
    },
    {
        "name": "深夜大额交易",
        "definition": {
            "description": "凌晨 0-6 点金额超过 2000 元转人工审核",
            "priority": 40,
            "weight": 4,
            "action": "REVIEW",
            "conditions": [
                {"field": "hour", "op": "lt", "value": 6},
                {"field": "amount", "op": "gt", "value": 2000},
            ],
            "dedup_seconds": 120,
        },
    },
    {
        "name": "高风险商户拦截",
        "definition": {
            "description": "来自黑名单商户的交易直接拒绝",
            "priority": 90,
            "weight": 9,
            "action": "REJECT",
            "conditions": [{"field": "merchant", "op": "in", "value": ["black_shop", "crypto_exchange", "gambling_site"]}],
            "dedup_seconds": 120,
        },
    },
]


def get_connection():
    """获取一个 MySQL 连接。"""
    cfg = dict(DB_CONFIG)
    cfg["cursorclass"] = DictCursor
    return pymysql.connect(**cfg)


def init_db():
    """创建数据库与表结构，并在首次启动时写入种子规则。"""
    base = dict(DB_CONFIG)
    db_name = base.pop("database")
    base.pop("cursorclass", None)  # 建库连接无需 DictCursor
    # 先建库
    conn = pymysql.connect(**base)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
                "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        conn.commit()
    finally:
        conn.close()

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for stmt in _SCHEMA.split(";"):
                stmt = stmt.strip()
                if stmt:
                    cur.execute(stmt)
            cur.execute("SELECT COUNT(*) AS c FROM rules")
            count = cur.fetchone()["c"]
            if count == 0:
                for rule in _SEED_RULES:
                    cur.execute(
                        "INSERT INTO rules (name, definition, enabled) VALUES (%s, %s, 1)",
                        (rule["name"], json.dumps(rule["definition"], ensure_ascii=False)),
                    )
        conn.commit()
    finally:
        conn.close()
    log.info("数据库初始化完成（种子规则 %d 条）", len(_SEED_RULES))


def fetch_rules():
    """查询全部规则（含禁用），返回行字典列表。"""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM rules ORDER BY id ASC")
            return cur.fetchall()
    finally:
        conn.close()


def rules_version():
    """返回规则配置的单调版本号（用于热更新探测）。"""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT v FROM meta WHERE k='rules_version'")
            row = cur.fetchone()
            return int(row["v"]) if row else 0
    finally:
        conn.close()


def bump_rules_version(cur):
    """在已有事务中自增规则版本号。"""
    cur.execute(
        "INSERT INTO meta (k, v) VALUES ('rules_version', 1) "
        "ON DUPLICATE KEY UPDATE v = v + 1"
    )
