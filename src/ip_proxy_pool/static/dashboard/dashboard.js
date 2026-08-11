import { DashboardApi, DashboardApiError } from "/dashboard/assets/api.js";
import { renderDistribution, renderLineChart } from "/dashboard/assets/charts.js";

const KEY_NAME = "ipPoolDashboardKey";
const MAX_REFRESH_SECONDS = 120;
let baseRefreshSeconds = 30;

const elements = {
  dashboard: document.querySelector("#dashboard"),
  content: document.querySelector("#dashboard-content"),
  authPanel: document.querySelector("#auth-panel"),
  authForm: document.querySelector("#auth-form"),
  apiKey: document.querySelector("#api-key"),
  authError: document.querySelector("#auth-error"),
  fatal: document.querySelector("#fatal-state"),
  refresh: document.querySelector("#refresh-button"),
  autoRefresh: document.querySelector("#auto-refresh"),
  domain: document.querySelector("#domain-select"),
  health: document.querySelector("#health-strip"),
  freshness: document.querySelector("#freshness-badge"),
  lastUpdated: document.querySelector("#last-updated"),
  kpis: document.querySelector("#kpi-grid"),
  rangeSelector: document.querySelector("#range-selector"),
  trendMessage: document.querySelector("#trend-message"),
  trendChart: document.querySelector("#trend-chart"),
  trendSummary: document.querySelector("#trend-summary"),
  qualitySample: document.querySelector("#quality-sample"),
  scoreDistribution: document.querySelector("#score-distribution"),
  stateDistribution: document.querySelector("#state-distribution"),
  latency: document.querySelector("#latency-summary"),
  sourceSample: document.querySelector("#source-sample"),
  sourceBody: document.querySelector("#source-table-body"),
  proxyDetails: document.querySelector("#proxy-details"),
  proxyFilters: document.querySelector("#proxy-filters"),
  proxyState: document.querySelector("#proxy-state"),
  proxyMinScore: document.querySelector("#proxy-min-score"),
  proxyMaxScore: document.querySelector("#proxy-max-score"),
  proxySource: document.querySelector("#proxy-source"),
  proxyRegion: document.querySelector("#proxy-table-region"),
  nextPage: document.querySelector("#next-page"),
};

let state = Object.freeze({
  key: null,
  domain: "",
  range: "24h",
  controller: null,
  timer: null,
  failureCount: 0,
  filters: Object.freeze({ state: "", minScore: 0, maxScore: 100, source: "" }),
  cursor: null,
});
let api = null;
let refreshPromise = null;

function updateState(patch) {
  state = Object.freeze({ ...state, ...patch });
  return state;
}

function formatInteger(value) {
  return Number(value || 0).toLocaleString("zh-CN");
}

function formatRate(value) {
  return value === null || value === undefined ? "—" : `${(value * 100).toFixed(1)}%`;
}

function formatLatency(value) {
  return value === null || value === undefined ? "—" : `${Math.round(value)} ms`;
}

function createTextElement(tag, text, className = "") {
  const element = document.createElement(tag);
  element.textContent = text;
  if (className) element.className = className;
  return element;
}

function clearTimer() {
  if (state.timer !== null) window.clearTimeout(state.timer);
  updateState({ timer: null });
}

function scheduleRefresh() {
  clearTimer();
  if (!state.key || !elements.autoRefresh.checked || document.hidden) return;
  const delay = Math.min(
    baseRefreshSeconds * (2 ** Math.max(0, state.failureCount - 1)),
    Math.max(baseRefreshSeconds, MAX_REFRESH_SECONDS),
  );
  elements.dashboard.dataset.refreshDelay = String(delay);
  elements.dashboard.dataset.polling = "active";
  const timer = window.setTimeout(() => refresh({ includeHistory: false }), delay * 1000);
  updateState({ timer });
}

function clearData() {
  elements.kpis.replaceChildren();
  elements.health.replaceChildren();
  elements.trendChart.replaceChildren();
  elements.trendSummary.textContent = "";
  elements.scoreDistribution.replaceChildren();
  elements.stateDistribution.replaceChildren();
  elements.latency.replaceChildren();
  elements.sourceBody.replaceChildren();
  elements.proxyRegion.replaceChildren();
  elements.nextPage.hidden = true;
}

function logout(message) {
  clearTimer();
  state.controller?.abort();
  sessionStorage.removeItem(KEY_NAME);
  api = null;
  updateState({ key: null, controller: null, failureCount: 0, cursor: null });
  clearData();
  elements.content.hidden = true;
  elements.authPanel.hidden = false;
  elements.fatal.hidden = true;
  elements.authError.textContent = message;
  elements.dashboard.setAttribute("aria-busy", "false");
  elements.apiKey.focus();
}

