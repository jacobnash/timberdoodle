"""
Against a live Oxigraph (see test_remote_store.py's rationale for why -
proves this works against the real store, not just the in-memory one).
The injected fake llm_classify never touches the network - classify_point
with_fallback's LLM step is a plain injected callable, so nothing here
needs ANTHROPIC_API_KEY or the `anthropic` package installed.
"""

from dataclasses import dataclass

import pytest
from rdflib import Namespace

from timberdoodle.autotag import find_unclassified
from timberdoodle.ingest import ingest_tags
from timberdoodle.mapping import classify_point_with_fallback
from timberdoodle.remote_store import RemoteStore
from timberdoodle.store import BRICK

BLDG = Namespace("urn:test-autotag#")


@dataclass
class _FakeResult:
    brick_class: str
    confidence: float
    reasoning: str


@pytest.fixture
def store():
    s = RemoteStore()
    s._update(f'DELETE {{ ?s ?p ?o }} WHERE {{ ?s ?p ?o . FILTER(STRSTARTS(STR(?s), "urn:point:test-autotag")) }}')
    return s


@pytest.mark.integration
def test_find_unclassified_includes_a_fresh_miss_point(store):
    point_uri = ingest_tags(store, "test-autotag:mystery-1", {"someRandomTag": True})

    assert point_uri in find_unclassified(store)


@pytest.mark.integration
def test_find_unclassified_excludes_directly_classified_points(store):
    point_uri = ingest_tags(store, "test-autotag:direct-1", {"zone": True, "air": True, "temp": True, "sensor": True})
    classify_point_with_fallback(store, point_uri)  # direct rule match, no LLM needed

    assert point_uri not in find_unclassified(store)


@pytest.mark.integration
def test_classify_point_with_fallback_applies_llm_guess_above_threshold(store):
    point_uri = ingest_tags(store, "test-autotag:chiller-temp", {"someRandomTag": True})

    calls = []

    def fake_llm_classify(tags, label, description, existing_brick_classes):
        calls.append((tags, label))
        return _FakeResult(brick_class="Chiller", confidence=0.9, reasoning="looks like a chiller point")

    outcome, brick_class = classify_point_with_fallback(store, point_uri, llm_classify=fake_llm_classify)

    assert len(calls) == 1
    assert (outcome, brick_class) == ("llm", "Chiller")
    assert list(store.query(f"SELECT ?o WHERE {{ <{point_uri}> a <{BRICK.Chiller}> }}"))


@pytest.mark.integration
def test_classify_point_with_fallback_ignores_low_confidence_llm_guess(store):
    point_uri = ingest_tags(store, "test-autotag:low-confidence", {"someRandomTag": True})

    def fake_llm_classify(tags, label, description, existing_brick_classes):
        return _FakeResult(brick_class="Chiller", confidence=0.2, reasoning="not sure")

    outcome, brick_class = classify_point_with_fallback(store, point_uri, llm_classify=fake_llm_classify)

    assert (outcome, brick_class) == ("miss", None)
    assert not list(store.query(f"SELECT ?o WHERE {{ <{point_uri}> a <{BRICK.Chiller}> }}"))
