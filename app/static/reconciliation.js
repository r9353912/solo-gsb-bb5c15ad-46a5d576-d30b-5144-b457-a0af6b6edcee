/* 双来源事件对账抽屉 —— 复用 app.js 的 state / overrides / el / esc / toast /
   focusEvent / shortId / colorFor 与 compare.js 的抽屉样式。
   只读：不新增约束、不合并记录；点击事件 ID 回主时间线定位。
   对账完成后可在已配对事件上勾选 1～20 对锚点做 offset 校准：
   采用中点只写入左栏未保存假设并触发既有重算，仍可"放弃调整"。 */
"use strict";

const reconState = {
  result: null,
  anchors: [],              // 勾选的锚点 key（"aID||bID"，对账结果对顺序）
  calibratedSide: "a",      // 待校准方：a | b
  anchorResult: null,       // 最近一次锚点校准结果
};
const recon$ = (sel) => document.querySelector(sel);

/* --------------------------------------------------------------- 开关 */

function reconOpen() {
  recon$("#recon-overlay").classList.remove("hidden");
  reconFillSources();
}
function reconClose() {
  recon$("#recon-overlay").classList.add("hidden");
}

function reconFillSources() {
  const sources = (state.result && state.result.sources) || [];
  const opts = sources.map((s) =>
    `<option value="${esc(s.id)}">${esc(s.name || s.id)} (${esc(s.id)} · ${s.event_count})</option>`).join("");
  const selA = recon$("#recon-source-a");
  const selB = recon$("#recon-source-b");
  const prevA = selA.value, prevB = selB.value;
  selA.innerHTML = opts;
  selB.innerHTML = opts;
  if (!sources.length) return;  // 首屏数据尚未载入：留空，等下次打开再填
  // 默认选两个不同来源（A 第一个、B 第二个）
  selA.value = prevA && sources.some((s) => s.id === prevA)
    ? prevA : (sources[0] && sources[0].id) || "";
  if (prevB && sources.some((s) => s.id === prevB) && prevB !== selA.value) {
    selB.value = prevB;
  } else {
    const other = sources.find((s) => s.id !== selA.value);
    selB.value = other ? other.id : (sources[0] && sources[0].id) || "";
  }
}

/* --------------------------------------------------------------- 对账 */

async function reconRun() {
  const sourceA = recon$("#recon-source-a").value;
  const sourceB = recon$("#recon-source-b").value;
  const tolRaw = recon$("#recon-tol").value;
  const toleranceMs = tolRaw === "" || tolRaw === null ? NaN : Number(tolRaw);
  const hint = recon$("#recon-hint");
  const box = recon$("#recon-result");

  if (!sourceA || !sourceB) { toast("请先在两侧各选择一个来源", "error"); return; }
  if (sourceA === sourceB) { toast("两侧必须选择不同来源", "error"); return; }
  if (!isFinite(toleranceMs) || toleranceMs < 0) {
    toast("容差必须是非负数值（毫秒）", "error"); return;
  }

  hint.textContent = "对账中…";
  box.innerHTML = "";
  box.appendChild(el("div", "cmp-empty", "全局最优配对求解中…"));

  const res = await fetch("/api/source-reconciliation", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    // 携带页面当前（含未保存）的时钟假设；只读，不改写任何数据
    body: JSON.stringify({
      source_a: sourceA,
      source_b: sourceB,
      tolerance_ms: toleranceMs,
      overrides: state.overrides || {},
    }),
  });
  hint.textContent = "";
  if (!res.ok) {
    let msg = await res.text();
    try { msg = JSON.parse(msg).detail || msg; } catch { /* 保留原文 */ }
    reconState.result = null;
    reconState.anchorResult = null;
    box.innerHTML = "";
    box.appendChild(el("div", "cmp-empty", "对账失败: " + esc(msg)));
    toast("对账失败: " + msg, "error");
    return;
  }
  reconState.result = await res.json();
  reconAnchorPrune();   // 重新对账后丢弃已失效的旧锚点（结果对以本次为准）
  reconState.anchorResult = null;
  reconRender();
}

/* --------------------------------------------------------------- 锚点 */

const RECON_MAX_ANCHORS = 20;
const reconPairKey = (p) => `${p.a.id}||${p.b.id}`;

