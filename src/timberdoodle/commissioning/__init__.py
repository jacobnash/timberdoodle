"""
Spec-grounded BAS discovery, commissioning and verification.

The spec is the hypothesis, the field is the evidence, and disagreement
between them is a finding to surface - never something to smooth over.
Every module here works in a plain-English canonical vocabulary
(`vocabulary.py`); Brick/Haystack are projections out of it
(`projection.py`), written through the existing graph layer in
`timberdoodle.ingest`/`timberdoodle.store` rather than a second copy of it.

Modules, in the order a pass runs them (`engine.run_pass`):

- `spec_model`  - Phase 1: intended inventory from the spec alone.
- `discovery`   - what the existing connector has landed in the graph
                  and history, plus FBF device/learn output pushed in.
- `points`      - plain-English function/role for every BACnet object.
- `alignment`   - Phase 2/4/6: evidence ladder, confidence, deviations,
                  corrections and field captures as constraints.
- `ladder`      - Phase 3: existence/liveness/responsiveness/command.
- `reconcile`   - Phase 5/8: unchanged/absent/returned/new/drifted/
                  re-addressed, faults vs accepted risks, freshness.
- `punchlist`   - Phase 7: route-ordered field verification list.
- `db`          - Postgres persistence (JSONB documents, one table per
                  record kind, idempotent DDL like the rest of the repo).
- `api`         - HTTP surface (port 8008, `/commissioning/` at the gateway).
- `reconciler`  - the daemon that re-runs passes on a timer.
"""
