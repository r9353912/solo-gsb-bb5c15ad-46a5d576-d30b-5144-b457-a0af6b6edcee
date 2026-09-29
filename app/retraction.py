"""约束撤销建议：全局联立无解时，搜索需撤销的最少人工/导入约束。

定位
----
``feasibility`` 回答"当前时钟假设下全量联立是否可行"；本模块回答它的
追问：**无解时，最少撤销哪些人工录入或导入的约束就能恢复可行？**

- 只有人工/导入约束（kind 不是 ``source_order``）可被建议撤销；
  ``source_order`` 系统时序边与事件时间区间**不可撤销**——它们是观测事实，
  不是可裁量的声明。
- 每个候选方案都用**原求解器**（``feasibility._Solver``，仅内存中排除
  被撤销的约束）复核，可行才把该组合列为方案，并附撤销后的全量可行见证
  （每个事件的最早可行时刻）。
- 全程只读：不写数据库、不改写事件或假设；页面未保存的时钟假设通过
  overrides 参与每一次复核。

搜索（沿矛盾路径的冲突驱动命中集）
----------------------------------
关键观察：无解见证的矛盾路径本身是一个不可行子系统——路径上每一跳的所有
现存约束加上两端区间界限互相冲突。任何能恢复可行性的撤销集合，必须**命中**
该路径（至少把某一跳的全部约束撤完）；撤销后若仍无解，新矛盾路径同样必须
被命中。于是：

1. 候选池 = 当前矛盾路径各跳上的全部可撤销约束；
2. 按撤销条数 k = 1, 2, …, max_depth 逐层枚举候选池的组合，逐组合用原
   求解器复核；
3. 每层复核仍无解时，把新矛盾路径上的可撤销约束并入候选池再进入下一层。
   由命中集归纳可知：若不存在 < k 条的方案，则任何 k 条的最少方案必然
   整体落在扩展后的候选池内——逐层搜索在不触限时是**完备**的，首个产出
   方案的 k 即最少撤销条数，该层全部方案同样最少。

限制（防组合爆炸，保证交互响应）
--------------------------------
- 最多撤销 ``MAX_DEPTH`` 条；最多求解 ``MAX_SOLVES`` 次（含初始检查与
  全撤检查）；最多展示 ``MAX_SOLUTIONS`` 个同样最少的方案。
- 求解次数触限时 ``complete=False``：结果明确标为**未完成**，此时已找到
  的方案只能作为候选，不能宣称已穷尽（更小规模的组合在触限前已完整排除，
  但同规模方案可能未列全）。
- 若撤销**全部**可撤销约束后仍无解（矛盾只由 source_order 边与事件区间
  构成），明确报告：没有仅靠撤销约束的修复方案。
"""
from __future__ import annotations

from itertools import combinations
from typing import Any

from . import engine
from .feasibility import _Solver

MAX_DEPTH = 3       # 单个方案最多撤销的约束条数
MAX_SOLVES = 2000   # 求解次数上限（含初始联立检查与全撤检查）
MAX_SOLUTIONS = 5   # 最多展示的同样最少方案数

# 系统时序边（同来源本地时钟严格有序）是观测事实，不可建议撤销；
# 事件时间区间同样不可撤销（区间不是约束，本就不在撤销候选内）。
PROTECTED_KINDS = frozenset({"source_order"})


