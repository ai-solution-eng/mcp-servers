"use strict";
/*
 * HPE Kubernetes Ops Console — vanilla-JS MCP client.
 *
 * Talks to the same /mcp endpoint as every MCP client (same API key, same
 * namespace policy, same exec gates). Screens are presets over a universal,
 * schema-driven tool runner: anything registered by the server appears under
 * "Any tool" with a form generated from its inputSchema.
 *
 * Security notes:
 *  - the API key lives in memory (+ optionally sessionStorage for the tab);
 *    never localStorage;
 *  - every value from the cluster is rendered with textContent — no HTML
 *    interpolation of server data anywhere in this file;
 *  - the X-Exec-Namespaces header is a NARROWING header (server-side it can
 *    never widen a caller's scope).
 */

const PROTOCOL_VERSION = "2026-07-28";
const ENDPOINT = "/mcp";
const CONSOLE_VERSION = "0.2.0";
const META_PROTOCOL = "io.modelcontextprotocol/protocolVersion";
const META_CAPS = "io.modelcontextprotocol/clientCapabilities";

// Mirror of the server's read-only exec allowlist (server remains the boundary).
const EXEC_BINARIES = [
  "ps", "ls", "cat", "tail", "head", "grep", "df", "du", "free", "uptime",
  "hostname", "id", "whoami", "uname", "date", "stat", "ss", "netstat",
  "ip", "wc", "sleep",
];
const READ_VERBS = ["get", "describe", "logs", "top", "explain", "api-resources",
  "api-versions", "cluster-info", "version", "auth", "events"];

const state = {
  key: "",
  execHeader: "",
  tools: new Map(),        // name -> {description, inputSchema}
  namespaces: [],
  apiResources: [],
  current: null,           // active screen id
  lastText: "",            // unmodified tool output (Raw toggle)
  seq: 0,
};

const $ = (id) => document.getElementById(id);

/* ── MCP transport ────────────────────────────────────────────────────── */

function headersFor(modern, method, name) {
  const h = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "Authorization": "Bearer " + state.key,
  };
  if (state.execHeader) h["X-Exec-Namespaces"] = state.execHeader;
  if (modern && method) {
    h["Mcp-Protocol-Version"] = PROTOCOL_VERSION;
    h["Mcp-Method"] = method;
    if (name) h["Mcp-Name"] = name;
  }
  return h;
}

function withMeta(params) {
  const p = Object.assign({}, params || {});
  p._meta = {
    [META_PROTOCOL]: PROTOCOL_VERSION,
    [META_CAPS]: { tools: {}, clientInfo: { name: "hpe-k8s-ops-console", version: CONSOLE_VERSION } },
  };
  return p;
}

async function readRpc(res, id) {
  const text = await res.text();
  const ctype = res.headers.get("content-type") || "";
  if (ctype.includes("text/event-stream")) {
    for (const line of text.split("\n")) {
      if (!line.startsWith("data:")) continue;
      try {
        const msg = JSON.parse(line.slice(5).trim());
        if (msg && (msg.id === id || msg.error)) return msg;
      } catch { /* keep scanning */ }
    }
    throw new Error("no JSON-RPC response found in the event stream");
  }
  try { return JSON.parse(text); }
  catch { throw new Error(`unexpected non-JSON response (HTTP ${res.status}): ${text.slice(0, 200)}`); }
}

async function rpc(method, params, name) {
  for (const modern of [true, false]) {          // modern envelope, then legacy retry
    const body = { jsonrpc: "2.0", id: ++state.seq, method, params: modern ? withMeta(params) : (params || {}) };
    let res;
    try {
      res = await fetch(ENDPOINT, { method: "POST", headers: headersFor(modern, method, name), body: JSON.stringify(body) });
    } catch (e) {
      throw new Error(`network error reaching ${ENDPOINT}: ${e.message}`);
    }
    if (res.status === 401) { logout(false); throw new Error("Unauthorized (401) — the API key was rejected."); }
    if (modern && !res.ok && res.status !== 500) continue;   // envelope rejected → legacy path
    const msg = await readRpc(res, body.id);
    if (msg.error) throw new Error(msg.error.message || JSON.stringify(msg.error));
    return msg.result;
  }
  throw new Error("request failed");
}

async function callTool(name, args) {
  const result = await rpc("tools/call", { name, arguments: args || {} }, name);
  const text = (result && result.content ? result.content : [])
    .filter((p) => p.type === "text")
    .map((p) => p.text)
    .join("\n");
  if (result && result.isError) throw new Error(text || `tool ${name} failed`);
  return text;
}

