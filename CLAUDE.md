# Timberdoodle — agent notes

Full docs: [README.md](README.md) (setup, ports, tests) and
[docs-site/](docs-site) (architecture, fault detection, webhooks, Haxall
integrations). This file is gotchas only — don't duplicate those.

- **Src layout**: package is `src/timberdoodle/`, installed editable
  (`pip install -e .`; `pip install -e ".[dev,llm]"` for tests + autotag;
  add `,validate` for SHACL validation — `tests/test_shacl_validate.py`
  needs `pyshacl` installed or it errors at import).
- **No Makefile/justfile.** Every command is literally
  `python -m timberdoodle.<module>` or `docker compose ...` — check
  `docker-compose.yml`'s `command:` fields or a module's
  `if __name__ == "__main__"` block for the real invocation, don't assume
  a wrapper script exists. There is CI (`.github/workflows/ci.yml`, two
  jobs: `unit` needs no services, `integration` runs `docker compose up
  -d` first) — it just doesn't wrap anything, it runs the same bare
  commands above.
- **Env vars**: `.env.example` at the repo root is the single source of
  truth for every var any process reads — check there before grepping
  source.
- **Gateway auth files are gitignored and not auto-generated.**
  `docker compose up` will fail to start the `gateway` service until you
  run `htpasswd -bc gateway/read.htpasswd <user> <pass>` and the same for
  `write.htpasswd` (see README quick start). `-bc` creates-and-overwrites
  — if these files already exist (e.g. from a previous session) this
  silently replaces whatever credentials were in them; that's normal and
  expected, not a sign something's broken, but you'll need the new
  credentials for every subsequent request.
- **Gateway caches upstream addresses at its own startup.** If you
  restart a backend API container independently (e.g. `docker compose up
  -d --build ingest_api` after a code change) without also restarting
  `gateway`, routes through it can start returning 404/502 even though
  the backend is healthy. `docker compose restart gateway` fixes it —
  this is a real, reproducible gap between what the README documents as
  a supported independent-restart workflow and what actually works
  without the extra step.
- **`pytest -m "integration or not integration"` runs everything**, not a
  filtered subset — it's a tautology. It needs `docker compose up -d`
  first (hits live Postgres/Oxigraph/MQTT). Use
  `pytest -m "not integration"` for a services-free run.
- **`haystack_puller` is profile-gated**: `docker compose up -d` does not
  start it. Needs `docker compose --profile haystack-pull up -d
  haystack_puller`. Set `REMOTE_HAYSTACK_URL`/`REMOTE_HAYSTACK_USER` first
  — the container does NOT refuse to start without them (compose always
  passes them through, blank if unset), it just fails every pull cycle
  silently against an empty URL instead of erroring at startup.
- **Webhook URLs must be `https://` and resolve to a public address** —
  `fault_api.py`'s `POST /webhooks` and `fault_detector.py`'s delivery
  both reject loopback/private/reserved targets (SSRF protection,
  `webhooks.validate_url`). Testing against a local receiver (e.g.
  `http://127.0.0.1:<port>/`) needs `TIMBERDOODLE_ALLOW_PRIVATE_WEBHOOKS=1`
  set for whichever process is doing the registering/delivering — it's a
  deployment-level env var, not a per-webhook field, so it can't be set
  through the API itself.
- `todo/shacl-validation-and-service.md`'s design is now implemented
  (`ontology/`, `shacl_validate.py`, `validate_api.py` on port 8005) — the
  doc itself wasn't deleted (kept as design rationale), don't treat its
  "planned, not started" status line as current.