class _Search:
    """一次撤销建议搜索的全部状态（预算、候选池、方案）。"""

    def __init__(self, overrides: dict | None, max_depth: int,
                 max_solves: int, max_solutions: int):
        self.overrides = overrides
        self.max_depth = max(1, int(max_depth))
        self.max_solves = max(1, int(max_solves))
        self.max_solutions = max(1, int(max_solutions))
        self.solves_used = 0
        # 模型只读一次；每次复核都在内存中排除被撤销的约束后重解
        self.model = engine.load_model()

    # ------------------------------------------------------------ 求解

    def solve(self, drop: set[int] | None = None) -> tuple[_Solver, dict]:
        """用原求解器复核一个候选（drop 为拟撤销的约束 id 集合）。"""
        self.solves_used += 1
        solver = _Solver(self.overrides, drop_ids=drop, model=self.model)
        return solver, solver.result()

    @property
    def budget_left(self) -> bool:
        return self.solves_used < self.max_solves

    # ------------------------------------------------------------ 候选池

    @staticmethod
    def path_retractable_ids(solver: _Solver, witness: dict) -> list[int]:
        """矛盾路径各跳上的全部可撤销约束 id（按路径顺序去重）。

        同一对端点间可能并存多条约束（如 source_order 与声明约束）：
        要打断这一跳必须撤掉其上的全部可撤销约束，因此候选池按跳收集
        所有可撤销边，而不是只收集见证步骤里 id 最小的那条。
        """
        by_pair: dict[tuple[int, int], list[int]] = {}
        for e in solver.edges:
            if e["kind"] in PROTECTED_KINDS:
                continue
            by_pair.setdefault((e["u"], e["v"]), []).append(e["id"])
        out: list[int] = []
        seen: set[int] = set()
        for step in witness.get("steps", []):
            u = solver.id_idx.get(step["from"])
            v = solver.id_idx.get(step["to"])
            if u is None or v is None:
                continue
            for eid in by_pair.get((u, v), []):
                if eid not in seen:
                    seen.add(eid)
                    out.append(eid)
        return out

    # ------------------------------------------------------------ 主流程

    def run(self) -> dict[str, Any]:
        limits = {"max_depth": self.max_depth, "max_solves": self.max_solves,
                  "max_solutions": self.max_solutions}

        base_solver, base = self.solve()
        retractable = [e for e in base_solver.edges
                       if e["kind"] not in PROTECTED_KINDS]
        protected = [e for e in base_solver.edges
                     if e["kind"] in PROTECTED_KINDS]
        edge_info = {e["id"]: e for e in base_solver.edges}

        def stats() -> dict[str, int]:
            return {
                "event_count": base["stats"]["event_count"],
                "constraint_count": base["stats"]["constraint_count"],
                "retractable_constraints": len(retractable),
                "protected_constraints": len(protected),
                "solves_used": self.solves_used,
            }

        def envelope(status: str, complete: bool, summary: str,
                     **extra: Any) -> dict[str, Any]:
            return {
                "suggestion_type": "constraint-retraction",
                "status": status,
                "complete": complete,
                "summary": summary,
                "limits": limits,
                "stats": stats(),
                "min_retractions": None,
                "solutions_truncated": False,
                "solutions": [],
                "remaining_witness": None,
                "notice": None,
                **extra,
            }

        # 0) 当前假设下已可行：无需撤销（页面通常不会触发，防御性返回）
        if base["feasible"]:
            return envelope(
                "already_feasible", True,
                "当前时钟假设下全局时序已可行，无需撤销任何约束。")

        # 1) 撤销全部可撤销约束仍无解 → 矛盾只由 source_order 边与事件
        #    区间构成，没有仅靠撤销约束的修复方案
        if retractable:
            _, stripped = self.solve({e["id"] for e in retractable})
        else:
            stripped = base  # 没有可撤销约束：全撤等价于原系统
        if not stripped["feasible"]:
            if retractable:
                note = ("没有仅靠撤销约束的修复方案：即使撤销全部 "
                        f"{len(retractable)} 条可撤销约束（人工/导入），仅剩 "
                        "source_order 时序边与事件时间区间的系统仍然无解。")
            else:
                note = ("没有仅靠撤销约束的修复方案：当前没有任何可撤销的"
                        "人工/导入约束，矛盾只由 source_order 时序边与事件"
                        "时间区间构成。")
            note += ("请调整相关来源的时钟偏差 / 不确定度假设，或核对事件归属；"
                     "以下为全部撤销后依然存在的矛盾路径。")
            return envelope("no_retraction_fix", True, note,
                            remaining_witness=stripped["witness"])

        # 2) 沿矛盾路径的冲突驱动搜索：k = 1..max_depth 逐层枚举复核
        pool = self.path_retractable_ids(base_solver, base["witness"])
        solutions: list[tuple[tuple[int, ...], dict]] = []
        complete = True
        truncated = False
        depth_found: int | None = None

        for k in range(1, self.max_depth + 1):
            expansion: list[int] = []
            for combo in combinations(pool, k):
                if not self.budget_left:
                    complete = False
                    break
                solver, res = self.solve(set(combo))
                if res["feasible"]:
                    solutions.append((combo, res))
                    if len(solutions) >= self.max_solutions:
                        truncated = True  # 已达展示上限，同规模方案可能还有
                        break
                else:
                    # 仍无解：新矛盾路径上的可撤销约束也必须被命中，并入候选池
                    for eid in self.path_retractable_ids(solver, res["witness"]):
                        if eid not in pool and eid not in expansion:
                            expansion.append(eid)
            if solutions or not complete:
                depth_found = k if solutions else None
                break
            pool.extend(expansion)

        # 3) 汇总
        if not solutions:
            if complete:
                summary = (f"在最多撤销 {self.max_depth} 条的限制内没有找到可行的"
                           f"撤销方案（共复核 {self.solves_used} 次）：需要同时撤销"
                           "更多约束才能恢复可行性，或调整时钟假设。")
            else:
                summary = (f"搜索未完成：求解次数已达上限 {self.max_solves}，"
                           f"不能排除撤销 {self.max_depth} 条以内的修复方案；"
                           "以下不给出任何结论性判断。请缩小数据规模或调整时钟假设后重试。")
            return envelope("none_within_limits", complete, summary)

        depth_found = depth_found if depth_found is not None else len(solutions[0][0])
        sol_out = []
        for combo, res in solutions:
            sol_out.append({
                "constraints": [{
                    "id": eid,
                    "from": base_solver.ids[edge_info[eid]["u"]],
                    "to": base_solver.ids[edge_info[eid]["v"]],
                    "kind": edge_info[eid]["kind"],
                    "note": edge_info[eid]["note"],
                } for eid in combo],
                # 撤销后全量可行见证：每个事件的最早可行时刻（原求解器复核结果）
                "witness": {
                    "summary": res["summary"],
                    "stats": res["stats"],
                    "events": res["events"],
                },
            })

        n_found = len(sol_out)
        if not complete:
            summary = (f"搜索未完成（求解次数达上限 {self.max_solves}）：以下为撤销 "
                       f"{depth_found} 条的候选方案 {n_found} 个；更小规模的组合已完整"
                       "排除，但同规模方案可能未穷尽，不能视为最优结论。")
        elif truncated:
            summary = (f"最少撤销 {depth_found} 条约束即可恢复全局可行；已按上限展示 "
                       f"{n_found} 个同样最少的方案，可能还有更多同规模方案未列出。")
        else:
            summary = (f"最少撤销 {depth_found} 条约束即可恢复全局可行，"
                       f"共找到 {n_found} 个同样最少的方案；"
                       "每个方案均已用原求解器复核，附撤销后的全量可行见证。")

        return envelope(
            "ok", complete, summary,
            min_retractions=depth_found,
            solutions_truncated=truncated,
            solutions=sol_out,
            notice=("方案仅为预览：不写入数据库；确认后请在「顺序约束」中手动删除对应约束。"
                    "source_order 时序边与事件区间不可撤销，不参与候选。"),
        )


def suggest_retractions(overrides: dict | None = None,
                        max_depth: int = MAX_DEPTH,
                        max_solves: int = MAX_SOLVES,
                        max_solutions: int = MAX_SOLUTIONS) -> dict[str, Any]:
    """以当前生效假设（库存 + 页面未保存 overrides）搜索约束撤销建议。

    只读预览：不写数据库、不改写事件或假设；每个候选方案都用原求解器复核。
    """
    return _Search(overrides, max_depth, max_solves, max_solutions).run()
