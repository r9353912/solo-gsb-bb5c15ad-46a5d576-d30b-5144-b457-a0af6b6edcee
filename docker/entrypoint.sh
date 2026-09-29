#!/bin/sh
# 启动钩子：完成数据库初始化（建表 / 内置示例），随后拉起 API。
set -e

echo "[entrypoint] 初始化数据库 ..."
python - <<'PY'
from app.db import init_db
init_db()
print("[entrypoint] 数据库就绪")
PY

echo "[entrypoint] 启动 uvicorn ..."
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
