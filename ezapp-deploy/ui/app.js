/* ezapp-deploy UI. The shell carries no data: everything is fetched from
   /api/managed (read-only view) and /api/store/* (catalog + install) with
   the user-supplied API key (sessionStorage only). All cluster data renders
   via textContent — never HTML. */

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

/* ── Managed apps (read-only) ─────────────────────────────────────────── */

function renderApps(rows) {
  const tbody = $("apps").tBodies[0];
  tbody.textContent = "";
  if (!rows.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 7;
    td.className = "dim";
    td.textContent = "Nothing managed yet — install something from the app store, or use upload_chart + apply_ezappconfig.";
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

/* ── App store ────────────────────────────────────────────────────────── */

let currentRepo = "";
let currentRef = "";
let installing = false;

function storeStatus(text, isError) {
  const el = $("storestatus");
  el.textContent = text;
  el.classList.toggle("errortext", Boolean(isError));
}

function nsDefault(chartName) {
  const cleaned = String(chartName || "").toLowerCase().replace(/[^a-z0-9.-]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return (cleaned || "app") + "-ns";
}

function humanizeChartName(name) {
  // Mirrors the server's label default: "arize-phoenix" -> "Arize Phoenix".
  const words = String(name || "").trim().split(/[-_.]+/).filter(Boolean);
  const out = words.map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(" ");
  return out || String(name || "");
}

function renderTiles(data) {
  currentRepo = data.repo || currentRepo;
  currentRef = data.ref || "";
  const tiles = $("tiles");
  tiles.textContent = "";
  const apps = data.apps || [];
  if (!apps.length) {
    const p = document.createElement("p");
    p.className = "dim";
    p.textContent = "No packaged Helm charts found in this repository — store layout: one directory per app at the base, each holding packaged charts (.tgz / .tar.gz).";
    tiles.appendChild(p);
  }
  for (const app of apps) {
    tiles.appendChild(appTile(app));
  }
  if ((data.skipped || []).length) {
    const p = document.createElement("p");
    p.className = "dim";
    p.textContent = "Skipped chart directories: " +
      data.skipped.map((s) => `${s.dir} (${s.reason})`).join("; ");
    tiles.appendChild(p);
  }
}

const DESC_LIMIT = 160;   // characters shown before a More/Less expander

function setTileDescription(container, text) {
  container.textContent = "";
  const full = String(text || "").trim();
  if (!full) {
    container.textContent = "No description in Chart.yaml.";
    return;
  }
  if (full.length <= DESC_LIMIT) {
    container.textContent = full;
    return;
  }
  // trim at a word boundary so the cut reads naturally
  let short = full.slice(0, DESC_LIMIT);
  const lastSpace = short.lastIndexOf(" ");
  if (lastSpace > DESC_LIMIT * 0.6) short = short.slice(0, lastSpace);
  const head = document.createElement("span");
  head.className = "desc-short";
  head.textContent = short.trimEnd() + "\u2026 ";
  container.appendChild(head);
  const rest = document.createElement("span");
  rest.className = "desc-full hidden";
  rest.textContent = full;
  container.appendChild(rest);
  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "linklike";
  toggle.textContent = "More";
  toggle.setAttribute("aria-expanded", "false");
  toggle.addEventListener("click", () => {
    // read the live DOM state: hidden = collapsed right now
    const wasExpanded = !rest.classList.contains("hidden");
    const nowExpanded = !wasExpanded;
    rest.classList.toggle("hidden", wasExpanded);   // collapse hides the trim
    head.classList.toggle("hidden", nowExpanded);   // and reveals the full text
    toggle.textContent = nowExpanded ? "Less" : "More";
    toggle.setAttribute("aria-expanded", String(nowExpanded));
  });
  container.appendChild(toggle);
}

function appTile(app) {
  const box = document.createElement("div");
  box.className = "tile";
  const versions = app.versions || [];
  const defaultVersion = versions.find((v) => v.dir === app.default_dir) || versions[0] || {};

  const iconRow = document.createElement("div");
  iconRow.className = "iconrow";
  if (app.icon) {
    const img = document.createElement("img");
    img.className = "icon";
    img.alt = "";
    img.src = app.icon;
    iconRow.appendChild(img);
  } else {
    const ph = document.createElement("div");
    ph.className = "icon-placeholder";
    ph.textContent = (app.app || "?").slice(0, 2).toUpperCase();
    iconRow.appendChild(ph);
  }
  const count = document.createElement("span");
  count.className = "tmeta";
  count.textContent = versions.length + " version" + (versions.length === 1 ? "" : "s");
  iconRow.appendChild(count);
  box.appendChild(iconRow);

  const name = document.createElement("div");
  name.className = "tname";
  name.textContent = app.label || app.app || "(unnamed app)";
  box.appendChild(name);

  const desc = document.createElement("div");
  desc.className = "tdesc";
  setTileDescription(desc, defaultVersion.description);
  box.appendChild(desc);

  if (!app.icon) {
    const noLogo = document.createElement("div");
    noLogo.className = "tmeta warnnote";
    noLogo.textContent = "No logo file found — store convention is icon.png or logo.png (png/svg) in the app or chart directory.";
    box.appendChild(noLogo);
  }

  const dir = document.createElement("div");
  dir.className = "tmeta";
  dir.textContent = (currentRepo || "") +
    (app.app === "(repo root)" ? " (repo root)" : "/" + app.app) +
    (currentRef ? " @ " + currentRef : "");
  box.appendChild(dir);

  // Version dropdown: newest first (server-side order), newest preselected.
  const dupVersions = new Set();
  const seen = {};
  for (const v of versions) {
    seen[v.version] = (seen[v.version] || 0) + 1;
  }
  for (const ver of Object.keys(seen)) {
    if (seen[ver] > 1) dupVersions.add(ver);
  }
  const select = document.createElement("select");
  select.className = "versel";
  select.setAttribute("aria-label", "Version to deploy");
  for (const v of versions) {
    const opt = document.createElement("option");
    opt.value = v.dir;
    opt.textContent = "v" + v.version +
      (dupVersions.has(v.version) ? " — " + v.dir : "");
    select.appendChild(opt);
  }
  select.value = app.default_dir;
  box.appendChild(select);

  const installBtn = document.createElement("button");
  installBtn.type = "button";
  installBtn.className = "install";
  installBtn.textContent = "Install";
  installBtn.addEventListener("click", () => {
    const selected = versions.find((v) => v.dir === select.value) || defaultVersion;
    openInstallDialog(selected);
  });
  box.appendChild(installBtn);
  return box;
}

let progressTimer = null;
let progressStarted = 0;

function stopProgress() {
  if (progressTimer) {
    clearInterval(progressTimer);
    progressTimer = null;
  }
  $("storeprogress").classList.add("hidden");
}

function startProgress() {
  progressStarted = Date.now();
  $("storeprogressbar").value = 0;
  $("storeprogress").classList.remove("hidden");
  if (progressTimer) clearInterval(progressTimer);
  progressTimer = setInterval(() => {
    const label = $("storeprogresslabel").dataset.label || "Scanning repository";
    const secs = Math.round((Date.now() - progressStarted) / 1000);
    $("storeprogresslabel").textContent = label + " — " + secs + "s elapsed";
  }, 1000);
}

function updateProgress(done, total, current) {
  const pct = total ? Math.round((done * 100) / total) : 0;
  $("storeprogressbar").value = pct;
  const short = String(current || "").split("/").pop();
  $("storeprogresslabel").dataset.label =
    "Scanning packages: " + done + "/" + total +
    (short ? " — last: " + short : "");
}

const USER_REPOS_KEY = "ezappStoreRepos";
let configuredRepos = [];   // server-configured repo list (from /api/store/repos)
let storeBootstrapped = false;

/* ── YAML syntax highlighting for the values editor ──────────────────── */

function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function highlightYamlValue(text) {
  // tokenize quoted strings, numbers and booleans/null within one chunk
  let out = "";
  let last = 0;
  const re = /('(?:[^']|'')*'|"(?:[^"\\]|\\.)*")|(-?\b\d+(?:\.\d+)?\b)|(\b(?:true|false|null|yes|no|on|off|~)\b)/g;
  let m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out += escapeHtml(text.slice(last, m.index));
    if (m[1] !== undefined) out += '<span class="tok-str">' + escapeHtml(m[1]) + '</span>';
    else if (m[2] !== undefined) out += '<span class="tok-num">' + escapeHtml(m[2]) + '</span>';
    else out += '<span class="tok-bool">' + escapeHtml(m[3]) + '</span>';
    last = m.index + m[0].length;
  }
  out += escapeHtml(text.slice(last));
  return out;
}

function highlightYamlLine(line) {
  // split a trailing comment: the first # outside quotes, preceded by
  // start-of-line or whitespace
  let quote = "";   // '"' or "'" while inside a quoted string
  let cut = -1;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (quote) {
      if (c === quote) quote = "";
      continue;
    }
    if (c === '"' || c === "'") { quote = c; continue; }
    if (c === "#" && (i === 0 || /\s/.test(line[i - 1]))) { cut = i; break; }
  }
  let code = line, comment = "";
  if (cut >= 0) { code = line.slice(0, cut); comment = line.slice(cut); }
  let out = "";
  const keyMatch = code.match(/^(\s*)(-\s+)?([\w.\-]+)(:)(\s+|$)/);
  if (keyMatch) {
    out += keyMatch[1];
    if (keyMatch[2]) out += '<span class="tok-punc">' + escapeHtml(keyMatch[2]) + '</span>';
    out += '<span class="tok-key">' + escapeHtml(keyMatch[3]) + '</span>';
    out += '<span class="tok-punc">:</span>';
    out += escapeHtml(keyMatch[5]);   // keep the space after the colon
    out += highlightYamlValue(code.slice(keyMatch[0].length));
  } else {
    out += highlightYamlValue(code);
  }
  if (comment) out += '<span class="tok-com">' + escapeHtml(comment) + '</span>';
  return out;
}

