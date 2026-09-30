"use strict";

const REFRESH_MS = 2000;
const STEP_NAMES = {
  intent: "intent", rebase: "rebase", review: "review", test: "test", document: "document",
  lint: "lint", push: "push", pr: "pr", ci: "ci",
};
const MARKS = {
  attente: "", en_cours: "", correctif: "↻", decision: "?", fini: "✓", echoue: "✕", saute: "–",
};
const EXIT_LABELS = { ok: "ok", error: "erreur", cancelled: "annulé" };
const SELECTION_LABELS = { user: "choisie", auto_fix: "auto-fix", user_declined: "refusée" };
const COPY_ICON =
  '<svg viewBox="0 0 24 24" width="12" height="12" aria-hidden="true">'
  + '<rect x="9" y="9" width="13" height="13" rx="2" fill="none" stroke="currentColor" stroke-width="2"/>'
  + '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"'
  + ' fill="none" stroke="currentColor" stroke-width="2"/></svg>';
const FEEDBACK_MS = 1200;

let data = { tabs: [] };
let lastBody = "";
let selectedTab = null;
const selectedNode = {}; // id d'onglet → nom du noeud choisi à la main
const openState = {}; // clé de <details> → ouvert ou fermé
let clockSkew = 0;

const $ = (id) => document.getElementById(id);
const esc = (text) =>
  String(text ?? "").replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const nowS = () => Date.now() / 1000 + clockSkew;

function span(seconds) {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min ${String(s % 60).padStart(2, "0")} s`;
  return `${Math.floor(s / 3600)} h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")} min`;
}

const since = (ts, prefix = "") =>
  ts ? `<span data-since="${ts}" data-prefix="${esc(prefix)}"></span>` : "";