function reconAnchorPrune() {
  // 只保留本次对账结果里仍存在的配对（按 key 集合），顺序对齐当前 pairs
  const d = reconState.result;
  if (!d) { reconState.anchors = []; return; }
  const valid = new Set(d.pairs.map(reconPairKey));
  reconState.anchors = reconState.anchors.filter((k) => valid.has(k));
}

function reconAnchorToggle(key, checked) {
  const i = reconState.anchors.indexOf(key);
  if (checked) {
    if (i >= 0) return true;
    if (reconState.anchors.length >= RECON_MAX_ANCHORS) {
      toast(`最多勾选 ${RECON_MAX_ANCHORS} 对锚点`, "error");
      return false;  // 调用方负责回退 checkbox 视觉状态
    }
    reconState.anchors.push(key);
  } else if (i >= 0) {
    reconState.anchors.splice(i, 1);
  }
  reconState.anchorResult = null;  // 选择变化，旧校准结果作废
  reconAnchorRefresh();
  return true;
}

async function reconAnchorRun() {
  const d = reconState.result;
  if (!d) return;
  if (!reconState.anchors.length) { toast("请先勾选至少 1 对已配对事件作为锚点", "error"); return; }
  const byKey = new Map(d.pairs.map((p) => [reconPairKey(p), p]));
  const anchors = reconState.anchors
    .map((k) => byKey.get(k))
    .filter(Boolean)
    .map((p) => ({ a: p.a.id, b: p.b.id }));
  if (anchors.length !== reconState.anchors.length) {
    reconAnchorPrune();
    return reconAnchorRefresh();
  }
  const btn = recon$("#recon-anchor-run");
  btn.disabled = true;
  btn.textContent = "校准中…";
  const res = await fetch("/api/anchor-calibration", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    // 区间复用页面当前（含未保存）的时钟假设；只读，不改写任何数据
    body: JSON.stringify({
      source_a: d.sides.a.source_id,
      source_b: d.sides.b.source_id,
      calibrated_side: reconState.calibratedSide,
      anchors,
      overrides: state.overrides || {},
    }),
  });
  btn.disabled = false;
  btn.textContent = "▶ 计算可行 offset";
  if (!res.ok) {
    let msg = await res.text();
    try { msg = JSON.parse(msg).detail || msg; } catch { /* 保留原文 */ }
    reconState.anchorResult = null;
    reconAnchorRefresh(msg);
    toast("锚点校准失败: " + msg, "error");
    return;
  }
  reconState.anchorResult = await res.json();
  reconAnchorRefresh();
}

/* 采用中点：仅把待校准来源的未保存 offset 改为中点，走既有重算；仍可放弃。 */
async function reconAnchorApply() {
  const r = reconState.anchorResult;
  if (!r || !r.feasible) return;
  const sid = r.calibrated_source_id;
  const mid = r.intersection.absolute_offset_midpoint_seconds;
  state.overrides[sid] = state.overrides[sid] || {};
  state.overrides[sid].clock_offset = mid;
  reconClose();
  await recompute();
  toast(`已把 ${sid} 的 offset 设为 ${mid}s（未保存调整），时间线已重算；可在左栏放弃调整`, "ok");
}

/* --------------------------------------------------------------- 渲染 */

function reconTime(ms) {
  // 整数毫秒 epoch → HH:MM:SS.mmm（UTC），与后端 fmt 对齐仅取时间片段
  const d = new Date(ms);
  const p = (n, l) => String(n).padStart(l, "0");
  return `${p(d.getUTCHours(), 2)}:${p(d.getUTCMinutes(), 2)}:${p(d.getUTCSeconds(), 2)}.${p(d.getUTCMilliseconds(), 3)}`;
}

function reconMsText(v) { return `${v >= 0 ? "+" : ""}${v} ms`; }

function reconRender() {
  const d = reconState.result;
  const box = recon$("#recon-result");
  box.innerHTML = "";
  if (!d) return;
  box.appendChild(reconSummary(d));
  box.appendChild(reconAnchorBlock(d));
  box.appendChild(reconPairsBlock(d));
  box.appendChild(reconUnmatchedBlock(d, "a"));
  box.appendChild(reconUnmatchedBlock(d, "b"));
  // 点击任意事件 ID：关抽屉并在主时间线定位
  box.querySelectorAll("[data-eid]").forEach((n) =>
    n.addEventListener("click", () => { reconClose(); focusEvent(n.dataset.eid); }));
  reconAnchorRefresh();
}

