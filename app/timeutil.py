"""时间戳解析与格式化工具。"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def to_unix_seconds(value) -> float | None:
    """把常见时间表示解析为 Unix 秒（UTC），无法识别时返回 None。

    支持：数值（秒/毫秒/微秒/纳秒）、ISO-8601、RFC 2822、"YYYY/MM/DD HH:MM:SS"。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(numeric := _as_number(value), float):
        return numeric

    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    dt: datetime | None = None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = parsedate_to_datetime(text)  # RFC 2822
        except (TypeError, ValueError):
            dt = None
    if dt is None:
        m = re.match(
            r"^(\d{4})[/-](\d{2})[/-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?$",
            text,
        )
        if m:
            y, mo, d, h, mi, s = map(int, m.groups())
            try:
                dt = datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)
            except ValueError:
                return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - _EPOCH).total_seconds()


def _as_number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _scale_numeric(float(value))
    if isinstance(value, str):
        s = value.strip()
        if re.fullmatch(r"-?\d+(?:\.\d+)?", s):
            return _scale_numeric(float(s))
    return False


def _scale_numeric(n: float) -> float:
    """启发式判断秒/毫秒/微秒/纳秒时间戳。"""
    a = abs(n)
    if a >= 1e17:      # 纳秒
        return n / 1e9
    if a >= 1e14:      # 微秒
        return n / 1e6
    if a >= 1e11:      # 毫秒
        return n / 1e3
    return n            # 秒（含小秒）或极小的相对时间


def fmt(ts: float | None) -> str:
    """统一展示：UTC ISO，毫秒精度。"""
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def fmt_delta(seconds: float) -> str:
    """人性化的时间差。"""
    sign = "-" if seconds < 0 else ""
    s = abs(seconds)
    if s < 1:
        return f"{sign}{s * 1000:.0f} ms"
    if s < 60:
        return f"{sign}{s:.2f} s"
    if s < 3600:
        return f"{sign}{s / 60:.1f} min"
    if s < 86400:
        return f"{sign}{s / 3600:.1f} h"
    return f"{sign}{s / 86400:.1f} d"