function renderHealth(summary) {
  elements.health.replaceChildren();
  const statusLabels = { healthy: "正常", stale: "延迟", down: "离线" };
  const entries = [
    ["API", summary.api_status],
    ["Redis", summary.redis_status],
    ["采集", summary.collector.status],
    ["检测", summary.checker.status],
  ];
  for (const [label, status] of entries) {
    const pill = createTextElement(
      "span",
      `${label} ${statusLabels[status] || status}`,
      "health-pill",
    );
    pill.dataset.status = status;
    elements.health.append(pill);
  }
}

function renderKpis(summary) {
  const cards = [
    ["代理总数", formatInteger(summary.total), "池内记录", "neutral", "proxy-total"],
    ["可用代理", formatInteger(summary.available), "当前可分配", "healthy", "proxy-available"],
    ["主可用率", formatRate(summary.availability_rate), `占总池 ${formatRate(summary.available_pool_share)}`, "healthy", "availability-rate"],
    ["高质量", formatInteger(summary.high_quality), "可用且评分 ≥ 90", "healthy", "high-quality"],
    ["待检测", formatInteger(summary.due), "调度队列", "warning", "due-count"],
    ["隔离", formatInteger(summary.quarantined), "等待恢复", "warning", "quarantined-count"],
  ];
  elements.kpis.replaceChildren();
  for (const [label, value, meta, tone, testId] of cards) {
    const card = document.createElement("article");
    card.className = "kpi-card";
    card.dataset.tone = tone;
    const labelNode = createTextElement("div", label, "kpi-label");
    const valueNode = createTextElement("div", value, "kpi-value");
    valueNode.dataset.testid = testId;
    card.append(labelNode, valueNode, createTextElement("div", meta, "kpi-meta"));
    elements.kpis.append(card);
  }
}

function renderFreshness(summary) {
  const seconds = summary.freshness_seconds;
  elements.freshness.className = "badge";
  if (seconds === null || seconds === undefined) {
    elements.freshness.classList.add("badge-neutral");
    elements.freshness.textContent = "等待历史快照";
  } else if (seconds > 1800) {
    elements.freshness.classList.add("badge-danger");
    elements.freshness.textContent = "监测中断";
  } else if (seconds > 900) {
    elements.freshness.classList.add("badge-warning");
    elements.freshness.textContent = "数据延迟";
  } else {
    elements.freshness.classList.add("badge-healthy");
    elements.freshness.textContent = "数据新鲜";
  }
  elements.lastUpdated.textContent = `更新于 ${new Date(summary.observed_at).toLocaleTimeString("zh-CN")}`;
}

function renderSummary(summary) {
  baseRefreshSeconds = Number(summary.refresh_seconds || 30);
  renderHealth(summary);
  renderKpis(summary);
  renderFreshness(summary);
  elements.content.hidden = false;
  elements.authPanel.hidden = true;
  elements.fatal.hidden = true;
}

function renderQuality(value) {
  elements.qualitySample.textContent = value.partial
    ? `基于部分样本 · ${formatInteger(value.scanned)} 条`
    : `${formatInteger(value.scanned)} 条记录`;
  renderDistribution(elements.scoreDistribution, value.score_buckets, {
    title: "评分分布",
    labels: { low: "低于 60", watch: "60–79", usable: "80–89", high: "90–100" },
    colors: { low: "#d94f5c", watch: "#c58a27", usable: "#2f8ca3", high: "#168c6e" },
  });
  renderDistribution(elements.stateDistribution, {
    candidate: value.candidate,
    available: value.available,
    degraded: value.degraded,
    quarantined: value.quarantined,
  }, {
    title: "状态分布",
    labels: { candidate: "候选", available: "可用", degraded: "降级", quarantined: "隔离" },
    colors: { candidate: "#84919a", available: "#168c6e", degraded: "#c58a27", quarantined: "#d94f5c" },
  });
  elements.latency.replaceChildren();
  for (const [label, item] of [
    ["平均", value.latency.average_ms],
    ["P50", value.latency.p50_ms],
    ["P95", value.latency.p95_ms],
  ]) {
    const wrapper = document.createElement("div");
    wrapper.className = "latency-item";
    wrapper.append(createTextElement("dt", label), createTextElement("dd", formatLatency(item)));
    elements.latency.append(wrapper);
  }
}

