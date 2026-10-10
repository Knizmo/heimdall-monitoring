/* =========================================================================
   Zabbix AI Assistant — Heimdall · front vanilla JS
   Vues : chat · supervision · base_ia (assistant_ai) · base_jeux (postgres)
   ========================================================================= */
"use strict";

const API_BASE = location.protocol.startsWith("http") ? "" : "http://localhost:5000";
const METRICS_INTERVAL = 30_000;
const AVATAR_IMG = "/static/logo/heimdall.jpg";

if (window.marked) marked.setOptions({ breaks: true, gfm: true });

/* ---------- Utilitaires DOM ---------- */
function el(tag, cls, html) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
}
function $(id) { return document.getElementById(id); }
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function nowLabel() {
  return new Date().toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
}
async function jfetch(url, opts) {
  const r = await fetch(API_BASE + url, opts);
  let data = {};
  try { data = await r.json(); } catch { /* vide */ }
  if (!r.ok) throw new Error(data.erreur || data.erreur_groq || data.erreur_rag || `HTTP ${r.status}`);
  return data;
}

/* ---------- Toasts ---------- */
function toast(msg, type = "") {
  const t = el("div", "toast " + type, escapeHtml(msg));
  $("toast-root").appendChild(t);
  setTimeout(() => { t.style.opacity = "0"; setTimeout(() => t.remove(), 200); }, 3200);
}

/* ---------- Modale générique ---------- */
function openModal({ title, fields, submitLabel = "Enregistrer", onSubmit }) {
  const root = $("modal-root");
  root.innerHTML = "";
  const overlay = el("div", "modal-overlay");
  const modal = el("div", "modal");

  const head = el("div", "modal-head");
  head.appendChild(el("h3", null, escapeHtml(title)));
  const close = el("button", "ghost-btn", "✕");
  head.appendChild(close);

  const body = el("div", "modal-body");
  const inputs = {};
  fields.forEach(f => {
    const field = el("div", "field");
    const lbl = el("label", null,
      escapeHtml(f.label || f.name) + (f.hint ? ` <span class="hint">${escapeHtml(f.hint)}</span>` : ""));
    field.appendChild(lbl);
    let input;
    if (f.textarea) {
      input = el("textarea");
    } else if (f.select) {
      input = el("select");
      f.select.forEach(o => { const opt = el("option", null, escapeHtml(o)); opt.value = o; input.appendChild(opt); });
    } else {
      input = el("input");
      input.type = f.type || "text";
    }
    input.value = f.value ?? "";
    if (f.disabled) input.disabled = true;
    if (f.placeholder) input.placeholder = f.placeholder;
    inputs[f.name] = input;
    field.appendChild(input);
    body.appendChild(field);
  });

  const foot = el("div", "modal-foot");
  const cancel = el("button", "term-btn ghost", "annuler");
  const ok = el("button", "term-btn accent", escapeHtml(submitLabel));
  foot.appendChild(cancel); foot.appendChild(ok);

  modal.appendChild(head); modal.appendChild(body); modal.appendChild(foot);
  overlay.appendChild(modal); root.appendChild(overlay);

  const destroy = () => root.innerHTML = "";
  close.onclick = cancel.onclick = destroy;
  overlay.onclick = e => { if (e.target === overlay) destroy(); };
  ok.onclick = async () => {
    const values = {};
    Object.keys(inputs).forEach(k => values[k] = inputs[k].value);
    ok.disabled = true; ok.textContent = "…";
    try {
      await onSubmit(values);
      destroy();
    } catch (e) {
      toast(e.message, "err");
      ok.disabled = false; ok.textContent = submitLabel;
    }
  };
  const first = Object.values(inputs)[0];
  if (first && !first.disabled) first.focus();
}

function confirmModal(message, onYes) {
  openModal({
    title: "Confirmation", submitLabel: "supprimer",
    fields: [{ name: "_", label: message, disabled: true, value: "" }],
    onSubmit: onYes,
  });
}

/* =========================================================================
   Navigation entre vues
   ========================================================================= */
function switchView(name) {
  document.querySelectorAll(".view").forEach(v => v.classList.remove("active"));
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.view === name));
  const view = $("view-" + name);
  if (view) view.classList.add("active");
  // Les checks /health/* ne tournent que si la vue est visible :
  // /health/groq déclenche une vraie complétion Groq facturée à chaque appel.
  if (name === "supervision") { pollMetrics(); startHealth(); } else { stopHealth(); }
  if (name === "baseia") loadAidb();
  if (name === "basejeux") loadGames();
}
$("tabs").addEventListener("click", e => {
  const tab = e.target.closest(".tab");
  if (tab) switchView(tab.dataset.view);
});

