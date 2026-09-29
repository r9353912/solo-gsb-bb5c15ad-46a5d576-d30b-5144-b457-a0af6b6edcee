/* 全局时序可行性抽屉 —— 复用 app.js 的 state / overrides / el / esc / toast /
   focusEvent / shortId / fmtNum 与 compare.js 的抽屉样式。 */
"use strict";

const feasState = {
  result: null,
  shown: 0,          // 已渲染的见证行数（分批渲染，5000 事件不卡页面）
  filter: "",
};
const FEAS_PAGE = 200;
const feas$ = (sel) => document.querySelector(sel);

/* --------------------------------------------------------------- 开关 */

function feasOpen() {
  feas$("#feas-overlay").classList.remove("hidden");
  feasRun();
}
function feasClose() {
  feas$("#feas-overlay").classList.add("hidden");
}

/* --------------------------------------------------------------- 检查 */

async function feasRun() {
  if (typeof retReset === "function") retReset();  // 重算时清掉过期的撤销建议
  const box = feas$("#feas-result");
  box.innerHTML = "";
  box.appendChild(el("div", "cmp-empty", "联立求解中…"));
  const res = await fetch("/api/temporal-feasibility", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    // 页面当前（含未保存）的时钟假设；筛选不参与——始终全量联立
    body: JSON.stringify({ overrides: state.overrides || {} }),
  });
  if (!res.ok) {
    let msg = await res.text();
    try { msg = JSON.parse(msg).detail || msg; } catch { /* 保留原文 */ }
    feasState.result = null;
    box.innerHTML = "";
    box.appendChild(el("div", "cmp-empty", "检查失败: " + msg));
    return;
  }
  feasState.result = await res.json();
  feasState.shown = 0;
  feasState.filter = "";
  feasRender();
}

/* --------------------------------------------------------------- 渲染 */

function feasRender() {
  const d = feasState.result;
  const box = feas$("#feas-result");
  box.innerHTML = "";
  if (!d) return;
  box.appendChild(feasVerdict(d));
  if (d.feasible) {
    box.appendChild(feasWitnessBlock(d));
  } else {
    box.appendChild(feasConflictBlock(d.witness));
    // 无解时提供约束撤销建议入口（retraction.js，预览不写库）
    if (typeof retBlock === "function") box.appendChild(retBlock());
  }
}

function feasStatsHtml(d) {
  const s = d.stats;
  return `
    <span class="stat">事件 <b>${s.event_count}</b></span>
    <span class="stat">参与约束 <b>${s.constraint_count}</b></span>
    ${s.skipped_constraints ? `<span class="stat warn">悬空约束已忽略 <b>${s.skipped_constraints}</b></span>` : ""}
    <span class="stat ${s.cycle_count ? "warn" : ""}">因果环 <b>${s.cycle_count}</b></span>
    <span class="stat">环内事件 <b>${s.events_in_cycles}</b></span>`;
}

function feasVerdict(d) {
  const blk = el("div", "cmp-block feas-verdict " + (d.feasible ? "ok" : "bad"));
  const dirty = Object.keys(state.overrides || {}).length > 0;
  blk.innerHTML = `
    <div class="feas-verdict-head">
      <span class="feas-verdict-icon">${d.feasible ? "✓" : "✗"}</span>
      <div>
        <div class="feas-verdict-title">${d.feasible ? "全局时序可行" : "全局时序无解"}</div>
        <div class="feas-verdict-sub">${esc(d.summary)}
          ${dirty ? '<span class="tag amb">含未保存调整</span>' : ""}</div>
      </div>
    </div>
    <div class="sens-meta">${feasStatsHtml(d)}</div>`;
  return blk;
}

/* ---- 可行：每个事件的最早可行时刻（见证），分批渲染 + 关键字过滤 ---- */

function feasWitnessBlock(d) {
  const blk = el("div", "cmp-block");
  blk.innerHTML = `<h3>见证：每个事件的最早可行时刻
    <span class="cmp-count">落在各自闭区间内且满足全部约束的一种取值；点击行在主时间线定位</span></h3>`;
  const bar = el("div", "feas-filter-bar");
  bar.innerHTML = `<input type="search" id="feas-filter" placeholder="过滤摘要 / ID / 来源…"
      value="${esc(feasState.filter)}">
    <span class="cmp-hint2" id="feas-count"></span>`;
  blk.appendChild(bar);
  const list = el("div", "feas-list");
  list.id = "feas-list";
  blk.appendChild(list);
  const more = el("button", "btn tiny ghost feas-more");
  more.id = "feas-more";
  blk.appendChild(more);
  bar.querySelector("#feas-filter").addEventListener("input", (e) => {
    feasState.filter = e.target.value;
    feasState.shown = 0;
    feasRenderRows();
  });
  more.addEventListener("click", () => feasRenderRows(true));
  // 行渲染在节点挂到 DOM 后进行
  setTimeout(feasRenderRows, 0);
  return blk;
}

function feasFilteredEvents() {
  const d = feasState.result;
  const q = feasState.filter.trim().toLowerCase();
  if (!q) return d.events;
  return d.events.filter((e) =>
    (e.summary || "").toLowerCase().includes(q)
    || e.id.toLowerCase().includes(q)
    || (e.source_id || "").toLowerCase().includes(q));
}

