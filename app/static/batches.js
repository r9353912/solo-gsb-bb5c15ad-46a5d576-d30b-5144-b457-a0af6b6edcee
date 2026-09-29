/* 导入批次查看 / 撤回抽屉 —— 复用 app.js 的 el / esc / toast / shortId /
   focusEvent 与各抽屉的通用样式。 */
"use strict";

const bState = {
  list: null,
  detailId: null,    // 当前展开详情的批次
  plan: null,        // 当前批次撤回预案
};
const b$ = (sel) => document.querySelector(sel);

/* --------------------------------------------------------------- 开关 */

async function bOpen() {
  b$("#batches-overlay").classList.remove("hidden");
  await bLoadList();
}
function bClose() {
  b$("#batches-overlay").classList.add("hidden");
}

async function bLoadList() {
  bState.detailId = null;
  bState.plan = null;
  const box = b$("#batches-result");
  box.innerHTML = "";
  box.appendChild(el("div", "cmp-empty", "加载批次…"));
  const res = await fetch("/api/batches");
  if (!res.ok) {
    box.innerHTML = "";
    box.appendChild(el("div", "cmp-empty", "加载失败: " + await res.text()));
    return;
  }
  bState.list = (await res.json()).batches;
  bRenderList();
}

/* --------------------------------------------------------------- 列表 */

function bRenderList() {
  const box = b$("#batches-result");
  box.innerHTML = "";
  if (!bState.list.length) {
    box.appendChild(el("div", "cmp-empty", "暂无导入批次"));
    return;
  }
  const list = el("div", "b-list");
  for (const b of bState.list) {
    const row = el("div", "b-row " + b.status);
    const label = b.label || "（未命名批次）";
    const stCls = b.status === "retracted" ? "b-st st-retracted" : "b-st st-active";
    const stTxt = b.status === "retracted" ? "已撤回" : "可撤回";
    const sum = b.summary || {};
    row.innerHTML = `
      <div class="b-row-main">
        <span class="b-id">#${b.id}</span>
        <span class="b-label">${esc(label)}</span>
        <span class="${stCls}">${stTxt}</span>
      </div>
      <div class="b-row-meta">
        <span class="cmp-hint2">${esc(b.created_at || "")}${b.retracted_at ? " · 撤回于 " + esc(b.retracted_at) : ""}</span>
        <span class="tag ok">来源 ${b.claimed.sources}</span>
        <span class="tag ok">事件 ${b.claimed.events}</span>
        <span class="tag ok">约束 ${b.claimed.constraints}</span>
        ${(sum.duplicate_event_ids || []).length
          ? `<span class="tag amb" title="重复导入、本次未写入，撤回不会删除">重复 ID ${sum.duplicate_event_ids.length}</span>` : ""}
        ${(sum.rejected || []).length
          ? `<span class="tag conf">拒绝 ${sum.rejected.length}</span>` : ""}
      </div>`;
    row.addEventListener("click", () => bOpenDetail(b.id));
    list.appendChild(row);
  }
  box.appendChild(list);
}

/* --------------------------------------------------------------- 详情 */

async function bOpenDetail(bid) {
  bState.detailId = bid;
  bState.plan = null;
  const box = b$("#batches-result");
  box.innerHTML = "";
  box.appendChild(el("div", "cmp-empty", `加载批次 #${bid}…`));
  const [dRes, pRes] = await Promise.all([
    fetch(`/api/batches/${bid}`),
    fetch(`/api/batches/${bid}/retract`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: false }),
    }),
  ]);
  if (!dRes.ok) {
    box.innerHTML = "";
    let msg = await dRes.text();
    try { msg = JSON.parse(msg).detail || msg; } catch { /* 保留原文 */ }
    box.appendChild(bBackBar());
    box.appendChild(el("div", "cmp-empty", "加载失败: " + msg));
    return;
  }
  const detail = (await dRes.json()).batch;
  let plan = null;
  if (pRes.ok) {
    plan = await pRes.json();
  } else {
    // 已撤回 / 不存在：详情仍可查看，只是没有可执行的撤回预案。
    plan = { unavailable: true, status: detail.status };
  }
  bState.plan = plan;
  bRenderDetail(detail, plan);
}

function bBackBar() {
  const bar = el("div", "b-backbar");
  const btn = el("button", "btn tiny ghost", "← 返回批次列表");
  btn.addEventListener("click", bLoadList);
  bar.appendChild(btn);
  return bar;
}

