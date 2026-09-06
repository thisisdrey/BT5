### Title
Non-constant-time Authorization header comparison enables password recovery via timing side-channel - (File: stackslib/src/net/api/fastcallreadonly.rs)

### Summary
`RPCFastCallReadOnlyRequestHandler::try_parse_request` authenticates the `fast-call-read` RPC endpoint by comparing the client-supplied `Authorization` header against the configured secret using Rust's built-in `!=` operator on `&str`, which performs a byte-wise, early-exit comparison. This is a classic timing side-channel that leaks how many leading bytes of a guess match the real secret.

### Finding Description
The equality/fault claimed is real: `auth_header != password` at [1](#0-0)  uses the standard string `PartialEq` implementation, which compares byte-by-byte and returns as soon as a mismatch is found. This means the time to reject an incorrect `Authorization` header is *not* independent of how many leading bytes match the real password — a header matching the first `k` bytes of the secret takes measurably longer to reject than one that differs at byte 0, because more bytes must be compared before the mismatch is detected.

The check happens before any body parsing, contract resolution, or read-only execution, so it gates directly on the header value alone at [1](#0-0) . An attacker who does not know `self.auth`'s value can repeatedly send `POST /v3/contracts/fast-call-read/.../.../...` requests with `Authorization` headers that vary one byte at a time and observe response latency to statistically infer which byte-prefix guess causes the comparison to run longer, incrementally reconstructing the password one byte (or one character range) at a time. There is no rate limiting or constant-time comparison guard (e.g., `subtle::ConstantTimeEq`) present in this code path to prevent this.

### Impact Explanation
If successfully exploited, this leaks the configured RPC secret to a remote, unprivileged attacker, granting them unauthenticated access to the `fast-call-read` endpoint. That endpoint executes contract calls using `LimitedCostTracker::new_free()`, i.e., an unmetered execution path bounded only by wall-clock time rather than Clarity cost limits, which is itself flagged as a compute-DoS vector. Successful password recovery therefore escalates from an auth bypass to a bounded-but-uncapped-by-cost compute DoS on a node's Clarity execution engine — matching the Critical "auth bypass" and High "bounded compute DoS on a read endpoint" categories.

### Likelihood Explanation
The endpoint is reachable via a node's RPC/HTTP port with no privileged role required — the attacker only needs network connectivity, matching the "unprivileged remote attacker" threat model. Exploitation cost is high in practice: HTTP request/response processing at this layer (routing, regex matching, header parsing, JSON/database work occurring in other paths) introduces substantial noise that likely dwarfs the nanosecond-to-microsecond differences from a short-circuiting string comparison, and the fast-call-read handler additionally does a full `try_parse_request`/`try_handle_request` cycle with database and chain-tip lookups on any body that gets past auth — meaning many statistical samples per byte would be required over a real network, and the endpoint is only reachable at all if `auth` is configured (`self.auth` must be `Some`). Still, the underlying code pattern is a genuine, remotely-triggerable defect requiring no privileged role or knowledge of the secret to attempt.

### Recommendation
Replace the `!=` comparison with a constant-time comparison, e.g. using the `subtle` crate's `ConstantTimeEq` on the byte representations of `auth_header` and `password`, or a fixed-time byte-wise XOR-accumulate comparison, ensuring the comparison always processes the full length of both strings before returning a result regardless of where a mismatch occurs. Apply the same fix to the other endpoints found reusing the same `auth_header != password` pattern (`postblock_proposal.rs`, `postblock_v3.rs`, `blockreplay.rs`, `blocksimulate.rs`, `txsimulate.rs`).

### Proof of Concept
In `stackslib/src/net/api/fastcallreadonly.rs` (or a new test module), construct an `RPCFastCallReadOnlyRequestHandler` with `auth = Some("supersecretpassword".to_string())`. For a set of candidate headers `h_0..h_N` where `h_i` matches the first `i` bytes of the real password before diverging, call `try_parse_request` (or directly benchmark the `auth_header != password` expression) many times per candidate and record wall-clock duration with `std::time::Instant`. Assert that mean comparison time is statistically correlated with `i` (e.g., using a t-test or simple threshold comparison between `h_0` and `h_{N-1}` timings), demonstrating the leak at the exact site [2](#0-1) .

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
