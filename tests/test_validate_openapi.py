"""
Same two-layer pattern as test_fault_openapi.py: (1) the spec is
well-formed OpenAPI, (2) real live requests against the real running
validate_api.py produce responses matching what the spec documents.
"""

from http.server import ThreadingHTTPServer
from threading import Thread

import jsonschema
import pytest
import requests
import yaml
from openapi_spec_validator import validate
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from timberdoodle.remote_store import RemoteStore
from timberdoodle.validate_api import OPENAPI_SPEC_PATH, make_handler


@pytest.fixture(scope="module")
def spec() -> dict:
    with open(OPENAPI_SPEC_PATH) as f:
        return yaml.safe_load(f)


def test_spec_is_well_formed_openapi(spec):
    validate(spec)


def _assert_matches_schema(instance, schema: dict, spec: dict) -> None:
    resource = Resource.from_contents(spec, default_specification=DRAFT202012)
    registry = Registry().with_resource(uri="spec", resource=resource)
    if "$ref" in schema and schema["$ref"].startswith("#"):
        schema = {"$ref": f"spec{schema['$ref']}"}
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls(schema, registry=registry).validate(instance)


@pytest.fixture
def live_server():
    store = RemoteStore()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store))
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.mark.integration
def test_get_openapi_yaml_serves_the_real_spec_file(live_server):
    resp = requests.get(f"{live_server}/openapi.yaml")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"] == "application/yaml"
    served = yaml.safe_load(resp.text)
    assert served["info"]["title"] == "Timberdoodle Validate API"


@pytest.mark.integration
def test_post_validate_matches_documented_schema(live_server, spec):
    resp = requests.post(f"{live_server}/validate")
    assert resp.status_code == 200
    body = resp.json()
    _assert_matches_schema(
        body,
        spec["paths"]["/validate"]["post"]["responses"]["200"]["content"]["application/json"]["schema"],
        spec,
    )
    assert isinstance(body["conforms"], bool)
    assert isinstance(body["violations"], list)
