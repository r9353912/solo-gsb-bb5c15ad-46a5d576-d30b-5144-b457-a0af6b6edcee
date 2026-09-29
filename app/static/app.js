/* 事件证据链审计台 —— 原生前端（无框架、无构建） */
"use strict";

const state = {
  result: null,
  overrides: {},          // 未保存的偏差假设 { sourceId: {clock_offset,...} }
  sourceColors: {},
};

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html !== undefined) n.innerHTML = html;
  return n;
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const fmtNum = (n) => {
  n = Number(n);
  if (!isFinite(n)) return "-";
  if (Math.abs(n) >= 100 || n === 0) return n.toFixed(1);
  return n.toFixed(3).replace(/0+$/, "").replace(/\.$/, "");
};

const SOURCE_FIELDS = [
  ["clock_offset", "时钟偏差 offset(s)", "本地时钟快多少秒"],
  ["drift_ppm", "漂移 drift(ppm)", "相对时间的线性漂移"],
  ["uncertainty", "不确定度 ±(s)", "时钟可信半径"],
  ["reliability", "可靠度 0~1", "来源整体可信度"],
];

/* ------------------------------------------------------------------ 数据 */

async function recompute() {
  const body = {
    overrides: state.overrides,
    source_id: $("#f-source").value || null,
    event_type: $("#f-type").value || null,
    q: $("#f-q").value.trim() || null,
    only_conflicts: $("#f-conflicts").checked,
    only_ambiguous: $("#f-ambiguous").checked,
    lo: null, hi: null,
  };
  const res = await fetch("/api/recompute", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    toast("重算失败: " + (await res.text()), "error");
    return;
  }
  state.result = await res.json();
  render();
}

async function loadTypes() {
  const res = await fetch("/api/event-types");
  const data = await res.json();
  const sel = $("#f-type");
  const cur = sel.value;
  sel.innerHTML = '<option value="">全部类型</option>' +
    data.types.map((t) => `<option value="${esc(t.name)}">${esc(t.name)} (${t.count})</option>`).join("");
  sel.value = cur;
}

/* ------------------------------------------------------------------ 渲染 */

function colorFor(sourceId) {
  if (!state.sourceColors[sourceId]) {
    let h = 0;
    for (const c of sourceId) h = (h * 31 + c.charCodeAt(0)) % 360;
    state.sourceColors[sourceId] = `hsl(${h}, 68%, 62%)`;
  }
  return state.sourceColors[sourceId];
}

function render() {
  const r = state.result;
  if (!r) return;
  renderStats(r.stats);
  renderSources(r.sources);
  renderTimeline(r.events);
  renderConflicts(r.conflicts);
  renderAmbiguities(r.ambiguity_groups);
  renderEdges(r.edges);
  // 来源筛选选项（用全量来源，不受类型筛选影响）
  const sel = $("#f-source");
  const cur = sel.value;
  sel.innerHTML = '<option value="">全部来源</option>' + r.sources.map(
    (s) => `<option value="${esc(s.id)}">${esc(s.name || s.id)} (${s.event_count})</option>`).join("");
  sel.value = cur;
}

function renderStats(s) {
  $("#stats").innerHTML = `
    <span class="stat">事件 <b>${s.event_count}</b></span>
    <span class="stat">来源 <b>${s.source_count}</b></span>
    <span class="stat">约束 <b>${s.edge_count}</b></span>
    <span class="stat ${s.impossible_edges ? "bad" : ""}">不可能边 <b>${s.impossible_edges}</b></span>
    <span class="stat ${s.conflict_count ? "bad" : ""}">冲突链 <b>${s.conflict_count}</b></span>
    <span class="stat ${s.ambiguity_group_count ? "warn" : ""}">歧义组 <b>${s.ambiguity_group_count}</b></span>
    <span class="stat">跨度 <b>${fmtNum(s.timeline_span)}s</b></span>`;
  const cc = $("#conflict-count"), ac = $("#ambig-count"), ec = $("#edge-count");
  cc.textContent = s.conflict_count;
  ac.textContent = s.ambiguity_group_count;
  ec.textContent = s.edge_count;
}

