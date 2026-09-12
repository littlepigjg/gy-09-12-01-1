"""实时交易风控系统 —— Flask 入口与 REST API。"""
import json
import logging
import random
import threading
import time
from datetime import datetime

from flask import Flask, jsonify, request

import db
from config import HTTP_PORT
from engine.engine import RiskEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("risk.app")

app = Flask(__name__, static_folder="static", static_url_path="")

_engine = None
_engine_lock = threading.Lock()


def get_engine() -> RiskEngine:
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = RiskEngine()
                threading.Thread(target=_engine.hot_reload_loop, daemon=True).start()
    return _engine


# ---------------- 事件规范化 ----------------
def normalize_event(payload: dict):
    user_id = str(payload.get("user_id", "")).strip()
    amount = float(payload.get("amount", 0) or 0)
    merchant = str(payload.get("merchant", "")).strip()
    t = payload.get("time")
    if t is None:
        now = datetime.now()
    elif isinstance(t, (int, float)):
        now = datetime.fromtimestamp(float(t))
    else:
        s = str(t).replace("Z", "+00:00")
        now = datetime.fromisoformat(s)
        if now.tzinfo is not None:
            now = now.astimezone().replace(tzinfo=None)

    event = {
        "user_id": user_id,
        "amount": amount,
        "merchant": merchant,
        "time": now.timestamp(),
        "hour": now.hour,
        "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return event, now


# ---------------- 告警去重 ----------------
def _upsert_alarm(cur, event, event_id, m):
    rule_id = m["id"]
    user_id = event["user_id"]
    level = "HIGH" if m["action"] == "REJECT" else "MEDIUM"
    if m.get("agg_value") is not None:
        message = f"用户 {user_id} 触发「{m['name']}」：窗口聚合值 {m['agg_value']}"
    else:
        message = f"用户 {user_id} 触发「{m['name']}」"
    # 去重键 = 规则 + 用户 + 时间桶，同一窗口内只累计一次，避免告警风暴
    dedup_seconds = int(m.get("dedup_seconds", 60))
    bucket = int(time.time() // max(dedup_seconds, 1))
    dedup_key = f"{rule_id}:{user_id}:{bucket}"
    cur.execute(
        "INSERT INTO alarms (dedup_key, user_id, rule_id, rule_name, level, message, event_id) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE hit_count = hit_count + 1",
        (dedup_key, user_id, rule_id, m["name"], level, message, event_id),
    )


# ---------------- 事件处理核心 ----------------
def process_event(payload: dict):
    event, now = normalize_event(payload)
    result = get_engine().evaluate(event)
    conn = db.get_connection()
    event_id = None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO events (user_id, amount, merchant, event_time) VALUES (%s,%s,%s,%s)",
                (event["user_id"], event["amount"], event["merchant"], event["datetime"]),
            )
            event_id = cur.lastrowid
            cur.execute(
                "INSERT INTO decisions (event_id, user_id, amount, merchant, decision, score, matched_rules, latency_ms) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (event_id, event["user_id"], event["amount"], event["merchant"],
                 result["decision"], result["score"],
                 json.dumps(result["matched"], ensure_ascii=False), result["latency_ms"]),
            )
            for m in result["matched"]:
                if m["action"] in ("REJECT", "REVIEW"):
                    _upsert_alarm(cur, event, event_id, m)
        conn.commit()
    finally:
        conn.close()
    return {"event_id": event_id, "event": event, **result}


# ---------------- 前端页面 ----------------
@app.route("/")
def index():
    return app.send_static_file("index.html")


# ---------------- 规则管理 ----------------
@app.route("/api/rules", methods=["GET"])
def list_rules():
    rows = db.fetch_rules()
    out = []
    for r in rows:
        d = r["definition"]
        if isinstance(d, str):
            d = json.loads(d)
        out.append({
            "id": r["id"],
            "name": r["name"],
            "enabled": bool(r["enabled"]),
            "definition": d,
            "created_at": str(r["created_at"]),
            "updated_at": str(r["updated_at"]),
        })
    return jsonify(out)


@app.route("/api/rules", methods=["POST"])
def create_rule():
    data = request.get_json(force=True, silent=True) or {}
    name = str(data.get("name", "")).strip()
    definition = data.get("definition")
    if not name:
        return jsonify({"error": "规则名称不能为空"}), 400
    if not isinstance(definition, dict):
        return jsonify({"error": "definition 必须是 JSON 对象"}), 400
    enabled = 1 if data.get("enabled", True) else 0
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO rules (name, definition, enabled) VALUES (%s,%s,%s)",
                (name, json.dumps(definition, ensure_ascii=False), enabled),
            )
            rule_id = cur.lastrowid
            db.bump_rules_version(cur)
        conn.commit()
    finally:
        conn.close()
    get_engine().reload()
    return jsonify({"id": rule_id}), 201


@app.route("/api/rules/<int:rid>", methods=["PUT"])
def update_rule(rid):
    data = request.get_json(force=True, silent=True) or {}
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM rules WHERE id=%s", (rid,))
            if not cur.fetchone():
                return jsonify({"error": "规则不存在"}), 404
            fields, vals = [], []
            if "name" in data:
                fields.append("name=%s")
                vals.append(str(data["name"]).strip())
            if "definition" in data:
                if not isinstance(data["definition"], dict):
                    return jsonify({"error": "definition 必须是 JSON 对象"}), 400
                fields.append("definition=%s")
                vals.append(json.dumps(data["definition"], ensure_ascii=False))
            if "enabled" in data:
                fields.append("enabled=%s")
                vals.append(1 if data["enabled"] else 0)
            if fields:
                cur.execute("UPDATE rules SET " + ",".join(fields) + " WHERE id=%s", (*vals, rid))
                db.bump_rules_version(cur)
        conn.commit()
    finally:
        conn.close()
    get_engine().reload()
    return jsonify({"ok": True})


