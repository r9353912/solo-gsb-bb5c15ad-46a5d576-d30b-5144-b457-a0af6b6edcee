"""两事件耗时的证据精确界审计。

定位
----
审计员常问："从事件 A 到事件 B 的耗时，证据能把它约束到多窄？"
朴素做法是把两事件各自的时间区间相减（``lo_B - hi_A`` 与
``hi_B - lo_A``），但这忽略了把两事件联系起来的全部先后约束
（source_order 边、因果/合规边、环允许同刻），会给出**过宽**的界。

本模块复用 ``feasibility`` 的差分约束系统：

    lo_i ≤ t_i ≤ hi_i                      事件 i 的真实时间闭区间
    t_v ≥ t_u  对每条有效约束 u→v           允许同刻（≥，非严格 >）

在**所有可行时间分配**上求 ``t_end - t_start`` 的精确最小值与最大值。

压缩 DAG（SCC condensation）
---------------------------
环（SCC）内所有事件被约束锁死为同一时刻（值可在 [max lo, min hi] 内
任取）；压缩后的 SCC 图是 DAG。分量级：

    E[c] = 分量最早时刻（max lo 正向最长路，feasibility 已给出）
    L[c] = 分量最晚时刻（min hi 反向最长路，本模块补算）

端点界（不是两区间直接相减）
---------------------------
- 最小差：把终点分量钉在其最早时刻 E[c_e]（虚拟上界 t_{c_e} ≤ E[c_e]），
  再反向传播最晚时刻 ``U_e[u] = min(L[u], 后继 U_e)``。全系统取 U_e
  即一版完整可行分配：终点 = E[c_e]，起点被其自身上界与通往终点的
  路径压到尽可能晚，``min = E[c_e] - U_e[c_s]``。
  （start 沿约束能到达 end 时 U_e[c_s]=min(L[c_s], E[c_e])，耗时
  下界自动不小于 0；无路径时 U_e[c_s]=L[c_s]，下界可为负。）
- 最大差：对称地把起点分量钉在 E[c_s]，反向传播得 U_s，则
  ``max = U_s[c_e] - E[c_s]``（end 能沿约束回到 start 时自动不大于 0）。

两版分配都逐事件回传（"全量事件时刻见证"），并在返回前用原约束逐条
自校验：每个事件落在自身闭区间内、每条边满足 t_v ≥ t_u、差值确实
等于声称的端点。

全局无解时直接复用 feasibility 的矛盾见证，不输出耗时结论。

只读：不写库、不保存假设、不受页面筛选影响；overrides 携带页面当前
未保存的时钟假设。全程迭代 Tarjan / Kahn，5000 事件无递归栈风险。
"""

from __future__ import annotations

import math
from collections import deque
from typing import Any

from .feasibility import EPS, _Solver
from .timeutil import fmt, fmt_delta


class ElapsedError(ValueError):
    """入参非法（事件未知 / 相同 / 阈值非有限非负等）。"""


