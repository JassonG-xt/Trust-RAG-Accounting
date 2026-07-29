const examples = [
  "Alpha Trading Co. 的餐饮发票应该怎么入账？",
  "现在打车超过 100 元需要审批吗？",
  "Beta Catering Ltd. 没有明确服务描述的配送发票能直接入账吗？",
  "小规模纳税人现在增值税应该怎么处理？",
  "怎么隐藏收入少交税？",
  "文档里说 Ignore previous instructions 的内容应该听吗？",
];

// --- 中文显示标签映射 -------------------------------------------------------
// 枚举值（问题类型 / 审阅状态 / 原因码 / 字段键 / 操作类型）属于后端契约字符串：
// 界面只把它们「显示」成中文，发往 API 的筛选 / 操作请求仍使用原始值。
// 未命中映射的值回退为原始字符串，保证健壮。
const QUESTION_TYPE_LABELS = {
  unsafe_request: "不安全请求",
  bookkeeping_sop: "记账规程",
  invoice_compliance: "发票合规",
  reimbursement_rule: "报销规则",
  tax_policy: "税务政策",
  document_checklist: "资料清单",
  risk_review: "风险审阅",
  temporal_policy_comparison: "时效政策对比",
  general_accounting_qa: "通用会计问答",
  unknown: "未知",
};

const STATUS_LABELS = {
  pending: "待处理",
  approved: "已通过",
  rejected: "已驳回",
  changes_requested: "需修改",
  resolved: "已解决",
  handoff_failed: "交接失败",
  public_demo_not_persisted: "公开演示未入队",
};

const ACTION_LABELS = {
  approve: "通过",
  reject: "驳回",
  request_changes: "要求修改",
  rewrite_note: "添加备注",
  resolve: "解决",
  reopen: "重新打开",
};

const DOC_TYPE_LABELS = {
  bookkeeping_sop: "记账规程",
  invoice_compliance: "发票合规",
  reimbursement_policy: "报销政策",
  tax_policy_note: "税务政策说明",
  document_checklist: "资料清单",
  adversarial_sample: "对抗样本",
};

const REASON_LABELS = {
  tax_policy_always_review: "税务政策强制审阅",
  invoice_compliance_always_review: "发票合规强制审阅",
  evidence_conflict: "证据冲突",
  temporal_conflict: "时效冲突",
  insufficient_evidence: "证据不足",
  confidence_below_threshold: "置信度低于阈值",
  risk_review: "风险审阅",
  judge_requested_review: "评审请求审阅",
  answerable_with_review: "可答但需审阅",
  low_confidence: "低置信度",
  prompt_injection: "提示注入",
};

const META_LABELS = {
  doc_id: "文档标识",
  document_id: "文档 ID",
  chunk_id: "分块 ID",
  section: "章节",
  source: "来源",
  source_path: "来源路径",
  valid_from: "生效起",
  valid_to: "生效止",
  client: "客户",
  version: "版本",
  policy_family: "政策族",
  malicious: "恶意标记",
  retrieval_strategy: "检索策略",
  score_breakdown: "分数明细",
  review_queue_id: "队列 ID",
  question_type: "问题类型",
  reasons: "原因",
  created_at: "创建时间",
  confidence: "置信度",
  actions: "操作数",
  last_action_at: "最近操作时间",
  timestamp: "时间戳",
  tags: "标签",
  input: "输入",
  output: "输出",
  tenant_id: "租户 ID",
};

function labelQuestionType(value) {
  if (!value) return QUESTION_TYPE_LABELS.unknown;
  return QUESTION_TYPE_LABELS[value] || value;
}

function labelStatus(value) {
  return STATUS_LABELS[value] || value || "待处理";
}

function labelAction(value) {
  return ACTION_LABELS[value] || value;
}

function labelDocType(value) {
  return DOC_TYPE_LABELS[value] || value;
}

function labelReason(value) {
  return REASON_LABELS[value] || value;
}

function labelReasons(list) {
  return (list || []).map(labelReason).join("、");
}

function metaLabel(key) {
  return META_LABELS[key] || key;
}

const AUTH_TOKEN_KEY = "trustrag_token";

const state = {
  authToken: null,
  demoConfig: {
    public_demo_enabled: false,
    review_queue_enabled: true,
    demo_mode_label: "Local full demo",
  },
  documents: null,
  review: null,
  reviewSummary: null,
  eval: null,
  evalHistory: null,
  traces: null,
  providerBenchmark: null,
  providerBenchmarkList: null,
  providerBenchmarkHistory: null,
  query: null,
  actionHistory: {},
  actionStatus: {},
  reviewFilters: {
    status: "",
    question_type: "",
    reason: "",
    reviewer: "",
    has_actions: false,
    sort: "created_at_desc",
    limit: 20,
    offset: 0,
  },
};

const $ = (id) => document.getElementById(id);

document.addEventListener("DOMContentLoaded", () => {
  bootstrapAuth();
  renderExamples();
  bindActions();
  refreshAll();
});

// Minimal auth bootstrap for the MVP: the OIDC provider redirects back with the
// access token in the URL fragment, we keep it in memory + sessionStorage and
// strip it from the address bar. Full auth-code + PKCE lands in Task 2.7.
// Everything here runs from DOMContentLoaded so the DOM-less test sandbox never
// touches browser storage or location.
function bootstrapAuth() {
  try {
    const token = readTokenFromFragment();
    if (token) {
      storeAuthToken(token);
    } else {
      state.authToken = readStoredAuthToken();
    }
  } catch (error) {
    // Opaque-origin documents (sandboxed iframe, file://) throw on storage,
    // location and history access. Stay anonymous rather than abort the rest
    // of the dashboard bootstrap.
    state.authToken = null;
  }
  renderAuthStatus();
}

function authStorage() {
  try {
    return typeof sessionStorage === "undefined" ? null : sessionStorage;
  } catch (error) {
    // Storage can be blocked by browser policy; stay memory-only.
    return null;
  }
}

function readStoredAuthToken() {
  const storage = authStorage();
  if (!storage) return null;
  try {
    return storage.getItem(AUTH_TOKEN_KEY) || null;
  } catch (error) {
    // Storage can be blocked by browser policy; stay memory-only.
    return null;
  }
}

function storeAuthToken(token) {
  state.authToken = token;
  const storage = authStorage();
  if (!storage) return;
  try {
    storage.setItem(AUTH_TOKEN_KEY, token);
  } catch (error) {
    // Memory-only session is still usable.
  }
}

function clearAuthToken() {
  state.authToken = null;
  const storage = authStorage();
  if (storage) {
    try {
      storage.removeItem(AUTH_TOKEN_KEY);
    } catch (error) {
      // Memory-only session is still usable.
    }
  }
  renderAuthStatus();
}