/* ── Shared data (namespaces, resource types) ─────────────────────────── */

function parseNamespaces(text) {
  const out = [];
  for (const line of (text || "").split("\n")) {
    const t = line.trim();
    if (!t || t.startsWith("NAMESPACES")) continue;
    const name = t.split(/\s+/)[0];
    if (name && !out.includes(name)) out.push(name);
  }
  return out;
}

function parseApiResources(text) {
  const out = [];
  for (const line of (text || "").split("\n")) {
    const t = line.trim();
    if (!t || t.startsWith("NAME ") || t.startsWith("NAME\t")) continue;
    const name = t.split(/\s+/)[0];               // first column: resource name
    if (name && !out.includes(name)) out.push(name);
  }
  return out;
}

async function refreshNamespaces(silent) {
  try {
    state.namespaces = parseNamespaces(await callTool("list_namespaces", {}));
  } catch (e) {
    if (!silent) toast("Could not list namespaces: " + e.message, true);
  }
}

async function refreshApiResources(silent) {
  try {
    state.apiResources = parseApiResources(await callTool("list_api_resources", {}));
  } catch (e) {
    if (!silent) toast("Could not list api-resources: " + e.message, true);
  }
}

/* ── Schema-driven form builder ───────────────────────────────────────── */

function inferWidget(propName, prop) {
  if (propName === "namespace") return "namespace";
  if (propName === "resource_type") return "resourcetype";
  if (propName === "command" && prop.type === "array") return "command";
  if (propName === "verb") return "verb";
  if (prop.type === "boolean") return "bool";
  if (prop.type === "integer" || prop.type === "number") return "number";
  if (prop.type === "array") return "lines";
  if (Array.isArray(prop.enum)) return "select";
  return "text";
}

function widgetFor(name, prop, override) {
  const w = override || inferWidget(name, prop);
  const wrap = document.createElement("div");
  wrap.className = "field";
  const label = document.createElement("label");
  const title = document.createElement("span");
  title.textContent = name;
  label.appendChild(title);
  if (w.required) {
    const star = document.createElement("span");
    star.className = "req";
    star.textContent = " *";
    label.appendChild(star);
  }
  if (prop.description) {
    const hint = document.createElement("span");
    hint.className = "hint";
    hint.textContent = ` — ${prop.description}`;
    label.appendChild(hint);
  }
  wrap.appendChild(label);

  let input;
  if (w.kind === "namespace") {
    input = document.createElement("input");
    input.type = "text";
    input.setAttribute("list", "ns-options");
    input.placeholder = "(empty = all namespaces)";
  } else if (w.kind === "resourcetype") {
    input = document.createElement("input");
    input.type = "text";
    input.setAttribute("list", "rt-options");
    input.placeholder = "e.g. pods, deployments, inferenceservices";
  } else if (w.kind === "verb") {
    input = document.createElement("select");
    for (const v of READ_VERBS) {
      const o = document.createElement("option");
      o.value = o.textContent = v;
      input.appendChild(o);
    }
    input.value = String(prop.default || "get");
  } else if (w.kind === "select") {
    input = document.createElement("select");
    for (const v of prop.enum) {
      const o = document.createElement("option");
      o.value = String(v); o.textContent = String(v);
      input.appendChild(o);
    }
    if (prop.default !== undefined) input.value = String(prop.default);
  } else if (w.kind === "bool") {
    input = document.createElement("input");
    input.type = "checkbox";
    if (prop.default === true) input.checked = true;
  } else if (w.kind === "number") {
    input = document.createElement("input");
    input.type = "number";
    if (prop.default !== undefined) input.value = String(prop.default);
  } else if (w.kind === "lines") {
    input = document.createElement("textarea");
    input.placeholder = "one value per line";
  } else if (w.kind === "command") {
    // argv builder: allowlisted binary + argument tokens → list[str]
    input = document.createElement("div");
    input.className = "field-row";
    const bin = document.createElement("select");
    for (const b of EXEC_BINARIES) {
      const o = document.createElement("option");
      o.value = o.textContent = b;
      bin.appendChild(o);
    }
    const args = document.createElement("input");
    args.type = "text";
    args.placeholder = "arguments, e.g. -n 50 /var/log/app.log";
    args.style.flex = "2";
    input.appendChild(bin);
    input.appendChild(args);
    input.dataset.bin = "";
    bin.addEventListener("change", () => { input.dataset.bin = bin.value; });
  } else {
    input = document.createElement("input");
    input.type = "text";
    if (prop.default !== undefined) input.value = String(prop.default);
  }
  input.dataset.param = name;
  input.dataset.kind = w.kind;
  wrap.appendChild(input);
  return wrap;
}

