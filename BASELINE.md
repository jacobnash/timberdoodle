# Code quality baseline — 2026-09-05

Recorded per `PLAN.md` Stage 0.1, from the audit in `AUDIT.md`. Numbers here are the "before"
snapshot for the metrics tracked in `PLAN.md`'s Guardrails/Metrics sections — re-run the same
commands to compare after later stages land.

| Metric | Command | Baseline |
|---|---|---|
| `ruff check` (default rule set) | `ruff check src/ tests/` | 63 errors |
| `mypy` errors | `mypy --ignore-missing-imports src/timberdoodle` | 19 errors in 11 files |
| Cyclomatic complexity (average) | `radon cc -s -a src/` | A (2.73), 214 blocks, nothing above CC 18 |
| Maintainability index | `radon mi -s -n B src/` | no file below grade A |
| Line coverage | `pytest -m "integration or not integration" --cov=timberdoodle` | 81% (2,712 stmts / 524 missed) |
| Python duplication | `jscpd src/timberdoodle tests ui gateway/njs` | 6.06% (645 duplicated lines, 50 clone pairs) |
| JS duplication | same `jscpd` run | 3.26% (55 duplicated lines, 5 clone pairs) |
| `mapping.py` mutation score | `mutmut run` scoped to `mapping.py`+`webhooks.py` | 70.6% (230 killed / 96 survived, all 96 in `mapping.py`) |
| `webhooks.py` mutation score | same `mutmut run` | 100% (0 survivors) |

See `AUDIT.md` for full methodology, per-finding evidence, and what's explicitly out of scope for these numbers (e.g. `ui/` has no coverage/mutation tooling by design — buildless, no test runner).

## After Stage 0-2 (PLAN.md)

| Metric | Before | After |
|---|---|---|
| `ruff check` (default rule set) | 63 | 62 (one finding was in a since-stashed WIP file, not fixed by this work) |
| `mypy` errors | 19 | 9 |
| `ruff` `BLE001`/`RUF013` | 9 / 3 | 0 / 0 (Stage 1.4) |
| Python duplication | 6.06% (645 lines, 50 clones) | 3.79% (404 lines, 30 clones) (Stage 1.3 + Stage 2.1) |
| `mapping.py` mutation score | 70.6% (96 survivors) | closed the specific `classify_point` fallback-path gap `mutmut` flagged (Stage 0.3); full re-run not repeated here |

Stage 2.1 (extracting `http_handler_base.py` for the 5 stdlib HTTP APIs) is the largest single contributor to the duplication drop. Full audit-tool re-run (radon/vulture/full mutmut) not repeated here — see `AUDIT.md` for the original methodology if a fresh full pass is wanted later.
