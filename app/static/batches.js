/* 导入批次查看与撤回 —— 复用 app.js 的 el / esc / toast / recompute / loadTypes。
   纯原生 JS，无框架、无构建。 */
"use strict";

const batchState = {
  list: null,      // 批次摘要列表
  detail: null,    // 当前展开的批次详情
  busy: false,
};
const batch$ = (sel) => document.querySelector(sel);

/* --------------------------------------------------------------- 开关 */

function batchOpen() {
  batch$("#batch-overlay").classList.remove("hidden");
  batchLoad();
}
function batchClose() {
  batch$("#batch-overlay").classList.add("hidden");
  batchState.detail = null;
}

/* --------------------------------------------------------------- 数据 */

async function batchLoad() {
  const listBox = batch$("#batch-list");
  listBox.innerHTML = "";
  listBox.appendChild(el("div", "cmp-empty", "载入批次…"));
  batchState.detail = null;
  batch$("#batch-detail").innerHTML = "";
  let res;
  try {
    res = await fetch("/api/batches");
  } catch (e) {
    listBox.innerHTML = "";
    listBox.appendChild(el("div", "cmp-empty", "批次加载失败: " + e));
    return;
  }
  if (!res.ok) {
    listBox.innerHTML = "";
    listBox.appendChild(el("div", "cmp-empty", "批次加载失败: " + (await res.text())));
    return;
  }
  batchState.list = (await res.json()).batches;
  batchRenderList();
}

async function batchShow(id) {
  const box = batch$("#batch-detail");
  box.innerHTML = "";
  box.appendChild(el("div", "cmp-empty", `载入批次 #${id} 明细…`));
  const res = await fetch("/api/batches/" + id);
  if (!res.ok) {
    box.innerHTML = "";
    box.appendChild(el("div", "cmp-empty", "明细加载失败: " + (await res.text())));
    return;
  }
  batchState.detail = (await res.json()).batch;
  batchRenderDetail();
}

/* --------------------------------------------------------------- 列表 */

function batchRenderList() {
  const box = batch$("#batch-list");
  box.innerHTML = "";
  const list = batchState.list || [];
  if (!list.length) {
    box.appendChild(el("div", "cmp-empty", "暂无导入批次。导入 JSON 后会在此形成批次。"));
    return;
  }
  for (const b of list) {
    const c = b.counts || {};
    const row = el("div", "batch-row" + (b.status === "retracted" ? " retracted" : ""));
    const activeCls = batchState.detail && batchState.detail.id === b.id ? " selected" : "";
    row.className = "batch-row" + (b.status === "retracted" ? " retracted" : "") + activeCls;
    row.innerHTML = `
      <div class="batch-row-main">
        <span class="batch-id">#${b.id}</span>
        <span class="batch-status ${b.status}">${b.status === "retracted" ? "已撤回" : "可撤回"}</span>
        ${b.case_ref ? `<span class="batch-case">📁 ${esc(b.case_ref)}</span>` : ""}
        ${b.filename ? `<span class="batch-file">${esc(b.filename)}</span>` : ""}
      </div>
      <div class="batch-row-meta">
        <span>${esc((b.created_at || "").replace("T", " ").replace("+00:00", "Z"))}</span>
        <span class="batch-counts">
          +${b.sources_added} 来源 · +${b.events_added} 事件 · +${b.constraints_added} 约束
          ${(b.rejected_count || b.warning_count)
            ? ` · ⚠ ${b.rejected_count} 拒 / ${b.warning_count} 警` : ""}
        </span>
      </div>`;
    row.addEventListener("click", () => batchShow(b.id));
    box.appendChild(row);
  }
}

/* --------------------------------------------------------------- 详情 */

