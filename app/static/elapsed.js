/* 两事件耗时精确界审计抽屉 —— 复用 app.js 的 state / overrides / el / esc /
   toast / focusEvent / shortId / fmtNum / colorFor。
   只读：携带页面未保存假设，不保存假设、不改写数据，不受页面筛选影响。 */
"use strict";

const elapsedState = {
  events: [],            // 全量事件（打开时无筛选拉取，不受页面筛选影响）
  result: null,
  shown: { minimum: 0, maximum: 0 },
};
const ELAPSED_PAGE = 100;
const el$ = (sel) => document.querySelector(sel);

/* --------------------------------------------------------------- 开关 */

async function elapsedOpen() {
  el$("#elapsed-overlay").classList.remove("hidden");
  el$("#elapsed-result").innerHTML = "";
  el$("#elapsed-result").appendChild(el("div", "cmp-empty", "载入事件列表…"));
  // 全量事件（不带任何筛选），保证与页面筛选无关
  try {
    const res = await fetch("/api/recompute", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ overrides: state.overrides || {} }),
    });
    if (!res.ok) throw new Error(await res.text());
    elapsedState.events = (await res.json()).events || [];
  } catch (e) {
    elapsedState.events = [];
  }
  elapsedFillEvents();
  if (!elapsedState.events.length) {
    el$("#elapsed-result").innerHTML = "";
    el$("#elapsed-result").appendChild(
      el("div", "cmp-empty", "暂无可审计事件，请先导入 JSON。"));
  }
}

function elapsedClose() {
  el$("#elapsed-overlay").classList.add("hidden");
}

function elapsedFillEvents() {
  const opt = (e) => {
    const label = `${e.id} · ${e.est_text.slice(11, 23)} · `
      + `${e.source_name || e.source_id} · ${e.summary || e.event_type || ""}`;
    return `<option value="${esc(e.id)}">${esc(label)}</option>`;
  };
  const html = elapsedState.events.map(opt).join("");
  const sSel = el$("#elapsed-start");
  const eSel = el$("#elapsed-end");
  const prevS = sSel.value, prevE = eSel.value;
  sSel.innerHTML = html;
  eSel.innerHTML = html;
  const ids = elapsedState.events.map((x) => x.id);
  sSel.value = ids.includes(prevS) ? prevS : (ids[0] || "");
  eSel.value = ids.includes(prevE) && prevE !== sSel.value
    ? prevE
    : (ids[1] !== undefined ? ids[1] : (ids[0] || ""));
}

/* --------------------------------------------------------------- 计算 */

async function elapsedRun() {
  const start_event = el$("#elapsed-start").value;
  const end_event = el$("#elapsed-end").value;
  const thrRaw = el$("#elapsed-threshold").value;
  const threshold = thrRaw === "" || thrRaw === null ? NaN : Number(thrRaw);
  const hint = el$("#elapsed-hint");
  const box = el$("#elapsed-result");

  if (!start_event || !end_event) { toast("请选择起点与终点事件", "error"); return; }
  if (start_event === end_event) { toast("起点与终点必须是不同事件", "error"); return; }
  if (!isFinite(threshold) || threshold < 0) {
    toast("阈值必须是非负有限数值（秒）", "error"); return;
  }

  hint.textContent = "联立求解中…";
  box.innerHTML = "";
  box.appendChild(el("div", "cmp-empty",
    "在全量事件区间 × 全部约束上求精确耗时界…"));

  const res = await fetch("/api/elapsed-time-bounds", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      start_event, end_event,
      threshold_seconds: threshold,
      overrides: state.overrides || {},
    }),
  });
  hint.textContent = "";
  if (!res.ok) {
    let msg = await res.text();
    try { msg = JSON.parse(msg).detail || msg; } catch { /* 保留原文 */ }
    elapsedState.result = null;
    box.innerHTML = "";
    box.appendChild(el("div", "cmp-empty", "审计被拒绝: " + esc(msg)));
    toast("审计失败: " + msg, "error");
    return;
  }
  elapsedState.result = await res.json();
  elapsedState.shown = { minimum: ELAPSED_PAGE, maximum: ELAPSED_PAGE };
  elapsedRender();
}