function buildForm(toolName, overrides) {
  const def = state.tools.get(toolName);
  if (!def) return null;
  const schema = def.inputSchema || {};
  const props = schema.properties || {};
  const required = new Set(schema.required || []);
  const form = $("form");
  form.textContent = "";
  const fields = [];
  for (const [name, prop] of Object.entries(props)) {
    const w = { kind: overrides && overrides[name] ? overrides[name] : inferWidget(name, prop), required: required.has(name) };
    const el = widgetFor(name, prop, w);
    form.appendChild(el);
    fields.push(el);
  }
  // datalists for combobox widgets (namespace / resource types)
  for (const [id, values] of [["ns-options", state.namespaces], ["rt-options", state.apiResources]]) {
    let dl = $(id);
    if (dl) dl.remove();
    dl = document.createElement("datalist");
    dl.id = id;
    for (const v of values) {
      const o = document.createElement("option");
      o.value = v;
      dl.appendChild(o);
    }
    form.appendChild(dl);
  }
  const actions = document.createElement("div");
  actions.className = "form-actions";
  const run = document.createElement("button");
  run.type = "submit";
  run.className = "btn primary";
  run.textContent = "Run";
  actions.appendChild(run);
  form.appendChild(actions);
  return fields;
}

function splitArgv(raw) {
  // whitespace split with quote awareness — mirrors shell intent without a shell
  const out = [];
  let cur = "", quote = null;
  for (const ch of raw) {
    if (quote) {
      if (ch === quote) quote = null; else cur += ch;
    } else if (ch === '"' || ch === "'") quote = ch;
    else if (/\s/.test(ch)) { if (cur) { out.push(cur); cur = ""; } }
    else cur += ch;
  }
  if (cur) out.push(cur);
  return out;
}

function readForm() {
  const args = {};
  for (const field of $("form").querySelectorAll("[data-param]")) {
    const name = field.dataset.param, kind = field.dataset.kind;
    if (name.startsWith("__")) continue;          // internal widgets (tool picker)
    if (kind === "command") {
      const bin = field.querySelector("select").value;
      const rest = splitArgv(field.querySelector("input").value.trim());
      args[name] = [bin, ...rest];
    } else if (kind === "bool") {
      args[name] = field.querySelector("input").checked;
    } else if (kind === "lines") {
      const lines = field.querySelector("textarea").value.split("\n").map((s) => s.trim()).filter(Boolean);
      if (lines.length) args[name] = lines;
    } else {
      const el = field.querySelector("input, select");
      const v = (el.value || "").trim();
      if (v !== "") args[name] = kind === "number" ? Number(v) : v;
    }
  }
  return args;
}

/* ── Output rendering (textContent only) ──────────────────────────────── */

function prettyJson(text) {
  try {
    const j = JSON.parse(text);
    if (j && typeof j === "object") return JSON.stringify(j, null, 2);
  } catch { /* not JSON */ }
  return null;
}

function showOutput(text) {
  state.lastText = text;
  const pretty = prettyJson(text);
  $("btn-raw").textContent = pretty ? "Raw" : "Formatted";
  $("output").textContent = pretty || text;
  $("out-meta").textContent = `${text.length.toLocaleString()} chars`;
  $("output-wrap").classList.remove("hidden");
}

/* ── Pod table + drill-down ───────────────────────────────────────────── */

function parsePods(text) {
  const rows = [];
  for (const line of (text || "").split("\n")) {
    const m = line.trim().match(/^(\S+)\/(\S+):\s+(\S+)\s+\|\s+Restarts:\s+(\d+)\s+\|\s+Age:\s+(\S+)/);
    if (m) rows.push({ namespace: m[1], name: m[2], phase: m[3], restarts: +m[4], age: m[5] });
  }
  return rows;
}

