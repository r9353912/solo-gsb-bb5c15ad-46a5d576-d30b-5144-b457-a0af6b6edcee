"""核心推理：时钟偏差修正 → 时间区间 → 约束传播 → 冲突链 / 歧义定位。

模型
----
对来源 s 的观测本地时间 t_local，真实时间估计：

    t_real ≈ t_local - (clock_offset_s + drift_ppm*1e-6 * (t_local - t_ref))

真实时间只可能落在一个区间内（时钟永远不可能被绝对校准）：

    [t_real - uncertainty_s, t_real + uncertainty_s]

事件还携带观测固有抖动 sigma（JSON 中可选 sigma/error_ms），最终区间半径取二者之和。

对约束边 a → b（a 必须先于 b），真实间隔是一个区间：

    gap ∈ [ lo_b - hi_a ,  hi_b - lo_a ]

- gap_hi < 0：b 必定早于 a，**顺序不可能（直接冲突）**；
- gap_lo ≥ 0：a 必定先于 b，顺序成立；
- 其余：区间跨越 0，二者先后无法判定。

在“可能成立”的边上做传递闭包：若 a→b 直接不可能，却存在一条全绿的
b ⇝ a 路径，则形成矛盾链（含环）。以上即为可向审计员解释的冲突证据链。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .db import db, row_to_dict
from .timeutil import fmt, fmt_delta

EPS = 1e-6
CONFLICT_LIMIT = 100  # 最多输出的矛盾链条数

SOURCE_PARAMS = ("clock_offset", "drift_ppm", "uncertainty", "reliability")


@dataclass
class Node:
    raw: dict
    source: dict
    est: float
    lo: float
    hi: float
    radius: float
    sigma: float
    time_conf: float
    confidence: float
    idx: int = -1
    ambig_group: int | None = None
    order_key: float = 0.0


@dataclass
class Edge:
    id: int | None
    frm: str
    to: str
    kind: str
    note: str
    gap_lo: float = 0.0
    gap_hi: float = 0.0
    status: str = "ok"          # ok | boundary | impossible
    explanation: str = ""
    conflict_ids: list = field(default_factory=list)


# ------------------------------------------------------------ 数据装载

def load_model() -> tuple[dict[str, dict], list[dict], list[dict]]:
    with db() as conn:
        sources = {r["id"]: row_to_dict(r)
                   for r in conn.execute("SELECT * FROM sources ORDER BY id")}
        events = [row_to_dict(r)
                  for r in conn.execute("SELECT * FROM events ORDER BY local_ts, id")]
        edges = [row_to_dict(r)
                 for r in conn.execute("SELECT * FROM constraints ORDER BY id")]
    return sources, events, edges


def _effective_sources(stored: dict[str, dict], overrides: dict | None) -> dict[str, dict]:
    """合并库存假设与审计员当前（可能尚未保存）的调整。"""
    overrides = overrides or {}
    out: dict[str, dict] = {}
    for sid, s in stored.items():
        eff = dict(s)
        ov = overrides.get(sid) or {}
        for k in SOURCE_PARAMS:
            if k in ov and ov[k] is not None:
                try:
                    eff[k] = float(ov[k])
                except (TypeError, ValueError):
                    pass
        eff["uncertainty"] = max(0.0, eff["uncertainty"])
        eff["reliability"] = min(1.0, max(0.0, eff["reliability"]))
        out[sid] = eff
    return out


# ------------------------------------------------------------ 区间与置信度

def _build_nodes(events: list[dict], sources: dict[str, dict]) -> list[Node]:
    locals_ = [e["local_ts"] for e in events]
    t_ref = min(locals_) if locals_ else 0.0
    nodes: list[Node] = []

    for e in events:
        s = sources.get(e["source_id"])
        if s is None:  # 防御：来源被删后
            s = {"id": e["source_id"], "name": e["source_id"], "clock_offset": 0.0,
                 "drift_ppm": 0.0, "uncertainty": 1.0, "reliability": 0.5}
        offset = s["clock_offset"] + s["drift_ppm"] * 1e-6 * (e["local_ts"] - t_ref)
        est = e["local_ts"] - offset
        try:
            raw = e.get("raw") or {}
            sigma = float(raw.get("sigma",
                                 raw.get("error_ms", 0) / 1000.0 if "error_ms" in raw else 0))
        except (TypeError, ValueError):
            sigma = 0.0
        sigma = max(0.0, sigma)
        radius = s["uncertainty"] + sigma
        # 时间可信度：区间半径越小越高（5 秒半径对应 0.5），再与来源可靠度融合。
        time_conf = 5.0 / (5.0 + radius)
        confidence = round(0.6 * time_conf + 0.4 * s["reliability"], 4)
        nodes.append(Node(raw=e, source=s, est=est, lo=est - radius, hi=est + radius,
                          radius=radius, sigma=sigma, time_conf=round(time_conf, 4),
                          confidence=confidence))
    return nodes


# ------------------------------------------------------------ 边分类

def _classify_edges(nodes: list[Node], raw_edges: list[dict]) -> list[Edge]:
    by_id = {n.raw["id"]: n for n in nodes}
    edges: list[Edge] = []
    for r in raw_edges:
        a, b = by_id.get(r["from_event"]), by_id.get(r["to_event"])
        if a is None or b is None:
            continue
        e = Edge(id=r.get("id"), frm=a.raw["id"], to=b.raw["id"],
                 kind=r.get("kind", "causal"), note=r.get("note", ""))
        e.gap_lo = round(b.lo - a.hi, 6)
        e.gap_hi = round(b.hi - a.lo, 6)
        if e.gap_hi < -EPS:
            e.status = "impossible"
            e.explanation = (
                f"按两端真实时间区间，终点最晚 {fmt(b.hi)} 仍早于起点最早 {fmt(a.lo)}"
                f"（最有利间隔 {fmt_delta(e.gap_hi)} < 0），该顺序不可能成立。"
            )
        elif e.gap_lo < -EPS:
            e.status = "ok"  # 方向成立但区间重叠，顺序“可能但不唯一”
            e.explanation = (
                f"最早可能间隔 {fmt_delta(e.gap_lo)}、最晚 {fmt_delta(e.gap_hi)}；"
                "方向可能成立，但区间存在重叠，先后无法完全锁定。"
            )
        elif e.gap_lo <= EPS:
            e.status = "boundary"
            e.explanation = "两端区间仅在边界相接，顺序未被严格支持，证据非常脆弱。"
        else:
            e.status = "ok"
            e.explanation = f"最有利间隔 {fmt_delta(e.gap_lo)} ≥ 0，顺序被时间区间支持。"
        edges.append(e)
    return edges


# ------------------------------------------------------------ 传递闭包/环

def _graph(nodes: list[Node], edges: list[Edge]):
    """在非不可能边上构建正/反向邻接表（不做全量闭包，按冲突边按需搜索）。"""
    id_idx = {n.raw["id"]: i for i, n in enumerate(nodes)}
    for i, n in enumerate(nodes):
        n.idx = i
    n = len(nodes)
    adj: list[list[int]] = [[] for _ in range(n)]
    for e in edges:
        if e.status == "impossible":
            continue
        u, v = id_idx[e.frm], id_idx[e.to]
        adj[u].append(v)
    return id_idx, adj


def _find_path(adj: list[list[int]], src: int, target: int) -> list[int] | None:
    """BFS 求 src -> target 的一条有向路径。"""
    if src == target:
        return [src]
    prev: dict[int, int | None] = {src: None}
    q = [src]
    head = 0
    while head < len(q):
        u = q[head]
        head += 1
        for v in adj[u]:
            if v in prev:
                continue
            prev[v] = u
            if v == target:
                path = [v]
                cur: int | None = u
                while cur is not None:
                    path.append(cur)
                    cur = prev[cur]
                path.reverse()
                return path
            q.append(v)
    return None


def _tarjan_scc(adj: list[list[int]]) -> list[list[int]]:
    """迭代版 Tarjan，避免大图（5000 节点）下递归栈压力。"""
    n = len(adj)
    index = [-1] * n
    low = [0] * n
    on = [False] * n
    stack: list[int] = []
    result: list[list[int]] = []
    counter = 0

    for root in range(n):
        if index[root] >= 0:
            continue
        work: list[tuple[int, int]] = [(root, 0)]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on[root] = True
        while work:
            v, pi = work[-1]
            if pi < len(adj[v]):
                w = adj[v][pi]
                work[-1] = (v, pi + 1)
                if index[w] < 0:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on[w] = True
                    work.append((w, 0))
                elif on[w]:
                    low[v] = min(low[v], index[w])
            else:
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[v])
                if low[v] == index[v]:
                    comp: list[int] = []
                    while True:
                        w = stack.pop()
                        on[w] = False
                        comp.append(w)
                        if w == v:
                            break
                    result.append(comp)
    return result


# ------------------------------------------------------------ 歧义聚类

def _ambiguity_clusters(nodes: list[Node]) -> list[list[Node]]:
    """区间互相重叠的事件可能顺序不定；按估计时间扫描 + 并查集聚类。"""
    parent = list(range(len(nodes)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    order = sorted(range(len(nodes)), key=lambda i: nodes[i].est)
    active: list[int] = []
    for i in order:
        hi = nodes[i].lo
        active = [j for j in active if nodes[j].hi >= hi - EPS]
        for j in active:
            union(i, j)
        active.append(i)

    groups: dict[int, list[Node]] = {}
    for i, n in enumerate(nodes):
        groups.setdefault(find(i), []).append(n)
    clusters = list(groups.values())
    clusters.sort(key=lambda g: min(n.lo for n in g))
    return clusters


# ------------------------------------------------------------ 主入口

def compute(overrides: dict | None = None) -> dict:
    stored_sources, raw_events, raw_edges = load_model()
    sources = _effective_sources(stored_sources, overrides)
    nodes = _build_nodes(raw_events, sources)
    ids = [n.raw["id"] for n in nodes]
    edges = _classify_edges(nodes, raw_edges)
    id_idx, adj = _graph(nodes, edges)
    edge_between: dict[tuple[int, int], Edge] = {}
    for e in edges:
        edge_between[(id_idx[e.frm], id_idx[e.to])] = e

    conflicts: list[dict] = []
    used_edges: set[int] = set()
    cid = 0

    def add_conflict(kind: str, severity: str, chain_edges: list[Edge],
                     event_ids: list[str], explanation: str, signature: str) -> None:
        nonlocal cid
        cid += 1
        cid_str = f"C{cid:03d}"
        for e in chain_edges:
            e.conflict_ids.append(cid_str)
            if e.id is not None:
                used_edges.add(e.id)
        conflicts.append({
            "id": cid_str,
            "kind": kind,
            "severity": severity,
            # signature 与编号无关：同一矛盾在不同假设下重算后仍可匹配（用于方案对比）
            "signature": signature,
            "events": event_ids,
            "edge_ids": [e.id for e in chain_edges],
            "edge_keys": [{"from": e.frm, "to": e.to, "kind": e.kind,
                           "status": e.status} for e in chain_edges],
            "explanation": explanation,
        })

    # 1) 不可能顺序：直接边不可能 + 反向传递路径 => 矛盾链
    for e in edges:
        if len(conflicts) >= CONFLICT_LIMIT:
            break
        if e.status != "impossible":
            continue
        u, v = id_idx[e.frm], id_idx[e.to]
        chain_events = [ids[u]]
        chain_edges = [e]
        # 是否存在一条“顺序成立”的链 v ⇝ u，与本不可能边闭合为矛盾
        path = _find_path(adj, v, u) if u != v else None
        if path:
            # path 为 v -> ... -> u：从终点 v 开始展示，最后由不可能边回到 v
            chain_events = [ids[n] for n in path] + [ids[v]]
            for a, b in zip(path, path[1:]):
                pe = edge_between.get((a, b))
                if pe is not None:
                    chain_edges.append(pe)
            chain_edges.append(e)
            text = (
                f"约束要求 {e.frm} 先于 {e.to}，但其时间区间显示该顺序不可能；"
                f"同时存在 {len(path) - 1} 条顺序成立的链路（{e.to} ⇝ {e.frm}），"
                "二者闭合为矛盾。请检查相关来源的时钟偏差假设，或核对事件归属。"
            )
        else:
            chain_events.append(ids[v])
            text = (
                f"约束要求 {e.frm} 先于 {e.to}，但即使取两端最有利的时间区间，"
                f"{e.to} 仍必定早于 {e.frm}（{e.explanation}）"
                "——典型成因是来源时钟偏差假设错误或事件时间被伪造/误抄。"
            )
        # 不可能边由其端点唯一标识（DB 边 id 同样稳定，端点更稳健）
        sig = f"impossible:{e.frm}->{e.to}"
        add_conflict("impossible_order", "high", chain_edges, chain_events, text, sig)

    # 2) 环：由“可能成立”的边构成的强连通分量本身即逻辑矛盾
    if len(conflicts) < CONFLICT_LIMIT:
        for comp in _tarjan_scc(adj):
            if len(comp) <= 1:
                continue
            comp_set = set(comp)
            # 环内节点按 est 排列，并取沿图的一条实际闭合路径
            comp_edges = [edge_between[(a, b)] for a in comp for b in adj[a]
                          if b in comp_set and (a, b) in edge_between]
            ev_ids = [ids[i] for i in comp]
            text = (
                f"{len(comp)} 个事件通过 {len(comp_edges)} 条“顺序成立”的约束首尾相连，"
                "形成因果环；真实时间不可能同时满足全部先后关系，至少有一条约束或时钟假设错误。"
            )
            add_conflict("causal_cycle", "high", comp_edges[:12], ev_ids[:16], text,
                         f"cycle:{','.join(sorted(ev_ids))}")
            if len(conflicts) >= CONFLICT_LIMIT:
                break

    # 3) 时间歧义：区间重叠且跨多来源，事件先后无法判定
    clusters = _ambiguity_clusters(nodes)
    ambig_groups: list[dict] = []
    gid = 0
    for g in clusters:
        srcs = {n.raw["source_id"] for n in g}
        if len(g) < 2 or len(srcs) < 2:
            continue
        gid += 1
        label = f"A{gid:02d}"
        for n in g:
            n.ambig_group = label
        group_ids = sorted(n.raw["id"] for n in g)
        ambig_groups.append({
            "id": label,
            # 同样提供与编号无关的签名，跨方案匹配同一歧义组
            "signature": "ambig:" + ",".join(group_ids),
            "events": [n.raw["id"] for n in sorted(g, key=lambda x: x.est)],
            "source_count": len(srcs),
            "overlap_window": round(max(n.hi for n in g) - min(n.lo for n in g), 6),
            "explanation": (
                f"{len(g)} 个来自 {len(srcs)} 个来源的事件时间区间互相重叠，"
                "在当前时钟不确定度下先后顺序无法唯一确定；收窄来源不确定度或增加偏差证据可消除歧义。"
            ),
        })

    # 时间线排序：估计时间为主，同刻按 (来源, 本地时间, id) 稳定排序
    order = sorted(range(len(nodes)),
                   key=lambda i: (nodes[i].est, nodes[i].raw["source_id"],
                                  nodes[i].raw["local_ts"], ids[i]))
    for rank, i in enumerate(order):
        nodes[i].order_key = rank

    # ------------------------------------------------------------ 序列化
    event_count_by_source: dict[str, int] = {}
    for n in nodes:
        event_count_by_source[n.raw["source_id"]] = \
            event_count_by_source.get(n.raw["source_id"], 0) + 1

    out_sources = []
    for sid, eff in sources.items():
        stored = stored_sources.get(sid, {})
        out_sources.append({
            "id": sid,
            "name": eff.get("name", sid),
            "kind": eff.get("kind", "external"),
            "event_count": event_count_by_source.get(sid, 0),
            "effective": {k: round(float(eff[k]), 6) for k in SOURCE_PARAMS},
            "stored": {k: round(float(stored.get(k, eff[k])), 6) for k in SOURCE_PARAMS},
            "dirty": any(abs(float(eff[k]) - float(stored.get(k, eff[k]))) > 1e-9
                         for k in SOURCE_PARAMS),
        })

    out_events = []
    for n in sorted(nodes, key=lambda x: x.order_key):
        e = n.raw
        out_events.append({
            "id": e["id"],
            "source_id": e["source_id"],
            "source_name": n.source.get("name", e["source_id"]),
            "event_type": e["event_type"],
            "actor": e.get("actor"),
            "summary": e.get("summary", ""),
            "seq": e.get("seq"),
            "local_ts": e["local_ts"],
            "local_ts_text": e.get("ts_text") or fmt(e["local_ts"]),
            "est": round(n.est, 6),
            "est_text": fmt(n.est),
            "lo": round(n.lo, 6),
            "hi": round(n.hi, 6),
            "lo_text": fmt(n.lo),
            "hi_text": fmt(n.hi),
            "radius": round(n.radius, 6),
            "time_confidence": n.time_conf,
            "confidence": n.confidence,
            "ambiguity_group": n.ambig_group,
            "raw": e.get("raw", {}),
        })

    out_edges = [{
        "id": e.id,
        "from": e.frm,
        "to": e.to,
        "kind": e.kind,
        "note": e.note,
        "gap_lo": e.gap_lo,
        "gap_hi": e.gap_hi,
        "gap_lo_text": fmt_delta(e.gap_lo),
        "gap_hi_text": fmt_delta(e.gap_hi),
        "status": e.status,
        "conflict_ids": e.conflict_ids,
        "explanation": e.explanation,
    } for e in edges]

    stats = {
        "event_count": len(nodes),
        "source_count": len(sources),
        "edge_count": len(edges),
        "impossible_edges": sum(1 for e in edges if e.status == "impossible"),
        "boundary_edges": sum(1 for e in edges if e.status == "boundary"),
        "conflict_count": len(conflicts),
        "ambiguity_group_count": len(ambig_groups),
        "ambiguous_event_count": sum(1 for n in nodes if n.ambig_group),
        "timeline_span": round(max((n.hi for n in nodes), default=0)
                               - min((n.lo for n in nodes), default=0), 6),
    }
    return {
        "stats": stats,
        "sources": out_sources,
        "events": out_events,
        "edges": out_edges,
        "conflicts": conflicts,
        "ambiguity_groups": ambig_groups,
    }


def build_export(overrides: dict | None = None) -> dict:
    from datetime import datetime, timezone
    result = compute(overrides)
    return {
        "export_type": "event-evidence-chain-audit",
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "clock_model": {
            "formula": "t_real = t_local - clock_offset - drift_ppm*1e-6*(t_local-t_ref)",
            "interval": "[t_real - uncertainty - sigma, t_real + uncertainty + sigma]",
            "reference_epoch": "数据集中最早的本地时间戳",
        },
        "applied_source_assumptions": {
            s["id"]: s["effective"] for s in result["sources"]
        },
        **result,
    }
