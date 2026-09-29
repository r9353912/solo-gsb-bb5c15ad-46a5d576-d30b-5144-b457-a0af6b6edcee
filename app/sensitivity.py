"""偏移敏感性扫描：单一来源 clock_offset 的分段稳定性分析。

审计员选定一个来源并给出 offset 扫描上下界 [lo, hi] 后，本模块不做暴力采样，
而是利用区间模型对 clock_offset 的**线性性**解析求解临界点：

- 来源 s 的 offset 取值 x 时，其所有事件区间整体平移 -x（其它来源不动）；
- 对约束边 a → b：
  - 仅 a 属于 s：gap_lo / gap_hi 随 x 以斜率 +1 变化；
  - 仅 b 属于 s：gap_lo / gap_hi 随 x 以斜率 -1 变化；
  - 两端都属于 s：两边同步平移，offset 抵消，gap 恒定 —— 不制造临界点。

因此每条相关边最多贡献 2 个临界点（gap_lo = 0 与 gap_hi = 0 的穿越点），
把 [lo, hi] 切成若干**边状态稳定**的连续区间；每段取一个代表值调用
``engine.compute`` 全量推理，与当前生效假设下的结果对比，按与编号无关的
签名报告不可能边与冲突链的新增 / 消失 / 持续。
"""
from __future__ import annotations

import math
from typing import Any

from . import engine
from .scenarios import _diff_findings

# 每段都要全量重算，限制段数保证交互响应；超出时提示审计员缩小范围。
MAX_INTERVALS = 25
_EPS = 1e-9  # 临界点去重 / 区间包含判断的容差（秒）


class SensitivityError(ValueError):
    """可直接映射为 HTTP 4xx 的扫描请求错误。"""


def _clean_bound(value: Any, name: str) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError) as exc:
        raise SensitivityError(f"扫描{name}必须是数值，收到: {value!r}") from exc
    if not math.isfinite(v):
        raise SensitivityError(f"扫描{name}必须是有限数值（不能为 NaN/无穷），收到: {value!r}")
    return v


def _impossible_index(result: dict) -> dict[str, dict]:
    """{签名: 边} —— 签名与冲突链 impossible_order 保持一致，便于交叉对照。"""
    return {f"impossible:{e['from']}->{e['to']}": e
            for e in result["edges"] if e["status"] == "impossible"}


def _edge_brief(e: dict) -> dict:
    return {
        "from": e["from"], "to": e["to"], "kind": e["kind"], "note": e["note"],
        "gap_lo": e["gap_lo"], "gap_hi": e["gap_hi"],
        "gap_lo_text": e["gap_lo_text"], "gap_hi_text": e["gap_hi_text"],
    }


def _diff_impossible(base: dict[str, dict], other: dict[str, dict]) -> dict[str, list]:
    """不可能边集合的新增 / 消失 / 持续（other 相对 base）。"""
    added, removed, persisted = [], [], []
    for sig, e in other.items():
        item = {"signature": sig, "edge": _edge_brief(e)}
        (persisted if sig in base else added).append(item)
    for sig, e in base.items():
        if sig not in other:
            removed.append({"signature": sig, "edge": _edge_brief(e)})
    return {"added": added, "removed": removed, "persisted": persisted}


