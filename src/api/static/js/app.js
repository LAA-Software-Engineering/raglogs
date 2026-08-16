"use strict";

// ── Utilities ────────────────────────────────────────────────────────────────

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value === null || value === undefined ? "" : String(value);
  return div.innerHTML;
}

function fmtTime(iso) {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString();
  } catch (e) {
    return iso;
  }
}

function confidenceBadge(confidence) {
  const cls = confidence === "high" ? "badge-high" : confidence === "medium" ? "badge-medium" : "badge-low";
  return `<span class="badge ${cls}">${escapeHtml(confidence)}</span>`;
}

// ── Ingestion picker ─────────────────────────────────────────────────────────

async function loadIngestions() {
  const select = document.getElementById("ingestion-select");
  try {
    const resp = await fetch("/ingestions");
    const data = await resp.json();
    const ingestions = data.ingestions || [];

    select.innerHTML = "";

    const allOption = document.createElement("option");
    allOption.value = "";
    allOption.textContent = "All ingestions";
    select.appendChild(allOption);

    ingestions.forEach((ing) => {
      const opt = document.createElement("option");
      opt.value = ing.ingestion_job_id;
      const shortId = ing.ingestion_job_id.slice(0, 8);
      const when = fmtTime(ing.finished_at);
      opt.textContent = `${shortId} · ${ing.source_name} · ${ing.parsed_count} logs · ${when}`;
      select.appendChild(opt);
    });

    // Default to the most recent completed ingestion, matching CLI behavior.
    if (ingestions.length) select.value = ingestions[0].ingestion_job_id;
  } catch (e) {
    select.innerHTML = '<option value="">could not load ingestions</option>';
  } finally {
    select.disabled = false;
  }
}

function selectedIngestionJobId() {
  const value = document.getElementById("ingestion-select").value;
  return value || null;
}

// ── Tabs ─────────────────────────────────────────────────────────────────────

function initTabs() {
  const buttons = document.querySelectorAll(".tab-btn");
  buttons.forEach((btn) => {
    btn.addEventListener("click", () => {
      buttons.forEach((b) => b.classList.remove("active"));
      document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      document.getElementById(`panel-${btn.dataset.tab}`).classList.add("active");
    });
  });
}

// ── Presets ──────────────────────────────────────────────────────────────────

function initPresets() {
  document.querySelectorAll(".window-form").forEach((form) => {
    const sinceInput = form.querySelector('input[name="since"]');
    form.querySelectorAll(".preset-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        form.querySelectorAll(".preset-btn").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        sinceInput.value = btn.dataset.since;
      });
    });
  });
}

// ── Form submission ──────────────────────────────────────────────────────────

function formToBody(form) {
  const body = {};
  new FormData(form).forEach((value, key) => {
    const trimmed = String(value).trim();
    if (trimmed !== "") body[key] = trimmed;
  });

  const jobId = selectedIngestionJobId();
  if (jobId) {
    body.ingestion_job_id = jobId;
  } else {
    // "All ingestions" selected. Explain/ask already merge all ingestions
    // when ingestion_job_id is omitted; timeline/compare need this explicit
    // flag or they'll silently fall back to the latest ingestion.
    body.all_ingestions = true;
  }
  return body;
}

const renderers = {
  explain: renderExplain,
  timeline: renderTimeline,
  compare: renderCompare,
  ask: renderAsk,
};

function initForms() {
  document.querySelectorAll(".window-form").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const endpoint = form.dataset.endpoint;
      const renderKey = form.dataset.render;
      const resultEl = document.getElementById(`result-${renderKey}`);
      const submitBtn = form.querySelector(".run-btn");

      submitBtn.disabled = true;
      resultEl.innerHTML = '<p class="empty-state">Loading…</p>';

      try {
        const resp = await fetch(endpoint, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(formToBody(form)),
        });
        const data = await resp.json();
        if (!resp.ok) {
          resultEl.innerHTML = `<p class="error-state">${escapeHtml(data.detail || "Request failed")}</p>`;
          return;
        }
        renderers[renderKey](resultEl, data);
      } catch (e) {
        resultEl.innerHTML = `<p class="error-state">${escapeHtml(e.message || "Request failed")}</p>`;
      } finally {
        submitBtn.disabled = false;
      }
    });
  });
}

// ── Renderers ────────────────────────────────────────────────────────────────

