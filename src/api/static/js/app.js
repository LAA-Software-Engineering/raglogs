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

// datetime-local yields "YYYY-MM-DDTHH:MM" (seconds optional, no timezone).
// The API treats naive timestamps as UTC, so we emit explicit UTC ISO-8601.
const DATETIME_WINDOW_KEYS = {
  from_time: true,
  to_time: true,
  window_a_from: true,
  window_a_to: true,
  window_b_from: true,
  window_b_to: true,
};

function datetimeLocalToIso(value) {
  const trimmed = String(value == null ? "" : value).trim();
  if (!trimmed) return "";
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(trimmed)) {
    return trimmed + ":00Z";
  }
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/.test(trimmed)) {
    return trimmed + "Z";
  }
  return trimmed;
}

function confidenceBadge(confidence) {
  const label = confidence && typeof confidence === "object" ? confidence.label : confidence;
  const cls = label === "high" ? "badge-high" : label === "medium" || label === "medium-high" ? "badge-medium" : "badge-low";
  return `<span class="badge ${cls}">${escapeHtml(label || "")}</span>`;
}

// FastAPI error bodies are either {"detail": "message"} (HTTPException) or
// {"detail": [{"loc": [...], "msg": "...", ...}, ...]} (pydantic 422s).
function formatErrorDetail(detail, fallback) {
  if (!detail) return fallback;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((d) => (d && d.msg) || JSON.stringify(d)).join("; ");
  }
  return JSON.stringify(detail);
}

// ── Ingestion picker ─────────────────────────────────────────────────────────

async function loadIngestions() {
  const select = document.getElementById("ingestion-select");
  try {
    const resp = await fetch("/v1/ingestions");
    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(formatErrorDetail(data.detail, "Failed to load ingestions"));
    }
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

// ── Window mode (relative duration vs absolute from/to) ──────────────────────

function applyWindowMode(form, mode) {
  const next = mode === "absolute" ? "absolute" : "relative";
  form.dataset.windowMode = next;

  form.querySelectorAll(".mode-btn").forEach((btn) => {
    const active = btn.dataset.mode === next;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", active ? "true" : "false");
  });

  const relative = form.querySelector(".window-relative");
  const absolute = form.querySelector(".window-absolute");
  if (relative) {
    relative.hidden = next !== "relative";
    relative.querySelectorAll("input, button").forEach((el) => {
      el.disabled = next !== "relative";
    });
  }
  if (absolute) {
    absolute.hidden = next !== "absolute";
    absolute.querySelectorAll("input").forEach((el) => {
      el.disabled = next !== "absolute";
    });
  }
}

function initWindowModes() {
  document.querySelectorAll(".window-form").forEach((form) => {
    applyWindowMode(form, form.dataset.windowMode || "relative");
    form.querySelectorAll(".mode-btn").forEach((btn) => {
      btn.addEventListener("click", () => applyWindowMode(form, btn.dataset.mode));
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
        if (sinceInput) sinceInput.value = btn.dataset.since;
      });
    });
  });
}

// ── Form submission ──────────────────────────────────────────────────────────

