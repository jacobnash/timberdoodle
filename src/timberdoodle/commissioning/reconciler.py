"""
Phase 8 daemon - runs a scheduled pass for every project so the model
keeps up with the building without anyone asking. Same shape as
fault_detector.py's His-mode sweep: one process, one loop, one Postgres
connection, recover-on-error.

    python -m timberdoodle.commissioning.reconciler [--interval SECONDS]

COMMISSIONING_PASS_INTERVAL (seconds, default 900) sets the cadence. A
pass only reads the graph and history; it writes the canonical model and
the shared faults table, never the graph, unless a project's settings
carry {"project_to_graph": true}.
"""

from __future__ import annotations

import argparse
import logging
import os
import time

from timberdoodle import tracing
from timberdoodle.commissioning import db, engine
from timberdoodle.remote_store import RemoteStore
from timberdoodle.timeseries import connect

log = logging.getLogger("commissioning.reconciler")
tracer = tracing.get_tracer(__name__)


def run_once(conn, store) -> list[dict]:
    repo = db.PgRepo(conn)
    out: list[dict] = []
    for project in repo.list_projects():
        with tracer.start_as_current_span("reconciler.pass") as span:
            span.set_attribute("project_id", project["id"])
            try:
                report = engine.run_pass(repo, project["id"], store=store, trigger="scheduled", project_to_graph=bool((project.get("settings") or {}).get("project_to_graph")))
                summary = {"project_id": project["id"], "pass_id": report["id"], **report["summary"], "changes": {k: len(v) for k, v in report["changes"].items() if v}, "freshness": report["freshness"]["statement"]}
                log.info("pass %s: %s", project["id"], summary)
                out.append(summary)
            except Exception as exc:
                span.set_attribute("error", str(exc))
                log.exception("pass failed for project %s", project["id"])
                out.append({"project_id": project["id"], "error": f"{exc.__class__.__name__}: {exc}"})
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=float(os.environ.get("COMMISSIONING_PASS_INTERVAL", "900")))
    parser.add_argument("--once", action="store_true", help="run one sweep and exit")
    parser.add_argument("--no-graph", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    tracing.init_tracing("timberdoodle-commissioning-reconciler")

    conn = connect()
    db.ensure_schema(conn)
    store = None if args.no_graph else RemoteStore()
    while True:
        started = time.monotonic()
        try:
            run_once(conn, store)
        except Exception:
            log.exception("sweep failed; reconnecting")
            try:
                conn.close()
            except Exception:  # noqa: BLE001, S110
                pass
            conn = connect()
        if args.once:
            return
        time.sleep(max(1.0, args.interval - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