function renderSources(sources) {
  const box = $("#sources");
  box.innerHTML = "";
  if (!sources.length) {
    box.appendChild(el("div", "empty ok-state", "暂无数据<br>导入 JSON 事件文件开始审计"));
    return;
  }
  for (const s of sources) {
    const eff = s.effective, stored = s.stored;
    const ov = state.overrides[s.id] || {};
    const dirty = Object.keys(ov).length > 0;
    const card = el("div", "source-card" + (dirty ? " dirty" : ""));
    card.innerHTML = `
      <div class="source-title">
        <span class="source-dot" style="background:${colorFor(s.id)}"></span>
        <span class="source-name">${esc(s.name || s.id)}</span>
        ${dirty ? '<span class="dirty-flag">● 未保存调整</span>' : ""}
      </div>
      <div class="source-meta">
        <span class="source-id">${esc(s.id)}</span> · ${esc(s.kind)} · ${s.event_count} 条事件
      </div>`;
    const grid = el("div", "assump-grid");
    for (const [key, label, title] of SOURCE_FIELDS) {
      const val = ov[key] !== undefined ? ov[key] : eff[key];
      const changed = ov[key] !== undefined && Math.abs(Number(ov[key]) - Number(stored[key])) > 1e-9;
      const f = el("div", "field");
      f.innerHTML = `<label title="${title}">${label}</label>`;
      const input = el("input");
      input.type = "number"; input.step = key === "reliability" ? "0.05" : "any";
      input.value = val;
      if (changed) input.classList.add("changed");
      input.addEventListener("input", () => {
        const v = parseFloat(input.value);
        state.overrides[s.id] = state.overrides[s.id] || {};
        if (isNaN(v)) delete state.overrides[s.id][key];
        else state.overrides[s.id][key] = v;
        renderSources(state.result.sources);
        scheduleRecompute();
      });
      f.appendChild(input);
      grid.appendChild(f);
    }
    card.appendChild(grid);
    box.appendChild(card);
  }
}

function renderTimeline(events) {
  const box = $("#timeline");
  box.innerHTML = "";
  if (!events.length) {
    box.appendChild(el("div", "empty", "没有符合筛选条件的事件"));
    $("#scale-lo").textContent = "";
    $("#scale-hi").textContent = "";
    return;
  }
  const lo0 = Math.min(...events.map((e) => e.lo));
  const hi0 = Math.max(...events.map((e) => e.hi));
  const span = Math.max(hi0 - lo0, 1e-3);
  const pct = (t) => ((t - lo0) / span) * 100;
  $("#scale-lo").textContent = events[0].lo_text.slice(11, 23);
  $("#scale-hi").textContent = events[events.length - 1].hi_text.slice(11, 23);

  const conflictEvents = new Set();
  for (const c of state.result.conflicts) c.events.forEach((id) => conflictEvents.add(id));

  for (const e of events) {
    const inConflict = conflictEvents.has(e.id);
    const item = el("div", "tl-item" + (inConflict ? " in-conflict" : "")
      + (e.ambiguity_group ? " ambiguous" : ""));
    item.id = "tl-" + cssEscape(e.id);

    const tags = [
      `<span class="tag src" style="border-color:${colorFor(e.source_id)}55;color:${colorFor(e.source_id)}">${esc(e.source_name || e.source_id)}</span>`,
      `<span class="tag type">${esc(e.event_type)}</span>`,
      e.actor ? `<span class="tag actor">👤 ${esc(e.actor)}</span>` : "",
      `<span class="tag id">${esc(e.id)}</span>`,
      inConflict ? eventConflictTags(e.id) : "",
      e.ambiguity_group ? `<span class="tag amb" data-jump="amb">⚠ ${e.ambiguity_group}</span>` : "",
    ].filter(Boolean).join("");

    const confCls = e.confidence >= 0.75 ? "conf-hi" : e.confidence >= 0.5 ? "conf-mid" : "conf-lo";
    const color = colorFor(e.source_id);
    item.innerHTML = `
      <div class="tl-time">
        <span class="est">${esc(e.est_text.slice(11, 23))}</span>
        <span class="local">本地 ${esc(e.local_ts_text.slice(11, 23))}</span>
      </div>
      <div class="tl-body">
        <div class="tl-line1">
          <span class="tl-summary">${esc(e.summary)}</span>
          <span class="tl-tags">${tags}</span>
        </div>
        <div class="bar-wrap">
          <div class="bar">
            <span class="interval" style="left:${pct(e.lo).toFixed(2)}%;width:${Math.max(pct(e.hi) - pct(e.lo), .6).toFixed(2)}%;background:${color}"></span>
            <span class="point" style="left:${pct(e.est).toFixed(2)}%;background:${color}"></span>
          </div>
          <span class="conf-meter ${confCls}" title="综合可信度（时间精度 60% + 来源可靠度 40%）">
            可信度 <b>${(e.confidence * 100).toFixed(0)}%</b> · ±${fmtNum(e.radius)}s
          </span>
        </div>
      </div>`;
    box.appendChild(item);
  }
  box.querySelectorAll(".tag.conf").forEach((t) =>
    t.addEventListener("click", () => jumpToConflict(t.dataset.cid)));
  box.querySelectorAll(".tag.amb").forEach((t) =>
    t.addEventListener("click", () => {
      const g = state.result.ambiguity_groups.find((x) => x.id === e_ambId(t));
      if (g) toast(g.explanation, "info");
    }));
}