function highlightYaml(text) {
  // the trailing newline keeps the highlight layer's height in sync with
  // the textarea when the values end with an empty line
  return text.split("\n").map(highlightYamlLine).join("\n") + "\n";
}

function syncValuesHighlight() {
  const ta = $("dlg-values");
  const hl = $("dlg-values-hl");
  if (!ta || !hl) return;
  hl.innerHTML = highlightYaml(ta.value);
  autoSizeValuesEditor();   // textarea fills its content → never scrolls internally
}

function autoSizeValuesEditor() {
  const ta = $("dlg-values");
  if (!ta) return;
  ta.style.height = "auto";
  ta.style.height = ta.scrollHeight + "px";
}

function userRepos() {
  try { return JSON.parse(localStorage.getItem(USER_REPOS_KEY) || "[]"); }
  catch (e) { return []; }
}

function saveUserRepo(repo) {
  const repos = userRepos();
  if (!repos.includes(repo)) {
    repos.push(repo);
    try { localStorage.setItem(USER_REPOS_KEY, JSON.stringify(repos)); }
    catch (e) { /* storage full/blocked — the dropdown just won't remember */ }
  }
}

async function loadRepoOptions() {
  // merge server-configured repos with locally saved ones (server first;
  // the first server entry is the default)
  let serverRepos = [];
  const key = getKey();
  if (key) {
    try {
      const resp = await fetch("/api/store/repos", {
        headers: { "Authorization": "Bearer " + key },
      });
      if (resp.ok) serverRepos = (await resp.json()).repos || [];
    } catch (err) { /* offline/local-only is fine */ }
  }
  configuredRepos = serverRepos.slice();
  const all = [...new Set([...serverRepos, ...userRepos()])];
  const dropdown = $("repodropdown");
  const previous = dropdown.value;
  dropdown.textContent = "";
  for (const r of all) {
    const opt = document.createElement("option");
    opt.value = r;
    opt.textContent = r + (r === serverRepos[0] ? " (default)" : "");
    dropdown.appendChild(opt);
  }
  const custom = document.createElement("option");
  custom.value = "__custom__";
  custom.textContent = "Other repo…";
  dropdown.appendChild(custom);
  dropdown.classList.toggle("hidden", all.length === 0);
  if (previous && all.includes(previous)) {
    dropdown.value = previous;   // keep the user's selection across rebuilds
  }
  return { all, serverDefault: serverRepos[0] || null };
}

