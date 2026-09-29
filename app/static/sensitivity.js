/* 偏移敏感性扫描抽屉 —— 复用 app.js 的 state / overrides / recompute 与
   compare.js 的抽屉样式（el / esc / toast / focusEvent / shortId / fmtNum / STATUS_TEXT）。 */
"use strict";

const sensState = {
  result: null,
  source: "",
};
const sens$ = (sel) => document.querySelector(sel);

/* --------------------------------------------------------------- 开关 */

function sensOpen() {
  sens$("#sens-overlay").classList.remove("hidden");
  sensFillSources();
}
function sensClose() {
  sens$("#sens-overlay").classList.add("hidden");
}

function sensFillSources() {
  const sources = (state.result && state.result.sources) || [];
  const sel = sens$("#sens-source");
  sel.innerHTML = sources.map((s) =>
    `<option value="${esc(s.id)}">${esc(s.name || s.id)} (${esc(s.id)})</option>`).join("");
  if (!sensState.source || !sources.some((s) => s.id === sensState.source)) {
    sensState.source = sources.length ? sources[0].id : "";
  }
  sel.value = sensState.source;
  sensResetRange();
}

function sensCurrentOffset() {
  const s = ((state.result && state.result.sources) || [])
    .find((x) => x.id === sensState.source);
  return s ? Number(s.effective.clock_offset) : 0;
}

function sensResetRange() {
  const cur = sensCurrentOffset();
  const round3 = (v) => Math.round(v * 1000) / 1000;
  sens$("#sens-lo").value = round3(cur - 120);
  sens$("#sens-hi").value = round3(cur + 120);
}

/* --------------------------------------------------------------- 扫描 */

async function sensRun() {
  if (!sensState.source) return toast("请先选择要扫描的来源", "error");
  const lo = parseFloat(sens$("#sens-lo").value);
  const hi = parseFloat(sens$("#sens-hi").value);
  const hint = sens$("#sens-hint");
  hint.textContent = "扫描中…";
  const res = await fetch("/api/offset-sensitivity", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      source_id: sensState.source, lo, hi,
      overrides: state.overrides || {},
    }),
  });
  hint.textContent = "";
  if (!res.ok) {
    let msg = await res.text();
    try { msg = JSON.parse(msg).detail || msg; } catch { /* 保留原文 */ }
    sensState.result = null;
    sens$("#sens-result").innerHTML = "";
    return toast("扫描失败: " + msg, "error");
  }
  sensState.result = await res.json();
  sensRender();
}

/* 点击某段：把该段中点写入该来源未保存 offset，并触发现有时间线重算。 */
async function sensApply(seg) {
  const sid = sensState.result.source_id;
  state.overrides[sid] = state.overrides[sid] || {};
  state.overrides[sid].clock_offset = seg.midpoint;
  sensClose();
  await recompute();
  toast(`已把 ${sid} 的 offset 设为 ${seg.midpoint}s（未保存调整），时间线已重算`, "ok");
}

/* --------------------------------------------------------------- 渲染 */

function sensRender() {
  const d = sensState.result;
  const box = sens$("#sens-result");
  box.innerHTML = "";
  if (!d) return;
  box.appendChild(sensSummary(d));
  if (d.critical_points.length) box.appendChild(sensPoints(d));
  if (d.cancelled_edges.length) box.appendChild(sensCancelled(d));
  box.appendChild(sensBar(d));
  for (const seg of d.intervals) box.appendChild(sensCard(d, seg));
}

function sensSummary(d) {
  const blk = el("div", "cmp-block");
  blk.innerHTML = `<h3>扫描概要
    <span class="cmp-count">${esc(d.source_name || d.source_id)} · 当前 offset ${fmtNum(d.current_offset)}s</span></h3>`;
  const meta = el("div", "sens-meta");
  meta.innerHTML = `
    <span class="stat">范围 <b>[${fmtNum(d.range.lo)}, ${fmtNum(d.range.hi)}]s</b></span>
    <span class="stat">相关跨来源边 <b>${d.involved_edge_count}</b></span>
    <span class="stat">临界点 <b>${d.critical_points.length}</b></span>
    <span class="stat">稳定区间 <b>${d.intervals.length}</b></span>
    <span class="stat ${d.cancelled_edges.length ? "warn" : ""}">offset 抵消边 <b>${d.cancelled_edges.length}</b></span>`;
  blk.appendChild(meta);
  if (d.notice) blk.appendChild(el("div", "sens-notice", esc(d.notice)));
  return blk;
}

