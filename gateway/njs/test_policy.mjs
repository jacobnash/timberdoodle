// Plain-`node`-runnable self-check for policy.js. `node gateway/njs/test_policy.mjs`.
import assert from "node:assert/strict";
import policy from "./policy.js";

let passed = 0;
function check(name, fn) {
    fn();
    passed++;
    console.log(`ok - ${name}`);
}

check("every row in ROUTES resolves against its own path", () => {
    for (const [method, re, resource, minRole] of policy.ROUTES) {
        // Build a concrete URI from the regex source by stripping
        // anchors/groups for the common `[^/]+` id-placeholder case.
        const sampleUri = re.source
            .replace(/^\^/, "")
            .replace(/\$$/, "")
            .replace(/\\\//g, "/")
            .replace(/\[\^\/\]\+/g, "abc123")
            .replace(/\(openapi\\\.yaml\|docs\)/, "openapi.yaml")
            .replace(/\[a-z\]\+/g, "fault");
        const resolved = policy.resolveRoute(method, sampleUri);
        assert.ok(resolved, `expected ${method} ${sampleUri} to resolve`);
        assert.equal(resolved.resource, resource);
        assert.equal(resolved.minRole, minRole);
    }
});

check("unmapped route resolves to null", () => {
    assert.equal(policy.resolveRoute("GET", "/ingest/does-not-exist"), null);
    assert.equal(policy.resolveRoute("PATCH", "/fault/rules"), null); // wrong method
});

check("public routes (openapi.yaml, docs) have no role requirement", () => {
    const spec = policy.resolveRoute("GET", "/fault/openapi.yaml");
    assert.ok(spec);
    assert.equal(spec.minRole, null);
    const docs = policy.resolveRoute("GET", "/validate/docs");
    assert.ok(docs);
    assert.equal(docs.minRole, null);
});

check("roleSatisfies respects the viewer < operator < admin ordinal", () => {
    assert.equal(policy.roleSatisfies("viewer", "viewer"), true);
    assert.equal(policy.roleSatisfies("viewer", "operator"), false);
    assert.equal(policy.roleSatisfies("operator", "viewer"), true);
    assert.equal(policy.roleSatisfies("admin", "operator"), true);
    assert.equal(policy.roleSatisfies("operator", "admin"), false);
});

check("roleSatisfies always grants public routes (minRole null) regardless of role", () => {
    assert.equal(policy.roleSatisfies("viewer", null), true);
    assert.equal(policy.roleSatisfies("nonsense-role", null), true);
});

check("service role satisfies operator-tier routes (same rank)", () => {
    assert.equal(policy.roleSatisfies("service", "operator"), true);
    assert.equal(policy.roleSatisfies("service", "admin"), false);
});

check("unknown role satisfies nothing but public routes", () => {
    assert.equal(policy.roleSatisfies("bogus", "viewer"), false);
});

check("no two rows share an identical (method, source) pair", () => {
    const seen = new Set();
    for (const [method, re] of policy.ROUTES) {
        const key = `${method} ${re.source}`;
        assert.ok(!seen.has(key), `duplicate route: ${key}`);
        seen.add(key);
    }
});

console.log(`\n${passed} passed`);