function formToBody(form) {
  const body = {};
  new FormData(form).forEach((value, key) => {
    const trimmed = String(value).trim();
    if (trimmed === "") return;
    if (DATETIME_WINDOW_KEYS[key]) {
      const iso = datetimeLocalToIso(trimmed);
      if (iso) body[key] = iso;
      return;
    }
    body[key] = trimmed;
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
        if ((form.dataset.windowMode || "relative") === "absolute") {
          const missing = Array.from(form.querySelectorAll('input[type="datetime-local"]:not([disabled])')).some(
            (input) => !String(input.value || "").trim()
          );
          if (missing) {
            resultEl.innerHTML = '<p class="error-state">Pick from and to datetimes for the absolute window.</p>';
            return;
          }
        }

        const resp = await fetch(endpoint, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(formToBody(form)),
        });
        const data = await resp.json();
        if (!resp.ok) {
          resultEl.innerHTML = `<p class="error-state">${escapeHtml(formatErrorDetail(data.detail, "Request failed"))}</p>`;
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
  const message = cluster.template || cluster.message || "";
  return `
    <div class="cluster-card">
      <div class="cluster-message">${escapeHtml(message)}</div>
      <div class="cluster-meta">${escapeHtml(cluster.count)} events${services ? " · " + services : ""}</div>
    </div>`;
}

function evidenceDetail(item) {
  if (item && typeof item === "object") {
    return item.detail || "";
  }
  return item;
}

function triggerCandidatesFrom(data) {
  const raw = data && data.trigger_candidates;
  if (Array.isArray(raw) && raw.length) {
    return raw;
  }
  const trigger = data && data.trigger;
  if (trigger && typeof trigger === "object" && trigger.detected) {
    return [trigger];
  }
  return [];
}

function renderTriggerCandidate(trigger) {
  const message = trigger.message || trigger.detail || trigger.type || "";
  const ts = fmtTime(trigger.timestamp || trigger.at);
  const extras = [];
  if (trigger.service) extras.push(escapeHtml(trigger.service));
  if (ts) extras.push(escapeHtml(ts));
  if (trigger.type && trigger.message) extras.push(escapeHtml(trigger.type));
  if (trigger.correlation) extras.push(escapeHtml(trigger.correlation));
  return `
    <div class="cluster-card">
      <div class="cluster-message">${escapeHtml(message)}</div>
      ${extras.length ? `<div class="cluster-meta">${extras.join(" · ")}</div>` : ""}
    </div>`;
}

function renderTriggerSection(data) {
  const candidates = triggerCandidatesFrom(data);
  if (!candidates.length) return "";
  return `<div class="section-title">Likely trigger</div>${candidates.map(renderTriggerCandidate).join("")}`;
}

function renderExplain(el, data) {
  const evidence = (data.evidence || [])
    .map((item) => `<li>${escapeHtml(evidenceDetail(item))}</li>`)
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

  html += renderTriggerSection(data);

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

function timelineEventMeta(event) {
  const parts = [];
  if (event.label && event.label !== event.category) {
    parts.push(escapeHtml(event.label));
  }
  if (event.count != null && event.count !== "") {
    const n = Number(event.count);
    parts.push(`${n} event${n === 1 ? "" : "s"}`);
  }
  const services = (event.services || []).filter(Boolean).map(escapeHtml);
  if (services.length) {
    parts.push(services.join(", "));
  }
  if (event.duration_minutes != null && event.duration_minutes !== "") {
    parts.push(`${escapeHtml(event.duration_minutes)} min span`);
  }
  return parts.join(" · ");
}

function renderTimelineEvent(event) {
  const meta = timelineEventMeta(event);
  return `
    <div class="timeline-event">
      <span class="timeline-ts">${escapeHtml(fmtTime(event.timestamp))}</span>
      <span class="timeline-category">${escapeHtml(event.category)}</span>
      <div class="timeline-body">
        <span class="timeline-desc">${escapeHtml(event.description)}</span>
        ${meta ? `<div class="timeline-meta">${meta}</div>` : ""}
      </div>
    </div>`;
}

function renderTimeline(el, data) {
  const events = data.events || [];
  if (!events.length) {
    el.innerHTML = '<p class="empty-state">No events in this window.</p>';
    return;
  }
  el.innerHTML = events.map(renderTimelineEvent).join("");
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

function windowStart(w) {
  if (!w || typeof w !== "object") return "";
  return w.from || w.from_ || w.start || "";
}

function windowEnd(w) {
  if (!w || typeof w !== "object") return "";
  return w.to || w.end || "";
}

function formatWindowBounds(w) {
  const start = windowStart(w);
  const end = windowEnd(w);
  if (!start && !end) return "";
  if (start && end) return `${fmtTime(start)} → ${fmtTime(end)}`;
  return fmtTime(start || end);
}

function renderCompareWindows(data) {
  const a = formatWindowBounds(data.window_a);
  const b = formatWindowBounds(data.window_b);
  if (!a && !b) return "";
  let html = `<div class="meta-row window-bounds">`;
  if (a) html += `<span>Window A (now): ${escapeHtml(a)}</span>`;
  if (b) html += `<span>Window B (baseline): ${escapeHtml(b)}</span>`;
  html += `</div>`;
  return html;
}

function renderTriggerDiffList(title, items, cssClass) {
  if (!items || !items.length) return "";
  const rows = items
    .map((t) => {
      const service = t.service
        ? ` <span class="cluster-meta">(${escapeHtml(t.service)})</span>`
        : "";
      return `<li class="${cssClass}">${escapeHtml(t.message)}${service}</li>`;
    })
    .join("");
  return `<div class="section-title">${escapeHtml(title)}</div><ul class="plain-list">${rows}</ul>`;
}

function renderCompare(el, data) {
  let html = renderCompareWindows(data);

  if (!data.has_changes) {
    html += '<p class="empty-state">No significant changes between windows.</p>';
    html += renderTriggerDiffList("Dropped triggers", data.dropped_triggers, "diff-dropped");
    el.innerHTML = html;
    return;
  }

  html += renderDiffList("New error clusters", data.new_clusters, "diff-new");
  html += renderDiffList("Errors that disappeared", data.disappeared_clusters, "diff-disappeared");
  html += renderDiffList("Errors that increased", data.increased_clusters, "diff-increased");
  html += renderDiffList("Errors that decreased", data.decreased_clusters, "diff-decreased");
  html += renderTriggerDiffList("New triggers", data.new_triggers, "diff-new");
  html += renderTriggerDiffList("Dropped triggers", data.dropped_triggers, "diff-dropped");

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
  initWindowModes();
  initPresets();
  initForms();
  loadIngestions();
});
