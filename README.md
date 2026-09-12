# 实时交易风控系统

基于 Python + Flask + MySQL 的实时交易风控系统。接收交易事件（用户 ID、金额、商户、时间），根据 JSON 定义的规则进行实时风险评估，输出 **通过（PASS）/ 拒绝（REJECT）/ 人工审核（REVIEW）** 三种决策，并提供规则配置、实时决策流、用户风险画像和报警列表的可视化前端。

## 功能特性

- **规则引擎**：规则以 JSON 定义，支持单笔条件与滑动窗口聚合两类规则。
- **用户风险画像**：基于历史行为为每个用户维护可升可降的风险评分与四档等级（LOW / MEDIUM / HIGH / CRITICAL），新用户中性偏保守起步，等级可直接被规则引用。
- **高效匹配**：基于决策树对规则条件做共享求值与路径剪枝，避免逐条线性扫描。
- **精确滑动窗口**：按分组键维护事件 `deque`，新事件先剔除过期再聚合，非分桶近似。
- **规则热更新**：规则变更后无需重启进程，后台线程轮询版本号自动重建引擎。
- **告警去重**：以「规则 + 用户 + 时间桶」为去重键，窗口内只累计不刷屏。
- **可视化前端**：规则配置、实时风控决策流、用户风险画像、报警列表四大面板，1s 自动刷新。

## 技术栈

- 后端：Python 3.12、Flask、PyMySQL
- 数据库：MySQL 8.0（`root/root`）
- 前端：原生 HTML / CSS / JavaScript（无构建步骤）
- 部署：Docker / Docker Compose

## 目录结构

```
.
├── app.py                 # Flask 入口与 REST API
├── config.py              # 配置（数据库、端口、热更新间隔、风险评分参数）
├── db.py                  # MySQL 连接、建表、种子规则、版本号、用户画像读写
├── risk_score.py          # 用户风险评分模型（纯逻辑，可独立测试）
├── test_risk_score.py     # 评分模型单元测试（python3 test_risk_score.py）
├── engine/
│   ├── rules.py           # 规则 JSON 解析与条件求值
│   ├── matcher.py         # 决策树规则匹配器
│   ├── window.py          # 精确滑动窗口聚合
│   └── engine.py          # 引擎编排 + 规则热更新线程
├── static/                # 前端页面（index.html / style.css / app.js）
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── start.sh               # 一键启动
└── stop.sh                # 停止服务
```

## 一键启动（Docker）

需要本机已安装 Docker 与 Docker Compose。

```bash
chmod +x start.sh stop.sh
./start.sh
```

或直接使用 Compose：

```bash
docker compose up -d --build
```

启动后访问：<http://localhost:8002/>

- 应用端口映射：宿主机 `8002` → 容器 `8000`（如被占用可改 `docker-compose.yml` 中 `ports`）
- MySQL 端口映射：宿主机 `3308` → 容器 `3306`
- 数据卷 `mysql_data` 持久化 MySQL 数据，`docker compose down` 不会丢失数据

停止服务：

```bash
./stop.sh          # 等价于 docker compose down（保留数据）
docker compose down -v   # 连同数据一起删除
```

查看日志：

```bash
docker compose logs -f app
```

## 本地运行（不使用 Docker）

```bash
python3 -m pip install --break-system-packages -r requirements.txt
python3 app.py
```

默认连接 `127.0.0.1:3306` 的 MySQL（`root/root`），监听端口 `8002`。首次启动会自动建库建表并写入 8 条种子规则。