function sensPoints(d) {
  const blk = el("div", "cmp-block");
  blk.innerHTML = `<h3>临界点 <span class="cmp-count">gap_lo / gap_hi 穿越 0 的 offset 取值，按此切分稳定区间</span></h3>`;
  const list = el("div", "sens-points");
  for (const p of d.critical_points) {
    const chip = el("div", "sens-point");
    chip.innerHTML = `<b>${fmtNum(p.offset)}s</b>
      <span class="sens-point-edge">${esc(shortId(p.from))} → ${esc(shortId(p.to))}</span>
      <span class="sens-point-bound">${p.bound === "gap_lo" ? "gap 下界" : "gap 上界"} · ${p.direction === "rising" ? "随 offset 上升" : "随 offset 下降"}</span>`;
    list.appendChild(chip);
  }
  blk.appendChild(list);
  return blk;
}

function sensCancelled(d) {
  const blk = el("div", "cmp-block");
  blk.innerHTML = `<h3>offset 抵消边 <span class="cmp-count">两端同属该来源，整体平移相互抵消，状态不随扫描变化</span></h3>`;
  const list = el("div", "cmp-edges");
  for (const e of d.cancelled_edges) {
    const row = el("div", "cmp-edge-row");
    row.innerHTML = `
      <span class="cmp-edge-nodes chain-node" data-eid="${esc(e.from)}">${esc(shortId(e.from))}</span>
      <span class="chain-arrow">→</span>
      <span class="cmp-edge-nodes chain-node" data-eid="${esc(e.to)}">${esc(shortId(e.to))}</span>
      <span class="cmp-edge-kind">${esc(e.kind)}${e.note ? " · " + esc(e.note) : ""}</span>
      <span class="cmp-edge-st ${e.status}">${STATUS_TEXT[e.status] || e.status}</span>`;
    row.querySelectorAll(".chain-node").forEach((n) =>
      n.addEventListener("click", () => { sensClose(); focusEvent(n.dataset.eid); }));
    list.appendChild(row);
  }
  blk.appendChild(list);
  return blk;
}

function sensBar(d) {
  const blk = el("div", "cmp-block");
  blk.innerHTML = `<h3>稳定区间 <span class="cmp-count">点击任意段：把该段中点写入「${esc(d.source_id)}」未保存 offset 并重算时间线</span></h3>`;
  const bar = el("div", "sens-bar");
  const span = Math.max(d.range.hi - d.range.lo, 1e-9);
  for (const seg of d.intervals) {
    const s = el("div", "sens-seg " + (seg.stats.conflict_count ? "bad" : "ok")
      + (seg.contains_current ? " current" : ""));
    s.style.width = Math.max(((seg.hi - seg.lo) / span) * 100, 2.5) + "%";
    s.title = `offset ∈ [${seg.lo}, ${seg.hi}]s · 冲突 ${seg.stats.conflict_count} · 不可能边 ${seg.stats.impossible_edges} · 中点 ${seg.midpoint}s`;
    s.textContent = fmtNum(seg.midpoint);
    s.addEventListener("click", () => sensApply(seg));
    bar.appendChild(s);
  }
  blk.appendChild(bar);
  const scale = el("div", "sens-bar-scale");
  scale.innerHTML = `<span>${fmtNum(d.range.lo)}s</span><span>◄ offset 扫描范围 ►</span><span>${fmtNum(d.range.hi)}s</span>`;
  blk.appendChild(scale);
  return blk;
}

