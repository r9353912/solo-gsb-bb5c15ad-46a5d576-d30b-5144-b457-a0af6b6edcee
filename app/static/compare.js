/* 假设方案对比 —— 复用 app.js 的全局工具（el / esc / toast / focusEvent / state）。 */
"use strict";

const cmpState = {
  scenarios: [],
  result: null,
  selection: { a: "current", b: "" },   // current 或 sc-<id>
};
const TIMELINE_DIFF_LIMIT = 200;        // 与后端 scenarios.TIMELINE_DIFF_LIMIT 一致

const cmp$ = (sel) => document.querySelector(sel);
const STATUS_TEXT = { ok: "🟢 支持", boundary: "🟠 脆弱", impossible: "🔴 不可能" };

/* ------------------------------------------------------------- 方案管理 */

async function cmpLoadScenarios() {
  const res = await fetch("/api/scenarios");
  if (!res.ok) return toast("方案列表加载失败", "error");
  cmpState.scenarios = (await res.json()).scenarios;
  cmpRenderScenarioBar();
  cmpRenderSelects();
}

function cmpSideValue(kind) {
  return kind === "a" ? cmpState.selection.a : cmpState.selection.b;
}

function cmpSetSide(kind, value) {
  cmpState.selection[kind] = value;
}

function cmpSideSpec(value) {
  if (value === "current") {
    // 带上页面上尚未保存的调整，使"当前"侧即所见即所算
    return { kind: "current", overrides: state.overrides || {} };
  }
  const id = Number(String(value).replace(/^sc-/, ""));
  return { kind: "scenario", scenario_id: id };
}

function cmpRenderSelects() {
  const opts = ['<option value="current">● 当前生效假设（含未保存调整）</option>']
    .concat(cmpState.scenarios.map(
      (s) => `<option value="sc-${s.id}">⚖ ${esc(s.name)}</option>`)).join("");
  for (const [kind, selId] of [["a", "#cmp-side-a"], ["b", "#cmp-side-b"]]) {
    const sel = cmp$(selId);
    sel.innerHTML = opts;
    let val = cmpSideValue(kind);
    if (!val || ![...sel.options].some((o) => o.value === val)) {
      // B 默认选第一个已保存方案（没有则与 A 同为 current）
      val = kind === "b" && cmpState.scenarios.length
        ? `sc-${cmpState.scenarios[0].id}` : "current";
      cmpSetSide(kind, val);
    }
    sel.value = val;
  }
}

function cmpRenderScenarioBar() {
  const box = cmp$("#cmp-scenarios");
  box.innerHTML = "";
  if (!cmpState.scenarios.length) {
    box.appendChild(el("div", "cmp-empty",
      "还没有命名方案 —— 在左侧调整好来源假设后，点上方“💾 保存当前假设为方案”即可命名留存。"));
    return;
  }
  const wrap = el("div", "cmp-chips");
  for (const s of cmpState.scenarios) {
    const nChanged = Object.values(s.assumptions)
      .reduce((n, p) => n + Object.keys(p).length, 0);
    const chip = el("div", "cmp-chip");
    chip.innerHTML = `
      <span class="cmp-chip-name">⚖ ${esc(s.name)}</span>
      <span class="cmp-chip-meta">${Object.keys(s.assumptions).length} 来源 · ${nChanged} 参数</span>
      ${s.description ? `<span class="cmp-chip-desc">${esc(s.description)}</span>` : ""}
      <span class="cmp-chip-ops">
        <button data-op="use-a" title="作为方案 A">→A</button>
        <button data-op="use-b" title="作为方案 B">→B</button>
        <button data-op="rename" title="重命名 / 改描述">✎</button>
        <button data-op="del" title="删除方案">✕</button>
      </span>`;
    chip.querySelectorAll("button").forEach((btn) =>
      btn.addEventListener("click", () => cmpChipAction(btn.dataset.op, s)));
    wrap.appendChild(chip);
  }
  box.appendChild(wrap);
}

