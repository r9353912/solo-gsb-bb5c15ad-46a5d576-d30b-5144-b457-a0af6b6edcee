"""双来源事件对账：在两个不同来源的事件之间做最优一一配对。

定位
----
审计员常需要回答："网关日志与应用日志记录的是不是同一批真实动作？"
本模块是一个**只读**对账入口：

- 复用 ``engine`` 的时钟校正：事件真实时间区间
  ``[lo, hi] = [t_est - uncertainty - sigma, t_est + uncertainty + sigma]``
  在当前生效假设（库存 + 页面**未保存** overrides）下实时计算，不新增任何
  校正数据、不写数据库、不改写事件；
- 用户选择两个**不同**来源与一个非负容差（毫秒）；
- 仅 ``actor``（去空白后）非空且相同、``event_type`` 相同、且两侧**闭区间**
  之间的间距不超过容差的事件可以配对；
- 每条事件最多参与一对；
- 目标按字典序为：① 最大化配对数；② 最小化配对双方区间中点差绝对值之和；
  ③ 同优结果按事件 ID 稳定（确定性）输出。**不允许逐条贪心**——贪心可能
  少配对（见 tests/test_reconciliation.py），这里用全局最优匹配求解。

整数毫秒
--------
需求规定时间量统一四舍五入到整数毫秒，因此所有进入判定/目标函数的量
（区间端点、中点、闭区间间距、中点差、容差）都先按"四舍五入"（half-up，
非 Python 的银行家舍入）换算成整数毫秒再做整数运算，浮点误差不可能影响
配对边界。

算法
----
候选关系天然按 ``(actor, event_type)`` 分组，跨组永远不可能配对，因此
逐 (actor, event_type) 组独立求二分图最优匹配即可（组互不竞争同一事件，
分组不改变全局最优）。组内取较小的一侧为行（n ≤ m）。

* **同代价组**：若该组所有可行边的中点差相同（同一批 / 重合时间戳的常见
  情形），只需最大化配对数——用迭代版 Kuhn 增广路即可。退化的全等代价
  矩阵会让经典匈牙利做大量冗余增广，这一快速路径把 200×200 同刻情形从
  数秒降到 1 秒内。
* **异代价组**：先 Kuhn 求最大基数并判断行侧能否全部配对。
  - 能（常见）：直接在 n × m **真实代价矩阵**（不可行格填兜底大代价）上
    跑矩形匈牙利（Kuhn–Munkres 势函数，O(n²·m)），无虚拟列；
  - 不能（部分行注定无对）：在 n × (m + n) 矩阵上跑匈牙利——真实列取
    中点差、n 个虚拟（不配对）列取统一惩罚 PENALTY（严格大于任意全真实
    配对的总代价），故最优解先占满真实配对数、再压中点差之和。

匈牙利在行/列均按事件 ID 排序的整数矩阵上运行，最小约化列并列时取编号
更小者，平局由固定遍历顺序唯一决定，因此同一输入永远得到同一输出——
配合结果按事件 ID 排序，即为需求要求的稳定输出。每侧上限 200 条，规模
有界；**全程全局最优，不允许逐条贪心**（贪心可能少配对，见测试）。
"""
from __future__ import annotations

import math
from typing import Any

from . import engine
from .timeutil import fmt

MAX_EVENTS_PER_SIDE = 200   # 每个来源参与对账的事件数硬上限，超限明确拒绝

# ---------------------------------------------------------------- 舍入

def round_half_up_ms(seconds: float) -> int:
    """秒 → 整数毫秒，四舍五入（0.5 一律向远离 0 的方向进 1）。

    不用 Python 内置 round()——它是银行家舍入（0.5 偶进奇不进），
    会让"恰好 0.5 ms"的边界取决于前一位奇偶。正负值都向远离 0 的方向进位。
    """
    x = seconds * 1000.0
    return int(math.floor(x + 0.5)) if x >= 0 else int(math.ceil(x - 0.5))


# ---------------------------------------------------------------- 求解

FORBIDDEN_REAL = 1 << 40  # 完美匹配情形下不可行真实格的兜底大代价