async function refreshStore() {
  // refresh the repo dropdown (server-configured + saved) and, the first
  // time the store opens, auto-load the configured default repo
  const { serverDefault } = await loadRepoOptions();
  if (!storeBootstrapped) {
    storeBootstrapped = true;
    if (serverDefault && !$("repobox").value) {
      $("repobox").value = serverDefault;
      $("repodropdown").value = serverDefault;
      loadCatalog(false);   // open the store tab already showing the default repo
    }
  }
}

async function loadCatalog(refresh) {
  const key = getKey();
  if (!key) {
    storeStatus("Enter the MCP API key and press “Save key” first.", true);
    return;
  }
  const repo = $("repobox").value.trim();
  if (!repo) {
    storeStatus("Enter a GitHub repository URL, e.g. https://github.com/&lt;owner&gt;/&lt;repo&gt;.", true);
    return;
  }
  storeStatus("Loading catalog…");
  const params = new URLSearchParams({ repo: repo, stream: "1" });
  if (refresh) params.set("refresh", "true");
  let resp;
  try {
    resp = await fetch("/api/store/catalog?" + params.toString(), {
      headers: { "Authorization": "Bearer " + key },
    });
  } catch (err) {
    storeStatus("Network error: " + err, true);
    return;
  }
  if (resp.status === 401) {
    storeStatus("401 — wrong or missing API key.", true);
    return;
  }
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    storeStatus("HTTP " + resp.status + (body.error ? " — " + body.error : ""), true);
    return;
  }
  const ctype = (resp.headers.get("content-type") || "");
  if (!ctype.includes("x-ndjson")) {
    // non-streaming server (or proxy stripped the stream): single JSON
    finishCatalog(await resp.json().catch(() => ({})), refresh);
    return;
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  startProgress();
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let newline;
      while ((newline = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, newline).trim();
        buffer = buffer.slice(newline + 1);
        if (!line) continue;
        let event;
        try { event = JSON.parse(line); } catch (e) { continue; }
        if (event.type === "progress") {
          updateProgress(event.done, event.total, event.current);
        } else if (event.type === "error") {
          stopProgress();
          storeStatus("Catalog failed: " + event.error, true);
          return;
        } else if (event.type === "catalog") {
          stopProgress();
          finishCatalog(event, refresh);
          return;
        }
      }
    }
    stopProgress();
    storeStatus("Catalog stream ended without a result — try again or use refresh.", true);
  } catch (err) {
    stopProgress();
    storeStatus("Stream error: " + err, true);
  }
}

