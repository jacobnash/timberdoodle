"""
Shared setup for the OpenAPI contract tests (test_*_openapi.py) - see
PLAN.md Stage 1.3. AUDIT.md's jscpd pass found this exact
schema-validation helper duplicated verbatim across files; extracted
here so a future OpenAPI-contract-test file doesn't have to copy it a
sixth time. Not a place for per-service fixtures (live_server, DB pool
setup, etc.) - each service's handler takes different constructor args,
and forcing those into one shared fixture would trade a small amount of
duplication for real coupling between otherwise-independent test files.
"""

import yaml
from jsonschema.validators import validator_for
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012


def load_spec(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def assert_matches_schema(instance, schema: dict, spec: dict) -> None:
    resource = Resource.from_contents(spec, default_specification=DRAFT202012)
    registry = Registry().with_resource(uri="spec", resource=resource)
    if "$ref" in schema and schema["$ref"].startswith("#"):
        schema = {"$ref": f"spec{schema['$ref']}"}
    validator_cls = validator_for(schema)
    validator_cls(schema, registry=registry).validate(instance)