def _hungarian_min_cost(cost: list[list[int]]) -> list[int]:
    """矩形匈牙利（最小权完美匹配），行 n ≤ 列 m，返回每行匹配的列（0 基）。

    经典 Kuhn–Munkres 势函数实现，O(n²·m)；零势初始化后全部边为约化零边。
    way[j] 记录增广树中列 j 的父列，增广结束后沿它翻转匹配。行/列顺序由
    调用方按事件 ID 固定，最小约化列并列时取列号最小，平局路径唯一，
    因而同一输入永远得到同一输出（确定性、可复现，非逐条贪心）。
    """
    n = len(cost)
    if n == 0:
        return []
    m = len(cost[0])
    INF = 1 << 60
    # u[i]/v[j]：行/列势；p[j]：列 j 当前匹配的行（0 表示未匹配，行号 1 基）
    u = [0] * (n + 1)
    v = [0] * (m + 1)
    p = [0] * (m + 1)
    way = [0] * (m + 1)

    for i in range(1, n + 1):
        p[0] = i
        j0 = 0  # 虚拟列 0：增广树的根挂在它上面
        minv = [INF] * (m + 1)
        used = [False] * (m + 1)
        while p[j0] != 0:
            used[j0] = True
            i0 = p[j0]
            delta = INF
            j1 = 0
            row = cost[i0 - 1]
            ui = u[i0]
            # 最小约化列；并列取列号最小（确定性平局）
            for j in range(1, m + 1):
                if used[j]:
                    continue
                cur = row[j - 1] - ui - v[j]
                if cur < minv[j]:
                    minv[j] = cur
                    way[j] = j0
                if minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
        # 沿 way 父列链翻转匹配
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break

    match_row = [-1] * n
    for j in range(1, m + 1):
        if p[j] != 0:
            match_row[p[j] - 1] = j - 1
    return match_row


def _max_cardinality_assignment(adj: list[list[tuple[int, int]]],
                                m: int) -> list[int]:
    """只求最大基数（忽略代价差异），迭代版 Kuhn 增广路，避免递归栈。

    adj[x] 为行 x 可连的真实列 (列号, 代价)，列号按事件 ID 升序排列，
    平局选择确定、结果可复现。返回行→列（-1 表示该行未配对）。
    """
    n = len(adj)
    match_col = [-1] * m

    def augment(root: int) -> None:
        # 迭代 DFS：stack 每帧 (行, 下一邻边游标)；chosen[d] 为该帧占用的列。
        seen = [False] * m
        stack: list[list[int]] = [[root, 0]]
        chosen: list[int] = []
        while stack:
            r, k = stack[-1]
            if k < len(adj[r]):
                stack[-1][1] = k + 1
                c = adj[r][k][0]
                if seen[c]:
                    continue
                seen[c] = True
                chosen.append(c)
                owner = match_col[c]
                if owner < 0:
                    # 找到增广路：第 d 帧的行改配 chosen[d]
                    for d, frame in enumerate(stack):
                        match_col[chosen[d]] = frame[0]
                    return
                stack.append([owner, 0])
            else:
                stack.pop()
                if chosen:
                    chosen.pop()

    assign = [-1] * n
    for x in range(n):
        augment(x)
    for c, r in enumerate(match_col):
        if r >= 0:
            assign[r] = c
    return assign


def _max_cardinality_min_cost(adj: list[list[tuple[int, int]]], n: int,
                              m: int, max_delta: int) -> list[int]:
    """先最大化配对数，再最小化中点差绝对值之和（整数毫秒）。

    1. 先 Kuhn 求最大基数，判断较小的行侧能否全部配对；
    2. 能（K == n，常见情形）：直接在 n × m **真实代价矩阵**上跑矩形
       匈牙利，无虚拟列——全部事件同刻这类退化矩阵也不会触发长增广；
    3. 不能（部分行注定无对）：在 n × (m + n) 矩阵上跑匈牙利，真实列取
       中点差、n 个虚拟（不配对）列取统一惩罚 PENALTY。PENALTY 严格大于
       任意"全真实配对"总代价，故最优解先占满真实配对数再压中点差之和。
    """
    cardinality = _max_cardinality_assignment(adj, m)
    if sum(1 for y in cardinality if y >= 0) == n:
        # 行侧可全部配对：纯真实列（不可行格用兜底大代价，存在全可行解时
        # 不会被选中）。邻边按列号升序填入矩阵。
        cost = [[FORBIDDEN_REAL] * m for _ in range(n)]
        for x, edges in enumerate(adj):
            for y, d in edges:
                cost[x][y] = d
        return _hungarian_min_cost(cost)

    # 部分配对：真实列 + 虚拟列。
    penalty = max(1, n * max(1, max_delta) + 1)
    forbidden = n * penalty + 1
    cost = [[0] * (m + n) for _ in range(n)]
    for x, edges in enumerate(adj):
        feas = {y: d for y, d in edges}
        for y in range(m):
            cost[x][y] = feas.get(y, forbidden)
        for y in range(m, m + n):
            cost[x][y] = penalty
    assign = _hungarian_min_cost(cost)
    return [y if y < m else -1 for y in assign]