function tokens(n) {
  if (n == null) return "—";
  if (n >= 1e9) return `${(n / 1e9).toFixed(1).replace(".", ",")} G`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1).replace(".", ",")} M`;
  if (n >= 1e4) return `${(n / 1e3).toFixed(0)} k`;
  return n.toLocaleString("fr-FR");
}

const clock = (ts) =>
  new Date(ts * 1000).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });

function restartText(restart) {
  return `repris depuis ${esc(STEP_NAMES[restart.step] || restart.step)} à ${clock(restart.at)}`
    + ` (renvoyé par ${esc(STEP_NAMES[restart.after] || restart.after)})`;
}

function restartReason(restart) {
  const reason = restart.reason;
  if (!reason) return "";
  return reason.label
    ? `<span title="${esc(reason.raw)}">${esc(reason.label)}</span>`
    : `<code>${esc(reason.raw)}</code>`;
}

function baseName(path) {
  return path ? path.split("/").filter(Boolean).pop() : null;
}

function agentText(agent) {
  return [agent.tool, agent.model, agent.effort && `effort ${agent.effort}`]
    .filter(Boolean).map(esc).join(" · ");
}

function sha(full) {
  return `<code>${esc((full || "").slice(0, 10) || "—")}</code>`;
}

function nodeTime(node) {
  if (node.live) return since(node.started_at);
  if (node.duration_ms != null) return esc(span(node.duration_ms / 1000));
  return "";
}

function findingLine(f) {
  const bits = [
    `<b>${esc(f.title || f.id || "finding")}</b>`,
    f.severity ? `<span class="sev s-${esc(f.severity)}">${esc(f.severity)}</span>` : "",
    f.file ? `<span class="file" title="${esc(f.file)}${f.line ? ":" + f.line : ""}">${esc(baseName(f.file))}${f.line ? `:${f.line}` : ""}</span>` : "",
  ].filter(Boolean);
  return `<li title="${esc(f.description || "")}">${bits.join(" ")}</li>`;
}

function renderNode(node, index, chosen) {
  const used = node.agents
    .map((a) => `<span>${esc(a.tool)}</span>${a.model ? `<span>${esc(a.model)}</span>` : ""}`).join("");
  const findings = node.findings
    ? `<span class="findings">${node.findings} finding${node.findings > 1 ? "s" : ""}</span>` : "";
  const stuck = node.live && node.live.stuck ? `<span class="stuck" title="${esc(node.live.stuck.join(" · "))}">⚠</span>` : "";
  return `
    <button class="node s-${node.state}${node.redo ? " redo" : ""}${node.name === chosen ? " chosen" : ""}"
            data-node="${esc(node.name)}" aria-pressed="${node.name === chosen}">
      <span class="disc">${MARKS[node.state] || index + 1}</span>
      <span class="name">${esc(STEP_NAMES[node.name] || node.name)}${stuck}</span>
      <span class="state">${esc(node.state_label)}</span>
      <span class="time">${nodeTime(node)}</span>
      ${findings}
      <span class="used">${used}</span>
    </button>`;
}

function renderRounds(node) {
  const rows = node.round_list.filter((r) => r.duration_ms != null
    || r.selection_source || r.fix_summary || r.findings_count || r.selected.length);
  if (!rows.length) return "";
  const items = rows.map((r) => {
    const bits = [
      r.duration_ms != null ? esc(span(r.duration_ms / 1000)) : "",
      r.selection_source
        ? `sélection ${esc(SELECTION_LABELS[r.selection_source] || r.selection_source)}`
          + (r.selected.length ? ` (${r.selected.length})` : "")
        : "",
      r.findings_count ? `${r.findings_count} finding${r.findings_count > 1 ? "s" : ""}` : "",
      r.fix_summary ? `→ ${esc(r.fix_summary)}` : "",
    ].filter(Boolean);
    const chosen = r.selected.map((f) => esc(f.title || f.id)).join(", ");
    return `<li><b>tour ${esc(r.round)} · ${esc(r.trigger_label || r.trigger || "")}</b> ${bits.join(" · ")
      }${chosen ? `<div class="dim">${chosen}</div>` : ""}</li>`;
  });
  return `<div class="sub"><h3>Tours</h3><ul>${items.join("")}</ul></div>`;
}

function renderDetail(node, column) {
  const rows = [];
  const live = node.live;
  if (live) {
    rows.push(["Agent", live.agent
      ? `<strong>${agentText(live.agent)}</strong>
         <em>outil lu dans ${live.agent.tool_source === "log" ? "le log de l'étape" : "la config"}${
           live.agent.model_source ? `, modèle : ${esc(live.agent.model_source)}` : ""}</em>`
      : "aucun agent lancé sur cette étape en ce moment"]);
    rows.push(["Tour", `${esc(live.round_label)} ${since(live.round_started_at, "depuis ")}`]);
    if (live.waiting_since) rows.push(["Décision", since(live.waiting_since, "attendue depuis ")]);
    if (live.stuck) {
      rows.push(["Bloqué ?", `<span class="bad">${live.stuck.map(esc).join(" · ")}</span>`]);
    }
    rows.push(["Dernière activité", live.last_activity
      ? `${esc(live.last_activity)} ${since(live.last_activity_at, "il y a ")}` : "—"]);
    if (live.last_log_line) rows.push(["Dernière ligne du log", `<code>${esc(live.last_log_line)}</code>`]);
    rows.push(["Temps écoulé", since(node.started_at)]);
  } else {
    rows.push(["État", esc(node.state_label)]);
    if (node.duration_ms != null) rows.push(["Durée", esc(span(node.duration_ms / 1000))]);
    if (node.rounds > 1) rows.push(["Tours", String(node.rounds)]);
  }
  if (node.exit_code != null) rows.push(["Code de sortie", String(node.exit_code)]);
  if (node.override_reason) rows.push(["Passage forcé", esc(node.override_reason)]);
  if (node.redo) {
    rows.push(["Reprise", "déjà passée au passage précédent, à refaire depuis que le run est reparti en arrière"]);
  }
  if (node.agents.length) {
    rows.push(["Agents utilisés", node.agents
      .map((a) => `${agentText(a)} <em>× ${a.calls}</em>`).join("<br>")]);
  }
  if (node.earlier_agents.length) {
    rows.push(["Passages précédents", `<span class="dim">${node.earlier_agents
      .map((a) => `${agentText(a)} <em>× ${a.calls}</em>`).join("<br>")}</span>`]);
  }
  let html = `
    <section class="detail s-${node.state} col-${column}">
      <h2>${esc(STEP_NAMES[node.name] || node.name)} <small>${esc(node.state_label)}</small></h2>
      <dl>${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>`;
  if (node.finding_list.length) {
    html += `<div class="sub"><h3>Findings (${node.finding_list.length})</h3>
      <ul>${node.finding_list.map(findingLine).join("")}</ul></div>`;
  }
  html += renderRounds(node);
  if (node.note) html += `<p class="note">${esc(node.note)}</p>`;
  return html + `</section>`;
}

const SESSION_COLUMNS = 15;

function passRow(restart, note) {
  const reason = restartReason(restart);
  return `<tr class="pass-sep"><td colspan="${SESSION_COLUMNS}">↺ <b>passage ${restart.pass}</b> · ${
    restartText(restart)}${reason ? ` · ${reason}` : ""}${note ? ` <em>${note}</em>` : ""}</td></tr>`;
}

function sessionsTable(sessions, tab) {
  const t = sessions.totals;
  if (!t.count) return `<p class="dim">Aucun appel d'agent enregistré pour ce run.</p>`;
  const restarts = Object.fromEntries(tab.restarts.map((r) => [r.pass, r]));
  let shown = 1;
  const body = sessions.rows.map((r) => {
    const sep = r.pass > shown && restarts[r.pass] ? passRow(restarts[r.pass]) : "";
    shown = r.pass;
    const classes = [r.exit && r.exit !== "ok" ? "bad-row" : "", r.pass < tab.pass ? "old-pass" : ""]
      .filter(Boolean).join(" ");
    return `${sep}
    <tr${classes ? ` class="${classes}"` : ""}>
      <td>${esc(STEP_NAMES[r.step] || r.step)}</td>
      <td>${r.round ?? ""}</td>
      <td>${esc(r.purpose || "")}</td>
      <td>${esc(r.agent)}</td>
      <td>${r.model ? esc(r.model) : ""}</td>
      <td>${r.effort ? esc(r.effort) : ""}</td>
      <td>${r.duration_ms != null ? esc(span(r.duration_ms / 1000)) : ""}</td>
      <td>${tokens(r.tokens.input)}</td>
      <td>${tokens(r.tokens.output)}</td>
      <td>${tokens(r.tokens.cache_read)}</td>
      <td>${tokens(r.tokens.cache_creation)}</td>
      <td>${tokens(r.tokens.reasoning)}</td>
      <td>${tokens(r.roundtrips)}</td>
      <td title="${esc(Object.entries(r.tools).map(([k, n]) => `${k} ${n}`).join(", ") || "")}">${
        tokens(r.tools_total)}</td>
      <td>${r.exit ? esc(EXIT_LABELS[r.exit] || r.exit) : ""}${
        r.failure ? ` · ${esc(r.failure)}` : ""}${r.fallback ? `<em>fallback ${esc(r.fallback)}</em>` : ""}</td>
    </tr>`;
  }).join("") + (tab.pass > shown && restarts[tab.pass]
    ? passRow(restarts[tab.pass], "aucun appel fini pour l'instant : un appel n'est enregistré qu'à sa fin")
    : "");
  return `
    <table class="sessions">
      <thead><tr>
        <th>étape</th><th>tour</th><th>objet</th><th>agent</th><th>modèle</th>
        <th title="lu dans la config actuelle, vide si elle a changé de modèle depuis">effort</th><th>durée</th>
        <th>entrée</th><th>sortie</th><th>cache lu</th><th>cache écrit</th><th>raisonnement</th>
        <th>allers-retours</th><th title="appels d'outils">outils</th><th>sortie</th>
      </tr></thead>
      <tbody>${body}</tbody>
      <tfoot><tr>
        <td colspan="6">total ${t.count} appel${t.count > 1 ? "s" : ""}${
          tab.pass > 1 ? ` sur ${tab.pass} passages` : ""}</td>
        <td>${esc(span(t.duration_ms / 1000))}</td>
        <td>${tokens(t.tokens.input)}</td>
        <td>${tokens(t.tokens.output)}</td>
        <td>${tokens(t.tokens.cache_read)}</td>
        <td>${tokens(t.tokens.cache_creation)}</td>
        <td>${tokens(t.tokens.reasoning)}</td>
        <td>${tokens(t.roundtrips)}</td>
        <td title="${esc(Object.entries(t.tools).map(([k, n]) => `${k} ${n}`).join(", ") || "")}">${
          tokens(Object.values(t.tools).reduce((a, b) => a + b, 0))}</td>
        <td></td>
      </tr></tfoot>
    </table>`;
}

