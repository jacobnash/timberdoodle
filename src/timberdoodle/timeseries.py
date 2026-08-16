"""
Point history in Postgres/Timescale - the thing Grafana (or Tableau, or
anything else that speaks SQL) reads natively, no plugin, no custom
connector. Three nullable value columns because sources emit int/float/str/
bool values today (e.g. FBF's fan_status.presentValue = "active") - one
float column would lose that.
"""

import os
from datetime import datetime

import psycopg
from psycopg_pool import ConnectionPool

DEFAULT_DSN = os.environ.get(
    "TIMBERDOODLE_PG_DSN", "postgresql://timberdoodle:timberdoodle@localhost:5433/timberdoodle"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS point_history (
    point_uri  TEXT NOT NULL,
    ts         TIMESTAMPTZ NOT NULL,
    value      DOUBLE PRECISION,
    value_text TEXT,
    value_bool BOOLEAN,
    unit       TEXT,
    label      TEXT,
    PRIMARY KEY (point_uri, ts)
);
"""


def _setup(conn: psycopg.Connection) -> None:
    """Runs on every fresh connection, pooled or not - idempotent, same as
    CREATE TABLE IF NOT EXISTS."""
    conn.execute(SCHEMA)
    # We run the TimescaleDB image specifically for this - a plain
    # CREATE TABLE gets none of its time-based chunking (faster inserts
    # and time-range reads at volume as the table grows past what one
    # unpartitioned btree handles well). migrate_data converts any rows
    # that already exist under the plain-table schema; idempotent like
    # the CREATE TABLE above.
    conn.execute("SELECT create_hypertable('point_history', 'ts', if_not_exists => TRUE, migrate_data => TRUE)")
    # Real tradeoff, taken deliberately: every INSERT otherwise waits on a
    # full WAL fsync before returning. Scoped to this connection only (a
    # session-level SET, not a postgresql.conf edit) - other clients
    # (Grafana reads, anything else) are unaffected. The risk this accepts:
    # a write could be lost on an OS/DB crash within the tiny WAL-flush
    # window between "client got 204" and "WAL synced to disk" - not a
    # corruption risk, a durability-window risk, and one this point-history
    # table (telemetry, not financial records) can afford.
    conn.execute("SET synchronous_commit = off")


def connect(dsn: str = DEFAULT_DSN) -> psycopg.Connection:
    conn = psycopg.connect(dsn, autocommit=True)
    _setup(conn)
    return conn


def connect_pool(dsn: str = DEFAULT_DSN, min_size: int = 2, max_size: int = 10) -> ConnectionPool:
    """One connection per request instead of one shared connection for
    every concurrent request - psycopg.Connection isn't safe for
    concurrent use from multiple threads, and a single shared connection
    is exactly what made ingest_api.py fragile (it died once already this
    session, silently, from a stale connection with no way to recover
    without a restart)."""
    pool = ConnectionPool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        kwargs={"autocommit": True},
        configure=_setup,
        open=True,
    )
    pool.wait()
    return pool


def write_point_value(conn: psycopg.Connection, point_uri: str, value, ts: datetime, unit: str | None = None, label: str | None = None) -> None:
    """Dispatches on type(value) into the right nullable column. bool must
    be checked before int/float - bool is a subclass of int in Python, the
    same trap normalize_value() guards against in FBF's bridges."""
    num, text, flag = None, None, None
    if isinstance(value, bool):
        flag = value
    elif isinstance(value, (int, float)):
        num = float(value)
    elif value is None:
        pass
    else:
        text = str(value)

    conn.execute(
        """
        INSERT INTO point_history (point_uri, ts, value, value_text, value_bool, unit, label)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (point_uri, ts) DO UPDATE SET
            value = EXCLUDED.value,
            value_text = EXCLUDED.value_text,
            value_bool = EXCLUDED.value_bool,
            unit = EXCLUDED.unit,
            label = EXCLUDED.label
        """,
        (point_uri, ts, num, text, flag, unit, label),
    )


def _row_to_value(row):
    value, value_text, value_bool = row
    if value_bool is not None:
        return value_bool
    if value_text is not None:
        return value_text
    return value


def read_latest(conn: psycopg.Connection, point_uri: str):
    cur = conn.execute(
        "SELECT value, value_text, value_bool FROM point_history WHERE point_uri = %s ORDER BY ts DESC LIMIT 1",
        (point_uri,),
    )
    row = cur.fetchone()
    return None if row is None else _row_to_value(row)


def read_range(conn: psycopg.Connection, point_uri: str, start_ts: datetime, end_ts: datetime):
    cur = conn.execute(
        """
        SELECT ts, value, value_text, value_bool FROM point_history
        WHERE point_uri = %s AND ts >= %s AND ts <= %s
        ORDER BY ts ASC
        """,
        (point_uri, start_ts, end_ts),
    )
    return [(ts, _row_to_value((value, value_text, value_bool))) for ts, value, value_text, value_bool in cur.fetchall()]


def delete_point_value(conn: psycopg.Connection, point_uri: str, ts: datetime) -> bool:
    """The Haxall IHisExt side of `val == None.val`: a specific timestamp
    is removed outright, not overwritten with a null value. Returns
    whether a row actually existed to delete."""
    cur = conn.execute(
        "DELETE FROM point_history WHERE point_uri = %s AND ts = %s",
        (point_uri, ts),
    )
    return cur.rowcount > 0
