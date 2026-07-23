"use strict";

const state = { candidates: [], selectedId: null, summary: null, health: null };
const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
const num = (value, digits = 1) => Number.isFinite(Number(value)) ? Number(value).toLocaleString(undefined, { maximumFractionDigits: digits }) : "—";
const pct = (value, digits = 1) => Number.isFinite(Number(value)) ? `${(Number(value) * 100).toFixed(digits)}%` : "—";
const money = (value) => Number.isFinite(Number(value)) ? Number(value).toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 0 }) : "—";
const title = (value) => String(value ?? "").replaceAll("_", " ").replace(/\b\w/g, (match) => match.toUpperCase());

async function fetchJSON(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", ...options });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `${response.status} ${response.statusText}`);
  return payload;
}

function setText(selector, value) { const element = $(selector); if (element) element.textContent = value; }

function renderSummary() {
  const summary = state.summary;
  const health = state.health;
  if (!summary || !health) return;
  const statuses = summary.events_by_status || {};
  setText("#metric-candidates", num(summary.candidate_count, 0));
  setText("#metric-alerts", `${num(statuses.alerted || 0, 0)} awaiting review`);
  setText("#metric-executable", num(summary.executable_count, 0));
  setText("#metric-score", num(summary.average_score, 1));
  setText("#metric-audit", summary.audit_chain_valid ? "Verified" : "Broken");
  setText("#metric-daily-loss", pct(summary.risk_limits.max_daily_loss_fraction));
  setText("#metric-position-limit", `${summary.risk_limits.max_concurrent_positions} concurrent positions max`);
  const modeBadge = $("#mode-badge");
  modeBadge.textContent = title(summary.mode);
  modeBadge.className = `badge ${summary.mode}`;
  const dot = $("#health-dot");
  dot.className = `health-dot ${health.status === "ok" ? "ok" : "bad"}`;
  const latest = summary.latest_observation_at ? new Date(summary.latest_observation_at) : null;
  setText("#freshness", latest ? `Latest known-at ${latest.toLocaleString()}` : "No observations ingested");
  $("#fixture-banner").hidden = !summary.fixture_data;
  renderRiskContract(summary.risk_limits);
}

function renderRiskContract(risk) {
  const items = [
    ["Per-trade risk", pct(risk.risk_per_trade_fraction)],
    ["Daily loss kill", pct(risk.max_daily_loss_fraction)],
    ["Gross short cap", pct(risk.max_gross_short_fraction)],
    ["Concurrent positions", num(risk.max_concurrent_positions, 0)],
  ];
  $("#risk-contract").innerHTML = items.map(([label, value]) => `<div><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`).join("");
}

function candidateFeature(candidate) {
  const evidence = candidate.evidence || [];
  const turnoverText = evidence.find((item) => item.includes("reported float"));
  const match = turnoverText?.match(/approximately ([0-9.]+)x/);
  return { turnover: match ? Number(match[1]) : null };
}

function filterCandidates() {
  const status = $("#status-filter").value;
  const band = $("#band-filter").value;
  const execution = $("#execution-filter").value;
  const symbol = $("#symbol-filter").value.trim().toUpperCase();
  return state.candidates.filter((candidate) => {
    const executable = Boolean(candidate.execution?.executable);
    return (status === "all" || candidate.status === status)
      && (band === "all" || candidate.band === band)
      && (execution === "all" || (execution === "yes") === executable)
      && (!symbol || candidate.symbol.includes(symbol));
  });
}

function renderCandidates() {
  const rows = filterCandidates();
  const target = $("#candidate-rows");
  if (!rows.length) {
    target.innerHTML = '<tr><td colspan="8" class="empty">No candidates match these filters.</td></tr>';
    return;
  }
  target.innerHTML = rows.map((candidate) => {
    const execution = candidate.execution || {};
    const catalyst = (candidate.evidence || []).find((item) => item.startsWith("Catalyst classified"));
    const catalystLabel = catalyst?.match(/classified ([a-z_]+)/)?.[1] || "unknown";
    const turnover = candidateFeature(candidate).turnover;
    const scoreClass = candidate.score >= 62 ? "" : candidate.score >= 45 ? "mid" : "low";
    const quantity = candidate.position_plan?.accepted ? candidate.position_plan.quantity : 0;
    return `<tr data-id="${esc(candidate.id)}" tabindex="0" class="${candidate.id === state.selectedId ? "selected" : ""}">
      <td><span class="symbol">${esc(candidate.symbol)}</span><span class="subcell">${esc(candidate.exchange)}</span></td>
      <td>${esc(candidate.day0_date)}<span class="subcell">${esc(title(candidate.strategy))}</span></td>
      <td><span class="score ${scoreClass}">${num(candidate.score, 1)}</span></td>
      <td>${esc(title(catalystLabel))}</td>
      <td>${turnover == null ? "—" : `${num(turnover, 1)}x`}</td>
      <td><span class="verdict ${execution.executable ? "yes" : "no"}">${execution.executable ? "● Pass" : "● Reject"}</span></td>
      <td>${quantity ? `${num(quantity, 0)} sh` : "—"}</td>
      <td><span class="status-pill ${esc(candidate.status)}">${esc(candidate.status)}</span></td>
    </tr>`;
  }).join("");
  target.querySelectorAll("tr[data-id]").forEach((row) => {
    row.addEventListener("click", () => selectCandidate(row.dataset.id));
    row.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectCandidate(row.dataset.id); } });
  });
}