function renderRunDetails(tab) {
  const key = tab.id;
  const wait = tab.waiting_since
    ? `<div class="askline">${since(tab.waiting_since, "attend une décision depuis ")}</div>` : "";
  const times = tab.total_s != null
    ? `<p class="times">Attente ${esc(span(tab.wait_s || 0))} · actif ${esc(span(tab.active_s || 0))}
       <span class="dim">(total ${esc(span(tab.total_s))})</span></p>` : "";
  const log = tab.log_lines && tab.log_lines.length
    ? `<details data-open-key="log:${esc(key)}">
         <summary>Log de l'étape ${esc(STEP_NAMES[tab.focus] || tab.focus)}
           (${tab.log_lines.length} dernières lignes)</summary>
         <pre class="log">${tab.log_lines.map((l) => esc(l)).join("\n")}</pre>
       </details>` : "";
  return `
    <details class="rundetails" data-open-key="run:${esc(key)}">
      <summary>Détails du run — intention, agents, coûts${wait ? " · ⏳" : ""}</summary>
      <div class="inner">
        ${wait}
        ${tab.intent
          ? `<div class="sub"><h3>Intention <em>source ${esc(tab.intent_source || "?")}</em></h3>
             <div class="intent">${esc(tab.intent)}</div></div>`
          : ""}
        <div class="sub"><h3>Branchement</h3>
          <dl class="inline">
            <dt>base</dt><dd>${sha(tab.base_sha)}</dd>
            <dt>tête</dt><dd>${sha(tab.head_sha)}</dd>
            <dt>arbre de travail</dt><dd><code>${esc(tab.worktree_dir || "—")}</code></dd>
            <dt>no-mistakes</dt><dd>${esc(tab.no_mistakes_version || "—")}</dd>
          </dl>
        </div>
        ${times}
        <div class="sub"><h3>Appels d'agents</h3>${sessionsTable(tab.sessions, tab)}</div>
        ${log}
      </div>
    </details>`;
}

