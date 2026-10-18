"use strict";
const $ = (s, r = document) => r.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n, d = 2) => (n === null || n === undefined ? "n/a" : Number(n).toFixed(d));
let customers = [];
const charts = {};

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  let data = null;
  try { data = await res.json(); } catch (e) { /* empty body */ }
  if (!res.ok) {
    const msg = (data && (data.error || data.detail || JSON.stringify(data))) || res.statusText;
    toast(`Error ${res.status}: ${msg}`);
    throw new Error(msg);
  }
  return data;
}
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.style.display = "block"; clearTimeout(toast.t); toast.t = setTimeout(() => (t.style.display = "none"), 5000); }
const post = (p, body) => api(p, { method: "POST", body: JSON.stringify(body || {}) });

// ------------------------------------------------------------------ navigation
document.querySelectorAll("#nav button").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));
function show(v) {
  document.querySelectorAll("#nav button").forEach((b) => b.classList.toggle("active", b.dataset.view === v));
  document.querySelectorAll(".view").forEach((s) => s.classList.toggle("active", s.id === "view-" + v));
  if (v === "overview") loadOverview();
  if (v === "memory") loadMemory();
  if (v === "evaluation") loadEval(false);
  if (v === "challenge") loadChallengeInfo();
}

// ------------------------------------------------------------------ report rendering
const bandPill = (b) => `<span class="band ${esc(b)}">${esc(b)}</span>`;
const LAYER = { observed_fact: ["fact", "observed fact"], computed_metric: ["metric", "computed metric"], heuristic_score: ["heur", "heuristic"], ai_interpretation: ["ai", "AI interpretation"] };
const layerTag = (l) => { const [c, t] = LAYER[l] || ["", l]; return `<span class="tag ${c}">${t}</span>`; };

function evBlock(items, cls) {
  if (!items.length) return '<p class="muted small">none</p>';
  return items.map((e) => `<div class="ev ${cls}">${layerTag(e.layer)}${esc(e.text)}${e.provenance && e.provenance.queries ? `<div class="muted small">source: ${esc(e.provenance.queries.join(", "))}</div>` : ""}</div>`).join("");
}
function dist(d) { return Object.entries(d || {}).map(([k, v]) => `${esc(k)} ${(v * 100).toFixed(0)}%`).join(", ") || "—"; }