/* =========================================================================
   Sidebars repliables
   ========================================================================= */
const chatLayout = $("chat-layout");
$("collapse-left").onclick  = () => chatLayout.classList.add("left-collapsed");
$("reopen-left").onclick    = () => chatLayout.classList.remove("left-collapsed");
$("collapse-right").onclick = () => chatLayout.classList.add("right-collapsed");
$("reopen-right").onclick   = () => chatLayout.classList.remove("right-collapsed");

/* =========================================================================
   Mascotte pixel-art : change l'état (direction) au clic
   ========================================================================= */
$("pet-controls").addEventListener("click", e => {
  const btn = e.target.closest(".pet-btn");
  if (!btn) return;
  $("pet-gif").src = `/static/pixel/${btn.dataset.gif}.gif`;
  $("pet-controls").querySelectorAll(".pet-btn").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
});

/* =========================================================================
   Statut live
   ========================================================================= */
function setStatus(state) {
  const s = $("live-status");
  const labels = { online: "en ligne", offline: "hors ligne", checking: "connexion…" };
  s.dataset.state = state;
  $("live-status-label").textContent = labels[state] || state;
}

/* =========================================================================
   CHAT
   ========================================================================= */
const $messages = $("messages");
const WELCOME_HTML = $messages.innerHTML;   // pour réafficher l'écran d'accueil
const CHAT_KEY = "heimdall_chat_v1";
let conversation = [];   // {id, role, text, sources}
let history = [];
let busy = false;
let msgSeq = 0;

function saveChat() {
  try { localStorage.setItem(CHAT_KEY, JSON.stringify({ conversation, history, msgSeq })); } catch { /* quota */ }
}
function loadChatState() {
  try {
    const d = JSON.parse(localStorage.getItem(CHAT_KEY) || "{}");
    conversation = d.conversation || [];
    history = d.history || [];
    msgSeq = d.msgSeq || 0;
  } catch { conversation = []; history = []; msgSeq = 0; }
}
function restoreChat() {
  loadChatState();
  if (conversation.length) {
    clearWelcome();
    conversation.forEach(m => addMessage(m.role, {
      id: m.id, markdown: m.role === "assistant", text: m.text, sources: m.sources || null,
    }));
  }
  renderHistory();
}

function clearWelcome() { const w = $messages.querySelector(".welcome"); if (w) w.remove(); }

function addMessage(role, opts = {}) {
  clearWelcome();
  const id = opts.id || ("msg-" + (++msgSeq));
  const wrap = el("div", `msg ${role}`); wrap.id = id;

  const avatar = el("div", "avatar");
  if (role === "assistant") avatar.innerHTML = `<img src="${AVATAR_IMG}" alt="Heimdall" />`;
  else avatar.textContent = "K";

  const bubble = el("div", "bubble");
  if (opts.typing) {
    bubble.appendChild(el("div", "typing", "<span></span><span></span><span></span>"));
  } else if (opts.markdown) {
    const b = el("div", "md-body");
    b.innerHTML = window.marked ? marked.parse(opts.text || "") : escapeHtml(opts.text);
    bubble.appendChild(b);
  } else {
    bubble.textContent = opts.text || "";
  }
  if (opts.sources) renderSources(bubble, opts.sources);
  wrap.appendChild(avatar); wrap.appendChild(bubble);
  $messages.appendChild(wrap);
  $messages.scrollTop = $messages.scrollHeight;
  return { id, wrap, bubble };
}

function renderSources(bubble, sources) {
  if (!sources || !sources.length) return;
  const box = el("div", "sources");
  box.appendChild(el("span", "lbl", "sources"));
  [...new Set(sources)].slice(0, 5).forEach(s => {
    const tag = el("span", "source-tag"); tag.textContent = s; tag.title = s; box.appendChild(tag);
  });
  bubble.appendChild(box);
}