function renderTabs() {
  $("tabs").innerHTML = data.tabs.map((tab, i) => {
    const wait = tab.waiting_since
      ? `<span class="wait" title="attend une décision" data-since="${tab.waiting_since}" data-prefix="⏳ "></span>`
      : "";
    const stuck = tab.stuck.length ? `<span class="stuck" title="étape sans signe de vie : ${esc(tab.stuck.join(", "))}">⚠</span>` : "";
    const again = tab.pass > 1
      ? `<span class="again" title="passage ${tab.pass} : le run est reparti en arrière">↺${tab.pass}</span>` : "";
    return `
    <button role="tab" class="tab r-${tab.status}" data-tab="${esc(tab.id)}"
            aria-selected="${tab.id === selectedTab}" title="${esc(tab.project)} · ${esc(tab.branch)}">
      <span class="dot"></span>
      <span class="key">${i + 1}</span>
      <span class="label"><b>${esc(tab.project)}</b> ${esc(tab.branch)}</span>
      ${again}${stuck}${wait}
      ${tab.closes_at ? `<span class="closing" data-until="${tab.closes_at}"></span>` : ""}
    </button>`;
  }).join("");
}

function prTag(tab) {
  if (!tab.pr_url) return "";
  const label = `PR ${tab.pr_state_label ? esc(tab.pr_state_label) : ""}`.trim();
  return `<a class="pr" href="${esc(tab.pr_url)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
}

function ciTag(tab) {
  if (!tab.ci_ready_at) return "";
  return `<span class="ci" title="${new Date(tab.ci_ready_at * 1000).toLocaleString("fr-FR")}">CI prête</span>`;
}

function restartLine(tab) {
  const last = tab.restarts[tab.restarts.length - 1];
  if (!last) return "";
  const reason = restartReason(last);
  return `<p class="restartline">↺ <b>passage ${tab.pass}</b> : ${restartText(last)}${
    reason ? `<br><span class="dim">${reason}</span>` : ""}</p>`;
}

function renderView() {
  const tab = data.tabs.find((t) => t.id === selectedTab);
  if (!tab) {
    $("view").innerHTML = `
      <div class="empty">
        <p>Aucun run no-mistakes en cours sur cette machine.</p>
        <p class="dim">${esc(data.error || "Un onglet s'ouvrira dès qu'un run démarre.")}</p>
      </div>`;
    return;
  }
  const chosen = selectedNode[tab.id] || tab.focus;
  const index = Math.max(0, tab.nodes.findIndex((n) => n.name === chosen));
  const node = tab.nodes[index];
  const waiting = tab.waiting_since
    ? `<span class="askline">⏳ ${since(tab.waiting_since, "attend depuis ")}</span>` : "";
  const stuck = tab.stuck.length
    ? `<span class="bad">⚠ sans signe de vie : ${esc(tab.stuck.join(", "))}</span>` : "";
  $("view").innerHTML = `
    <div class="runhead r-${tab.status}">
      <h1>${esc(tab.project)} <span>${esc(tab.branch)}</span><button class="copy"
          data-copy="${esc(tab.branch)}" title="Copier le nom de la branche"
          aria-label="Copier la branche ${esc(tab.branch)}">${COPY_ICON}</button></h1>
      <p><span class="badge">${esc(tab.status_label)}</span>
         ${prTag(tab)} ${ciTag(tab)}
         ${tab.finished_at
           ? `durée ${esc(span(tab.finished_at - tab.started_at))}
              <span class="closing" data-until="${tab.closes_at}" data-long="1"></span>`
           : since(tab.started_at, "lancé il y a ")}
         ${waiting}
      </p>
      ${stuck ? `<p class="stuckline">${stuck}</p>` : ""}
      ${restartLine(tab)}
      ${tab.error ? `<p class="error">${esc(tab.error)}</p>` : ""}
    </div>
    <div class="pipeline">${tab.nodes.map((n, i) => renderNode(n, i, node && node.name)).join("")}</div>
    ${node ? renderDetail(node, index + 1) : ""}
    ${renderRunDetails(tab)}`;
  for (const box of document.querySelectorAll("details[data-open-key]")) {
    const key = box.dataset.openKey;
    box.open = openState[key] === true;
    box.addEventListener("toggle", () => { openState[key] = box.open; });
  }
}

function flash(button, mark, state) {
  button.classList.add(state);
  button.textContent = mark;
  setTimeout(() => {
    // Un rafraîchissement a pu remplacer le bouton entre-temps.
    if (button.isConnected) {
      button.classList.remove(state);
      button.innerHTML = COPY_ICON;
    }
  }, FEEDBACK_MS);
}

function copyBranch(button) {
  navigator.clipboard.writeText(button.dataset.copy).then(
    () => flash(button, "✓", "copied"),
    () => flash(button, "✕", "failed"),
  );
}

function tick() {
  const now = nowS();
  for (const el of document.querySelectorAll("[data-since]")) {
    el.textContent = el.dataset.prefix + span(now - Number(el.dataset.since));
  }
  for (const el of document.querySelectorAll("[data-until]")) {
    const left = Math.max(0, Math.round(Number(el.dataset.until) - now));
    el.textContent = el.dataset.long ? `· l'onglet se ferme dans ${left} s` : `${left} s`;
  }
}

