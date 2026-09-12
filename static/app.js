/* 实时交易风控系统 —— 前端逻辑 */
const $ = (sel) => document.querySelector(sel);
const headers = { "Content-Type": "application/json" };

const USERS = ["u1001", "u1002", "u1003", "u1004", "u1005", "u1006"];
const MERCHANTS = ["shop_a", "shop_b", "shop_c", "shop_d", "black_shop", "crypto_exchange", "gambling_site"];
const AMOUNTS = [50, 120, 300, 900, 2000, 6000, 12000, 30000];

const state = {
  rules: [],
  lastDecisionId: 0,
  feed: [],
  simulating: false,
  timer: null,
  focusUser: "u1001",
};

function pick(arr) { return arr[Math.floor(Math.random() * arr.length)]; }
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
function fmt(n) { return Number(n).toLocaleString("zh-CN", { maximumFractionDigits: 2 }); }

async function api(path, opts) {
  const res = await fetch(path, opts);
  const text = await res.text();
  try {
    return JSON.parse(text);
  } catch (e) {
    return {};
  }
}

/* ---------------- 统计 ---------------- */
function renderStats(s) {
  const e = s.engine || {};
  $("#statsBar").innerHTML = `
    <div class="stat"><div class="num">${s.total_events}</div><div class="lbl">事件总数</div></div>
    <div class="stat pass"><div class="num">${s.decisions.PASS}</div><div class="lbl">通过</div></div>
    <div class="stat review"><div class="num">${s.decisions.REVIEW}</div><div class="lbl">人工审核</div></div>
    <div class="stat reject"><div class="num">${s.decisions.REJECT}</div><div class="lbl">拒绝</div></div>
    <div class="stat"><div class="num">${e.rule_count || 0}</div><div class="lbl">启用规则</div></div>
    <div class="stat reject"><div class="num">${s.open_alarms}</div><div class="lbl">未处理告警</div></div>
    <div class="stat"><div class="num">${e.node_count || 0}</div><div class="lbl">决策树节点</div></div>
  `;
}

/* ---------------- 规则 ---------------- */
function ruleSummary(r) {
  const d = r.definition || {};
  if (d.window) {
    const w = d.window;
    return `窗口聚合 ${w.agg}(${w.field || ""}) ${w.op} ${w.value}，窗口 ${w.seconds}s，按 ${w.group_by || "user_id"}`;
  }
  if (d.conditions && d.conditions.length) {
    return d.conditions.map((c) => `${c.field} ${c.op} ${JSON.stringify(c.value)}`).join(" 且 ");
  }
  return "无条件（匹配全部事件）";
}

function renderRules(rules) {
  const el = $("#ruleList");
  if (!rules.length) { el.innerHTML = '<div class="empty">暂无规则</div>'; return; }
  el.innerHTML = rules.map((r) => {
    const d = r.definition || {};
    const action = (d.action || "REVIEW").toLowerCase();
    return `<div class="rule-item">
      <div class="rule-top">
        <span class="rule-name">${esc(r.name)}</span>
        <span class="tag ${action}">${d.action}</span>
        <span class="tag">优先级 ${d.priority || 0}</span>
        <span class="tag ${r.enabled ? "" : "off"}">${r.enabled ? "启用" : "停用"}</span>
        <span class="rule-actions">
          <button class="btn" onclick="editRule(${r.id})">编辑</button>
          <button class="btn" onclick="toggleRule(${r.id})">${r.enabled ? "停用" : "启用"}</button>
          <button class="btn" onclick="removeRule(${r.id})">删除</button>
        </span>
      </div>
      <div class="rule-desc">${esc(ruleSummary(r))}</div>
    </div>`;
  }).join("");
}

function resetForm() {
  $("#ruleForm").classList.add("hidden");
  $("#ruleId").value = "";
  $("#formHint").textContent = "";
}

function showAddRule() {
  $("#ruleForm").classList.remove("hidden");
  $("#ruleId").value = "";
  $("#rName").value = "";
  $("#rAction").value = "REVIEW";
  $("#rPriority").value = "50";
  $("#rWeight").value = "5";
  $("#rDedup").value = "60";
  $("#rConditions").value = '[{"field":"amount","op":"gt","value":10000}]';
  $("#rWindow").value = "";
  $("#formHint").textContent = "";
}

