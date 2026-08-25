// Plain-`node`-runnable self-check for jwt.js - njs's WebCrypto/Buffer
// surface is deliberately Node-API-shaped, so the same verify() logic
// that runs inside nginx runs here unmodified. `node gateway/njs/test_jwt.mjs`.
import assert from "node:assert/strict";
import jwt from "./jwt.js";

const SECRET = "test-secret-do-not-use-in-prod";

function b64url(buf) {
    return Buffer.from(buf).toString("base64url");
}

async function signToken(claims, { secret = SECRET, alg = "HS256", typ = "JWT" } = {}) {
    const header = { alg, typ };
    const headerPart = b64url(JSON.stringify(header));
    const payloadPart = b64url(JSON.stringify(claims));
    const signingInput = `${headerPart}.${payloadPart}`;
    const key = await crypto.subtle.importKey(
        "raw",
        Buffer.from(secret),
        { name: "HMAC", hash: "SHA-256" },
        false,
        ["sign"],
    );
    const sig = await crypto.subtle.sign({ name: "HMAC" }, key, Buffer.from(signingInput));
    return `${signingInput}.${b64url(sig)}`;
}

let passed = 0;
async function check(name, fn) {
    await fn();
    passed++;
    console.log(`ok - ${name}`);
}

const now = Math.floor(Date.now() / 1000);

await check("valid signature is accepted and claims returned", async () => {
    const token = await signToken({ sub: "user-1", role: "operator", exp: now + 3600 });
    const claims = await jwt.verify(token, SECRET);
    assert.ok(claims);
    assert.equal(claims.sub, "user-1");
    assert.equal(claims.role, "operator");
});

await check("wrong secret is rejected", async () => {
    const token = await signToken({ sub: "user-1", exp: now + 3600 });
    const claims = await jwt.verify(token, "a-different-secret");
    assert.equal(claims, null);
});

await check("tampered payload is rejected", async () => {
    const token = await signToken({ sub: "user-1", role: "viewer", exp: now + 3600 });
    const [h, p, s] = token.split(".");
    const tamperedPayload = b64url(JSON.stringify({ sub: "user-1", role: "admin", exp: now + 3600 }));
    const claims = await jwt.verify(`${h}.${tamperedPayload}.${s}`, SECRET);
    assert.equal(claims, null);
});

await check("expired token is rejected", async () => {
    const token = await signToken({ sub: "user-1", exp: now - 10 });
    const claims = await jwt.verify(token, SECRET);
    assert.equal(claims, null);
});

await check("token with no exp is accepted (long-lived api_key class)", async () => {
    const token = await signToken({ sub: "svc-1", kind: "api_key" });
    const claims = await jwt.verify(token, SECRET);
    assert.ok(claims);
});

await check("wrong algorithm in header is rejected (algorithm confusion guard)", async () => {
    const token = await signToken({ sub: "user-1", exp: now + 3600 }, { alg: "none" });
    const claims = await jwt.verify(token, SECRET);
    assert.equal(claims, null);
});

await check("malformed token (wrong number of segments) is rejected", async () => {
    assert.equal(await jwt.verify("not.a.valid.jwt.token", SECRET), null);
    assert.equal(await jwt.verify("not-a-jwt-at-all", SECRET), null);
    assert.equal(await jwt.verify("", SECRET), null);
    assert.equal(await jwt.verify(null, SECRET), null);
});

await check("malformed base64/JSON in header or payload is rejected", async () => {
    assert.equal(await jwt.verify("not-base64!.also-not.sig", SECRET), null);
});

console.log(`\n${passed} passed`);