@app.route("/api/rules/<int:rid>", methods=["DELETE"])
def delete_rule(rid):
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM rules WHERE id=%s", (rid,))
            if cur.rowcount == 0:
                return jsonify({"error": "规则不存在"}), 404
            db.bump_rules_version(cur)
        conn.commit()
    finally:
        conn.close()
    get_engine().reload()
    return jsonify({"ok": True})


@app.route("/api/rules/<int:rid>/toggle", methods=["POST"])
def toggle_rule(rid):
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE rules SET enabled = 1 - enabled WHERE id=%s", (rid,))
            if cur.rowcount == 0:
                return jsonify({"error": "规则不存在"}), 404
            db.bump_rules_version(cur)
        conn.commit()
    finally:
        conn.close()
    get_engine().reload()
    return jsonify({"ok": True})


# ---------------- 事件接入 / 模拟 ----------------
@app.route("/api/events", methods=["POST"])
def ingest_event():
    payload = request.get_json(force=True, silent=True) or {}
    return jsonify(process_event(payload))


@app.route("/api/simulate", methods=["POST"])
def simulate():
    data = request.get_json(force=True, silent=True) or {}
    count = max(1, min(int(data.get("count", 1)), 200))
    focus = data.get("user_id")
    results = []
    users = [focus] * 3 if focus else ["u1001", "u1002", "u1003", "u1004", "u1005"]
    merchants = ["shop_a", "shop_b", "shop_c", "shop_d", "black_shop", "crypto_exchange", "gambling_site"]
    amounts = [50, 120, 300, 900, 2000, 6000, 12000, 30000]
    for _ in range(count):
        payload = {
            "user_id": random.choice(users),
            "amount": random.choice(amounts),
            "merchant": random.choices(merchants, weights=[3, 3, 3, 3, 1, 1, 1])[0],
        }
        results.append(process_event(payload))
    return jsonify({"count": len(results), "results": results})


# ---------------- 查询 ----------------
@app.route("/api/decisions", methods=["GET"])
def decisions():
    since = request.args.get("since_id", type=int, default=0)
    limit = min(request.args.get("limit", type=int, default=30), 200)
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            if since > 0:
                cur.execute("SELECT * FROM decisions WHERE id > %s ORDER BY id DESC LIMIT %s", (since, limit))
            else:
                cur.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT %s", (limit,))
            rows = list(cur.fetchall())
    finally:
        conn.close()
    for r in rows:
        r["matched_rules"] = json.loads(r["matched_rules"]) if r["matched_rules"] else []
        r["amount"] = float(r["amount"])
        r["created_at"] = str(r["created_at"])
    rows.reverse()
    return jsonify(rows)


@app.route("/api/alarms", methods=["GET"])
def alarms():
    status = request.args.get("status", "OPEN").upper()
    if status not in ("OPEN", "RESOLVED"):
        status = "OPEN"
    limit = min(request.args.get("limit", type=int, default=100), 500)
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM alarms WHERE status=%s ORDER BY id DESC LIMIT %s", (status, limit))
            rows = cur.fetchall()
    finally:
        conn.close()
    for r in rows:
        r["first_seen_at"] = str(r["first_seen_at"])
        r["last_seen_at"] = str(r["last_seen_at"])
    return jsonify(rows)


@app.route("/api/alarms/<int:aid>/resolve", methods=["POST"])
def resolve_alarm(aid):
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE alarms SET status='RESOLVED' WHERE id=%s", (aid,))
            if cur.rowcount == 0:
                return jsonify({"error": "告警不存在"}), 404
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


@app.route("/api/stats", methods=["GET"])
def stats():
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) c FROM events")
            total_events = cur.fetchone()["c"]
            cur.execute("SELECT decision, COUNT(*) c FROM decisions GROUP BY decision")
            breakdown = {r["decision"]: r["c"] for r in cur.fetchall()}
            cur.execute("SELECT COUNT(*) c FROM alarms WHERE status='OPEN'")
            open_alarms = cur.fetchone()["c"]
    finally:
        conn.close()
    return jsonify({
        "total_events": total_events,
        "decisions": {
            "PASS": breakdown.get("PASS", 0),
            "REJECT": breakdown.get("REJECT", 0),
            "REVIEW": breakdown.get("REVIEW", 0),
        },
        "open_alarms": open_alarms,
        "engine": get_engine().stats(),
    })


def main():
    # 等待 MySQL 就绪（容器编排时 mysql 可能仍在初始化）
    last_err = None
    for attempt in range(30):
        try:
            db.init_db()
            last_err = None
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("等待 MySQL 就绪 (%d/30)：%s", attempt + 1, e)
            time.sleep(2)
    if last_err is not None:
        log.error("MySQL 连接失败，退出：%s", last_err)
        raise SystemExit(1)

    get_engine()  # 预加载规则并启动热更新线程
    log.info("实时交易风控系统启动，监听 0.0.0.0:%s", HTTP_PORT)
    app.run(host="0.0.0.0", port=HTTP_PORT, threaded=True, debug=False)


if __name__ == "__main__":
    main()