def scan_offset(source_id: str, lo: Any, hi: Any,
                overrides: dict | None = None) -> dict[str, Any]:
    """对 source_id 的 clock_offset 在 [lo, hi] 内做分段稳定性扫描。

    overrides 为页面当前（可能未保存）的其它来源假设：临界点求解与
    “当前结果”基线都在这一上下文下进行。
    """
    lo_f = _clean_bound(lo, "下界")
    hi_f = _clean_bound(hi, "上界")
    if lo_f >= hi_f:
        raise SensitivityError(f"扫描下界必须小于上界（收到 [{lo_f}, {hi_f}]）")

    overrides = overrides or {}
    stored_sources, raw_events, raw_edges = engine.load_model()
    if source_id not in stored_sources:
        raise SensitivityError(f"来源不存在: {source_id}")

    sources = engine._effective_sources(stored_sources, overrides)
    current_offset = float(sources[source_id]["clock_offset"])
    ev_source = {e["id"]: e["source_id"] for e in raw_events}

    # 当前生效假设（含未保存调整）下的基线结果，作为差异对照组
    baseline = engine.compute(overrides)
    base_edges = {e["id"]: e for e in baseline["edges"]}

    # ------------------------------------------------------------ 解析求临界点
    critical: list[dict] = []
    cancelled: list[dict] = []
    involved = 0
    for r in raw_edges:
        sa = ev_source.get(r["from_event"])
        sb = ev_source.get(r["to_event"])
        if sa is None or sb is None:
            continue
        a_in, b_in = sa == source_id, sb == source_id
        if a_in and b_in:
            # 两端同属该来源：offset 整体平移相互抵消，gap 恒定，不制造临界点
            now = base_edges.get(r["id"])
            cancelled.append({
                "id": r["id"], "from": r["from_event"], "to": r["to_event"],
                "kind": r.get("kind", "causal"), "note": r.get("note", ""),
                "status": now["status"] if now else "ok",
            })
            continue
        if not a_in and not b_in:
            continue
        involved += 1
        now = base_edges.get(r["id"])
        if now is None:
            continue
        # gap(x) 是斜率 ±1 的直线：a 端在来源内 → 斜率 +1；b 端在来源内 → -1。
        # 零点 x* = current_offset - sign * gap(x0)。
        sign = 1.0 if a_in else -1.0
        for bound, gap_now in (("gap_lo", now["gap_lo"]), ("gap_hi", now["gap_hi"])):
            x_star = current_offset - sign * gap_now
            if lo_f + _EPS < x_star < hi_f - _EPS:
                critical.append({
                    "offset": round(x_star, 6),
                    "edge_id": r["id"],
                    "from": r["from_event"], "to": r["to_event"],
                    "kind": r.get("kind", "causal"),
                    "bound": bound,
                    "direction": "rising" if sign > 0 else "falling",
                })

    # 排序 + 去重（同一条边两个零点可能重合，或多条边共享同一临界点）
    critical.sort(key=lambda p: (p["offset"], p["from"], p["to"], p["bound"]))
    merged: list[float] = []
    for x in sorted({p["offset"] for p in critical}):
        if merged and x - merged[-1] <= _EPS:
            continue
        merged.append(x)

    if len(merged) + 1 > MAX_INTERVALS:
        raise SensitivityError(
            f"扫描范围内临界点过多（{len(merged)} 个，将产生 {len(merged) + 1} 段；"
            f"上限 {MAX_INTERVALS} 段），请缩小上下界范围")

    # ------------------------------------------------------------ 切段逐段重算
    bounds = [lo_f] + merged + [hi_f]
    base_impossible = _impossible_index(baseline)
    intervals: list[dict] = []
    for i in range(len(bounds) - 1):
        seg_lo, seg_hi = bounds[i], bounds[i + 1]
        mid = (seg_lo + seg_hi) / 2.0
        # 当前 offset 所在的段直接以当前值为代表：该段差异恒为空，便于对照
        contains_current = seg_lo + _EPS < current_offset < seg_hi - _EPS
        rep = current_offset if contains_current else mid
        scan_ov = {sid: dict(ov) for sid, ov in overrides.items()}
        scan_ov.setdefault(source_id, {})["clock_offset"] = rep
        res = engine.compute(scan_ov)
        intervals.append({
            "index": i,
            "lo": round(seg_lo, 6),
            "hi": round(seg_hi, 6),
            "midpoint": round(mid, 6),
            "representative": round(rep, 6),
            "contains_current": contains_current,
            "stats": {
                "conflict_count": res["stats"]["conflict_count"],
                "impossible_edges": res["stats"]["impossible_edges"],
                "ambiguity_group_count": res["stats"]["ambiguity_group_count"],
            },
            "impossible_edges": _diff_impossible(base_impossible,
                                                 _impossible_index(res)),
            "conflicts": _diff_findings(baseline["conflicts"], res["conflicts"]),
        })

    # ------------------------------------------------------------ 提示信息
    notice = None
    if involved == 0:
        notice = (f"来源 {source_id} 的事件不参与任何跨来源约束边"
                  + ("（仅有 offset 相互抵消的同来源边）" if cancelled else "")
                  + "；扫描不会改变任何边的状态，整个范围状态恒定。")
    elif not merged:
        notice = (f"范围 [{round(lo_f, 6)}, {round(hi_f, 6)}] 内没有临界点："
                  f"{involved} 条相关约束边在整个扫描区间内状态保持稳定。")

    return {
        "scan_type": "offset-sensitivity",
        "source_id": source_id,
        "source_name": sources[source_id].get("name", source_id),
        "range": {"lo": round(lo_f, 6), "hi": round(hi_f, 6)},
        "current_offset": round(current_offset, 6),
        "involved_edge_count": involved,
        "critical_points": critical,
        "cancelled_edges": cancelled,
        "intervals": intervals,
        "notice": notice,
    }
