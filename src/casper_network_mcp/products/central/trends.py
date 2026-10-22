"""Hour-of-day profiles from Central trend replies, for picking a quiet maintenance window.

Central's trend endpoints share one shape: ``keys`` naming the series and ``samples`` of
``{"data": [...], "ts" | "timestamp": ...}``, sometimes under ``graph``. The timestamp is an
RFC 3339 string or epoch seconds/milliseconds. A day or less comes back in 5-minute samples; a
longer window drops to 3-hour samples, so a week's hourly profile needs one call per day.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

__all__ = ["hourly_profile", "parse_samples", "quietest_window"]

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _when(value: Any) -> datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e12 else value
        return datetime.fromtimestamp(seconds, tz=UTC)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def parse_samples(reply: Any) -> tuple[list[str], list[tuple[datetime, float]]]:
    """The series names and ``(time, total of the sample's numbers)`` for each sample in a trend reply."""
    body = reply.get("graph") if isinstance(reply, dict) and isinstance(reply.get("graph"), dict) else reply
    if not isinstance(body, dict):
        return [], []
    # ``keys`` names the series; only a list counts.
    raw_keys = body.get("keys")
    keys = [str(k) for k in raw_keys if isinstance(k, (str, int))] if isinstance(raw_keys, list) else []
    out: list[tuple[datetime, float]] = []
    for sample in body.get("samples") or []:
        if not isinstance(sample, dict):
            continue
        when = _when(sample.get("ts", sample.get("timestamp")))
        numbers = [v for v in sample.get("data") or [] if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if when is not None and numbers:
            out.append((when, float(sum(numbers))))
    return keys, out


def hourly_profile(samples: list[tuple[datetime, float]], zone: ZoneInfo) -> dict[str, Any]:
    """Average, low and high per local hour, and the average per weekday."""
    by_hour: dict[int, list[float]] = defaultdict(list)
    by_day: dict[int, list[float]] = defaultdict(list)
    for when, value in samples:
        local = when.astimezone(zone)
        by_hour[local.hour].append(value)
        by_day[local.weekday()].append(value)
    hours = [
        {
            "hour": f"{h:02d}:00",
            "avg": round(sum(v) / len(v), 2),
            "min": round(min(v), 2),
            "max": round(max(v), 2),
            "samples": len(v),
        }
        for h in range(24)
        if (v := by_hour.get(h))
    ]
    weekdays = {_WEEKDAYS[d]: round(sum(v) / len(v), 2) for d in range(7) if (v := by_day.get(d))}
    return {"hours": hours, "weekday_avg": weekdays}


def quietest_window(hours: list[dict[str, Any]], length: int) -> dict[str, Any] | None:
    """The run of ``length`` consecutive local hours (across midnight too) with the lowest average."""
    avg = {int(h["hour"][:2]): h["avg"] for h in hours}
    if len(avg) < 24 or not 1 <= length <= 24:
        return None
    best_start, best = 0, float("inf")
    for start in range(24):
        mean = sum(avg[(start + i) % 24] for i in range(length)) / length
        if mean < best:
            best_start, best = start, mean
    peak = max(avg, key=lambda h: avg[h])
    return {
        "start": f"{best_start:02d}:00",
        "end": f"{(best_start + length) % 24:02d}:00",
        "avg": round(best, 2),
        "busiest_hour": f"{peak:02d}:00",
        "busiest_avg": avg[peak],
    }