function bRenderDetail(d, plan) {
  const box = b$("#batches-result");
  box.innerHTML = "";
  box.appendChild(bBackBar());

  const head = el("div", "cmp-block b-head " + d.status);
  head.innerHTML = `
    <h3>批次 #${d.id} · ${esc(d.label || "（未命名批次）")}
      <span class="cmp-count">${d.status === "retracted" ? "已撤回" : "可撤回"} ·
      导入于 ${esc(d.created_at || "")}${d.retracted_at ? " · 撤回于 " + esc(d.retracted_at) : ""}</span></h3>`;
  box.appendChild(head);

  // 新增记录三栏
  box.appendChild(bRecordsBlock("新增来源", d.sources, (s) => `
    <span class="chain-node">${esc(s.id)}</span>
    <span class="b-rec-name">${esc(s.name || "")}</span>
    <span class="cmp-hint2">现存事件 ${s.event_count}</span>
    ${s.present ? '<span class="tag ok">仍存在</span>' : '<span class="tag conf">已不存在</span>'}`));
  box.appendChild(bRecordsBlock("新增事件", d.events, (e) => `
    <span class="chain-node" data-eid="${esc(e.id)}">${esc(shortId(e.id))}</span>
    <span class="b-rec-name">${esc(e.summary || "")}</span>
    <span class="tag type">${esc(e.event_type || "")}</span>
    <span class="cmp-hint2">${esc(e.source_id || "")}${e.actor ? " · " + esc(e.actor) : ""}</span>
    ${e.present ? '<span class="tag ok">仍存在</span>' : '<span class="tag conf">已删除（悬空）</span>'}`));
  box.appendChild(bRecordsBlock("新增声明约束", d.constraints, (c) => `
    <span class="chain-node">#${c.id}</span>
    <span class="b-rec-name">${esc(shortId(c.from_event))} → ${esc(shortId(c.to_event))}</span>
    <span class="tag type">${esc(c.kind)}</span>
    ${c.note ? `<span class="cmp-hint2">${esc(c.note)}</span>` : ""}
    ${c.present ? '<span class="tag ok">仍存在</span>' : '<span class="tag conf">已删除（悬空）</span>'}`));

  // 导入摘要里的重复 / 拒绝 / 告警
  const sum = d.summary || {};
  const notes = [];
  if ((sum.duplicate_event_ids || []).length)
    notes.push(["重复导入未写入（撤回不会删除）", sum.duplicate_event_ids.join(", "), "amb"]);
  if ((sum.rejected || []).length)
    notes.push(["导入时拒绝", sum.rejected.map((r) => `${esc("")}${r.raw || "?"}: ${r.reason}`).join("；"), "conf"]);
  if ((sum.warnings || []).length)
    notes.push(["导入告警", sum.warnings.join("；"), ""]);
  if (notes.length) {
    const blk = el("div", "cmp-block");
    blk.appendChild(el("h3", "", "导入备注"));
    for (const [title, body, cls] of notes) {
      const n = el("div", "b-note " + (cls || ""));
      n.innerHTML = `<b>${title}：</b>${body}`;
      blk.appendChild(n);
    }
    box.appendChild(blk);
  }

  // 撤回预案 / 撤回结果
  if (d.status === "retracted") {
    const blk = el("div", "cmp-block b-verdict");
    blk.innerHTML = `<h3>此批次已撤回</h3>
      <p class="cmp-hint2">台账保留以供审计；不能重复撤回。</p>`;
    box.appendChild(blk);
  } else if (plan && !plan.unavailable) {
    box.appendChild(bPlanBlock(d.id, plan));
  }

  // 点击事件定位主时间线
  box.querySelectorAll(".chain-node[data-eid]").forEach((n) =>
    n.addEventListener("click", () => { bClose(); focusEvent(n.dataset.eid); }));
}

function bRecordsBlock(title, items, render) {
  const blk = el("div", "cmp-block");
  blk.appendChild(el("h3", "", `${title}
    <span class="cmp-count">台账 ${items.length} 条</span>`));
  if (!items.length) {
    blk.appendChild(el("div", "cmp-hint2 b-empty-rec", "（无）"));
    return blk;
  }
  const list = el("div", "b-recs");
  for (const it of items) {
    const row = el("div", "b-rec");
    row.innerHTML = render(it);
    list.appendChild(row);
  }
  blk.appendChild(list);
  return blk;
}

/* --------------------------------------------------------------- 撤回预案 */