function editRule(id) {
  const r = state.rules.find((x) => x.id === id);
  if (!r) return;
  const d = r.definition || {};
  $("#ruleForm").classList.remove("hidden");
  $("#ruleId").value = r.id;
  $("#rName").value = r.name;
  $("#rAction").value = d.action || "REVIEW";
  $("#rPriority").value = d.priority ?? 0;
  $("#rWeight").value = d.weight ?? 1;
  $("#rDedup").value = d.dedup_seconds ?? 60;
  $("#rConditions").value = JSON.stringify(d.conditions || []);
  $("#rWindow").value = d.window ? JSON.stringify(d.window) : "";
  $("#formHint").textContent = "";
}

function setHint(msg) { $("#formHint").textContent = msg; }

function collectRule() {
  const id = $("#ruleId").value;
  const name = $("#rName").value.trim();
  if (!name) { setHint("请填写规则名称"); return null; }
  let conditions;
  try {
    conditions = $("#rConditions").value.trim() ? JSON.parse($("#rConditions").value) : [];
  } catch (e) { setHint("条件 JSON 格式错误"); return null; }
  let windowDef = null;
  const wtext = $("#rWindow").value.trim();
  if (wtext) {
    try { windowDef = JSON.parse(wtext); }
    catch (e) { setHint("窗口 JSON 格式错误"); return null; }
  }
  const definition = {
    action: $("#rAction").value,
    priority: parseInt($("#rPriority").value) || 0,
    weight: parseInt($("#rWeight").value) || 1,
    conditions,
    dedup_seconds: parseInt($("#rDedup").value) || 60,
  };
  if (windowDef) definition.window = windowDef;
  return { id, name, definition };
}

async function saveRule(e) {
  e.preventDefault();
  const data = collectRule();
  if (!data) return;
  const body = { name: data.name, definition: data.definition };
  const res = data.id
    ? await api("/api/rules/" + data.id, { method: "PUT", headers, body: JSON.stringify(body) })
    : await api("/api/rules", { method: "POST", headers, body: JSON.stringify(body) });
  if (res.error) { setHint(res.error); return; }
  resetForm();
  await loadRules();
}

async function toggleRule(id) {
  await api("/api/rules/" + id + "/toggle", { method: "POST" });
  await loadRules();
}

async function removeRule(id) {
  if (!confirm("确认删除该规则？")) return;
  await api("/api/rules/" + id, { method: "DELETE" });
  await loadRules();
}

/* ---------------- 决策流 ---------------- */
function renderFlow() {
  const el = $("#flowList");
  if (!state.feed.length) {
    el.innerHTML = '<div class="empty">暂无决策，点击「开始模拟交易」或发送单笔测试交易</div>';
    return;
  }
  el.innerHTML = state.feed.map((d) => {
    const rules = (d.matched_rules || [])
      .map((r) => `<span class="chip">${esc(r.name)}</span>`).join("");
    return `<div class="flow-row">
      <span class="time">${esc(String(d.created_at).slice(11, 19))}</span>
      <span>${esc(d.user_id)}</span>
      <span class="mono">¥${fmt(d.amount)}</span>
      <span>${esc(d.merchant)}</span>
      <span><span class="badge ${d.decision}">${d.decision}</span></span>
      <span class="rules-hit">${rules || '<span class="hint">无</span>'}<span class="score">评分 ${d.score}</span></span>
      <span class="mono">${d.latency_ms}ms</span>
    </div>`;
  }).join("");
  el.scrollTop = el.scrollHeight;
}

