"""
Same method surface as store.Store, backed by a running `oxigraph serve`
instance over HTTP+SPARQL instead of in-memory rdflib. store.Store stays
untouched for fast unit tests; this is what the live daemon points at.

Verified against a real running Oxigraph instance: single-triple adds via
SPARQL 1.1 Update (`/update`, INSERT DATA) land in the true default graph;
queries via `/query` with a JSON results Accept header. Ontology files are
parsed locally with rdflib (already-proven parsing) and pushed as batched
INSERT DATA statements, rather than relying on Oxigraph's bulk graph-store
endpoint, whose default-graph-targeting semantics weren't worth the time to
pin down for what's ultimately the same handful of one-time ontology loads.
"""

import os

import requests
from rdflib.term import Literal, URIRef


def _to_sparql_term(value) -> str:
    """Correct escaping for arbitrary literals (quotes, newlines, language
    tags, datatypes) matters here - Brick's ontology has plenty of them in
    rdfs:label/skos:definition. Reuse rdflib's own N3 serializer instead of
    hand-rolling escaping logic."""
    if isinstance(value, (URIRef, Literal)):
        return value.n3()
    if isinstance(value, str):
        return Literal(value).n3()
    return f"<{value}>"


class SparqlRow:
    """Attribute-style access over one SPARQL JSON result row, matching
    rdflib's `row.varname` style so callers don't care which Store they're
    talking to."""

    def __init__(self, bindings: dict):
        self._bindings = bindings

    def __getattr__(self, name: str):
        if name not in self._bindings:
            raise AttributeError(name)
        binding = self._bindings[name]
        value = binding["value"]
        if binding["type"] == "literal" and binding.get("datatype", "").endswith(("integer", "int")):
            return int(value)
        if binding["type"] == "literal" and binding.get("datatype", "").endswith(("double", "float", "decimal")):
            return float(value)
        return value


class RemoteStore:
    def __init__(self, base_url: str = None):
        self.base_url = (base_url or os.environ.get("OXIGRAPH_URL", "http://localhost:7878")).rstrip("/")
        # A bare requests.post() opens a fresh TCP connection every call -
        # a Session reuses one via HTTP keep-alive, cutting handshake cost
        # off every write/query this store ever does.
        self._session = requests.Session()

    def _update(self, sparql: str) -> None:
        resp = self._session.post(
            f"{self.base_url}/update",
            headers={"Content-Type": "application/sparql-update"},
            data=sparql.encode("utf-8"),
        )
        resp.raise_for_status()

    def load_ontology(self, path: str, fmt: str = "turtle") -> None:
        """Stream the file straight to Oxigraph's bulk graph-store endpoint
        (bare POST /store, no query params, targets the true default graph)
        rather than parsing with rdflib and re-serializing as batched INSERT
        DATA - ontologies like Brick are full of blank nodes (OWL
        restrictions), and SPARQL's INSERT DATA form rejects them; splitting
        across batches would also risk breaking blank node identity across
        requests. Letting Oxigraph parse the file directly sidesteps both."""
        content_type = {"turtle": "text/turtle", "xml": "application/rdf+xml", "n3": "text/n3"}.get(fmt, "text/turtle")
        with open(path, "rb") as f:
            resp = self._session.post(f"{self.base_url}/store", headers={"Content-Type": content_type}, data=f)
        resp.raise_for_status()

    def add_entity(self, uri: URIRef, rdf_type: URIRef) -> None:
        self._update(f"INSERT DATA {{ {_to_sparql_term(uri)} a {_to_sparql_term(rdf_type)} . }}")

    def add_relationship(self, subj: URIRef, predicate: URIRef, obj: URIRef) -> None:
        self._update(
            f"INSERT DATA {{ {_to_sparql_term(subj)} {_to_sparql_term(predicate)} {_to_sparql_term(obj)} . }}"
        )

    def remove_relationship(self, subj: URIRef, predicate: URIRef, obj: URIRef) -> None:
        self._update(
            f"DELETE DATA {{ {_to_sparql_term(subj)} {_to_sparql_term(predicate)} {_to_sparql_term(obj)} . }}"
        )

    def add_many(self, triples) -> None:
        """Every triple in one INSERT DATA block - one HTTP round trip
        instead of one per triple. Standard multi-triple SPARQL, not a
        multi-operation request - no need to verify Oxigraph accepts
        chained update operations, this is just several triple patterns
        inside a single INSERT DATA {}."""
        triples = list(triples)
        if not triples:
            return
        body = " ".join(
            f"{_to_sparql_term(subj)} {_to_sparql_term(predicate)} {_to_sparql_term(obj)} ."
            for subj, predicate, obj in triples
        )
        self._update(f"INSERT DATA {{ {body} }}")

    def add_derived_from(self, computed_point: URIRef, source_point: URIRef) -> None:
        from timberdoodle.store import PROV

        self.add_relationship(computed_point, PROV.wasDerivedFrom, source_point)

    def query(self, sparql: str):
        resp = self._session.post(
            f"{self.base_url}/query",
            headers={
                "Content-Type": "application/sparql-query",
                "Accept": "application/sparql-results+json",
            },
            data=sparql.encode("utf-8"),
        )
        resp.raise_for_status()
        return [SparqlRow(b) for b in resp.json()["results"]["bindings"]]

    def __len__(self) -> int:
        rows = self.query("SELECT (COUNT(*) as ?c) WHERE { ?s ?p ?o }")
        return rows[0].c