/* --------------------------------------------------------------- 渲染 */

const VERDICT_META = {
  must_exceed: { cls: "bad", icon: "✗", label: "必超" },
  must_not_exceed: { cls: "ok", icon: "✓", label: "必不超" },
  possible_exceed: { cls: "warn", icon: "？", label: "可能超" },
};

function elapsedRender() {
  const d = elapsedState.result;
  const box = el$("#elapsed-result");
  box.innerHTML = "";
  if (!d) return;
  if (!d.feasible) {
    box.appendChild(elapsedInfeasibleBlock(d));
    return;
  }
  box.appendChild(elapsedVerdictBlock(d));
  box.appendChild(elapsedBoundsBlock(d));
  box.appendChild(elapsedWitnessBlock(d, "minimum"));
  box.appendChild(elapsedWitnessBlock(d, "maximum"));
}

function elapsedInfeasibleBlock(d) {
  const blk = el("div", "cmp-block");
  blk.appendChild(el("div", "feas-verdict bad", `
    <div class="feas-verdict-head">
      <span class="feas-verdict-icon">✗</span>
      <div>
        <div class="feas-verdict-title">全局时序无解 · 不输出耗时结论</div>
        <div class="feas-verdict-sub">${esc(d.summary)}</div>
      </div>
    </div>`));
  const w = d.witness;
  const chain = el("div", "chain feas-chain");
  w.steps.forEach((s, i) => {
    if (i === 0) chain.appendChild(elapsedNode(s.from));
    chain.innerHTML += `<span class="feas-hop${s.individually_feasible ? "" : " bad"}">
      <span class="chain-arrow ${s.individually_feasible ? "" : "bad"}">→</span>
      <span class="feas-hop-cid">#${esc(s.constraint_id)}${s.individually_feasible ? "" : " ⚠"}</span>
    </span>`;
    chain.appendChild(elapsedNode(s.to, !s.individually_feasible));
  });
  const note = el("div", "feas-note "
    + (w.all_edges_individually_feasible ? "ok" : "warn"));
  note.textContent = w.explanation;
  const inner = el("div", "cmp-block");
  inner.innerHTML = `<h3>现有矛盾见证
    <span class="cmp-count">起点下界 ${esc(w.head.text)} &gt; 终点上界 ${esc(w.tail.text)}
    （超出 ${esc(w.overshoot_text)}）</span></h3>`;
  inner.appendChild(chain);
  inner.appendChild(note);
  const wrap = el("div");
  wrap.appendChild(blk);
  wrap.appendChild(inner);
  wrap.querySelectorAll(".chain-node").forEach((n) =>
    n.addEventListener("click", () => { elapsedClose(); focusEvent(n.dataset.eid); }));
  return wrap;
}

function elapsedNode(eid, bad) {
  const n = el("span", "chain-node" + (bad ? " bad" : ""));
  n.dataset.eid = eid;
  n.textContent = shortId(eid);
  n.title = "点击在主时间线定位";
  return n;
}

function elapsedVerdictBlock(d) {
  const meta = VERDICT_META[d.verdict] || VERDICT_META.possible_exceed;
  const dirty = Object.keys(state.overrides || {}).length > 0;
  const blk = el("div", "cmp-block feas-verdict " + meta.cls);
  blk.innerHTML = `
    <div class="feas-verdict-head">
      <span class="feas-verdict-icon">${meta.icon}</span>
      <div>
        <div class="feas-verdict-title">${esc(shortId(d.end_event))} 相对
          ${esc(shortId(d.start_event))} 的耗时：${meta.label}阈值
          ${esc(d.threshold_text)}</div>
        <div class="feas-verdict-sub">${esc(d.summary)}
          ${dirty ? '<span class="tag amb">含未保存调整</span>' : ""}</div>
      </div>
    </div>
    <div class="sens-meta">
      <span class="stat">事件 <b>${d.stats.event_count}</b></span>
      <span class="stat">参与约束 <b>${d.stats.constraint_count}</b></span>
      ${d.stats.skipped_constraint_count
        ? `<span class="stat warn">悬空约束已忽略 <b>${d.stats.skipped_constraint_count}</b></span>` : ""}
      <span class="stat ${d.stats.cycle_count ? "warn" : ""}">因果环 <b>${d.stats.cycle_count}</b></span>
    </div>`;
  return blk;
}

