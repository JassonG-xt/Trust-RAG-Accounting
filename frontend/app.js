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
  eval: null,
  traces: null,
  query: null,
  actionHistory: {},
  actionStatus: {},
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
    refreshTraces(),
  ]);
}

async function refreshPanel(name) {
  if (name === "documents") return refreshDocuments();
  if (name === "review") return refreshReview();
  if (name === "eval") return refreshEval();
  if (name === "traces") return refreshTraces();
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
  try {
    state.review = await fetchJson("/v1/review/queue");
    renderReview(state.review);
  } catch (error) {
    $("review-summary").textContent = `Review queue unavailable: ${messageOf(error)}`;
    $("review-list").innerHTML = emptyHtml("No review queue data available.");
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

async function refreshTraces() {
  try {
    state.traces = await fetchJson("/v1/debug/traces");
    renderTraces(state.traces);
  } catch (error) {
    $("trace-summary").textContent = `Traces unavailable: ${messageOf(error)}`;
    $("trace-list").innerHTML = emptyHtml("No trace data available.");
  }
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
  $("review-summary").textContent = `${data.count ?? entries.length} review checkpoints`;
  $("review-list").innerHTML = entries.length
    ? entries.map((entry) => reviewEntryHtml(entry)).join("")
    : emptyHtml("No review checkpoints in the queue.");
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
