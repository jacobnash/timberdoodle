# Timberdoodle

Ontology-agnostic building data platform: API-first, load-time-pluggable
(Brick/Haystack/DBO), headless by design. Time-series lives in
Postgres/TimescaleDB (so Grafana, or anything else that speaks SQL, reads
it natively — no plugin required); entity/relationship data lives in
Oxigraph as RDF, queryable over SPARQL. Every API is documented with
OpenAPI and served live off the process that implements it.

Feeds from [FBF](../fbf) (BACnet/Modbus → MQTT) over the same wire
envelope `mqtt_listener.py` and `POST /ingest` both consume.

## Quick start

```bash
htpasswd -bc gateway/read.htpasswd  <read-user>  <read-password>   # first time only
htpasswd -bc gateway/write.htpasswd <write-user> <write-password>  # first time only
docker compose up -d
```

That's everything — 5 infra containers (mosquitto, oxigraph, postgres,
jaeger, grafana), all 6 processes below, and an nginx gateway in front of
the 3 HTTP APIs, all with one shared lifecycle now. Each process is still
its own container/image build target if you need to restart or update one
independently (`docker compose up -d --build ingest_api`), but nothing
requires manual per-process startup anymore.

| Process | What it does | Port (direct) |
|---|---|---|
| `mqtt_listener` | Subscribes to `fbf/#`, writes every reading to Oxigraph + Postgres. | — |
| `ingest_api` | `POST /ingest` — same write path as the listener, for push-style sources. | 8000 |
| `fault_api` | CRUD for fault-detection rules + webhook subscriptions, `GET /faults`. | 8002 |
| `fault_detector` | Evaluates rules against live readings (Cur) and history (His), fires webhooks on fault open/resolve. | — |
| `derivation_api` | CRUD for derivations (computed/synthetic histories) + per-target health, `POST /derivations/test`, `POST /derivations/dry-run`. | 8003 |
| `derivation_engine` | Evaluates derivations on a sweep and, for cur-mode ones, on matching MQTT messages; writes results back into Oxigraph + Postgres. | — |

The 3 HTTP APIs aren't exposed on the host directly anymore — go through
the gateway at `localhost:8080` instead, prefixed by API name
(`/ingest/*`, `/fault/*`, `/derivation/*`). It requires HTTP basic auth:
`GET` requests need a `gateway/read.htpasswd` credential, anything else
(`POST`/`DELETE`) needs a `gateway/write.htpasswd` credential (a write
credential doesn't also satisfy read unless you add it to both files).
Both files are gitignored — generate your own with `htpasswd -bc` as shown
above. The demo browser UI (`ui/`) is served straight off the gateway at
[`localhost:8080/ui/`](http://localhost:8080/ui/) — no separate
`ui_server.py` needed for this path, and its `/ingest/history` calls go
through the same gateway (same origin), so the browser's native basic-auth
prompt covers it too.

```bash
curl -u <read-user>:<read-password> localhost:8080/ingest/openapi.yaml
curl -u <write-user>:<write-password> -X POST localhost:8080/ingest/ingest -d '...'
```

Each API still serves its own OpenAPI spec at `GET /openapi.yaml` and a
Swagger UI at `GET /docs` (through the gateway). For the full
cross-referenced documentation site (all 3 API catalog entries plus
architecture and feature docs), see [`docs-site/`](docs-site) —
`cd docs-site && npm install && npm run dev`.

Postgres's password comes from `POSTGRES_PASSWORD` in `.env` (defaults to
`timberdoodle` if unset — fine for local dev, change it for anything else).

### Mock hospital demo: auto-tagging, derivations/faults, UI

After [FBF](../fbf)'s mock hospital site is generated and provisioned (see
`../fbf/README.md`) and `mqtt_listener.py` is ingesting:

```bash
pip install -e ".[llm]"                    # anthropic + pydantic, only needed for autotag
ANTHROPIC_API_KEY=... python -m timberdoodle.autotag   # rule engine + LLM fallback for what it can't place
python scripts/seed_derivations_and_faults.py           # Brick derivations + fault rules (needs derivation_api/fault_api running)
python -m timberdoodle.ui_server                         # browse equipment/points/charts at localhost:8004 (no-store, so edits show up on refresh)
```

`ui_server.py` also serves `ui/devices.html` (localhost:8004/devices.html)
- a dashboard for FBF's periodic BACnet/Modbus device discovery: review
discovered devices, set per-device credentials, and provision connections,
all via direct browser calls to `fbf.api` (see
`../fbf/docs/periodic-discovery-and-credentials.md`). Pass `?fbf=<url>` if
`fbf.api` isn't on the default `localhost:8001`.

`autotag.py` sweeps every point/equip with tags but no direct Brick match,
runs `mapping.classify_point_with_fallback` on each (rule engine first,
Claude Haiku fallback second), and appends high-confidence guesses that
reuse an existing Brick class to `rules/haystack_to_brick.yaml`/
`rules/haystack_equip_to_brick.yaml` as commented-out proposals for a
human to review and promote - it never edits the live rule set itself.

## Infrastructure ports

| Service | Port | Purpose |
|---|---|---|
| mosquitto | 1883 | MQTT broker |
| oxigraph | 7878 | RDF/SPARQL graph store |
| postgres | 5433 (→5432 in-container) | Timescale hypertable, `point_history` |
| jaeger | 16686 (UI), 4318 (OTLP HTTP) | Traces — every process exports here |
| grafana | 3033 (→3000 in-container) | Dashboards over Postgres, anonymous admin enabled |
| gateway | 8080 | Basic-auth-gated reverse proxy in front of ingest/fault/derivation APIs |

Postgres is remapped to host port 5433, not the default 5432, to avoid
colliding with a locally-running Postgres instance. `ingest_api`/`fault_api`/
`derivation_api` don't publish host ports at all anymore — reach them
through the gateway on 8080.

## Tests

```bash
python -m pytest -m "integration or not integration"
```

The `integration` marker means "requires the docker-compose services
running" — see `pyproject.toml`.