function finishCatalog(body, refresh) {
  renderTiles(body);
  // remember manually-loaded repos in the dropdown (server-configured ones
  // are already there)
  if (currentRepo && !configuredRepos.includes(currentRepo)
      && !userRepos().includes(currentRepo)) {
    saveUserRepo(currentRepo);
    loadRepoOptions().then(() => {
      if (currentRepo && configuredRepos.concat(userRepos()).includes(currentRepo)) {
        $("repodropdown").value = currentRepo;
      }
    });
  }
  const apps = body.apps || [];
  const versionCount = apps.reduce((n, a) => n + (a.versions || []).length, 0);
  storeStatus(
    (body.cached ? "Catalog (cached" : "Catalog (fresh") +
    ", ref " + (body.ref || "?") + "): " + apps.length + " app" +
    (apps.length === 1 ? "" : "s") + ", " + versionCount + " installable chart version" +
    (versionCount === 1 ? "" : "s") + ". Pick a version, then Install." +
    (refresh ? "" : " (shift-click Load to force a re-fetch)"),
  );
}

let dlgLogoUpload = "";   // data URI of a user-uploaded logo ("" = use package logo)
let dlgChart = null;       // the version the dialog is open for
const LOGO_MAX_BYTES = 256 * 1024;

function setDialogLogo(chart) {
  const preview = $("dlg-logo-preview");
  const status = $("dlg-logo-status");
  if (chart.icon) {
    preview.src = chart.icon;
    preview.classList.remove("hidden");
    status.textContent = "Logo: " + (chart.logo_file || "from package") +
      " — choose a file to replace it.";
  } else {
    preview.classList.add("hidden");
    preview.removeAttribute("src");
    status.textContent = "Required — this package ships no logo. " +
      "Choose a png/svg image (< 256 KB).";
  }
}