## 环境变量配置

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MYSQL_HOST` | `127.0.0.1` | MySQL 地址（容器内为 `mysql`） |
| `MYSQL_PORT` | `3306` | MySQL 端口 |
| `MYSQL_USER` | `root` | MySQL 用户名 |
| `MYSQL_PASSWORD` | `root` | MySQL 密码 |
| `MYSQL_DATABASE` | `risk_control` | 数据库名 |
| `PORT` | `8002` | 服务监听端口 |
| `HOT_RELOAD_INTERVAL` | `2.0` | 规则热更新轮询间隔（秒） |
| `RISK_NEW_USER_SCORE` | `40` | 新用户起始风险分（0-100，中性偏保守） |
| `RISK_HALF_LIFE_HOURS` | `24` | 风险评分半衰期（小时），越小历史遗忘越快 |
| `RISK_FREQ_HALF_LIFE_HOURS` | `1` | 频率计数半衰期（小时），高频是短期信号 |

## 规则 JSON 定义

规则以 JSON 形式存储在 `rules.definition` 字段，结构如下：

```json
{
  "description": "规则描述",
  "priority": 100,
  "weight": 10,
  "action": "REJECT",
  "conditions": [
    {"field": "amount", "op": "gt", "value": 10000}
  ],
  "window": {
    "seconds": 60,
    "group_by": "user_id",
    "agg": "count",
    "op": "gt",
    "value": 5
  },
  "dedup_seconds": 120
}
```

- `action`：`REJECT`（拒绝）/ `REVIEW`（人工审核）
- `priority`：规则优先级（数值越大越先评估）
- `weight`：命中后累加到风险评分
- `conditions`：AND 组合的事件过滤条件（可选，为空则匹配全部事件）
- `window`：滑动窗口聚合条件（可选，配置后为聚合型规则）
- `dedup_seconds`：告警去重窗口（秒）

### 可用字段

| 字段 | 说明 |
|---|---|
| `user_id` | 用户 ID（字符串） |
| `amount` | 交易金额（数值） |
| `merchant` | 商户（字符串） |
| `time` | 事件时间戳（秒） |
| `hour` | 事件小时（0-23，派生字段） |
| `user_risk_score` | 用户当前风险评分（0-100，派生字段） |
| `user_risk_level` | 用户当前风险等级（LOW / MEDIUM / HIGH / CRITICAL，派生字段） |

### 条件运算符 `op`

- 数值：`gt` / `gte` / `lt` / `lte` / `eq` / `neq`
- 字符串：`eq` / `neq` / `contains` / `in` / `not_in` / `regex`

### 窗口聚合 `window`

| 字段 | 说明 |
|---|---|
| `seconds` | 窗口长度（秒） |
| `group_by` | 分组键，默认 `user_id` |
| `agg` | 聚合方式：`count` / `sum` / `avg` / `max` / `min` / `distinct` |
| `field` | 聚合字段（`count` 时可不填），默认 `amount` |
| `op` | 阈值比较运算符 |
| `value` | 阈值 |

### 内置种子规则示例

| 规则 | 类型 | 逻辑 |
|---|---|---|
| 单笔金额超限 | 条件 | `amount > 10000` → REJECT |
| 大额交易人工审核 | 条件 | `amount >= 5000` → REVIEW |
| 短时高频交易 | 窗口 | 同一用户 60s 内交易次数 > 5 → REJECT |
| 短时累计金额异常 | 窗口 | 同一用户 300s 内累计金额 > 50000 → REVIEW |
| 深夜大额交易 | 条件 | `0 ≤ hour < 6` 且 `amount > 2000` → REVIEW |
| 高风险商户拦截 | 条件 | `merchant in [black_shop, ...]` → REJECT |
| 高风险用户交易审核 | 条件 | `user_risk_level = HIGH` → REVIEW |
| 极高风险用户拦截 | 条件 | `user_risk_level = CRITICAL` → REJECT |

## 用户风险画像与等级

系统在每笔交易落库的同时，为每个用户维护一个 **0~100 的风险评分**，映射为四档等级：

| 等级 | 分数区间 | 含义 |
|---|---|---|
| LOW | < 30 | 表现良好，可信 |
| MEDIUM | 30 ~ 59 | 中性 / 需关注（新用户起点所在档） |
| HIGH | 60 ~ 84 | 高风险，建议人工审核 |
| CRITICAL | ≥ 85 | 极高风险，建议直接拦截 |

### 评分模型（`risk_score.py`）

- **新用户起点**：无历史记录的用户从 40 分（MEDIUM）起步 —— 中性偏保守，既不盲目信任，也不一棒子打死。首笔正常交易即开始建立信任，持续良好行为可降入 LOW。
- **行为加减分**：每笔交易按**行为决策**调整 —— `REJECT +20`、`REVIEW +8`、`PASS -2`；大额（≥5000 / ≥10000）、深夜（0-6 点）、短时高频（约 1 小时内 > 10 笔）分别附加 +3 / +6、+2、+5。
- **行为决策 vs 最终决策**：评分调节使用的「行为决策」会**剔除引用风险画像字段的规则**（如「CRITICAL 用户自动拦截」）。这类规则的命中是评分的结果而非新的行为证据，若回灌评分会形成「等级高 → 被自动拦截 → 拦截加分 → 等级更高」的自强化回路，导致活跃用户永远无法降级。剔除后，用户变老实，等级随良好行为与时间衰减正常下降。
- **时间衰减**：评分按 24 小时半衰期指数衰减（频率计数按 1 小时半衰期）—— 历史污点会被时间冲淡，**不会只升不降**。
- **累计统计**：交易数、被拦次数、审核次数、深夜笔数、累计金额按**真实最终决策**全量累计（运营看到的是事实），在「用户风险画像」面板一眼可见。

### 在规则中引用风险等级

评估前系统会把 `user_risk_score` / `user_risk_level` 作为派生字段注入事件，规则条件可直接引用（与 `hour` 相同的方式），例如：

```json
{
  "description": "极高风险用户直接拒绝",
  "action": "REJECT",
  "priority": 95,
  "weight": 10,
  "conditions": [{"field": "user_risk_level", "op": "eq", "value": "CRITICAL"}]
}
```

或用评分阈值：`{"field": "user_risk_score", "op": "gte", "value": 60}`。

> 设计说明：风险等级规则只影响**当前交易的决策**，不影响评分本身（评分调节基于剔除风险规则后的行为决策）。因此 CRITICAL 用户被自动拦截期间，只要后续交易行为正常，评分仍会逐笔下降并随时间衰减，等级可以回落，不会锁死。

评分模型的行为（新用户起点、能升能降、时间衰减、频率信号、自动拦截不回灌评分等）由 `test_risk_score.py` 中的单元测试保证：

```bash
python3 test_risk_score.py
```

## REST API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/rules` | 规则列表 |
| POST | `/api/rules` | 新增规则 |
| PUT | `/api/rules/<id>` | 更新规则 |
| DELETE | `/api/rules/<id>` | 删除规则 |
| POST | `/api/rules/<id>/toggle` | 启用/停用规则 |
| POST | `/api/events` | 接入单笔交易事件 |
| POST | `/api/simulate` | 批量模拟交易事件 |
| GET | `/api/decisions?since_id=&limit=` | 查询决策流（支持增量拉取） |
| GET | `/api/users/risk?limit=&level=` | 用户风险画像列表（按评分降序，可按等级过滤） |
| GET | `/api/users/<user_id>/risk` | 单个用户风险画像（新用户返回中性默认值） |
| GET | `/api/alarms?status=OPEN` | 查询告警 |
| POST | `/api/alarms/<id>/resolve` | 处理告警 |
| GET | `/api/stats` | 统计信息（事件/决策/引擎） |

