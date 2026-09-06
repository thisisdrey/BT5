### Title
Non-constant-time authorization header comparison enables timing side-channel to recover the fast-call-read-only secret - (File: stackslib/src/net/api/fastcallreadonly.rs)

### Summary
`RPCFastCallReadOnlyRequestHandler::try_parse_request` authenticates requests to the unbounded-cost `/v3/contracts/fast-call-read/...` endpoint using a plain `auth_header != password` comparison on `String`/`&str` values. This comparison short-circuits on the first mismatching byte, creating a timing side channel that a remote, unauthenticated caller can use to recover the configured secret byte-by-byte.

### Finding Description
At [1](#0-0) , the handler reads the `authorization` header supplied by the remote HTTP client and compares it against the configured `password` using Rust's default `PartialEq` for `String`/`&str`. That implementation compares the underlying byte slices and returns as soon as a differing byte (or differing length) is found, rather than in constant time. Because HTTP response timing is observable over the network, an attacker who does not know `password` can submit guesses with an increasing correct prefix and measure whether the rejection path takes marginally longer (more bytes compared before divergence), incrementally reconstructing `password` one byte at a time. This defeats the sole authentication gate protecting the `fast-call-read` endpoint, which otherwise grants a remote caller access to `LimitedCostTracker::new_free()` (unmetered/unbounded-cost Clarity read-only calls), a capability intentionally gated by this secret per the code's own comment ("If no authorization is set, then... the endpoint is not enabled").

### Impact Explanation
A successful timing attack yields the plaintext `password` used to gate the fast-call-read-only RPC endpoint, directly undermining the endpoint's authentication and matching the "auth bypass" Critical category: an unprivileged remote attacker (who does not hold the secret per the threat model) can escalate to a fully-authenticated, unmetered read-only-call capability against any node that has this endpoint enabled. The attack is repeatable per byte position and requires no privileged role, only network access to the RPC port.

### Likelihood Explanation
Preconditions: the operator must have configured a non-empty `auth` password for the fast-call-read-only handler (endpoint returns 400 if unset), and the attacker must be able to send repeated HTTP POST requests to `/v3/contracts/fast-call-read/...` and measure response latency. Attacker cost is amortized over many requests per candidate byte to average out network/system jitter, but no special privileges, credentials, or race conditions are needed — only remote reachability of the RPC port, which is assumed reachable by the threat model.

### Recommendation
Replace the `!=` comparison with a constant-time comparison, e.g. using the `subtle` crate's `ConstantTimeEq` (`auth_header.as_bytes().ct_eq(password.as_bytes())`) or an equivalent fixed-time byte comparison that does not short-circuit on length or content mismatch, and ensure length is not leaked either (e.g., hash both sides to a fixed-length MAC before comparing, or pad to a fixed length).

### Proof of Concept
Add a net-level test in `stackslib/src/net/api/fastcallreadonly.rs`'s test module that:
1. Constructs `RPCFastCallReadOnlyRequestHandler` with a fixed `auth` secret (e.g., 32 random bytes as a `Bearer` token).
2. For a set of guess headers with increasing correct-prefix length (0, 1, 2, ..., N-1 correct bytes followed by a wrong byte), repeatedly invoke `try_parse_request` (or the underlying comparison directly) and measure wall-clock time over many iterations (e.g., using `std::time::Instant`), discarding outliers.
3. Assert that mean/median latency increases monotonically (or shows statistically significant correlation, e.g., via a simple regression/correlation coefficient) with prefix length under the current `!=` implementation.
4. Replace `!=` with a constant-time comparison (`subtle::ConstantTimeEq`) and re-run the same measurement, asserting the correlation is no longer statistically significant (timing-invariance holds). [2](#0-1)

### Citations

**File:** stackslib/src/net/api/fastcallreadonly.rs (L100-110)
```rust
    ) -> Result<HttpRequestContents, Error> {
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