function renderSources(value) {
  elements.sourceSample.textContent = value.partial
    ? `部分样本 · ${formatInteger(value.scanned)}`
    : `${formatInteger(value.scanned)} 条记录`;
  elements.sourceBody.replaceChildren();
  if (!value.items.length) {
    const row = document.createElement("tr");
    const cell = createTextElement("td", "暂无来源数据");
    cell.colSpan = 4;
    row.append(cell);
    elements.sourceBody.append(row);
    return;
  }
  for (const item of value.items) {
    const row = document.createElement("tr");
    row.append(
      createTextElement("td", item.name),
      createTextElement("td", `${formatInteger(item.available)} / ${formatInteger(item.tested)}`),
      createTextElement("td", formatRate(item.availability_rate)),
      createTextElement("td", item.average_score === null ? "—" : item.average_score.toFixed(1)),
    );
    elements.sourceBody.append(row);
  }
}

function renderHistory(value) {
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  renderLineChart(elements.trendChart, value.points, {
    resolution: value.resolution,
    reducedMotion,
    labels: [
      { key: "total", label: "总量", color: "#147d92", rate: false },
      { key: "available", label: "可用", color: "#168c6e", rate: false },
      { key: "availability_rate", label: "主可用率", color: "#a86f16", rate: true },
    ],
  });
  const latest = value.points.at(-1);
  elements.trendSummary.textContent = latest
    ? `最新快照：总量 ${formatInteger(latest.total)}，可用 ${formatInteger(latest.available)}，主可用率 ${formatRate(latest.availability_rate)}。缺失时间桶按断点显示。`
    : "当前时间范围暂无快照，图表不会用零值填补缺口。";
  elements.trendMessage.textContent = `粒度 ${value.resolution === "1h" ? "1 小时" : "5 分钟"} · ${value.points.length} 个数据点`;
}

function panelUnavailable(panel, message) {
  panel.replaceChildren(createTextElement("p", message, "empty-state"));
}

function firstError(results) {
  return results.find((item) => item.status === "rejected")?.reason || null;
}

function handleRequestError(error) {
  if (error?.name === "AbortError") return;
  if (error instanceof DashboardApiError && error.status === 401) {
    logout("API Key 无效或已失效，请重新输入。");
    return;
  }
  const failureCount = state.failureCount + 1;
  updateState({ failureCount });
  elements.fatal.hidden = false;
  if (error instanceof DashboardApiError && error.status === 429) {
    elements.fatal.textContent = `请求过于频繁，请在 ${error.retryAfter || 30} 秒后重试。`;
  } else {
    elements.fatal.textContent = "监测服务暂不可用，页面将自动重试。";
    elements.content.hidden = true;
  }
  scheduleRefresh();
}

async function refresh({ includeHistory = false } = {}) {
  if (!api || refreshPromise) return refreshPromise;
  const controller = new AbortController();
  updateState({ controller });
  elements.dashboard.setAttribute("aria-busy", "true");
  const domain = state.domain || null;
  refreshPromise = (async () => {
    const requests = [api.summary(domain, controller.signal)];
    if (domain) {
      requests.push(api.quality(domain, controller.signal));
      requests.push(api.sources(domain, 20, controller.signal));
    }
    if (includeHistory) requests.push(api.history(domain, state.range, controller.signal));
    const results = await Promise.allSettled(requests);
    const error = firstError(results);
    if (error instanceof DashboardApiError && error.status === 401) throw error;
    if (results[0].status === "rejected") throw results[0].reason;
    renderSummary(results[0].value);
    let index = 1;
    if (domain) {
      const qualityResult = results[index++];
      const sourcesResult = results[index++];
      if (qualityResult.status === "fulfilled") renderQuality(qualityResult.value);
      else panelUnavailable(elements.scoreDistribution, "质量数据暂不可用");
      if (sourcesResult.status === "fulfilled") renderSources(sourcesResult.value);
      else panelUnavailable(elements.sourceBody, "来源数据暂不可用");
    }
    if (includeHistory) {
      const historyResult = results[index];
      if (historyResult.status === "fulfilled") renderHistory(historyResult.value);
      else panelUnavailable(elements.trendMessage, "历史数据暂不可用");
    }
    updateState({ failureCount: 0 });
    scheduleRefresh();
  })().catch(handleRequestError).finally(() => {
    if (state.controller === controller) updateState({ controller: null });
    elements.dashboard.setAttribute("aria-busy", "false");
    refreshPromise = null;
  });
  return refreshPromise;
}

