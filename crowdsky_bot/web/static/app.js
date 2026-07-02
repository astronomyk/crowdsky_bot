"use strict";

const $ = (id) => document.getElementById(id);
const api = async (path, opts) => {
  const r = await fetch(path, opts);
  return r.json();
};
const post = (path, body) =>
  api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });

let SUMMARY_ROWS = [];

// ---- clock ----------------------------------------------------------------
function tickClock() {
  $("clock").textContent = new Date().toLocaleString();
}
setInterval(tickClock, 1000);
tickClock();

// ---- status polling -------------------------------------------------------
function renderStatus(s) {
  const p = s.progress || {};
  const state = $("status-state");
  state.textContent = p.state || "idle";
  state.className = p.state === "running" ? "running" : "idle";
  $("status-msg").textContent = p.kind
    ? `— ${p.kind}${p.message ? ": " + p.message : ""}`
    : "";

  const per = p.per_scope || {};
  const names = {};
  (s.scopes || []).forEach((sc) => (names[sc.ip] = sc.name || sc.hostname || sc.ip));
  $("status-scopes").innerHTML = Object.keys(per).length
    ? Object.entries(per)
        .map(([ip, msg]) => `${names[ip] || ip}: ${msg}`)
        .join(" · ")
    : "";

  const lr = p.last_run;
  $("status-last").textContent = lr
    ? `Last: ${lr.kind} ${lr.status} at ${fmt(lr.finished_at)}`
    : "";

  // populate gallery scope/target dropdown from scopes list once
  window.CROWDSKY.scopes = s.scopes || [];
}

function fmt(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch (e) {
    return iso;
  }
}

async function pollStatus() {
  try {
    renderStatus(await api("/api/status"));
  } catch (e) {}
}

// ---- summary table --------------------------------------------------------
function pill(n, kind) {
  const cls = n > 0 ? kind : "zero";
  return `<span class="pill ${cls}">${n}</span>`;
}

function renderSummary(data) {
  SUMMARY_ROWS = data.rows || [];
  const body = $("summary-body");
  if (!SUMMARY_ROWS.length) {
    body.innerHTML = `<tr><td colspan="6" class="muted">No targets yet — Refresh to scan.</td></tr>`;
  } else {
    body.innerHTML = SUMMARY_ROWS.map(
      (r, i) => `<tr>
        <td><input type="checkbox" class="rowchk" data-i="${i}"></td>
        <td>${esc(r.name || r.hostname || r.scope_ip)}</td>
        <td>${esc(r.target)}</td>
        <td class="num">${pill(r.on_server, "have")}</td>
        <td class="num">${pill(r.awaiting_upload, "pending")}</td>
        <td class="num">${pill(r.awaiting_stacking, "pending")}</td>
      </tr>`
    ).join("");
  }
  $("last-refreshed").textContent = fmt(data.last_refreshed);

  // gallery target dropdown = unique (scope,target)
  const sel = $("gallery-select");
  const cur = sel.value;
  sel.innerHTML =
    `<option value="">—</option>` +
    SUMMARY_ROWS.map(
      (r) =>
        `<option value="${esc(r.scope_ip)}||${esc(r.target)}">${esc(
          r.name || r.scope_ip
        )} / ${esc(r.target)}</option>`
    ).join("");
  sel.value = cur;
}

async function pollSummary() {
  try {
    renderSummary(await api("/api/summary"));
  } catch (e) {}
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])
  );
}

// ---- selection -> {scopes, targets} --------------------------------------
function currentSelection() {
  const checked = [...document.querySelectorAll(".rowchk:checked")].map(
    (c) => SUMMARY_ROWS[+c.dataset.i]
  );
  if (!checked.length) return { scopes: "all", targets: "all" };
  return {
    scopes: [...new Set(checked.map((r) => r.scope_ip))],
    targets: [...new Set(checked.map((r) => r.target))],
  };
}

// ---- action buttons -------------------------------------------------------
async function trigger(path, extra) {
  const sel = currentSelection();
  await post(path, Object.assign(sel, extra || {}));
  pollStatus();
}

$("btn-refresh").onclick = () => post("/api/refresh").then(pollStatus);
$("btn-stack").onclick = () => trigger("/api/stack");
$("btn-upload").onclick = () => trigger("/api/upload");
$("btn-purge").onclick = () => {
  if (
    confirm(
      "Delete CrowdSky_* stacks from the selected Seestar folders? This cannot be undone."
    )
  ) {
    trigger("/api/purge", { confirm: true });
  }
};

// ---- config save ----------------------------------------------------------
$("save-config").onclick = async () => {
  const body = {
    scopes: { count: +$("cfg-scopes-count").value || 0 },
    crowdsky: {
      username: $("cfg-username").value,
      password: $("cfg-password").value,
    },
    location: {
      source: $("cfg-loc-source").value,
      lat: parseFloat($("cfg-lat").value) || 0,
      lon: parseFloat($("cfg-lon").value) || 0,
      timezone: $("cfg-tz").value,
    },
    schedule: {
      auto_stack: $("cfg-auto-stack").checked,
      auto_upload: $("cfg-auto-upload").checked,
      trigger: $("cfg-trigger").value,
      offset_minutes: +$("cfg-offset").value || 0,
      fixed_time: $("cfg-fixed").value,
    },
  };
  const st = $("save-status");
  st.textContent = "Saving…";
  st.className = "muted";
  try {
    await post("/api/config", body);
    st.textContent = "Saved ✓ (re-discovering scopes if changed)";
    st.className = "save-ok";
  } catch (e) {
    st.textContent = "Save failed";
    st.className = "save-err";
  }
};

// ---- gallery --------------------------------------------------------------
$("gallery-select").onchange = async (e) => {
  const g = $("gallery");
  g.innerHTML = "";
  const v = e.target.value;
  if (!v) return;
  const [scope, target] = v.split("||");
  g.innerHTML = `<p class="muted">Loading…</p>`;
  const data = await api(
    `/api/gallery?scope=${encodeURIComponent(scope)}&target=${encodeURIComponent(
      target
    )}`
  );
  if (!data.images || !data.images.length) {
    g.innerHTML = `<p class="muted">No thumbnails found.</p>`;
    return;
  }
  g.innerHTML = data.images
    .map((im) => `<img loading="lazy" src="${im.url}" alt="${esc(im.name)}">`)
    .join("");
};

// ---- boot -----------------------------------------------------------------
pollStatus();
pollSummary();
setInterval(pollStatus, 3000);
setInterval(pollSummary, 10000);