function reconSummary(d) {
  const s = d.stats;
  const side = d.sides;
  const dirty = Object.keys(state.overrides || {}).length > 0;
  const blk = el("div", "cmp-block");
  blk.innerHTML = `
    <h3>对账结果
      <span class="cmp-count">${esc(d.summary)}</span></h3>
    <div class="sens-meta">
      <span class="stat">A · ${esc(side.a.source_name || side.a.source_id)} <b>${s.events_a}</b></span>
      <span class="stat">B · ${esc(side.b.source_name || side.b.source_id)} <b>${s.events_b}</b></span>
      <span class="stat">容差 <b>±${d.tolerance_ms} ms</b></span>
      <span class="stat">候选边 <b>${s.candidate_edges}</b></span>
      <span class="stat ${s.matched_pairs ? "warn" : ""}">配对 <b>${s.matched_pairs}</b></span>
      <span class="stat">A 未匹配 <b>${s.unmatched_a}</b></span>
      <span class="stat">B 未匹配 <b>${s.unmatched_b}</b></span>
      <span class="stat">中点差绝对值之和 <b>${s.mid_delta_abs_sum_ms} ms</b></span>
      ${dirty ? '<span class="stat warn">含未保存假设</span>' : ""}
    </div>
    <div class="sens-notice" style="margin-top:8px">${esc(d.matching.rule)}；${esc(d.matching.rounding)}；每侧上限 ${d.matching.limit_per_side} 条。</div>`;
  return blk;
}

function reconEventCell(ev, cls) {
  const color = colorFor(ev.source_id);
  return `
    <div class="recon-cell ${cls}">
      <div class="recon-cell-id">
        <span class="chain-node" data-eid="${esc(ev.id)}" title="点击在主时间线定位">${esc(shortId(ev.id))}</span>
        <span class="tag actor">👤 ${esc(ev.actor || "")}</span>
      </div>
      <div class="recon-cell-summary" title="${esc(ev.summary)}">${esc(ev.summary || "")}</div>
      <div class="recon-cell-range" style="border-color:${color}55">
        <span title="闭区间 [lo, hi]（整数毫秒）">[${esc(reconTime(ev.lo_ms))} ~ ${esc(reconTime(ev.hi_ms))}]</span>
        <span class="recon-mid" title="区间中点">mid ${esc(reconTime(ev.mid_ms))}</span>
      </div>
    </div>`;
}

function reconPairsBlock(d) {
  const blk = el("div", "cmp-block");
  const n = d.pairs.length;
  blk.innerHTML = `<h3>已配对事件
    <span class="cmp-count">${n} 对 · 按 A 侧事件 ID 稳定排序；勾选左侧复选框可加入锚点校准（1～${RECON_MAX_ANCHORS} 对）</span></h3>`;
  if (!n) {
    blk.appendChild(el("div", "cmp-empty", "没有满足条件的配对（actor 相同 / 类型相同 / 闭区间间距 ≤ 容差）；有配对后才能做锚点校准"));
    return blk;
  }
  const list = el("div", "recon-pairs");
  for (const p of d.pairs) {
    const key = reconPairKey(p);
    const checked = reconState.anchors.includes(key);
    const row = el("div", "recon-pair");
    const gapCls = p.closed_gap_ms === 0 ? "touch" : "gap";
    row.innerHTML = `
      <label class="recon-anchor-pick" title="勾选为锚点（确认这对事件是同一真实动作）">
        <input type="checkbox" class="recon-anchor-cb" data-key="${esc(key)}" ${checked ? "checked" : ""}>
        <span>锚点</span>
      </label>
      ${reconEventCell(p.a, "a")}
      <div class="recon-delta">
        <div class="recon-delta-main ${p.mid_delta_ms >= 0 ? "pos" : "neg"}"
             title="B 区间中点 − A 区间中点（整数毫秒）">${esc(reconMsText(p.mid_delta_ms))}</div>
        <div class="recon-delta-abs">|Δ| ${p.mid_delta_abs_ms} ms</div>
        <div class="recon-delta-gap ${gapCls}"
             title="两侧闭区间之间的间距（相接/重叠为 0）">闭区间间距 ${p.closed_gap_ms} ms</div>
        <div class="recon-delta-type">${esc(p.event_type)}</div>
      </div>
      ${reconEventCell(p.b, "b")}`;
    row.querySelector(".recon-anchor-cb").addEventListener("change", (e) => {
      const ok = reconAnchorToggle(e.target.dataset.key, e.target.checked);
      if (ok === false) e.target.checked = false;  // 超上限：回退视觉，不整页重绘
    });
    list.appendChild(row);
  }
  blk.appendChild(list);
  return blk;
}