function render() {
  if (!data.tabs.some((t) => t.id === selectedTab)) {
    selectedTab = data.tabs.length ? data.tabs[0].id : null;
  }
  renderTabs();
  renderView();
  tick();
}

function selectTab(id) {
  selectedTab = id;
  render();
}

async function refresh() {
  try {
    const reply = await fetch("/api/state", { cache: "no-store" });
    const next = await reply.json();
    clockSkew = next.now - Date.now() / 1000;
    const known = new Set(data.tabs.map((t) => t.id));
    const gone = selectedTab && !next.tabs.some((t) => t.id === selectedTab);
    if (gone) {
      // L'onglet affiché vient de se fermer : on passe à son voisin.
      const at = data.tabs.findIndex((t) => t.id === selectedTab);
      const neighbour = data.tabs.slice(at + 1).concat(data.tabs.slice(0, at).reverse())
        .find((t) => next.tabs.some((n) => n.id === t.id));
      selectedTab = neighbour ? neighbour.id : null;
    }
    const body = JSON.stringify({ ...next, now: 0 });
    data = next;
    if (body !== lastBody || gone) {
      lastBody = body;
      render();
      for (const tab of next.tabs) {
        if (known.size && !known.has(tab.id)) {
          const el = document.querySelector(`[data-tab="${CSS.escape(tab.id)}"]`);
          if (el) el.classList.add("fresh");
        }
      }
    }
    const n = next.tabs.length;
    $("status").textContent = `${n} run${n > 1 ? "s" : ""} · lecture seule`;
    $("status").className = next.error ? "bad" : "";
  } catch (err) {
    if (quitting) return;
    $("status").textContent = "serveur local injoignable";
    $("status").className = "bad";
  }
}