function e_ambId(tag) { return tag.textContent.trim().replace(/^⚠\s*/, ""); }

function eventConflictTags(eventId) {
  return state.result.conflicts
    .filter((c) => c.events.includes(eventId))
    .map((c) => `<span class="tag conf" data-cid="${c.id}">⛓ ${c.id}</span>`)
    .join("");
}

function renderConflicts(conflicts) {
  const box = $("#conflicts");
  box.innerHTML = "";
  if (!conflicts.length) {
    box.appendChild(el("div", "empty ok-state", "✓ 当前时钟假设下没有冲突链"));
    return;
  }
  for (const c of conflicts) {
    const f = el("div", "finding");
    f.id = "cf-" + c.id;
    const kindText = c.kind === "causal_cycle" ? "因果环" : "不可能顺序";
    // edge_keys 已按链路顺序排列（闭合矛盾时最后一条是不可能边）
    let chainHtml = "";
    const keys = c.edge_keys || [];
    if (keys.length) {
      keys.forEach((k, i) => {
        if (i === 0) {
          chainHtml += `<span class="chain-node" data-eid="${esc(k.from)}">${esc(shortId(k.from))}</span>`;
        }
        const bad = k.status === "impossible";
        chainHtml += `<span class="chain-arrow ${bad ? "bad" : ""}" title="${esc(k.kind)}">→</span>`;
        chainHtml += `<span class="chain-node ${bad ? "bad" : ""}" data-eid="${esc(k.to)}">${esc(shortId(k.to))}</span>`;
      });
    } else {
      chainHtml = c.events.map((id) =>
        `<span class="chain-node" data-eid="${esc(id)}">${esc(shortId(id))}</span>`)
        .join('<span class="chain-arrow">→</span>');
    }
    f.innerHTML = `
      <div class="finding-head">
        <span class="finding-id">⛓ ${c.id}</span>
        <span class="sev">${kindText}</span>
      </div>
      <div class="finding-explain">${esc(c.explanation)}</div>
      <div class="chain">${chainHtml}</div>
      <div class="finding-jump"><button class="btn tiny" data-jump="first">🔍 在时间线中定位</button></div>`;
    f.querySelector("[data-jump=first]").addEventListener("click", () => jumpToConflict(c.id));
    f.querySelectorAll(".chain-node").forEach((n) =>
      n.addEventListener("click", () => focusEvent(n.dataset.eid)));
    box.appendChild(f);
  }
}