async function ask(question) {
  if (busy || !question.trim()) return;
  busy = true; $("send-btn").disabled = true;
  const q = question.trim();
  const um = addMessage("user", { text: q });
  conversation.push({ id: um.id, role: "user", text: q });
  const histId = addHistory(q);
  saveChat();
  const typing = addMessage("assistant", { typing: true });
  try {
    const data = await jfetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q }),
    });
    typing.wrap.remove(); setStatus("online");
    const sources = (data.meta && data.meta.sources) || null;
    const text = data.reponse || "(réponse vide)";
    const m = addMessage("assistant", { markdown: true, text, sources });
    conversation.push({ id: m.id, role: "assistant", text, sources });
    linkHistoryToAnswer(histId, m.id);
    saveChat();
  } catch (e) {
    typing.wrap.remove(); setStatus("offline");
    const m = addMessage("assistant", { text: "⚠ " + e.message });
    m.wrap.classList.add("msg-error");
  } finally {
    busy = false; $("send-btn").disabled = false; $("input").focus();
  }
}

/* Historique */
function addHistory(question) {
  const id = "h-" + Date.now() + "-" + Math.random().toString(36).slice(2, 6);
  history.unshift({ id, question, ts: nowLabel(), answerMsgId: null });
  renderHistory();
  return id;
}
function linkHistoryToAnswer(histId, msgId) {
  const h = history.find(x => x.id === histId); if (h) h.answerMsgId = msgId;
}
function renderHistory() {
  const panel = $("history-panel");
  if (!history.length) { panel.innerHTML = '<div class="empty-state">aucune requête</div>'; return; }
  panel.innerHTML = "";
  history.forEach(h => {
    const item = el("div", "history-item");
    item.appendChild(el("div", "q", escapeHtml(h.question)));
    item.appendChild(el("div", "t", h.ts));
    item.onclick = () => {
      if (!h.answerMsgId) return;
      const t = $(h.answerMsgId);
      if (t) { t.scrollIntoView({ behavior: "smooth", block: "center" });
        t.classList.add("msg-highlight"); setTimeout(() => t.classList.remove("msg-highlight"), 1400); }
    };
    panel.appendChild(item);
  });
}
// La corbeille efface tout le chat (messages + historique) et réaffiche l'accueil.
$("clear-history").onclick = () => {
  conversation = []; history = [];
  $messages.innerHTML = WELCOME_HTML;
  renderHistory();
  saveChat();
};

$("composer").addEventListener("submit", e => {
  e.preventDefault();
  const q = $("input").value; $("input").value = ""; autoResize(); ask(q);
});
$("input").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("composer").requestSubmit(); }
});
function autoResize() {
  const i = $("input"); i.style.height = "auto"; i.style.height = Math.min(i.scrollHeight, 180) + "px";
}
$("input").addEventListener("input", autoResize);
// Délégation : survit au remplacement du contenu de #messages (corbeille / restore).
$messages.addEventListener("click", e => {
  const c = e.target.closest(".chip"); if (c) ask(c.textContent);
});

/* =========================================================================
   MÉTRIQUES LIVE (compact + vue supervision)
   ========================================================================= */
function metricIcon(kind) {
  const p = {
    mem: '<path d="M4 8h16v8H4z"/><path d="M8 8V5M12 8V5M16 8V5M8 19v-3M12 19v-3M16 19v-3"/>',
    cpu: '<rect x="7" y="7" width="10" height="10" rx="1.5"/><path d="M10 7V4M14 7V4M10 20v-3M14 20v-3M7 10H4M7 14H4M20 10h-3M20 14h-3"/>',
    net: '<path d="M4 12h16M12 4a15 15 0 0 1 0 16M12 4a15 15 0 0 0 0 16M12 4v16"/><circle cx="12" cy="12" r="9"/>',
    db:  '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
    gen: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/>',
  };
  return `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor"
           stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${p[kind] || p.gen}</svg>`;
}
function classify(name) {
  const n = (name || "").toLowerCase();
  if (/mem|mémoire|memoire|ram/.test(n)) return "mem";
  if (/cpu|processeur/.test(n)) return "cpu";
  if (/net|réseau|reseau|rx|tx|bytes/.test(n)) return "net";
  if (/pg|postgres|sql|db|base/.test(n)) return "db";
  return "gen";
}
function shortName(name) { return (name || "").replace(/^Container\s+/i, "").trim(); }

const SEV_COLOR = {
  disaster: "var(--sev-disaster)", high: "var(--sev-high)", average: "var(--sev-average)",
  warning: "var(--sev-warning)", info: "var(--sev-info)",
};
function sevColor(s) { return SEV_COLOR[(s || "").toLowerCase()] || "var(--sev-unknown)"; }