function renderReport(r) {
  const m = r.layers.computed_metrics, risk = r.risk;
  const comps = risk.components.map((c) => {
    const w = Math.min(100, (Math.abs(c.points) / Math.abs(c.max_points || 1)) * 100);
    return `<tr><td>${esc(c.name)}<div class="muted small">${esc(c.group)}</div></td><td style="width:120px"><div class="bar"><i class="${c.points < 0 ? "neg" : ""}" style="width:${w}%"></i></div></td><td>${c.points >= 0 ? "+" : ""}${fmt(c.points, 1)} / ${fmt(c.max_points, 0)}</td><td class="small">${esc(c.rationale)}</td></tr>`;
  }).join("");
  const paths = r.graph_paths.length ? r.graph_paths.map((p) => `<div class="path"><b>${esc(p.other_id)}</b> · ${p.customer_hops} hop(s) · <span class="tag">${esc(p.link_class)}</span>${p.established_benign ? '<span class="tag">established / benign</span>' : ""}<br>${esc(p.display)}</div>`).join("") : '<p class="muted small">No linked customers within the traversal depth.</p>';
  const ch = r.changes;
  const changes = ch ? `<div class="card"><h3>What changed since ${esc(ch.previous_investigation_id)}</h3><p><b>${esc(ch.explanation)}</b></p>
    ${ch.new_events.length ? `<table><tr><th>Event</th><th>Customer</th><th>Type</th><th>Category</th><th>seq</th></tr>${ch.new_events.map((e) => `<tr><td>${esc(e.id)}</td><td>${esc(e.customer_id)}</td><td>${esc(e.type)}</td><td>${esc(e.category)}</td><td>${e.seq}</td></tr>`).join("")}</table>` : ""}
    ${ch.evidence_added.map((e) => `<div class="ev sup">＋ ${esc(e.text)}</div>`).join("")}${ch.evidence_removed.map((e) => `<div class="ev con">− ${esc(e.text)}</div>`).join("")}</div>` : '<div class="card muted small">No earlier investigation of this customer in memory.</div>';
  const prev = r.previous_investigations;
  const prevHtml = [...prev.same_target.map((p) => `<tr><td>${esc(p.id)}</td><td>same target</td><td>${fmt(p.risk_score, 0)} ${bandPill(p.band)}</td><td></td></tr>`),
    ...prev.related.map((p) => `<tr><td>${esc(p.id)}</td><td>about ${esc(p.target_id)}</td><td>${fmt(p.risk_score, 0)} ${bandPill(p.band)}</td><td class="small">via ${esc(p.shared_entities.join(", "))}</td></tr>`)].join("");
  const trace = r.tool_trace.map((t) => `<tr><td>${t.step}</td><td>${esc(t.tool)}</td><td>${t.ms} ms</td><td class="small">${esc(t.summary)}<br>${t.queries.map((q) => `<span class="tag">${esc(q.name)} · ${q.rows} rows</span>`).join("")}</td></tr>`).join("");
  const ai = r.layers.ai_interpretation;
  return `
  <div class="card"><div class="toolbar" style="margin:0"><div><div class="muted small">Investigation ${esc(r.investigation_id || "(not saved)")} · target ${esc(r.target.id)} (${esc(r.target.name)}) · ${esc(r.max_customer_hops)} customer hop(s)</div>
    <div class="score">${fmt(risk.score, 0)}<small class="muted" style="font-size:16px">/100</small> ${bandPill(risk.band)}</div></div></div>
    <p>${esc(risk.interpretation)}</p><p class="small"><b>${esc(risk.not_proof_notice)}</b></p>
    <p><b>Recommended next action:</b> ${esc(r.recommended_action)}</p></div>
  ${changes}
  <div class="card"><h3>${layerTag("ai_interpretation")} Interpretation <small class="muted">(${esc(ai.label)})</small></h3><p>${esc(ai.text)}</p></div>
  <div class="card"><h3>${layerTag("computed_metric")} Behavioural metrics</h3>
    <table><tr><th></th><th>Baseline (${m.baseline_events} events)</th><th>Recent (${m.recent_events} events)</th></tr>
    <tr><td>Category entropy (bits)</td><td>${fmt(m.category_entropy.baseline.entropy_bits)}</td><td>${fmt(m.category_entropy.recent.entropy_bits)}</td></tr>
    <tr><td>Normalised entropy</td><td>${fmt(m.category_entropy.baseline.normalized_entropy)}</td><td>${fmt(m.category_entropy.recent.normalized_entropy)}</td></tr>
    <tr><td>Category mix</td><td class="small">${dist(m.category_entropy.baseline.distribution)}</td><td class="small">${dist(m.category_entropy.recent.distribution)}</td></tr></table>
    <p>Behavioural drift (Jensen–Shannon, bits): category <b>${fmt(m.drift.category_js, 3)}</b>, time-of-day <b>${fmt(m.drift.hour_js, 3)}</b>, composite <b>${fmt(m.drift.composite_js, 3)}</b> · entropy shift ${fmt(m.entropy_shift, 3)} · status ${esc(m.status)}</p>
    <p class="muted small">${esc(m.formulae)}. High entropy alone is never scored as fraud.</p></div>
  <div class="card"><h3>${layerTag("heuristic_score")} Risk components <small class="muted">config ${esc(risk.config_version)} · behavioural ${fmt(risk.behavioural_points, 1)} + graph ${fmt(risk.graph_points, 1)} + mitigation ${fmt(risk.mitigation_points, 1)}</small></h3>
    <table><tr><th>Component</th><th></th><th>Points</th><th>Why</th></tr>${comps}</table></div>
  <div class="card"><h3>${layerTag("observed_fact")} Multi-hop graph paths (from FalkorDB)</h3>${paths}</div>
  <div class="grid3"><div class="card"><h3>Supporting evidence</h3>${evBlock(r.evidence.supporting, "sup")}</div>
    <div class="card"><h3>Contradicting evidence</h3>${evBlock(r.evidence.contradicting, "con")}</div>
    <div class="card"><h3>Missing evidence</h3>${evBlock(r.evidence.missing, "mis")}</div></div>
  <div class="card"><h3>Alternative benign explanations</h3><ul>${r.benign_explanations.map((b) => `<li>${esc(b)}</li>`).join("")}</ul></div>
  <div class="card"><h3>Previous investigations (memory)</h3>${prevHtml ? `<table>${prevHtml}</table>` : '<p class="muted small">none</p>'}</div>
  <div class="card"><h3>Provenance &amp; limitations</h3><p class="small">Graph <b>${esc(r.provenance.graph)}</b> · scenario ${esc(r.provenance.scenario)} · event sources ${esc(JSON.stringify(r.provenance.event_sources))}</p>
    <ul class="small">${r.limitations.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>
    <details><summary>Agent tool trace &amp; Cypher queries executed (${r.total_ms} ms)</summary><table><tr><th>#</th><th>Tool</th><th>Time</th><th>Detail</th></tr>${trace}</table></details></div>`;
}