function openInstallDialog(chart) {
  if (!getKey()) {
    storeStatus("Enter the MCP API key and press “Save key” first.", true);
    return;
  }
  dlgChart = chart;
  dlgLogoUpload = "";
  $("dlg-title").textContent = "Install " + (chart.name || "chart") + " " + (chart.version || "");
  $("dlg-sub").textContent =
    "Uploads the chart to ChartMuseum and applies a generated EzAppConfig " +
    "(namespace " + nsDefault(chart.name) + " by default). Every field below is editable.";
  $("dlg-namespace").value = nsDefault(chart.name);
  $("dlg-category").value = "dataScience";
  $("dlg-release-tick").checked = false;
  $("dlg-releasename").value = chart.name || "";
  $("dlg-releasename").disabled = true;
  $("dlg-label").value = humanizeChartName(chart.name);
  $("dlg-description").value = chart.name || "";
  $("dlg-values").value = chart.values || "";
  syncValuesHighlight();
  $("dlg-logo").value = "";
  setDialogLogo(chart);
  $("dlg-error").classList.add("hidden");
  $("dlg-install").disabled = false;
  $("installdialog").dataset.dir = chart.dir || "";
  $("installdialog").dataset.ref = currentRef || "";
  $("installdialog").showModal();
  syncValuesHighlight();   // once visible: match the textarea's scroll state
}

function showDlgError(text) {
  const el = $("dlg-error");
  el.textContent = text;
  el.classList.remove("hidden");
}

const INSTALL_PHASES = [
  ["github", "Downloading repo from GitHub"],
  ["chart", "Extracting packaged chart"],
  ["metadata", "Preparing install settings"],
  ["chartmuseum", "Uploading to ChartMuseum"],
  ["render", "Generating EzAppConfig"],
  ["apply", "Applying EzAppConfig (kubectl)"],
];
let installTimer = null;

function showInstallPhases() {
  // transition: values form → live phase list
  $("dlg-form").classList.add("hidden");
  $("dlg-progress").classList.remove("hidden");
  $("dlg-result").classList.add("hidden");
  $("dlg-progress-buttons").classList.add("hidden");
  const list = $("dlg-phases");
  list.textContent = "";
  for (const [stage, label] of INSTALL_PHASES) {
    const li = document.createElement("li");
    li.dataset.stage = stage;
    li.className = "phase pending";
    li.textContent = label;
    list.appendChild(li);
  }
  progressStarted = Date.now();
  if (installTimer) clearInterval(installTimer);
  installTimer = setInterval(() => {
    const secs = Math.round((Date.now() - progressStarted) / 1000);
    const active = list.querySelector(".phase.active");
    const label = active ? active.textContent : "Installing";
    $("dlg-sub").textContent = label.replace(/^▸\s*/, "") + " — " + secs + "s elapsed";
  }, 1000);
}

function markInstallStage(stage) {
  const rows = $("dlg-phases").children;
  let seen = false;
  for (const row of rows) {
    if (row.dataset.stage === stage) {
      row.className = "phase active";
      row.textContent = "▸ " + row.textContent.replace(/^[▸✓✗] /, "");
      seen = true;
    } else if (!seen) {
      row.className = "phase done";
      row.textContent = "✓ " + row.textContent.replace(/^[▸✓✗] /, "");
    }
  }
}