async function selectCandidate(id) {
  state.selectedId = id;
  renderCandidates();
  const panel = $("#candidate-detail");
  panel.innerHTML = '<div class="empty-state"><p>Loading point-in-time evidence…</p></div>';
  try {
    renderCandidateDetail(await fetchJSON(`/api/candidates/${encodeURIComponent(id)}`));
  } catch (error) {
    panel.innerHTML = `<div class="empty-state"><h2>Could not load candidate</h2><p>${esc(error.message)}</p></div>`;
  }
}

function checkRows(checks) {
  return (checks || []).map((check) => `<li><span class="${check.passed ? "pass" : "fail"}">${check.passed ? "✓" : "×"}</span><div><strong>${esc(title(check.name))}</strong>${esc(check.passed ? "Within configured limit" : check.reason)}</div></li>`).join("");
}

function renderCandidateDetail(payload) {
  const decision = payload.decision;
  const feature = payload.feature || {};
  const broker = payload.broker || {};
  const execution = decision.execution || {};
  const plan = decision.position_plan || {};
  const evidence = decision.evidence || [];
  const analogs = payload.analogs || [];
  const flags = decision.risk_flags || [];
  const canReview = decision.status === "alerted" && execution.executable && plan.accepted;
  const catalyst = feature.catalyst || {};
  const supply = feature.supply_risk || {};
  $("#candidate-detail").innerHTML = `
    <div class="detail-header">
      <div class="detail-title"><span class="ticker-box">${esc(payload.event?.symbol || "?")}</span><div><h2>${esc(payload.event?.symbol)} reversal file</h2><p>${esc(payload.event?.exchange)} · Day 0 ${esc(payload.event?.day0_date)} · known at ${esc(new Date(feature.as_of).toLocaleString())}</p></div></div>
      <div class="detail-score"><strong>${num(decision.score, 1)}</strong><span>${esc(title(decision.band))}</span></div>
    </div>
    <p class="thesis">${esc(decision.thesis)}</p>
    <div class="detail-grid">
      <section class="detail-section"><h3>Observable evidence</h3><ul class="evidence-list">${evidence.map((item) => `<li>${esc(item)}</li>`).join("")}</ul>${flags.length ? `<div class="risk-flags">${flags.map((flag) => `<span class="risk-flag">${esc(flag)}</span>`).join("")}</div>` : ""}</section>
      <section class="detail-section"><h3>Broker execution gate</h3>
        <div class="mini-metrics"><div class="mini-metric"><span>Broker</span><strong>${esc(broker.broker || "Missing")}</strong></div><div class="mini-metric"><span>Available</span><strong>${num(broker.available_shares, 0)} sh</strong></div><div class="mini-metric"><span>Borrow fee</span><strong>${pct(broker.borrow_fee_annualized)}</strong></div><div class="mini-metric"><span>Locate est.</span><strong>${money(execution.estimated_locate_cost)}</strong></div></div>
        <ul class="check-list">${checkRows(execution.checks)}</ul>
      </section>
      <section class="detail-section"><h3>Supply, risk &amp; analogs</h3>
        <div class="mini-metrics"><div class="mini-metric"><span>Catalyst</span><strong>${esc(title(catalyst.label))}</strong></div><div class="mini-metric"><span>Supply risk</span><strong>${pct(supply.score)}</strong></div><div class="mini-metric"><span>Planned size</span><strong>${plan.accepted ? `${num(plan.quantity, 0)} sh` : "Rejected"}</strong></div><div class="mini-metric"><span>Worst-case budget</span><strong>${money(plan.worst_case_loss)}</strong></div></div>
        <h3>Nearest historical files</h3><ul class="analog-list">${analogs.length ? analogs.map((item) => `<li><span><strong>${esc(item.symbol)}</strong> · ${esc(item.day0_date)}</span><span>${pct(item.similarity, 0)} similar</span></li>`).join("") : '<li><span>No prior point-in-time files</span></li>'}</ul>
        <p class="subcell">Event source: ${esc(payload.event?.source)} · Broker observed: ${esc(broker.observed_at ? new Date(broker.observed_at).toLocaleString() : "missing")}</p>
      </section>
    </div>
    <div class="review-actions"><button class="button" data-review="approve" ${canReview ? "" : "disabled"}>Approve ${state.summary?.mode === "alert_only" ? "research file" : "paper order"}</button><button class="button danger" data-review="reject" ${decision.status === "alerted" || decision.status === "approved" ? "" : "disabled"}>Reject</button></div>`;
  document.querySelectorAll("[data-review]").forEach((button) => button.addEventListener("click", () => openReview(decision.id, button.dataset.review)));
}