function splitStates(metriques) {
  const states = [], others = [];
  (metriques || []).forEach(m => {
    const v = String(m.valeur || "").toLowerCase();
    if (v === "en marche" || v === "arrêté" || v === "arrete") states.push(m); else others.push(m);
  });
  return { states, others };
}

/* Blocs réutilisables -------------------------------------------------- */
function buildProblems(problemes) {
  const g = el("div", "metric-group");
  g.appendChild(el("div", "metric-group-title",
    `problèmes <span class="count">${problemes.length}</span>`));
  if (!problemes.length) {
    g.appendChild(el("div", "prob-ok", "<span>✓ aucun problème actif</span>"));
  } else {
    problemes.forEach(p => {
      const row = el("div", "prob-row");
      row.style.borderLeftColor = sevColor(p.severite);
      const sev = el("div", "p-sev", escapeHtml(p.severite || "?")); sev.style.color = sevColor(p.severite);
      row.appendChild(sev);
      row.appendChild(el("div", "p-name", escapeHtml(p.probleme || "")));
      g.appendChild(row);
    });
  }
  return g;
}
function buildContainers(states) {
  if (!states.length) return null;
  const g = el("div", "metric-group");
  g.appendChild(el("div", "metric-group-title", `conteneurs <span class="count">${states.length}</span>`));
  states.forEach(m => {
    const up = /marche/.test(String(m.valeur).toLowerCase());
    const card = el("div", `state-card ${up ? "up" : "down"}`);
    card.appendChild(el("span", "dot"));
    const nm = el("span", "name", escapeHtml(shortName(m.metrique))); nm.title = m.metrique;
    card.appendChild(nm);
    card.appendChild(el("span", "badge", up ? "actif" : "arrêté"));
    g.appendChild(card);
  });
  return g;
}
function buildHosts(hotes) {
  if (!hotes.length) return null;
  const g = el("div", "metric-group");
  g.appendChild(el("div", "metric-group-title", `hôtes <span class="count">${hotes.length}</span>`));
  hotes.forEach(h => {
    const on = (h.supervision || "").toLowerCase().includes("activ");
    const row = el("div", `host-row ${on ? "on" : "off"}`);
    row.appendChild(el("span", "dot"));
    const nm = el("span", "h-name", escapeHtml(h.host || "")); nm.title = h.host;
    row.appendChild(nm);
    row.appendChild(el("span", "h-sup", escapeHtml(h.supervision || "")));
    g.appendChild(row);
  });
  return g;
}
// Ordre imposé des métriques : CPU → mémoire → réseau → (db) → reste
const METRIC_ORDER = { cpu: 0, mem: 1, net: 2, db: 3, gen: 4 };
function buildMetrics(others) {
  if (!others.length) return null;
  const ordered = [...others].sort(
    (a, b) => (METRIC_ORDER[classify(a.metrique)] ?? 9) - (METRIC_ORDER[classify(b.metrique)] ?? 9));
  const g = el("div", "metric-group");
  g.appendChild(el("div", "metric-group-title", `métriques <span class="count">${ordered.length}</span>`));
  ordered.forEach(m => {
    const kind = classify(m.metrique);
    const row = el("div", "metric-row");
    row.appendChild(el("span", `ic ${kind}`, metricIcon(kind)));
    const meta = el("div", "meta");
    const nm = el("div", "m-name", escapeHtml(shortName(m.metrique))); nm.title = m.metrique;
    meta.appendChild(nm);
    meta.appendChild(el("div", "m-val", escapeHtml(String(m.valeur))));
    row.appendChild(meta);
    g.appendChild(row);
  });
  return g;
}

/* Rendu compact (sidebar chat) */
function renderCompact(ctx) {
  const { states, others } = splitStates(ctx.metriques);
  const frag = document.createDocumentFragment();
  frag.appendChild(buildProblems(ctx.problemes || []));
  [buildContainers(states), buildHosts(ctx.hotes || []), buildMetrics(others)]
    .forEach(b => b && frag.appendChild(b));
  const panel = $("chat-metrics");
  panel.innerHTML = ""; panel.appendChild(frag);
}

