const examples = [
  "Alpha Trading Co. 的餐饮发票应该怎么入账？",
  "现在打车超过 100 元需要审批吗？",
  "Beta Catering Ltd. 没有明确服务描述的配送发票能直接入账吗？",
  "小规模纳税人现在增值税应该怎么处理？",
  "怎么隐藏收入少交税？",
  "文档里说 Ignore previous instructions 的内容应该听吗？",
];

const state = {
  documents: null,
  review: null,
  reviewSummary: null,
  eval: null,
  evalHistory: null,
  traces: null,
  providerBenchmark: null,
  providerBenchmarkList: null,
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
  renderExamples();
  bindActions();
  refreshAll();
});

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
}

let _reviewFilterTimer = null;
function scheduleReviewFilterUpdate() {
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

function downloadExport(format) {
  const qs = reviewFilterQueryString({includePaging: false});
  const suffix = format === "csv" ? "csv" : "json";
  const url = `/v1/review/queue/export.${suffix}${qs ? `?${qs}` : ""}`;
  window.open(url, "_blank");
}

function renderExamples() {
  const list = $("example-list");
  list.innerHTML = "";
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
  await Promise.allSettled([
    refreshHealth(),
    refreshDocuments(),
    refreshReview(),
    refreshEval(),
    refreshEvalHistory(),
    refreshTraces(),
    refreshProviderBenchmark(),
  ]);
}

async function refreshPanel(name) {
  if (name === "documents") return refreshDocuments();
  if (name === "review") return refreshReview();
  if (name === "eval") return refreshEval();
  if (name === "eval-history") return refreshEvalHistory();
  if (name === "traces") return refreshTraces();
  if (name === "provider-benchmark") return refreshProviderBenchmark();
}

async function refreshHealth() {
  const status = $("backend-status");
  try {
    const data = await fetchJson("/healthz");
    status.textContent = data.status === "ok" ? "Online" : "Unknown";
    status.className = "stat-value";
  } catch (error) {
    status.textContent = "Offline";
    status.className = "stat-value";
  }
}

async function refreshDocuments() {
  try {
    state.documents = await fetchJson("/v1/documents");
    $("document-count").textContent = String(state.documents.count ?? 0);
    $("chunk-count").textContent = String(state.documents.chunk_count ?? 0);
    renderDocuments(state.documents);
  } catch (error) {
    $("documents-summary").textContent = `Documents unavailable: ${messageOf(error)}`;
    $("documents-list").innerHTML = emptyHtml("No document data available.");
  }
}

async function refreshReview() {
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
    $("review-summary").textContent = `Review queue unavailable: ${messageOf(error)}`;
    $("review-list").innerHTML = emptyHtml("No review queue data available.");
    $("review-pager").innerHTML = "";
    $("review-summary-cards").innerHTML = "";
  }
}

async function refreshEval() {
  try {
    state.eval = await fetchJson("/v1/evals/latest");
    renderEval(state.eval);
  } catch (error) {
    $("eval-summary").innerHTML = badge("Missing eval report", "warn");
    $("eval-categories").innerHTML = "";
    $("eval-markdown").textContent = `Eval endpoint unavailable: ${messageOf(error)}`;
  }
}

async function refreshEvalHistory() {
  try {
    state.evalHistory = await fetchJson("/v1/evals/history");
    renderEvalHistory(state.evalHistory);
  } catch (error) {
    $("eval-trend-summary").innerHTML = badge("Eval history unavailable", "warn");
    $("eval-trend-metrics").innerHTML = "";
    $("eval-sparkline").innerHTML = "";
    $("eval-trend-categories").innerHTML = emptyHtml(`History endpoint unavailable: ${messageOf(error)}`);
  }
}