function elapsedBoundsBlock(d) {
  const b = d.bounds;
  const tightened = b.naive_minimum_seconds !== b.minimum_seconds
    || b.naive_maximum_seconds !== b.maximum_seconds;
  let reachHint = "两事件之间没有任一方向的约束路径，耗时界仅由各自闭区间决定（可为负）";
  if (b.same_component) reachHint = "两事件在同一约束环（SCC）内：环允许同刻，耗时恒为 0";
  else if (b.start_reaches_end && b.end_reaches_start)
    reachHint = "两事件互相可达（同环），耗时恒为 0";
  else if (b.start_reaches_end)
    reachHint = "存在 起点 → 终点 的约束路径：终点不早于起点，最小耗时 ≥ 0";
  else if (b.end_reaches_start)
    reachHint = "存在 终点 → 起点 的约束路径：终点不晚于起点，最大耗时 ≤ 0";

  const blk = el("div", "cmp-block elapsed-bounds");
  blk.innerHTML = `
    <h3>精确耗时区间（终点 − 起点，秒）
      <span class="cmp-count">在所有可行时间分配上的最小 / 最大值，含达到端点的全量见证</span></h3>
    <div class="elapsed-bound-grid">
      <div class="elapsed-bound min">
        <span class="elapsed-bound-label">最小耗时</span>
        <span class="elapsed-bound-val">${fmtNum(b.minimum_seconds)} s</span>
        <span class="elapsed-bound-h">${esc(b.minimum_text)}</span>
      </div>
      <div class="elapsed-bound thr">
        <span class="elapsed-bound-label">阈值</span>
        <span class="elapsed-bound-val">${fmtNum(d.threshold_seconds)} s</span>
        <span class="elapsed-bound-h">${esc(d.threshold_text)}</span>
      </div>
      <div class="elapsed-bound max">
        <span class="elapsed-bound-label">最大耗时</span>
        <span class="elapsed-bound-val">${fmtNum(b.maximum_seconds)} s</span>
        <span class="elapsed-bound-h">${esc(b.maximum_text)}</span>
      </div>
    </div>
    <div class="elapsed-naive">朴素两区间直接相减：
      <b>[${fmtNum(b.naive_minimum_seconds)}, ${fmtNum(b.naive_maximum_seconds)}] s</b>
      ${tightened
        ? '<span class="tag ok">约束已把界收窄，勿用朴素结果</span>'
        : '<span class="tag amb">无额外约束路径，二者相同</span>'}
    </div>
    <div class="elapsed-reach">🔗 ${esc(reachHint)}</div>`;
  return blk;
}

function elapsedWitnessBlock(d, kind) {
  const w = d[kind + "_witness"];
  const title = kind === "minimum" ? "最小耗时见证" : "最大耗时见证";
  const blk = el("div", "cmp-block");
  blk.innerHTML = `<h3>${title}
    <span class="cmp-count">达到 ${esc(w.elapsed_text)} 的一版完整可行分配：每个事件取其闭区间内一个时刻、满足全部约束；点击行在主时间线定位</span></h3>`;

  const endpoints = el("div", "elapsed-endpoints");
  endpoints.innerHTML = `
    <span class="elapsed-ep start">起点 <span class="chain-node" data-eid="${esc(w.start.id)}">${esc(shortId(w.start.id))}</span>
      t = <b>${esc(w.start.time_text.slice(11, 23))}</b>
      [区间 ${esc(w.start.lo_text.slice(11, 23))} ~ ${esc(w.start.hi_text.slice(11, 23))}]</span>
    <span class="elapsed-ep-arrow">—</span>
    <span class="elapsed-ep end">终点 <span class="chain-node" data-eid="${esc(w.end.id)}">${esc(shortId(w.end.id))}</span>
      t = <b>${esc(w.end.time_text.slice(11, 23))}</b>
      [区间 ${esc(w.end.lo_text.slice(11, 23))} ~ ${esc(w.end.hi_text.slice(11, 23))}]</span>
    <span class="elapsed-ep-delta">差值 <b>${fmtNum(w.elapsed)} s</b>（${esc(w.elapsed_text)}）</span>`;
  blk.appendChild(endpoints);

  const list = el("div", "feas-list");
  list.className = "feas-list elapsed-witness-list";
  list.dataset.kind = kind;
  blk.appendChild(list);
  const more = el("button", "btn tiny ghost feas-more");
  blk.appendChild(more);
  more.addEventListener("click", () => {
    elapsedState.shown[kind] += ELAPSED_PAGE;
    elapsedRenderWitnessRows(kind);
  });
  setTimeout(() => elapsedRenderWitnessRows(kind), 0);
  blk.querySelectorAll(".chain-node").forEach((n) =>
    n.addEventListener("click", () => { elapsedClose(); focusEvent(n.dataset.eid); }));
  return blk;
}