// ------------------------------------------------------------------ cytoscape
const COLORS = { Customer: "#4f46e5", Session: "#9aa3b8", Device: "#16a34a", IP: "#d97706", Coupon: "#9333ea", Investigation: "#dc2626", Order: "#0891b2" };
function drawGraph(el, data, onTap) {
  if (charts[el]) charts[el].destroy();
  const els = [...data.nodes.map((n) => ({ data: { id: n.id, label: n.raw_id, type: n.label, kind: n.kind, net: n.network_type, props: n } })),
    ...data.edges.map((e, i) => ({ data: { id: "e" + i, source: e.source, target: e.target, label: e.type, link: e.link_class || "" } }))];
  const cy = cytoscape({
    container: document.getElementById(el), elements: els, wheelSensitivity: 0.3,
    style: [
      { selector: "node", style: { "background-color": (n) => COLORS[n.data("type")] || "#888", label: "data(label)", "font-size": 9, color: "#555", "text-valign": "bottom", "text-margin-y": 3, width: (n) => (n.data("type") === "Customer" ? 26 : n.data("type") === "Session" ? 9 : 17), height: (n) => (n.data("type") === "Customer" ? 26 : n.data("type") === "Session" ? 9 : 17), shape: (n) => (n.data("type") === "Investigation" ? "diamond" : n.data("type") === "Coupon" ? "round-rectangle" : "ellipse") } },
      { selector: "edge", style: { width: 1.5, "line-color": "#c3c8d8", "target-arrow-color": "#c3c8d8", "target-arrow-shape": "triangle", "curve-style": "bezier", "arrow-scale": 0.7 } },
      { selector: 'edge[link = "private_device"]', style: { "line-color": "#dc2626", "target-arrow-color": "#dc2626", width: 2.5 } },
      { selector: 'edge[link = "ip_weak"]', style: { "line-color": "#d97706", "target-arrow-color": "#d97706", width: 2 } },
      { selector: 'edge[link = "public_shared"]', style: { "line-style": "dashed" } },
      { selector: "edge[label = 'REDEEMED']", style: { "line-color": "#9333ea", "target-arrow-color": "#9333ea", "line-style": "dotted" } },
      { selector: ":selected", style: { "border-width": 3, "border-color": "#111" } },
    ],
    layout: { name: "cose", animate: false, padding: 24, nodeRepulsion: 12000, idealEdgeLength: 60 },
  });
  if (onTap) cy.on("tap", "node", (e) => onTap(e.target.data("props")));
  charts[el] = cy;
  return cy;
}
const legendHtml = () => Object.entries(COLORS).map(([k, c]) => `<span><i style="background:${c}"></i>${k}</span>`).join("") + '<span style="color:#dc2626">━ private-device link</span><span style="color:#d97706">━ weak IP link</span>';