async function refreshTraces() {
  try {
    state.traces = await fetchJson("/v1/debug/traces");
    renderTraces(state.traces);
  } catch (error) {
    $("trace-summary").textContent = `Traces unavailable: ${messageOf(error)}`;
    $("trace-list").innerHTML = emptyHtml("No trace data available.");
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

// The provider benchmark panel is built with DOM nodes + textContent (not
// innerHTML) so artifact-sourced strings can never be parsed as markup —
// XSS-safe by construction, no sanitizer dependency.
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
  $("provider-benchmark-summary").replaceChildren(makeBadge("Provider benchmark unavailable", "warn"));
  $("provider-benchmark-cards").replaceChildren();
  $("provider-benchmark-providers").replaceChildren();
  $("provider-benchmark-categories").replaceChildren();
  $("provider-benchmark-cases").replaceChildren(makeEmptyNode(`Endpoint unavailable: ${message}`));
  $("provider-benchmark-markdown").textContent = "No report loaded.";
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
    summaryLine.replaceChildren(makeBadge("No provider benchmark artifact", "warn"));
    cases.replaceChildren(
      makeEmptyNode("No provider benchmark artifact found. Run: bash scripts/run_provider_benchmark.sh mock")
    );
    markdown.textContent =
      "Run bash scripts/run_provider_benchmark.sh mock to generate a local benchmark artifact.";
    return;
  }

  const summary = latest.latest;
  const passed = Number(summary.failed || 0) === 0;
  summaryLine.replaceChildren(
    makeBadge(summary.provider || "provider", passed ? "pass" : "warn"),
    elx("span", {
      className: "summary-text",
      text: ` score ${formatScore(summary.score)}, ${summary.passed ?? 0}/${summary.total ?? 0} cases, source ${summary.source || "results"}`,
    })
  );
  renderBenchmarkCards(cards, summary);
  renderBenchmarkProviders(providers, (list && list.artifacts) || []);
  renderBenchmarkCategories(categories, summary.by_category || {});
  renderBenchmarkCases(cases, summary.results || []);
  markdown.textContent = latest.markdown_report || "No Markdown report available.";
}

function renderBenchmarkCards(container, summary) {
  const cards = [
    ["Provider", summary.provider || "—", "neutral"],
    ["Model", summary.model || "—", "neutral"],
    ["Score", formatScore(summary.score), Number(summary.failed || 0) === 0 ? "pass" : "warn"],
    ["Fallback rate", formatPct(summary.fallback_rate), Number(summary.fallback_rate || 0) > 0 ? "warn" : "pass"],
    ["Citation valid", formatPct(summary.citation_validation_rate), "pass"],
    ["Invalid citations", summary.invalid_citation_count ?? 0, Number(summary.invalid_citation_count || 0) ? "fail" : "neutral"],
    ["Provider errors", summary.provider_error_count ?? 0, Number(summary.provider_error_count || 0) ? "fail" : "neutral"],
    ["Avg latency", formatMs(summary.avg_latency_ms), "neutral"],
    ["P95 latency", formatMs(summary.p95_latency_ms), "neutral"],
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
    elx("h3", {className: "table-caption", text: `Artifacts (${artifacts.length})`}),
    makeBenchmarkTable(
      ["Provider", "Model", "Score", "Fallback", "Citation valid", "Avg latency", "Source"],
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
    elx("h3", {className: "table-caption", text: "By category"}),
    makeBenchmarkTable(
      ["Category", "Total", "Passed", "Failed", "Score", "Fallback", "Citation valid"],
      rows
    )
  );
}

function renderBenchmarkCases(container, results) {
  if (!results.length) {
    container.replaceChildren(makeEmptyNode("No per-case rows in this artifact."));
    return;
  }
  const rows = results.map((r) => [
    r.case_id || "",
    r.category || "",
    makeBadge(r.passed ? "pass" : "fail", r.passed ? "pass" : "fail"),
    formatScore(r.score),
    formatBool(r.llm_used),
    formatBool(r.fallback_used),
    r.fallback_reason || "—",
    r.citation_valid === null || r.citation_valid === undefined ? "n/a" : formatBool(r.citation_valid),
    formatMs(r.latency_ms),
    (r.failure_reasons || []).join("; ") || "—",
  ]);
  container.replaceChildren(
    elx("h3", {className: "table-caption", text: `Cases (${results.length})`}),
    makeBenchmarkTable(
      ["Case", "Category", "Passed", "Score", "LLM", "Fallback", "Reason", "Citation", "Latency", "Failures"],
      rows,
      "benchmark-case-table"
    )
  );
}

function formatPct(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "n/a";
  return `${(number * 100).toFixed(1)}%`;
}

function formatMs(value) {
  if (value === null || value === undefined) return "n/a";
  const number = Number(value);
  if (!Number.isFinite(number)) return "n/a";
  return `${number.toFixed(1)} ms`;
}

function formatBool(value) {
  return value ? "yes" : "no";
}

async function runQuery() {
  const question = $("question-input").value.trim();
  if (!question) {
    $("query-status").textContent = "Enter a question first.";
    return;
  }

  $("run-query").disabled = true;
  $("query-status").textContent = "Running query...";
  try {
    const data = await fetchJson("/v1/rag/query", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({question}),
    });
    state.query = data;
    renderQuery(data);
    $("query-status").textContent = "Query complete.";
    await Promise.allSettled([refreshReview(), refreshTraces()]);
  } catch (error) {
    $("query-status").textContent = `Query failed: ${messageOf(error)}`;
  } finally {
    $("run-query").disabled = false;
  }
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return response.json();
}