function showPodTable(rows) {
  const wrap = $("table-wrap");
  wrap.textContent = "";
  if (!rows.length) { wrap.classList.add("hidden"); return; }
  const table = document.createElement("table");
  table.className = "data";
  const thead = document.createElement("thead");
  const hr = document.createElement("tr");
  for (const h of ["Namespace", "Pod", "Phase", "Restarts", "Age", ""]) {
    const th = document.createElement("th");
    th.textContent = h;
    hr.appendChild(th);
  }
  thead.appendChild(hr);
  table.appendChild(thead);
  const tbody = document.createElement("tbody");
  for (const r of rows) {
    const tr = document.createElement("tr");
    tr.className = "click";
    tr.title = "Click for logs";
    for (const v of [r.namespace, r.name]) {
      const td = document.createElement("td");
      td.textContent = v;
      tr.appendChild(td);
    }
    const tdPhase = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = "badge " + r.phase;
    badge.textContent = r.phase;
    tdPhase.appendChild(badge);
    tr.appendChild(tdPhase);
    for (const v of [String(r.restarts), r.age]) {
      const td = document.createElement("td");
      td.textContent = v;
      tr.appendChild(td);
    }
    const tdAct = document.createElement("td");
    if (state.tools.has("exec_in_pod")) {
      const b = document.createElement("button");
      b.className = "btn ghost small";
      b.type = "button";
      b.textContent = "Exec";
      b.addEventListener("click", (ev) => {
        ev.stopPropagation();
        navigate("exec", { namespace: r.namespace, pod_name: r.name });
      });
      tdAct.appendChild(b);
    }
    tr.appendChild(tdAct);
    tr.addEventListener("click", () => navigate("logs", { namespace: r.namespace, pod_name: r.name }, true));
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
  wrap.classList.remove("hidden");
}

/* ── Screens (presets over the universal runner) ──────────────────────── */

const SCREENS = [
  {
    group: "Overview",
    items: [
      { id: "health", title: "Cluster health", desc: "Nodes, pod summary and resource usage.", tool: "cluster_health", auto: true },
      { id: "namespaces", title: "Namespaces", desc: "List namespaces with status and age.", tool: "list_namespaces", auto: true },
    ],
  },
  {
    group: "Workloads",
    items: [
      { id: "pods", title: "Pods", desc: "List pods; click a row for logs, Exec for the shell-free debug tool.", tool: "list_pods", table: "pods" },
      { id: "logs", title: "Pod logs", desc: "Fetch container logs (tail, previous crashed container).", tool: "get_pod_logs" },
      { id: "workloads", title: "Deployments & jobs", desc: "Deployments, StatefulSets, DaemonSets and Jobs readiness.", tool: "list_workloads" },
      { id: "events", title: "Events", desc: "Cluster events, filterable by namespace, object and type.", tool: "get_events" },
    ],
  },
  {
    group: "Networking",
    items: [
      { id: "services", title: "Services", desc: "ClusterIP/NodePort/LoadBalancer services and ports.", tool: "list_services" },
      { id: "virtualservices", title: "VirtualServices (Istio)", desc: "Hosts, gateways and route targets; name + namespace for the full definition.", tool: "list_virtual_services" },
    ],
  },
  {
    group: "Config & Storage",
    items: [
      { id: "configmaps", title: "ConfigMaps", desc: "Read a ConfigMap's data by name and namespace.", tool: "get_configmap" },
      { id: "pvcs", title: "PVCs", desc: "PersistentVolumeClaims: phase, capacity, storage class.", tool: "list_pvcs" },
      { id: "secrets", title: "Secrets (names only)", desc: "Names and key lists only — values are blocked by RBAC by design.", tool: "list_secrets" },
    ],
  },
  {
    group: "Custom resources",
    items: [
      { id: "crds", title: "CRDs", desc: "Installed Custom Resource Definitions (group + scope).", tool: "list_crds", auto: true },
      { id: "custom", title: "Custom resource lookup", desc: "Query any CRD: group, version, plural, optional name/namespace.", tool: "get_custom_resource" },
    ],
  },
  {
    group: "Security",
    items: [
      { id: "rbac", title: "RBAC check (can-I)", desc: "kubectl auth can-i for a verb/resource — answers come from the API server.", tool: "check_rbac" },
    ],
  },
  {
    group: "Debug",
    items: [
      { id: "exec", title: "Exec in pod (opt-in)", desc: "One allowlisted read-only command inside a labeled container. Server enforces label, allowlist and policy.", tool: "exec_in_pod", exec: true },
    ],
  },
  {
    group: "Advanced",
    items: [
      { id: "get-resource", title: "Any resource (get)", desc: "kubectl get against any type, incl. CRDs; output yaml/json/jsonpath.", tool: "get_resource", widgets: { resource_type: "resourcetype" } },
      { id: "describe", title: "Describe resource", desc: "kubectl describe — events, conditions, full status.", tool: "describe_resource", widgets: { resource_type: "resourcetype" } },
      { id: "kubectl", title: "kubectl console (read-only)", desc: "The escape hatch: read verbs only (get, describe, logs, top, events, auth, …). Write verbs and connection flags are rejected server-side.", tool: "run_kubectl", widgets: { command: "text" } },
      { id: "anytool", title: "Any tool", desc: "Every tool the server registers, with a form generated from its inputSchema.", tool: "*", anytool: true },
    ],
  },
];

function allScreens() {
  return SCREENS.flatMap((g) => g.items).filter((it) => it.tool === "*" || state.tools.has(it.tool));
}

function renderNav(activeId) {
  const nav = $("nav-groups");
  nav.textContent = "";
  for (const group of SCREENS) {
    const items = group.items.filter((it) => it.tool === "*" || state.tools.has(it.tool));
    if (!items.length) continue;
    const div = document.createElement("div");
    div.className = "nav-group";
    const h = document.createElement("h3");
    h.textContent = group.group;
    div.appendChild(h);
    for (const item of items) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "nav-item" + (item.id === activeId ? " active" : "");
      b.textContent = item.title;
      b.addEventListener("click", () => navigate(item.id));
      div.appendChild(b);
    }
    nav.appendChild(div);
  }
}