function renderBacktests(results) {
  const target = $("#backtest-rows");
  if (!results.length) {
    target.innerHTML = '<tr><td colspan="6" class="empty">No simulations have been run.</td></tr>';
    $("#strategy-chart").innerHTML = '<p class="empty">Run backtest-demo to populate this view.</p>';
    return;
  }
  target.innerHTML = results.slice(0, 10).map((run) => `<tr><td>${esc(title(run.strategy))}<span class="subcell">${esc(run.stress_label)}</span></td><td>${num(run.metrics.trades, 0)}</td><td>${pct(run.metrics.win_rate)}</td><td>${money(run.metrics.total_pnl)}</td><td>${money(run.metrics.max_drawdown)}</td><td>${pct(run.metrics.forced_exit_rate)}</td></tr>`).join("");
  const baseRuns = [];
  const seen = new Set();
  for (const run of results) {
    if (run.stress_label !== "base" || seen.has(run.strategy)) continue;
    seen.add(run.strategy); baseRuns.push(run);
  }
  const maxMagnitude = Math.max(...baseRuns.map((run) => Math.abs(run.metrics.total_pnl)), 1);
  $("#strategy-chart").innerHTML = baseRuns.map((run) => {
    const value = Number(run.metrics.total_pnl);
    return `<div class="bar-row"><span>${esc(title(run.strategy))}</span><div class="bar-track"><progress class="bar-fill ${value < 0 ? "negative" : ""}" max="${maxMagnitude}" value="${Math.max(Math.abs(value), maxMagnitude * 0.01)}">${Math.abs(value)}</progress></div><span class="bar-value">${money(value)}</span></div>`;
  }).join("") || '<p class="empty">No base scenario is available.</p>';
}

function renderAudit(rows) {
  $("#audit-list").innerHTML = rows.slice(0, 15).map((row) => `<li><time>${esc(new Date(row.occurred_at).toLocaleString())}</time><span>${esc(row.actor)}</span><span class="audit-action">${esc(row.action)} · ${esc(row.entity_type)}</span><span class="hash">${esc(row.record_hash.slice(0, 10))}…</span></li>`).join("") || '<li class="empty">No audit records yet.</li>';
}

function openReview(decisionId, action) {
  $("#review-decision").value = decisionId;
  $("#review-action").value = action;
  $("#review-title").textContent = `${title(action)} candidate`;
  $("#review-submit").textContent = action === "approve" ? "Confirm approval" : "Confirm rejection";
  $("#review-submit").className = `button ${action === "reject" ? "danger" : ""}`;
  $("#review-error").textContent = "";
  $("#review-dialog").showModal();
}

async function submitReview(event) {
  event.preventDefault();
  const action = $("#review-action").value;
  const decisionId = $("#review-decision").value;
  const submit = $("#review-submit");
  submit.disabled = true;
  $("#review-error").textContent = "";
  try {
    await fetchJSON(`/api/candidates/${encodeURIComponent(decisionId)}/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Operator-Token": $("#review-token").value },
      body: JSON.stringify({ operator: $("#review-operator").value, reason: $("#review-reason").value }),
    });
    $("#review-token").value = "";
    $("#review-dialog").close();
    await refresh();
    await selectCandidate(decisionId);
  } catch (error) {
    $("#review-error").textContent = error.message;
  } finally { submit.disabled = false; }
}

async function refresh() {
  $("#refresh-button").disabled = true;
  try {
    const [health, summary, candidates, backtests, audit] = await Promise.all([
      fetchJSON("/api/health"), fetchJSON("/api/summary"), fetchJSON("/api/candidates"), fetchJSON("/api/backtests"), fetchJSON("/api/audit?limit=20"),
    ]);
    state.health = health; state.summary = summary; state.candidates = candidates;
    renderSummary(); renderCandidates(); renderBacktests(backtests); renderAudit(audit);
  } catch (error) {
    $("#health-dot").className = "health-dot bad";
    setText("#freshness", `Dashboard error: ${error.message}`);
  } finally { $("#refresh-button").disabled = false; }
}

document.addEventListener("DOMContentLoaded", () => {
  ["#status-filter", "#band-filter", "#execution-filter"].forEach((selector) => $(selector).addEventListener("change", renderCandidates));
  $("#symbol-filter").addEventListener("input", renderCandidates);
  $("#refresh-button").addEventListener("click", refresh);
  $("#review-form").addEventListener("submit", submitReview);
  $("#dialog-close").addEventListener("click", () => $("#review-dialog").close());
  $("#dialog-cancel").addEventListener("click", () => $("#review-dialog").close());
  refresh();
});