/* ----------------------------------------------------- 锚点校准区块 */

function reconAnchorBlock(d) {
  const blk = el("div", "cmp-block anchor-block");
  const n = d.pairs.length;
  blk.innerHTML = `
    <h3>🎯 锚点校准
      <span class="cmp-count">勾选 1～${RECON_MAX_ANCHORS} 对已配对且确认同一动作的事件，指定待校准方；
        固定漂移与不确定度，求使每对锚点闭区间相交的统一 offset 增量范围，再取交集</span></h3>
    <div class="anchor-toolbar">
      <span class="anchor-tool-label">待校准方</span>
      <label class="anchor-radio"><input type="radio" name="anchor-cal-side" value="a">
        A · ${esc(d.sides.a.source_name || d.sides.a.source_id)}</label>
      <label class="anchor-radio"><input type="radio" name="anchor-cal-side" value="b">
        B · ${esc(d.sides.b.source_name || d.sides.b.source_id)}</label>
      <span class="anchor-side-hint">另一方作为参照（只整体平移待校准方 offset，不动参照方）</span>
      <span class="anchor-count">已选锚点 <b>${reconState.anchors.length}</b>/${RECON_MAX_ANCHORS}</span>
      <button class="btn tiny primary" id="recon-anchor-run" ${n ? "" : "disabled"}>▶ 计算可行 offset</button>
    </div>
    <div class="anchor-toolbar-sub">复用当前含未保存调整的事件时间区间；采用中点只更新左栏<b>未保存</b>假设并重算时间线，不会保存，仍可"放弃调整"。</div>
    <div id="recon-anchor-result"></div>`;
  const radios = blk.querySelectorAll('input[name="anchor-cal-side"]');
  radios.forEach((r) => {
    r.checked = r.value === reconState.calibratedSide;
    r.addEventListener("change", (e) => {
      reconState.calibratedSide = e.target.value;
      reconState.anchorResult = null;
      reconAnchorRefresh();
    });
  });
  blk.querySelector("#recon-anchor-run").addEventListener("click", reconAnchorRun);
  return blk;
}

function reconAnchorRefresh(errorMsg) {
  const count = reconState.anchors.length;
  const countEl = document.querySelector(".anchor-count");
  if (countEl) countEl.innerHTML = `已选锚点 <b>${count}</b>/${RECON_MAX_ANCHORS}`;
  const runBtn = document.getElementById("recon-anchor-run");
  if (runBtn) runBtn.disabled = !reconState.result || !reconState.result.pairs.length;
  // 同步 radio 视觉
  document.querySelectorAll('input[name="anchor-cal-side"]').forEach((r) => {
    r.checked = r.value === reconState.calibratedSide;
  });
  const box = document.getElementById("recon-anchor-result");
  if (!box) return;
  box.innerHTML = "";
  if (errorMsg) {
    box.appendChild(el("div", "anchor-error", "✕ " + esc(errorMsg)));
    return;
  }
  const r = reconState.anchorResult;
  if (!r) {
    if (count) {
      box.appendChild(el("div", "sens-notice",
        `已勾选 ${count} 对锚点：选择待校准方后点"▶ 计算可行 offset"。`));
    }
    return;
  }
  if (r.feasible) box.appendChild(reconAnchorFeasible(r));
  else box.appendChild(reconAnchorConflict(r));
  box.appendChild(reconAnchorDetails(r));
  box.querySelectorAll("[data-eid]").forEach((n) =>
    n.addEventListener("click", () => { reconClose(); focusEvent(n.dataset.eid); }));
}