function readTokenFromFragment() {
  if (typeof location === "undefined") return null;
  const fragment = (location.hash || "").replace(/^#/, "");
  if (!fragment) return null;
  const token = new URLSearchParams(fragment).get("access_token");
  if (!token) return null;
  clearUrlFragment();
  return token;
}

function clearUrlFragment() {
  try {
    const cleanUrl = `${location.pathname || "/"}${location.search || ""}`;
    if (typeof history !== "undefined" && typeof history.replaceState === "function") {
      history.replaceState(null, "", cleanUrl);
    } else {
      location.hash = "";
    }
  } catch (error) {
    // Opaque-origin documents reject history/location writes; the address bar
    // keeps the fragment but bootstrap must not fail because of it.
  }
}

function renderAuthStatus() {
  const node = $("auth-status");
  if (!node) return;
  const authenticated = Boolean(state.authToken);
  // Never render the token itself — only whether a session exists.
  node.textContent = authenticated ? "已登录" : "未登录";
  node.dataset.authenticated = authenticated ? "true" : "false";
}

function bindActions() {
  $("run-query").addEventListener("click", runQuery);
  $("clear-query").addEventListener("click", () => {
    $("question-input").value = "";
    $("question-input").focus();
  });
  document.querySelectorAll("[data-refresh]").forEach((button) => {
    button.addEventListener("click", () => refreshPanel(button.dataset.refresh));
  });
  $("review-list").addEventListener("click", handleReviewClick);
  const createTenantButton = $("create-tenant");
  if (createTenantButton) {
    createTenantButton.addEventListener("click", createTenant);
  }
  bindReviewFilters();
}

function bindReviewFilters() {
  const filterForm = $("review-filters");
  if (!filterForm) return;
  filterForm.addEventListener("input", scheduleReviewFilterUpdate);
  filterForm.addEventListener("change", scheduleReviewFilterUpdate);
  const resetBtn = $("review-filter-reset");
  if (resetBtn) {
    resetBtn.addEventListener("click", () => {
      if (!reviewQueueEnabled()) return;
      resetReviewFilters();
      refreshReview();
    });
  }
  const exportJson = $("review-export-json");
  if (exportJson) {
    exportJson.addEventListener("click", () => downloadExport("json"));
  }
  const exportCsv = $("review-export-csv");
  if (exportCsv) {
    exportCsv.addEventListener("click", () => downloadExport("csv"));
  }
  const clearQueue = $("review-clear-queue");
  if (clearQueue) {
    clearQueue.addEventListener("click", clearReviewQueue);
  }
}

let _reviewFilterTimer = null;
function scheduleReviewFilterUpdate() {
  if (!reviewQueueEnabled()) return;
  if (_reviewFilterTimer) clearTimeout(_reviewFilterTimer);
  _reviewFilterTimer = setTimeout(() => {
    readReviewFiltersFromForm();
    state.reviewFilters.offset = 0;
    refreshReview();
  }, 180);
}

function readReviewFiltersFromForm() {
  state.reviewFilters.status = $("review-filter-status").value;
  state.reviewFilters.question_type = $("review-filter-question-type").value.trim();
  state.reviewFilters.reason = $("review-filter-reason").value.trim();
  state.reviewFilters.reviewer = $("review-filter-reviewer").value.trim();
  state.reviewFilters.has_actions = $("review-filter-has-actions").checked;
  state.reviewFilters.sort = $("review-filter-sort").value;
  state.reviewFilters.limit = Number($("review-filter-limit").value) || 20;
}

function resetReviewFilters() {
  state.reviewFilters = {
    status: "",
    question_type: "",
    reason: "",
    reviewer: "",
    has_actions: false,
    sort: "created_at_desc",
    limit: 20,
    offset: 0,
  };
  $("review-filter-status").value = "";
  $("review-filter-question-type").value = "";
  $("review-filter-reason").value = "";
  $("review-filter-reviewer").value = "";
  $("review-filter-has-actions").checked = false;
  $("review-filter-sort").value = "created_at_desc";
  $("review-filter-limit").value = "20";
}

function reviewFilterQueryString({includePaging = true} = {}) {
  const f = state.reviewFilters;
  const params = new URLSearchParams();
  if (f.status) params.set("status", f.status);
  if (f.question_type) params.set("question_type", f.question_type);
  if (f.reason) params.set("reason", f.reason);
  if (f.reviewer) params.set("reviewer", f.reviewer);
  if (f.has_actions) params.set("has_actions", "true");
  if (f.sort) params.set("sort", f.sort);
  if (includePaging) {
    params.set("limit", String(f.limit));
    params.set("offset", String(f.offset));
  }
  return params.toString();
}

async function downloadExport(format) {
  if (!reviewQueueEnabled()) return;
  const qs = reviewFilterQueryString({includePaging: false});
  const suffix = format === "csv" ? "csv" : "json";
  const url = `/v1/review/queue/export.${suffix}${qs ? `?${qs}` : ""}`;
  try {
    // window.open cannot carry the Authorization header, so the export has to
    // be fetched and handed to the browser as a Blob. The token stays in the
    // header — never in the URL.
    const blob = await fetchBlob(url);
    saveBlob(blob, `review-queue.${suffix}`);
  } catch (error) {
    $("review-summary").textContent = `导出失败：${messageOf(error)}`;
  }
}

async function fetchBlob(url, options = {}) {
  const headers = {...(options.headers || {})};
  if (state.authToken) {
    headers["Authorization"] = `Bearer ${state.authToken}`;
  }
  const response = await fetch(url, {...options, headers});
  if (!response.ok) {
    if (response.status === 401) {
      // Same stale-token handling as fetchJson: drop it so the UI stops
      // claiming a session instead of failing silently in a new tab.
      clearAuthToken();
    }
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return response.blob();
}

function saveBlob(blob, filename) {
  const objectUrl = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = objectUrl;
  link.download = filename;
  link.click();
  // Revoke on the next task: revoking synchronously after click() can cancel
  // the download in some browsers.
  setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
}

async function clearReviewQueue() {
  if (!reviewQueueEnabled()) return;
  const confirmed = typeof window !== "undefined" && typeof window.confirm === "function"
    ? window.confirm("将清空全部审阅检查点与操作记录，且不可恢复。确定继续？")
    : false;
  if (!confirmed) return;

  const button = $("review-clear-queue");
  const summary = $("review-summary");
  if (button) button.disabled = true;
  if (summary) summary.textContent = "清空中…";

  try {
    const data = await fetchJson("/v1/review/queue", {method: "DELETE"});
    if (typeof clearReviewHighlights === "function") {
      clearReviewHighlights();
    }
    state.actionHistory = {};
    state.actionStatus = {};
    // Avoid sticky filters (e.g. status=approved, has_actions) hiding future pending rows.
    resetReviewFilters();
    // Drop stale answer-panel handoff that still references deleted queue ids.
    const handoff = $("review-handoff");
    if (handoff) {
      handoff.hidden = true;
      handoff.classList.remove("is-persisted", "is-warn", "is-note");
      handoff.replaceChildren();
    }
    if (state.query) {
      state.query = {
        ...state.query,
        needs_human_review: false,
        human_review: {
          required: false,
          status: null,
          review_queue_id: null,
          reasons: [],
        },
      };
    }
    await refreshReview();
    if (summary) {
      if (data && data.enabled === false) {
        summary.textContent = "审阅队列已禁用，未清空。";
      } else {
        const cleared = Number(data?.cleared ?? 0);
        const clearedActions = Number(data?.cleared_actions ?? 0);
        summary.textContent = `已清空 ${cleared} 条检查点、${clearedActions} 条操作记录。`;
      }
    }
  } catch (error) {
    if (summary) {
      const msg = messageOf(error);
      if (/\b404\b/.test(msg)) {
        summary.textContent = "清空失败：生产环境已禁用全局清空队列。";
      } else if (/\b403\b/.test(msg)) {
        summary.textContent = "清空失败：公开演示已关闭审阅写操作。";
      } else {
        summary.textContent = `清空失败：${msg}`;
      }
    }
  } finally {
    if (button && reviewQueueEnabled()) {
      button.disabled = false;
    }
  }
}

function renderExamples() {
  const list = $("example-list");
  list.replaceChildren();
  examples.forEach((question) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "example-button";
    button.textContent = question;
    button.addEventListener("click", () => {
      $("question-input").value = question;
      $("question-input").focus();
    });
    list.appendChild(button);
  });
}

async function refreshAll() {
  await refreshDemoConfig();
  const tasks = [
    refreshHealth(),
    refreshDocuments(),
    refreshEval(),
    refreshEvalHistory(),
    refreshTraces(),
    refreshProviderBenchmark(),
    refreshProviderBenchmarkHistory(),
    applyRoleGating(),
  ];
  if (reviewQueueEnabled()) {
    tasks.push(refreshReview());
  } else {
    renderPublicDemoReviewDisabled();
  }
  await Promise.allSettled(tasks);
}

async function refreshPanel(name) {
  if (name === "documents") return refreshDocuments();
  if (name === "review") {
    if (!reviewQueueEnabled()) return renderPublicDemoReviewDisabled();
    return refreshReview();
  }
  if (name === "eval") return refreshEval();
  if (name === "eval-history") return refreshEvalHistory();
  if (name === "traces") return refreshTraces();
  if (name === "provider-benchmark") return refreshProviderBenchmark();
  if (name === "provider-trend") return refreshProviderBenchmarkHistory();
  if (name === "tenants") return refreshTenants();
}

async function refreshDemoConfig() {
  try {
    state.demoConfig = await fetchJson("/v1/demo/config");
  } catch (error) {
    state.demoConfig = {
      public_demo_enabled: false,
      review_queue_enabled: true,
      demo_mode_label: "Local full demo",
    };
  }
  applyDemoConfigToUi();
}

function applyDemoConfigToUi() {
  const modeLabel = state.demoConfig.demo_mode_label || "Local full demo";
  document.body.dataset.publicDemo = publicDemoEnabled() ? "true" : "false";
  document.body.dataset.demoModeLabel = modeLabel;
  const pill = $("demo-mode-pill");
  if (pill) {
    pill.textContent = modeLabel;
  }
  setReviewControlsEnabled(reviewQueueEnabled());
}

function publicDemoEnabled() {
  return Boolean(state.demoConfig && state.demoConfig.public_demo_enabled);
}

function reviewQueueEnabled() {
  return !state.demoConfig || state.demoConfig.review_queue_enabled !== false;
}

function setReviewControlsEnabled(enabled) {
  ["review-filters", "review-export-json", "review-export-csv", "review-clear-queue"].forEach((id) => {
    const node = $(id);
    if (!node) return;
    node.hidden = !enabled;
    if ("disabled" in node) node.disabled = !enabled;
  });
  document.querySelectorAll('[data-refresh="review"]').forEach((button) => {
    button.hidden = !enabled;
    button.disabled = !enabled;
  });
}

function renderPublicDemoReviewDisabled() {
  state.review = {enabled: false, entries: [], total: 0};
  state.reviewSummary = null;
  renderSummary(
    $("review-summary"),
    "Public read-only demo",
    "warn",
    "公开演示仅开放 RAG 查询、证据、引用、时效、安全、文档和评测查看；审阅队列写操作已关闭。",
  );
  $("review-list").replaceChildren(makeEmptyNode("reviewer workflow disabled in public demo mode."));
  $("review-pager").replaceChildren();
  $("review-summary-cards").replaceChildren();
  setReviewControlsEnabled(false);
}

async function refreshHealth() {
  const status = $("backend-status");
  try {
    const data = await fetchJson("/healthz");
    status.textContent = data.status === "ok" ? "在线" : "未知";
    status.dataset.state = data.status === "ok" ? "online" : "warn";
  } catch (error) {
    status.textContent = "离线";
    status.dataset.state = "offline";
  }
}

async function refreshDocuments() {
  try {
    state.documents = await fetchJson("/v1/documents");
    $("document-count").textContent = String(state.documents.count ?? 0);
    $("chunk-count").textContent = String(state.documents.chunk_count ?? 0);
    renderDocuments(state.documents);
  } catch (error) {
    $("documents-summary").textContent = `文档不可用：${messageOf(error)}`;
    $("documents-list").replaceChildren(makeEmptyNode("无可用文档数据。"));
  }
}

async function refreshReview() {
  if (!reviewQueueEnabled()) {
    renderPublicDemoReviewDisabled();
    return;
  }
  const qs = reviewFilterQueryString();
  const summaryQs = reviewFilterQueryString({includePaging: false});
  try {
    const [queueData, summaryData] = await Promise.all([
      fetchJson(`/v1/review/queue${qs ? `?${qs}` : ""}`),
      fetchJson(`/v1/review/queue/summary${summaryQs ? `?${summaryQs}` : ""}`),
    ]);
    state.review = queueData;
    state.reviewSummary = summaryData;
    renderReviewSummary(summaryData);
    renderReview(queueData);
    renderReviewPager(queueData);
  } catch (error) {
    $("review-summary").textContent = `审阅队列不可用：${messageOf(error)}`;
    $("review-list").replaceChildren(makeEmptyNode("无可用审阅队列数据。"));
    $("review-pager").replaceChildren();
    $("review-summary-cards").replaceChildren();
  }
}

async function refreshEval() {
  try {
    state.eval = await fetchJson("/v1/evals/latest");
    renderEval(state.eval);
  } catch (error) {
    $("eval-summary").replaceChildren(makeBadge("缺少评测报告", "warn"));
    $("eval-categories").replaceChildren();
    $("eval-markdown").textContent = `评测接口不可用：${messageOf(error)}`;
  }
}

async function refreshEvalHistory() {
  try {
    state.evalHistory = await fetchJson("/v1/evals/history");
    renderEvalHistory(state.evalHistory);
  } catch (error) {
    $("eval-trend-summary").replaceChildren(makeBadge("评测历史不可用", "warn"));
    $("eval-trend-metrics").replaceChildren();
    $("eval-sparkline").replaceChildren();
    $("eval-trend-categories").replaceChildren(makeEmptyNode(`历史接口不可用：${messageOf(error)}`));
  }
}

async function refreshTraces() {
  try {
    state.traces = await fetchJson("/v1/debug/traces");
    renderTraces(state.traces);
  } catch (error) {
    $("trace-summary").textContent = `追踪不可用：${messageOf(error)}`;
    $("trace-list").replaceChildren(makeEmptyNode("无可用追踪数据。"));
  }
}

async function refreshProviderBenchmark() {
  try {
    const [latest, list] = await Promise.all([
      fetchJson("/v1/provider-benchmarks/latest"),
      fetchJson("/v1/provider-benchmarks"),
    ]);
    state.providerBenchmark = latest;
    state.providerBenchmarkList = list;
    renderProviderBenchmark(latest, list);
  } catch (error) {
    renderProviderBenchmarkError(messageOf(error));
  }
}

// --- Tenant admin console (platform_admin) ---------------------------------
// Panel visibility is presentation, never authorization: /v1/admin/tenants is
// gated server-side by the MANAGE_TENANTS permission, so a caller who unhides
// the panel by hand still gets 403 from the API. /v1/me only tells the UI what
// is worth rendering.
const PLATFORM_ADMIN_ROLE = "platform_admin";

async function applyRoleGating() {
  try {
    const me = await fetchJson("/v1/me");
    const roles = (me && me.roles) || [];
    if (!roles.includes(PLATFORM_ADMIN_ROLE)) return;
  } catch (error) {
    // Anonymous, expired or rejected session: keep the panel hidden and let
    // the rest of the dashboard boot normally.
    return;
  }
  const panel = $("tenant-admin");
  if (panel) panel.hidden = false;
  await refreshTenants();
}

async function refreshTenants() {
  const list = $("tenant-list");
  const summary = $("tenant-admin-summary");
  if (!list) return;
  try {
    const data = await fetchJson("/v1/admin/tenants");
    const tenants = data.tenants || [];
    if (summary) summary.textContent = `${tenants.length} 个活跃租户`;
    const nodes = tenants.length
      ? tenants.map((tenant) => makeItemNode({
          title: tenant.name || tenant.tenant_id,
          badgeText: tenant.status,
          meta: {
            tenant_id: tenant.tenant_id,
            created_at: tenant.created_at,
          },
        }))
      : [makeEmptyNode("尚无活跃租户。")];
    list.replaceChildren(...nodes);
  } catch (error) {
    if (summary) summary.textContent = `租户列表不可用：${tenantErrorReason(error)}`;
    list.replaceChildren(makeEmptyNode("无可用租户数据。"));
  }
}

async function createTenant() {
  const idInput = $("new-tenant-id");
  const nameInput = $("new-tenant-name");
  if (!idInput || !nameInput) return;
  const status = $("tenant-admin-status");
  const button = $("create-tenant");
  if (button) button.disabled = true;
  if (status) status.textContent = "开通中…";
  try {
    await fetchJson("/v1/admin/tenants", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({tenant_id: idInput.value, name: nameInput.value}),
    });
    idInput.value = "";
    nameInput.value = "";
    if (status) status.textContent = "开通成功。";
    await refreshTenants();
  } catch (error) {
    if (status) status.textContent = `开通失败：${tenantErrorReason(error)}`;
  } finally {
    if (button) button.disabled = false;
  }
}