let timer = null;
let busy = false;
let quitting = false;

function loop() {
  clearTimeout(timer);
  if (document.hidden || busy || quitting) return; // fenêtre cachée : aucune relecture
  busy = true;
  refresh().finally(() => {
    busy = false;
    timer = setTimeout(loop, REFRESH_MS);
  });
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) loop();
});

$("tabs").addEventListener("click", (event) => {
  const tab = event.target.closest("[data-tab]");
  if (tab) selectTab(tab.dataset.tab);
});

$("view").addEventListener("click", (event) => {
  const copy = event.target.closest("[data-copy]");
  if (copy) {
    copyBranch(copy);
    return;
  }
  const node = event.target.closest("[data-node]");
  if (node && selectedTab) {
    selectedNode[selectedTab] = node.dataset.node;
    render();
  }
});

document.addEventListener("keydown", (event) => {
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  const at = data.tabs.findIndex((t) => t.id === selectedTab);
  const tab = data.tabs[at];
  if (event.key === "Escape" && tab) {
    delete selectedNode[tab.id];
    render();
  } else if ((event.key === "ArrowLeft" || event.key === "ArrowRight") && tab) {
    const step = event.key === "ArrowRight" ? 1 : -1;
    if (event.shiftKey) {
      const current = selectedNode[tab.id] || tab.focus;
      const i = tab.nodes.findIndex((n) => n.name === current);
      const next = tab.nodes[Math.min(tab.nodes.length - 1, Math.max(0, i + step))];
      if (next) selectedNode[tab.id] = next.name;
      render();
    } else {
      selectTab(data.tabs[(at + step + data.tabs.length) % data.tabs.length].id);
    }
    event.preventDefault();
  } else if (/^[1-9]$/.test(event.key) && data.tabs[Number(event.key) - 1]) {
    selectTab(data.tabs[Number(event.key) - 1].id);
  } else if (event.key === "q" || event.key === "Q") {
    quit();
  }
});

function quit() {
  if (quitting) return;
  quitting = true; // plus de relecture : le serveur s'arrête
  clearTimeout(timer);
  $("status").textContent = "arrêt…";
  fetch("/api/quit", { method: "POST" }).then(
    () => { $("status").textContent = "arrêté, retour au terminal"; window.close(); },
    () => {
      quitting = false;
      $("status").textContent = "arrêt impossible";
      $("status").className = "bad";
      loop();
    },
  );
}

setInterval(tick, 1000);
loop();