# ---------------------------------------------------------------- 数据

class ReconcileError(ValueError):
    """可直接映射为 HTTP 4xx 的对账请求错误。"""


def _clean_side(raw: Any, name: str) -> str:
    sid = str(raw if raw is not None else "").strip()
    if not sid:
        raise ReconcileError(f"必须选择{name}来源")
    return sid


def _clean_tolerance_ms(raw: Any) -> int:
    """容差（毫秒）：必须是有限数值且非负；统一四舍五入到整数毫秒。"""
    if isinstance(raw, bool):
        raise ReconcileError(f"容差必须是非负数值（毫秒），收到: {raw!r}")
    try:
        v = float(raw)
    except (TypeError, ValueError) as exc:
        raise ReconcileError(f"容差必须是非负数值（毫秒），收到: {raw!r}") from exc
    if not math.isfinite(v):
        raise ReconcileError(f"容差必须是有限数值（不能为 NaN/无穷），收到: {raw!r}")
    if v < 0:
        raise ReconcileError(f"容差不能为负（收到 {v} ms）；不允许负容差")
    return round_half_up_ms(v / 1000.0)


def _event_record(node: engine.Node) -> dict[str, Any]:
    """组装单侧事件输出（区间端点/中点已统一为整数毫秒）。"""
    e = node.raw
    lo_ms = round_half_up_ms(node.lo)
    hi_ms = round_half_up_ms(node.hi)
    mid_ms = round_half_up_ms((node.lo + node.hi) / 2.0)
    return {
        "id": e["id"],
        "source_id": e["source_id"],
        "source_name": node.source.get("name", e["source_id"]),
        "event_type": e["event_type"],
        "actor": (e.get("actor") or "").strip() if e.get("actor") is not None else "",
        "summary": e.get("summary", ""),
        "est_text": fmt(node.est),
        "lo_ms": lo_ms,
        "hi_ms": hi_ms,
        "mid_ms": mid_ms,
        "lo_text": fmt(node.lo),
        "hi_text": fmt(node.hi),
        "mid_text": fmt(mid_ms / 1000.0),
        "radius_ms": round_half_up_ms(node.radius),
    }


# ---------------------------------------------------------------- 主入口