function prefillForm(prefill) {
  if (!prefill) return;
  for (const [k, v] of Object.entries(prefill)) {
    const field = $("form").querySelector(`[data-param="${CSS.escape(k)}"]`);
    if (!field) continue;
    const kind = field.dataset.kind;
    if (kind === "command") {
      const parts = Array.isArray(v) ? v.slice() : String(v).split(/\s+/);
      field.querySelector("select").value = parts[0] || field.querySelector("select").value;
      field.querySelector("input").value = parts.slice(1).join(" ");
    } else if (kind === "bool") {
      field.querySelector("input").checked = !!v;
    } else {
      const el = field.querySelector("input, select, textarea");
      if (el) el.value = String(v);
    }
  }
}

async function navigate(screenId, prefill, autoRun) {
  const screen = allScreens().find((s) => s.id === screenId) || allScreens()[0];
  if (!screen) return;
  state.current = screen.id;
  renderNav(screen.id);
  $("screen-title").textContent = screen.title;
  $("screen-desc").textContent = screen.desc || "";
  $("table-wrap").classList.add("hidden");
  $("output-wrap").classList.add("hidden");
  $("output").textContent = "";
  $("form").classList.remove("hidden");

  if (screen.anytool) { renderAnyTool(prefill); return; }
  buildForm(screen.tool, screen.widgets);
  prefillForm(prefill);

  if (screen.exec) prepareExecScreen(prefill);

  if (autoRun || screen.auto) runCurrent();
}

