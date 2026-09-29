"""锚点校准：用审计员确认的"同一动作"事件对反推待校准来源的 offset。

背景
----
双来源对账（``reconciliation``）给出候选配对后，审计员可以人工勾选其中
1～20 对**已配对且确认是同一真实动作**的事件，指定其中一个来源为
**待校准方**（其时钟偏差假设需要修正），另一个来源作**参照**。

复用当前含未保存调整（overrides）的事件时间区间：漂移（drift_ppm）与
不确定度（uncertainty）保持不变，只允许对待校准来源的 clock_offset
增加一个**统一增量** δ。来源 s 的 clock_offset 增加 δ 后，其全部事件的
真实时间区间整体平移 -δ 秒（漂移项在同一次校准内固定，故 δ 对该来源的
所有事件是同量平移）。

单对锚点
--------
设待校准方闭区间为 [lo_c, hi_c]、参照方为 [lo_r, hi_r]（整数毫秒）。
偏移后两侧闭区间相交（含边界相接）当且仅当

    lo_c - δ ≤ hi_r  且  lo_r ≤ hi_c - δ
    ⇒  δ ∈ [lo_c - hi_r ,  hi_c - lo_r]  （毫秒）

即每对锚点给出一个 offset 增量范围。

所有锚点范围取交集
------------------
    δ ∈ [max_i lo_i, min_i hi_i]
feasible 当且仅当 max lo ≤ min hi。

- 非空：给出允许的 offset **增量**范围与换算后的**绝对** offset 范围
  （当前生效 offset + 增量），中点供"✍ 采用中点"一键写入左栏未保存假设；
- 为空：明确列出**分别决定冲突上下界的两对锚点**（上界锚点 = 提供
  min hi 的一对；下界锚点 = 提供 max lo 的一对）及各自范围。

所有区间端点先按对账同款规则四舍五入到整数毫秒（half-up）再做整数
比较，浮点误差不可能影响边界结论。整个计算**只读**：不写数据库、
不改写事件或假设；"采用中点"是前端把值写入未保存 overrides 后走既有
``/api/recompute`` 流程，仍可"放弃调整"。
"""
from __future__ import annotations

from typing import Any

from . import engine
from .reconciliation import (
    ReconcileError,
    _event_record,
    _paired_event,
    round_half_up_ms,
)

MAX_ANCHORS = 20   # 锚点对数上限（需求：1～20 对）


# ---------------------------------------------------------------- 校验

def _clean_side(raw: Any, name: str) -> str:
    sid = str(raw if raw is not None else "").strip()
    if not sid:
        raise ReconcileError(f"必须指定{name}来源")
    return sid


def _clean_eid(raw: Any, name: str) -> str:
    if isinstance(raw, bool) or raw is None:
        raise ReconcileError(f"{name}事件 ID 必须是非空字符串")
    eid = str(raw).strip()
    if not eid:
        raise ReconcileError(f"{name}事件 ID 不能为空")
    return eid


def _clean_anchors(raw: Any) -> list[tuple[str, str]]:
    """校验锚点列表：必须是 1～20 对 (a, b) 事件 ID；锚点重复明确报错。"""
    if not isinstance(raw, list):
        raise ReconcileError("anchors 必须是锚点对列表，每项形如 {\"a\": 事件ID, \"b\": 事件ID}")
    if not raw:
        raise ReconcileError("至少勾选 1 对锚点（已配对且确认是同一动作的事件）")
    if len(raw) > MAX_ANCHORS:
        raise ReconcileError(f"锚点最多 {MAX_ANCHORS} 对（收到 {len(raw)} 对）")
    pairs: list[tuple[str, str]] = []
    seen_pairs: set[tuple[str, str]] = set()
    seen_a: set[str] = set()
    seen_b: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ReconcileError(f"第 {i + 1} 个锚点必须是对象（a / b 两个事件 ID）")
        ea, eb = _clean_eid(item.get("a"), f"第 {i + 1} 个锚点的 A 侧"), \
                  _clean_eid(item.get("b"), f"第 {i + 1} 个锚点的 B 侧")
        if ea == eb:
            raise ReconcileError(f"第 {i + 1} 个锚点的两个事件 ID 相同（{ea}）：锚点必须跨两个来源")
        key = (ea, eb)
        if key in seen_pairs:
            raise ReconcileError(f"锚点重复：{ea} ↔ {eb} 在列表中出现多次，每对锚点只能勾选一次")
        if ea in seen_a:
            raise ReconcileError(f"锚点重复：A 侧事件 {ea} 参与了多对锚点，每条事件最多作为一个锚点")
        if eb in seen_b:
            raise ReconcileError(f"锚点重复：B 侧事件 {eb} 参与了多对锚点，每条事件最多作为一个锚点")
        seen_pairs.add(key)
        seen_a.add(ea)
        seen_b.add(eb)
        pairs.append(key)
    return pairs