接入事件示例：

```bash
curl -X POST http://localhost:8002/api/events \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"u1001","amount":20000,"merchant":"shop_a"}'
```

响应：

```json
{
  "decision": "REJECT",
  "score": 15,
  "latency_ms": 0.086,
  "matched": [
    {"id": 1, "name": "单笔金额超限", "action": "REJECT", "priority": 100, "weight": 10}
  ]
}
```

## 数据模型

| 表 | 说明 |
|---|---|
| `rules` | 规则（`definition` 为 JSON 定义） |
| `events` | 交易事件 |
| `decisions` | 风控决策流（含决策时用户风险等级快照） |
| `alarms` | 告警（含 `dedup_key` 唯一键去重） |
| `user_risk` | 用户风险画像（评分、等级、累计统计） |
| `meta` | 规则配置版本号（热更新探测） |

## 核心难点实现

1. **规则引擎性能**：`engine/matcher.py` 将所有规则条件集合构建成二叉决策树，选择「出现频次最高」的条件作为分裂节点。共享条件只求值一次，命中集合沿路径剪枝得到，规则越多收益越明显。
2. **滑动窗口精确聚合**：`engine/window.py` 按 `(规则, 分组键)` 维护事件时间戳 `deque`，新事件到达时先剔除 `ts < now - window` 的过期元素再聚合，得到精确的滑动窗口结果。
3. **规则动态加载**：规则写入时自增 `meta.rules_version`；`engine/engine.py` 的后台守护线程轮询该版本号，变化即加锁重建规则与决策树，全程无需重启。
4. **告警去重**：告警以 `规则ID:用户ID:时间桶` 为 `dedup_key`，使用 `INSERT ... ON DUPLICATE KEY UPDATE hit_count = hit_count + 1` 实现窗口内去重累计，避免告警风暴。
5. **用户风险评分**：`risk_score.py` 将行为决策、金额、时段、频率折算为加减分项，并以 24h 半衰期对历史评分做指数衰减，保证等级能升能降；评分与等级作为派生字段注入事件（`user_risk_score` / `user_risk_level`），规则引擎零改动即可在条件中引用。引擎为引用风险字段的规则打上 `uses_risk_level` 标记，评分更新时剔除其影响，避免「等级高 → 自动拦截 → 拦截加分 → 等级更高」的自强化回路。
