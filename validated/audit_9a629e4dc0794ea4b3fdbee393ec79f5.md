I have enough evidence to finalize the analog. The bearer-token comparison across six RPC handlers in `stackslib/src/net/api/*.rs` uses plain Rust `String`/`&str` `!=`/`==` comparison, which is not constant-time and leaks timing information proportional to the length of the matching prefix — directly analogous to Radicale's htpasswd timing-oracle bug.

### Title
Non-constant-time auth-token comparison enables timing-oracle brute-force of privileged RPC endpoints - (File: `stackslib/src/net/api/postblock_v3.rs`)

### Summary
Several privileged Stacks node RPC endpoints (`/v3/blocks/replay`, `/v3/blocks/simulate`, `/v3/transactions/simulate`, `/v3/block_proposal`, the `broadcast=1` path of `/v3/blocks/upload` in `postblock_v3.rs`, and the read-only fast-call endpoint) gate access with a single shared secret (`connection_options.auth_token`). All of these handlers compare the client-supplied `authorization` header against the configured secret using plain Rust string inequality (`auth_header != password`), which is a byte-wise comparison that returns as soon as a mismatching byte is found. This is the same bug class as CVE-2017-8342 (Radicale htpasswd): a remote, unauthenticated attacker who can measure response timing can recover the secret byte-by-byte instead of needing to brute-force the whole token at once.

### Finding Description
Each of these `try_parse_request` implementations performs the identical pattern: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

`auth_header != password` compares two `String`s via Rust's derived slice/byte comparison, which is a standard (non-constant-time) `memcmp`-like operation: it stops scanning at the first byte that differs. Because the shared secret (`auth_token`/`auth_password`) is a single string compared in full on every request, and the endpoints are reachable pre-authentication over the network with no rate limiting, an attacker can:
1. Send many requests, each guessing one additional correct byte of the token/prefix.
2. Measure round-trip latency; requests whose prefix matches take marginally longer (more bytes compared / same-length branch predictor and cache behavior) than requests that mismatch at byte 0.
3. Iteratively recover the full token, byte by byte, reducing the brute-force search space from `O(charset^length)` to `O(charset*length)`.

This exactly mirrors the underlying weakness described in the Radicale advisory: htpasswd verification via ordinary string equality instead of a constant-time comparison, enabling both a timing oracle and, once combined with the reduced effective keyspace, straightforward brute-forcing.

### Impact Explanation
The `auth_token` gates write/privileged actions: unauthenticated broadcast of blocks via `postblock_v3.rs` (`broadcast=1`), the block-proposal validation endpoint, and simulation/read-only endpoints that consume node CPU resources. Recovering this token via a timing side channel would let a remote, unprivileged attacker authenticate as a trusted subsystem (e.g., a signer or miner) and abuse these endpoints — an auth-bypass condition. This maps to the "High" bucket in the given impact rubric (auth bypass on a privileged endpoint), though it is a probabilistic/side-channel path rather than a deterministic bypass.

### Likelihood Explanation
Exploitability depends on how measurable the timing signal is over the network (jitter, TLS, HTTP framing) and the token length/charset, so it is not a trivial one-shot exploit; averaging many samples per byte is typically required to filter noise. Still, it requires no privileges, no valid credentials, and is reachable directly at the HTTP layer on every listed endpoint, making it a realistic — if statistically noisy — remote likelihood.

### Recommendation
Replace all `auth_header != password` (and equivalent `==`) checks in `blockreplay.rs`, `blocksimulate.rs`, `txsimulate.rs`, `postblock_v3.rs`, `postblock_proposal.rs`, and `fastcallreadonly.rs` with a constant-time comparison (e.g., `subtle::ConstantTimeEq`, or compare fixed-length HMACs of the token instead of the raw token) so that comparison time is independent of where a mismatch occurs.

### Proof of Concept
1. Configure a node with `connection_options.auth_token = "<secret>"`.
2. From a remote client, repeatedly send POST/GET requests to any of the affected endpoints (e.g., `/v3/transactions/simulate`) with an `authorization` header whose first `k` bytes match the real token, varying the `(k+1)`-th byte over the full character set.
3. Measure average response latency per candidate byte over many trials; the candidate producing the latency consistent with matching one more byte than the rest indicates the correct next byte.
4. Repeat byte-by-byte to reconstruct the full `auth_token`, then use it to submit unauthenticated (from the attacker's true privilege level) broadcast/proposal requests.

### Citations

**File:** stackslib/src/net/api/blockreplay.rs (L574-583)
```rust
        // If no authorization is set, then the block replay endpoint is not enabled
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

**File:** stackslib/src/net/api/blocksimulate.rs (L152-161)
```rust
        // If no authorization is set, then the block replay endpoint is not enabled
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

**File:** stackslib/src/net/api/txsimulate.rs (L351-360)
```rust
        // If no authorization is set, then the transaction simulation endpoint is not enabled
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

**File:** stackslib/src/net/api/postblock_v3.rs (L99-111)
```rust
        // if broadcast=1 is set, then the requester must be authenticated
        let mut broadcast = false;
        let mut authenticated = false;

        // look for authorization header
        if let Some(password) = &self.auth {
            if let Some(auth_header) = preamble.headers.get("authorization") {
                if auth_header != password {
                    return Err(Error::Http(401, "Unauthorized".into()));
                }
                authenticated = true;
            }
        }
```