function finishInstall(result) {
  if (installTimer) { clearInterval(installTimer); installTimer = null; }
  const failedStage = result.status === "installed" ? null : (result.stage || null);
  const rows = $("dlg-phases").children;
  let seen = failedStage === null;
  for (const row of rows) {
    if (failedStage && row.dataset.stage === failedStage) {
      row.className = "phase failed";
      row.textContent = "✗ " + row.textContent.replace(/^[▸✓✗] /, "");
      seen = true;
    } else if (!seen) {
      row.className = "phase done";
      row.textContent = "✓ " + row.textContent.replace(/^[▸✓✗] /, "");
    }
  }
  const resultEl = $("dlg-result");
  resultEl.classList.remove("hidden");
  resultEl.classList.toggle("errortext", result.status !== "installed");
  if (result.status === "installed") {
    const reused = result.chartmuseum === "reused";
    resultEl.textContent = "Installed ✓ — chart " + (result.chart || "") + " " +
      (result.version || "") + (reused ? " (already in ChartMuseum — reused)" : "") +
      " → EzAppConfig " + (result.ezappconfig || "?") +
      " in namespace " + (result.namespace || "?") + ".";
    setStatus("Installed " + (result.chart || "chart") + " " + (result.version || "") +
      " → EzAppConfig " + (result.ezappconfig || "?") +
      " (namespace " + (result.namespace || "?") + "). Watch the rollout below.");
    setTimeout(() => {
      if ($("installdialog").open) $("installdialog").close();
      showView("apps");
      load();
    }, 1400);
  } else {
    resultEl.textContent = (result.detail || "Install failed") +
      (result.upload && failedStage !== "chartmuseum" ? " — upload: " + result.upload : "");
  }
  $("dlg-progress-buttons").classList.remove("hidden");
  installing = false;
}

function failInstall(message, stage) {
  if (installTimer) { clearInterval(installTimer); installTimer = null; }
  finishInstall({ status: "error", stage: stage || null, detail: message, upload: "" });
}

function backToSettings() {
  $("dlg-progress").classList.add("hidden");
  $("dlg-form").classList.remove("hidden");
  $("dlg-error").classList.add("hidden");
  installing = false;
  $("dlg-install").disabled = false;
}

async function confirmInstall() {
  if (installing) return;
  const key = getKey();
  const dir = $("installdialog").dataset.dir || "";
  const ref = $("installdialog").dataset.ref || "";
  const payload = {
    repo: currentRepo,
    dir: dir,
    namespace: $("dlg-namespace").value.trim(),
    category: $("dlg-category").value,
    label: $("dlg-label").value.trim(),
    description: $("dlg-description").value.trim(),
    values: $("dlg-values").value,
  };
  if (ref) payload.ref = ref;
  if ($("dlg-release-tick").checked) {
    const releaseName = $("dlg-releasename").value.trim();
    if (!releaseName) {
      showDlgError("A custom release name is ticked on — enter a name, or untick to skip.");
      return;
    }
    if (!/^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/.test(releaseName)) {
      showDlgError("Invalid release name — use lowercase letters, digits and dashes (e.g. my-release).");
      return;
    }
    payload.releaseName = releaseName;
  }
  if (dlgLogoUpload) {
    payload.logo = dlgLogoUpload;         // uploaded logo (overrides package)
  } else if (dlgChart && !dlgChart.icon) {
    showDlgError("A logo is required — this package ships none. Choose a png/svg image (< 256 KB).");
    return;
  }
  installing = true;
  $("dlg-install").disabled = true;
  $("dlg-error").classList.add("hidden");
  showInstallPhases();
  let resp;
  try {
    resp = await fetch("/api/store/install?stream=1", {
      method: "POST",
      headers: {
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
    });
  } catch (err) {
    failInstall("Network error: " + err);
    return;
  }
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    failInstall(body.error || "HTTP " + resp.status);
    return;
  }
  const ctype = (resp.headers.get("content-type") || "");
  if (!ctype.includes("x-ndjson")) {
    finishInstall(await resp.json().catch(() => ({ status: "error", detail: "bad response" })));
    return;
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let newline;
      while ((newline = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, newline).trim();
        buffer = buffer.slice(newline + 1);
        if (!line) continue;
        let event;
        try { event = JSON.parse(line); } catch (e) { continue; }
        if (event.type === "stage") {
          markInstallStage(event.stage);
        } else if (event.type === "done") {
          finishInstall(event);
          return;
        } else if (event.type === "error") {
          failInstall(event.error || "install failed");
          return;
        }
      }
    }
    failInstall("Install stream ended without a result.");
  } catch (err) {
    failInstall("Stream error: " + err);
  }
}

