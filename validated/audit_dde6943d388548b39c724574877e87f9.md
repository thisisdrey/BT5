### Title
Non-constant-time comparison of the `/v3/block_proposal` bearer credential enables remote timing-based token recovery - (File: stackslib/src/net/api/postblock_proposal.rs)

### Summary
`RPCBlockProposalRequestHandler::try_parse_request` authenticates callers of the `/v3/block_proposal` HTTP endpoint by comparing the `authorization` header byte-for-byte against the configured `auth_token` using Rust's standard `!=` (`PartialEq`) string comparison, which short-circuits on the first mismatching byte. This is a variable-time equality check on a security-critical secret, letting a remote, unauthenticated attacker recover the shared `auth_token`/`auth_password` byte-by-byte via a timing side channel and then submit unauthorized block proposals.

### Finding Description
The handler stores the node-configured secret in `self.auth` (populated from `connection_options.auth_token`, see `stackslib/src/config/mod.rs:3799-3816`) and checks the caller-supplied header against it: [1](#0-0) 

```rust
let Some(password) = &self.auth else {
    return Err(Error::Http(400, "Bad Request.".into()));
};
let Some(auth_header) = preamble.headers.get("authorization") else {
    return Err(Error::Http(401, "Unauthorized".into()));
};
if auth_header != password {
    return Err(Error::Http(401, "Unauthorized".into()));
}
```

`auth_header != password` compares `String`/`&str` values using the derived `PartialEq` for strings, which is implemented as a length check followed by a `memcmp`-style comparison that returns as soon as a differing byte is found (it does not run in constant time). Because this comparison gates access to a `POST` endpoint that is reachable by any remote peer able to reach the node's RPC port (no other authentication layer precedes this check — see `docs/rpc/openapi.yaml:905-916`, which documents `/v3/block_proposal` as requiring only "a basic Authorization header"), an attacker can:

1. Send repeated requests with candidate authorization strings that share an increasing common prefix with the true secret.
2. Measure response latency; a longer comparison time (one extra byte matched before divergence) indicates the guessed prefix is correct.
3. Iterate byte-by-byte (or in larger batches) until the full secret is reconstructed.

This breaks the intended equality guarantee between "authenticated caller" and "party in possession of the auth secret" — the attacker never needs to know the token to eventually derive it, defeating the entire authentication scheme for this endpoint. This is the direct code analog of the reported bug class: a security-relevant secret ("credential") is not adequately protected, in this case not by encryption at rest (irrelevant here) but by an insufficiently protected/leaking comparison, letting a remote low-privilege actor extract it.

Because this same `auth_token` value is also documented as being used to authenticate `/v2/blocks?broadcast=1` (`stackslib/src/config/mod.rs:3799-3816`), recovering it would extend the attacker's authorized-write capability beyond block-proposal validation.

### Impact Explanation
An attacker who recovers the `auth_token` gains the ability to submit block proposals to the node's `/v3/block_proposal` endpoint as if they were the trusted `stacks-signer`, and (per the documented reuse of the token) potentially broadcast blocks via `/v2/blocks?broadcast=1`. This is an authentication-bypass primitive against a privileged write endpoint reachable by any remote, unauthenticated party — matching the "auth bypass" / "unauthorized write to state" impact tier in scope. The severity is bounded by the practicality of network timing attacks (jitter, network noise), which typically require many samples, but the class of vulnerability (observable timing discrepancy on a security-sensitive comparison, CWE-208, closely related to CWE-522's "insufficiently protected credential") is a legitimate, exploitable analog of the referenced report.

### Likelihood Explanation
The endpoint is intentionally exposed to the network (that is its purpose: to let a configured `stacks-signer` submit proposals over the network), so no prior access or privilege is required to reach the vulnerable comparison — only the ability to send repeated HTTP POST requests and measure timing. Exploitation requires statistical timing analysis (many requests per byte position to average out noise), which is a known but non-trivial technique; likelihood is therefore moderate rather than trivial, but the vulnerable code path is unconditionally reachable by any remote peer.

### Recommendation
Replace the direct `!=` comparison with a constant-time comparison, e.g. using `subtle::ConstantTimeEq` or `ring::constant_time::verify_slices_are_equal`, and compare fixed-size hashes (e.g. compare `Sha256(auth_header)` to a precomputed `Sha256(password)`) rather than raw variable-length strings to also normalize timing across length differences. Apply the same fix to any other location performing raw comparisons of `auth_token`/`auth_header` for RPC authentication (e.g., `/v2/blocks?broadcast=1`, if implemented via a similar comparison — not fully confirmed in this review; the `stackslib/src/net/api/mod.rs` and `stackslib/src/net/httpcore.rs` matches for `auth_token` were not read in full and should be audited for the same pattern).

### Proof of Concept
1. Configure a node with `connection_options.auth_token = "supersecrettoken1234567890"`.
2. As a remote client with no knowledge of the token, send many `POST /v3/block_proposal` requests, each with a different `authorization` header value, varying one byte position at a time (e.g., trying all 256 values for byte 0 while keeping the rest constant/padding).
3. Measure response latency for each guess at high sample count to filter out noise; the guess causing a statistically longer time-to-401 indicates a correct byte, because `auth_header != password` (implemented via short-circuiting slice/string comparison) compares one additional byte before diverging.
4. Repeat for each subsequent byte position until the full token is reconstructed, then use it to submit an authenticated block proposal at `stackslib/src/net/api/postblock_proposal.rs:1142` without ever having been given the token by the operator.

### Citations

**File:** stackslib/src/net/api/postblock_proposal.rs (L1135-1144)
```rust
        // If no authorization is set, then the block proposal endpoint is not enabled
        let Some(password) = &self.auth else {
            return Err(Error::Http(400, "Bad Request.".into()));
        };
        let Some(auth_header) = preamble.headers.get("authorization") else {
            return Err(Error::Http(401, "Unauthorized".into()));
        };
        if auth_header != password {
            return Err(Error::Http(401, "Unauthorized".into()));
        }
```