def reconcile_sources(side_a: Any, side_b: Any, tolerance_ms: Any,
                      overrides: dict | None = None) -> dict[str, Any]:
    """对两个不同来源的事件做最优一一配对（只读，不写数据库）。

    overrides 为页面当前（可能未保存）的时钟假设；事件区间完全复用
    ``engine`` 在该假设下的时钟校正结果。
    """
    sid_a = _clean_side(side_a, "A 侧")
    sid_b = _clean_side(side_b, "B 侧")
    if sid_a == sid_b:
        raise ReconcileError(f"两侧必须选择不同来源（收到的两侧都是 {sid_a}）")
    tol_ms = _clean_tolerance_ms(tolerance_ms)

    stored_sources, raw_events, _ = engine.load_model()
    if sid_a not in stored_sources:
        raise ReconcileError(f"来源不存在: {sid_a}")
    if sid_b not in stored_sources:
        raise ReconcileError(f"来源不存在: {sid_b}")

    sources = engine._effective_sources(stored_sources, overrides)
    nodes = engine._build_nodes(raw_events, sources)
    nodes_a = sorted((n for n in nodes if n.raw["source_id"] == sid_a),
                     key=lambda n: n.raw["id"])
    nodes_b = sorted((n for n in nodes if n.raw["source_id"] == sid_b),
                     key=lambda n: n.raw["id"])

    # 每侧最多 200 条事件（先按来源全量计数），超限明确拒绝、不做截断
    if len(nodes_a) > MAX_EVENTS_PER_SIDE:
        raise ReconcileError(
            f"A 侧来源 {sid_a} 有 {len(nodes_a)} 条事件，超过每侧 "
            f"{MAX_EVENTS_PER_SIDE} 条上限；请先在主时间线按类型/操作者"
            "缩小范围所依赖的来源数据，或选择事件更少的来源")
    if len(nodes_b) > MAX_EVENTS_PER_SIDE:
        raise ReconcileError(
            f"B 侧来源 {sid_b} 有 {len(nodes_b)} 条事件，超过每侧 "
            f"{MAX_EVENTS_PER_SIDE} 条上限；请选择事件更少的来源")

    rec_a = [_event_record(n) for n in nodes_a]
    rec_b = [_event_record(n) for n in nodes_b]

    # actor 非空才可配对；空 actor 事件直接进入未匹配列表
    def actor_of(rec: dict) -> str:
        return (rec.get("actor") or "").strip()

    groups: dict[tuple[str, str], tuple[list[int], list[int]]] = {}
    for i, rec in enumerate(rec_a):
        act = actor_of(rec)
        if act:
            groups.setdefault((act, rec["event_type"]), ([], []))[0].append(i)
    for j, rec in enumerate(rec_b):
        act = actor_of(rec)
        if act:
            g = groups.get((act, rec["event_type"]))
            if g is not None:
                g[1].append(j)
    # 组内下标列表天然按事件 ID 排序（rec 已排序）

    matched_a: set[int] = set()
    matched_b: set[int] = set()
    pairs: list[dict[str, Any]] = []
    candidate_edge_count = 0

    for (act, etype), (ia, ib) in groups.items():
        if not ia or not ib:
            continue
        # 较小一侧取为行（rows），保证 n ≤ m；两侧下标都已按事件 ID 排序。
        swapped = len(ia) > len(ib)
        rows_side = ib if swapped else ia          # 全局 rec 下标
        cols_side = ia if swapped else ib
        rows_rec = [rec_b[i] if swapped else rec_a[i] for i in rows_side]
        cols_rec = [rec_a[i] if swapped else rec_b[i] for i in cols_side]
        n, m = len(rows_rec), len(cols_rec)

        # 可行边：闭区间间距 ≤ 容差。闭区间间距
        #   dist = max(0, lo_右 - hi_左, lo_左 - hi_右)
        # 全部为整数毫秒；同时记录中点差 |Δmid| 作为二级代价。
        adj: list[list[tuple[int, int]]] = [[] for _ in range(n)]
        distinct_costs: set[int] = set()
        max_delta = 0
        for x, rx in enumerate(rows_rec):
            for y, cy in enumerate(cols_rec):
                gap = max(0, cy["lo_ms"] - rx["hi_ms"],
                          rx["lo_ms"] - cy["hi_ms"])
                if gap <= tol_ms:
                    d = abs(rx["mid_ms"] - cy["mid_ms"])
                    adj[x].append((y, d))
                    distinct_costs.add(d)
                    max_delta = max(max_delta, d)
            # 列号按事件 ID 升序（rows/cols 已排序）：Kuhn 平局确定
            adj[x].sort(key=lambda e: e[0])
        edge_total = sum(len(a) for a in adj)
        candidate_edge_count += edge_total
        if edge_total == 0:
            continue

        if len(distinct_costs) == 1:
            # 全部可行边代价相同（常见于同批/重合时间戳）：只求最大基数，
            # Kuhn 即可，避免退化代价矩阵让匈牙利做大量冗余增广。
            assign = _max_cardinality_assignment(adj, m)
        else:
            # 代价不同：先最大化配对数，再最小化中点差绝对值之和。
            assign = _max_cardinality_min_cost(adj, n, m, max_delta)

        for x, y in enumerate(assign):
            if y < 0:
                continue  # 该行未配对
            ri, ci = rows_side[x], cols_side[y]
            ar_idx, br_idx = (ci, ri) if swapped else (ri, ci)
            ar, br = rec_a[ar_idx], rec_b[br_idx]
            gap_closed = max(0, br["lo_ms"] - ar["hi_ms"],
                             ar["lo_ms"] - br["hi_ms"])
            mid_delta = br["mid_ms"] - ar["mid_ms"]
            pairs.append({
                "a": _paired_event(ar),
                "b": _paired_event(br),
                "actor": act,
                "event_type": etype,
                "mid_delta_ms": mid_delta,
                "mid_delta_abs_ms": abs(mid_delta),
                "closed_gap_ms": gap_closed,
                "within_tolerance": gap_closed <= tol_ms,
            })
            matched_a.add(ar_idx)
            matched_b.add(br_idx)

    # 稳定输出：按 A 侧事件 ID（并列再按 B 侧 ID）排序
    pairs.sort(key=lambda p: (p["a"]["id"], p["b"]["id"]))
    unmatched_a = [_paired_event(rec_a[i]) for i in range(len(rec_a))
                   if i not in matched_a]
    unmatched_b = [_paired_event(rec_b[i]) for i in range(len(rec_b))
                   if i not in matched_b]

    name_a = sources[sid_a].get("name", sid_a)
    name_b = sources[sid_b].get("name", sid_b)
    summary = (
        f"{name_a}（A）{len(rec_a)} 条 × {name_b}（B）{len(rec_b)} 条事件，"
        f"在容差 ±{tol_ms} ms 内配对 {len(pairs)} 对："
        f"A 侧未匹配 {len(unmatched_a)} 条、B 侧未匹配 {len(unmatched_b)} 条。"
        "配对优先最大化对数，再最小化区间中点差绝对值之和（整数毫秒，全局最优，非逐条贪心）。"
    )

    return {
        "reconcile_type": "dual-source-event-reconciliation",
        "summary": summary,
        "sides": {
            "a": {"source_id": sid_a, "source_name": name_a,
                  "event_count": len(rec_a)},
            "b": {"source_id": sid_b, "source_name": name_b,
                  "event_count": len(rec_b)},
        },
        "tolerance_ms": tol_ms,
        "pairs": pairs,
        "unmatched_a": unmatched_a,
        "unmatched_b": unmatched_b,
        "stats": {
            "events_a": len(rec_a),
            "events_b": len(rec_b),
            "candidate_edges": candidate_edge_count,
            "matched_pairs": len(pairs),
            "unmatched_a": len(unmatched_a),
            "unmatched_b": len(unmatched_b),
            "matched_event_count": len(pairs) * 2,
            "mid_delta_abs_sum_ms": sum(p["mid_delta_abs_ms"] for p in pairs),
        },
        "matching": {
            "rule": "maximize pair count, then minimize sum of |interval-midpoint "
                    "differences| (integer ms, half-up rounded); global optimum "
                    "via the Hungarian algorithm, no per-event greedy",
            "pair_criteria": [
                "actor 非空且两侧完全相同（去首尾空白后区分大小写精确相等）",
                "event_type 完全相同",
                "两侧闭区间 [lo,hi] 的间距不超过容差（整数毫秒）",
                "每条事件最多参与一对",
            ],
            "rounding": "区间端点 / 中点 / 闭区间间距 / 容差均四舍五入到整数毫秒（half-up）",
            "limit_per_side": MAX_EVENTS_PER_SIDE,
        },
    }


def _paired_event(rec: dict) -> dict[str, Any]:
    """配对/未匹配行中的单侧事件信息（点击 ID 在主时间线定位用）。"""
    return {
        "id": rec["id"],
        "source_id": rec["source_id"],
        "source_name": rec["source_name"],
        "event_type": rec["event_type"],
        "actor": rec["actor"],
        "summary": rec["summary"],
        "lo_ms": rec["lo_ms"],
        "hi_ms": rec["hi_ms"],
        "mid_ms": rec["mid_ms"],
        "lo_text": rec["lo_text"],
        "hi_text": rec["hi_text"],
        "mid_text": rec["mid_text"],
        "est_text": rec["est_text"],
    }