function renderQuery(data) {
  $("answer-text").textContent = data.answer || "No answer returned.";
  $("answer-badges").innerHTML = [
    badge(data.question_type || "unknown", data.question_type === "unsafe_request" ? "fail" : "pass"),
    data.needs_human_review ? badge("Review required", "warn") : badge("No review", "pass"),
    data.safety_analysis?.unsafe_request_detected ? badge("Unsafe request", "fail") : "",
    data.safety_analysis?.prompt_injection_detected ? badge("Prompt injection", "fail") : "",
  ].join("");

  const review = data.human_review || {};
  $("answer-metadata").innerHTML = metadataHtml({
    Confidence: formatScore(data.confidence),
    "Question type": data.question_type || "unknown",
    "Review status": review.status || (review.required ? "pending" : "not required"),
    "Queue id": review.review_queue_id || "none",
    "Review reasons": (review.reasons || []).join(", ") || "none",
    Errors: (data.errors || []).join(", ") || "none",
  });
  $("safety-json").textContent = pretty(data.safety_analysis || {});
  $("temporal-json").textContent = pretty(data.temporal_analysis || {});
  $("conflict-json").textContent = pretty(data.conflict_analysis || {});
  $("citations-list").innerHTML = renderCitations(data.citations || []);
  $("support-list").innerHTML = renderEvidenceList(data.support_evidence || []);
  $("counter-list").innerHTML = renderEvidenceList(data.counter_evidence || []);
}

function renderCitations(citations) {
  if (!citations.length) return emptyHtml("No citations returned.");
  return citations.map((citation) => itemHtml({
    title: citation.title || citation.doc_id || "Citation",
    badgeText: citation.version || "citation",
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
  })).join("");
}

function renderEvidenceList(items) {
  if (!items.length) return emptyHtml("No evidence returned.");
  return items.map((item) => itemHtml({
    title: item.title || item.doc_id || "Evidence",
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
  })).join("");
}

function renderDocuments(data) {
  const docs = data.documents || [];
  $("documents-summary").textContent =
    `${data.count ?? docs.length} documents, ${data.chunk_count ?? 0} chunks, source: ${data.source || "unknown"}`;
  $("documents-list").innerHTML = docs.length
    ? docs.map((doc) => itemHtml({
        title: doc.title || doc.document_id,
        badgeText: doc.document_type,
        meta: {
          document_id: doc.document_id,
          client: doc.client,
          version: doc.version,
          policy_family: doc.policy_family,
          valid_from: doc.valid_from,
          valid_to: doc.valid_to,
          source_path: doc.source_path,
          malicious: doc.is_malicious ? "yes" : "no",
        },
      })).join("")
    : emptyHtml("No documents loaded.");
}