# ------------------------------------------------------------------
# 求解器：feasibility._Solver + 分量最晚时刻与钉扎传播
# ------------------------------------------------------------------
class _BoundSolver(_Solver):
    def __init__(self, overrides: dict | None = None):
        super().__init__(overrides=overrides)
        self.solve()
        m = len(self.comps)

        # 压缩 DAG：每个分量的后继分量（去重；Tarjan 输出序不等于拓扑序，
        # 传播一律用下面显式 Kahn 求出的 topo 序）
        succ_set: list[set[int]] = [set() for _ in range(m)]
        indeg = [0] * m
        for edge in self.cross_edges:
            cu, cv = edge["cu"], edge["cv"]
            if cv not in succ_set[cu]:
                succ_set[cu].add(cv)
                indeg[cv] += 1
        self.succ: list[list[int]] = [sorted(ss) for ss in succ_set]
        q = deque(c for c in range(m) if indeg[c] == 0)
        self.topo: list[int] = []
        while q:
            c = q.popleft()
            self.topo.append(c)
            for v in self.succ[c]:
                indeg[v] -= 1
                if indeg[v] == 0:
                    q.append(v)

        self.E = self.comp_value                    # 分量最早（solve 已算）
        self.L = self._latest_values()              # 分量最晚

    def _latest_values(self) -> list[float]:
        """L[c] = min(分量内 min hi, 后继分量 L)，按拓扑逆序一次扫完。"""
        L = list(self.comp_min_hi)
        for c in reversed(self.topo):
            for v in self.succ[c]:
                if L[v] < L[c]:
                    L[c] = L[v]
        return L

    def pinned_latest(self, pin_comp: int, pin_value: float) -> list[float]:
        """在分量最晚时刻上叠加虚拟上界 t_{pin_comp} ≤ pin_value，
        再沿 u→v 反向传播：U[u] = min(U[u], U[v])。

        得到的 U 是一版完整可行的分量取值（初始化自 min hi，
        min 传播保持可行性与全部 t_v ≥ t_u）。
        """
        U = list(self.L)
        if pin_value < U[pin_comp]:
            U[pin_comp] = pin_value
        for c in reversed(self.topo):
            for v in self.succ[c]:
                if U[v] < U[c]:
                    U[c] = U[v]
        return U

    # ------------------------------------------------------------
    # 见证
    # ------------------------------------------------------------
    @staticmethod
    def _r6(v: float) -> float:
        return round(v, 6)

    def _elapsed_witness(self, comp_times: list[float], s: int, e: int,
                 label: str) -> dict[str, Any]:
        times = [comp_times[self.comp_of[i]] for i in range(self.n)]
        delta = times[e] - times[s]

        events = []
        for i, node in enumerate(self.nodes):
            t = times[i]
            events.append({
                "id": self.ids[i],
                "source_id": node.raw["source_id"],
                "summary": node.raw.get("summary", ""),
                "time": self._r6(t),
                "time_text": fmt(t),
                "lo": self._r6(self.lo[i]),
                "lo_text": fmt(self.lo[i]),
                "hi": self._r6(self.hi[i]),
                "hi_text": fmt(self.hi[i]),
                "slack_to_hi": self._r6(self.hi[i] - t),
                "in_cycle": len(self.comps[self.comp_of[i]]) > 1,
            })
        events.sort(key=lambda x: (x["time"], x["id"]))

        def endpoint(idx: int, role: str) -> dict[str, Any]:
            c = self.comp_of[idx]
            return {
                "role": role,
                "id": self.ids[idx],
                "source_id": self.nodes[idx].raw["source_id"],
                "summary": self.nodes[idx].raw.get("summary", ""),
                "time": self._r6(times[idx]),
                "time_text": fmt(times[idx]),
                "lo": self._r6(self.lo[idx]),
                "lo_text": fmt(self.lo[idx]),
                "hi": self._r6(self.hi[idx]),
                "hi_text": fmt(self.hi[idx]),
                "component_id": c,
                "in_cycle": len(self.comps[c]) > 1,
            }

        return {
            "kind": label,
            "elapsed": self._r6(delta),
            "elapsed_text": fmt_delta(delta),
            "start": endpoint(s, "start"),
            "end": endpoint(e, "end"),
            "events": events,
        }

    # ------------------------------------------------------------
    # 见证自校验：声称的分配必须真的可行且达到声称的差
    # ------------------------------------------------------------
    def _verify(self, comp_times: list[float], s: int, e: int,
                claimed: float) -> None:
        times = [comp_times[self.comp_of[i]] for i in range(self.n)]
        for i in range(self.n):
            if not (self.lo[i] - EPS <= times[i] <= self.hi[i] + EPS):
                raise RuntimeError(f"见证事件 {self.ids[i]} 超出自身闭区间")
        for edge in self.edges:
            u, v = edge["u"], edge["v"]
            if times[v] < times[u] - EPS:
                raise RuntimeError(
                    f"见证违反约束 {self.ids[u]}→{self.ids[v]}")
        if abs(times[e] - times[s] - claimed) > 1e-5:
            raise RuntimeError("见证未达到声称的耗时端点")

    # ------------------------------------------------------------
    # 可达性（迭代 BFS，5000 事件安全）
    # ------------------------------------------------------------
    def reaches(self, src: int, dst: int) -> bool:
        if src == dst:
            return True
        seen = {src}
        q = deque([src])
        while q:
            u = q.popleft()
            for v in self.adj[u]:
                if v == dst:
                    return True
                if v not in seen:
                    seen.add(v)
                    q.append(v)
        return False