async function loadDomains() {
  const controller = new AbortController();
  const value = await api.domains(controller.signal);
  elements.domain.replaceChildren();
  const globalOption = document.createElement("option");
  globalOption.value = "";
  globalOption.textContent = "全局";
  elements.domain.append(globalOption);
  for (const domain of value.domains) {
    const option = document.createElement("option");
    option.value = domain;
    option.textContent = domain;
    elements.domain.append(option);
  }
  const selected = value.domains[0] || "";
  elements.domain.value = selected;
  elements.domain.disabled = false;
  updateState({ domain: selected });
}

async function login(key) {
  clearData();
  elements.authError.textContent = "";
  sessionStorage.setItem(KEY_NAME, key);
  api = new DashboardApi(key);
  updateState({ key, failureCount: 0, range: "24h", cursor: null });
  try {
    await loadDomains();
    await refresh({ includeHistory: true });
  } catch (error) {
    handleRequestError(error);
  }
}

function setRange(range) {
  updateState({ range });
  for (const button of elements.rangeSelector.querySelectorAll("button[data-range]")) {
    button.setAttribute("aria-pressed", String(button.dataset.range === range));
  }
  state.controller?.abort();
  refreshPromise = null;
  refresh({ includeHistory: true });
}

function proxyTable(items) {
  const table = document.createElement("table");
  const head = document.createElement("thead");
  const headerRow = document.createElement("tr");
  for (const label of ["地址", "状态", "评分", "延迟", "来源", "最近检测", "下次检测"]) {
    headerRow.append(createTextElement("th", label));
  }
  head.append(headerRow);
  const body = document.createElement("tbody");
  for (const item of items) {
    const row = document.createElement("tr");
    row.append(
      createTextElement("td", item.endpoint),
      createTextElement("td", item.state),
      createTextElement("td", String(item.score)),
      createTextElement("td", formatLatency(item.latency_ewma_ms)),
      createTextElement("td", item.source_names.join(", ")),
      createTextElement("td", item.last_checked_at ? new Date(item.last_checked_at).toLocaleString("zh-CN") : "—"),
      createTextElement("td", new Date(item.next_check_at).toLocaleString("zh-CN")),
    );
    body.append(row);
  }
  table.append(head, body);
  return table;
}

async function loadProxies({ cursor = null } = {}) {
  if (!api || !state.domain) {
    panelUnavailable(elements.proxyRegion, "请选择具体域名后查看代理明细");
    return;
  }
  try {
    const value = await api.proxies(state.domain, state.filters, cursor, undefined);
    elements.proxyRegion.replaceChildren(
      value.items.length ? proxyTable(value.items) : createTextElement("p", "没有符合筛选条件的代理", "empty-state"),
    );
    updateState({ cursor: value.next_cursor });
    elements.nextPage.hidden = !value.next_cursor;
  } catch (error) {
    handleRequestError(error);
  }
}

elements.authForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const key = elements.apiKey.value.trim();
  if (key) login(key);
});

elements.refresh.addEventListener("click", () => refresh({ includeHistory: false }));
elements.autoRefresh.addEventListener("change", scheduleRefresh);
elements.domain.addEventListener("change", () => {
  updateState({ domain: elements.domain.value, cursor: null });
  state.controller?.abort();
  refreshPromise = null;
  refresh({ includeHistory: true });
  if (elements.proxyDetails.open) loadProxies();
});
elements.rangeSelector.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-range]");
  if (button) setRange(button.dataset.range);
});
elements.proxyDetails.addEventListener("toggle", () => {
  if (elements.proxyDetails.open && !elements.proxyRegion.hasChildNodes()) loadProxies();
});
elements.proxyFilters.addEventListener("submit", (event) => {
  event.preventDefault();
  const minScore = Number(elements.proxyMinScore.value);
  const maxScore = Number(elements.proxyMaxScore.value);
  if (minScore > maxScore) {
    panelUnavailable(elements.proxyRegion, "最低分不能高于最高分");
    return;
  }
  updateState({
    filters: Object.freeze({
      state: elements.proxyState.value,
      minScore,
      maxScore,
      source: elements.proxySource.value.trim(),
    }),
    cursor: null,
  });
  loadProxies();
});
elements.nextPage.addEventListener("click", () => loadProxies({ cursor: state.cursor }));
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    clearTimer();
    elements.dashboard.dataset.polling = "paused";
  } else if (state.key) {
    refresh({ includeHistory: false });
  }
});

const savedKey = sessionStorage.getItem(KEY_NAME);
if (savedKey) {
  elements.apiKey.value = savedKey;
  login(savedKey);
} else {
  elements.dashboard.setAttribute("aria-busy", "false");
}