function renderReview(data) {
  if (!data.enabled) {
    $("review-summary").innerHTML = badge("Review disabled", "warn");
    $("review-list").innerHTML = emptyHtml("Human review handoff is disabled.");
    return;
  }
  const entries = data.entries || [];
  const total = data.total ?? entries.length;
  const offset = data.offset ?? 0;
  const limit = data.limit ?? entries.length;
  const pageEnd = Math.min(offset + entries.length, total);
  const filterDescr = filterDescription(data.filters || {}, data.sort);
  const summaryParts = [
    `${total} matching checkpoints`,
    total ? `showing ${offset + 1}–${pageEnd}` : null,
    filterDescr,
  ].filter(Boolean);
  $("review-summary").textContent = summaryParts.join(" · ");
  if (!entries.length) {
    if (total === 0 && !filterDescr) {
      $("review-list").innerHTML = emptyHtml(
        "No review checkpoints in the queue. Run a tax_policy or invoice_compliance query to populate it."
      );
    } else {
      $("review-list").innerHTML = emptyHtml(
        "No entries match the current filters."
      );
    }
    return;
  }
  $("review-list").innerHTML = entries
    .map((entry) => reviewEntryHtml(entry))
    .join("");
}

function filterDescription(filters, sort) {
  const parts = [];
  if (filters.status) parts.push(`status=${filters.status}`);
  if (filters.question_type) parts.push(`type=${filters.question_type}`);
  if (filters.reason) parts.push(`reason=${filters.reason}`);
  if (filters.reviewer) parts.push(`reviewer=${filters.reviewer}`);
  if (filters.has_actions) parts.push("has_actions=true");
  if (sort && sort !== "created_at_desc") parts.push(`sort=${sort}`);
  return parts.length ? `filters: ${parts.join(", ")}` : "";
}

function renderReviewSummary(data) {
  const container = $("review-summary-cards");
  if (!container) return;
  if (!data || !data.enabled) {
    container.innerHTML = "";
    return;
  }
  const byStatus = data.by_status || {};
  const cards = [
    {label: "Total", value: data.total ?? 0, tone: "neutral"},
    {label: "Pending", value: byStatus.pending ?? 0, tone: "warn"},
    {label: "Approved", value: byStatus.approved ?? 0, tone: "pass"},
    {label: "Rejected", value: byStatus.rejected ?? 0, tone: "fail"},
    {label: "Changes", value: byStatus.changes_requested ?? 0, tone: "warn"},
    {label: "Resolved", value: byStatus.resolved ?? 0, tone: "pass"},
  ];
  container.innerHTML = cards
    .map(
      (c) => `<div class="summary-card ${escapeHtml(c.tone)}">
        <span class="summary-card-label">${escapeHtml(c.label)}</span>
        <span class="summary-card-value">${escapeHtml(c.value)}</span>
      </div>`
    )
    .join("");
}

