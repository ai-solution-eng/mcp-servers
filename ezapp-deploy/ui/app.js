/* ezapp-deploy read-only UI. The shell carries no data: everything is
   fetched from /api/managed with the user-supplied API key (sessionStorage
   only). All cluster data is rendered via textContent — never HTML. */

const KEY_STORAGE = "ezappDeployKey";
const REFRESH_MS = 15000;

const $ = (id) => document.getElementById(id);

function getKey() {
  return sessionStorage.getItem(KEY_STORAGE) || "";
}

function setStatus(text) {
  $("statusbar").textContent = text;
}

function badge(text) {
  const span = document.createElement("span");
  span.className = "badge";
  const normalized = (text || "").toLowerCase();
  if (normalized === "ready") span.classList.add("ok");
  else if (normalized === "error") span.classList.add("err");
  else if (normalized === "warning") span.classList.add("warn");
  else if (normalized === "initialized" || normalized === "installing") span.classList.add("info");
  else span.classList.add("unk");
  span.textContent = text || "unknown";
  return span;
}

function cell(text) {
  const td = document.createElement("td");
  td.textContent = (text === undefined || text === null || text === "") ? "—" : text;
  return td;
}

function renderApps(rows) {
  const tbody = $("apps").tBodies[0];
  tbody.textContent = "";
  if (!rows.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 7;
    td.className = "dim";
    td.textContent = "Nothing managed yet — upload_chart + apply_ezappconfig will populate this.";
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }
  for (const row of rows) {
    const tr = document.createElement("tr");

    tr.appendChild(cell(row.name));

    // Chart + version: live values when the CR still exists, ledger otherwise.
    const live = row.live;
    tr.appendChild(cell(live ? live.chart : row.ledger_chart));
    tr.appendChild(cell(live ? live.chart_version : row.ledger_chart_version));
    tr.appendChild(cell(live ? live.target_namespace : row.ledger_target_namespace));

    const statusTd = document.createElement("td");
    if (live) {
      statusTd.appendChild(badge(live.status));
    } else {
      statusTd.appendChild(badge("n/a"));
    }
    tr.appendChild(statusTd);

    tr.appendChild(cell(row.applied_at));

    const details = document.createElement("td");
    if (row.error) {
      details.textContent = row.error;
      details.className = "dim";
    } else if (live && live.failure_reason) {
      details.textContent = live.failure_reason;
      details.className = "dim";
    } else {
      details.textContent = "—";
    }
    tr.appendChild(details);

    tbody.appendChild(tr);
  }
}

function renderCharts(rows) {
  const tbody = $("charts").tBodies[0];
  tbody.textContent = "";
  if (!rows.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 4;
    td.className = "dim";
    td.textContent = "No chart versions uploaded by this server yet.";
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }
  for (const row of rows) {
    const tr = document.createElement("tr");
    tr.appendChild(cell(row.chart));
    tr.appendChild(cell(row.version));
    tr.appendChild(cell(row.uploaded_at));
    tr.appendChild(cell(row.filename));
    tbody.appendChild(tr);
  }
}

function render(data) {
  renderApps(data.apps || []);
  renderCharts(data.charts || []);
  $("meta").textContent =
    `Ledger: ${data.ledger_configmap || "?"} · snapshot: ${data.generated_at || "?"}`;
}

async function load() {
  const key = getKey();
  if (!key) {
    setStatus("Enter the MCP API key and press “Save key” — the same key your MCP client uses.");
    return;
  }
  setStatus("loading…");
  let resp;
  try {
    resp = await fetch("/api/managed", {
      headers: { "Authorization": "Bearer " + key },
    });
  } catch (err) {
    setStatus("Network error: " + err);
    return;
  }
  if (resp.status === 401) {
    setStatus("401 — wrong or missing API key. Paste the key from the ezapp-deploy-apikey Secret and save.");
    return;
  }
  if (!resp.ok) {
    let detail = "";
    try { detail = (await resp.json()).error || ""; } catch (e) { /* body not JSON */ }
    setStatus("HTTP " + resp.status + (detail ? " — " + detail : ""));
    return;
  }
  try {
    render(await resp.json());
    setStatus("Updated " + new Date().toLocaleTimeString());
  } catch (err) {
    setStatus("Bad response: " + err);
  }
}

function setAutoRefresh(enabled) {
  if (window.__ezappTimer) {
    clearInterval(window.__ezappTimer);
    window.__ezappTimer = null;
  }
  if (enabled) {
    window.__ezappTimer = setInterval(load, REFRESH_MS);
  }
}

$("savekey").addEventListener("click", () => {
  const value = $("keybox").value.trim();
  if (value) {
    sessionStorage.setItem(KEY_STORAGE, value);
  } else {
    sessionStorage.removeItem(KEY_STORAGE);
  }
  load();
});

$("keybox").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    $("savekey").click();
  }
});

$("refresh").addEventListener("click", load);

$("autorefresh").addEventListener("change", (event) => {
  setAutoRefresh(event.target.checked);
});

$("keybox").value = getKey();
load();
setAutoRefresh($("autorefresh").checked);