function sensCard(d, seg) {
  const card = el("div", "cmp-block sens-card" + (seg.contains_current ? " current" : ""));
  card.innerHTML = `
    <div class="sens-card-head">
      <span class="sens-card-title">区间 ${seg.index + 1} · offset ∈ [${fmtNum(seg.lo)}, ${fmtNum(seg.hi)}]s</span>
      ${seg.contains_current ? '<span class="cmp-badge persisted">当前所在区间</span>' : ""}
      <span class="cmp-badge ${seg.stats.conflict_count ? "added" : "removed"}">冲突 ${seg.stats.conflict_count}</span>
      <span class="cmp-badge">不可能边 ${seg.stats.impossible_edges}</span>
      <span class="cmp-badge">歧义组 ${seg.stats.ambiguity_group_count}</span>
      <button class="btn tiny primary sens-apply">✍ 采用中点 ${fmtNum(seg.midpoint)}s</button>
    </div>`;
  const body = el("div", "sens-card-body");
  body.appendChild(sensImpossibleDiff(seg.impossible_edges));
  body.appendChild(sensConflictDiff(seg.conflicts));
  card.appendChild(body);
  card.querySelector(".sens-apply").addEventListener("click", () => sensApply(seg));
  card.querySelectorAll(".chain-node").forEach((n) =>
    n.addEventListener("click", () => { sensClose(); focusEvent(n.dataset.eid); }));
  return card;
}

function sensDiffHead(label, diff) {
  return `<div class="sens-diff-head">${label}
    <span class="cmp-delta added" title="该区间相对当前结果新增">＋${diff.added.length} 新增</span>
    <span class="cmp-delta removed" title="该区间相对当前结果消失">－${diff.removed.length} 消失</span>
    <span class="cmp-delta persisted" title="两侧都存在">＝${diff.persisted.length} 持续</span></div>`;
}

function sensImpossibleDiff(diff) {
  const wrap = el("div", "sens-diff");
  let html = sensDiffHead("不可能边", diff);
  const lines = [];
  for (const it of diff.added) {
    lines.push(`<div class="sens-line added">＋ <code>${esc(it.signature)}</code>
      <span class="sens-gap">gap [${esc(it.edge.gap_lo_text)}, ${esc(it.edge.gap_hi_text)}]</span></div>`);
  }
  for (const it of diff.removed) {
    lines.push(`<div class="sens-line removed">－ <code>${esc(it.signature)}</code></div>`);
  }
  if (!lines.length) lines.push('<div class="sens-line none">与当前结果一致</div>');
  wrap.innerHTML = html + lines.join("");
  return wrap;
}

function sensConflictDiff(diff) {
  const wrap = el("div", "sens-diff");
  let html = sensDiffHead("冲突链", diff);
  const lines = [];
  for (const it of diff.added) lines.push(sensConflictLine("added", "＋", it.signature, it.b));
  for (const it of diff.removed) lines.push(sensConflictLine("removed", "－", it.signature, it.a));
  if (!lines.length) lines.push('<div class="sens-line none">与当前结果一致</div>');
  wrap.innerHTML = html + lines.join("");
  return wrap;
}

function sensConflictLine(cls, mark, sig, f) {
  const kindText = f.kind === "causal_cycle" ? "因果环" : "不可能顺序";
  const nodes = (f.events || []).map((id) =>
    `<span class="chain-node" data-eid="${esc(id)}" title="点击在主时间线定位">${esc(shortId(id))}</span>`
  ).join(" ");
  return `<div class="sens-line ${cls}">${mark} <code>${esc(sig)}</code>
    <span class="cmp-fkind">${kindText}</span> ${nodes}</div>`;
}

/* --------------------------------------------------------------- 绑定 */

sens$("#btn-sensitivity").addEventListener("click", sensOpen);
sens$("#sens-close").addEventListener("click", sensClose);
sens$("#sens-overlay").addEventListener("click", (e) => {
  if (e.target.id === "sens-overlay") sensClose();
});
sens$("#sens-run").addEventListener("click", sensRun);
sens$("#sens-source").addEventListener("change", (e) => {
  sensState.source = e.target.value;
  sensResetRange();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !sens$("#sens-overlay").classList.contains("hidden")) sensClose();
});
