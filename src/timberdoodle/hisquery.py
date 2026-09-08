"""
Axon-style `readAll(filter).hisRead(span)` queries over Timberdoodle's own
two stores - the thing a SkySpark user reaches for first when they want
to see what a piece of equipment is doing ("read(ahu and air).hisRead(
thisMonth)" and every point's history is on screen). Served by
ingest_api.py's `GET /his`, rendered by ui/his.html.

Three separate concerns, three separate steps, so each is testable
without live services:

  1. parse_query()   - Axon-shaped text -> Query (mode, filter AST, span
                        text, optional rollup). Grammar in the docstring
                        of Parser below.
  2. compile_filter() - Haystack filter AST -> one SPARQL SELECT over the
                        entity graph. A name matches three ways at once:
                        as a Haystack marker (haystack:hasTag, see
                        ingest._write_tags); as a Brick class - with every
                        subclass and Brick alias folded in, case-insensitive,
                        so `Temperature_Sensor`, `AHU`, `air_handling_unit`
                        all work; and as a Brick *tag word* - the words a
                        class name is made of (brick:hasAssociatedTag), so
                        `temperature and sensor` finds Brick-typed points
                        that never had a Haystack tag. The hierarchy comes
                        from the vendored ontology via brick_vocab.py, not
                        from the live graph (see that module for why).
  3. resolve_span()  - Axon DateSpan vocabulary (today, thisMonth,
                        2026-09, 2026-09-01..2026-09-07, ...) -> a
                        half-open [start, end) pair of aware datetimes in
                        a given IANA timezone.

The only deliberate departure from Axon: hisRead here accepts EQUIPMENT
matches, not just points, and expands them to every point they
brick:hasPoint (through owl:sameAs merges, same as ingest.points_of_equip).
In Axon, `read(ahu).hisRead(today)` is an error (an equip rec has no
history); here it's the whole reason to have the feature - "show me this
AHU" shouldn't require the user to know the equipRef syntax first.

Not Axon, and not pretending to be: no `->` tag paths (`equipRef->dis`),
no arithmetic, no functions beyond hisRead/hisRollup, no unit conversion
on comparisons (`temp > 72°F` compares the bare number). Each of those
produces an explicit parse error naming what isn't supported, never a
silent wrong answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from timberdoodle import brick_vocab

DEFAULT_SPAN = "today"

# Axon's hisRead applies a default row limit too (10,000 in SkySpark, with
# `{-limit}` to lift it) - this keeps a month of 5-second data from
# turning one browser request into a 500k-row JSON body by accident.
DEFAULT_LIMIT = 10_000
MAX_LIMIT = 200_000

FOLDS = ("avg", "min", "max", "sum", "count")

# Axon duration unit suffixes -> seconds, or None for calendar units that
# have no fixed length (handled by date_trunc in timeseries.read_rollup).
_UNIT_SECONDS = {
    "s": 1, "sec": 1,
    "min": 60,
    "h": 3600, "hr": 3600,
    "day": 86400,
    "wk": 7 * 86400,
    "mo": None,
    "yr": None,
}


class HisQueryError(ValueError):
    """Anything wrong with the query text itself - surfaces as a 400."""


class NoMatchError(HisQueryError):
    """`read(filter)` (singular) matched nothing - Axon's UnknownRecErr.
    Surfaces as a 404; a readAll that matches nothing is just empty."""


# --- tokenizer ---------------------------------------------------------

_TOKEN_RE = re.compile(
    r"""
      (?P<DATE>\d{4}-\d{2}-\d{2})
    | (?P<MONTH>\d{4}-\d{2})(?![\d-])
    | (?P<NUMBER>-?\d+(?:\.\d+)?)(?P<UNIT>[A-Za-z°%][A-Za-z0-9°%/_]*)?
    | (?P<STRING>"(?:[^"\\]|\\.)*")
    | (?P<REF>@[A-Za-z0-9_:~./,\-]+)
    | (?P<RANGE>\.\.)
    | (?P<ARROW>->)
    | (?P<OP>==|!=|<=|>=|<|>)
    | (?P<PUNCT>[().,])
    | (?P<NAME>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<WS>\s+)
    """,
    re.VERBOSE,
)

KEYWORDS = {"and", "or", "not", "true", "false", "null"}


@dataclass
class Token:
    kind: str
    value: str
    pos: int
    unit: str | None = None


def tokenize(source: str) -> list[Token]:
    tokens: list[Token] = []
    i = 0
    while i < len(source):
        m = _TOKEN_RE.match(source, i)
        if m is None:
            raise HisQueryError(f"unexpected character {source[i]!r} at position {i}")
        i = m.end()
        kind = m.lastgroup
        if kind == "WS":
            continue
        if kind == "UNIT":
            # NUMBER's optional unit suffix - lastgroup reports the last
            # group that matched, so fold it back into a NUMBER token.
            tokens.append(Token("NUMBER", m.group("NUMBER"), m.start(), unit=m.group("UNIT")))
            continue
        value = m.group()
        if kind == "NAME" and value in KEYWORDS:
            kind = value
        tokens.append(Token(kind or "?", value, m.start()))
    tokens.append(Token("EOF", "", len(source)))
    return tokens


# --- filter AST ----------------------------------------------------------


@dataclass(frozen=True)
class Ref:
    id: str


@dataclass(frozen=True)
class Has:
    name: str


@dataclass(frozen=True)
class Cmp:
    name: str
    op: str
    value: object  # str | float | int | bool | Ref


@dataclass(frozen=True)
class Not:
    node: object


@dataclass(frozen=True)
class And:
    left: object
    right: object


@dataclass(frozen=True)
class Or:
    left: object
    right: object


@dataclass
class Rollup:
    fold: str
    interval: str  # as written, e.g. "1hr" - see parse_interval


@dataclass
class Query:
    mode: str  # "read" (first match only) or "readAll"
    filter: object
    filter_text: str
    span_text: str | None  # None -> DEFAULT_SPAN
    rollup: Rollup | None
    source: str


# --- parser --------------------------------------------------------------


class Parser:
    """
    query    := source ("." call)*
    source   := ("read" | "readAll") "(" filter ")"
              | filter                        # bare filter = readAll
    call     := "hisRead" "(" spanText ")"
              | "hisRollup" "(" fold "," interval ")"
    filter   := condOr
    condOr   := condAnd ("or" condAnd)*
    condAnd  := term ("and" term)*
    term     := "(" filter ")" | "not" term | name cmpOp value | name
    value    := string | number | bool | ref
    """

    def __init__(self, source: str):
        self.source = source
        self.tokens = tokenize(source)
        self.i = 0

    def _peek(self, offset: int = 0) -> Token:
        j = min(self.i + offset, len(self.tokens) - 1)
        return self.tokens[j]

    def _advance(self) -> Token:
        tok = self.tokens[self.i]
        if tok.kind != "EOF":
            self.i += 1
        return tok

    def _at(self, kind: str, value: str | None = None) -> bool:
        tok = self._peek()
        return tok.kind == kind and (value is None or tok.value == value)

    def _expect(self, kind: str, value: str | None = None) -> Token:
        tok = self._peek()
        if not self._at(kind, value):
            want = value or kind.lower()
            got = tok.value or "end of input"
            raise HisQueryError(f"expected {want!r} but found {got!r} at position {tok.pos}")
        return self._advance()

    def parse(self) -> Query:
        mode = "readAll"
        if self._at("NAME") and self._peek().value in ("read", "readAll") and self._peek(1).kind == "PUNCT" and self._peek(1).value == "(":
            mode = self._advance().value
            self._expect("PUNCT", "(")
            start = self._peek().pos
            node = self._parse_filter()
            end_tok = self._expect("PUNCT", ")")
            filter_text = self.source[start:end_tok.pos].strip()
        else:
            start = self._peek().pos
            node = self._parse_filter()
            filter_text = self.source[start:self._peek().pos].strip()

        span_text = None
        rollup = None
        while self._at("PUNCT", "."):
            self._advance()
            fn = self._expect("NAME")
            if fn.value == "hisRead":
                if span_text is not None:
                    raise HisQueryError("hisRead may only appear once")
                self._expect("PUNCT", "(")
                span_start = self._peek().pos
                depth = 0
                while not (self._at("PUNCT", ")") and depth == 0):
                    tok = self._advance()
                    if tok.kind == "EOF":
                        raise HisQueryError("unclosed hisRead( - missing ')'")
                    if tok.kind == "PUNCT" and tok.value == "(":
                        depth += 1
                    elif tok.kind == "PUNCT" and tok.value == ")":
                        depth -= 1
                close = self._expect("PUNCT", ")")
                span_text = self.source[span_start:close.pos].strip()
                if not span_text:
                    raise HisQueryError("hisRead() needs a span, e.g. hisRead(today) or hisRead(2026-09)")
                validate_span_text(span_text)
            elif fn.value == "hisRollup":
                if rollup is not None:
                    raise HisQueryError("hisRollup may only appear once")
                self._expect("PUNCT", "(")
                fold = self._expect("NAME").value
                if fold not in FOLDS:
                    raise HisQueryError(f"unknown rollup fold {fold!r}; supported: {', '.join(FOLDS)}")
                self._expect("PUNCT", ",")
                interval_tok = self._expect("NUMBER")
                interval_text = interval_tok.value + (interval_tok.unit or "")
                parse_interval(interval_text)  # raises on a bad unit
                self._expect("PUNCT", ")")
                rollup = Rollup(fold, interval_text)
            else:
                raise HisQueryError(
                    f"unsupported function {fn.value!r} at position {fn.pos}; only hisRead(span) and hisRollup(fold, interval) are supported"
                )

        tok = self._peek()
        if tok.kind != "EOF":
            raise HisQueryError(f"unexpected {tok.value!r} at position {tok.pos}")
        return Query(mode, node, filter_text, span_text, rollup, self.source)

    def _parse_filter(self):
        node = self._parse_and()
        while self._at("or"):
            self._advance()
            node = Or(node, self._parse_and())
        return node

    def _parse_and(self):
        node = self._parse_term()
        while self._at("and"):
            self._advance()
            node = And(node, self._parse_term())
        return node

    def _parse_term(self):
        tok = self._peek()
        if tok.kind == "PUNCT" and tok.value == "(":
            self._advance()
            node = self._parse_filter()
            self._expect("PUNCT", ")")
            return node
        if tok.kind == "not":
            self._advance()
            return Not(self._parse_term())
        if tok.kind != "NAME":
            got = tok.value or "end of input"
            raise HisQueryError(f"expected a tag name but found {got!r} at position {tok.pos}")
        name = self._advance().value
        if self._at("ARROW"):
            raise HisQueryError(
                f"tag paths ('{name}->...') aren't supported at position {self._peek().pos}; filter on the tag directly"
            )
        if self._at("OP"):
            op = self._advance().value
            return Cmp(name, op, self._parse_value())
        return Has(name)

    def _parse_value(self):
        tok = self._advance()
        if tok.kind == "STRING":
            return _unescape_string(tok.value[1:-1])
        if tok.kind == "NUMBER":
            # A unit suffix (72°F) is accepted and ignored - the store holds
            # bare numbers; no unit conversion happens on comparison.
            return float(tok.value) if "." in tok.value else int(tok.value)
        if tok.kind == "true":
            return True
        if tok.kind == "false":
            return False
        if tok.kind == "REF":
            return Ref(tok.value[1:])
        if tok.kind == "null":
            raise HisQueryError(f"comparing to null isn't supported at position {tok.pos}; use 'not <tag>' for a missing tag")
        got = tok.value or "end of input"
        raise HisQueryError(f"expected a value (string, number, true/false, or @ref) but found {got!r} at position {tok.pos}")


def _unescape_string(body: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), body)


def parse_query(source: str) -> Query:
    if not source or not source.strip():
        raise HisQueryError("empty query - try readAll(ahu).hisRead(today)")
    query = Parser(source).parse()
    # Semantic checks (id needs a @ref, '<' doesn't apply to refs, ...)
    # live in the compiler; running it here means every error a query can
    # produce surfaces at parse time, before any store is touched.
    compile_filter(query.filter)
    return query


# --- filter -> SPARQL ----------------------------------------------------

PREFIXES = """PREFIX haystack: <urn:timberdoodle:haystack#>
PREFIX brick: <https://brickschema.org/schema/Brick#>
PREFIX proj: <urn:timberdoodle:proj#>
PREFIX td: <urn:timberdoodle:td#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX sh: <http://www.w3.org/ns/shacl#>
"""

# If Brick itself has been loaded into the store (RemoteStore.load_ontology
# - queried through Oxigraph's --union-default-graph), its ~10k
# class/tag/shape subjects would otherwise all be filter candidates, and
# `readAll(Tag)` would hand back Brick's tag vocabulary as if it were
# equipment. Schema-level things aren't entities; drop them by what they
# are, not by named graph, so the same SPARQL runs on the in-memory rdflib
# Store (which can't evaluate GRAPH) as on Oxigraph.
_SCHEMA_TYPES = (
    "owl:Class", "owl:ObjectProperty", "owl:DatatypeProperty", "owl:AnnotationProperty", "owl:Ontology",
    "rdfs:Datatype", "rdfs:Class", "sh:NodeShape", "sh:PropertyShape", "brick:Tag",
)


def _looks_like_class(name: str) -> bool:
    """Brick classes are CapitalCase_With_Underscores; Haystack markers are
    lowerCamel with no underscores. So any uppercase letter *or* an
    underscore means "a class name" - which is what lets
    `air_handling_unit` resolve case-insensitively without ever colliding
    with a marker like `ahu`."""
    return "_" in name or any(ch.isupper() for ch in name)


@lru_cache(maxsize=1)
def _rule_classes_by_tag() -> dict[str, frozenset[str]]:
    """Inverse of rules/haystack_*_to_brick.yaml: a Haystack marker -> the
    Brick classes this project's own mapping rules mint from tag sets
    containing it. Sound in this direction (an entity mapping.py typed as
    Fan_Status *was* `fan run sensor`), and it's what makes a marker like
    `run` - which Brick has no tag word for - still find Brick-only points."""
    from timberdoodle import mapping

    out: dict[str, set[str]] = {}
    for path in (mapping.DEFAULT_RULES_PATH, mapping.DEFAULT_RULES_PATH.replace("haystack_to_brick", "haystack_equip_to_brick")):
        try:
            rules = mapping.load_rules(path)
        except FileNotFoundError:
            continue
        for rule in rules:
            for tag in rule["tags"]:
                out.setdefault(tag, set()).add(rule["brick_class"])
    return {tag: frozenset(classes) for tag, classes in out.items()}

# Structural markers every Haystack rec of that kind carries, but which a
# BACnet/MQTT-sourced entity here never got as an explicit tag (FBF pushes
# domain markers like `temp`/`sensor`, not `point`; equip identity is a
# topic_prefix, see ingest.topic_prefix_to_equip_uri). Matching them on
# graph structure too is what makes `point and equipRef==@x` behave the
# way a SkySpark user expects regardless of which source the data came
# from.
_STRUCTURAL_ALIASES = {
    "point": ["?s a td:RawPoint", "?s brick:isPointOf ?{v}"],
    "equip": ["?s brick:hasPoint ?{v}"],
    "site": ["?s a brick:Site"],
}


def _sparql_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    return f'"{escaped}"'


def _sparql_literal(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return _sparql_string(str(value))


def ref_to_equip_uri(ref: str) -> str:
    """A ref is either a full graph URI (`@urn:equip:fbf/ahu-1`, the way
    this UI's tree links write them) or a bare Haystack id (`@AHU-1`),
    which resolves the same way ingest.link_equip_ref does."""
    if ref.startswith("urn:"):
        return ref
    return f"urn:equip:haystack:{ref}"


_SPARQL_OPS = {"==": "=", "!=": "!=", "<": "<", "<=": "<=", ">": ">", ">=": ">="}


BRICK_NS = "https://brickschema.org/schema/Brick#"
PROJ_NS = "urn:timberdoodle:proj#"


def custom_subclass_map(store) -> dict[str, set[str]]:
    """Every rdfs:subClassOf the live graph asserts about a class that
    isn't Brick's own - in practice the PROJ classes mapping.py mints as
    direct subclasses of a Brick class (urn:timberdoodle:proj#X). Keyed by
    parent IRI -> child IRIs. One cheap query; folding this into the class
    list handed to SPARQL is what lets the compiled filter stay a bare
    `?s a ?c` (Oxigraph spends ~1s per query on `?t rdfs:subClassOf ?c`
    inside EXISTS with the full ontology loaded, whatever the list size)."""
    rows = store.query(
        PREFIXES
        + f"""SELECT ?sub ?sup WHERE {{
            ?sub rdfs:subClassOf ?sup .
            FILTER(isIRI(?sub) && isIRI(?sup) && !STRSTARTS(STR(?sub), "{BRICK_NS}"))
        }}"""
    )
    out: dict[str, set[str]] = {}
    for row in rows:
        out.setdefault(str(row.sup), set()).add(str(row.sub))
    return out


class _Compiler:
    """Every leaf test (a tag, a class, a comparison) becomes a *set of
    subjects* - `OPTIONAL { { SELECT DISTINCT ?s (true AS ?mK) WHERE {...}
    } }` - and the boolean expression is composed from `BOUND(?mK)`.
    Each set is computed once and hash-joined to the candidates, so cost
    is O(matching triples), not O(candidates x list size): the same query
    that took 0.4-5s as per-candidate EXISTS clauses is a flat ~0.15s
    here, whether the class list has 5 entries or 1,400. `not` is just
    `!BOUND`, and a comparison on a missing tag is unbound, i.e. false -
    Haystack's semantics for free."""

    def __init__(self, custom_subclasses: dict[str, set[str]] | None = None):
        self.n = 0
        self.custom_subclasses = custom_subclasses or {}
        self.flags: list[tuple[str, str]] = []  # (flag var, pattern that binds ?s)

    def _with_custom_descendants(self, class_iris: set[str]) -> set[str]:
        out, stack = set(), list(class_iris)
        while stack:
            iri = stack.pop()
            if iri in out:
                continue
            out.add(iri)
            stack.extend(self.custom_subclasses.get(iri, ()))
        return out

    def _var(self) -> str:
        self.n += 1
        return f"v{self.n}"

    def _flag(self, pattern: str) -> str:
        """Register a subject-set and return the boolean expression that
        tests membership."""
        m = f"m{len(self.flags) + 1}"
        self.flags.append((m, pattern))
        return f"BOUND(?{m})"

    def _any(self, exprs: list[str]) -> str:
        return exprs[0] if len(exprs) == 1 else "(" + " || ".join(exprs) + ")"

    def expr(self, node) -> str:
        if isinstance(node, Has):
            return self._has(node.name)
        if isinstance(node, Cmp):
            return self._cmp(node)
        if isinstance(node, Not):
            return f"(!{self.expr(node.node)})"
        if isinstance(node, And):
            return f"({self.expr(node.left)} && {self.expr(node.right)})"
        if isinstance(node, Or):
            return f"({self.expr(node.left)} || {self.expr(node.right)})"
        raise HisQueryError(f"cannot compile {node!r}")

    def _typed_as_any(self, class_iris: set[str]) -> str:
        """`?s` is typed as one of these classes or anything the live graph
        declares beneath them (PROJ classes). Every hop of hierarchy is
        resolved before SPARQL sees it - Brick's own from brick_vocab, the
        graph's from custom_subclass_map - so this is a bound VALUES list
        and a single `?s a ?c`. Any formulation that walks rdfs:subClassOf
        inside the query measured 1s-50s on Oxigraph with the full
        ontology loaded."""
        c = self._var()
        iris = self._with_custom_descendants(class_iris)
        members = " ".join(f"<{iri}>" for iri in sorted(iris))
        return self._flag(f"VALUES ?{c} {{ {members} }} ?s a ?{c}")

    def _has(self, name: str) -> str:
        vocab = brick_vocab.load()
        parts = [self._flag(f"?s haystack:hasTag {_sparql_string(name)}")]
        if _looks_like_class(name):
            canonical = vocab.canonical_class(name)
            # Air_Handling_Unit also finds Rooftop_Unit, DOAS, ... and
            # entities typed with Brick's own alias brick:AHU. A name Brick
            # doesn't know (a PROJ class, a newer Brick, an ontology-less
            # deployment) is matched as an exact brick:/proj: type.
            brick_names = vocab.descendants(canonical) if canonical else {name}
            parts.append(self._typed_as_any({BRICK_NS + n for n in brick_names} | {PROJ_NS + name}))
        else:
            # A plain word: Brick's own tag vocabulary (`temperature`,
            # `setpoint`, `ahu`) with Haystack spellings mapped (`temp`,
            # `sp`, `cmd`), plus this project's mapping rules run backwards.
            classes: set[str] = set()
            tag = vocab.canonical_tag(name)
            if tag:
                classes |= vocab.classes_with_tag(tag)
            for cls in _rule_classes_by_tag().get(name, ()):
                classes |= vocab.descendants(cls) or {cls}
            if classes:
                parts.append(self._typed_as_any({BRICK_NS + n for n in classes}))
        for alias in _STRUCTURAL_ALIASES.get(name, []):
            parts.append(self._flag(alias.replace("{v}", self._var())))
        return self._any(parts)

    def _cmp(self, node: Cmp) -> str:
        if node.name == "id":
            if not isinstance(node.value, Ref):
                raise HisQueryError("id must be compared to a @ref, e.g. id == @urn:equip:fbf/ahu-1")
            if node.op not in ("==", "!="):
                raise HisQueryError("id only supports == and !=")
            e = f"(?s = <{_uri(node.value.id)}>)"
            return e if node.op == "==" else f"(!{e})"

        if node.op not in ("==", "!=") and isinstance(node.value, (Ref, bool)):
            raise HisQueryError(f"'{node.op}' only applies to numbers and strings, not {type(node.value).__name__.lower()}s")

        if isinstance(node.value, Ref):
            v = self._var()
            parts = [self._flag(f"?s haystack:{node.name} ?{v} . FILTER(STR(?{v}) = {_sparql_string(node.value.id)})")]
            if node.name == "equipRef":
                parts.append(self._flag(f"?s brick:isPointOf <{_uri(ref_to_equip_uri(node.value.id))}>"))
            e = self._any(parts)
            return e if node.op == "==" else f"(!{e})"

        v = self._var()
        # A comparison on a missing tag is false in Haystack (so `x != 1`
        # doesn't match recs with no x at all) - an unbound flag is exactly that.
        return self._flag(f"?s haystack:{node.name} ?{v} . FILTER(?{v} {_SPARQL_OPS[node.op]} {_sparql_literal(node.value)})")


def _uri(text: str) -> str:
    if any(c in text for c in "<> \"{}|\\^`"):
        raise HisQueryError(f"invalid characters in ref {text!r}")
    return text


def compile_filter(node, custom_subclasses: dict[str, set[str]] | None = None) -> str:
    """One SELECT over every entity subject in the graph. Each leaf test
    of the filter is a subject-set (see _Compiler), left-joined once;
    the whole boolean expression is one FILTER over those flags, so
    and/or/not compose as plain SPARQL &&/||/! with no MINUS juggling.

    `custom_subclasses` is custom_subclass_map(store) - pass it when
    compiling against a live store so PROJ classes count as their Brick
    parent; parse_query compiles without it purely to surface errors."""
    compiler = _Compiler(custom_subclasses)
    body = compiler.expr(node)
    flags = "\n".join(
        f"  OPTIONAL {{ {{ SELECT DISTINCT ?s (true AS ?{m}) WHERE {{ {pattern} }} }} }}"
        for m, pattern in compiler.flags
    )
    return (
        PREFIXES
        + "SELECT DISTINCT ?s WHERE {\n"
        + "  { SELECT DISTINCT ?s WHERE { ?s ?p ?o . FILTER(isIRI(?s))\n"
        + f"      FILTER NOT EXISTS {{ ?s a ?schema . FILTER(?schema IN ({', '.join(_SCHEMA_TYPES)})) }} }} }}\n"
        + (flags + "\n" if flags else "")
        + f"  FILTER({body})\n"
        + "}"
    )


def _has_names(node) -> list[str]:
    if isinstance(node, Has):
        return [node.name]
    if isinstance(node, Not):
        return _has_names(node.node)
    if isinstance(node, (And, Or)):
        return _has_names(node.left) + _has_names(node.right)
    return []


def hints_for_empty_result(node) -> list[dict]:
    """Why might this filter have matched nothing? Only Brick-vocabulary
    answers - a class name Brick doesn't have but nearly does
    (Air_Handeling_Unit), a word that's almost a Brick tag (temperture).
    Never raised as an error: the name may be a perfectly good Haystack
    marker or PROJ class this file can't know about."""
    vocab = brick_vocab.load()
    hints = []
    seen = set()
    for name in _has_names(node):
        if name in seen or name in _STRUCTURAL_ALIASES:
            continue
        seen.add(name)
        if _looks_like_class(name):
            if vocab.canonical_class(name):
                continue
            suggestions = vocab.suggest_class(name)
        else:
            if vocab.canonical_tag(name) or name in _rule_classes_by_tag():
                continue
            suggestions = vocab.suggest_tag(name)
        if suggestions:
            hints.append({"token": name, "suggestions": suggestions})
    return hints


# --- spans -----------------------------------------------------------------


@dataclass(frozen=True)
class Span:
    start: datetime  # inclusive, aware
    end: datetime  # exclusive, aware
    label: str


_NAMED_SPANS = {
    "today", "yesterday",
    "thisWeek", "lastWeek", "pastWeek",
    "thisMonth", "lastMonth", "pastMonth",
    "thisQuarter", "lastQuarter",
    "thisYear", "lastYear", "pastYear",
}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
_YEAR_RE = re.compile(r"^\d{4}$")


def _first_of_month(d: date) -> date:
    return d.replace(day=1)


def _add_months(d: date, n: int) -> date:
    """First-of-month arithmetic only - d is always day 1 here."""
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def _date_span(text: str, today: date) -> tuple[date, date]:
    """Returns (first date, last date) inclusive, Axon DateSpan-style."""
    if _DATE_RE.match(text):
        d = date.fromisoformat(text)
        return d, d
    if _MONTH_RE.match(text):
        first = date.fromisoformat(text + "-01")
        return first, _add_months(first, 1) - timedelta(days=1)
    if _YEAR_RE.match(text):
        y = int(text)
        return date(y, 1, 1), date(y, 12, 31)

    match text:
        case "today":
            return today, today
        case "yesterday":
            return today - timedelta(days=1), today - timedelta(days=1)
        case "thisWeek":
            # ISO weeks (Monday start). Axon is locale-based; this repo
            # doesn't carry a locale, so it's fixed and documented.
            start = today - timedelta(days=today.weekday())
            return start, start + timedelta(days=6)
        case "lastWeek":
            start = today - timedelta(days=today.weekday() + 7)
            return start, start + timedelta(days=6)
        case "pastWeek":
            return today - timedelta(days=7), today
        case "thisMonth":
            first = _first_of_month(today)
            return first, _add_months(first, 1) - timedelta(days=1)
        case "lastMonth":
            first = _add_months(_first_of_month(today), -1)
            return first, _first_of_month(today) - timedelta(days=1)
        case "pastMonth":
            return today - timedelta(days=30), today
        case "thisQuarter":
            first = date(today.year, ((today.month - 1) // 3) * 3 + 1, 1)
            return first, _add_months(first, 3) - timedelta(days=1)
        case "lastQuarter":
            this_first = date(today.year, ((today.month - 1) // 3) * 3 + 1, 1)
            first = _add_months(this_first, -3)
            return first, this_first - timedelta(days=1)
        case "thisYear":
            return date(today.year, 1, 1), date(today.year, 12, 31)
        case "lastYear":
            return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
        case "pastYear":
            return today - timedelta(days=365), today
    raise HisQueryError(
        f"unknown span {text!r}; use one of {', '.join(sorted(_NAMED_SPANS))}, "
        "a date (2026-09-03), a month (2026-09), a year (2026), or a range (2026-09-01..2026-09-07)"
    )


def _normalize_span_text(text: str) -> str:
    text = text.strip()
    # today() and today are the same thing in Axon.
    return text.removesuffix("()")


def validate_span_text(text: str) -> None:
    resolve_span(text, now=datetime(2000, 1, 1, tzinfo=ZoneInfo("UTC")), tz="UTC")


def resolve_span(text: str | None, now: datetime, tz: str = "UTC") -> Span:
    """`now` may be in any zone; 'today' etc. are computed in `tz`, and the
    returned bounds are midnight-aligned in that zone (so a day span in
    America/New_York is the local calendar day, not the UTC one)."""
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HisQueryError(f"unknown timezone {tz!r}") from exc
    label = _normalize_span_text(text or DEFAULT_SPAN)
    today = now.astimezone(zone).date()

    if ".." in label:
        a, b = (part.strip() for part in label.split("..", 1))
        if not a or not b:
            raise HisQueryError(f"range {label!r} needs both ends, e.g. 2026-09-01..2026-09-07")
        first, _ = _date_span(_normalize_span_text(a), today)
        _, last = _date_span(_normalize_span_text(b), today)
        if last < first:
            raise HisQueryError(f"range {label!r} ends before it starts")
    else:
        first, last = _date_span(label, today)

    start = datetime(first.year, first.month, first.day, tzinfo=zone)
    end_day = last + timedelta(days=1)
    end = datetime(end_day.year, end_day.month, end_day.day, tzinfo=zone)
    return Span(start, end, label)


# --- execution -------------------------------------------------------------

_MATCHED_PREVIEW = 200


def _local_name(uri: str) -> str:
    i = max(uri.rfind("#"), uri.rfind("/"), uri.rfind(":"))
    return uri[i + 1 :]


def _split_concat(value) -> list[str]:
    return [part for part in str(value).split(" ") if part] if value else []


def _point_key(uri: str) -> str:
    """The `point` value GET /history takes back - the same identity POST
    /ingest used (see ingest.topic_to_point_uri)."""
    return uri.removeprefix("urn:point:")


def _infer_kind(rows) -> str | None:
    for _, value in rows:
        if isinstance(value, bool):
            return "Bool"
        if isinstance(value, (int, float)):
            return "Number"
        if value is not None:
            return "Str"
    return None


def match_entities(store, node) -> list[str]:
    rows = store.query(compile_filter(node, custom_subclass_map(store)))
    return sorted(str(row.s) for row in rows)


def expand_to_points(store, entity_uris: list[str]) -> list[str]:
    """Points among the matches, plus every point of any matched equip
    (walking owl:sameAs merges the same way ingest.points_of_equip does).
    Two simple queries rather than one clever UNION/BIND - the plain
    version has no unbound-variable corner cases to get wrong."""
    if not entity_uris:
        return []
    points = {uri for uri in entity_uris if uri.startswith("urn:point:")}
    values = " ".join(f"<{_uri(u)}>" for u in entity_uris)
    rows = store.query(
        PREFIXES
        + f"""SELECT DISTINCT ?point WHERE {{
            VALUES ?m {{ {values} }}
            ?m owl:sameAs* ?e .
            ?e brick:hasPoint ?point .
        }}"""
    )
    points.update(str(row.point) for row in rows)
    return sorted(points)


def describe_points(store, point_uris: list[str]) -> dict[str, dict]:
    if not point_uris:
        return {}
    values = " ".join(f"<{_uri(u)}>" for u in point_uris)
    rows = store.query(
        PREFIXES
        + f"""SELECT ?point
                 (GROUP_CONCAT(DISTINCT COALESCE(STR(?type), ""); separator=" ") AS ?types)
                 (GROUP_CONCAT(DISTINCT COALESCE(STR(?tag), ""); separator=" ") AS ?tags)
                 (SAMPLE(?topic) AS ?topic_)
                 (SAMPLE(?dis) AS ?dis_)
                 (SAMPLE(?unit) AS ?unit_)
                 (SAMPLE(?kind) AS ?kind_)
                 (SAMPLE(?equip) AS ?equip_)
                 (SAMPLE(?equipDis) AS ?equipDis_)
        WHERE {{
            VALUES ?point {{ {values} }}
            OPTIONAL {{ ?point a ?type }}
            OPTIONAL {{ ?point haystack:hasTag ?tag }}
            OPTIONAL {{ ?point td:sourceTopic ?topic }}
            OPTIONAL {{ ?point haystack:dis ?dis }}
            OPTIONAL {{ ?point haystack:unit ?unit }}
            OPTIONAL {{ ?point haystack:kind ?kind }}
            OPTIONAL {{ ?point brick:isPointOf ?equip . OPTIONAL {{ ?equip haystack:dis ?equipDis }} }}
        }}
        GROUP BY ?point"""
    )
    out: dict[str, dict] = {}
    for row in rows:
        uri = str(row.point)
        types = _split_concat(getattr(row, "types", None))
        # A real Brick class over a PROJ fallback when a point somehow has both.
        classes = sorted(
            (t for t in types if t.startswith(("https://brickschema.org/", "urn:timberdoodle:proj#"))),
            key=lambda t: (not t.startswith("https://brickschema.org/"), t),
        )
        brick_classes = [_local_name(t) for t in classes]
        topic = getattr(row, "topic_", None)
        equip = getattr(row, "equip_", None)
        equip = str(equip) if equip else None
        dis_tag = getattr(row, "dis_", None)
        dis = dis_tag or (str(topic).rsplit("/", 1)[-1] if topic else _local_name(uri))
        equip_dis = getattr(row, "equipDis_", None) or (equip.replace("urn:equip:", "", 1) if equip else None)
        unit = getattr(row, "unit_", None)
        kind = getattr(row, "kind_", None)
        out[uri] = {
            "id": uri,
            "point": _point_key(uri),
            "dis": str(dis),
            "_dis_from_tag": dis_tag is not None,
            "unit": str(unit) if unit else None,
            "kind": str(kind) if kind else None,
            "brickClass": brick_classes[0] if brick_classes else None,
            "tags": sorted(_split_concat(getattr(row, "tags", None))),
            "equip": equip,
            "equipDis": str(equip_dis) if equip_dis else None,
        }
    for uri in point_uris:
        out.setdefault(uri, {
            "id": uri, "point": _point_key(uri), "dis": _local_name(uri), "_dis_from_tag": False, "unit": None, "kind": None,
            "brickClass": None, "tags": [], "equip": None, "equipDis": None,
        })
    return out


def run_query(store, ts_conn, query: Query, now: datetime, tz: str = "UTC", limit: int = DEFAULT_LIMIT) -> dict:
    """The whole read(...).hisRead(...).hisRollup(...) pipeline against
    live stores. Raises HisQueryError for a `read` (singular) that matches
    nothing - Axon's read() throws UnknownRecErr for the same case; a
    readAll with no matches is an empty, successful result."""
    from timberdoodle import timeseries

    span = resolve_span(query.span_text, now, tz)
    matched = match_entities(store, query.filter)
    if query.mode == "read":
        if not matched:
            raise NoMatchError(f"read({query.filter_text}): no rec matches")
        matched = matched[:1]

    point_uris = expand_to_points(store, matched)
    meta = describe_points(store, point_uris)
    # Computed points (derivation_engine) carry unit/label only on their
    # history rows, never as graph tags - without this they'd chart as an
    # unlabelled "mock-ahu-1" with no unit.
    for uri, (unit, label) in timeseries.read_point_labels(ts_conn, point_uris).items():
        info = meta[uri]
        if not info["unit"] and unit:
            info["unit"] = unit
        if label and not info["_dis_from_tag"]:
            info["dis"] = label

    rollup = None
    if query.rollup:
        interval = parse_interval(query.rollup.interval)
        try:
            history = {
                uri: (rows, False)
                for uri, rows in timeseries.read_rollup(
                    ts_conn, point_uris, span.start, span.end, query.rollup.fold,
                    interval.seconds, interval.months, tz,
                ).items()
            }
        except ValueError as exc:
            raise HisQueryError(str(exc)) from exc
        rollup = {"fold": query.rollup.fold, "interval": interval.text}
    else:
        history = timeseries.read_range_many(ts_conn, point_uris, span.start, span.end, limit)

    series = []
    for uri in point_uris:
        info = dict(meta[uri])
        info.pop("_dis_from_tag")
        rows, truncated = history.get(uri, ([], False))
        if rollup:
            info["kind"] = "Number"
        elif not info.get("kind"):
            info["kind"] = _infer_kind(rows)
        info["history"] = [{"ts": ts.timestamp(), "value": value} for ts, value in rows]
        info["truncated"] = truncated
        series.append(info)
    series.sort(key=lambda s: ((s["equipDis"] or ""), s["dis"], s["id"]))

    return {
        "expr": query.source,
        "mode": query.mode,
        "filter": query.filter_text,
        "span": {"start": span.start.timestamp(), "end": span.end.timestamp(), "label": span.label, "tz": tz},
        "rollup": rollup,
        "limit": limit,
        "matched": matched[:_MATCHED_PREVIEW],
        "matchedCount": len(matched),
        "hints": hints_for_empty_result(query.filter) if not matched else [],
        "series": series,
    }


# --- rollup intervals ------------------------------------------------------


@dataclass(frozen=True)
class Interval:
    text: str
    seconds: int | None  # fixed-length intervals
    months: int | None  # calendar intervals (mo/yr)


_INTERVAL_RE = re.compile(r"^(\d+)([A-Za-z]+)$")


def parse_interval(text: str) -> Interval:
    m = _INTERVAL_RE.match(text.strip())
    if not m:
        raise HisQueryError(f"bad interval {text!r}; expected e.g. 15min, 1hr, 1day, 1wk, 1mo")
    n, unit = int(m.group(1)), m.group(2)
    if n <= 0:
        raise HisQueryError(f"interval {text!r} must be positive")
    if unit not in _UNIT_SECONDS:
        raise HisQueryError(f"unknown interval unit {unit!r}; supported: {', '.join(_UNIT_SECONDS)}")
    secs = _UNIT_SECONDS[unit]
    if secs is None:
        return Interval(text, None, n * (12 if unit == "yr" else 1))
    return Interval(text, n * secs, None)