function batchRenderDetail() {
  const b = batchState.detail;
  const box = batch$("#batch-detail");
  box.innerHTML = "";
  if (!b) return;

  const head = el("div", "batch-detail-head");
  head.innerHTML = `
    <h3>批次 #${b.id}
      <span class="batch-status ${b.status}">${b.status === "retracted" ? "已撤回" : "可撤回"}</span>
    </h3>
    <div class="batch-detail-meta">
      <div>${b.case_ref ? "案件 " + esc(b.case_ref) + " · " : ""}${b.filename ? esc(b.filename) : "直接提交 / 内置载入"}
        · 导入于 ${esc((b.created_at || "").replace("T", " ").replace("+00:00", "Z"))}</div>
      ${b.status === "retracted"
        ? `<div class="batch-retracted-note">该批次已于 ${esc((b.retracted_at || "").replace("T", " ").replace("+00:00", "Z"))} 撤回，不能重复撤回。</div>`
        : ""}
    </div>`;
  box.appendChild(head);

  const writtenSources = b.items.sources.filter((i) => i.status === "written");
  const skippedSources = b.items.sources.filter((i) => i.status !== "written");
  const writtenEvents = b.items.events.filter((i) => i.status === "written");
  const skippedEvents = b.items.events.filter((i) => i.status !== "written");
  const writtenCons = b.items.constraints.filter((i) => i.status === "written");
  const skippedCons = b.items.constraints.filter((i) => i.status !== "written");

  box.appendChild(batchSection("🆕 新增来源", writtenSources, batchSourceCard, "empty-inline"));
  box.appendChild(batchSection("🆕 新增事件", writtenEvents, batchEventCard, "empty-inline"));
  box.appendChild(batchSection("🔗 批次产生的约束", writtenCons, batchConstraintCard, "empty-inline"));
  box.appendChild(batchSection("⏭ 重复未写入 / 跳过的 ID（撤回不会删除）", skippedEvents,
                               (i) => batchSkippedCard(i, "event"), "empty-inline", true));
  box.appendChild(batchSection("⏭ 跳过的约束（重复 / 悬空端点）", skippedCons,
                               (i) => batchSkippedCard(i, "constraint"), "empty-inline", true));
  if (skippedSources.length)
    box.appendChild(batchSection("⏭ 已存在来源（未覆盖假设）", skippedSources,
                                 (i) => batchSkippedCard(i, "source"), "empty-inline", true));

  if (b.status === "active") {
    const actions = el("div", "batch-actions");
    actions.innerHTML = `
      <div class="batch-warn-text">撤回将只删除本批次<b>实际写入且仍存在</b>的来源 / 事件 / 约束，
        自动移除相关 source_order 时序边并按留存事件重建；导入前数据、重复未写入的 ID 不受影响。
        含无法安全判断归属的记录时将拒绝撤回。</div>`;
    const btn = el("button", "btn danger", "🗑 确认撤回批次 #" + b.id);
    btn.addEventListener("click", () => batchConfirmRetract(b.id, btn));
    actions.appendChild(btn);
    box.appendChild(actions);
  }
}

function batchSection(title, items, cardFn, emptyCls, muted) {
  const sec = el("div", "batch-section" + (muted ? " muted" : ""));
  sec.innerHTML = `<div class="batch-section-head"><h4>${title}</h4>
    <span class="badge">${items.length}</span></div>`;
  const body = el("div", "batch-section-body");
  if (!items.length) {
    body.appendChild(el("div", emptyCls, "（无）"));
  } else {
    for (const it of items) body.appendChild(cardFn(it));
  }
  sec.appendChild(body);
  return sec;
}

function batchSourceCard(it) {
  const d = it.detail || {};
  const card = el("div", "batch-card source");
  card.innerHTML = `
    <span class="batch-card-id">${esc(it.ref_id)}</span>
    <span class="batch-card-sub">${esc(d.name || "")} · ${esc(d.kind || "")}</span>
    <span class="batch-card-sub">offset ${fmtNum(d.clock_offset)}s · ±${fmtNum(d.uncertainty)}s · 可靠度 ${fmtNum(d.reliability)}</span>`;
  return card;
}

