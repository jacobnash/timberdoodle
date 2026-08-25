/*
 * Pure JWT (HS256) verification - no nginx globals (`r`, `ngx`) touched
 * here, so this file is plain-`node`-runnable (see test_jwt.mjs). The
 * WebCrypto surface (`crypto.subtle`) is available identically in both
 * njs and Node, which is what makes that possible.
 *
 * Verifies the signature over the exact "header.payload" bytes received,
 * never a re-serialized copy of the decoded JSON - same principle as
 * webhooks.py's HMAC signing/verification.
 */

function base64UrlDecode(s) {
    return Buffer.from(s, "base64url");
}

async function importHmacKey(secret) {
    return crypto.subtle.importKey(
        "raw",
        Buffer.from(secret),
        { name: "HMAC", hash: "SHA-256" },
        false,
        ["verify"],
    );
}

/*
 * Returns the decoded claims object if `token` is a well-formed,
 * correctly-signed, unexpired HS256 JWT; otherwise null. Never throws -
 * every failure mode (malformed token, wrong algorithm, bad signature,
 * expired) collapses to the same "null means reject" outcome, since the
 * caller (main.js) only ever needs to know accept-or-401.
 */
async function verify(token, secret) {
    if (typeof token !== "string") {
        return null;
    }
    const parts = token.split(".");
    if (parts.length !== 3) {
        return null;
    }
    // No array destructuring - njs 1.0.0 (bundled with nginx:alpine)
    // doesn't support it. Same reason for every other plain-index access
    // in this file/policy.js/main.js.
    const headerPart = parts[0];
    const payloadPart = parts[1];
    const signaturePart = parts[2];

    let header, claims;
    try {
        header = JSON.parse(base64UrlDecode(headerPart).toString());
        claims = JSON.parse(base64UrlDecode(payloadPart).toString());
    } catch (e) {
        return null;
    }

    // Algorithm confusion guard: only ever accept the exact algorithm
    // this verifier implements. Never branch on header.alg.
    if (header.alg !== "HS256" || header.typ !== "JWT") {
        return null;
    }

    let signature;
    try {
        signature = base64UrlDecode(signaturePart);
    } catch (e) {
        return null;
    }

    const signingInput = Buffer.from(`${headerPart}.${payloadPart}`);
    const key = await importHmacKey(secret);
    const ok = await crypto.subtle.verify({ name: "HMAC" }, key, signature, signingInput);
    if (!ok) {
        return null;
    }

    const now = Math.floor(Date.now() / 1000);
    if (typeof claims.exp === "number" && claims.exp < now) {
        return null;
    }

    return claims;
}

export default { verify };
