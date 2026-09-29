/* 约束撤销建议 —— 嵌在全局时序可行性抽屉内（无解时出现）。
   复用 app.js 的 state / el / esc / toast / focusEvent / shortId / fmtNum
   与 feasibility.js 的 feas$ / feasConflictBlock / feas-row 样式。 */
"use strict";

const retState = {
  result: null,      // 最近一次搜索结果（null = 尚未搜索）
  running: false,
};
const RET_WITNESS_PAGE = 100;   // 见证事件分批渲染，5000 事件不卡页面

/* --------------------------------------------------------------- 搜索 */

function retReset() {
  retState.result = null;
  retState.running = false;
}

async function retRun() {
  retState.running = true;
  retState.result = null;
  retRefresh();
  const res = await fetch("/api/retraction-suggestions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    // 页面当前（含未保存）的时钟假设参与每一次候选复核
    body: JSON.stringify({ overrides: state.overrides || {} }),
  });
  retState.running = false;
  if (!res.ok) {
    let msg = await res.text();
    try { msg = JSON.parse(msg).detail || msg; } catch { /* 保留原文 */ }
    retState.result = { status: "__error__", summary: "搜索失败: " + msg };
  } else {
    retState.result = await res.json();
  }
  retRefresh();
}

/* 重新渲染可行性抽屉里的撤销建议块（如果当前已挂载） */
function retRefresh() {
  const slot = feas$("#ret-slot");
  if (!slot) return;
  const fresh = retBlock();
  slot.replaceWith(fresh);
}

/* --------------------------------------------------------------- 渲染 */

function retBlock() {
  const blk = el("div", "cmp-block ret-block");
  blk.id = "ret-slot";

  if (retState.running) {
    blk.innerHTML = `<h3>🪓 约束撤销建议</h3>
      <div class="cmp-empty">沿矛盾路径搜索撤销方案并逐一复核中…</div>`;
    return blk;
  }

  const d = retState.result;
  if (!d) {
    blk.innerHTML = `<h3>🪓 约束撤销建议
      <span class="cmp-count">沿矛盾路径搜索需撤销的最少人工/导入约束（source_order 时序边与事件区间不可撤销）；
      每个候选都用原求解器复核，预览不写入数据库</span></h3>`;
    const bar = el("div", "ret-start");
    bar.innerHTML = `<span class="cmp-hint2">最多撤销 3 条 · 最多求解 2000 次 · 至多展示 5 个同样最少的方案</span>`;
    const btn = el("button", "btn tiny primary", "▶ 搜索撤销方案");
    btn.addEventListener("click", retRun);
    bar.appendChild(btn);
    blk.appendChild(bar);
    return blk;
  }

  if (d.status === "__error__") {
    blk.innerHTML = `<h3>🪓 约束撤销建议</h3>`;
    blk.appendChild(el("div", "cmp-empty", esc(d.summary)));
    blk.appendChild(retRerunBar());
    return blk;
  }

  blk.appendChild(retVerdict(d));
  if (d.status === "no_retraction_fix" && d.remaining_witness) {
    blk.appendChild(feasConflictBlock(d.remaining_witness));
  }
  if (d.status === "ok") {
    d.solutions.forEach((sol, i) => blk.appendChild(retSolution(d, sol, i)));
    if (d.notice) blk.appendChild(el("div", "feas-note warn", esc(d.notice)));
  }
  blk.appendChild(retRerunBar());
  return blk;
}

function retRerunBar() {
  const bar = el("div", "ret-start");
  const btn = el("button", "btn tiny", "⟳ 重新搜索");
  btn.title = "按当前（含未保存）的时钟假设重新搜索撤销方案";
  btn.addEventListener("click", retRun);
  bar.appendChild(btn);
  return bar;
}

function retVerdict(d) {
  const head = el("div", "ret-head");
  const cls = { ok: "ok", already_feasible: "ok",
                no_retraction_fix: "bad", none_within_limits: "warn" }[d.status] || "warn";
  const icon = { ok: "🪓", already_feasible: "✓",
                 no_retraction_fix: "✗", none_within_limits: "⚠" }[d.status] || "⚠";
  const title = {
    ok: d.complete ? "约束撤销建议（同样最少）" : "约束撤销候选（搜索未完成）",
    already_feasible: "无需撤销",
    no_retraction_fix: "没有仅靠撤销约束的修复方案",
    none_within_limits: d.complete ? "限制内没有撤销方案" : "搜索未完成",
  }[d.status] || "约束撤销建议";
  const dirty = Object.keys(state.overrides || {}).length > 0;
  head.innerHTML = `
    <div class="feas-verdict-head">
      <span class="feas-verdict-icon ret-icon ${cls}">${icon}</span>
      <div>
        <div class="feas-verdict-title ret-title ${cls}">${esc(title)}
          ${d.complete ? "" : '<span class="tag amb">未完成</span>'}
          ${d.solutions_truncated ? '<span class="tag amb">仅展示前 5 个</span>' : ""}
          ${dirty ? '<span class="tag amb">含未保存调整</span>' : ""}
        </div>
        <div class="feas-verdict-sub">${esc(d.summary)}</div>
      </div>
    </div>`;
  const s = d.stats, L = d.limits;
  const meta = el("div", "sens-meta");
  meta.innerHTML = `
    <span class="stat">可撤销约束 <b>${s.retractable_constraints}</b></span>
    <span class="stat">受保护 source_order <b>${s.protected_constraints}</b></span>
    <span class="stat ${s.solves_used >= L.max_solves ? "warn" : ""}">求解 <b>${s.solves_used}</b> / ${L.max_solves}</span>
    <span class="stat">撤销上限 <b>${L.max_depth}</b> 条</span>
    ${d.min_retractions ? `<span class="stat ${d.complete ? "" : "warn"}">最少撤销 <b>${d.min_retractions}</b> 条</span>` : ""}`;
  head.appendChild(meta);
  return head;
}