function reconAnchorFeasible(r) {
  const ix = r.intersection;
  const blk = el("div", "anchor-feasible");
  const apply = `<button class="btn tiny primary" id="recon-anchor-apply"
      title="把绝对 offset 中点写入「${esc(r.calibrated_source_id)}」的左栏未保存假设并重算时间线">
      ✍ 采用中点 ${fmtNum(ix.absolute_offset_midpoint_seconds)}s（${ix.absolute_offset_midpoint_ms} ms）</button>`;
  blk.innerHTML = `
    <div class="anchor-result-head">
      <span class="anchor-badge ok">✓ ${r.anchor_count} 对锚点存在公共可行域</span>
      ${apply}
    </div>
    <div class="anchor-lines">
      <div class="anchor-line">待校准方 <b>${esc(r.calibrated_source_name || r.calibrated_source_id)}</b>
        （当前生效 offset <b>${fmtNum(r.current_offset_seconds)}s</b>），参照方
        <b>${esc(r.reference_source_name || r.reference_source_id)}</b>；漂移与不确定度固定不变。</div>
      <div class="anchor-line">允许的 offset <b>增量</b>范围（闭区间，整数毫秒）：
        <code>[${ix.delta_range_ms.lo}, ${ix.delta_range_ms.hi}] ms</code>
        （${fmtNum(ix.delta_range_seconds.lo)}s ～ ${fmtNum(ix.delta_range_seconds.hi)}s）</div>
      <div class="anchor-line strong">允许的<b>绝对 offset</b>范围：
        <code>[${fmtNum(ix.absolute_offset_range_seconds.lo)}, ${fmtNum(ix.absolute_offset_range_seconds.hi)}] s</code>
        （[${ix.absolute_offset_range_ms.lo}, ${ix.absolute_offset_range_ms.hi}] ms）</div>
      <div class="anchor-line">增量中点 <code>${fmtNum(ix.delta_midpoint_seconds)}s</code>
        （${ix.delta_midpoint_ms} ms，0.5 ms 粒度）→ 绝对 offset 中点
        <code>${fmtNum(ix.absolute_offset_midpoint_seconds)}s</code>。</div>
      <div class="anchor-line muted">${esc(r.notice)}</div>
    </div>`;
  blk.querySelector("#recon-anchor-apply").addEventListener("click", reconAnchorApply);
  return blk;
}

function reconAnchorConflict(r) {
  const c = r.conflict;
  const lo = c.lower_anchor.delta_range_ms;
  const hi = c.upper_anchor.delta_range_ms;
  const blk = el("div", "anchor-conflict");
  blk.innerHTML = `
    <div class="anchor-result-head">
      <span class="anchor-badge bad">✕ ${r.anchor_count} 对锚点的增量范围交集为空</span>
    </div>
    <div class="anchor-lines">
      <div class="anchor-line">${esc(r.summary)}</div>
    </div>
    ${reconAnchorConflictCard("下界锚点（提供最大下界，要求增量 ≥）", c.lower_anchor, "lo", lo.lo, lo.hi)}
    ${reconAnchorConflictCard("上界锚点（提供最小上界，要求增量 ≤）", c.upper_anchor, "hi", hi.lo, hi.hi)}`;
  return blk;
}

function reconAnchorConflictCard(title, a, which, loMs, hiMs) {
  const cls = which === "lo" ? "lower" : "upper";
  return `
    <div class="anchor-conf-card ${cls}">
      <div class="anchor-conf-title">${esc(title)} · 允许增量
        <code>[${loMs}, ${hiMs}] ms</code></div>
      <div class="anchor-mini-pair">
        <span class="chain-node" data-eid="${esc(a.calibrated_event.id)}"
          title="待校准方事件">${esc(shortId(a.calibrated_event.id))}（校准）</span>
        <span class="chain-arrow">↔</span>
        <span class="chain-node" data-eid="${esc(a.reference_event.id)}"
          title="参照方事件">${esc(shortId(a.reference_event.id))}（参照）</span>
        <span class="anchor-conf-type">${esc(a.calibrated_event.event_type)}</span>
        <span class="tag actor">👤 ${esc(a.calibrated_event.actor || "")}</span>
      </div>
    </div>`;
}