// ------------------------------------------------------------------ overview
async function loadOverview() {
  const o = await api("/api/overview");
  const labels = o.labels.map((l) => `<tr><td>${esc(l.label)}</td><td>${l.count}</td></tr>`).join("");
  const rels = o.relationships.map((l) => `<tr><td>${esc(l.type)}</td><td>${l.count}</td></tr>`).join("");
  const board = o.leaderboard.map((b) => `<tr><td><a href="#" data-inv="${esc(b.customer_id)}">${esc(b.customer_id)}</a> ${esc(b.name)}</td><td>${fmt(b.score, 0)}</td><td>${bandPill(b.band)}</td><td class="small">${esc(b.linked.join(", ") || "—")}</td></tr>`).join("");
  const mem = o.recent_investigations.map((m) => `<tr><td>${esc(m.id)}</td><td>${esc(m.target_id)}</td><td>${fmt(m.risk_score, 0)} ${bandPill(m.band)}</td></tr>`).join("");
  $("#view-overview").innerHTML = `<h2>Overview <small>scenario ${esc(o.clock.scenario)}</small></h2>
   <div class="grid3"><div class="card"><h3>Nodes in FalkorDB</h3><table>${labels}</table></div><div class="card"><h3>Relationships</h3><table>${rels}</table></div>
   <div class="card"><h3>Recent investigations (memory)</h3>${mem ? `<table>${mem}</table>` : '<p class="muted small">none yet</p>'}</div></div>
   <div class="card"><h3>Highest graph-assisted risk assessments</h3><p class="muted small">${esc(o.note)}</p><table><tr><th>Customer</th><th>Score</th><th>Band</th><th>Linked accounts</th></tr>${board}</table></div>`;
  document.querySelectorAll("[data-inv]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); $("#inv-customer").value = a.dataset.inv; show("investigate"); runInvestigate(); }));
}

// ------------------------------------------------------------------ investigate
async function runInvestigate() {
  const body = { customer_id: $("#inv-customer").value, persist: $("#inv-persist").checked, max_customer_hops: +$("#inv-hops").value };
  $("#inv-out").innerHTML = '<p class="muted">Investigating…</p>';
  const r = await post("/api/investigate", body);
  $("#inv-out").innerHTML = renderReport(r);
}
$("#inv-run").addEventListener("click", runInvestigate);

// ------------------------------------------------------------------ explorer
async function loadExplorer() {
  const q = new URLSearchParams({ customer_id: $("#ex-customer").value, hops: $("#ex-hops").value, sessions: $("#ex-sessions").checked, commerce: $("#ex-commerce").checked, memory: $("#ex-memory").checked });
  const d = await api("/api/graph/neighborhood?" + q);
  $("#legend").innerHTML = legendHtml();
  const cy = drawGraph("ex-graph", d, (p) => ($("#ex-node").textContent = JSON.stringify(p, null, 2)));
  cy.getElementById(d.center).select();
  $("#ex-paths").innerHTML = d.paths.map((p) => `<div class="path"><b>${esc(p.other_id)}</b> · ${p.customer_hops} hop(s) · <span class="tag">${esc(p.link_class)}</span><br>${esc(p.display)}</div>`).join("") || '<p class="muted small">No linked customers.</p>';
}
$("#ex-run").addEventListener("click", loadExplorer);

// ------------------------------------------------------------------ timeline
async function loadTimeline() {
  const id = $("#tl-customer").value;
  const d = await api(`/api/customers/${encodeURIComponent(id)}/timeline?include_linked=${$("#tl-linked").checked}`);
  const rows = [];
  d.events.filter((e) => e.type !== "view" || e.is_target).slice(-120).forEach((e) => rows.push({ ts: e.ts, html: `<div class="it ${e.is_target ? "t" : ""} ${e.type === "purchase" ? "buy" : ""}"><b>${esc(e.customer_id)}</b> ${esc(e.type)} ${esc(e.category)}${e.amount ? " ($" + e.amount + ")" : ""} · ${esc(e.device_id)} / ${esc(e.ip_id)} · <span class="muted">${esc(e.source)}</span></div>` }));
  const items = rows.sort((a, b) => a.ts - b.ts).map((r) => `<div class="muted small">${new Date(r.ts * 1000).toISOString().replace("T", " ").slice(0, 16)}</div>${r.html}`).join("");
  const co = d.coupon_redemptions.map((c) => `<tr><td>${esc(c.coupon)}</td><td>${esc(c.customer_id)}</td><td>${esc(c.order_id)}</td><td>${new Date(c.placed_at * 1000).toISOString().slice(0, 16).replace("T", " ")}</td></tr>`).join("");
  const inv = d.investigations.map((m) => `<div class="it inv"><b>${esc(m.id)}</b> ${fmt(m.risk_score, 0)} ${bandPill(m.band)} · graph seq ${m.event_seq}</div>`).join("");
  $("#tl-out").innerHTML = `<div class="grid2"><div class="card"><h3>Events ${d.linked_customers.length ? "(incl. linked: " + esc(d.linked_customers.join(", ")) + ")" : ""}</h3><p class="muted small">Views of linked accounts are hidden; purchases are red, the target is blue.</p><div class="tl">${items}</div></div>
   <div><div class="card"><h3>Coupon redemptions</h3>${co ? `<table><tr><th>Coupon</th><th>Customer</th><th>Order</th><th>When</th></tr>${co}</table>` : '<p class="muted small">none</p>'}</div>
   <div class="card"><h3>Investigations of this customer</h3><div class="tl">${inv || '<p class="muted small">none</p>'}</div></div></div></div>`;
}
$("#tl-run").addEventListener("click", loadTimeline);

// ------------------------------------------------------------------ memory
async function loadMemory() {
  const d = await api("/api/investigations");
  $("#mem-list").innerHTML = d.investigations.length ? `<table><tr><th>ID</th><th>Target</th><th>Score</th><th>Graph seq</th></tr>${d.investigations.map((m) => `<tr style="cursor:pointer" data-id="${esc(m.id)}"><td>${esc(m.id)}</td><td>${esc(m.target_id)}</td><td>${fmt(m.risk_score, 0)} ${bandPill(m.band)}</td><td>${m.event_seq}</td></tr>`).join("")}</table>` : '<p class="muted">Memory is empty. Run an investigation.</p>';
  document.querySelectorAll("#mem-list tr[data-id]").forEach((tr) => tr.addEventListener("click", async () => {
    const m = await api("/api/investigations/" + encodeURIComponent(tr.dataset.id));
    $("#mem-detail").innerHTML = `<div class="card"><h3>${esc(m.id)} → ${esc(m.target_id)}</h3><p>${esc(m.conclusion)}</p><p class="small muted">stored ${esc(m.created_at)} · supersedes: ${esc(m.supersedes.map((s) => s.id).join(", ") || "none")} · ${m.findings.length} Finding nodes</p></div>` + (m.report ? renderReport(m.report) : "");
  }));
}
$("#mem-refresh").addEventListener("click", loadMemory);
$("#mem-restart").addEventListener("click", async () => { const r = await post("/api/admin/restart"); toast(`Restarted. ${r.memory_investigations_found} investigation(s) recovered from FalkorDB.`); loadMemory(); });

// ------------------------------------------------------------------ evaluation
const pct = (x) => (x === null || x === undefined ? "n/a" : (x * 100).toFixed(0) + "%");
async function loadEval(run) {
  let d;
  try { d = run ? await post("/api/evaluation/run") : await api("/api/evaluation/latest"); } catch (e) { return; }
  const rows = Object.entries(d.methods).filter(([, m]) => m.precision !== undefined).map(([n, m]) => `<tr><td><b>${esc(n)}</b><div class="small muted">${esc(m.description)}</div></td><td>${m.tp}/${m.fp}/${m.fn}/${m.tn}</td><td>${pct(m.precision)}</td><td>${pct(m.recall)}</td><td>${fmt(m.f1)}</td><td>${pct(m.false_positive_rate)}</td><td>${fmt(m.roc_auc, 3)}</td><td>${m.latency_ms_mean} / ${m.latency_ms_p95}</td><td class="small">${esc(JSON.stringify(m.false_positives_by_group))}</td></tr>`).join("");
  const pd = ["path_discovery_1_hop", "path_discovery_2_hop"].map((k) => `<tr><td>${k}</td><td>${d.methods[k].all_expected_counterparts_found}/${d.methods[k].cases}</td></tr>`).join("");
  const u = d.conclusion_update, mem = d.memory;
  $("#ev-note").textContent = `${d.source || "live run"} · hash ${d.result_hash}`;
  $("#ev-out").innerHTML = `<div class="card"><h3>Same ${d.n_cases} cases (${d.n_positive} planted positives), same threshold (${d.flag_threshold}) for every method</h3>
   <table><tr><th>Method</th><th>TP/FP/FN/TN</th><th>Precision</th><th>Recall</th><th>F1</th><th>FPR</th><th>ROC-AUC</th><th>Latency ms (mean / p95)</th><th>False positives by group</th></tr>${rows}</table></div>
   <div class="grid3"><div class="card"><h3>Correct path discovery</h3><table>${pd}</table></div>
   <div class="card"><h3>Memory retrieval</h3><p>Same target: ${mem.same_target_retrieval.correct}/${mem.same_target_retrieval.cases}<br>Related entities: ${mem.related_entity_retrieval.correct}/${mem.related_entity_retrieval.cases}</p></div>
   <div class="card"><h3>Conclusion update</h3><p>${fmt(u.before.score, 1)} (${esc(u.before.band)}) → ${fmt(u.after.score, 1)} (${esc(u.after.band)})<br>${u.passed ? "✔ passed" : "✘ failed"}</p></div></div>
   <div class="card"><h3>Caveats</h3><ul>${d.caveats.map((c) => `<li>${esc(c)}</li>`).join("")}</ul></div>`;
}
$("#ev-run").addEventListener("click", () => { $("#ev-out").innerHTML = '<p class="muted">Running on a separate evaluation graph…</p>'; loadEval(true); });

// ------------------------------------------------------------------ ingest / reset
$("#ing-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = Object.fromEntries(new FormData(e.target).entries());
  const body = { ...f, amount: f.amount === "" ? null : +f.amount, coupon: f.coupon || null };
  try { $("#ing-out").textContent = JSON.stringify(await post("/api/events", body), null, 2); toast("Event ingested into FalkorDB"); } catch (err) { $("#ing-out").textContent = String(err.message); }
});
$("#reset-btn").addEventListener("click", async () => { if (!confirm("Delete the graph and memory, then reload the scenario?")) return; $("#ing-out").textContent = JSON.stringify(await post("/api/scenario/reset"), null, 2); await loadCustomers(); toast("Scenario reset"); });