function elapsedRenderWitnessRows(kind) {
  const d = elapsedState.result;
  if (!d || !d.feasible) return;
  const block = document.querySelector(`.elapsed-witness-list[data-kind="${kind}"]`);
  if (!block) return;
  const moreBtn = block.parentElement.querySelector(".feas-more");
  const w = d[kind + "_witness"];
  const limit = elapsedState.shown[kind];
  const slice = w.events.slice(0, limit);
  const frag = document.createDocumentFragment();
  slice.forEach((e) => {
    const isS = e.id === w.start.id;
    const isE = e.id === w.end.id;
    const role = isS ? " 起点" : (isE ? " 终点" : "");
    const row = el("div", "feas-row" + (e.in_cycle ? " in-cycle" : "")
      + (role ? " is-endpoint" : ""));
    row.innerHTML = `
      <span class="feas-row-time">${esc(e.time_text.slice(11, 23))}</span>
      <span class="chain-node feas-row-id" data-eid="${esc(e.id)}">${esc(shortId(e.id))}</span>
      <span class="feas-row-src">${esc(e.source_id)}</span>
      <span class="feas-row-summary">${esc(e.summary || "")}${role ? ` <span class="tag ${isS ? "ok" : "warn"}">${role.trim()}</span>` : ""}</span>
      <span class="feas-row-range" title="真实时间闭区间 [lo, hi]；距上界余量">
        [${esc(e.lo_text.slice(11, 23))} ~ ${esc(e.hi_text.slice(11, 23))}] · 余量 ${fmtNum(e.slack_to_hi)}s
        ${e.in_cycle ? '<span class="tag amb" title="环内事件同刻">环</span>' : ""}
      </span>`;
    row.querySelector(".feas-row-id").addEventListener("click", () => {
      elapsedClose(); focusEvent(e.id);
    });
    frag.appendChild(row);
  });
  block.innerHTML = "";
  block.appendChild(frag);
  if (limit >= w.events.length) {
    moreBtn.classList.add("hidden");
    moreBtn.textContent = "";
  } else {
    moreBtn.classList.remove("hidden");
    moreBtn.textContent = `显示更多（${limit} / ${w.events.length}）`;
  }
}

/* --------------------------------------------------------------- 绑定 */

el$("#btn-elapsed").addEventListener("click", elapsedOpen);
el$("#elapsed-close").addEventListener("click", elapsedClose);
el$("#elapsed-run").addEventListener("click", elapsedRun);
el$("#elapsed-overlay").addEventListener("click", (e) => {
  if (e.target.id === "elapsed-overlay") elapsedClose();
});
el$("#elapsed-start").addEventListener("change", () => {
  // 阻止起终点选成同一事件：若相同，把终点挪到另一个
  if (el$("#elapsed-end").value === el$("#elapsed-start").value) {
    const ids = elapsedState.events.map((x) => x.id);
    const other = ids.find((id) => id !== el$("#elapsed-start").value);
    if (other) el$("#elapsed-end").value = other;
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !el$("#elapsed-overlay").classList.contains("hidden"))
    elapsedClose();
});