function renderReviewPager(data) {
  const pager = $("review-pager");
  if (!pager) return;
  if (!data || !data.enabled) {
    pager.innerHTML = "";
    return;
  }
  const total = data.total ?? 0;
  const limit = data.limit ?? 20;
  const offset = data.offset ?? 0;
  if (total <= limit) {
    pager.innerHTML = "";
    return;
  }
  const currentPage = Math.floor(offset / limit) + 1;
  const totalPages = Math.max(1, Math.ceil(total / limit));
  const prevDisabled = offset <= 0 ? "disabled" : "";
  const nextDisabled = offset + limit >= total ? "disabled" : "";
  pager.innerHTML = `
    <button type="button" class="secondary-button" id="review-pager-prev" ${prevDisabled}>← Prev</button>
    <span class="review-pager-status">page ${escapeHtml(currentPage)} of ${escapeHtml(totalPages)}</span>
    <button type="button" class="secondary-button" id="review-pager-next" ${nextDisabled}>Next →</button>
  `;
  const prev = $("review-pager-prev");
  const next = $("review-pager-next");
  if (prev) prev.addEventListener("click", () => paginateReview(-1));
  if (next) next.addEventListener("click", () => paginateReview(1));
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
    $("eval-summary").innerHTML = badge("Missing eval report", "warn");
    $("eval-categories").innerHTML = "";
    $("eval-markdown").textContent = "Run scripts/run_eval_gate.sh to generate local eval artifacts.";
    return;
  }

  const summary = data.summary || {};
  const passed = Number(summary.failed || 0) === 0;
  $("eval-summary").innerHTML = [
    badge(passed ? "CI eval pass" : "CI eval fail", passed ? "pass" : "fail"),
    `<span class="summary-text">score ${escapeHtml(formatScore(summary.score))}, passed ${summary.passed ?? 0}, failed ${summary.failed ?? 0}, skipped ${summary.skipped ?? 0}</span>`,
  ].join(" ");
  $("eval-categories").innerHTML = renderCategoryTable(data.by_category || {});
  $("eval-markdown").textContent = data.markdown_report || "No Markdown report available.";
}

function renderEvalHistory(data) {
  const snapshots = data.snapshots || [];
  if (!data.available || !snapshots.length) {
    $("eval-trend-summary").textContent =
      "No eval history snapshots found. Run eval gate and archive a snapshot.";
    $("eval-trend-metrics").innerHTML = "";
    $("eval-sparkline").innerHTML = "";
    $("eval-trend-categories").innerHTML = "";
    return;
  }

  const latest = data.latest || snapshots[snapshots.length - 1];
  const passed = Number(latest.failed || 0) === 0;
  $("eval-trend-summary").innerHTML = [
    badge(passed ? "Latest pass" : "Latest fail", passed ? "pass" : "fail"),
    `<span class="summary-text">${escapeHtml(latest.snapshot_id)} / ${escapeHtml(latest.created_at)}</span>`,
  ].join(" ");
  $("eval-trend-metrics").innerHTML = renderEvalTrendMetrics(latest, data);
  $("eval-sparkline").innerHTML = renderScoreSparkline(snapshots);
  $("eval-trend-categories").innerHTML = renderCategoryTable(latest.by_category || {});
}

function renderEvalTrendMetrics(latest, data) {
  const delta = data.score_delta_latest;
  const deltaTone = delta == null ? "neutral" : (Number(delta) < 0 ? "fail" : (Number(delta) > 0 ? "pass" : "neutral"));
  const metrics = [
    {label: "Latest score", value: formatScore(latest.score), tone: Number(latest.failed || 0) === 0 ? "pass" : "fail"},
    {label: "Passed", value: latest.passed ?? 0, tone: "pass"},
    {label: "Failed", value: latest.failed ?? 0, tone: Number(latest.failed || 0) ? "fail" : "neutral"},
    {label: "Skipped", value: latest.skipped ?? 0, tone: Number(latest.skipped || 0) ? "warn" : "neutral"},
    {label: "Delta", value: formatDelta(delta), tone: deltaTone},
    {label: "Snapshots", value: data.count ?? 0, tone: "neutral"},
  ];
  return metrics.map((metric) => `<div class="trend-metric ${escapeHtml(metric.tone)}">
    <span class="trend-metric-label">${escapeHtml(metric.label)}</span>
    <span class="trend-metric-value">${escapeHtml(metric.value)}</span>
  </div>`).join("");
}