/* ── View switching + wiring ──────────────────────────────────────────── */

function showView(name) {
  const apps = name === "apps";
  $("view-apps").classList.toggle("hidden", !apps);
  $("view-store").classList.toggle("hidden", apps);
  $("tab-apps").classList.toggle("active", apps);
  $("tab-store").classList.toggle("active", !apps);
  $("refresh").style.display = apps ? "" : "none";
  if (apps) load();
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
  refreshStore();   // the repo dropdown needs the key — populate it now
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

$("tab-apps").addEventListener("click", () => showView("apps"));
$("tab-store").addEventListener("click", () => {
  showView("store");
  refreshStore();   // every open: fresh repo list; first open: default repo
});
$("repodropdown").addEventListener("change", (event) => {
  const value = event.target.value;
  if (value === "__custom__") {
    $("repobox").focus();
    return;
  }
  $("repobox").value = value;
  loadCatalog(false);
});
$("loadstore").addEventListener("click", (event) => {
  // plain click: cached scan; shift-click (or ctrl/meta): force a re-fetch
  loadCatalog(event.shiftKey || event.ctrlKey || event.metaKey);
});
$("repobox").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    loadCatalog(false);
  }
});
$("dlg-cancel").addEventListener("click", () => $("installdialog").close());
$("dlg-install").addEventListener("click", confirmInstall);
$("dlg-back").addEventListener("click", backToSettings);
$("dlg-close").addEventListener("click", () => $("installdialog").close());

$("dlg-release-tick").addEventListener("change", (event) => {
  $("dlg-releasename").disabled = !event.target.checked;
  if (event.target.checked) {
    $("dlg-releasename").focus();
  }
});

$("dlg-values").addEventListener("input", () => {
  syncValuesHighlight();
  autoSizeValuesEditor();
});

$("dlg-logo").addEventListener("change", () => {
  const file = $("dlg-logo").files[0];
  dlgLogoUpload = "";
  $("dlg-error").classList.add("hidden");
  if (!file) {
    setDialogLogo(dlgChart || {});
    return;
  }
  if (file.size > LOGO_MAX_BYTES) {
    setDialogLogo(dlgChart || {});
    showDlgError("Logo image is too large (" + Math.round(file.size / 1024) +
      " KB) — use an image under 256 KB.");
    $("dlg-logo").value = "";
    return;
  }
  if (file.type && file.type !== "image/png" && file.type !== "image/svg+xml") {
    setDialogLogo(dlgChart || {});
    showDlgError("Logo must be a PNG or SVG image.");
    $("dlg-logo").value = "";
    return;
  }
  const reader = new FileReader();
  reader.onload = () => {
    dlgLogoUpload = String(reader.result || "");
    const preview = $("dlg-logo-preview");
    preview.src = dlgLogoUpload;
    preview.classList.remove("hidden");
    $("dlg-logo-status").textContent = "Uploaded logo: " + file.name +
      " (" + Math.round(file.size / 1024) + " KB) — used as the app logo.";
  };
  reader.onerror = () => {
    showDlgError("Could not read the logo file: " + reader.error);
    $("dlg-logo").value = "";
    setDialogLogo(dlgChart || {});
  };
  reader.readAsDataURL(file);
});

$("keybox").value = getKey();
load();
setAutoRefresh($("autorefresh").checked);