function feasRenderRows(append) {
  const d = feasState.result;
  if (!d || !d.feasible) return;
  const list = feas$("#feas-list");
  const more = feas$("#feas-more");
  if (!list || !more) return;
  const events = feasFilteredEvents();
  if (!append) {
    list.innerHTML = "";
    feasState.shown = 0;
  }
  const end = Math.min(feasState.shown + FEAS_PAGE, events.length);
  const frag = document.createDocumentFragment();
  for (let i = feasState.shown; i < end; i++) {
    const e = events[i];
    const row = el("div", "feas-row" + (e.in_cycle ? " in-cycle" : ""));
    row.innerHTML = `
      <span class="feas-row-time" title="最早可行时刻">${esc(e.earliest_text.slice(11, 23))}</span>
      <span class="chain-node feas-row-id" data-eid="${esc(e.id)}">${esc(shortId(e.id))}</span>
      <span class="feas-row-src">${esc(e.source_id)}</span>
      <span class="feas-row-summary">${esc(e.summary || "")}</span>
      <span class="feas-row-range" title="真实时间闭区间 [lo, hi] 与余量（hi − 最早可行时刻）">
        [${esc(e.lo_text.slice(11, 23))} ~ ${esc(e.hi_text.slice(11, 23))}] · 余量 ${fmtNum(e.slack)}s
        ${e.in_cycle ? '<span class="tag amb" title="处于因果环内：环内事件取同一时刻">环</span>' : ""}
      </span>`;
    row.querySelector(".feas-row-id").addEventListener("click", () => {
      feasClose();
      focusEvent(e.id);
    });
    frag.appendChild(row);
  }
  list.appendChild(frag);
  feasState.shown = end;
  const count = feas$("#feas-count");
  if (count) count.textContent = `${events.length} / ${d.events.length} 个事件`;
  if (feasState.shown >= events.length) {
    more.classList.add("hidden");
  } else {
    more.classList.remove("hidden");
    more.textContent = `显示更多（已 ${feasState.shown} / ${events.length}）`;
  }
}

/* ---- 无解：矛盾路径（事件 ID + 约束 ID + 两端冲突界限） ---- */

function feasConflictBlock(w) {
  const blk = el("div", "cmp-block");
  blk.innerHTML = `<h3>矛盾路径
    <span class="cmp-count">下界沿路径逐段传递，最终在终点越过其区间上界；点击事件在主时间线定位</span></h3>`;

  const bounds = el("div", "feas-bounds");
  bounds.innerHTML = `
    <div class="feas-bound">
      <span class="feas-bound-label">路径起点 · 区间下界</span>
      <span class="chain-node" data-eid="${esc(w.head.event)}">${esc(shortId(w.head.event))}</span>
      <span class="feas-bound-val">t ≥ ${esc(w.head.text)}</span>
    </div>
    <div class="feas-bound bad">
      <span class="feas-bound-label">路径终点 · 区间上界</span>
      <span class="chain-node" data-eid="${esc(w.tail.event)}">${esc(shortId(w.tail.event))}</span>
      <span class="feas-bound-val">t ≤ ${esc(w.tail.text)}</span>
    </div>
    <div class="feas-bound bad">
      <span class="feas-bound-label">冲突超出</span>
      <span class="feas-bound-val">${esc(w.overshoot_text)}</span>
    </div>`;
  blk.appendChild(bounds);

  const chain = el("div", "chain feas-chain");
  w.steps.forEach((s, i) => {
    if (i === 0) chain.appendChild(feasNode(s.from));
    const hop = el("span", "feas-hop" + (s.individually_feasible ? "" : " bad"));
    const cid = s.constraint_id === null || s.constraint_id === undefined
      ? "?" : `#${s.constraint_id}`;
    hop.title = `约束 ${cid}（${s.kind}）要求 t(${s.to}) ≥ t(${s.from})；`
      + `传递到此的下界 ${fmtNum(s.running_lo)}s；`
      + (s.individually_feasible ? "该边单独可行" : "该边单独即不可能");
    hop.innerHTML = `<span class="chain-arrow ${s.individually_feasible ? "" : "bad"}">→</span>
      <span class="feas-hop-cid">${cid}${s.individually_feasible ? "" : " ⚠"}</span>`;
    chain.appendChild(hop);
    chain.appendChild(feasNode(s.to, !s.individually_feasible));
  });
  blk.appendChild(chain);

  const noteCls = w.all_edges_individually_feasible ? "ok" : "warn";
  const note = el("div", "feas-note " + noteCls);
  note.textContent = w.explanation;
  blk.appendChild(note);

  blk.querySelectorAll(".chain-node").forEach((n) =>
    n.addEventListener("click", () => { feasClose(); focusEvent(n.dataset.eid); }));
  return blk;
}

function feasNode(eid, bad) {
  const n = el("span", "chain-node" + (bad ? " bad" : ""));
  n.dataset.eid = eid;
  n.textContent = shortId(eid);
  n.title = "点击在主时间线定位";
  return n;
}

/* --------------------------------------------------------------- 绑定 */

feas$("#btn-feasibility").addEventListener("click", feasOpen);
feas$("#btn-feasibility-2").addEventListener("click", feasOpen);
feas$("#feas-close").addEventListener("click", feasClose);
feas$("#feas-rerun").addEventListener("click", feasRun);
feas$("#feas-overlay").addEventListener("click", (e) => {
  if (e.target.id === "feas-overlay") feasClose();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !feas$("#feas-overlay").classList.contains("hidden")) feasClose();
});
