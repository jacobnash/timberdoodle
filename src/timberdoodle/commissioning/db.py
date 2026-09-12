"""
Postgres persistence for the commissioning agent - the same database and
the same idempotent-DDL `ensure_schema()` style as faults.py/timeseries.py.

The canonical model is stored as JSONB documents, one row per record,
keyed by (project_id, id). The documents are the source of truth; Brick
in Oxigraph is a projection written from them, never read back into them.
JSONB rather than a wide relational schema because the model is still
plain-English-first and evolves with what the field teaches it; the
queries this needs are "everything for this project" and "this one
record", nothing that wants a join.

Two things are NOT stored here on purpose:

  * point history - `point_history` (timeseries.py) is read through
    `history.PostgresHistory`; the agent never keeps its own copy.
  * faults - sustained-absence and ladder faults on well-identified
    equipment go into the existing `faults` table via faults.open_fault/
    close_fault under `commissioning:*` rule ids, so the fault API, the
    webhooks and the console see them alongside every other fault.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import psycopg

TABLES = ("cx_projects", "cx_spec", "cx_devices", "cx_entities", "cx_deviations", "cx_corrections", "cx_captures", "cx_risks", "cx_punch", "cx_passes")

SCHEMA = "\n".join(
    f"""
CREATE TABLE IF NOT EXISTS {t} (
    project_id  TEXT NOT NULL,
    id          TEXT NOT NULL,
    doc         JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, id)
);
CREATE INDEX IF NOT EXISTS {t}_project_idx ON {t} (project_id, updated_at DESC);
"""
    for t in TABLES
)


def ensure_schema(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA)


def _dump(doc) -> str:
    return json.dumps(doc, default=str)


def upsert(conn: psycopg.Connection, table: str, project_id: str, doc_id: str, doc: dict, now: datetime | None = None) -> None:
    _check(table)
    conn.execute(
        f"INSERT INTO {table} (project_id, id, doc, updated_at) VALUES (%s, %s, %s::jsonb, %s) "
        f"ON CONFLICT (project_id, id) DO UPDATE SET doc = EXCLUDED.doc, updated_at = EXCLUDED.updated_at",
        (project_id, doc_id, _dump(doc), now or datetime.now(timezone.utc)),
    )


def upsert_many(conn: psycopg.Connection, table: str, project_id: str, docs: list[dict], now: datetime | None = None) -> int:
    _check(table)
    if not docs:
        return 0
    now = now or datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.executemany(
            f"INSERT INTO {table} (project_id, id, doc, updated_at) VALUES (%s, %s, %s::jsonb, %s) "
            f"ON CONFLICT (project_id, id) DO UPDATE SET doc = EXCLUDED.doc, updated_at = EXCLUDED.updated_at",
            [(project_id, d["id"], _dump(d), now) for d in docs],
        )
    return len(docs)


def replace_all(conn: psycopg.Connection, table: str, project_id: str, docs: list[dict], now: datetime | None = None) -> int:
    """The pass output for a table is the whole truth for that project -
    rows no longer produced are removed (their history lives in the pass
    report, not as stale rows)."""
    _check(table)
    keep = [d["id"] for d in docs]
    if keep:
        conn.execute(f"DELETE FROM {table} WHERE project_id = %s AND NOT (id = ANY(%s))", (project_id, keep))
    else:
        conn.execute(f"DELETE FROM {table} WHERE project_id = %s", (project_id,))
    return upsert_many(conn, table, project_id, docs, now)


def get(conn: psycopg.Connection, table: str, project_id: str, doc_id: str) -> dict | None:
    _check(table)
    row = conn.execute(f"SELECT doc FROM {table} WHERE project_id = %s AND id = %s", (project_id, doc_id)).fetchone()
    return row[0] if row else None


def list_docs(conn: psycopg.Connection, table: str, project_id: str, limit: int | None = None) -> list[dict]:
    _check(table)
    sql = f"SELECT doc FROM {table} WHERE project_id = %s ORDER BY updated_at DESC, id"
    params: tuple = (project_id,)
    if limit:
        sql += " LIMIT %s"
        params = (project_id, limit)
    return [r[0] for r in conn.execute(sql, params).fetchall()]


def delete(conn: psycopg.Connection, table: str, project_id: str, doc_id: str) -> bool:
    _check(table)
    cur = conn.execute(f"DELETE FROM {table} WHERE project_id = %s AND id = %s", (project_id, doc_id))
    return (cur.rowcount or 0) > 0


def list_projects(conn: psycopg.Connection) -> list[dict]:
    return [r[0] for r in conn.execute("SELECT doc FROM cx_projects ORDER BY updated_at DESC").fetchall()]


def get_project(conn: psycopg.Connection, project_id: str) -> dict | None:
    row = conn.execute("SELECT doc FROM cx_projects WHERE id = %s", (project_id,)).fetchone()
    return row[0] if row else None


def put_project(conn: psycopg.Connection, project: dict) -> None:
    upsert(conn, "cx_projects", project["id"], project["id"], project)


def delete_project(conn: psycopg.Connection, project_id: str) -> bool:
    found = get_project(conn, project_id) is not None
    for t in TABLES:
        conn.execute(f"DELETE FROM {t} WHERE project_id = %s", (project_id,))
    return found


def _check(table: str) -> None:
    if table not in TABLES:
        raise ValueError(f"unknown table {table!r}")


class PgRepo:
    """The module functions above, bound to one connection. `conn` is
    exposed so the engine can also reach the shared `faults` table."""

    def __init__(self, conn: psycopg.Connection):
        self.conn = conn
        ensure_schema(conn)

    def list_projects(self) -> list[dict]:
        return list_projects(self.conn)

    def get_project(self, project_id: str) -> dict | None:
        return get_project(self.conn, project_id)

    def put_project(self, project: dict) -> None:
        put_project(self.conn, project)

    def delete_project(self, project_id: str) -> bool:
        return delete_project(self.conn, project_id)

    def get(self, table: str, project_id: str, doc_id: str) -> dict | None:
        return get(self.conn, table, project_id, doc_id)

    def list_docs(self, table: str, project_id: str, limit: int | None = None) -> list[dict]:
        return list_docs(self.conn, table, project_id, limit)

    def upsert(self, table: str, project_id: str, doc: dict, now: datetime | None = None) -> None:
        upsert(self.conn, table, project_id, doc["id"], doc, now)

    def upsert_many(self, table: str, project_id: str, docs: list[dict], now: datetime | None = None) -> int:
        return upsert_many(self.conn, table, project_id, docs, now)

    def replace_all(self, table: str, project_id: str, docs: list[dict], now: datetime | None = None) -> int:
        return replace_all(self.conn, table, project_id, docs, now)

    def delete(self, table: str, project_id: str, doc_id: str) -> bool:
        return delete(self.conn, table, project_id, doc_id)


class MemoryRepo:
    """Same surface, dicts only - for tests and dry runs without Postgres.
    No `conn`, so the engine skips the shared faults table."""

    conn = None

    def __init__(self):
        self._t: dict[str, dict[tuple[str, str], tuple[dict, datetime]]] = {t: {} for t in TABLES}

    def list_projects(self) -> list[dict]:
        return [doc for doc, _ in sorted(self._t["cx_projects"].values(), key=lambda x: x[1], reverse=True)]

    def get_project(self, project_id: str) -> dict | None:
        hit = self._t["cx_projects"].get((project_id, project_id))
        return json.loads(_dump(hit[0])) if hit else None

    def put_project(self, project: dict) -> None:
        self.upsert("cx_projects", project["id"], project)

    def delete_project(self, project_id: str) -> bool:
        found = self.get_project(project_id) is not None
        for t in TABLES:
            self._t[t] = {k: v for k, v in self._t[t].items() if k[0] != project_id}
        return found

    def get(self, table: str, project_id: str, doc_id: str) -> dict | None:
        _check(table)
        hit = self._t[table].get((project_id, doc_id))
        return json.loads(_dump(hit[0])) if hit else None

    def list_docs(self, table: str, project_id: str, limit: int | None = None) -> list[dict]:
        _check(table)
        rows = sorted(((k, v) for k, v in self._t[table].items() if k[0] == project_id), key=lambda kv: (kv[1][1], kv[0][1]), reverse=True)
        docs = [json.loads(_dump(v[0])) for _, v in rows]
        return docs[:limit] if limit else docs

    def upsert(self, table: str, project_id: str, doc: dict, now: datetime | None = None) -> None:
        _check(table)
        self._t[table][(project_id, doc["id"])] = (json.loads(_dump(doc)), now or datetime.now(timezone.utc))

    def upsert_many(self, table: str, project_id: str, docs: list[dict], now: datetime | None = None) -> int:
        for d in docs:
            self.upsert(table, project_id, d, now)
        return len(docs)

    def replace_all(self, table: str, project_id: str, docs: list[dict], now: datetime | None = None) -> int:
        _check(table)
        self._t[table] = {k: v for k, v in self._t[table].items() if k[0] != project_id}
        return self.upsert_many(table, project_id, docs, now)

    def delete(self, table: str, project_id: str, doc_id: str) -> bool:
        _check(table)
        return self._t[table].pop((project_id, doc_id), None) is not None
