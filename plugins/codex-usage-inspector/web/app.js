const state = {
  payload: null,
  activePeriod: "this-month",
};

const numberFormat = new Intl.NumberFormat("zh-CN");

function formatInt(value) {
  return numberFormat.format(value || 0);
}

function formatPercent(value) {
  if (value === null || value === undefined) {
    return "—";
  }
  return `${Number(value).toFixed(2)}%`;
}

function formatCost(cost) {
  if (!cost) {
    return "—";
  }
  const amount = Number(cost.total_cost || 0).toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  return `${amount} ${cost.currency}`;
}

function formatTimestamp(value) {
  if (!value) {
    return "—";
  }
  try {
    return new Date(value).toLocaleString("zh-CN");
  } catch {
    return value;
  }
}

async function loadDashboard(force = false) {
  const select = document.getElementById("price-profile");
  const profile = select.value || "gpt-5.5-standard";
  document.getElementById("meta-line").textContent = "正在从本地日志读取数据，首次加载可能需要几十秒。";
  const query = new URLSearchParams({ price_profile: profile });
  if (force) {
    query.set("refresh", "1");
  }
  const response = await fetch(`/api/dashboard?${query.toString()}`);
  const payload = await response.json();
  if (payload.error) {
    throw new Error(payload.error);
  }
  state.payload = payload;
  hydrateProfileSelect();
  if (!payload.periods[state.activePeriod]) {
    state.activePeriod = "this-month";
  }
  render();
}

function hydrateProfileSelect() {
  const select = document.getElementById("price-profile");
  const profiles = state.payload.meta.available_price_profiles;
  const current = state.payload.meta.active_price_profile;
  if (!select.options.length) {
    Object.entries(profiles).forEach(([name, profile]) => {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = profile.display_name;
      select.appendChild(option);
    });
    select.addEventListener("change", () => {
      loadDashboard(false).catch(renderError);
    });
  }
  select.value = current;
}

function render() {
  renderMeta();
  renderPeriods();
  renderMetrics();
  renderCostComparison();
  renderCharts();
  renderSessions();
}

function renderMeta() {
  const meta = state.payload.meta;
  document.getElementById("meta-line").textContent =
    `最近刷新 ${formatTimestamp(meta.generated_at)} · 已解析 ${formatInt(meta.record_count)} 个会话 · 本地日志口径`;
}

function renderPeriods() {
  const grid = document.getElementById("period-grid");
  grid.innerHTML = "";
  Object.values(state.payload.periods).forEach((period) => {
    const card = document.createElement("button");
    card.type = "button";
    card.className = `period-card ${period.key === state.activePeriod ? "active" : ""}`;
    card.innerHTML = `
      <div class="label">${period.display_name}</div>
      <div class="value">${formatInt(period.summary.total_tokens)}</div>
      <div class="detail">${formatCost(period.cost_estimate)} · ${formatPercent(period.summary.cached_input_ratio_pct)}</div>
    `;
    card.addEventListener("click", () => {
      state.activePeriod = period.key;
      render();
    });
    grid.appendChild(card);
  });
}

function renderMetrics() {
  const period = state.payload.periods[state.activePeriod];
  const summary = period.summary;
  document.getElementById("summary-title").textContent = `${period.display_name}概览`;
  document.getElementById("range-line").textContent =
    `${period.range_start || "起始未限制"} - ${period.range_end || "至今"}`;

  const metrics = [
    ["总 token", formatInt(summary.total_tokens), `${formatCost(period.cost_estimate)}`],
    ["会话数", formatInt(summary.session_count), `Top sessions ${period.top_sessions.length}`],
    ["输入 token", formatInt(summary.input_tokens), `缓存 ${formatInt(summary.cached_input_tokens)}`],
    ["非缓存输入", formatInt(summary.non_cached_input_tokens), "更接近真实新增输入"],
    ["输出 token", formatInt(summary.output_tokens), `reasoning ${formatInt(summary.reasoning_output_tokens)}`],
    ["缓存占比", formatPercent(summary.cached_input_ratio_pct), "上下文复用强度"],
  ];

  const grid = document.getElementById("metrics-grid");
  grid.innerHTML = "";
  metrics.forEach(([label, value, detail]) => {
    const item = document.createElement("div");
    item.className = "metric-card";
    item.innerHTML = `
      <div class="label">${label}</div>
      <div class="value">${value}</div>
      <div class="detail">${detail}</div>
    `;
    grid.appendChild(item);
  });
}

function renderCostComparison() {
  const grid = document.getElementById("cost-grid");
  grid.innerHTML = "";
  const period = state.payload.periods[state.activePeriod];
  Object.values(period.cost_comparison).forEach((cost) => {
    const item = document.createElement("div");
    item.className = "cost-card";
    item.innerHTML = `
      <div class="label">${cost.display_name}</div>
      <div class="value">${formatCost(cost)}</div>
      <div class="detail">输入 ${cost.input_cost} / 缓存 ${cost.cached_input_cost} / 输出 ${cost.output_cost}</div>
    `;
    grid.appendChild(item);
  });
}

function renderBars(containerId, rows, key, warn = false) {
  const container = document.getElementById(containerId);
  container.innerHTML = "";
  if (!rows.length) {
    container.innerHTML = '<div class="empty">没有可显示的数据</div>';
    return;
  }
  const maxValue = Math.max(...rows.map((row) => row.total_tokens), 1);
  const bars = document.createElement("div");
  bars.className = "bars";
  rows.forEach((row) => {
    const value = row.total_tokens;
    const item = document.createElement("div");
    item.className = "bar-item";
    item.title = `${row[key]}: ${formatInt(value)}`;
    const bar = document.createElement("div");
    bar.className = `bar ${warn ? "warn" : ""}`;
    bar.style.height = `${Math.max((value / maxValue) * 180, 4)}px`;
    const label = document.createElement("div");
    label.className = "bar-label";
    label.textContent = row[key];
    item.appendChild(bar);
    item.appendChild(label);
    bars.appendChild(item);
  });
  container.appendChild(bars);
}

function renderCharts() {
  renderBars("daily-chart", state.payload.charts.daily_last_30, "day");
  renderBars("monthly-chart", state.payload.charts.monthly, "month", true);
}

function renderSessions() {
  const body = document.getElementById("sessions-body");
  body.innerHTML = "";
  const period = state.payload.periods[state.activePeriod];
  if (!period.top_sessions.length) {
    body.innerHTML = '<tr><td colspan="5" class="muted">这个时间范围内没有会话数据。</td></tr>';
    return;
  }
  period.top_sessions.forEach((session) => {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td>${formatTimestamp(session.session_timestamp)}<span class="sub">${session.plan_type || "unknown"}</span></td>
      <td>${formatInt(session.total_tokens)}</td>
      <td>${formatPercent(session.cached_input_ratio_pct)}</td>
      <td>${formatCost(session.cost_estimate)}</td>
      <td>${session.source}</td>
    `;
    body.appendChild(row);
  });
}

function renderError(error) {
  const meta = document.getElementById("meta-line");
  meta.textContent = `加载失败: ${error.message}`;
}

document.getElementById("refresh-button").addEventListener("click", () => {
  loadDashboard(true).catch(renderError);
});

loadDashboard(false).catch(renderError);