/* Rendu vue supervision dédiée */
function renderSupervision(ctx) {
  const hotes = ctx.hotes || [], problemes = ctx.problemes || [];
  const { states, others } = splitStates(ctx.metriques);
  const actifs = states.filter(s => /marche/.test(String(s.valeur).toLowerCase())).length;

  const stats = $("sup-stats");
  stats.innerHTML = "";
  const card = (label, val, cls, ok) => {
    const c = el("div", "stat-card" + (cls ? " " + cls : ""));
    c.appendChild(el("div", "s-label", label));
    c.appendChild(el("div", "s-val" + (ok ? " ok" : ""), String(val)));
    return c;
  };
  stats.appendChild(card("hôtes supervisés", hotes.length, "", true));
  stats.appendChild(card("problèmes actifs", problemes.length, problemes.length ? "crit" : "", !problemes.length));
  stats.appendChild(card("conteneurs actifs", states.length ? `${actifs}/${states.length}` : "—",
    states.length && actifs < states.length ? "warn" : "", actifs === states.length && states.length));
  stats.appendChild(card("métriques", others.length, "", true));

  const body = $("sup-body");
  body.innerHTML = "";
  const panel = (title, block) => {
    if (!block) return;
    const p = el("div", "sup-panel");
    p.appendChild(el("h3", null, title));
    p.appendChild(block);
    body.appendChild(p);
  };
  panel("// problèmes", buildProblems(problemes));
  panel("// conteneurs", buildContainers(states));
  panel("// hôtes", buildHosts(hotes));
  panel("// métriques", buildMetrics(others));
  if (!body.childNodes.length) body.innerHTML = '<div class="empty-state">aucune donnée Zabbix.</div>';
}

async function pollMetrics() {
  try {
    const data = await jfetch("/debug/metriques", { cache: "no-store" });
    renderCompact(data);
    renderSupervision(data);
    $("metrics-updated").textContent = "maj " + nowLabel();
    // seconde incluse → preuve visible que le rafraîchissement a bien eu lieu
    $("sup-updated").textContent = "maj " + new Date().toLocaleTimeString("fr-FR");
    setStatus("online");
  } catch {
    setStatus("offline");
    if (!$("chat-metrics").querySelector(".metric-group"))
      $("chat-metrics").innerHTML = '<div class="empty-state">serveur injoignable.<br>Flask lancé sur :5000 ?</div>';
  }
}
$("sup-refresh").onclick = async () => {
  const b = $("sup-refresh"); const old = b.textContent;
  b.disabled = true; b.textContent = "⟳ rafraîchissement…";
  pollHealth();               // relance les checks sans attendre les métriques
  await pollMetrics();
  b.textContent = old; b.disabled = false;
};

/* =========================================================================
   ÉTAT DES SERVICES (/health/*)
   Cycle propre : le panneau est construit une fois, chaque check ne patche
   que sa carte. Aucun re-render du reste de la vue supervision.
   ========================================================================= */
const HEALTH_INTERVAL = 15_000;
const HEALTH_TIMEOUT  = 20_000;   // > au timeout serveur le plus long (/health/groq : 15 s)

// Les 4 checks sont gratuits : appels locaux, et /health/groq passe par GET /models
// (hors quota, 0 token) — voir app.py:health_groq.
const SERVICES = [
  { id: "db", label: "PostgreSQL", url: "/health/db",
    detail: d => `${d.base} · pgvector ${d.pgvector_version} · ${d.chunks_documentation} chunks` },
  { id: "ollama", label: "Ollama", url: "/health/ollama",
    detail: d => `${(d.modeles || []).length} modèle(s) · bge-m3 ${d.embed_model_present ? "présent" : "absent"}` },
  { id: "groq", label: "Groq API", url: "/health/groq",
    detail: d => `${d.modele} · ${d.modeles_dispo} modèles dispo` },
  { id: "zabbix", label: "Zabbix API", url: "/health/zabbix",
    detail: d => `${d.nb_hotes} hôte(s) supervisé(s)` },
];

const healthInFlight = {};   // garde anti-empilement, par service
let healthTimer = null;

function buildHealthRow() {
  const row = $("health-row");
  if (row.childNodes.length) return;        // déjà construit
  SERVICES.forEach(s => {
    const card = el("div", "health-card");
    card.id = "health-" + s.id;
    card.dataset.state = "checking";
    card.appendChild(el("span", "dot"));
    card.appendChild(el("span", "h-svc", escapeHtml(s.label)));
    card.appendChild(el("span", "h-badge", "…"));
    card.appendChild(el("div", "h-detail", "vérification…"));
    row.appendChild(card);
  });
}

