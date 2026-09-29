"""命名假设方案管理与双方案对比。

一个"方案"是审计员保存的各来源时钟假设快照（offset / drift / uncertainty /
reliability）。方案对比在**同一批事件**上分别用两套假设全量重算，再比较：

- 冲突链（impossible_order / causal_cycle）：新增 / 消失 / 持续；
- 时间歧义组：新增 / 消失 / 持续；
- 顺序边状态变化（ok / boundary / impossible）；
- 时间线排序变化（估计时刻位次交换）；
- 两套假设本身的参数差异。

冲突与歧义组通过 ``engine.compute`` 输出中与编号无关的 ``signature`` 匹配；
边通过数据库自增 id 匹配（source_order 与用户约束同表，重导幂等，id 稳定）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

from . import engine
from .db import db

SCENARIO_PARAMS = engine.SOURCE_PARAMS

# 大库下时间线交换对数量可能很大，导出/展示只保留前 N（仍给出总数）。
TIMELINE_DIFF_LIMIT = 200


class ScenarioError(ValueError):
    """可直接映射为 HTTP 4xx 的方案操作错误。"""


class ScenarioConflict(ScenarioError):
    """名称重复（映射 409）。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------ 参数校验

def clean_assumptions(raw: Any, strict: bool = False) -> dict[str, dict[str, float]]:
    """校验并归一化 {source_id: {clock_offset: ...}} 形态的假设体。

    默认宽松：引用的来源当前不存在（可能已被删除的旧方案快照 / 页面上的陈旧
    overrides）时忽略；strict=True 用于显式新建方案时及时报错。
    """
    if not isinstance(raw, dict):
        raise ScenarioError("assumptions 必须是 {来源ID: {参数: 值}} 的对象")
    known: set[str] = set()
    with db() as conn:
        known = {r["id"] for r in conn.execute("SELECT id FROM sources")}
    out: dict[str, dict[str, float]] = {}
    for sid, params in raw.items():
        sid = str(sid).strip()
        if not sid:
            continue
        if strict and sid not in known:
            raise ScenarioError(f"来源不存在: {sid}")
        if not strict and sid not in known:
            continue  # 已删除来源的旧方案快照 / 页面陈旧 overrides：忽略
        if not isinstance(params, dict):
            raise ScenarioError(f"来源 {sid} 的假设必须是对象")
        clean: dict[str, float] = {}
        for k, v in params.items():
            if k not in SCENARIO_PARAMS or v is None:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError) as exc:
                raise ScenarioError(f"{sid}.{k} 不是数值: {v!r}") from exc
            if k == "uncertainty" and fv < 0:
                raise ScenarioError(f"{sid}.uncertainty 不能为负")
            if k == "reliability" and not 0.0 <= fv <= 1.0:
                raise ScenarioError(f"{sid}.reliability 必须在 0~1 之间")
            clean[k] = fv
        if clean:
            out[sid] = clean
    return out


def clean_name(raw: Any) -> str:
    name = str(raw or "").strip()
    if not name:
        raise ScenarioError("方案名称不能为空")
    if len(name) > 80:
        raise ScenarioError("方案名称过长（上限 80 字）")
    return name


# ------------------------------------------------------------ CRUD

