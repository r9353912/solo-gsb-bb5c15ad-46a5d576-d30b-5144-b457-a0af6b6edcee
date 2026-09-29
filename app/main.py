"""FastAPI 入口：导入、假设调整、重算、筛选、导出。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import (
    anchor_calibration,
    elapsed_time,
    engine,
    feasibility,
    reconciliation,
    retraction,
    scenarios,
    sensitivity,
)
from .db import db, init_db
from .importer import import_payload, reset_all

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(
    title="事件证据链审计台",
    description="导入多来源 JSON 事件记录，校正时钟偏差，重建可解释时间线并定位冲突链。",
    version="1.7.0",
)


@app.on_event("startup")
def _startup() -> None:
    init_db()


# ----------------------------------------------------------------- 模型

class SourceAssumption(BaseModel):
    clock_offset: float | None = None
    drift_ppm: float | None = None
    uncertainty: float | None = Field(default=None, ge=0)
    reliability: float | None = Field(default=None, ge=0, le=1)


class RecomputeBody(BaseModel):
    overrides: dict[str, SourceAssumption] = Field(default_factory=dict)
    source_id: str | None = None
    event_type: str | None = None
    q: str | None = None
    only_conflicts: bool = False
    only_ambiguous: bool = False
    lo: float | None = None
    hi: float | None = None


class SaveAssumptionsBody(BaseModel):
    assumptions: dict[str, SourceAssumption]


class ConstraintBody(BaseModel):
    from_event: str
    to_event: str
    kind: str = "claimed"
    note: str = ""


class ScenarioBody(BaseModel):
    name: str
    description: str = ""
    assumptions: dict[str, dict[str, float | None]] = Field(default_factory=dict)
    from_current: bool = False


class ScenarioPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    assumptions: dict[str, dict[str, float | None]] | None = None
    from_current: bool = False


class ScenarioSide(BaseModel):
    kind: str = "scenario"                 # scenario | current
    scenario_id: int | None = None
    overrides: dict[str, dict[str, float | None]] | None = None


class CompareBody(BaseModel):
    a: ScenarioSide
    b: ScenarioSide


class SensitivityBody(BaseModel):
    source_id: str
    lo: Any = None                        # 扫描下界（秒），在 scan 内严格校验
    hi: Any = None                        # 扫描上界（秒）
    overrides: dict[str, SourceAssumption] = Field(default_factory=dict)


class FeasibilityBody(BaseModel):
    """全局时序可行性：只接收假设 overrides；不接受任何筛选参数（始终全量联立）。"""
    overrides: dict[str, SourceAssumption] = Field(default_factory=dict)


class ReconcileBody(BaseModel):
    """双来源事件对账：两个不同来源 + 非负容差（毫秒），可携带未保存假设。"""
    source_a: str
    source_b: str
    tolerance_ms: Any                    # 在 reconcile 内严格校验（非负/有限/数值）
    overrides: dict[str, SourceAssumption] = Field(default_factory=dict)


class AnchorItem(BaseModel):
    a: str
    b: str


class AnchorCalibrationBody(BaseModel):
    """锚点校准：1～20 对已配对事件 + 待校准方（a|b），可携带未保存假设。"""
    source_a: str
    source_b: str
    calibrated_side: str                 # "a" | "b"，在 calibrate 内严格校验
    anchors: list[AnchorItem]
    overrides: dict[str, SourceAssumption] = Field(default_factory=dict)


class ElapsedBoundsBody(BaseModel):
    """两事件耗时审计：起 / 终点事件 + 非负有限阈值（秒），可携带未保存假设。"""
    start_event: str
    end_event: str
    threshold_seconds: Any               # 在 audit_elapsed 内严格校验（非负/有限/数值）
    overrides: dict[str, SourceAssumption] = Field(default_factory=dict)


def _scenario_exc(exc: Exception) -> HTTPException:
    code = 409 if isinstance(exc, scenarios.ScenarioConflict) else 400
    notfound = ("不存在" in str(exc))
    return HTTPException(404 if notfound else code, str(exc))


# ----------------------------------------------------------------- 路由

@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/sample")
def download_sample() -> JSONResponse:
    from .sample import SAMPLE_PAYLOAD
    return JSONResponse(
        SAMPLE_PAYLOAD,
        headers={"Content-Disposition": 'attachment; filename="login-anomaly.json"'},
    )


@app.post("/api/import")
async def import_events(file: UploadFile = File(...)) -> dict[str, Any]:
    raw = await file.read()
    if len(raw) > 8 * 1024 * 1024:
        raise HTTPException(413, "文件过大（上限 8MB）")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, f"JSON 解析失败: {exc}") from exc
    try:
        summary = import_payload(payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"imported": True, "summary": summary}


@app.post("/api/import/raw")
def import_raw(payload: Any = Body(...)) -> dict[str, Any]:
    """便于 curl/测试直接提交 JSON 体。"""
    try:
        return {"imported": True, "summary": import_payload(payload)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/recompute")
def recompute(body: RecomputeBody) -> dict[str, Any]:
    overrides = {sid: v.model_dump(exclude_none=True) for sid, v in body.overrides.items()}
    result = engine.compute(overrides)

    # 筛选只影响时间线展示；冲突/歧义始终全量计算，避免漏报。
    conflict_ids: set[str] = set()
    for c in result["conflicts"]:
        conflict_ids.update(c["events"])

    visible = _apply_filters(result["events"], body, conflict_ids)
    result["events"] = visible
    result["visible_count"] = len(visible)

    # 边也按可见事件裁剪
    visible_ids = {e["id"] for e in visible}
    if (body.source_id or body.event_type or body.q or body.lo is not None
            or body.hi is not None):
        result["edges"] = [e for e in result["edges"]
                           if e["from"] in visible_ids and e["to"] in visible_ids]
    result["overrides_applied"] = overrides
    return result


def _apply_filters(events: list[dict], body: RecomputeBody,
                   conflict_ids: set[str] | None = None) -> list[dict]:
    out = events
    if body.source_id:
        out = [e for e in out if e["source_id"] == body.source_id]
    if body.event_type:
        out = [e for e in out if e["event_type"] == body.event_type]
    if body.q:
        needle = body.q.lower()
        out = [e for e in out if needle in (e["summary"] or "").lower()
               or needle in e["id"].lower()
               or needle in (e.get("actor") or "").lower()
               or needle in e["event_type"].lower()]
    if body.lo is not None:
        out = [e for e in out if e["hi"] >= body.lo]
    if body.hi is not None:
        out = [e for e in out if e["lo"] <= body.hi]
    if body.only_conflicts:
        out = [e for e in out if e["id"] in (conflict_ids or set())]
    if body.only_ambiguous:
        out = [e for e in out if e.get("ambiguity_group")]
    return out


@app.post("/api/sources/assumptions")
def save_assumptions(body: SaveAssumptionsBody) -> dict[str, Any]:
    """把审计员确认后的偏差假设持久化。"""
    updated: list[str] = []
    with db() as conn:
        for sid, a in body.assumptions.items():
            row = conn.execute("SELECT id FROM sources WHERE id=?", (sid,)).fetchone()
            if row is None:
                continue
            data = a.model_dump(exclude_none=True)
            sets, vals = [], []
            for k, v in data.items():
                sets.append(f"{k}=?")
                vals.append(float(v))
            if not sets:
                continue
            sets.append("updated_at=CURRENT_TIMESTAMP")
            vals.append(sid)
            conn.execute(f"UPDATE sources SET {', '.join(sets)} WHERE id=?", vals)
            updated.append(sid)
    return {"saved": True, "updated_sources": updated}


@app.get("/api/sources")
def list_sources() -> dict[str, Any]:
    result = engine.compute()
    return {"sources": result["sources"]}


@app.delete("/api/sources/{source_id}")
def delete_source(source_id: str) -> dict[str, Any]:
    with db() as conn:
        cur = conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "来源不存在")
    return {"deleted": source_id}


@app.post("/api/constraints")
def add_constraint(body: ConstraintBody) -> dict[str, Any]:
    if body.from_event == body.to_event:
        raise HTTPException(400, "自环约束无意义")
    with db() as conn:
        for eid in (body.from_event, body.to_event):
            if conn.execute("SELECT 1 FROM events WHERE id=?", (eid,)).fetchone() is None:
                raise HTTPException(404, f"事件不存在: {eid}")
        cur = conn.execute(
            "INSERT OR IGNORE INTO constraints(from_event,to_event,kind,note,created_at) "
            "VALUES(?,?,?,?,CURRENT_TIMESTAMP)",
            (body.from_event, body.to_event, body.kind, body.note),
        )
        if cur.rowcount == 0:
            raise HTTPException(409, "约束已存在")
    return {"created": True}


@app.delete("/api/constraints/{edge_id}")
def delete_constraint(edge_id: int) -> dict[str, Any]:
    with db() as conn:
        cur = conn.execute("DELETE FROM constraints WHERE id=?", (edge_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "约束不存在")
    return {"deleted": edge_id}


@app.get("/api/event-types")
def event_types() -> dict[str, Any]:
    with db() as conn:
        rows = conn.execute(
            "SELECT event_type, COUNT(*) n FROM events GROUP BY event_type ORDER BY n DESC"
        ).fetchall()
    return {"types": [{"name": r["event_type"], "count": r["n"]} for r in rows]}


@app.post("/api/reset")
def reset_data() -> dict[str, Any]:
    reset_all()
    return {"reset": True}


@app.get("/api/export")
def export_audit(clock_offset: str | None = None, drift_ppm: str | None = None,
                 uncertainty: str | None = None) -> JSONResponse:
    """导出完整审计结果 JSON；可经 query 传一次性假设（用于未保存即导出）。"""
    overrides: dict[str, dict[str, float]] = {}
    for name, raw in (("clock_offset", clock_offset), ("drift_ppm", drift_ppm),
                      ("uncertainty", uncertainty)):
        if not raw:
            continue
        for item in raw.split(","):
            if ":" not in item:
                continue
            sid, val = item.split(":", 1)
            try:
                overrides.setdefault(sid.strip(), {})[name] = float(val)
            except ValueError:
                raise HTTPException(400, f"无法解析 {name}: {item}")
    payload = engine.build_export(overrides)
    return JSONResponse(
        payload,
        headers={"Content-Disposition": 'attachment; filename="audit_result.json"'},
    )


# ----------------------------------------------------- 假设方案与方案对比

@app.get("/api/scenarios")
def scenario_list() -> dict[str, Any]:
    """命名假设方案列表（含每个方案相对当前库存假设的摘要）。"""
    return {"scenarios": scenarios.list_scenarios()}


@app.post("/api/scenarios")
def scenario_create(body: ScenarioBody) -> dict[str, Any]:
    """把一组来源时钟假设保存为命名方案。

    from_current=true 时 assumptions 只传页面未保存的 overrides 即可，
    服务端会与当前库存假设合并为完整快照（"保存当前为方案"）。
    """
    try:
        sc = scenarios.create_scenario(
            body.name, body.assumptions, body.description, body.from_current)
    except scenarios.ScenarioError as exc:
        raise _scenario_exc(exc) from exc
    return {"created": True, "scenario": sc}


@app.get("/api/scenarios/current/assumptions")
def scenario_current() -> dict[str, Any]:
    """当前库存的完整生效假设（供'另存为方案'时预览基线）。"""
    return {"assumptions": scenarios.current_effective_assumptions()}


@app.get("/api/scenarios/{scenario_id}")
def scenario_get(scenario_id: int) -> dict[str, Any]:
    try:
        return {"scenario": scenarios.get_scenario(scenario_id)}
    except scenarios.ScenarioError as exc:
        raise _scenario_exc(exc) from exc


@app.patch("/api/scenarios/{scenario_id}")
def scenario_update(scenario_id: int, body: ScenarioPatch) -> dict[str, Any]:
    try:
        sc = scenarios.update_scenario(
            scenario_id, body.name, body.description, body.assumptions,
            body.from_current)
    except scenarios.ScenarioError as exc:
        raise _scenario_exc(exc) from exc
    return {"updated": True, "scenario": sc}


@app.delete("/api/scenarios/{scenario_id}")
def scenario_delete(scenario_id: int) -> dict[str, Any]:
    try:
        scenarios.delete_scenario(scenario_id)
    except scenarios.ScenarioError as exc:
        raise _scenario_exc(exc) from exc
    return {"deleted": scenario_id}


@app.post("/api/compare")
def compare_scenarios(body: CompareBody) -> dict[str, Any]:
    """两套命名方案（或当前假设）在同一批事件上分别重算并输出结构化差异。"""
    def side_dict(s: ScenarioSide) -> dict:
        return {"kind": s.kind, "scenario_id": s.scenario_id,
                "overrides": {sid: {k: v for k, v in ov.items() if v is not None}
                              for sid, ov in (s.overrides or {}).items()}}
    try:
        return scenarios.compare_scenarios(side_dict(body.a), side_dict(body.b))
    except scenarios.ScenarioError as exc:
        raise _scenario_exc(exc) from exc


@app.post("/api/compare/export")
async def compare_export(body: CompareBody) -> JSONResponse:
    """导出方案对比 JSON 证据包（POST，因为侧引用可携带未保存 overrides）。"""
    def side_dict(s: ScenarioSide) -> dict:
        return {"kind": s.kind, "scenario_id": s.scenario_id,
                "overrides": {sid: {k: v for k, v in ov.items() if v is not None}
                              for sid, ov in (s.overrides or {}).items()}}
    try:
        payload = scenarios.compare_scenarios(side_dict(body.a), side_dict(body.b))
    except scenarios.ScenarioError as exc:
        raise _scenario_exc(exc) from exc
    payload["export_type"] = "assumption-scenario-comparison"
    return JSONResponse(
        payload,
        headers={"Content-Disposition":
                 'attachment; filename="scenario_comparison.json"'},
    )


# ----------------------------------------------------- 偏移敏感性扫描

@app.post("/api/offset-sensitivity")
def offset_sensitivity(body: SensitivityBody) -> dict[str, Any]:
    """偏移敏感性扫描：解析临界点 → 切稳定区间 → 逐段重算并与当前结果对比。

    overrides 传页面当前（含未保存）的其它来源假设，扫描只替换目标来源的
    clock_offset；上下界无效 / 来源不存在 / 临界点过多返回 4xx 明确提示。
    """
    overrides = {sid: v.model_dump(exclude_none=True)
                 for sid, v in body.overrides.items()}
    try:
        return sensitivity.scan_offset(body.source_id.strip(), body.lo, body.hi,
                                       overrides)
    except sensitivity.SensitivityError as exc:
        raise HTTPException(404 if "不存在" in str(exc) else 400, str(exc)) from exc


# ----------------------------------------------------- 全局时序可行性

@app.post("/api/temporal-feasibility")
def temporal_feasibility(body: FeasibilityBody) -> dict[str, Any]:
    """全量事件区间 × 全部有效约束联立求解（只读，不改写事件或假设）。

    overrides 传页面当前（含未保存）的时钟假设；不接受筛选参数——
    页面筛选只影响时间线展示，本检查始终对全量数据联立。
    可行时返回每个事件的最早可行时刻（见证）；无解时返回一条带事件 ID、
    约束 ID 与两端冲突界限的矛盾路径。
    """
    overrides = {sid: v.model_dump(exclude_none=True)
                 for sid, v in body.overrides.items()}
    result = feasibility.check_feasibility(overrides)
    result["overrides_applied"] = overrides
    return result


@app.post("/api/retraction-suggestions")
def retraction_suggestions(body: FeasibilityBody) -> dict[str, Any]:
    """约束撤销建议（只读预览，不写数据库）。

    全量联立无解时，沿矛盾路径搜索需撤销的最少人工/导入约束
    （source_order 时序边与事件区间不可撤销），每个候选用原求解器复核，
    返回至多 5 个同样最少的方案及撤销后的全量可行见证。
    搜索限定最多撤销 3 条、最多 2000 次求解；触限时 complete=false
    明确标为未完成。overrides 传页面当前（含未保存）的时钟假设。
    """
    overrides = {sid: v.model_dump(exclude_none=True)
                 for sid, v in body.overrides.items()}
    result = retraction.suggest_retractions(overrides)
    result["overrides_applied"] = overrides
    return result


# ----------------------------------------------------- 双来源事件对账

@app.post("/api/source-reconciliation")
def source_reconciliation(body: ReconcileBody) -> dict[str, Any]:
    """两个不同来源的事件对账（只读，不写数据库、不合并记录、不新增约束）。

    复用现有时钟校正区间（库存假设 + 页面未保存 overrides）；仅 actor 非空
    且相同、event_type 相同、闭区间间距不超过容差（统一四舍五入到整数毫秒）
    的事件可配对。每条事件最多参与一对：先最大化配对数，再最小化配对双方
    区间中点差绝对值之和（全局最优，非逐条贪心），同优结果按事件 ID 稳定
    输出。每侧最多 200 条事件，超限 / 同来源 / 负容差返回 4xx 明确拒绝。
    """
    overrides = {sid: v.model_dump(exclude_none=True)
                 for sid, v in body.overrides.items()}
    try:
        return reconciliation.reconcile_sources(
            body.source_a, body.source_b, body.tolerance_ms, overrides)
    except reconciliation.ReconcileError as exc:
        raise HTTPException(404 if "不存在" in str(exc) else 400, str(exc)) from exc


# ----------------------------------------------------- 锚点校准（对账结果上）

@app.post("/api/anchor-calibration")
def anchor_calibrate(body: AnchorCalibrationBody) -> dict[str, Any]:
    """对账锚点校准（只读，不写数据库、不改写事件或假设）。

    审计员勾选 1～20 对已配对且确认同一动作的事件，指定一侧为待校准方、
    另一侧为参照；复用当前含未保存调整的事件时间区间（漂移/不确定度固定），
    逐对求使两侧闭区间相交的待校准方 offset 增量范围，再取所有范围的交集。
    交集为空时返回分别决定冲突上下界的两对锚点及范围；非空时返回允许的
    绝对 offset 范围与中点（前端"采用中点"只更新左栏未保存假设并重算时间线）。
    锚点重复、事件失效（已删除）或来源不一致均以 4xx 明确拒绝。
    """
    overrides = {sid: v.model_dump(exclude_none=True)
                 for sid, v in body.overrides.items()}
    anchors = [{"a": it.a, "b": it.b} for it in body.anchors]
    try:
        return anchor_calibration.calibrate_anchors(
            body.source_a, body.source_b, body.calibrated_side,
            anchors, overrides)
    except reconciliation.ReconcileError as exc:
        raise HTTPException(404 if "不存在" in str(exc) else 400, str(exc)) from exc


# ----------------------------------------------------- 两事件耗时精确界审计

@app.post("/api/elapsed-time-bounds")
def elapsed_time_bounds(body: ElapsedBoundsBody) -> dict[str, Any]:
    """两事件耗时是否被证据真正约束（只读，不写库、不保存假设）。

    复用现有时间闭区间与全部有效先后约束（含 source_order，允许同刻），
    在所有可行时间分配上求 t_end − t_start 的精确最小 / 最大值（可为负），
    分别返回达到端点的全量事件时刻见证（非两区间直接相减）。
    最小 > 阈值判“必超”，最大 ≤ 阈值判“必不超”，其余为“可能超”。
    全局无解时返回现有矛盾见证、不输出耗时结论；起点/终点未知或相同、
    阈值非有限非负均以 4xx 明确拒绝。不受页面筛选影响；overrides 传页面
    当前（含未保存）的时钟假设。
    """
    overrides = {sid: v.model_dump(exclude_none=True)
                 for sid, v in body.overrides.items()}
    try:
        return elapsed_time.audit_elapsed(
            body.start_event, body.end_event, body.threshold_seconds, overrides)
    except elapsed_time.ElapsedError as exc:
        raise HTTPException(404 if "不存在" in str(exc) else 400,
                            str(exc)) from exc


# ----------------------------------------------------------------- 前端

@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
