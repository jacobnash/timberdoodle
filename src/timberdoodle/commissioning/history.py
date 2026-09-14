"""
The one seam between the commissioning logic and where point history
actually lives. The ladder and reconciliation only ever need two reads -
"latest sample per point" and "samples over a window" - so that's the
whole interface. `PostgresHistory` is the real one (timeseries.py's
point_history table); `InMemoryHistory` exists so every rung of the
ladder is unit-testable with hand-built series and no database.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from timberdoodle import timeseries

Sample = tuple[datetime, object]


class History(Protocol):
    def latest(self, point_uris: list[str]) -> dict[str, Sample]: ...

    def window(self, point_uris: list[str], start: datetime, end: datetime) -> dict[str, list[Sample]]: ...


class PostgresHistory:
    def __init__(self, conn, per_point_limit: int = 5000):
        self.conn = conn
        self.per_point_limit = per_point_limit

    def latest(self, point_uris: list[str]) -> dict[str, Sample]:
        return timeseries.read_latest_many(self.conn, list(point_uris))

    def window(self, point_uris: list[str], start: datetime, end: datetime) -> dict[str, list[Sample]]:
        raw = timeseries.read_range_many(self.conn, list(point_uris), start, end, self.per_point_limit)
        return {uri: rows for uri, (rows, _truncated) in raw.items()}


class InMemoryHistory:
    def __init__(self, series: dict[str, list[Sample]] | None = None):
        self.series: dict[str, list[Sample]] = {k: sorted(v) for k, v in (series or {}).items()}

    def add(self, point_uri: str, ts: datetime, value) -> None:
        self.series.setdefault(point_uri, []).append((ts, value))
        self.series[point_uri].sort()

    def latest(self, point_uris: list[str]) -> dict[str, Sample]:
        return {u: self.series[u][-1] for u in point_uris if self.series.get(u)}

    def window(self, point_uris: list[str], start: datetime, end: datetime) -> dict[str, list[Sample]]:
        return {u: [(t, v) for t, v in self.series.get(u, []) if start <= t < end] for u in point_uris}