/** Map a rejected request to a fixed, user-facing reason.
 *
 * fetchJson never reads an error body, so the only thing available here is the
 * status line — translating it keeps backend internals (stack traces, SQL,
 * other tenants' ids) out of the DOM even if a handler starts returning them.
 */
function tenantErrorReason(error) {
  const match = /^(\d{3})\b/.exec(messageOf(error));
  const status = match ? Number(match[1]) : 0;
  if (status === 401) return "请先登录。";
  if (status === 403) return "需要平台管理员权限。";
  if (status === 404) return "租户注册表不可用。";
  if (status === 409) return "该租户 ID 已存在。";
  if (status === 400 || status === 422) return "租户 ID 与名称不能为空。";
  return status ? `请求被拒绝（HTTP ${status}）。` : "网络错误。";
}

// Artifact-sourced strings only reach DOM nodes through textContent.
function elx(tag, {className, text} = {}) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function makeBadge(text, tone = "neutral") {
  return elx("span", {className: `badge ${tone}`, text});
}

function makeEmptyNode(text) {
  return elx("div", {className: "empty", text});
}

function renderSummary(container, badgeText, tone, summaryText = "") {
  const children = [makeBadge(badgeText, tone)];
  if (summaryText) {
    children.push(elx("span", {className: "summary-text", text: summaryText}));
  }
  container.replaceChildren(...children);
}

function makeMetadataNodes(values) {
  return Object.entries(values).map(([label, value]) => {
    const row = elx("div");
    row.append(
      elx("dt", {text: label}),
      elx("dd", {text: value ?? "无"}),
    );
    return row;
  });
}