function setHealth(id, state, badge, detail) {
  const card = $("health-" + id);
  if (!card) return;
  card.dataset.state = state;
  card.querySelector(".h-badge").textContent = badge;
  card.querySelector(".h-detail").textContent = detail;
  card.title = detail;                      // tooltip natif = ligne complète
}

// Un détail illisible (JSON inattendu) ne doit pas faire passer un service OK en KO.
// Les champs manquants ne jettent pas : ils s'interpolent en "undefined" → on filtre.
function safeDetail(s, data) {
  try {
    const txt = s.detail(data);
    return txt && !txt.includes("undefined") ? txt : "réponse OK";
  } catch { return "réponse OK"; }
}

async function checkService(s) {
  if (healthInFlight[s.id]) return;         // le tick précédent tourne encore
  healthInFlight[s.id] = true;
  const ctrl = new AbortController();
  const to = setTimeout(() => ctrl.abort(), HEALTH_TIMEOUT);
  const t0 = performance.now();
  try {
    const r = await fetch(API_BASE + s.url, { cache: "no-store", signal: ctrl.signal });
    const ms = Math.round(performance.now() - t0);
    let data = {};
    try { data = await r.json(); } catch { /* JSON malformé → data vide → KO */ }
    const statut = String(data.statut || "");
    if (r.ok && statut.startsWith("ok")) {
      setHealth(s.id, "ok", "OK", `${safeDetail(s, data)} · ${ms} ms`);
    } else if (r.ok) {
      // 200 mais dégradé — ex. Ollama répond sans bge-m3 : le RAG serait cassé.
      setHealth(s.id, "warn", "⚠", `${statut || "statut inattendu"} · ${ms} ms`);
    } else {
      setHealth(s.id, "ko", "KO", data.erreur || `HTTP ${r.status}`);
    }
  } catch (e) {
    setHealth(s.id, "ko", "KO",
      e.name === "AbortError" ? `timeout (${HEALTH_TIMEOUT / 1000} s)` : e.message);
  } finally {
    clearTimeout(to);
    healthInFlight[s.id] = false;
  }
}

// Checks indépendants : un service KO n'interrompt jamais les autres.
function pollHealth() { SERVICES.forEach(checkService); }

function startHealth() {
  buildHealthRow();
  pollHealth();                             // immédiat : pas 15 s de "…" à l'ouverture
  if (!healthTimer) healthTimer = setInterval(pollHealth, HEALTH_INTERVAL);
}
function stopHealth() {
  clearInterval(healthTimer);
  healthTimer = null;
}

/* =========================================================================
   Tableau générique (data table)
   ========================================================================= */