// ------------------------------------------------------------------ challenge
async function loadChallengeInfo() { const i = await api("/api/challenge/info"); $("#challenge-story").textContent = i.story; }
let chInv1 = null;
const chOut = (h) => ($("#ch-out").innerHTML = h);
const enable = (n) => ($("#ch-" + n).disabled = false);
async function chGraph(id) {
  const d = await api(`/api/graph/neighborhood?customer_id=${id}&hops=2&sessions=true&commerce=true&memory=true`);
  drawGraph("ch-graph", d, null);
  $("#ch-paths").innerHTML = d.paths.map((p) => `<div class="path"><b>${esc(p.other_id)}</b> (${esc(p.link_class)}): ${esc(p.display)}</div>`).join("");
}
$("#ch-1").addEventListener("click", async () => { const s = await post("/api/scenario/reset"); chOut(`<div class="card"><b>Scenario loaded</b><pre class="small">${esc(JSON.stringify(s, null, 2))}</pre></div>`); await loadCustomers(); await chGraph("A1"); enable(2); });
$("#ch-2").addEventListener("click", async () => { chInv1 = await post("/api/investigate", { customer_id: "A1", persist: true }); chOut(renderReport(chInv1)); await chGraph("A1"); enable(3); enable(4); });
$("#ch-3").addEventListener("click", async () => {
  const c = await api("/api/compare/A1");
  const row = (m, n) => `<tr><td><b>${n}</b><div class="small muted">${esc(m.uses)}</div></td><td>${fmt(m.score, 0)}</td><td>${m.flagged ? "flagged" : "not flagged"}</td><td>${m.ms} ms</td></tr>`;
  chOut(`<div class="card"><h3>A1 — same data, same threshold (${c.flag_threshold})</h3><table><tr><th>Method</th><th>Score</th><th>Decision</th><th>Time</th></tr>${row(c.isolated, "Isolated-event baseline")}${row(c.naive_graph, "Naive shared-infrastructure rule")}${row(c.graph_assisted, "Graph-assisted agent")}</table>
   <p class="small">The graph sees ${c.scope.reachable_customers_1hop} other customer(s) through ${c.scope.shared_infra_nodes} shared node(s) at one hop; the isolated baseline sees none. Naive rule shares with: ${esc((c.naive_graph.shares_with || []).join(", "))} (it also flags the benign household member C1).</p></div>`);
});
$("#ch-4").addEventListener("click", async () => { try { const r = await post("/api/challenge/new-event"); chOut(`<div class="card"><h3>New event ingested</h3><pre class="small">${esc(JSON.stringify(r, null, 2))}</pre></div>`); enable(5); } catch (e) { /* toast shown */ } });
$("#ch-5").addEventListener("click", async () => { const r = await post("/api/investigate", { customer_id: "A1", persist: true }); chOut(renderReport(r)); await chGraph("A1"); enable(6); });
$("#ch-6").addEventListener("click", async () => { const r = await post("/api/admin/restart"); chOut(`<div class="card"><h3>Backend connection and caches dropped, then reconnected</h3><p>${r.memory_investigations_found} investigation(s) found in FalkorDB immediately after reconnecting.</p><p class="small muted">For a true process restart see DEMO_SCRIPT.md (docker compose restart api / falkordb).</p></div>`); enable(7); });
$("#ch-7").addEventListener("click", async () => {
  const l = await api("/api/investigations?customer_id=A1"); const first = l.investigations[l.investigations.length - 1];
  const m = await api("/api/investigations/" + first.id);
  chOut(`<div class="card"><h3>Retrieved from persistent memory</h3><p>${l.investigations.map((x) => `<b>${esc(x.id)}</b> ${fmt(x.risk_score, 0)} ${bandPill(x.band)}`).join(" → ")}</p><p class="small muted">${m.findings.length} Finding nodes stored for ${esc(m.id)}</p></div>` + renderReport(m.report));
});