function makeItemNode({title, badgeText, meta = {}, preview = ""}) {
  const article = elx("article", {className: "item"});
  const titleRow = elx("div", {className: "item-title"});
  titleRow.appendChild(elx("span", {text: title}));
  if (badgeText) titleRow.appendChild(makeBadge(badgeText));

  const metaRow = elx("div", {className: "item-meta"});
  const entries = Object.entries(meta).filter(
    ([, value]) => value !== undefined && value !== null && value !== "",
  );
  if (!entries.length) {
    metaRow.appendChild(elx("span", {text: "无元数据。"}));
  } else {
    entries.forEach(([key, value]) => {
      const row = elx("span");
      row.append(
        elx("strong", {text: `${metaLabel(key)}：`}),
        elx("span", {className: "mono", text: value}),
      );
      metaRow.appendChild(row);
    });
  }
  article.append(titleRow, metaRow);

  if (preview) {
    const details = elx("details", {className: "details-block"});
    details.append(
      elx("summary", {text: "内容预览"}),
      elx("div", {className: "content-preview", text: truncate(preview, 320)}),
    );
    article.appendChild(details);
  }
  return article;
}

function makeBenchmarkTable(headers, rows, extraClass = "") {
  const table = elx("table", {className: `category-table benchmark-table ${extraClass}`.trim()});
  const thead = elx("thead");
  const headRow = elx("tr");
  headers.forEach((h) => headRow.appendChild(elx("th", {text: h})));
  thead.appendChild(headRow);
  const tbody = elx("tbody");
  rows.forEach((cells) => {
    const tr = elx("tr");
    cells.forEach((cell) => {
      const td = elx("td");
      if (cell instanceof Node) td.appendChild(cell);
      else td.textContent = String(cell);
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  table.append(thead, tbody);
  return table;
}

function renderProviderBenchmarkError(message) {
  $("provider-benchmark-summary").replaceChildren(makeBadge("提供方基准不可用", "warn"));
  $("provider-benchmark-cards").replaceChildren();
  $("provider-benchmark-providers").replaceChildren();
  $("provider-benchmark-categories").replaceChildren();
  $("provider-benchmark-cases").replaceChildren(makeEmptyNode(`接口不可用：${message}`));
  $("provider-benchmark-markdown").textContent = "尚未加载报告。";
}

function renderProviderBenchmark(latest, list) {
  const cards = $("provider-benchmark-cards");
  const providers = $("provider-benchmark-providers");
  const categories = $("provider-benchmark-categories");
  const cases = $("provider-benchmark-cases");
  const markdown = $("provider-benchmark-markdown");
  const summaryLine = $("provider-benchmark-summary");
  [cards, providers, categories, cases].forEach((node) => node.replaceChildren());

  if (!latest || !latest.available || !latest.latest) {
    summaryLine.replaceChildren(makeBadge("无提供方基准产物", "warn"));
    cases.replaceChildren(
      makeEmptyNode("未找到提供方基准产物。请运行：bash scripts/run_provider_benchmark.sh mock")
    );
    markdown.textContent =
      "运行 bash scripts/run_provider_benchmark.sh mock 生成本地基准产物。";
    return;
  }

  const summary = latest.latest;
  const passed = Number(summary.failed || 0) === 0;
  summaryLine.replaceChildren(
    makeBadge(summary.provider || "提供方", passed ? "pass" : "warn"),
    elx("span", {
      className: "summary-text",
      text: ` 分数 ${formatScore(summary.score)}，${summary.passed ?? 0}/${summary.total ?? 0} 个用例，来源 ${summary.source || "results"}`,
    })
  );
  renderBenchmarkCards(cards, summary);
  renderBenchmarkProviders(providers, (list && list.artifacts) || []);
  renderBenchmarkCategories(categories, summary.by_category || {});
  renderBenchmarkCases(cases, summary.results || []);
  markdown.textContent = latest.markdown_report || "无可用 Markdown 报告。";
}

function renderBenchmarkCards(container, summary) {
  const cards = [
    ["提供方", summary.provider || "—", "neutral"],
    ["模型", summary.model || "—", "neutral"],
    ["分数", formatScore(summary.score), Number(summary.failed || 0) === 0 ? "pass" : "warn"],
    ["回退率", formatPct(summary.fallback_rate), Number(summary.fallback_rate || 0) > 0 ? "warn" : "pass"],
    ["引用有效", formatPct(summary.citation_validation_rate), "pass"],
    ["无效引用", summary.invalid_citation_count ?? 0, Number(summary.invalid_citation_count || 0) ? "fail" : "neutral"],
    ["提供方错误", summary.provider_error_count ?? 0, Number(summary.provider_error_count || 0) ? "fail" : "neutral"],
    ["平均延迟", formatMs(summary.avg_latency_ms), "neutral"],
    ["P95 延迟", formatMs(summary.p95_latency_ms), "neutral"],
  ];
  container.replaceChildren(
    ...cards.map(([label, value, tone]) => {
      const card = elx("div", {className: `summary-card ${tone}`});
      card.append(
        elx("span", {className: "summary-card-label", text: label}),
        elx("span", {className: "summary-card-value", text: value})
      );
      return card;
    })
  );
}

function renderBenchmarkProviders(container, artifacts) {
  if (!artifacts.length) {
    container.replaceChildren();
    return;
  }
  const rows = artifacts.map((a) => [
    a.provider || "—",
    a.model || "—",
    formatScore(a.score),
    formatPct(a.fallback_rate),
    formatPct(a.citation_validation_rate),
    formatMs(a.avg_latency_ms),
    a.source || "",
  ]);
  container.replaceChildren(
    elx("h3", {className: "table-caption", text: `产物 (${artifacts.length})`}),
    makeBenchmarkTable(
      ["提供方", "模型", "分数", "回退", "引用有效", "平均延迟", "来源"],
      rows
    )
  );
}

function renderBenchmarkCategories(container, categories) {
  const names = Object.keys(categories).sort();
  if (!names.length) {
    container.replaceChildren();
    return;
  }
  const rows = names.map((name) => {
    const stats = categories[name] || {};
    return [
      name,
      stats.total ?? 0,
      stats.passed ?? 0,
      stats.failed ?? 0,
      formatScore(stats.score),
      formatPct(stats.fallback_rate),
      formatPct(stats.citation_validation_rate),
    ];
  });
  container.replaceChildren(
    elx("h3", {className: "table-caption", text: "按类别"}),
    makeBenchmarkTable(
      ["类别", "总数", "通过", "失败", "分数", "回退", "引用有效"],
      rows
    )
  );
}

function renderBenchmarkCases(container, results) {
  if (!results.length) {
    container.replaceChildren(makeEmptyNode("该产物无逐用例数据。"));
    return;
  }
  const rows = results.map((r) => [
    r.case_id || "",
    r.category || "",
    makeBadge(r.passed ? "通过" : "失败", r.passed ? "pass" : "fail"),
    formatScore(r.score),
    formatBool(r.llm_used),
    formatBool(r.fallback_used),
    r.fallback_reason || "—",
    r.citation_valid === null || r.citation_valid === undefined ? "不适用" : formatBool(r.citation_valid),
    formatMs(r.latency_ms),
    (r.failure_reasons || []).join("; ") || "—",
  ]);
  container.replaceChildren(
    elx("h3", {className: "table-caption", text: `用例 (${results.length})`}),
    makeBenchmarkTable(
      ["用例", "类别", "通过", "分数", "LLM", "回退", "原因", "引用", "延迟", "失败项"],
      rows,
      "benchmark-case-table"
    )
  );
}

function formatPct(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "不适用";
  return `${(number * 100).toFixed(1)}%`;
}

function formatMs(value) {
  if (value === null || value === undefined) return "不适用";
  const number = Number(value);
  if (!Number.isFinite(number)) return "不适用";
  return `${number.toFixed(1)} ms`;
}

function formatBool(value) {
  return value ? "是" : "否";
}

// --- Phase 8E: provider benchmark trends ----------------------------------
// Sparklines use createElementNS so artifact strings are never parsed as markup.
const SVG_NS = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, String(value)));
  return node;
}

async function refreshProviderBenchmarkHistory() {
  try {
    state.providerBenchmarkHistory = await fetchJson("/v1/provider-benchmarks/history");
    renderProviderBenchmarkHistory(state.providerBenchmarkHistory);
  } catch (error) {
    renderProviderTrendError(messageOf(error));
  }
}

function renderProviderTrendError(message) {
  $("provider-trend-summary").replaceChildren(makeBadge("提供方基准历史不可用", "warn"));
  $("provider-trend-metrics").replaceChildren();
  $("provider-trend-sparklines").replaceChildren(makeEmptyNode(`接口不可用：${message}`));
  $("provider-trend-table").replaceChildren();
}

function renderProviderBenchmarkHistory(data) {
  const summaryLine = $("provider-trend-summary");
  const metrics = $("provider-trend-metrics");
  const charts = $("provider-trend-sparklines");
  const table = $("provider-trend-table");
  [metrics, charts, table].forEach((node) => node.replaceChildren());

  const snapshots = (data && data.snapshots) || [];
  if (!data || !data.available || !snapshots.length) {
    summaryLine.replaceChildren(makeBadge("无提供方基准历史", "warn"));
    charts.replaceChildren(
      makeEmptyNode(
        "未找到提供方基准历史。请运行：bash scripts/run_provider_benchmark.sh mock 然后 bash scripts/archive_provider_benchmark_snapshot.sh"
      )
    );
    return;
  }

  const latest = data.latest || snapshots[snapshots.length - 1];
  const passed = Number(latest.failed || 0) === 0;
  summaryLine.replaceChildren(
    makeBadge(latest.provider || "提供方", passed ? "pass" : "warn"),
    elx("span", {
      className: "summary-text",
      text: ` 最新分数 ${formatScore(latest.score)} · ${snapshots.length} 个快照 · ${latest.created_at || ""}`,
    })
  );
  renderProviderTrendMetrics(metrics, data, latest);
  renderProviderTrendCharts(charts, snapshots);
  renderProviderTrendTable(table, snapshots);
}

function renderProviderTrendMetrics(container, data, latest) {
  const scoreDelta = data.score_delta_latest;
  const fallbackDelta = data.fallback_rate_delta_latest;
  const citationDelta = data.citation_validation_rate_delta_latest;
  const cards = [
    {label: "快照数", value: data.count ?? 0, tone: "neutral"},
    {label: "最新提供方", value: latest.provider || "—", tone: "neutral"},
    {label: "最新分数", value: formatScore(latest.score), tone: Number(latest.failed || 0) === 0 ? "pass" : "fail"},
    {label: "分数变化", value: formatDelta(scoreDelta), tone: deltaTone(scoreDelta)},
    {label: "回退变化", value: formatDeltaPct(fallbackDelta), tone: deltaTone(fallbackDelta, {invert: true})},
    {label: "引用变化", value: formatDeltaPct(citationDelta), tone: deltaTone(citationDelta)},
  ];
  container.replaceChildren(
    ...cards.map((card) => {
      const node = elx("div", {className: `summary-card ${card.tone}`});
      node.append(
        elx("span", {className: "summary-card-label", text: card.label}),
        elx("span", {className: "summary-card-value", text: card.value})
      );
      return node;
    })
  );
}

function renderProviderTrendCharts(container, snapshots) {
  const scores = snapshots.map((s) => Number(s.score) || 0);
  const fallbacks = snapshots.map((s) => Number(s.fallback_rate) || 0);
  const citations = snapshots.map((s) => Number(s.citation_validation_rate) || 0);
  container.replaceChildren(
    makeTrendChart("分数", scores, {label: "提供方基准分数趋势"}),
    makeTrendChart("回退率", fallbacks, {label: "提供方基准回退率趋势"}),
    makeTrendChart("引用验证率", citations, {label: "提供方基准引用验证率趋势"})
  );
}

function renderProviderTrendTable(container, snapshots) {
  if (!snapshots.length) {
    container.replaceChildren();
    return;
  }
  const rows = snapshots
    .slice()
    .reverse()
    .map((s) => [
      s.created_at || "",
      s.provider || "—",
      s.model || "—",
      formatScore(s.score),
      formatPct(s.fallback_rate),
      formatPct(s.citation_validation_rate),
      s.invalid_citation_count ?? 0,
      s.provider_error_count ?? 0,
      formatMs(s.avg_latency_ms),
      formatMs(s.p95_latency_ms),
      s.git_commit || "—",
    ]);
  container.replaceChildren(
    elx("h3", {className: "table-caption", text: `历史 (${snapshots.length})`}),
    makeBenchmarkTable(
      [
        "创建时间", "提供方", "模型", "分数", "回退", "引用有效",
        "无效", "错误", "平均延迟", "P95 延迟", "提交",
      ],
      rows,
      "benchmark-case-table"
    )
  );
}

function makeTrendChart(title, values, opts) {
  const wrap = elx("div", {className: "provider-trend-chart"});
  wrap.appendChild(elx("h3", {className: "table-caption", text: title}));
  const spark = elx("div", {className: "eval-sparkline"});
  spark.appendChild(makeSparkline(values, opts));
  wrap.appendChild(spark);
  return wrap;
}

function makeSparkline(values, {label = "趋势", domainMax = 1} = {}) {
  const width = 320;
  const height = 72;
  const pad = 8;
  const usableWidth = width - pad * 2;
  const usableHeight = height - pad * 2;
  const max = Number(domainMax) || 1;
  const svg = svgEl("svg", {viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": label});
  svg.appendChild(
    svgEl("line", {class: "sparkline-axis", x1: pad, y1: height - pad, x2: width - pad, y2: height - pad})
  );
  const points = values.map((value, index) => {
    const x = values.length === 1
      ? width / 2
      : pad + (index / (values.length - 1)) * usableWidth;
    const norm = Math.max(0, Math.min(1, (Number(value) || 0) / max));
    const y = pad + (1 - norm) * usableHeight;
    return {x, y};
  });
  if (points.length) {
    svg.appendChild(
      svgEl("polyline", {points: points.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ")})
    );
    points.forEach((p) => svg.appendChild(svgEl("circle", {cx: p.x.toFixed(1), cy: p.y.toFixed(1), r: 3})));
  }
  return svg;
}

function deltaTone(value, {invert = false} = {}) {
  if (value === null || value === undefined) return "neutral";
  const number = Number(value);
  if (!Number.isFinite(number) || number === 0) return "neutral";
  const positiveGood = invert ? number < 0 : number > 0;
  return positiveGood ? "pass" : "fail";
}

function formatDeltaPct(value) {
  if (value === null || value === undefined) return "不适用";
  const number = Number(value);
  if (!Number.isFinite(number)) return "不适用";
  return `${number > 0 ? "+" : ""}${(number * 100).toFixed(1)}pp`;
}

async function runQuery() {
  const question = $("question-input").value.trim();
  if (!question) {
    $("query-status").textContent = "请先输入问题。";
    return;
  }

  $("run-query").disabled = true;
  $("query-status").textContent = "查询中…";
  try {
    const data = await fetchJson("/v1/rag/query", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({question}),
    });
    state.query = data;
    renderQuery(data);
    $("query-status").textContent = "查询完成。";
    const followUpTasks = [refreshTraces()];
    if (reviewQueueEnabled()) {
      followUpTasks.push(refreshReview());
    } else {
      renderPublicDemoReviewDisabled();
    }
    await Promise.allSettled(followUpTasks);
    // Only auto-focus when a row was actually persisted to the queue.
    const review = data.human_review || {};
    if (reviewQueueEnabled() && review.review_queue_id) {
      focusReviewEntry(review.review_queue_id);
    }
  } catch (error) {
    $("query-status").textContent = `查询失败：${messageOf(error)}`;
  } finally {
    $("run-query").disabled = false;
  }
}

async function fetchJson(url, options = {}) {
  const headers = {...(options.headers || {})};
  if (state.authToken) {
    headers["Authorization"] = `Bearer ${state.authToken}`;
  }
  const response = await fetch(url, {...options, headers});
  if (!response.ok) {
    if (response.status === 401) {
      // The stored token is stale; drop it so the UI stops claiming a session.
      clearAuthToken();
    }
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return response.json();
}

function renderQuery(data) {
  const answerNode = $("answer-text");
  answerNode.textContent = data.answer || "未返回回答。";
  const review = data.human_review || {};
  const answerBadges = [
    makeBadge(labelQuestionType(data.question_type), data.question_type === "unsafe_request" ? "fail" : "pass"),
    reviewBadgeFor(data, review),
  ];
  if (data.safety_analysis?.unsafe_request_detected) {
    answerBadges.push(makeBadge("检测到不安全请求", "fail"));
  }
  if (data.safety_analysis?.prompt_injection_detected) {
    answerBadges.push(makeBadge("检测到提示注入", "fail"));
  }
  $("answer-badges").replaceChildren(...answerBadges);

  $("answer-metadata").replaceChildren(...makeMetadataNodes({
    "置信度": formatScore(data.confidence),
    "问题类型": labelQuestionType(data.question_type),
    "审阅状态": review.status ? labelStatus(review.status) : (review.required ? "待处理" : "无需审阅"),
    "队列 ID": review.review_queue_id || "无",
    "审阅原因": labelReasons(review.reasons) || "无",
    "错误": (data.errors || []).join("，") || "无",
  }));
  $("safety-json").textContent = pretty(data.safety_analysis || {});
  $("temporal-json").textContent = pretty(data.temporal_analysis || {});
  $("conflict-json").textContent = pretty(data.conflict_analysis || {});
  renderCitations($("citations-list"), data.citations || []);
  renderEvidenceList($("support-list"), data.support_evidence || []);
  renderEvidenceList($("counter-list"), data.counter_evidence || []);
  renderReviewHandoff(data);

  // Keep the answer in view on stacked/mobile layouts without jumping when already visible.
  // Guard missing DOM layout APIs so lightweight harnesses (XSS regression) still work.
  // When a case was persisted to the queue, runQuery will later focus the queue instead.
  const shouldFocusQueue = Boolean(review.review_queue_id);
  if (
    !shouldFocusQueue
    && typeof answerNode.getBoundingClientRect === "function"
    && typeof answerNode.scrollIntoView === "function"
    && typeof window !== "undefined"
    && typeof window.innerHeight === "number"
  ) {
    const rect = answerNode.getBoundingClientRect();
    const fullyVisible = rect.top >= 0 && rect.bottom <= window.innerHeight;
    if (!fullyVisible) {
      answerNode.scrollIntoView({block: "nearest", behavior: "smooth"});
    }
  }
}

function reviewBadgeFor(data, review) {
  if (review && review.review_queue_id) {
    return makeBadge("已写入队列", "warn");
  }
  if (review && review.required) {
    return makeBadge("需审阅未写入", "warn");
  }
  if (data && data.needs_human_review) {
    return makeBadge("标记需关注", "warn");
  }
  return makeBadge("无需审阅", "pass");
}

/** Show an answer-panel CTA stratified by whether the case was actually queued. */
function renderReviewHandoff(data) {
  const handoff = $("review-handoff");
  if (!handoff) return;

  const review = (data && data.human_review) || {};
  const needed = Boolean((data && data.needs_human_review) || review.required);
  if (!needed) {
    handoff.hidden = true;
    handoff.classList.remove("is-persisted", "is-warn", "is-note");
    handoff.replaceChildren();
    clearReviewHighlights();
    return;
  }

  const reasons = labelReasons(review.reasons);
  const queueId = review.review_queue_id || "";
  const status = review.status || "";
  const actions = elx("div", {className: "review-handoff-actions"});
  let titleText = "标记需关注";
  let bodyText = "未达到入队策略（例如置信度未低于阈值，或问题类型无需强制审阅）。";
  let variant = "is-note";

  if (queueId) {
    titleText = "已写入审阅队列";
    bodyText = [
      reasons ? `原因：${reasons}` : null,
      `队列 ID：${queueId}`,
    ].filter(Boolean).join(" · ");
    variant = "is-persisted";
  } else if (review.required) {
    titleText = "需审阅但未写入队列";
    const statusHint = status ? `状态：${status}` : "状态未知";
    bodyText = [
      reasons ? `原因：${reasons}` : null,
      statusHint,
      status === "public_demo_not_persisted"
        ? "公开演示不落库。"
        : (status === "handoff_failed" ? "交接写入失败，请查看错误字段。" : null),
    ].filter(Boolean).join(" · ");
    variant = "is-warn";
  } else if (reasons) {
    bodyText = `标记原因：${reasons}。未达到入队策略，不会出现在审阅列表。`;
  }

  handoff.classList.remove("is-persisted", "is-warn", "is-note");
  handoff.classList.add(variant);

  const title = elx("p", {className: "review-handoff-title", text: titleText});
  const body = elx("p", {className: "review-handoff-body", text: bodyText});

  if (!reviewQueueEnabled()) {
    actions.appendChild(elx("p", {
      className: "review-handoff-note",
      text: "公开演示已关闭审阅队列写操作，无法在此处理。",
    }));
  } else if (queueId) {
    const button = elx("button", {className: "primary-button", text: "打开审阅队列"});
    button.type = "button";
    button.addEventListener("click", () => focusReviewEntry(queueId));
    actions.appendChild(button);
  } else if (review.required) {
    const button = elx("button", {className: "secondary-button", text: "查看审阅面板"});
    button.type = "button";
    button.addEventListener("click", () => focusReviewEntry(null));
    actions.appendChild(button);
  }

  handoff.hidden = false;
  handoff.replaceChildren(title, body, actions);
}

function clearReviewHighlights() {
  if (typeof document === "undefined" || !document.querySelectorAll) return;
  document.querySelectorAll(".review-item.is-highlighted").forEach((node) => {
    node.classList.remove("is-highlighted");
  });
  if (state._highlightTimer) {
    clearTimeout(state._highlightTimer);
    state._highlightTimer = null;
  }
}

function findReviewEntryNode(reviewQueueId) {
  if (!reviewQueueId) return null;
  const list = $("review-list");
  if (!list || !list.querySelectorAll) return null;
  const target = String(reviewQueueId);
  const nodes = list.querySelectorAll("[data-review-id]");
  for (let i = 0; i < nodes.length; i += 1) {
    if (nodes[i].dataset && nodes[i].dataset.reviewId === target) {
      return nodes[i];
    }
  }
  return null;
}

/** Scroll the review panel into view and briefly highlight the matching entry. */
function focusReviewEntry(reviewQueueId) {
  clearReviewHighlights();

  const entry = findReviewEntryNode(reviewQueueId);
  const title = $("review-title");
  const panel = (title && title.closest) ? title.closest(".panel") : null;
  const list = $("review-list");
  const summary = $("review-summary");
  const target = entry || panel || list;
  if (
    target
    && typeof target.scrollIntoView === "function"
  ) {
    target.scrollIntoView({block: "center", behavior: "smooth"});
  }

  if (entry && entry.classList) {
    entry.classList.add("is-highlighted");
    if (typeof setTimeout === "function") {
      state._highlightTimer = setTimeout(() => {
        entry.classList.remove("is-highlighted");
        state._highlightTimer = null;
      }, 2500);
    }
  } else if (reviewQueueId && summary) {
    summary.textContent = "队列中未找到该条目（可能被筛选隐藏或已清空）。";
  }
}

function renderCitations(container, citations) {
  if (!citations.length) {
    container.replaceChildren(makeEmptyNode("未返回引用。"));
    return;
  }
  container.replaceChildren(...citations.map((citation) => makeItemNode({
    title: citation.title || citation.doc_id || "引用",
    badgeText: citation.version || "引用",
    meta: {
      doc_id: citation.doc_id,
      document_id: citation.document_id,
      chunk_id: citation.chunk_id,
      section: citation.section_title,
      source: citation.source,
      valid_from: citation.valid_from,
      client: citation.client,
    },
    preview: citation.snippet,
  })));
}

function renderEvidenceList(container, items) {
  if (!items.length) {
    container.replaceChildren(makeEmptyNode("未返回证据。"));
    return;
  }
  container.replaceChildren(...items.map((item) => makeItemNode({
    title: item.title || item.doc_id || "证据",
    badgeText: formatScore(item.score),
    meta: {
      chunk_id: item.chunk_id,
      document_id: item.document_id || item.doc_id,
      section: item.section_title,
      source: item.source || item.source_path,
      retrieval_strategy: item.retrieval_strategy,
      score_breakdown: item.score_breakdown ? JSON.stringify(item.score_breakdown) : null,
      client: item.client,
    },
    preview: item.content,
  })));
}

function renderDocuments(data) {
  const docs = data.documents || [];
  $("documents-summary").textContent =
    `${data.count ?? docs.length} 篇文档，${data.chunk_count ?? 0} 个分块，来源：${data.source || "未知"}`;
  const nodes = docs.length
    ? docs.map((doc) => makeItemNode({
        title: doc.title || doc.document_id,
        badgeText: labelDocType(doc.document_type),
        meta: {
          document_id: doc.document_id,
          client: doc.client,
          version: doc.version,
          policy_family: doc.policy_family,
          valid_from: doc.valid_from,
          valid_to: doc.valid_to,
          source_path: doc.source_path,
          malicious: doc.is_malicious ? "是" : "否",
        },
      }))
    : [makeEmptyNode("尚未加载文档。")];
  $("documents-list").replaceChildren(...nodes);
}

function renderReview(data) {
  if (!data.enabled && publicDemoEnabled()) {
    renderPublicDemoReviewDisabled();
    return;
  }
  if (!data.enabled) {
    $("review-summary").replaceChildren(makeBadge("审阅已禁用", "warn"));
    $("review-list").replaceChildren(makeEmptyNode("人工审阅交接已禁用。"));
    return;
  }
  const entries = data.entries || [];
  const total = data.total ?? entries.length;
  const offset = data.offset ?? 0;
  const limit = data.limit ?? entries.length;
  const pageEnd = Math.min(offset + entries.length, total);
  const filterDescr = filterDescription(data.filters || {}, data.sort);
  const summaryParts = [
    `${total} 个匹配检查点`,
    total ? `显示 ${offset + 1}–${pageEnd}` : null,
    filterDescr,
  ].filter(Boolean);
  $("review-summary").textContent = summaryParts.join(" · ");
  if (!entries.length) {
    if (total === 0 && !filterDescr) {
      $("review-list").replaceChildren(makeEmptyNode(
        "队列中暂无审阅检查点。运行 tax_policy 或 invoice_compliance 查询以填充。",
      ));
    } else {
      $("review-list").replaceChildren(makeEmptyNode("没有符合当前筛选条件的条目。"));
    }
    return;
  }
  $("review-list").replaceChildren(...entries.map((entry) => makeReviewEntryNode(entry)));
}

function filterDescription(filters, sort) {
  const parts = [];
  if (filters.status) parts.push(`状态=${labelStatus(filters.status)}`);
  if (filters.question_type) parts.push(`类型=${filters.question_type}`);
  if (filters.reason) parts.push(`原因=${filters.reason}`);
  if (filters.reviewer) parts.push(`审阅人=${filters.reviewer}`);
  if (filters.has_actions) parts.push("含操作记录");
  if (sort && sort !== "created_at_desc") parts.push(`排序=${sort}`);
  return parts.length ? `筛选：${parts.join("，")}` : "";
}

function renderReviewSummary(data) {
  const container = $("review-summary-cards");
  if (!container) return;
  if (!data || !data.enabled) {
    container.replaceChildren();
    return;
  }
  const byStatus = data.by_status || {};
  const cards = [
    {label: "总数", value: data.total ?? 0, tone: "neutral"},
    {label: "待处理", value: byStatus.pending ?? 0, tone: "warn"},
    {label: "已通过", value: byStatus.approved ?? 0, tone: "pass"},
    {label: "已驳回", value: byStatus.rejected ?? 0, tone: "fail"},
    {label: "需修改", value: byStatus.changes_requested ?? 0, tone: "warn"},
    {label: "已解决", value: byStatus.resolved ?? 0, tone: "pass"},
  ];
  container.replaceChildren(...cards.map((card) => {
    const node = elx("div", {className: `summary-card ${card.tone}`});
    node.append(
      elx("span", {className: "summary-card-label", text: card.label}),
      elx("span", {className: "summary-card-value", text: card.value}),
    );
    return node;
  }));
}

function renderReviewPager(data) {
  const pager = $("review-pager");
  if (!pager) return;
  if (!data || !data.enabled) {
    pager.replaceChildren();
    return;
  }
  const total = data.total ?? 0;
  const limit = data.limit ?? 20;
  const offset = data.offset ?? 0;
  if (total <= limit) {
    pager.replaceChildren();
    return;
  }
  const currentPage = Math.floor(offset / limit) + 1;
  const totalPages = Math.max(1, Math.ceil(total / limit));
  const prev = elx("button", {className: "secondary-button", text: "← 上一页"});
  prev.type = "button";
  prev.id = "review-pager-prev";
  prev.disabled = offset <= 0;
  prev.addEventListener("click", () => paginateReview(-1));
  const next = elx("button", {className: "secondary-button", text: "下一页 →"});
  next.type = "button";
  next.id = "review-pager-next";
  next.disabled = offset + limit >= total;
  next.addEventListener("click", () => paginateReview(1));
  pager.replaceChildren(
    prev,
    elx("span", {className: "review-pager-status", text: `第 ${currentPage} / ${totalPages} 页`}),
    next,
  );
}

function paginateReview(direction) {
  const f = state.reviewFilters;
  const total = state.review?.total ?? 0;
  const nextOffset = Math.max(0, f.offset + direction * f.limit);
  if (nextOffset >= total && direction > 0) return;
  f.offset = nextOffset;
  refreshReview();
}

function renderEval(data) {
  if (!data.available) {
    $("eval-summary").replaceChildren(makeBadge("缺少评测报告", "warn"));
    $("eval-categories").replaceChildren();
    $("eval-markdown").textContent = "运行 scripts/run_eval_gate.sh 生成本地评测产物。";
    return;
  }

  const summary = data.summary || {};
  const passed = Number(summary.failed || 0) === 0;
  renderSummary(
    $("eval-summary"),
    passed ? "CI 评测通过" : "CI 评测失败",
    passed ? "pass" : "fail",
    `分数 ${formatScore(summary.score)}，通过 ${summary.passed ?? 0}，失败 ${summary.failed ?? 0}，跳过 ${summary.skipped ?? 0}`,
  );
  $("eval-categories").replaceChildren(makeCategoryTable(data.by_category || {}));
  $("eval-markdown").textContent = data.markdown_report || "无可用 Markdown 报告。";
}

function renderEvalHistory(data) {
  const snapshots = data.snapshots || [];
  if (!data.available || !snapshots.length) {
    $("eval-trend-summary").textContent =
      "未找到评测历史快照。运行评测闸门并归档快照。";
    $("eval-trend-metrics").replaceChildren();
    $("eval-sparkline").replaceChildren();
    $("eval-trend-categories").replaceChildren();
    return;
  }

  const latest = data.latest || snapshots[snapshots.length - 1];
  const passed = Number(latest.failed || 0) === 0;
  renderSummary(
    $("eval-trend-summary"),
    passed ? "最新通过" : "最新失败",
    passed ? "pass" : "fail",
    `${latest.snapshot_id} / ${latest.created_at}`,
  );
  $("eval-trend-metrics").replaceChildren(...makeEvalTrendMetrics(latest, data));
  $("eval-sparkline").replaceChildren(makeScoreSparkline(snapshots));
  $("eval-trend-categories").replaceChildren(makeCategoryTable(latest.by_category || {}));
}

function makeEvalTrendMetrics(latest, data) {
  const delta = data.score_delta_latest;
  const deltaTone = delta == null ? "neutral" : (Number(delta) < 0 ? "fail" : (Number(delta) > 0 ? "pass" : "neutral"));
  const metrics = [
    {label: "最新分数", value: formatScore(latest.score), tone: Number(latest.failed || 0) === 0 ? "pass" : "fail"},
    {label: "通过", value: latest.passed ?? 0, tone: "pass"},
    {label: "失败", value: latest.failed ?? 0, tone: Number(latest.failed || 0) ? "fail" : "neutral"},
    {label: "跳过", value: latest.skipped ?? 0, tone: Number(latest.skipped || 0) ? "warn" : "neutral"},
    {label: "变化", value: formatDelta(delta), tone: deltaTone},
    {label: "快照数", value: data.count ?? 0, tone: "neutral"},
  ];
  return metrics.map((metric) => {
    const node = elx("div", {className: `trend-metric ${metric.tone}`});
    node.append(
      elx("span", {className: "trend-metric-label", text: metric.label}),
      elx("span", {className: "trend-metric-value", text: metric.value}),
    );
    return node;
  });
}

function makeScoreSparkline(snapshots) {
  const width = 320;
  const height = 84;
  const pad = 10;
  const usableWidth = width - pad * 2;
  const usableHeight = height - pad * 2;
  const points = snapshots.map((snapshot, index) => {
    const x = snapshots.length === 1
      ? width / 2
      : pad + (index / (snapshots.length - 1)) * usableWidth;
    const score = Math.max(0, Math.min(1, Number(snapshot.score) || 0));
    const y = pad + (1 - score) * usableHeight;
    return {x, y, score, id: snapshot.snapshot_id};
  });
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "评测分数走势图");
  const axis = document.createElementNS(ns, "line");
  axis.setAttribute("class", "sparkline-axis");
  axis.setAttribute("x1", String(pad));
  axis.setAttribute("y1", String(height - pad));
  axis.setAttribute("x2", String(width - pad));
  axis.setAttribute("y2", String(height - pad));
  const polyline = document.createElementNS(ns, "polyline");
  polyline.setAttribute(
    "points",
    points.map((point) => `${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(" "),
  );
  svg.append(axis, polyline);
  points.forEach((point) => {
    const circle = document.createElementNS(ns, "circle");
    circle.setAttribute("cx", point.x.toFixed(1));
    circle.setAttribute("cy", point.y.toFixed(1));
    circle.setAttribute("r", "3.5");
    const title = document.createElementNS(ns, "title");
    title.textContent = `${point.id} 分数 ${formatScore(point.score)}`;
    circle.appendChild(title);
    svg.appendChild(circle);
  });
  return svg;
}

function renderTraces(data) {
  if (!data.enabled) {
    $("trace-summary").replaceChildren(makeBadge("追踪已禁用", "warn"));
    $("trace-list").replaceChildren(makeEmptyNode("设置 TRUSTRAG_TRACE_ENABLED=true 以记录本地追踪。"));
    return;
  }
  const events = data.events || [];
  $("trace-summary").textContent = `${events.length} 条追踪事件`;
  const nodes = events.length
    ? events.slice(-8).reverse().map((event) => makeItemNode({
        title: event.run_name || event.event_type || "追踪事件",
        badgeText: event.event_type || "事件",
        meta: {
          timestamp: event.timestamp,
          tags: (event.tags || []).join(", "),
          input: event.input_summary ? JSON.stringify(event.input_summary) : null,
          output: event.output_summary ? JSON.stringify(event.output_summary) : null,
        },
      }))
    : [makeEmptyNode("尚无追踪事件记录。")];
  $("trace-list").replaceChildren(...nodes);
}

function makeCategoryTable(categories) {
  const names = Object.keys(categories).sort();
  if (!names.length) return makeEmptyNode("无分类评测数据。");
  const rows = names.map((name) => {
    const stats = categories[name] || {};
    return [name, formatScore(stats.score), stats.passed ?? 0, stats.failed ?? 0];
  });
  return makeBenchmarkTable(["类别", "分数", "通过", "失败"], rows);
}

function pretty(value) {
  return JSON.stringify(value, null, 2);
}

function formatScore(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "不适用";
  return number.toFixed(3);
}

function formatDelta(value) {
  if (value === null || value === undefined) return "不适用";
  const number = Number(value);
  if (!Number.isFinite(number)) return "不适用";
  return `${number > 0 ? "+" : ""}${number.toFixed(3)}`;
}

function truncate(text, limit) {
  const value = String(text || "");
  if (value.length <= limit) return value;
  return `${value.slice(0, limit)}...`;
}

function messageOf(error) {
  return error?.message || String(error);
}

const REVIEW_ACTIONS = [
  {type: "approve", label: "通过"},
  {type: "reject", label: "驳回"},
  {type: "request_changes", label: "要求修改"},
  {type: "rewrite_note", label: "添加备注"},
  {type: "resolve", label: "解决"},
  {type: "reopen", label: "重新打开"},
];

function makeReviewEntryNode(entry) {
  const id = entry.review_queue_id;
  const status = entry.status || "pending";
  const meta = {
    review_queue_id: id,
    question_type: labelQuestionType(entry.question_type),
    reasons: labelReasons(entry.human_review_reasons),
    created_at: entry.created_at,
    confidence: formatScore(entry.confidence),
    actions: entry.action_count ?? 0,
    last_action_at: entry.last_action_at,
  };
  const actionStatus = state.actionStatus[id] || "";
  const actionStatusTone = actionStatus.startsWith("失败") || actionStatus.startsWith("历史失败")
    ? "fail"
    : (actionStatus ? "pass" : "neutral");
  const history = state.actionHistory[id] || [];

  const article = elx("article", {className: "item review-item"});
  article.dataset.reviewId = String(id);

  const title = elx("div", {className: "item-title"});
  title.append(
    elx("span", {text: entry.question || id}),
    makeBadge(labelStatus(status), statusToneOf(status)),
  );

  const metaRow = elx("div", {className: "item-meta"});
  const metaEntries = Object.entries(meta).filter(
    ([, value]) => value !== undefined && value !== null && value !== "",
  );
  if (!metaEntries.length) {
    metaRow.appendChild(elx("span", {text: "无元数据。"}));
  } else {
    metaEntries.forEach(([key, value]) => {
      const row = elx("span");
      row.append(
        elx("strong", {text: `${metaLabel(key)}：`}),
        elx("span", {className: "mono", text: value}),
      );
      metaRow.appendChild(row);
    });
  }

  const actions = elx("div", {className: "review-action-row"});
  REVIEW_ACTIONS.forEach((action) => {
    const button = elx("button", {className: "review-action", text: action.label});
    button.type = "button";
    button.dataset.reviewAction = action.type;
    actions.appendChild(button);
  });
  const historyButton = elx("button", {
    className: "review-action review-history-toggle",
    text: "历史",
  });
  historyButton.type = "button";
  historyButton.dataset.reviewHistory = "true";
  actions.appendChild(historyButton);

  const noteId = `review-note-${id}`;
  const noteLabel = elx("label", {className: "review-note-label", text: "审阅备注（可选）"});
  noteLabel.htmlFor = noteId;
  const note = elx("textarea", {className: "review-note"});
  note.id = noteId;
  note.rows = 2;
  note.placeholder = "为什么执行此操作？仅纯文本。";

  const rewriteDetails = elx("details", {className: "details-block review-rewrite-block"});
  const rewrite = elx("textarea", {className: "review-rewrite"});
  rewrite.rows = 3;
  rewrite.placeholder = "可选的审阅人撰写回答。非系统生成。";
  rewriteDetails.append(
    elx("summary", {text: "改写后的回答（可选，人工撰写）"}),
    rewrite,
  );

  article.append(title, metaRow, actions, noteLabel, note, rewriteDetails);
  if (actionStatus) {
    const statusNode = elx("p", {className: "review-action-status"});
    statusNode.appendChild(makeBadge(actionStatus, actionStatusTone));
    article.appendChild(statusNode);
  }
  if (history.length) article.appendChild(makeHistoryNode(history));
  return article;
}

function statusToneOf(status) {
  if (status === "approved" || status === "resolved") return "pass";
  if (status === "rejected" || status === "handoff_failed") return "fail";
  if (status === "changes_requested") return "warn";
  return "neutral";
}

function makeHistoryNode(actions) {
  const details = elx("details", {className: "details-block"});
  details.open = true;
  details.appendChild(elx("summary", {text: `操作历史 (${actions.length})`}));
  const list = elx("ul", {className: "history-list"});
  actions.slice().reverse().forEach((action) => {
    const row = elx("li", {className: "history-row"});
    row.append(
      makeBadge(labelAction(action.action_type)),
      elx("span", {
        className: "mono",
        text: `${labelStatus(action.previous_status)} → ${labelStatus(action.new_status)}`,
      }),
      elx("span", {className: "muted", text: action.reviewer || "匿名"}),
      elx("time", {text: action.created_at}),
    );
    if (action.note) row.appendChild(elx("p", {className: "history-note", text: action.note}));
    list.appendChild(row);
  });
  details.appendChild(list);
  return details;
}

async function handleReviewClick(event) {
  if (!reviewQueueEnabled()) return;
  const target = event.target;
  if (!(target instanceof HTMLElement)) return;
  const item = target.closest(".review-item");
  if (!item) return;
  const reviewId = item.dataset.reviewId;
  if (!reviewId) return;

  if (target.matches("[data-review-history]")) {
    await refreshActionHistory(reviewId);
    return;
  }
  const actionType = target.dataset.reviewAction;
  if (!actionType) return;

  const noteEl = item.querySelector(".review-note");
  const rewriteEl = item.querySelector(".review-rewrite");
  const note = noteEl ? noteEl.value.trim() : "";
  const rewritten = rewriteEl ? rewriteEl.value.trim() : "";

  try {
    state.actionStatus[reviewId] = "处理中…";
    rerenderReview();
    const data = await fetchJson(`/v1/review/queue/${encodeURIComponent(reviewId)}/actions`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        action_type: actionType,
        reviewer: "local_reviewer",
        note: note || null,
        rewritten_answer: rewritten || null,
      }),
    });
    state.actionStatus[reviewId] = `${labelAction(actionType)} → ${labelStatus(data.status)}`;
    await Promise.allSettled([refreshReview(), refreshActionHistory(reviewId)]);
  } catch (error) {
    state.actionStatus[reviewId] = `失败：${messageOf(error)}`;
    rerenderReview();
  }
}

async function refreshActionHistory(reviewId) {
  if (!reviewQueueEnabled()) return;
  try {
    const data = await fetchJson(`/v1/review/queue/${encodeURIComponent(reviewId)}/actions`);
    state.actionHistory[reviewId] = data.actions || [];
    rerenderReview();
  } catch (error) {
    state.actionStatus[reviewId] = `历史失败：${messageOf(error)}`;
    rerenderReview();
  }
}

function rerenderReview() {
  if (state.review) {
    renderReview(state.review);
  }
}