function renderClusterCard(cluster) {
  const services = (cluster.services || []).map(escapeHtml).join(", ");
  return `
    <div class="cluster-card">
      <div class="cluster-message">${escapeHtml(cluster.message)}</div>
      <div class="cluster-meta">${escapeHtml(cluster.count)} events${services ? " · " + services : ""}</div>
    </div>`;
}

function renderExplain(el, data) {
  const evidence = (data.evidence || [])
    .map((item) => `<li>${escapeHtml(item)}</li>`)
    .join("");

  let html = `
    <div class="meta-row">
      ${confidenceBadge(data.confidence)}
      <span>${escapeHtml(data.mode)} mode</span>
      <span>${escapeHtml(data.total_logs)} logs</span>
      <span>${escapeHtml((data.services_affected || []).join(", "))}</span>
      ${data.cached ? '<span class="badge badge-ok">cached</span>' : ""}
    </div>
    <p class="summary-text">${escapeHtml(data.summary)}</p>`;

  if (evidence) {
    html += `<div class="section-title">Evidence</div><ul class="evidence-list">${evidence}</ul>`;
  }

  if (data.primary_cluster) {
    html += `<div class="section-title">Primary cluster</div>${renderClusterCard(data.primary_cluster)}`;
  }

  if (data.secondary_clusters && data.secondary_clusters.length) {
    html += `<div class="section-title">Secondary clusters</div>`;
    html += data.secondary_clusters.map(renderClusterCard).join("");
  }

  el.innerHTML = html;
}

function renderTimeline(el, data) {
  const events = data.events || [];
  if (!events.length) {
    el.innerHTML = '<p class="empty-state">No events in this window.</p>';
    return;
  }
  el.innerHTML = events
    .map(
      (e) => `
    <div class="timeline-event">
      <span class="timeline-ts">${escapeHtml(fmtTime(e.timestamp))}</span>
      <span class="timeline-category">${escapeHtml(e.category)}</span>
      <span class="timeline-desc">${escapeHtml(e.description)}</span>
    </div>`
    )
    .join("");
}

function renderDiffList(title, items, cssClass) {
  if (!items || !items.length) return "";
  const rows = items
    .map((d) => {
      const services = (d.services || []).map(escapeHtml).join(", ");
      const counts = d.count_a != null && d.count_b != null
        ? `${d.count_b} → ${d.count_a}`
        : d.count_a != null
        ? `${d.count_a} events`
        : `${d.count_b} events`;
      return `<li class="${cssClass}">${escapeHtml(d.message)} <span class="cluster-meta">(${escapeHtml(counts)}${services ? " · " + services : ""})</span></li>`;
    })
    .join("");
  return `<div class="section-title">${escapeHtml(title)}</div><ul class="plain-list">${rows}</ul>`;
}

function renderCompare(el, data) {
  if (!data.has_changes) {
    el.innerHTML = '<p class="empty-state">No significant changes between windows.</p>';
    return;
  }
  let html = "";
  html += renderDiffList("New error clusters", data.new_clusters, "diff-new");
  html += renderDiffList("Errors that disappeared", data.disappeared_clusters, "diff-disappeared");
  html += renderDiffList("Errors that increased", data.increased_clusters, "diff-increased");
  html += renderDiffList("Errors that decreased", data.decreased_clusters, "diff-decreased");

  if (data.new_triggers && data.new_triggers.length) {
    html += `<div class="section-title">New triggers</div><ul class="plain-list">`;
    html += data.new_triggers
      .map((t) => `<li>${escapeHtml(t.message)} <span class="cluster-meta">(${escapeHtml(t.service)})</span></li>`)
      .join("");
    html += "</ul>";
  }

  el.innerHTML = html || '<p class="empty-state">No significant changes between windows.</p>';
}

function renderAsk(el, data) {
  const evidence = (data.evidence || [])
    .map((item) => `<li>${escapeHtml(item)}</li>`)
    .join("");

  let html = `
    <div class="meta-row"><span>${escapeHtml(data.total_matches)} matching logs</span></div>
    <p class="summary-text">${escapeHtml(data.answer)}</p>`;

  if (evidence) {
    html += `<div class="section-title">Evidence</div><ul class="evidence-list">${evidence}</ul>`;
  }

  el.innerHTML = html;
}

// ── Init ─────────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  initTabs();
  initPresets();
  initForms();
  loadIngestions();
});