function batchEventCard(it) {
  const d = it.detail || {};
  const card = el("div", "batch-card event");
  card.innerHTML = `
    <span class="batch-card-id">${esc(it.ref_id)}</span>
    <span class="tag type">${esc(d.event_type || "event")}</span>
    <span class="batch-card-sub">${esc(d.source_id || "")}${d.actor ? " · 👤 " + esc(d.actor) : ""}</span>
    <span class="batch-card-sub">${esc(d.summary || "")}</span>`;
  card.querySelector(".batch-card-id").addEventListener("click", () => {
    batchClose();
    if (typeof focusEvent === "function") focusEvent(it.ref_id);
  });
  return card;
}

function batchConstraintCard(it) {
  const d = it.detail || {};
  const card = el("div", "batch-card constraint");
  card.innerHTML = `
    <span class="batch-card-id">#${esc(it.ref_id)}</span>
    <span class="batch-edge-nodes">${esc(d.from)} → ${esc(d.to)}</span>
    <span class="tag type">${esc(d.kind || "causal")}</span>
    <span class="batch-card-sub">${esc(d.note || "")}</span>`;
  return card;
}

function batchSkippedCard(it, type) {
  const d = it.detail || {};
  let label = it.ref_id;
  if (type === "constraint") label = `${d.from || "?"} → ${d.to || "?"} / ${d.kind || ""}`;
  if (type === "source") label = `${it.ref_id}（${d.name || ""}）`;
  const reasonMap = {
    duplicate: "重复 ID，已存在未写入",
    duplicate_in_batch: "文件内重复，仅首条写入",
    rejected: "解析被拒绝",
    dangling_endpoint: "端点事件不存在，未写入",
  };
  const card = el("div", "batch-card skipped");
  card.innerHTML = `
    <span class="batch-card-id">${esc(label)}</span>
    <span class="batch-skip-reason">${esc(reasonMap[it.reason] || it.reason || "未写入")}</span>`;
  return card;
}

/* --------------------------------------------------------------- 撤回 */

async function batchConfirmRetract(id, btn) {
  if (batchState.busy) return;
  if (!confirm(`确认撤回批次 #${id}？\n\n`
      + "· 仅删除该批次实际写入且仍存在、归属可验证的记录；\n"
      + "· 导入前数据与重复未写入的 ID 保留；\n"
      + "· 自动移除并重建受影响来源的 source_order 边；\n"
      + "· 操作不可撤销（账本保留为已撤回审计轨迹）。")) {
    return;
  }
  batchState.busy = true;
  btn.disabled = true;
  btn.textContent = "撤回中…";
  let res, data;
  try {
    res = await fetch(`/api/batches/${id}/retract`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: true }),
    });
    data = await res.json();
  } catch (e) {
    data = { detail: String(e) };
    res = { ok: false };
  }
  batchState.busy = false;
  if (!res.ok) {
    btn.disabled = false;
    btn.textContent = "🗑 确认撤回批次 #" + id;
    alert("撤回被拒绝：\n\n" + (data.detail || JSON.stringify(data)));
    await batchShow(id);  // 刷新明细（状态可能已被外部改动）
    return;
  }
  toast(data.summary || `批次 #${id} 已撤回`, "ok");
  await Promise.all([batchLoad(), (typeof loadTypes === "function" ? loadTypes() : null),
                    (typeof recompute === "function" ? recompute() : null)]);
  // 撤回后重新展开该批次，展示已撤回状态
  await batchShow(id);
  // 列表选中态刷新
  batchRenderList();
  const rows = batch$("#batch-list").querySelectorAll(".batch-row");
  rows.forEach((r) => r.classList.remove("selected"));
}

/* --------------------------------------------------------------- 绑定 */

document.addEventListener("DOMContentLoaded", () => {
  const openBtn = batch$("#btn-batches");
  if (openBtn) openBtn.addEventListener("click", batchOpen);
  const closeBtn = batch$("#batch-close");
  if (closeBtn) closeBtn.addEventListener("click", batchClose);
  const refreshBtn = batch$("#batch-refresh");
  if (refreshBtn) refreshBtn.addEventListener("click", batchLoad);
  batch$("#batch-overlay").addEventListener("click", (e) => {
    if (e.target.id === "batch-overlay") batchClose();
  });
});