function retSolution(d, sol, idx) {
  const card = el("div", "ret-card");
  const k = sol.constraints.length;
  const badge = d.complete
    ? '<span class="cmp-badge removed">最少撤销</span>'
    : '<span class="cmp-badge persisted">候选 · 未完成</span>';
  card.innerHTML = `
    <div class="ret-card-head">
      <span class="ret-card-title">方案 ${idx + 1} · 撤销 ${k} 条约束</span>
      ${badge}
      <span class="cmp-hint2">撤销后 ${sol.witness.stats.event_count} 事件 / ${sol.witness.stats.constraint_count} 约束联立可行</span>
    </div>`;

  const list = el("div", "ret-constraints");
  sol.constraints.forEach((c) => {
    const row = el("div", "ret-constraint");
    row.innerHTML = `
      <span class="ret-cid" title="约束 ID">#${c.id}</span>
      <span class="chain-node" data-eid="${esc(c.from)}">${esc(shortId(c.from))}</span>
      <span class="chain-arrow">→</span>
      <span class="chain-node" data-eid="${esc(c.to)}">${esc(shortId(c.to))}</span>
      <span class="ret-kind">${esc(c.kind)}</span>
      ${c.note ? `<span class="ret-note">${esc(c.note)}</span>` : ""}`;
    row.querySelectorAll(".chain-node").forEach((n) =>
      n.addEventListener("click", () => { feasClose(); focusEvent(n.dataset.eid); }));
    list.appendChild(row);
  });
  card.appendChild(list);

  // 撤销后全量可行见证（默认折叠，展开后分批渲染）
  const toggle = el("button", "btn tiny ghost", `▸ 查看撤销后全量可行见证（${sol.witness.events.length} 事件）`);
  const box = el("div", "ret-witness hidden");
  toggle.addEventListener("click", () => {
    if (box.classList.contains("hidden")) {
      box.classList.remove("hidden");
      toggle.textContent = `▾ 收起见证（${sol.witness.events.length} 事件）`;
      if (!box.dataset.loaded) {
        box.dataset.loaded = "1";
        retRenderWitness(box, sol.witness.events, 0);
      }
    } else {
      box.classList.add("hidden");
      toggle.textContent = `▸ 查看撤销后全量可行见证（${sol.witness.events.length} 事件）`;
    }
  });
  card.appendChild(toggle);
  card.appendChild(box);
  return card;
}

function retRenderWitness(box, events, start) {
  const list = el("div", "feas-list");
  const end = Math.min(start + RET_WITNESS_PAGE, events.length);
  const frag = document.createDocumentFragment();
  for (let i = start; i < end; i++) {
    const e = events[i];
    const row = el("div", "feas-row" + (e.in_cycle ? " in-cycle" : ""));
    row.innerHTML = `
      <span class="feas-row-time" title="最早可行时刻">${esc(e.earliest_text.slice(11, 23))}</span>
      <span class="chain-node feas-row-id" data-eid="${esc(e.id)}">${esc(shortId(e.id))}</span>
      <span class="feas-row-src">${esc(e.source_id)}</span>
      <span class="feas-row-summary">${esc(e.summary || "")}</span>
      <span class="feas-row-range" title="真实时间闭区间 [lo, hi] 与余量">
        [${esc(e.lo_text.slice(11, 23))} ~ ${esc(e.hi_text.slice(11, 23))}] · 余量 ${fmtNum(e.slack)}s
        ${e.in_cycle ? '<span class="tag amb">环</span>' : ""}
      </span>`;
    row.querySelector(".feas-row-id").addEventListener("click", () => {
      feasClose();
      focusEvent(e.id);
    });
    frag.appendChild(row);
  }
  if (start === 0) box.innerHTML = "";
  else box.querySelector(".ret-witness-more")?.remove();
  list.appendChild(frag);
  box.appendChild(list);
  if (end < events.length) {
    const more = el("button", "btn tiny ghost feas-more ret-witness-more",
      `显示更多（已 ${end} / ${events.length}）`);
    more.addEventListener("click", () => retRenderWitness(box, events, end));
    box.appendChild(more);
  }
}