function renderAmbiguities(groups) {
  const box = $("#ambiguities");
  box.innerHTML = "";
  if (!groups.length) {
    box.appendChild(el("div", "empty", "无时间歧义"));
    return;
  }
  for (const g of groups) {
    const f = el("div", "finding ambig");
    f.innerHTML = `
      <div class="finding-head"><span class="finding-id">⚠ ${g.id}</span>
        <span class="sev" style="background:rgba(232,163,61,.18);color:#f0c273">${g.events.length} 事件 / ${g.source_count} 来源</span></div>
      <div class="finding-explain">${esc(g.explanation)}</div>
      <div class="chain">${g.events.map((id) =>
        `<span class="chain-node" data-eid="${esc(id)}">${esc(shortId(id))}</span>`).join("")}</div>`;
    f.querySelectorAll(".chain-node").forEach((n) =>
      n.addEventListener("click", () => focusEvent(n.dataset.eid)));
    box.appendChild(f);
  }
}

function renderEdges(edges) {
  const box = $("#edges");
  box.innerHTML = "";
  if (!edges.length) {
    box.appendChild(el("div", "empty", "无约束"));
    return;
  }
  const statusText = { ok: "支持", boundary: "脆弱", impossible: "不可能" };
  for (const e of edges) {
    const row = el("div", "edge-row");
    row.title = e.explanation;
    row.innerHTML = `
      <span class="edge-status ${e.status}" title="${statusText[e.status]}"></span>
      <div class="edge-main">
        <div class="edge-nodes">${esc(shortId(e.from))} → ${esc(shortId(e.to))}
          ${e.conflict_ids.map((c) => `<span class="tag conf" data-cid="${c}" style="margin-left:4px">⛓ ${c}</span>`).join("")}</div>
        <div class="edge-kind">${esc(e.kind)}${e.note ? " · " + esc(e.note) : ""}</div>
      </div>
      <div class="edge-gap ${e.status}">
        [${e.gap_lo_text}, ${e.gap_hi_text}]
      </div>
      ${e.kind !== "source_order" ? `<button class="edge-del" title="删除该约束">✕</button>` : ""}`;
    const del = row.querySelector(".edge-del");
    if (del) del.addEventListener("click", async () => {
      await fetch("/api/constraints/" + e.id, { method: "DELETE" });
      await Promise.all([loadTypes(), recompute()]);
    });
    row.querySelectorAll(".tag.conf").forEach((t) =>
      t.addEventListener("click", () => jumpToConflict(t.dataset.cid)));
    box.appendChild(row);
  }
}

/* ------------------------------------------------------------ 交互动作 */

function shortId(id) {
  return id.length > 10 ? id.slice(0, 8) + "…" : id;
}
function cssEscape(s) {
  return window.CSS && CSS.escape ? CSS.escape(s) : s.replace(/[^a-zA-Z0-9_-]/g, "\\$&");
}

function focusEvent(eid) {
  // 若被筛选隐藏，先清筛选
  let node = document.getElementById("tl-" + cssEscape(eid));
  if (!node) {
    clearFilters();
    state.result && (node = null);
    awaitFocus(eid);
    return;
  }
  flashNode(node);
}
async function awaitFocus(eid) {
  await recompute();
  const node = document.getElementById("tl-" + cssEscape(eid));
  if (node) flashNode(node);
}
function flashNode(node) {
  node.scrollIntoView({ behavior: "smooth", block: "center" });
  node.classList.remove("flash"); void node.offsetWidth; node.classList.add("flash");
}

function jumpToConflict(cid) {
  const c = state.result.conflicts.find((x) => x.id === cid);
  if (!c) return;
  const card = document.getElementById("cf-" + cid);
  if (card) {
    card.scrollIntoView({ behavior: "smooth", block: "center" });
    card.classList.remove("flash"); void card.offsetWidth; card.classList.add("flash");
  }
  const first = c.events[0];
  let node = document.getElementById("tl-" + cssEscape(first));
  if (!node) { clearFilters(); awaitFocus(first); }
  else flashNode(node);
}

