"""全局时序可行性联立求解：全量事件区间 × 全部有效约束，一次性判定。

模型
----
沿用 ``engine`` 的时钟模型：每个事件 i 的真实时刻只能落在闭区间
``[lo_i, hi_i]``；每条有效约束边 a → b 要求 ``t_b ≥ t_a``（允许同刻）。
问题即差分约束系统

    lo_i ≤ t_i ≤ hi_i          （每个事件）
    t_b - t_a ≥ 0              （每条约束边）

求解
----
1. 迭代版 Tarjan 求强连通分量（复用 ``engine._tarjan_scc``，无递归栈风险）。
   环（非平凡 SCC）内所有事件必须取**同一时刻**，因此环可行 ⟺ 环内区间
   有公共交集；该条件被下面的统一判定自然覆盖（环内每个成员都被钉在
   公共下界 max(lo) 上，逐一检查是否超过各自 hi）。
2. 在压缩后的 DAG 上用 Kahn 拓扑序做最长路传播：
   ``earliest_c = max(分量内 max(lo), 所有前驱分量的 earliest)``。
   每个事件的最早可行时刻即其所在分量的 earliest。
3. 对每个分量检查 ``earliest_c ≤ min(hi)``（容差 EPS）。全部满足 → 可行，
   返回每个事件的最早可行时刻作为**见证**；任一不满足 → 无解。

无解见证（矛盾路径）
--------------------
取超限最严重的分量 Y，沿传播时记录的前驱链回溯到某个分量的 max-lo 事件 x，
得到一条事件路径 x ⇝ y（y 为 Y 内 hi 最小的事件）。路径把下界 lo_x 一路
传递到 y，而 lo_x > hi_y：路径两端界限直接冲突。路径上每一步都标注约束
ID 与"该边单独是否可行"（lo_a ≤ hi_b），因此**各边单独可行而整条路径无解**
的情形（典型：环内区间无公共交集）同样被覆盖——此时路径在环内闭合。

全程迭代实现（Tarjan / Kahn / BFS），5000 事件规模无递归栈溢出风险；
只读数据库，不改写任何事件或假设。
"""
from __future__ import annotations

from collections import deque
from typing import Any

from . import engine
from .timeutil import fmt, fmt_delta

EPS = engine.EPS


# ------------------------------------------------------------ 内部结构