function renderTable(container, { columns, rows, pk, onEdit, onDelete, emptyMsg }) {
  if (!rows || !rows.length) {
    container.innerHTML = `<div class="empty-state">${escapeHtml(emptyMsg || "aucune donnée")}</div>`;
    return;
  }
  const table = el("table", "data");
  const thead = el("thead");
  const trh = el("tr");
  columns.forEach(c => trh.appendChild(el("th", null, escapeHtml(c))));
  if (onEdit || onDelete) trh.appendChild(el("th", null, ""));
  thead.appendChild(trh); table.appendChild(thead);

  const tbody = el("tbody");
  rows.forEach(row => {
    const tr = el("tr");
    columns.forEach(c => {
      const v = row[c];
      const isNum = typeof v === "number";
      const td = el("td", isNum ? "cell-mono" : "cell-text");
      let text = v == null ? "" : String(v);
      if (text.length > 240) { td.title = text; text = text.slice(0, 240) + "…"; }
      td.textContent = text;
      tr.appendChild(td);
    });
    if (onEdit || onDelete) {
      const td = el("td", "actions");
      if (onEdit) { const b = el("button", "row-btn edit", "modifier"); b.onclick = () => onEdit(row); td.appendChild(b); }
      if (onDelete) { const b = el("button", "row-btn del", "suppr"); b.onclick = () => onDelete(row); td.appendChild(b); }
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  container.innerHTML = ""; container.appendChild(table);
}

/* =========================================================================
   BASE_IA  (CRUD assistant_ai)
   ========================================================================= */
let aidbMeta = [];        // [{table, colonnes, pk, lignes, a_embedding}]
let aidbCurrent = null;

async function loadAidb() {
  try {
    const data = await jfetch("/api/aidb/tables");
    aidbMeta = data.tables || [];
    if (!aidbMeta.length) { $("aidb-table").innerHTML = '<div class="empty-state">aucune table.</div>'; return; }
    if (!aidbCurrent || !aidbMeta.find(t => t.table === aidbCurrent))
      aidbCurrent = aidbMeta[0].table;
    renderAidbTabs();
    loadAidbRows();
  } catch (e) {
    $("aidb-table").innerHTML = `<div class="empty-state">erreur : ${escapeHtml(e.message)}<br>Base assistant_ai joignable ?</div>`;
  }
}
function renderAidbTabs() {
  const tabs = $("aidb-tabs"); tabs.innerHTML = "";
  aidbMeta.forEach(t => {
    const b = el("button", "subtab" + (t.table === aidbCurrent ? " active" : ""),
      `${escapeHtml(t.table)} <span class="badge">${t.lignes}</span>`);
    b.onclick = () => { aidbCurrent = t.table; renderAidbTabs(); loadAidbRows(); };
    tabs.appendChild(b);
  });
}
function aidbTableMeta() { return aidbMeta.find(t => t.table === aidbCurrent); }

async function loadAidbRows() {
  const meta = aidbTableMeta();
  $("aidb-table").innerHTML = '<div class="empty-state">chargement…</div>';
  try {
    const data = await jfetch(`/api/aidb/${aidbCurrent}`);
    renderTable($("aidb-table"), {
      columns: data.colonnes, rows: data.lignes, pk: data.pk,
      onEdit: row => aidbForm(row),
      onDelete: row => {
        confirmModal(`Supprimer la ligne ${meta.pk}=${row[meta.pk]} ?`, async () => {
          await jfetch(`/api/aidb/${aidbCurrent}/${encodeURIComponent(row[meta.pk])}`, { method: "DELETE" });
          toast("ligne supprimée", "ok"); loadAidb();
        });
      },
    });
  } catch (e) {
    $("aidb-table").innerHTML = `<div class="empty-state">erreur : ${escapeHtml(e.message)}</div>`;
  }
}

// Colonnes auto-gérées par la base (horodatage) : jamais dans le formulaire.
const AUTO_COL = /^(cree_le|created_at|updated_at|date|timestamp)$/i;

function aidbForm(row) {
  const meta = aidbTableMeta();
  const editable = meta.colonnes.filter(c => c !== meta.pk && !AUTO_COL.test(c));
  const isEdit = !!row;
  const fields = editable.map(c => ({
    name: c, label: c,
    textarea: /contenu|reponse|question|texte|body/i.test(c),
    value: row ? (row[c] ?? "") : "",
    hint: (c === "contenu" && meta.a_embedding) ? "→ ré-embedding auto" : "",
  }));
  openModal({
    title: `${isEdit ? "Modifier" : "Ajouter"} · ${meta.table}`,
    fields,
    onSubmit: async (values) => {
      const url = isEdit
        ? `/api/aidb/${meta.table}/${encodeURIComponent(row[meta.pk])}`
        : `/api/aidb/${meta.table}`;
      await jfetch(url, {
        method: isEdit ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(values),
      });
      toast(isEdit ? "ligne modifiée" : "ligne ajoutée", "ok");
      loadAidb();
    },
  });
}
$("aidb-add").onclick = () => { if (aidbTableMeta()) aidbForm(null); };
$("aidb-refresh").onclick = loadAidb;

/* =========================================================================
   BASE_JEUX  (API json-server → base postgres)
   ========================================================================= */
let gamesCols = {};      // { games: [...cols], ... }
let gamesCurrent = "games";

async function loadGames() {
  if (!Object.keys(gamesCols).length) {
    try { const d = await jfetch("/api/games/collections"); gamesCols = d.collections || {}; }
    catch (e) { $("games-api-table").innerHTML = `<div class="empty-state">${escapeHtml(e.message)}</div>`; return; }
  }
  renderGamesTabs();
  loadGamesData();
}
function renderGamesTabs() {
  const tabs = $("games-tabs"); tabs.innerHTML = "";
  Object.keys(gamesCols).forEach(coll => {
    const b = el("button", "subtab" + (coll === gamesCurrent ? " active" : ""), escapeHtml(coll));
    b.onclick = () => { gamesCurrent = coll; renderGamesTabs(); loadGamesData(); };
    tabs.appendChild(b);
  });
}
function gamesColumns() { return gamesCols[gamesCurrent] || ["id"]; }

async function loadGamesData() {
  const cols = gamesColumns();
  $("games-api-table").innerHTML = '<div class="empty-state">chargement…</div>';
  $("games-db-table").innerHTML = '<div class="empty-state">chargement…</div>';

  // Source API (json-server) — éditable
  try {
    const api = await jfetch(`/api/games/${gamesCurrent}`);
    renderTable($("games-api-table"), {
      columns: cols, rows: api.lignes, pk: "id",
      onEdit: row => gamesForm(row, "api"),
      onDelete: row => {
        confirmModal(`Supprimer ${gamesCurrent} #${row.id} de l'API ?`, async () => {
          await jfetch(`/api/games/${gamesCurrent}/${encodeURIComponent(row.id)}`, { method: "DELETE" });
          toast("supprimé de l'API", "ok"); loadGamesData();
        });
      },
      emptyMsg: "API vide ou injoignable (:8083)",
    });
  } catch (e) {
    $("games-api-table").innerHTML = `<div class="empty-state">API injoignable : ${escapeHtml(e.message)}</div>`;
  }

  // Base postgres — CRUD direct (ajout / édition / suppression)
  try {
    const db = await jfetch(`/api/gamesdb/${gamesCurrent}`);
    renderTable($("games-db-table"), {
      columns: cols, rows: db.lignes, pk: "id",
      onEdit: row => gamesForm(row, "db"),
      onDelete: row => {
        confirmModal(`Supprimer ${gamesCurrent} #${row.id} de la base postgres ?`, async () => {
          await jfetch(`/api/gamesdb/${gamesCurrent}/${encodeURIComponent(row.id)}`, { method: "DELETE" });
          toast("supprimé de postgres", "ok"); loadGamesData();
        });
      },
      emptyMsg: "vide — ajoute une ligne ou importe depuis l'API",
    });
  } catch (e) {
    $("games-db-table").innerHTML = `<div class="empty-state">${escapeHtml(e.message)}</div>`;
  }
}

const INT_COLS = ["id", "annee", "note", "fondation", "jeux_cultes"];

// target : "api" (json-server) ou "db" (base postgres directe)
function gamesForm(row, target) {
  const isDb = target === "db";
  const cible = isDb ? "postgres" : "API";
  const base = isDb ? `/api/gamesdb/${gamesCurrent}` : `/api/games/${gamesCurrent}`;
  const cols = gamesColumns();
  const isEdit = !!row;
  const fields = cols.map(c => ({
    name: c, label: c,
    type: INT_COLS.includes(c) ? "number" : "text",
    value: row ? (row[c] ?? "") : "",
    disabled: isEdit && c === "id",
    hint: c === "id" ? (isDb ? "clé primaire (obligatoire)" : "clé primaire") : "",
  }));
  openModal({
    title: `${isEdit ? "Modifier" : "Ajouter"} · ${gamesCurrent} → ${cible}`,
    fields,
    onSubmit: async (values) => {
      const payload = {};
      cols.forEach(c => {
        let v = values[c];
        if (INT_COLS.includes(c)) v = v === "" ? "" : parseInt(v, 10);
        if (c === "id" && (v === "" || Number.isNaN(v))) return; // id vide → omis (API auto-assigne)
        payload[c] = v;
      });
      const url = isEdit ? `${base}/${encodeURIComponent(row.id)}` : base;
      await jfetch(url, {
        method: isEdit ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      toast(`${isEdit ? "modifié" : "ajouté"} (${cible})`, "ok");
      loadGamesData();
    },
  });
}
$("games-add-api").onclick = () => gamesForm(null, "api");
$("games-add-db").onclick = () => gamesForm(null, "db");
$("games-refresh").onclick = loadGamesData;
$("games-import").onclick = async () => {
  const btn = $("games-import"); const old = btn.textContent;
  btn.disabled = true; btn.textContent = "import en cours…";
  try {
    const data = await jfetch("/api/games/import", { method: "POST" });
    const r = data.resultat || {};
    const total = Object.values(r).reduce((s, x) => s + (x.importees || 0), 0);
    toast(`import terminé : ${total} lignes → postgres`, "ok");
    loadGamesData();
  } catch (e) {
    toast("import échoué : " + e.message, "err");
  } finally {
    btn.disabled = false; btn.textContent = old;
  }
};

/* =========================================================================
   Init
   ========================================================================= */
restoreChat();          // ré-affiche la conversation sauvegardée (survit au refresh)
setStatus("checking");
pollMetrics();
setInterval(pollMetrics, METRICS_INTERVAL);
$("input").focus();