function bPlanBlock(bid, plan) {
  const blk = el("div", "cmp-block");
  const st = plan.stats;
  blk.appendChild(el("h3", "", "撤回预案（确认后执行）"));

  const stat = el("div", "b-stats");
  stat.innerHTML = `
    <span class="stat">将删来源 <b>${st.deleted.sources}</b></span>
    <span class="stat">将删事件 <b>${st.deleted.events}</b></span>
    <span class="stat">将删约束 <b>${st.deleted.constraints}</b></span>
    <span class="stat" title="导入前/其它批次保留的来源、及其记录">
      保留来源 <b>${st.retained.sources}</b></span>
    <span class="stat">保留外来约束 <b>${st.retained.constraints}</b></span>
    <span class="stat warn" title="批次写入但撤回前已被删除的台账记录">
      悬空台账 <b>${st.dangling.sources + st.dangling.events + st.dangling.constraints}</b></span>`;
  blk.appendChild(stat);

  if (plan.affected_source_order_sources.length) {
    const r = el("div", "b-note");
    r.innerHTML = `撤回后重建 source_order 时序边的受影响来源：
      <b>${plan.affected_source_order_sources.map(esc).join(", ")}</b>`;
    blk.appendChild(r);
  }
  if (plan.dangling.sources.length || plan.dangling.events.length
      || plan.dangling.constraints.length) {
    const parts = [];
    if (plan.dangling.events.length) parts.push("事件 " + plan.dangling.events.map(shortId).join(", "));
    if (plan.dangling.constraints.length) parts.push("约束 #" + plan.dangling.constraints.join(", #"));
    if (plan.dangling.sources.length) parts.push("来源 " + plan.dangling.sources.join(", "));
    const n = el("div", "b-note amb");
    n.innerHTML = `<b>悬空台账记录（撤回前已不存在，不会删除、仅统计）：</b>${parts.join("；")}`;
    blk.appendChild(n);
  }
  if (plan.retained.sources.length) {
    const n = el("div", "b-note ok");
    n.innerHTML = `<b>保留来源：</b>${plan.retained.sources.map(esc).join(", ")}
      ${(plan.retained.unclaimed_sources || []).length
        ? "（其中 " + plan.retained.unclaimed_sources.map(esc).join(", ") + " 为导入前已存在来源）" : ""}`;
    blk.appendChild(n);
  }

  if (!plan.safe) {
    const danger = el("div", "b-blockers");
    danger.innerHTML = `<div class="b-blocker-head">✋ 无法安全撤回，操作已被拒绝：</div>`;
    for (const b of plan.blockers) {
      const row = el("div", "b-blocker");
      row.textContent = "• " + b.message;
      danger.appendChild(row);
    }
    blk.appendChild(danger);
    return blk;
  }

  const actions = el("div", "b-actions");
  const info = el("span", "cmp-hint2", "撤回只删除本批次实际写入且仍存在的记录，保留导入前数据。");
  const btn = el("button", "btn tiny danger", "✓ 确认撤回该批次");
  btn.addEventListener("click", () => bConfirmRetract(bid, btn));
  actions.appendChild(info);
  actions.appendChild(btn);
  blk.appendChild(actions);
  return blk;
}

async function bConfirmRetract(bid, btn) {
  if (!confirm(`确认撤回批次 #${bid}？\n\n将只删除该批次实际写入且仍存在的来源 / 事件 / 约束，\n`
      + `导入前数据保留，source_order 边会按受影响来源重建。此操作不可撤销。`)) return;
  btn.disabled = true;
  btn.textContent = "撤回中…";
  const res = await fetch(`/api/batches/${bid}/retract`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ confirm: true }),
  });
  if (!res.ok) {
    btn.disabled = false;
    btn.textContent = "✓ 确认撤回该批次";
    let msg = await res.text();
    try { msg = JSON.parse(msg).detail || msg; } catch { /* 保留原文 */ }
    toast("撤回被拒绝: " + msg, "error");
    await bOpenDetail(bid);
    return;
  }
  const r = await res.json();
  const s = r.stats;
  toast(`批次 #${bid} 已撤回：删除 ${s.deleted.sources} 来源 / ${s.deleted.events} 事件 / `
      + `${s.deleted.constraints} 约束；悬空 ${s.dangling.sources + s.dangling.events
         + s.dangling.constraints} 条`, "ok");
  state.overrides = {};
  await Promise.all([loadTypes(), recompute()]);
  await bOpenDetail(bid);
}

/* --------------------------------------------------------------- 绑定 */

b$("#btn-batches").addEventListener("click", bOpen);
b$("#batches-close").addEventListener("click", bClose);
b$("#batches-rerun").addEventListener("click", () =>
  bState.detailId ? bOpenDetail(bState.detailId) : bLoadList());
b$("#batches-overlay").addEventListener("click", (e) => {
  if (e.target.id === "batches-overlay") bClose();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !b$("#batches-overlay").classList.contains("hidden")) bClose();
});
