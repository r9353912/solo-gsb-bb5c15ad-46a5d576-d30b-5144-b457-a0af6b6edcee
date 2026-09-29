"""数据库初始化与访问层（SQLite）。

设计要点：
- 每条事件只存观测本地时间戳；真实时间区间在推理阶段根据来源时钟假设动态生成，
  因此审计员调整偏差 / 漂移 / 不确定度后可以即时重算，不需要迁移历史数据。
- 约束（before/after）以统一的有向边表示：from -> to 表示“from 必须先于 to”。
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
DB_PATH = Path(os.environ.get("DB_PATH", DATA_DIR / "audit.db"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_connection() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    clock_offset REAL NOT NULL DEFAULT 0,   -- 时钟偏差假设（秒），t_real = t_local - offset
    drift_ppm    REAL NOT NULL DEFAULT 0,   -- 漂移（ppm），相对参考纪元
    uncertainty  REAL NOT NULL DEFAULT 1,   -- 时钟不确定半径（秒）
    reliability  REAL NOT NULL DEFAULT 0.8, -- 来源可靠度 0~1
    kind         TEXT NOT NULL DEFAULT 'external',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id          TEXT PRIMARY KEY,
    source_id   TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    seq         INTEGER,                       -- 同来源内的观测序号（可空）
    local_ts    REAL NOT NULL,                 -- 观测本地时钟，Unix 秒
    ts_text     TEXT,                          -- 原始时间字符串，便于展示
    event_type  TEXT NOT NULL DEFAULT 'event',
    actor       TEXT,
    summary     TEXT NOT NULL DEFAULT '',
    raw         TEXT NOT NULL DEFAULT '{}',    -- 原始记录 JSON
    imported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_source ON events(source_id, local_ts);

CREATE TABLE IF NOT EXISTS constraints (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    from_event TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    to_event   TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL DEFAULT 'causal', -- causal | claimed | temporal
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(from_event, to_event, kind)
);
CREATE INDEX IF NOT EXISTS idx_constraints_from ON constraints(from_event);
CREATE INDEX IF NOT EXISTS idx_constraints_to ON constraints(to_event);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scenarios (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,       -- 命名假设方案
    description  TEXT NOT NULL DEFAULT '',
    assumptions  TEXT NOT NULL DEFAULT '{}', -- {source_id: {clock_offset,...}}
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- 一次 JSON 导入形成一个批次；撤回只允许针对 active 批次。
CREATE TABLE IF NOT EXISTS import_batches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    case_ref      TEXT NOT NULL DEFAULT '',  -- 导入文档中的 case（可空）
    filename      TEXT NOT NULL DEFAULT '',  -- 上传文件名（/api/import/raw 为空）
    status        TEXT NOT NULL DEFAULT 'active', -- active | retracted
    created_at    TEXT NOT NULL,
    retracted_at  TEXT,
    sources_added INTEGER NOT NULL DEFAULT 0,
    events_added  INTEGER NOT NULL DEFAULT 0,
    constraints_added INTEGER NOT NULL DEFAULT 0,
    rejected_count INTEGER NOT NULL DEFAULT 0,
    warning_count  INTEGER NOT NULL DEFAULT 0,
    note          TEXT NOT NULL DEFAULT ''
);

-- 批次账本：逐条登记该批次“实际写入（written）”与“因重复/拒绝而未写入（skipped）”
-- 的来源 / 事件 / 约束。撤回时只动 written 项，并用行上的 import_batch_id
-- 交叉验证归属；skipped 的重复 ID 永不被本批次撤回误删。
CREATE TABLE IF NOT EXISTS batch_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id   INTEGER NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
    item_type  TEXT NOT NULL,                -- source | event | constraint
    ref_id     TEXT NOT NULL,                -- 来源/事件为业务 ID，约束为自增 id（字符串保存）
    status     TEXT NOT NULL,                -- written | skipped
    reason     TEXT NOT NULL DEFAULT '',     -- skipped 原因（duplicate / rejected / dangling_endpoint ...）
    detail     TEXT NOT NULL DEFAULT '{}',   -- 快照 JSON：端点/类型等，便于页面展示
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_batch_items_batch ON batch_items(batch_id, item_type, status);
"""

# 旧库升级：给三张实体表补归属戳。NULL 表示“批次功能上线前的存量数据 / 页面手工数据”，
# 撤回时无法安全证明归属，按“无法安全判断归属”拒绝而不是删除。
_MIGRATIONS = (
    "ALTER TABLE sources ADD COLUMN import_batch_id INTEGER",
    "ALTER TABLE events ADD COLUMN import_batch_id INTEGER",
    "ALTER TABLE constraints ADD COLUMN import_batch_id INTEGER",
)


def _migrate(conn: sqlite3.Connection) -> None:
    have = {r["name"] for r in conn.execute(
        "SELECT name FROM pragma_table_info('sources')")}
    # 任意一张新列缺失即按顺序补齐（ALTER 只支持逐列添加）。
    if "import_batch_id" not in have:
        for stmt in _MIGRATIONS:
            conn.execute(stmt)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_batch ON events(import_batch_id)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_constraints_batch ON constraints(import_batch_id)")


def init_db(load_sample: bool | None = None) -> None:
    """建表；空库时可选载入示例数据（默认 data 目录为内置目录时载入）。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('initialized_at', ?) "
            "ON CONFLICT(key) DO NOTHING",
            (_now(),),
        )
    if load_sample is None:
        load_sample = os.environ.get("LOAD_SAMPLE", "1") == "1"
    if load_sample:
        with db() as conn:
            empty = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 0
        if empty:
            from .sample import SAMPLE_PAYLOAD
            from .importer import import_payload

            summary = import_payload(SAMPLE_PAYLOAD)
            print(f"[init] 已载入示例数据: {summary}")


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    if "raw" in out and isinstance(out["raw"], str):
        try:
            out["raw"] = json.loads(out["raw"])
        except (ValueError, TypeError):
            pass
    return out