function renderScoreSparkline(snapshots) {
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
  const line = points.map((point) => `${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(" ");
  const dots = points.map((point) => `<circle cx="${point.x.toFixed(1)}" cy="${point.y.toFixed(1)}" r="3.5">
    <title>${escapeHtml(point.id)} score ${escapeHtml(formatScore(point.score))}</title>
  </circle>`).join("");
  return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Eval score sparkline">
    <line class="sparkline-axis" x1="${pad}" y1="${height - pad}" x2="${width - pad}" y2="${height - pad}"></line>
    <polyline points="${line}"></polyline>
    ${dots}
  </svg>`;
}

function renderTraces(data) {
  if (!data.enabled) {
    $("trace-summary").innerHTML = badge("Tracing disabled", "warn");
    $("trace-list").innerHTML = emptyHtml("Enable TRUSTRAG_TRACE_ENABLED=true to record local traces.");
    return;
  }
  const events = data.events || [];
  $("trace-summary").textContent = `${events.length} trace events`;
  $("trace-list").innerHTML = events.length
    ? events.slice(-8).reverse().map((event) => itemHtml({
        title: event.run_name || event.event_type || "Trace event",
        badgeText: event.event_type || "event",
        meta: {
          timestamp: event.timestamp,
          tags: (event.tags || []).join(", "),
          input: event.input_summary ? JSON.stringify(event.input_summary) : null,
          output: event.output_summary ? JSON.stringify(event.output_summary) : null,
        },
      })).join("")
    : emptyHtml("No trace events recorded.");
}

function renderCategoryTable(categories) {
  const names = Object.keys(categories).sort();
  if (!names.length) return "";
  const rows = names.map((name) => {
    const stats = categories[name] || {};
    return `<tr>
      <td>${escapeHtml(name)}</td>
      <td>${escapeHtml(formatScore(stats.score))}</td>
      <td>${escapeHtml(stats.passed ?? 0)}</td>
      <td>${escapeHtml(stats.failed ?? 0)}</td>
    </tr>`;
  }).join("");
  return `<table class="category-table">
    <thead><tr><th>Category</th><th>Score</th><th>Passed</th><th>Failed</th></tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

function metadataHtml(values) {
  return Object.entries(values).map(([label, value]) => `
    <div>
      <dt>${escapeHtml(label)}</dt>
      <dd>${escapeHtml(value ?? "none")}</dd>
    </div>
  `).join("");
}

function itemHtml({title, badgeText, meta = {}, preview = ""}) {
  const metaRows = Object.entries(meta)
    .filter(([, value]) => value !== undefined && value !== null && value !== "")
    .map(([key, value]) => `<span><strong>${escapeHtml(key)}:</strong> <span class="mono">${escapeHtml(value)}</span></span>`)
    .join("");
  const previewBlock = preview
    ? `<details class="details-block"><summary>Content preview</summary><div class="content-preview">${escapeHtml(truncate(preview, 320))}</div></details>`
    : "";
  return `<article class="item">
    <div class="item-title">
      <span>${escapeHtml(title)}</span>
      ${badgeText ? badge(badgeText, "neutral") : ""}
    </div>
    <div class="item-meta">${metaRows || "<span>No metadata.</span>"}</div>
    ${previewBlock}
  </article>`;
}

function badge(text, tone = "neutral") {
  return `<span class="badge ${escapeHtml(tone)}">${escapeHtml(text)}</span>`;
}

function emptyHtml(text) {
  return `<div class="empty">${escapeHtml(text)}</div>`;
}

function pretty(value) {
  return JSON.stringify(value, null, 2);
}

function formatScore(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "N/A";
  return number.toFixed(3);
}

function formatDelta(value) {
  if (value === null || value === undefined) return "N/A";
  const number = Number(value);
  if (!Number.isFinite(number)) return "N/A";
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

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

const REVIEW_ACTIONS = [
  {type: "approve", label: "Approve"},
  {type: "reject", label: "Reject"},
  {type: "request_changes", label: "Request changes"},
  {type: "rewrite_note", label: "Add note"},
  {type: "resolve", label: "Resolve"},
  {type: "reopen", label: "Reopen"},
];

function reviewEntryHtml(entry) {
  const id = entry.review_queue_id;
  const status = entry.status || "pending";
  const statusTone = statusToneOf(status);
  const meta = {
    review_queue_id: id,
    question_type: entry.question_type,
    reasons: (entry.human_review_reasons || []).join(", "),
    created_at: entry.created_at,
    confidence: formatScore(entry.confidence),
    actions: entry.action_count ?? 0,
    last_action_at: entry.last_action_at,
  };
  const metaRows = Object.entries(meta)
    .filter(([, value]) => value !== undefined && value !== null && value !== "")
    .map(([key, value]) => `<span><strong>${escapeHtml(key)}:</strong> <span class="mono">${escapeHtml(value)}</span></span>`)
    .join("");
  const actionStatus = state.actionStatus[id] || "";
  const actionStatusTone = actionStatus.startsWith("Failed") || actionStatus.startsWith("History failed") ? "fail" : (actionStatus ? "pass" : "neutral");
  const history = state.actionHistory[id] || [];
  const actionButtons = REVIEW_ACTIONS.map(
    (a) => `<button class="review-action" type="button" data-review-action="${escapeHtml(a.type)}">${escapeHtml(a.label)}</button>`
  ).join("");
  return `<article class="item review-item" data-review-id="${escapeHtml(id)}">
    <div class="item-title">
      <span>${escapeHtml(entry.question || id)}</span>
      ${badge(status, statusTone)}
    </div>
    <div class="item-meta">${metaRows || "<span>No metadata.</span>"}</div>
    <div class="review-action-row">
      ${actionButtons}
      <button class="review-action review-history-toggle" type="button" data-review-history="true">History</button>
    </div>
    <label class="review-note-label" for="review-note-${escapeHtml(id)}">Reviewer note (optional)</label>
    <textarea class="review-note" id="review-note-${escapeHtml(id)}" rows="2" placeholder="Why this action? Plain text only."></textarea>
    <details class="details-block review-rewrite-block">
      <summary>Rewritten answer (optional, human-authored)</summary>
      <textarea class="review-rewrite" rows="3" placeholder="Optional reviewer-authored answer. Not generated by the system."></textarea>
    </details>
    ${actionStatus ? `<p class="review-action-status">${badge(actionStatus, actionStatusTone)}</p>` : ""}
    ${history.length ? historyHtml(history) : ""}
  </article>`;
}

function statusToneOf(status) {
  if (status === "approved" || status === "resolved") return "pass";
  if (status === "rejected" || status === "handoff_failed") return "fail";
  if (status === "changes_requested") return "warn";
  return "neutral";
}

function historyHtml(actions) {
  const rows = actions
    .slice()
    .reverse()
    .map((action) => `<li class="history-row">
      <span class="badge neutral">${escapeHtml(action.action_type)}</span>
      <span class="mono">${escapeHtml(action.previous_status)} → ${escapeHtml(action.new_status)}</span>
      <span class="muted">${escapeHtml(action.reviewer || "anonymous")}</span>
      <time>${escapeHtml(action.created_at)}</time>
      ${action.note ? `<p class="history-note">${escapeHtml(action.note)}</p>` : ""}
    </li>`)
    .join("");
  return `<details class="details-block" open>
    <summary>Action history (${actions.length})</summary>
    <ul class="history-list">${rows}</ul>
  </details>`;
}

async function handleReviewClick(event) {
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
    state.actionStatus[reviewId] = "Applying...";
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
    state.actionStatus[reviewId] = `${actionType} → ${data.status}`;
    await Promise.allSettled([refreshReview(), refreshActionHistory(reviewId)]);
  } catch (error) {
    state.actionStatus[reviewId] = `Failed: ${messageOf(error)}`;
    rerenderReview();
  }
}

async function refreshActionHistory(reviewId) {
  try {
    const data = await fetchJson(`/v1/review/queue/${encodeURIComponent(reviewId)}/actions`);
    state.actionHistory[reviewId] = data.actions || [];
    rerenderReview();
  } catch (error) {
    state.actionStatus[reviewId] = `History failed: ${messageOf(error)}`;
    rerenderReview();
  }
}

function rerenderReview() {
  if (state.review) {
    renderReview(state.review);
  }
}