// ------------------------------------------------------------------ boot
async function loadCustomers() {
  customers = (await api("/api/customers")).customers;
  const opts = customers.map((c) => `<option value="${esc(c.id)}">${esc(c.id)} — ${esc(c.name)}</option>`).join("");
  ["#inv-customer", "#ex-customer", "#tl-customer"].forEach((s) => { const keep = $(s).value; $(s).innerHTML = opts; if (keep) $(s).value = keep; });
  if (!$("#inv-customer").value) ["#inv-customer", "#ex-customer", "#tl-customer"].forEach((s) => ($(s).value = "A1"));
}
(async function boot() {
  try {
    const h = await api("/api/health");
    $("#health").textContent = `FalkorDB ${h.status} · ${h.db_mode} · ${h.interpretation_mode === "DeterministicInterpreter" ? "deterministic mode" : "LLM text mode"}`;
    $("#health").classList.add(h.status === "ok" ? "ok" : "bad");
    const cats = ["electronics", "books", "home", "fashion", "grocery", "sports", "beauty", "toys", "gift_cards", "travel"];
    $("#ing-cat").innerHTML = cats.map((c) => `<option ${c === "gift_cards" ? "selected" : ""}>${c}</option>`).join("");
    await loadCustomers(); loadOverview(); loadChallengeInfo();
  } catch (e) { $("#health").textContent = "backend unreachable"; $("#health").classList.add("bad"); }
})();