function reconAnchorDetails(r) {
  const blk = el("div", "cmp-block anchor-details");
  blk.innerHTML = `<h3>逐锚点增量范围
    <span class="cmp-count">按请求顺序；δ 使待校准方区间整体平移 -δ 后与参照方闭区间相交（含边界相接）</span></h3>`;
  const list = el("div", "anchor-rows");
  const dLo = (it) => it.delta_range_ms.lo;
  const dHi = (it) => it.delta_range_ms.hi;
  const maxLo = Math.max(...r.anchors.map(dLo));
  const minHi = Math.min(...r.anchors.map(dHi));
  for (const it of r.anchors) {
    const isLo = r.feasible && dLo(it) === maxLo;
    const isHi = r.feasible && dHi(it) === minHi;
    const isClo = !r.feasible && r.conflict.lower_anchor.index === it.index;
    const isChi = !r.feasible && r.conflict.upper_anchor.index === it.index;
    const row = el("div", "anchor-row" + ((isLo || isClo) ? " bound-lower" : "")
      + ((isHi || isChi) ? " bound-upper" : ""));
    row.innerHTML = `
      <span class="anchor-row-id chain-node" data-eid="${esc(it.a.id)}"
        title="A 侧事件，点击在主时间线定位">${esc(shortId(it.a.id))}</span>
      <span class="chain-arrow">↔</span>
      <span class="anchor-row-id chain-node" data-eid="${esc(it.b.id)}"
        title="B 侧事件，点击在主时间线定位">${esc(shortId(it.b.id))}</span>
      <span class="anchor-row-range" title="使两侧闭区间相交的 offset 增量范围">
        δ ∈ [${dLo(it)}, ${dHi(it)}] ms
      </span>
      <span class="anchor-row-flags">
        ${it.intersects_currently ? '<span class="anchor-tag ok">当前区间已相交</span>'
          : '<span class="anchor-tag warn">当前区间未相交</span>'}
        ${isLo ? '<span class="anchor-tag lower">决定交集下界</span>' : ""}
        ${isHi ? '<span class="anchor-tag upper">决定交集上界</span>' : ""}
        ${isClo ? '<span class="anchor-tag lower">冲突下界锚点</span>' : ""}
        ${isChi ? '<span class="anchor-tag upper">冲突上界锚点</span>' : ""}
      </span>`;
    list.appendChild(row);
  }
  blk.appendChild(list);
  return blk;
}

function reconUnmatchedBlock(d, which) {
  const list = which === "a" ? d.unmatched_a : d.unmatched_b;
  const side = d.sides[which];
  const blk = el("div", "cmp-block");
  blk.innerHTML = `<h3>${which.toUpperCase()} 侧未匹配事件
    <span class="cmp-count">${esc(side.source_name || side.source_id)} · ${list.length} 条 · 按事件 ID 排序（含 actor 为空 / 类型无对应 / 未获最优配对）</span></h3>`;
  if (!list.length) {
    blk.appendChild(el("div", "cmp-empty", "✓ 全部配对"));
    return blk;
  }
  const wrap = el("div", "recon-unmatched");
  for (const ev of list) {
    const row = el("div", "recon-um-row");
    const noActor = !ev.actor;
    row.innerHTML = `
      <span class="chain-node" data-eid="${esc(ev.id)}" title="点击在主时间线定位">${esc(shortId(ev.id))}</span>
      <span class="recon-um-type">${esc(ev.event_type)}</span>
      <span class="tag actor">👤 ${esc(ev.actor || "（空）")}</span>
      <span class="recon-um-summary" title="${esc(ev.summary)}">${esc(ev.summary || "")}</span>
      <span class="recon-um-range" title="闭区间中点">[mid ${esc(reconTime(ev.mid_ms))}]</span>
      ${noActor ? '<span class="tag amb" title="actor 为空，不参与配对">actor 空</span>' : ""}`;
    wrap.appendChild(row);
  }
  blk.appendChild(wrap);
  return blk;
}

/* --------------------------------------------------------------- 绑定 */

recon$("#btn-reconcile").addEventListener("click", reconOpen);
recon$("#recon-close").addEventListener("click", reconClose);
recon$("#recon-run").addEventListener("click", reconRun);
recon$("#recon-rerun").addEventListener("click", reconRun);
recon$("#recon-source-a").addEventListener("change", () => {
  // 避免两侧相同：若撞了，自动切到另一个来源
  const a = recon$("#recon-source-a"), b = recon$("#recon-source-b");
  if (a.value === b.value) {
    const other = [...b.options].find((o) => o.value !== a.value);
    if (other) b.value = other.value;
  }
});
recon$("#recon-source-b").addEventListener("change", () => {
  const a = recon$("#recon-source-a"), b = recon$("#recon-source-b");
  if (a.value === b.value) {
    const other = [...a.options].find((o) => o.value !== b.value);
    if (other) a.value = other.value;
  }
});
recon$("#recon-tol").addEventListener("keydown", (e) => {
  if (e.key === "Enter") reconRun();
});
recon$("#recon-overlay").addEventListener("click", (e) => {
  if (e.target.id === "recon-overlay") reconClose();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !recon$("#recon-overlay").classList.contains("hidden")) reconClose();
});