/* ---------------- 告警 ---------------- */
function renderAlarms(alarms) {
  $("#alarmCount").textContent = `${alarms.length} 条未处理`;
  const el = $("#alarmList");
  if (!alarms.length) { el.innerHTML = '<div class="empty">暂无未处理告警</div>'; return; }
  el.innerHTML = alarms.map((a) => `
    <div class="alarm-item">
      <span class="lvl ${a.level}"></span>
      <div class="alarm-msg">
        <div class="m">${esc(a.message)}</div>
        <div class="meta">规则「${esc(a.rule_name)}」 · 用户 ${esc(a.user_id)} · ${esc(String(a.last_seen_at).slice(0, 19))}</div>
      </div>
      <span class="hit">触发 ${a.hit_count} 次</span>
      <button class="btn btn-sm" onclick="resolveAlarm(${a.id})">处理</button>
    </div>`).join("");
}

async function resolveAlarm(id) {
  await api("/api/alarms/" + id + "/resolve", { method: "POST" });
  await refresh();
}

/* ---------------- 模拟 / 手动 ---------------- */
function weightedMerchant() {
  return Math.random() < 0.15
    ? pick(["black_shop", "crypto_exchange", "gambling_site"])
    : pick(["shop_a", "shop_b", "shop_c", "shop_d"]);
}

function sendRandomEvent() {
  if (Math.random() < 0.35) state.focusUser = pick(USERS);
  const user = Math.random() < 0.6 ? state.focusUser : pick(USERS);
  const payload = { user_id: user, amount: pick(AMOUNTS), merchant: weightedMerchant() };
  fetch("/api/events", { method: "POST", headers, body: JSON.stringify(payload) }).catch(() => {});
}

function toggleSimulate() {
  if (state.simulating) {
    state.simulating = false;
    clearInterval(state.timer);
    $("#btnSimulate").textContent = "▶ 开始模拟交易";
    $("#btnSimulate").classList.remove("running");
  } else {
    state.simulating = true;
    state.timer = setInterval(sendRandomEvent, 250);
    $("#btnSimulate").textContent = "■ 停止模拟";
    $("#btnSimulate").classList.add("running");
  }
}

function burst() {
  const u = "u" + (1000 + Math.floor(Math.random() * 9000));
  for (let i = 0; i < 10; i++) {
    const payload = {
      user_id: u,
      amount: pick([6000, 12000, 30000, 50, 900]),
      merchant: weightedMerchant(),
    };
    fetch("/api/events", { method: "POST", headers, body: JSON.stringify(payload) }).catch(() => {});
  }
}

async function manualSend() {
  const payload = {
    user_id: $("#mUser").value.trim(),
    amount: parseFloat($("#mAmount").value) || 0,
    merchant: $("#mMerchant").value.trim(),
  };
  await api("/api/events", { method: "POST", headers, body: JSON.stringify(payload) });
}

/* ---------------- 轮询 ---------------- */
async function loadRules() {
  const rules = await api("/api/rules");
  if (Array.isArray(rules)) {
    state.rules = rules;
    renderRules(rules);
  }
}

async function refresh() {
  try {
    const [stats, alarms, decisions] = await Promise.all([
      api("/api/stats"),
      api("/api/alarms"),
      api("/api/decisions?since_id=" + state.lastDecisionId + "&limit=100"),
    ]);
    if (stats && stats.decisions) renderStats(stats);
    if (Array.isArray(alarms)) renderAlarms(alarms);
    if (Array.isArray(decisions) && decisions.length) {
      const maxId = Math.max(...decisions.map((d) => d.id));
      if (maxId > state.lastDecisionId) state.lastDecisionId = maxId;
      state.feed = state.feed.concat(decisions).slice(-200);
      renderFlow();
    }
  } catch (e) {
    console.error(e);
  }
}

/* ---------------- 事件绑定 & 启动 ---------------- */
document.addEventListener("DOMContentLoaded", () => {
  $("#btnSimulate").addEventListener("click", toggleSimulate);
  $("#btnBurst").addEventListener("click", burst);
  $("#btnManual").addEventListener("click", manualSend);
  $("#btnClearFeed").addEventListener("click", () => { state.feed = []; renderFlow(); });
  $("#btnAddRule").addEventListener("click", showAddRule);
  $("#btnCancelRule").addEventListener("click", resetForm);
  $("#ruleForm").addEventListener("submit", saveRule);

  loadRules();
  refresh();
  setInterval(refresh, 1000);
});