async function cmpChipAction(op, s) {
  if (op === "use-a") { cmpSetSide("a", `sc-${s.id}`); cmpRenderSelects(); }
  else if (op === "use-b") { cmpSetSide("b", `sc-${s.id}`); cmpRenderSelects(); }
  else if (op === "del") {
    if (!confirm(`删除方案「${s.name}」？事件与已保存来源假设不受影响。`)) return;
    const res = await fetch(`/api/scenarios/${s.id}`, { method: "DELETE" });
    if (!res.ok) return toast("删除失败", "error");
    if (cmpState.selection.a === `sc-${s.id}`) cmpState.selection.a = "current";
    if (cmpState.selection.b === `sc-${s.id}`) cmpState.selection.b = "current";
    await cmpLoadScenarios();
  } else if (op === "rename") {
    const name = prompt("方案名称", s.name);
    if (name === null) return;
    const desc = prompt("备注（可留空）", s.description || "") ;
    const res = await fetch(`/api/scenarios/${s.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim(), description: desc || "" }),
    });
    if (!res.ok) return toast("重命名失败: " + (await res.text()), "error");
    await cmpLoadScenarios();
  }
}

async function cmpSaveCurrent() {
  const name = prompt("为当前来源假设命名（会把未保存调整一并快照）",
    `方案 ${cmpState.scenarios.length + 1}`);
  if (!name || !name.trim()) return;
  const res = await fetch("/api/scenarios", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: name.trim(),
      from_current: true,
      assumptions: state.overrides || {},
    }),
  });
  if (!res.ok) return toast("保存方案失败: " + (await res.text()), "error");
  const data = await res.json();
  await cmpLoadScenarios();
  cmpSetSide("b", `sc-${data.scenario.id}`);
  cmpRenderSelects();
  toast(`方案「${name.trim()}」已保存`, "ok");
}

/* --------------------------------------------------------------- 对比 */

async function cmpRun() {
  const specA = cmpSideSpec(cmp$("#cmp-side-a").value);
  const specB = cmpSideSpec(cmp$("#cmp-side-b").value);
  if (specA.kind === "scenario" && specB.kind === "scenario"
      && specA.scenario_id === specB.scenario_id) {
    return toast("请选择两套不同的方案（或一侧用“当前假设”）", "error");
  }
  const hint = cmp$("#cmp-hint");
  hint.textContent = "重算中…";
  const res = await fetch("/api/compare", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ a: specA, b: specB }),
  });
  hint.textContent = "";
  if (!res.ok) return toast("对比失败: " + (await res.text()), "error");
  cmpState.result = await res.json();
  cmpRenderResult();
}

/* ------------------------------------------------------------ 结果渲染 */

function cmpRenderResult() {
  const d = cmpState.result;
  const box = cmp$("#cmp-result");
  box.innerHTML = "";
  if (!d) return;
  box.appendChild(cmpSummary(d));
  box.appendChild(cmpAssumptions(d));
  box.appendChild(cmpFindingsBlock(d, "conflicts", "冲突链差异",
    "impossible_order / causal_cycle", true));
  box.appendChild(cmpFindingsBlock(d, "ambiguity_groups", "时间歧义组差异",
    "区间重叠、先后无法唯一判定", false));
  box.appendChild(cmpEdgeChanges(d));
  box.appendChild(cmpTimeline(d));
}

function chip(n, cls) {
  return `<span class="cmp-num ${cls || ""}">${n}</span>`;
}

function cmpSummary(d) {
  const s = d.summary;
  const blk = el("div", "cmp-block cmp-summary");
  blk.innerHTML = `
    <div class="cmp-cols">
      <div class="cmp-col-head a">A · ${esc(d.sides.a.label)}</div>
      <div class="cmp-col-head mid">差异</div>
      <div class="cmp-col-head b">B · ${esc(d.sides.b.label)}</div>
    </div>
    <div class="cmp-sum-row">
      <div class="cmp-col a">冲突链 <b>${s.conflicts_a}</b></div>
      <div class="cmp-col mid">
        <span class="cmp-delta added" title="B 相对 A 新增">＋${s.conflicts_added}</span>
        <span class="cmp-delta removed" title="B 相对 A 消失">－${s.conflicts_removed}</span>
        <span class="cmp-delta persisted" title="两方案都存在">＝${s.conflicts_persisted}</span>
      </div>
      <div class="cmp-col b">冲突链 <b>${s.conflicts_b}</b></div>
    </div>
    <div class="cmp-sum-row">
      <div class="cmp-col a">歧义组 <b>${s.ambiguity_groups_a}</b></div>
      <div class="cmp-col mid">
        <span class="cmp-delta added">＋${s.ambiguities_added}</span>
        <span class="cmp-delta removed">－${s.ambiguities_removed}</span>
        <span class="cmp-delta persisted">＝${s.ambiguities_persisted}</span>
      </div>
      <div class="cmp-col b">歧义组 <b>${s.ambiguity_groups_b}</b></div>
    </div>
    <div class="cmp-sum-row">
      <div class="cmp-col a">不可能边 <b>${s.impossible_edges_a}</b></div>
      <div class="cmp-col mid"><span class="cmp-hint2">${s.edges_changed} 条边状态变化</span></div>
      <div class="cmp-col b">不可能边 <b>${s.impossible_edges_b}</b></div>
    </div>
    <div class="cmp-sum-row">
      <div class="cmp-col a">事件 ${s.event_count}</div>
      <div class="cmp-col mid"><span class="cmp-hint2">${s.timeline_swapped_pairs} 对顺序交换</span></div>
      <div class="cmp-col b">事件 ${s.event_count}</div>
    </div>`;
  return blk;
}

function cmpAssumptions(d) {
  const blk = el("div", "cmp-block");
  const rows = d.assumption_diff;
  blk.innerHTML = `<h3>假设参数差异 <span class="cmp-count">${rows.length} 个来源</span></h3>`;
  if (!rows.length) {
    blk.appendChild(el("div", "cmp-empty", "两套方案的来源假设完全相同。"));
    return blk;
  }
  const grid = el("div", "cmp-assump");
  const PARAM_NAME = { clock_offset: "offset", drift_ppm: "drift",
    uncertainty: "不确定度", reliability: "可靠度" };
  for (const r of rows) {
    const row = el("div", "cmp-assump-row");
    row.innerHTML = `<div class="cmp-assump-src">${esc(r.source_id)}</div>`
      + r.params.map((p) => {
        const same = p.a === null || p.b === null;
        const arrow = same ? "∅ →" : "→";
        return `<div class="cmp-assump-p"><span class="cmp-pname">${esc(PARAM_NAME[p.param] || p.param)}</span>
          <span class="cmp-pval a">${p.a === null ? "∅" : cmpNum(p.a)}</span>
          <span class="cmp-parrow">${arrow}</span>
          <span class="cmp-pval b">${p.b === null ? "∅" : cmpNum(p.b)}</span></div>`;
      }).join("");
    grid.appendChild(row);
  }
  blk.appendChild(grid);
  return blk;
}

function cmpFindingsBlock(d, key, title, sub, isConflict) {
  const blk = el("div", "cmp-block");
  const groups = d[key];
  blk.innerHTML = `<h3>${title} <span class="cmp-count">${sub}</span></h3>`;

  const sections = [
    ["added", `B 新增（A 中没有）`, "added"],
    ["removed", `B 消失（仅 A 有）`, "removed"],
    ["persisted", "持续存在（两方案均有）", "persisted"],
  ];
  for (const [cat, label, cls] of sections) {
    const items = groups[cat];
    const sec = el("div", "cmp-fsec " + cls);
    sec.innerHTML = `<div class="cmp-fsec-head">${label}
      <span class="cmp-badge ${cls}">${items.length}</span></div>`;
    const body = el("div", "cmp-fsec-body" + (cat === "persisted" ? " paired" : ""));
    if (!items.length) {
      body.appendChild(el("div", "cmp-empty small", cat === "persisted" ? "无" : "无"));
    } else if (cat === "persisted") {
      for (const it of items) {
        const card = el("div", "cmp-pair-card");
        card.appendChild(cmpFindingHalf(it.a, "a", isConflict));
        card.appendChild(el("div", "cmp-pair-mid", "≈"));
        card.appendChild(cmpFindingHalf(it.b, "b", isConflict));
        body.appendChild(card);
      }
    } else {
      for (const it of items) {
        body.appendChild(cmpFindingHalf(it.a || it.b,
          it.a ? "a" : "b", isConflict));
      }
    }
    sec.appendChild(body);
    blk.appendChild(sec);
  }
  return blk;
}

function cmpFindingHalf(f, side, isConflict) {
  const card = el("div", "cmp-fcard " + side + (isConflict ? " conflict" : " ambig"));
  const kindLabel = isConflict
    ? (f.kind === "causal_cycle" ? "因果环" : "不可能顺序")
    : `歧义组 · ${f.source_count} 来源`;
  const chain = cmpChainHtml(f);
  card.innerHTML = `
    <div class="cmp-fcard-head">
      <span class="cmp-fid">${isConflict ? "⛓" : "⚠"} ${esc(f.id)}</span>
      <span class="cmp-fkind">${kindLabel}</span>
    </div>
    <div class="cmp-fexplain">${esc(f.explanation)}</div>
    <div class="chain">${chain}</div>`;
  card.querySelectorAll(".chain-node").forEach((n) =>
    n.addEventListener("click", () => cmpLocate(n.dataset.eid)));
  return card;
}

function cmpChainHtml(f) {
  const keys = f.edge_keys || [];
  if (keys.length) {
    let html = "";
    keys.forEach((k, i) => {
      if (i === 0) html += cmpNode(k.from);
      html += `<span class="chain-arrow ${k.status === "impossible" ? "bad" : ""}">→</span>`;
      html += cmpNode(k.to, k.status === "impossible");
    });
    return html;
  }
  return (f.events || []).map((id) => cmpNode(id)).join(
    '<span class="chain-arrow">·</span>');
}

function cmpNode(eid, bad) {
  return `<span class="chain-node ${bad ? "bad" : ""}" data-eid="${esc(eid)}"
    title="点击在主时间线定位">${esc(shortId(eid))}</span>`;
}

function cmpEdgeChanges(d) {
  const blk = el("div", "cmp-block");
  const items = d.edge_changes;
  blk.innerHTML = `<h3>顺序边状态变化 <span class="cmp-count">${items.length} 条</span></h3>`;
  if (!items.length) {
    blk.appendChild(el("div", "cmp-empty", "所有约束边在两套方案下判定一致。"));
    return blk;
  }
  const table = el("div", "cmp-edges");
  for (const e of items) {
    const row = el("div", "cmp-edge-row");
    row.innerHTML = `
      <span class="cmp-edge-nodes chain-node" data-eid="${esc(e.from)}">${esc(shortId(e.from))}</span>
      <span class="chain-arrow">→</span>
      <span class="cmp-edge-nodes chain-node" data-eid="${esc(e.to)}">${esc(shortId(e.to))}</span>
      <span class="cmp-edge-kind">${esc(e.kind)}${e.note ? " · " + esc(e.note) : ""}</span>
      <span class="cmp-edge-st ${e.status_a}">${STATUS_TEXT[e.status_a] || e.status_a}</span>
      <span class="cmp-edge-arrow">→</span>
      <span class="cmp-edge-st ${e.status_b}">${STATUS_TEXT[e.status_b] || e.status_b}</span>
      <span class="cmp-edge-gap" title="最有利间隔区间">
        [${e.gap_a.gap_lo_text}, ${e.gap_a.gap_hi_text}]
        → [${e.gap_b.gap_lo_text}, ${e.gap_b.gap_hi_text}]
      </span>`;
    row.querySelectorAll(".chain-node").forEach((n) =>
      n.addEventListener("click", () => cmpLocate(n.dataset.eid)));
    table.appendChild(row);
  }
  blk.appendChild(table);
  return blk;
}

function cmpTimeline(d) {
  const blk = el("div", "cmp-block");
  const t = d.timeline_changes;
  blk.innerHTML = `<h3>时间线排序变化
    <span class="cmp-count">${t.swapped_pair_count} 对交换 · ${t.rank_shift_count} 个位次漂移</span></h3>`;
  if (!t.swapped_pair_count && !t.rank_shift_count) {
    blk.appendChild(el("div", "cmp-empty", "两套方案下时间线相对顺序一致。"));
    return blk;
  }
  const body = el("div", "cmp-tl");
  if (t.swapped_pair_count) {
    const head = el("div", "cmp-fsec-head");
    head.innerHTML = `估计时刻顺序交换 <span class="cmp-badge added">${t.swapped_pair_count}</span>`;
    body.appendChild(head);
    for (const pair of t.swapped_pairs) body.appendChild(cmpSwapRow(pair));
    if (t.truncated) {
      body.appendChild(el("div", "cmp-empty small",
        `仅展示前 ${TIMELINE_DIFF_LIMIT} 对，完整列表与完整排列见导出 JSON（order_a / order_b）。`));
    }
  }
  if (t.rank_shifts.length) {
    const head = el("div", "cmp-fsec-head");
    head.innerHTML = `位次漂移 <span class="cmp-badge persisted">${t.rank_shift_count}</span>`;
    body.appendChild(head);
    for (const ev of t.rank_shifts.slice(0, 50)) body.appendChild(cmpShiftRow(ev));
  }
  blk.appendChild(body);
  return blk;
}

function pair_limit() { return 200; }

function cmpSwapRow(pair) {
  const [x, y] = pair.events;
  const row = el("div", "cmp-tl-row swap");
  row.innerHTML = `
    <div class="cmp-tl-order a">#${x.rank_a + 1} ${cmpNode(x.id)} <span class="chain-arrow">→</span> ${cmpNode(y.id)} #${y.rank_a + 1}</div>
    <div class="cmp-tl-order b">#${x.rank_b + 1} ${cmpNode(y.id)} <span class="chain-arrow">→</span> ${cmpNode(x.id)} #${x.rank_b + 1}</div>
    <div class="cmp-tl-est">
      <span class="a">${esc(x.est_text_a.slice(11, 23))}</span>
      <span class="chain-arrow">→</span>
      <span class="b">${esc(x.est_text_b.slice(11, 23))}</span>
    </div>`;
  row.querySelectorAll(".chain-node").forEach((n) =>
    n.addEventListener("click", () => cmpLocate(n.dataset.eid)));
  return row;
}

function cmpShiftRow(ev) {
  const row = el("div", "cmp-tl-row shift");
  const arrow = ev.rank_b > ev.rank_a ? "↓" : "↑";
  row.innerHTML = `
    <span class="chain-node" data-eid="${esc(ev.id)}">${esc(shortId(ev.id))}</span>
    <span class="cmp-shift">${arrow} ${Math.abs(ev.rank_b - ev.rank_a)} 位</span>
    <span class="cmp-tl-est"><span class="a">${esc(ev.est_text_a.slice(11, 23))}</span>
      <span class="chain-arrow">→</span><span class="b">${esc(ev.est_text_b.slice(11, 23))}</span></span>`;
  row.querySelector(".chain-node").addEventListener("click", () => cmpLocate(ev.id));
  return row;
}

function cmpLocate(eid) {
  cmpClose();
  focusEvent(eid);
}

function cmpNum(n) {
  n = Number(n);
  if (!isFinite(n)) return "-";
  if (Math.abs(n) >= 100 || n === 0) return n.toFixed(1);
  return n.toFixed(3).replace(/0+$/, "").replace(/\.$/, "");
}

/* --------------------------------------------------------------- 导出 */

async function cmpExport() {
  if (!cmpState.result) return toast("请先执行一次对比", "error");
  const specA = cmpSideSpec(cmp$("#cmp-side-a").value);
  const specB = cmpSideSpec(cmp$("#cmp-side-b").value);
  const res = await fetch("/api/compare/export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ a: specA, b: specB }),
  });
  if (!res.ok) return toast("导出失败: " + (await res.text()), "error");
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "scenario_comparison.json";
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
  toast("对比结果 JSON 已导出", "ok");
}

/* --------------------------------------------------------------- 开关 */

function cmpOpen() {
  cmp$("#compare-overlay").classList.remove("hidden");
  cmpLoadScenarios();
}
function cmpClose() {
  cmp$("#compare-overlay").classList.add("hidden");
}

cmp$("#btn-compare").addEventListener("click", cmpOpen);
cmp$("#cmp-close").addEventListener("click", cmpClose);
cmp$("#compare-overlay").addEventListener("click", (e) => {
  if (e.target.id === "compare-overlay") cmpClose();
});
cmp$("#cmp-save-current").addEventListener("click", cmpSaveCurrent);
cmp$("#cmp-run").addEventListener("click", cmpRun);
cmp$("#cmp-swap").addEventListener("click", () => {
  const a = cmp$("#cmp-side-a").value, b = cmp$("#cmp-side-b").value;
  cmpSetSide("a", b); cmpSetSide("b", a);
  cmpRenderSelects();
});
cmp$("#cmp-export").addEventListener("click", cmpExport);
cmp$("#cmp-side-a").addEventListener("change", (e) => cmpSetSide("a", e.target.value));
cmp$("#cmp-side-b").addEventListener("change", (e) => cmpSetSide("b", e.target.value));
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !cmp$("#compare-overlay").classList.contains("hidden")) cmpClose();
});