class _Solver:
    """一次可行性求解的全部中间状态（避免长参数链）。

    drop_ids: 本次求解要排除的约束 id 集合（约束撤销建议的复核场景；
    仅在内存中跳过，不写数据库）。model: 预载的
    ``engine.load_model()`` 三元组，批量复核时避免重复读库。
    """

    def __init__(self, overrides: dict | None,
                 drop_ids: set[int] | None = None,
                 model: tuple | None = None):
        if model is None:
            model = engine.load_model()
        stored_sources, raw_events, raw_edges = model
        sources = engine._effective_sources(stored_sources, overrides)
        nodes = engine._build_nodes(raw_events, sources)
        self.nodes = nodes
        self.ids = [n.raw["id"] for n in nodes]
        self.id_idx = {eid: i for i, eid in enumerate(self.ids)}
        self.lo = [n.lo for n in nodes]
        self.hi = [n.hi for n in nodes]
        self.n = len(nodes)

        # 全部有效约束（端点都存在），含 source_order 与不可能的边
        self.edges: list[dict] = []
        skipped = 0
        for r in raw_edges:
            if drop_ids and r.get("id") in drop_ids:
                continue  # 撤销建议预览：视为已撤销，仅在内存中排除
            u = self.id_idx.get(r["from_event"])
            v = self.id_idx.get(r["to_event"])
            if u is None or v is None:
                skipped += 1  # 端点事件已被删除的悬空约束：无法参与联立
                continue
            self.edges.append({
                "id": r.get("id"),
                "kind": r.get("kind", "causal"),
                "note": r.get("note", ""),
                "u": u,
                "v": v,
            })
        self.skipped_edges = skipped

        # 事件图（供 Tarjan / 环内 BFS）
        self.adj: list[list[int]] = [[] for _ in range(self.n)]
        for e in self.edges:
            self.adj[e["u"]].append(e["v"])

        # 同一对端点间可能有多条约束（如 source_order 与声明约束）：
        # 见证逐步取约束 id 最小的一条，预建索引保证 O(1) 查询
        self.edge_between: dict[tuple[int, int], dict] = {}
        for e in self.edges:
            key = (e["u"], e["v"])
            cur = self.edge_between.get(key)
            if cur is None or (e["id"] or 0) < (cur["id"] or 0):
                self.edge_between[key] = e

        # 分量级结构（solve() 填充）
        self.comp_of: list[int] = []
        self.comps: list[list[int]] = []
        self.comp_value: list[float] = []       # 最早可行时刻（分量级）
        self.comp_base_lo: list[float] = []     # 分量内 max(lo)
        self.comp_base_src: list[int] = []      # 取得 max(lo) 的事件下标
        self.comp_min_hi: list[float] = []      # 分量内 min(hi)
        self.comp_min_hi_src: list[int] = []    # 取得 min(hi) 的事件下标
        self.comp_pred: list[tuple[int, dict] | None] = []  # 传播前驱 (分量, 跨边)
        self.cross_edges: list[dict] = []       # 跨分量边（含分量 id）

    # ------------------------------------------------------------ 求解

    def solve(self) -> None:
        n = self.n
        comps = engine._tarjan_scc(self.adj)
        self.comps = comps
        self.comp_of = [0] * n
        for ci, members in enumerate(comps):
            for i in members:
                self.comp_of[i] = ci

        m = len(comps)
        self.comp_base_lo = [0.0] * m
        self.comp_base_src = [-1] * m
        self.comp_min_hi = [0.0] * m
        self.comp_min_hi_src = [-1] * m
        for ci, members in enumerate(comps):
            # 确定性 tie-break：同值取下标更小（事件 id 字典序）者
            src_lo = max(members, key=lambda i: (self.lo[i], -i))
            src_hi = min(members, key=lambda i: (self.hi[i], i))
            self.comp_base_lo[ci] = self.lo[src_lo]
            self.comp_base_src[ci] = src_lo
            self.comp_min_hi[ci] = self.hi[src_hi]
            self.comp_min_hi_src[ci] = src_hi

        # 压缩 DAG 上的跨分量边
        cadj: list[list[int]] = [[] for _ in range(m)]
        indeg = [0] * m
        for e in self.edges:
            cu, cv = self.comp_of[e["u"]], self.comp_of[e["v"]]
            if cu == cv:
                continue
            rec = dict(e)
            rec["cu"], rec["cv"] = cu, cv
            self.cross_edges.append(rec)
            cadj[cu].append(len(self.cross_edges) - 1)
            indeg[cv] += 1

        # Kahn 拓扑 + 最长路传播（迭代，无递归）
        self.comp_value = list(self.comp_base_lo)
        self.comp_pred = [None] * m
        queue = deque(ci for ci in range(m) if indeg[ci] == 0)
        while queue:
            cu = queue.popleft()
            val = self.comp_value[cu]
            for ei in cadj[cu]:
                e = self.cross_edges[ei]
                cv = e["cv"]
                if val > self.comp_value[cv] + EPS:
                    self.comp_value[cv] = val
                    self.comp_pred[cv] = (cu, e)
                indeg[cv] -= 1
                if indeg[cv] == 0:
                    queue.append(cv)

    # ------------------------------------------------------------ 环内路径

    def _bfs_intra(self, comp: set[int], src: int, dst: int) -> list[int] | None:
        """在分量内部求 src → dst 的一条有向路径（迭代 BFS）。"""
        if src == dst:
            return [src]
        prev: dict[int, int | None] = {src: None}
        queue = deque([src])
        while queue:
            u = queue.popleft()
            for v in self.adj[u]:
                if v not in comp or v in prev:
                    continue
                prev[v] = u
                if v == dst:
                    path = [v]
                    cur: int | None = u
                    while cur is not None:
                        path.append(cur)
                        cur = prev[cur]
                    path.reverse()
                    return path
                queue.append(v)
        return None

    def _pick_edge(self, u: int, v: int) -> dict:
        """同一对端点间取约束 id 最小的一条（确定性）。"""
        e = self.edge_between.get((u, v))
        return e if e is not None else {"id": None, "kind": "", "note": ""}

    # ------------------------------------------------------------ 见证组装

    def _event_path(self, y_comp: int) -> list[int]:
        """把分量级前驱链展开为事件路径：x（源分量 max-lo 事件）⇝ y 分量内某点。"""
        # 从 y_comp 沿 pred 回溯：comp_chain[0]=y_comp … comp_chain[-1]=源分量
        comp_chain: list[int] = []
        pred_of: dict[int, tuple[int, dict]] = {}
        seen: set[int] = set()
        c = y_comp
        while c not in seen:
            seen.add(c)
            comp_chain.append(c)
            pred = self.comp_pred[c]
            if pred is None:
                break
            pred_of[c] = pred
            c = pred[0]

        # 正向展开：源分量的 max-lo 事件出发，逐段环内 BFS + 跨分量边
        path = [self.comp_base_src[comp_chain[-1]]]
        for k in range(len(comp_chain) - 1, 0, -1):
            cur = comp_chain[k]
            cu, edge = pred_of[comp_chain[k - 1]]
            if cu != cur:  # 防御：前驱链与当前分量不一致（不应发生）
                break
            seg = self._bfs_intra(set(self.comps[cur]), path[-1], edge["u"])
            if seg:
                path.extend(seg[1:])
            path.append(edge["v"])
        return path

    def _witness(self, y_comp: int) -> dict:
        """为超限分量 y_comp 生成矛盾路径见证。"""
        y = self.comp_min_hi_src[y_comp]
        path = self._event_path(y_comp)
        if path[-1] != y:  # 末尾补到 y（同分量内）
            comp_set = set(self.comps[y_comp])
            seg = self._bfs_intra(comp_set, path[-1], y)
            if seg:
                path.extend(seg[1:])
            else:
                path.append(y)

        x = path[0]
        steps: list[dict] = []
        running = self.lo[x]
        for a, b in zip(path, path[1:]):
            e = self._pick_edge(a, b)
            running = max(running, self.lo[b])
            steps.append({
                "from": self.ids[a],
                "to": self.ids[b],
                "constraint_id": e["id"],
                "kind": e["kind"],
                "note": e["note"],
                # 该边单独可行 ⟺ 两端区间存在满足 t_b ≥ t_a 的取值
                "individually_feasible": bool(self.lo[a] <= self.hi[b] + EPS),
                "running_lo": round(running, 6),
            })

        infeasible_edges = sum(1 for s in steps if not s["individually_feasible"])
        overshoot = self.comp_value[y_comp] - self.comp_min_hi[y_comp]
        head, tail = self.ids[x], self.ids[y]
        if infeasible_edges:
            detail = (f"其中 {infeasible_edges} 条边单独即不可能（终点最晚时刻仍早于"
                      "起点最早时刻），请优先核对其时钟假设或事件归属")
        else:
            detail = ("路径上每条边单独都可行（两端区间两两相容），但联立后下界沿路径"
                      "逐段传递、最终越过终点上界——典型的环内区间无公共交集或长链累积")
        explanation = (
            f"全局联立无解：从 {head} 的区间下界 {fmt(self.lo[x])} 出发，沿 "
            f"{len(steps)} 条约束传递，到 {tail} 时要求的最早时刻已超出其区间上界 "
            f"{fmt(self.hi[y])}（超出 {fmt_delta(overshoot)}）。{detail}。"
            "请调整相关来源的时钟偏差 / 不确定度假设，或核对约束与事件归属。"
        )
        return {
            "head": {"event": head, "bound": "lo",
                     "value": round(self.lo[x], 6), "text": fmt(self.lo[x])},
            "tail": {"event": tail, "bound": "hi",
                     "value": round(self.hi[y], 6), "text": fmt(self.hi[y])},
            "overshoot": round(overshoot, 6),
            "overshoot_text": fmt_delta(overshoot),
            "events": [self.ids[i] for i in path],
            "steps": steps,
            "all_edges_individually_feasible": infeasible_edges == 0,
            "explanation": explanation,
        }

    # ------------------------------------------------------------ 主入口

    def result(self) -> dict[str, Any]:
        self.solve()
        m = len(self.comps)

        # 超限最严重的分量（overshoot 大者优先；并列取 min-hi 事件 id 小者）
        failed: int | None = None
        best_key: tuple[float, str] | None = None
        for ci in range(m):
            overshoot = self.comp_value[ci] - self.comp_min_hi[ci]
            if overshoot <= EPS:
                continue
            key = (overshoot, self.ids[self.comp_min_hi_src[ci]])
            if best_key is None or key[0] > best_key[0] + EPS or (
                    abs(key[0] - best_key[0]) <= EPS and key[1] < best_key[1]):
                best_key = key
                failed = ci

        cycle_comps = [ci for ci in range(m) if len(self.comps[ci]) > 1]
        cycle_member = [False] * self.n
        for ci in cycle_comps:
            for i in self.comps[ci]:
                cycle_member[i] = True

        stats = {
            "event_count": self.n,
            "constraint_count": len(self.edges),
            "skipped_constraints": self.skipped_edges,
            "component_count": m,
            "cycle_count": len(cycle_comps),
            "events_in_cycles": sum(cycle_member),
        }
        base: dict[str, Any] = {
            "check_type": "global-temporal-feasibility",
            "stats": stats,
        }

        if failed is not None:
            base.update({
                "feasible": False,
                "witness": self._witness(failed),
                "summary": "当前时钟假设下，全量事件区间与全部约束联立无解。",
            })
            return base

        events_out = []
        for i, node in enumerate(self.nodes):
            t = self.comp_value[self.comp_of[i]]
            events_out.append({
                "id": self.ids[i],
                "source_id": node.raw["source_id"],
                "summary": node.raw.get("summary", ""),
                "earliest": round(t, 6),
                "earliest_text": fmt(t),
                "lo": round(self.lo[i], 6),
                "hi": round(self.hi[i], 6),
                "lo_text": fmt(self.lo[i]),
                "hi_text": fmt(self.hi[i]),
                "slack": round(self.hi[i] - t, 6),
                "in_cycle": cycle_member[i],
            })
        events_out.sort(key=lambda e: (e["earliest"], e["id"]))
        base.update({
            "feasible": True,
            "events": events_out,
            "summary": (f"当前时钟假设下全局时序可行：{self.n} 个事件与 "
                        f"{len(self.edges)} 条约束联立有解，"
                        "以下为每个事件的最早可行时刻（见证）。"),
        })
        return base


def check_feasibility(overrides: dict | None = None,
                      drop_ids: set[int] | None = None) -> dict[str, Any]:
    """以当前生效假设（库存 + 页面未保存 overrides）联立求解全局时序可行性。

    只读：不写数据库、不改写事件或假设；不受页面筛选影响（全量事件与约束）。
    drop_ids 仅在内存中排除指定约束（撤销建议预览用），不落库。
    """
    return _Solver(overrides, drop_ids=drop_ids).result()