def _row_to_scenario(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "assumptions": json.loads(row["assumptions"] or "{}"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_scenarios() -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM scenarios ORDER BY name").fetchall()
    return [_row_to_scenario(r) for r in rows]


def get_scenario(scenario_id: int) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute("SELECT * FROM scenarios WHERE id=?", (scenario_id,)).fetchone()
    if row is None:
        raise ScenarioError(f"方案不存在: {scenario_id}")
    return _row_to_scenario(row)


def _full_snapshot(overrides: Any, baseline: dict[str, dict[str, float]] | None = None,
                   strict: bool = True) -> dict[str, dict[str, float]]:
    """把部分假设展开为完整快照：所有来源 × 四个参数齐全。

    基线取当前库存假设（创建时）或方案自身旧快照（更新时），保证方案自包含——
    日后审计员再改动库存假设，已命名方案的重算结果不会被悄悄改变。
    """
    overrides = clean_assumptions(overrides, strict=strict)
    base = current_effective_assumptions() if baseline is None else baseline
    snapshot: dict[str, dict[str, float]] = {}
    for sid, params in base.items():
        snapshot[sid] = {k: float(params.get(k, 0.0)) for k in SCENARIO_PARAMS}
    for sid, params in overrides.items():
        snapshot.setdefault(sid, {k: 0.0 for k in SCENARIO_PARAMS}).update(params)
    return snapshot


def create_scenario(name: str, assumptions: dict, description: str = "",
                    from_current: bool = False) -> dict[str, Any]:
    """新建命名方案（保存为完整自包含快照）。

    两种入口产生相同形态：
    - 直接保存：assumptions 为审计员显式指定的值，未指定参数取当前库存值；
    - from_current=True（"保存当前为方案"）：assumptions 只传页面未保存的
      overrides（可含已删除来源，忽略），同样合并到当前库存值上。
    """
    name = clean_name(name)
    snapshot = _full_snapshot(assumptions, strict=not from_current)
    now = _now()
    try:
        with db() as conn:
            cur = conn.execute(
                "INSERT INTO scenarios(name,description,assumptions,created_at,updated_at) "
                "VALUES(?,?,?,?,?)",
                (name, str(description or "")[:500],
                 json.dumps(snapshot, ensure_ascii=False), now, now),
            )
            sid = cur.lastrowid
    except Exception as exc:  # sqlite3.IntegrityError
        if "UNIQUE" in str(exc):
            raise ScenarioConflict(f"方案名称已存在: {name}") from exc
        raise
    return get_scenario(sid)


def update_scenario(scenario_id: int, name: str | None = None,
                    description: str | None = None,
                    assumptions: dict | None = None,
                    from_current: bool = False) -> dict[str, Any]:
    _ = from_current  # 见 assumptions 分支：为 True 时以当前库存而非旧快照为基线
    get_scenario(scenario_id)  # 404 前置
    sets, vals = [], []
    if name is not None:
        sets.append("name=?")
        vals.append(clean_name(name))
    if description is not None:
        sets.append("description=?")
        vals.append(str(description)[:500])
    if assumptions is not None:
        # 更新时默认以方案自身旧快照为基线做局部覆盖，保持自包含；
        # from_current=True 表示"用页面当前假设覆盖"，基线改为当前库存假设。
        old = get_scenario(scenario_id)
        baseline = None if from_current else old["assumptions"]
        snapshot = _full_snapshot(assumptions, baseline=baseline,
                                  strict=not from_current)
        sets.append("assumptions=?")
        vals.append(json.dumps(snapshot, ensure_ascii=False))
    if not sets:
        raise ScenarioError("没有需要更新的字段")
    sets.append("updated_at=?")
    vals.append(_now())
    vals.append(scenario_id)
    try:
        with db() as conn:
            conn.execute(f"UPDATE scenarios SET {', '.join(sets)} WHERE id=?", vals)
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise ScenarioConflict("方案名称已存在") from exc
        raise
    return get_scenario(scenario_id)


def delete_scenario(scenario_id: int) -> None:
    with db() as conn:
        cur = conn.execute("DELETE FROM scenarios WHERE id=?", (scenario_id,))
    if cur.rowcount == 0:
        raise ScenarioError(f"方案不存在: {scenario_id}")


def current_effective_assumptions() -> dict[str, dict[str, float]]:
    """当前库存（已保存）的全部来源完整假设——'当前生效方案'。"""
    result = engine.compute()
    return {s["id"]: {k: float(s["stored"][k]) for k in SCENARIO_PARAMS}
            for s in result["sources"]}


# ------------------------------------------------------------ 方案解析

@dataclass
class Side:
    label: str
    kind: str           # scenario | current
    scenario_id: int | None
    overrides: dict[str, dict[str, float]]


def resolve_side(kind: str, scenario_id: int | None,
                 overrides: Any = None) -> Side:
    """把对比一侧的引用解析为可直接喂给 engine.compute 的完整 overrides。"""
    if kind == "scenario":
        if scenario_id is None:
            raise ScenarioError("方案侧缺少 scenario_id")
        sc = get_scenario(int(scenario_id))
        return Side(label=sc["name"], kind="scenario",
                    scenario_id=sc["id"],
                    overrides=clean_assumptions(sc["assumptions"], strict=False))
    if kind == "current":
        # "当前"= 库存假设 + 页面未保存调整（后者可选）
        ov = clean_assumptions(overrides or {}, strict=False)
        merged = current_effective_assumptions()
        for sid, params in ov.items():
            merged.setdefault(sid, {}).update(params)
        return Side(label="当前假设（未保存）" if ov else "当前生效假设",
                    kind="current", scenario_id=None, overrides=merged)
    raise ScenarioError(f"未知方案引用类型: {kind}")


# ------------------------------------------------------------ 对比

def _index(items: list[dict], key: str) -> dict[str, dict]:
    return {it[key]: it for it in items}


def _pair_items(a_items: list[dict], b_items: list[dict],
                key: str = "signature") -> Iterator[tuple[str, dict | None, dict | None]]:
    """按稳定 key 配对两侧条目（保持 A 侧顺序，再追加仅 B 侧有的）。"""
    a_idx, b_idx = _index(a_items, key), _index(b_items, key)
    for sig, it in a_idx.items():
        yield sig, it, b_idx.get(sig)
    for sig, it in b_idx.items():
        if sig not in a_idx:
            yield sig, None, it


def _finding_brief(f: dict) -> dict:
    """冲突链/歧义组在对比结果中的精简形态（保留可定位信息）。"""
    brief = {
        "id": f["id"],
        "kind": f.get("kind", "ambiguity"),
        "severity": f.get("severity"),
        "events": f["events"],
        "edge_keys": f.get("edge_keys", []),
        "explanation": f["explanation"],
    }
    if "source_count" in f:
        brief["source_count"] = f["source_count"]
    if "overlap_window" in f:
        brief["overlap_window"] = f["overlap_window"]
    return brief


def _diff_findings(a_items: list[dict], b_items: list[dict]) -> dict[str, list]:
    added, removed, persisted = [], [], []
    for sig, fa, fb in _pair_items(a_items, b_items):
        if fa is None:
            added.append({"signature": sig, "b": _finding_brief(fb)})
        elif fb is None:
            removed.append({"signature": sig, "a": _finding_brief(fa)})
        else:
            persisted.append({
                "signature": sig,
                "a": _finding_brief(fa),
                "b": _finding_brief(fb),
            })
    return {"added": added, "removed": removed, "persisted": persisted}


def _diff_edges(a_edges: list[dict], b_edges: list[dict]) -> list[dict]:
    """状态发生变化的顺序边（ok / boundary / impossible 翻转）。"""
    a_idx = {e["id"]: e for e in a_edges if e["id"] is not None}
    b_idx = {e["id"]: e for e in b_edges if e["id"] is not None}
    out: list[dict] = []
    for eid in sorted(set(a_idx) | set(b_idx)):
        ea, eb = a_idx.get(eid), b_idx.get(eid)
        if ea is None or eb is None:
            continue  # 同一批事件下边集合应一致；防御性跳过
        if ea["status"] == eb["status"]:
            continue
        out.append({
            "edge_id": eid,
            "from": eb["from"],
            "to": eb["to"],
            "kind": eb["kind"],
            "note": eb["note"],
            "status_a": ea["status"],
            "status_b": eb["status"],
            "gap_a": {"lo": ea["gap_lo"], "hi": ea["gap_hi"],
                      "lo_text": ea["gap_lo_text"], "hi_text": ea["gap_hi_text"]},
            "gap_b": {"lo": eb["gap_lo"], "hi": eb["gap_hi"],
                      "lo_text": eb["gap_lo_text"], "hi_text": eb["gap_hi_text"]},
        })
    # 不可能相关的变化排最前，便于审计
    rank = {"impossible": 0, "boundary": 1, "ok": 2}
    out.sort(key=lambda d: (rank.get(d["status_a"], 3), rank.get(d["status_b"], 3),
                            d["from"], d["to"]))
    return out


def _inversions(order_b_of_a: list[int], limit: int) -> tuple[list[tuple[int, int]], int]:
    """归并排序求排列逆序对：a 序下的 (i, j)（i<j）但在 b 序中位置反转。

    返回 (前 limit 个逆序对, 逆序对总数)。总复杂度 O(n log n + k)，
    避免 5000 事件时 O(n²) 双重循环。
    """
    pairs: list[tuple[int, int]] = []
    total = 0

    def sort(indices: list[int]) -> list[int]:
        nonlocal total
        if len(indices) <= 1:
            return indices
        mid = len(indices) // 2
        left, right = sort(indices[:mid]), sort(indices[mid:])
        merged: list[int] = []
        i = j = 0
        while i < len(left) and j < len(right):
            if order_b_of_a[left[i]] <= order_b_of_a[right[j]]:
                merged.append(left[i])
                i += 1
            else:
                # right[j] 与 left[i:] 全部构成逆序对
                total += len(left) - i
                if len(pairs) < limit:
                    for k in range(i, len(left)):
                        pairs.append((left[k], right[j]))
                        if len(pairs) >= limit:
                            break
                merged.append(right[j])
                j += 1
        merged.extend(left[i:])
        merged.extend(right[j:])
        return merged

    sort(list(range(len(order_b_of_a))))
    return pairs[:limit], total


def _diff_timeline(a_events: list[dict], b_events: list[dict]) -> dict[str, Any]:
    """比较两侧按估计时刻排序的位次：位次交换 / 位次漂移。

    两侧事件集合相同（同一批事件、不同假设），交换对即两个排列间的逆序对。
    """
    a_rank = {e["id"]: i for i, e in enumerate(a_events)}
    b_rank = {e["id"]: i for i, e in enumerate(b_events)}
    a_idx = {e["id"]: e for e in a_events}
    b_idx = {e["id"]: e for e in b_events}

    # 以 A 序排列的事件 id，及其在 B 序中的位置
    a_order_ids = [e["id"] for e in a_events]
    order_b_of_a = [b_rank[eid] for eid in a_order_ids if eid in b_rank]
    inv_pairs, inv_total = _inversions(order_b_of_a, TIMELINE_DIFF_LIMIT)

    swaps = [{
        "events": [
            _event_move_brief(a_order_ids[i], a_rank[a_order_ids[i]],
                              b_rank[a_order_ids[i]],
                              a_idx[a_order_ids[i]], b_idx[a_order_ids[i]]),
            _event_move_brief(a_order_ids[j], a_rank[a_order_ids[j]],
                              b_rank[a_order_ids[j]],
                              a_idx[a_order_ids[j]], b_idx[a_order_ids[j]]),
        ],
    } for i, j in inv_pairs]
    swap_ids = {ev["id"] for pair in swaps for ev in pair["events"]}

    # 位次漂移但未参与交换（事件集合一致时通常为空，保留以应对集合差异/防御）
    shifted = []
    for eid in a_order_ids:
        if eid not in b_rank or eid in swap_ids:
            continue
        delta = b_rank[eid] - a_rank[eid]
        if delta:
            shifted.append(_event_move_brief(eid, a_rank[eid], b_rank[eid],
                                             a_idx[eid], b_idx[eid]))
    shifted.sort(key=lambda d: abs(d["rank_b"] - d["rank_a"]), reverse=True)
    involved = ({a_order_ids[i] for i, _ in inv_pairs}
                | {a_order_ids[j] for _, j in inv_pairs})
    return {
        "swapped_pairs": swaps,
        "swapped_pair_count": inv_total,
        "rank_shifts": shifted[:TIMELINE_DIFF_LIMIT],
        "rank_shift_count": len(shifted),
        # 参与顺序交换的去重事件数；截断时为下界（见 truncated）
        "changed_event_count": len(involved) + len(shifted),
        "changed_event_count_exact": not (inv_total > TIMELINE_DIFF_LIMIT),
        "truncated": inv_total > TIMELINE_DIFF_LIMIT,
        "order_a": a_order_ids,
        "order_b": [e["id"] for e in b_events],
    }


def _event_move_brief(eid: str, rank_a: int, rank_b: int,
                      ea: dict, eb: dict) -> dict:
    return {
        "id": eid,
        "summary": eb.get("summary") or ea.get("summary", ""),
        "source_id": eb.get("source_id") or ea.get("source_id"),
        "rank_a": rank_a,
        "rank_b": rank_b,
        "est_a": ea["est"],
        "est_b": eb["est"],
        "est_text_a": ea["est_text"],
        "est_text_b": eb["est_text"],
    }


def _diff_assumptions(ov_a: dict, ov_b: dict) -> list[dict]:
    out = []
    for sid in sorted(set(ov_a) | set(ov_b)):
        pa, pb = ov_a.get(sid, {}), ov_b.get(sid, {})
        params = []
        for k in SCENARIO_PARAMS:
            va, vb = pa.get(k), pb.get(k)
            if va is None and vb is None:
                continue
            if va is not None and vb is not None and abs(va - vb) <= 1e-9:
                continue
            params.append({"param": k, "a": va, "b": vb,
                           "delta": (vb - va) if (va is not None and vb is not None) else None})
        if params:
            out.append({"source_id": sid, "params": params})
    return out


def compare_scenarios(spec_a: dict, spec_b: dict) -> dict[str, Any]:
    """在同一批事件上分别重算两套假设并生成结构化差异。

    spec 形态：{"kind": "scenario", "scenario_id": 3}
              或 {"kind": "current", "overrides": {...可选的未保存调整...}}
    """
    side_a = resolve_side(spec_a.get("kind", "current"),
                          spec_a.get("scenario_id"), spec_a.get("overrides"))
    side_b = resolve_side(spec_b.get("kind", "current"),
                          spec_b.get("scenario_id"), spec_b.get("overrides"))
    if (side_a.kind == "scenario" and side_b.kind == "scenario"
            and side_a.scenario_id == side_b.scenario_id):
        raise ScenarioError("对比两侧不能是同一套方案")

    ra = engine.compute(side_a.overrides)
    rb = engine.compute(side_b.overrides)

    conflicts = _diff_findings(ra["conflicts"], rb["conflicts"])
    ambiguities = _diff_findings(ra["ambiguity_groups"], rb["ambiguity_groups"])
    edges = _diff_edges(ra["edges"], rb["edges"])
    timeline = _diff_timeline(ra["events"], rb["events"])
    assumptions = _diff_assumptions(side_a.overrides, side_b.overrides)

    return {
        "comparison_type": "assumption-scenario-diff",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sides": {
            "a": {"label": side_a.label, "kind": side_a.kind,
                  "scenario_id": side_a.scenario_id,
                  "assumptions": side_a.overrides},
            "b": {"label": side_b.label, "kind": side_b.kind,
                  "scenario_id": side_b.scenario_id,
                  "assumptions": side_b.overrides},
        },
        "summary": {
            "conflicts_a": ra["stats"]["conflict_count"],
            "conflicts_b": rb["stats"]["conflict_count"],
            "conflicts_added": len(conflicts["added"]),
            "conflicts_removed": len(conflicts["removed"]),
            "conflicts_persisted": len(conflicts["persisted"]),
            "ambiguity_groups_a": ra["stats"]["ambiguity_group_count"],
            "ambiguity_groups_b": rb["stats"]["ambiguity_group_count"],
            "ambiguities_added": len(ambiguities["added"]),
            "ambiguities_removed": len(ambiguities["removed"]),
            "ambiguities_persisted": len(ambiguities["persisted"]),
            "edges_changed": len(edges),
            "impossible_edges_a": ra["stats"]["impossible_edges"],
            "impossible_edges_b": rb["stats"]["impossible_edges"],
            "timeline_events_reordered": timeline["changed_event_count"],
            "timeline_swapped_pairs": timeline["swapped_pair_count"],
            "sources_with_assumption_diff": len(assumptions),
            "event_count": rb["stats"]["event_count"],
        },
        "conflicts": conflicts,
        "ambiguity_groups": ambiguities,
        "edge_changes": edges,
        "timeline_changes": timeline,
        "assumption_diff": assumptions,
        "stats_a": ra["stats"],
        "stats_b": rb["stats"],
    }