function clearFilters() {
  $("#f-q").value = "";
  $("#f-source").value = "";
  $("#f-type").value = "";
  $("#f-conflicts").checked = false;
  $("#f-ambiguous").checked = false;
}

let recomputeTimer = null;
function scheduleRecompute() {
  clearTimeout(recomputeTimer);
  recomputeTimer = setTimeout(recompute, 250); // 输入即时重算（轻量防抖）
}

async function saveAssumptions() {
  if (!Object.keys(state.overrides).length) {
    toast("没有需要保存的调整", "info");
    return;
  }
  const res = await fetch("/api/sources/assumptions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ assumptions: state.overrides }),
  });
  if (!res.ok) return toast("保存失败", "error");
  state.overrides = {};
  toast("假设已持久化，全链路已重算", "ok");
  await recompute();
}

async function importFile(file) {
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  const res = await fetch("/api/import", { method: "POST", body: fd });
  if (!res.ok) {
    toast("导入失败: " + (await res.text()), "error");
    return;
  }
  const data = await res.json();
  const s = data.summary;
  let msg = `导入完成：新增 ${s.events_added} 事件 / ${s.sources_added} 来源 / ${s.constraints_added} 约束`;
  if (s.rejected.length) msg += `；拒绝 ${s.rejected.length} 条坏记录`;
  if (s.warnings.length) msg += `；${s.warnings.length} 条告警`;
  toast(msg, s.rejected.length ? "error" : "ok");
  state.overrides = {};
  await Promise.all([loadTypes(), recompute()]);
}

function exportAudit() {
  const parts = [];
  for (const [sid, ov] of Object.entries(state.overrides)) {
    for (const [k, v] of Object.entries(ov)) {
      parts.push(`${encodeURIComponent(k)}=${encodeURIComponent(sid + ":" + v)}`);
    }
  }
  const url = "/api/export" + (parts.length ? "?" + parts.join("&") : "");
  const a = document.createElement("a");
  a.href = url; a.download = "audit_result.json";
  document.body.appendChild(a); a.click(); a.remove();
  toast("已导出审计结果 JSON", "ok");
}

let toastTimer = null;
function toast(msg, kind) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast " + (kind === "error" ? "error" : kind === "ok" ? "ok" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 4200);
}

/* ------------------------------------------------------------------ 绑定 */

$("#file-input").addEventListener("change", (e) => importFile(e.target.files[0]));
$("#btn-recompute").addEventListener("click", recompute);
$("#btn-save").addEventListener("click", saveAssumptions);
$("#btn-discard").addEventListener("click", async () => {
  state.overrides = {};
  await recompute();
  toast("已恢复为已保存的假设", "info");
});
$("#btn-export").addEventListener("click", exportAudit);
$("#btn-reset").addEventListener("click", async () => {
  if (!confirm("确定清空全部事件、来源、约束与假设？此操作不可恢复。")) return;
  await fetch("/api/reset", { method: "POST" });
  state.overrides = {};
  await Promise.all([loadTypes(), recompute()]);
  toast("数据已清空", "ok");
});
$("#f-clear").addEventListener("click", async () => { clearFilters(); await recompute(); });
for (const id of ["#f-source", "#f-type", "#f-conflicts", "#f-ambiguous"])
  $(id).addEventListener("change", recompute);
$("#f-q").addEventListener("input", () => { clearTimeout(recomputeTimer); recomputeTimer = setTimeout(recompute, 300); });

// 拖拽导入
let dragDepth = 0;
window.addEventListener("dragenter", (e) => {
  e.preventDefault(); dragDepth++;
  $("#drop-hint").classList.remove("hidden");
});
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("dragleave", () => {
  if (--dragDepth <= 0) { dragDepth = 0; $("#drop-hint").classList.add("hidden"); }
});
window.addEventListener("drop", (e) => {
  e.preventDefault(); dragDepth = 0;
  $("#drop-hint").classList.add("hidden");
  if (e.dataTransfer.files?.[0]) importFile(e.dataTransfer.files[0]);
});

(async function init() {
  await loadTypes();
  await recompute();
})();
