Confirmed: the comparison at line 108 of `fastcallreadonly.rs` uses a plain `String` `!=` operator, which is not constant-time.### Title
Non-constant-time authorization comparison enables timing side-channel attack on `/v3/contracts/fast-call-read/:address/:contract/:function` - (File: stackslib/src/net/api/fastcallreadonly.rs)

### Summary
`RPCFastCallReadOnlyRequestHandler::try_parse_request` compares the attacker-supplied `Authorization` header against the configured secret using the plain `!=` operator on `String`, which is `PartialEq`'s byte-wise, short-circuiting comparison rather than a constant-time comparison. A remote, unauthenticated attacker can exploit the resulting timing differential to recover the secret byte-by-byte via repeated guesses, ultimately bypassing the auth-gate that is supposed to restrict this endpoint (which runs Clarity calls with `LimitedCostTracker::new_free()`, i.e., cost tracking disabled) to trusted callers.

### Finding Description
In `try_parse_request`:
```rust
let Some(auth_header) = preamble.headers.get("authorization") else {
    return Err(Error::Http(401, "Unauthorized".into()));
};
if auth_header != password {
    return Err(Error::Http(401, "Unauthorized".into()));
}
``` [1](#0-0) 

`String`'s `PartialEq` implementation (used by `!=`) compares lengths first and then bytes in order, returning as soon as a mismatch is found. This means the time taken to reject a guess depends on how many leading bytes match the true secret — a wrong-prefix guess returns faster than a correct-prefix/wrong-suffix guess. An attacker with no knowledge of `self.auth`'s value can therefore issue a stream of guesses via the `Authorization` header, statistically measure round-trip latency for each guess, and progressively recover the secret one byte at a time, similar to classical timing side-channel attacks on token/password comparisons (CWE-208). No other verification step in the code path — regex-based route matching, content-length checks, or JSON content-type checks — occurs before this comparison, so the timing differential is isolated to the auth check itself and is fully attacker-observable from a remote, unprivileged connection to the RPC port. The comparison is the only guard standing between an anonymous caller and code that executes arbitrary Clarity read functions with cost tracking disabled (`LimitedCostTracker::new_free()`), which is explicitly gated behind this secret for security reasons per the code comments.

### Impact Explanation
If the timing channel is practically exploitable, a remote unauthenticated attacker can eventually recover the exact configured `auth` secret and gain full access to the "fast" call-read-only endpoint that runs without normal cost-based resource limits, defeating the intended access control. This matches the Critical category of "auth bypass" on a gated endpoint. The attack is fully repeatable since the endpoint can be queried an unbounded number of times.

### Likelihood Explanation
Exploitability requires the operator to have configured a non-empty `auth` secret for this endpoint (otherwise the endpoint returns 400 immediately, see `let Some(password) = &self.auth else`), and requires the attacker to be able to send many requests and perform statistical timing analysis over the network, which is subject to jitter/noise from TLS, TCP stack, OS scheduling, and load-balancing. Network-level timing attacks against short secrets are documented as feasible in practice but require substantial statistical sampling (thousands to millions of requests) to overcome noise, especially since the comparison itself is on the order of nanoseconds compared to network RTT of milliseconds. There is no rate limiting on this endpoint's auth check visible in this code path, so the attacker cost is bounded by sampling requirements rather than by the node.

### Recommendation
Replace the plain `!=` comparison with a constant-time comparison, e.g. using `subtle::ConstantTimeEq` or a manual XOR-accumulate compare over the raw bytes of `auth_header` and `password`, ensuring the comparison time is independent of the position of the first mismatched byte and of overall content (compare fixed-length hashes, e.g. HMAC, if practical). Apply the same fix consistently to the other endpoints (`blockreplay.rs`, `blocksimulate.rs`, `postblock_proposal.rs`, `postblock_v3.rs`, `txsimulate.rs`) that use the identical `!= password` pattern to prevent the same class of leak everywhere the pattern is used.

### Proof of Concept
Add a test in `stackslib::net::api::fastcallreadonly` that:
1. Constructs `RPCFastCallReadOnlyRequestHandler` with `auth = Some("s3cr3t-token-of-len-32-xxxxxxxx".to_string())`.
2. Builds a `HttpRequestPreamble` template with a valid path/captures/body so only the `authorization` header varies.
3. For many iterations, calls `try_parse_request` with (a) a guess sharing 0 correct leading bytes with the secret, and (b) a guess sharing N-1 correct leading bytes (only last byte wrong), measuring wall-clock time (`std::time::Instant`) around each call, repeated thousands of times to average out noise.
4. Asserts that the mean/median time for case (a) and case (b) are statistically indistinguishable (e.g., difference below a small threshold relative to standard deviation); demonstrate that, in the current code, case (b) is measurably slower because `String::eq` compares more bytes before returning `false`, whereas a correct fix (e.g. `subtle::ConstantTimeEq`) collapses that difference.

### Citations

**File:** stackslib/src/net/api/fastcallreadonly.rs (L105-110)
```rust
        let Some(auth_header) = preamble.headers.get("authorization") else {
            return Err(Error::Http(401, "Unauthorized".into()));
        };
        if auth_header != password {
            return Err(Error::Http(401, "Unauthorized".into()));
        }
```
