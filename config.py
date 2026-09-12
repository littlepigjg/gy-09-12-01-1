"""全局配置。"""
import os

# MySQL 连接配置
DB_CONFIG = {
    "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
    "user": os.getenv("MYSQL_USER", "root"),
    "password": os.getenv("MYSQL_PASSWORD", "root"),
    "database": os.getenv("MYSQL_DATABASE", "risk_control"),
    "charset": "utf8mb4",
    "autocommit": True,
    "cursorclass": None,  # 在 db 模块中动态设置
}

# 服务端口
HTTP_PORT = int(os.getenv("PORT", "8002"))

# 规则热更新轮询间隔（秒）
HOT_RELOAD_INTERVAL = float(os.getenv("HOT_RELOAD_INTERVAL", "2.0"))

# 用户风险画像评分参数
RISK_NEW_USER_SCORE = float(os.getenv("RISK_NEW_USER_SCORE", "40"))    # 新用户起始分（中性偏保守）
RISK_HALF_LIFE_HOURS = float(os.getenv("RISK_HALF_LIFE_HOURS", "24"))  # 评分半衰期（小时），越小遗忘越快
RISK_FREQ_HALF_LIFE_HOURS = float(os.getenv("RISK_FREQ_HALF_LIFE_HOURS", "1"))  # 频率计数半衰期（小时），高频是短期信号