async function runCurrent() {
  const screen = allScreens().find((s) => s.id === state.current);
  if (!screen) return;
  const btn = $("form").querySelector('button[type="submit"]');
  btn.disabled = true;
  try {
    const args = readForm();
    const text = await callTool(screen.tool === "*" ? state.anyTool : screen.tool, args);
    showOutput(text);
    if (screen.table === "pods") showPodTable(parsePods(text));
  } catch (e) {
    showOutput("Error: " + e.message);
    if (/not registered|unknown tool/i.test(e.message)) toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
}

/* ── "Any tool" universal runner ──────────────────────────────────────── */

async function renderAnyTool(prefill) {
  const form = $("form");
  form.textContent = "";
  const row = document.createElement("div");
  row.className = "field";
  const label = document.createElement("label");
  label.textContent = "tool";
  row.appendChild(label);
  const sel = document.createElement("select");
  for (const name of [...state.tools.keys()].sort()) {
    const o = document.createElement("option");
    o.value = o.textContent = name;
    sel.appendChild(o);
  }
  sel.dataset.param = "__tool__";
  row.appendChild(sel);
  const desc = document.createElement("p");
  desc.className = "muted";
  desc.style.margin = "4px 0 12px";
  form.appendChild(row);
  form.appendChild(desc);
  const rebuild = () => {
    const def = state.tools.get(sel.value);
    desc.textContent = def ? def.description || "" : "";
    buildForm(sel.value, null);
    // buildForm cleared the form — put the picker back on top
    form.prepend(row);
    row.after(desc);
    state.anyTool = sel.value;
    prefillForm(prefill);
  };
  sel.addEventListener("change", rebuild);
  rebuild();
}

/* ── Exec screen helpers ──────────────────────────────────────────────── */

async function prepareExecScreen(prefill) {
  const note = document.createElement("p");
  note.className = "muted";
  note.textContent = "Requires: K8S_MCP_EXEC_ENABLED=true on the server, the pod labeled k8s-mcp.io/exec=\"true\", the binary in the allowlist, and namespace policy coverage. Server messages explain any denial.";
  $("form").prepend(note);
}

/* ── Auth / boot ──────────────────────────────────────────────────────── */

function setConnected(on, detail) {
  const pill = $("conn-pill");
  pill.classList.toggle("on", on);
  pill.classList.toggle("off", !on);
  $("conn-label").textContent = on ? detail : "not connected";
}

function logout(toastMsg) {
  state.key = "";
  state.tools = new Map();
  sessionStorage.removeItem("k8s-mcp-key");
  $("login").classList.remove("hidden");
  $("form").classList.add("hidden");
  $("output-wrap").classList.add("hidden");
  $("table-wrap").classList.add("hidden");
  $("nav-groups").textContent = "";
  setConnected(false);
  if (toastMsg) toast(toastMsg, true);
}

async function connect(key, remember) {
  state.key = key;
  try {
    const res = await rpc("tools/list", {});
    state.tools = new Map((res.tools || []).map((t) => [t.name, t]));
    if (!state.tools.size) throw new Error("server registered no tools");
  } catch (e) {
    state.key = "";
    throw e;
  }
  if (remember) sessionStorage.setItem("k8s-mcp-key", key);
  $("login").classList.add("hidden");
  setConnected(true, `connected · ${state.tools.size} tools`);
  $("foot-version").textContent = `console ${CONSOLE_VERSION} · protocol ${PROTOCOL_VERSION}`;
  await Promise.all([refreshNamespaces(true), refreshApiResources(true)]);
  renderNav();
  navigate("health");
}

/* ── Toast ────────────────────────────────────────────────────────────── */

let toastTimer = null;
function toast(msg, isError) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.toggle("error", !!isError);
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 4200);
}

/* ── Wire up ──────────────────────────────────────────────────────────── */

function wire() {
  $("form").addEventListener("submit", (ev) => { ev.preventDefault(); runCurrent(); });
  $("login-go").addEventListener("click", async () => {
    const key = $("login-key").value.trim();
    const err = $("login-error");
    if (!key) { err.textContent = "Enter the API key."; err.classList.remove("hidden"); return; }
    err.classList.add("hidden");
    const btn = $("login-go");
    btn.disabled = true;
    try {
      await connect(key, $("login-remember").checked);
    } catch (e) {
      err.textContent = e.message;
      err.classList.remove("hidden");
    } finally {
      btn.disabled = false;
    }
  });
  $("login-key").addEventListener("keydown", (ev) => { if (ev.key === "Enter") $("login-go").click(); });

  $("btn-settings").addEventListener("click", () => {
    $("set-key").value = state.key;
    $("set-exec-ns").value = state.execHeader;
    $("settings").showModal();
  });
  $("set-cancel").addEventListener("click", () => $("settings").close());
  $("set-save").addEventListener("click", () => {
    const newKey = $("set-key").value.trim();
    state.execHeader = $("set-exec-ns").value.trim();
    if (newKey && newKey !== state.key) {
      state.key = newKey;
      sessionStorage.setItem("k8s-mcp-key", newKey);
    }
    $("settings").close();
    toast("Settings saved" + (state.execHeader ? ` (X-Exec-Namespaces: ${state.execHeader})` : ""));
  });
  $("btn-copy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(state.lastText); toast("Copied to clipboard"); }
    catch { toast("Clipboard unavailable", true); }
  });
  $("btn-raw").addEventListener("click", () => {
    const cur = $("output").textContent || "";
    const alt = prettyJson(state.lastText) || state.lastText;
    $("output").textContent = cur === alt ? state.lastText : alt;
  });
}

async function boot() {
  wire();
  const saved = sessionStorage.getItem("k8s-mcp-key");
  if (saved) {
    try { await connect(saved, true); return; } catch { /* fall through to login */ }
  }
  $("login").classList.remove("hidden");
}

boot();