# ---------------------------------------------------------------- 主入口

def calibrate_anchors(source_a: Any, source_b: Any, calibrated_side: Any,
                      anchors: Any, overrides: dict | None = None) -> dict[str, Any]:
    """按锚点对求待校准来源的统一 offset 增量可行范围（只读，不写数据库）。

    overrides 为页面当前（可能尚未保存）的时钟假设；事件区间完全复用
    ``engine`` 在该假设下（库存 + overrides）的时钟校正结果，漂移与
    不确定度固定不变。
    """
    sid_a = _clean_side(source_a, "A 侧")
    sid_b = _clean_side(source_b, "B 侧")
    if sid_a == sid_b:
        raise ReconcileError(f"两侧必须选择不同来源（收到的两侧都是 {sid_a}）")
    side_raw = str(calibrated_side if calibrated_side is not None else "").strip().lower()
    if side_raw not in ("a", "b"):
        raise ReconcileError(f"待校准方必须是 \"a\" 或 \"b\"（收到 {calibrated_side!r}）")
    pairs = _clean_anchors(anchors)

    stored_sources, raw_events, _ = engine.load_model()
    if sid_a not in stored_sources:
        raise ReconcileError(f"来源不存在: {sid_a}")
    if sid_b not in stored_sources:
        raise ReconcileError(f"来源不存在: {sid_b}")

    sources = engine._effective_sources(stored_sources, overrides)
    nodes = engine._build_nodes(raw_events, sources)
    nodes_by_id = {n.raw["id"]: n for n in nodes}

    cal_sid = sid_a if side_raw == "a" else sid_b
    ref_sid = sid_b if side_raw == "a" else sid_a
    name_cal = sources[cal_sid].get("name", cal_sid)
    name_ref = sources[ref_sid].get("name", ref_sid)
    current_offset = float(sources[cal_sid]["clock_offset"])

    # ------------------------------------------------ 逐锚点解析 + 求范围
    anchor_items: list[dict[str, Any]] = []
    lo_anchor: dict[str, Any] | None = None   # 提供最大下界的锚点（交集下界）
    hi_anchor: dict[str, Any] | None = None   # 提供最小上界的锚点（交集上界）

    for i, (id_in_a, id_in_b) in enumerate(pairs):
        na, nb = nodes_by_id.get(id_in_a), nodes_by_id.get(id_in_b)
        if na is None:
            raise ReconcileError(f"第 {i + 1} 个锚点的 A 侧事件不存在（可能已被删除）: {id_in_a}")
        if nb is None:
            raise ReconcileError(f"第 {i + 1} 个锚点的 B 侧事件不存在（可能已被删除）: {id_in_b}")
        if na.raw["source_id"] != sid_a:
            raise ReconcileError(
                f"第 {i + 1} 个锚点来源不一致：A 侧事件 {id_in_a} 属于 "
                f"{na.raw['source_id']}，不是 A 侧来源 {sid_a}；请只勾选本次对账已配对的事件")
        if nb.raw["source_id"] != sid_b:
            raise ReconcileError(
                f"第 {i + 1} 个锚点来源不一致：B 侧事件 {id_in_b} 属于 "
                f"{nb.raw['source_id']}，不是 B 侧来源 {sid_b}；请只勾选本次对账已配对的事件")

        rec_a, rec_b = _event_record(na), _event_record(nb)
        if side_raw == "a":
            rec_c, rec_r = rec_a, rec_b
        else:
            rec_c, rec_r = rec_b, rec_a

        # 待校准方区间整体平移 -δ（ms）；与参照方闭区间相交要求
        #   lo_c - δ ≤ hi_r 且 lo_r ≤ hi_c - δ
        # 即 δ ∈ [lo_c - hi_r, hi_c - lo_r]（整数毫秒，含边界）。
        d_lo = rec_c["lo_ms"] - rec_r["hi_ms"]
        d_hi = rec_c["hi_ms"] - rec_r["lo_ms"]
        intersects_now = not (rec_c["hi_ms"] < rec_r["lo_ms"]
                              or rec_r["hi_ms"] < rec_c["lo_ms"])

        item = {
            "index": i,
            "a": _paired_event(rec_a),
            "b": _paired_event(rec_b),
            "calibrated_event": _paired_event(rec_c),
            "reference_event": _paired_event(rec_r),
            "delta_range_ms": {"lo": d_lo, "hi": d_hi},
            "delta_range_seconds": {"lo": d_lo / 1000.0, "hi": d_hi / 1000.0},
            "absolute_offset_range_seconds": {
                "lo": round(current_offset + d_lo / 1000.0, 6),
                "hi": round(current_offset + d_hi / 1000.0, 6)},
            "intersects_currently": intersects_now,
        }
        anchor_items.append(item)
        if lo_anchor is None or d_lo > lo_anchor["delta_range_ms"]["lo"]:
            lo_anchor = item
        if hi_anchor is None or d_hi < hi_anchor["delta_range_ms"]["hi"]:
            hi_anchor = item

    assert lo_anchor is not None and hi_anchor is not None
    int_lo = lo_anchor["delta_range_ms"]["lo"]
    int_hi = hi_anchor["delta_range_ms"]["hi"]
    feasible = int_lo <= int_hi

    sides = {
        "a": {"source_id": sid_a,
              "source_name": sources[sid_a].get("name", sid_a)},
        "b": {"source_id": sid_b,
              "source_name": sources[sid_b].get("name", sid_b)},
    }
    response: dict[str, Any] = {
        "calibration_type": "anchor-offset-calibration",
        "summary": "",
        "sides": sides,
        "calibrated_side": side_raw,
        "reference_side": "b" if side_raw == "a" else "a",
        "calibrated_source_id": cal_sid,
        "calibrated_source_name": name_cal,
        "reference_source_id": ref_sid,
        "reference_source_name": name_ref,
        "anchor_count": len(pairs),
        "current_offset_seconds": round(current_offset, 6),
        "current_offset_ms": round_half_up_ms(current_offset),
        "anchors": anchor_items,
        "feasible": feasible,
    }

    if feasible:
        mid_ms = (int_lo + int_hi) // 2      # 整数毫秒和为奇时向下取整（0.5ms 粒度）
        delta_mid = mid_ms / 1000.0
        abs_lo = current_offset + int_lo / 1000.0
        abs_hi = current_offset + int_hi / 1000.0
        abs_mid = current_offset + delta_mid
        response.update({
            "intersection": {
                "delta_range_ms": {"lo": int_lo, "hi": int_hi},
                "delta_range_seconds": {"lo": round(int_lo / 1000.0, 6),
                                        "hi": round(int_hi / 1000.0, 6)},
                "delta_midpoint_ms": mid_ms,
                "delta_midpoint_seconds": round(delta_mid, 6),
                "absolute_offset_range_ms": {"lo": round_half_up_ms(abs_lo),
                                             "hi": round_half_up_ms(abs_hi)},
                "absolute_offset_range_seconds": {"lo": round(abs_lo, 6),
                                                  "hi": round(abs_hi, 6)},
                "absolute_offset_midpoint_ms": round_half_up_ms(abs_mid),
                "absolute_offset_midpoint_seconds": round(abs_mid, 6),
            },
            "conflict": None,
            "notice": (
                "漂移与不确定度固定不变，只对待校准来源 clock_offset 施加统一增量；"
                "采用中点仅写入左栏未保存假设并重算时间线，仍可放弃调整。"),
        })
        response["summary"] = (
            f"{len(pairs)} 对锚点一致：待校准方「{name_cal}」相对参照方「{name_ref}」"
            f"的 offset 增量可行域为 [{int_lo}, {int_hi}] ms（闭区间，含边界），"
            f"换算为绝对 offset [{round(abs_lo, 6)}, {round(abs_hi, 6)}] s；"
            f"建议中点 {round(abs_mid, 6)} s（当前生效 {round(current_offset, 6)} s）。"
        )
    else:
        response.update({"intersection": None,
                         "conflict": {"lower_anchor": lo_anchor,
                                      "upper_anchor": hi_anchor},
                         "notice": None})
        response["summary"] = (
            f"{len(pairs)} 对锚点的 offset 增量范围没有公共交集："
            f"下界锚点要求 δ ≥ {int_lo} ms（{lo_anchor['calibrated_event']['id']} ↔ "
            f"{lo_anchor['reference_event']['id']}），上界锚点要求 δ ≤ {int_hi} ms"
            f"（{hi_anchor['calibrated_event']['id']} ↔ "
            f"{hi_anchor['reference_event']['id']}），相差 {int_lo - int_hi} ms。"
            "请复核这两对锚点是否确为同一动作、事件归属来源是否正确，"
            "或检查漂移/不确定度假设（本次校准固定漂移与不确定度）。"
        )
    return response