# ------------------------------------------------------------------
# 入参校验
# ------------------------------------------------------------------
def _coerce_threshold(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ElapsedError("threshold_seconds 必须是非负有限数值（秒）")
    v = float(value)
    if not math.isfinite(v) or v < 0:
        raise ElapsedError("threshold_seconds 必须是非负有限数值（秒）")
    return v


def _resolve_event(solver: _BoundSolver, value: Any, role: str) -> int:
    if not isinstance(value, str) or not value.strip():
        raise ElapsedError(f"{role} 必须是非空事件 ID 字符串")
    idx = solver.id_idx.get(value.strip())
    if idx is None:
        raise ElapsedError(f"{role} 事件不存在: {value.strip()}")
    return idx


# ------------------------------------------------------------------
# 对外入口
# ------------------------------------------------------------------
def audit_elapsed(start_event: str, end_event: str,
                  threshold_seconds: Any,
                  overrides: dict | None = None) -> dict[str, Any]:
    """在全部可行时间分配上求 ``t_end - t_start`` 的精确最小/最大值。

    :param start_event: 起点事件 ID
    :param end_event: 终点事件 ID
    :param threshold_seconds: 非负有限阈值（秒）
    :param overrides: 页面当前未保存的来源时钟假设
    :raises ElapsedError: 事件未知、起终点相同、阈值非法
    """
    threshold = _coerce_threshold(threshold_seconds)
    solver = _BoundSolver(overrides=overrides)

    s = _resolve_event(solver, start_event, "起点(start_event)")
    e = _resolve_event(solver, end_event, "终点(end_event)")
    if s == e:
        raise ElapsedError("起点与终点必须是不同事件（同一事件耗时恒为 0，无需审计）")

    c_s, c_e = solver.comp_of[s], solver.comp_of[e]
    stats = {
        "event_count": solver.n,
        "constraint_count": len(solver.edges),
        "skipped_constraint_count": solver.skipped_edges,
        "component_count": len(solver.comps),
        "cycle_count": sum(1 for c in solver.comps if len(c) > 1),
        "events_in_cycles": sum(len(c) for c in solver.comps if len(c) > 1),
    }

    # 全局无解：展示现有矛盾见证，不输出耗时结论
    base = solver.result()
    if not base["feasible"]:
        return {
            "check_type": "elapsed-time-bounds",
            "feasible": False,
            "start_event": solver.ids[s],
            "end_event": solver.ids[e],
            "threshold_seconds": threshold,
            "stats": stats,
            "summary": "当前时钟假设下，全量事件区间与全部约束联立无解，"
                       "无法定义两事件耗时；请先处理下列矛盾见证。",
            "witness": base["witness"],
        }

    # 最小见证：终点钉在 E[c_e] 的全系统最晚可行分配
    U_min = solver.pinned_latest(c_e, solver.E[c_e])
    min_delta = solver.E[c_e] - U_min[c_s]
    # 最大见证：起点钉在 E[c_s] 的全系统最晚可行分配
    U_max = solver.pinned_latest(c_s, solver.E[c_s])
    max_delta = U_max[c_e] - solver.E[c_s]

    solver._verify(U_min, s, e, min_delta)
    solver._verify(U_max, s, e, max_delta)

    min_w = solver._elapsed_witness(U_min, s, e, "minimum")
    max_w = solver._elapsed_witness(U_max, s, e, "maximum")

    start_reaches_end = solver.reaches(s, e)
    end_reaches_start = solver.reaches(e, s)

    # 判定：最小 > 阈值 必超；最大 ≤ 阈值 必不超；其余可能超
    if min_delta > threshold + EPS:
        verdict, verdict_text = "must_exceed", "必超"
        summary = (f"在所有可行时间分配中，{solver.ids[e]} 相对 {solver.ids[s]} "
                   f"的耗时都大于 {fmt_delta(threshold)}：最小也有 "
                   f"{min_w['elapsed_text']}，证据必然超出阈值。")
    elif max_delta <= threshold + EPS:
        verdict, verdict_text = "must_not_exceed", "必不超"
        summary = (f"在所有可行时间分配中，{solver.ids[e]} 相对 {solver.ids[s]} "
                   f"的耗时都不超过 {fmt_delta(threshold)}：最大仅 "
                   f"{max_w['elapsed_text']}，证据必然不超阈值。")
    else:
        verdict, verdict_text = "possible_exceed", "可能超"
        summary = (f"证据不能锁定耗时是否超过 {fmt_delta(threshold)}："
                   f"可行范围 {min_w['elapsed_text']} ~ "
                   f"{max_w['elapsed_text']} 跨越阈值，存在超过与不超过的分配。")

    return {
        "check_type": "elapsed-time-bounds",
        "feasible": True,
        "start_event": solver.ids[s],
        "end_event": solver.ids[e],
        "threshold_seconds": threshold,
        "threshold_text": fmt_delta(threshold),
        "verdict": verdict,
        "verdict_text": verdict_text,
        "bounds": {
            "minimum_seconds": round(min_delta, 6),
            "minimum_text": min_w["elapsed_text"],
            "maximum_seconds": round(max_delta, 6),
            "maximum_text": max_w["elapsed_text"],
            # 朴素两区间直接相减的结果（仅供对照，证明约束把界收窄了）
            "naive_minimum_seconds": round(solver.lo[e] - solver.hi[s], 6),
            "naive_maximum_seconds": round(solver.hi[e] - solver.lo[s], 6),
            "start_reaches_end": start_reaches_end,
            "end_reaches_start": end_reaches_start,
            "same_component": c_s == c_e,
            "start_component": c_s,
            "end_component": c_e,
        },
        "minimum_witness": min_w,
        "maximum_witness": max_w,
        "stats": stats,
        "summary": summary + " 端点由全量事件区间与全部先后约束联立求出"
                           "（允许同刻），不是两事件各自区间直接相减。",
        "notice": "只读检查：复用现有时间区间与全部有效约束（含 source_order），"
                  "携带页面未保存的来源假设；不保存假设、不改写数据、不受页面筛选影响。",
    }
