### Title
Non-Constant-Time Comparison of the RPC `authorization` Token Enables Remote Timing-Based Auth Bypass - ([File: stackslib/src/net/api/postblock_proposal.rs])

### Summary
Every privileged Stacks node RPC endpoint that gates access behind the shared `connection_options.auth_token` secret (`/v3/block_proposal`, `/v3/blocks/replay/*`, `/v3/blocks/simulate/*`, `/v3/transactions/simulate`, `/v2/fast-call-read/*`, and the `broadcast=1` path of `/v3/blocks`) validates the client-supplied `authorization` header with a plain `!=` comparison against the configured password string, rather than a constant-time comparison. This reproduces the same observable-discrepancy bug class as CVE-2016-0762 (Tomcat Realm timing attack): the amount of work the comparison performs — and hence its response latency — depends on how many leading bytes of the guess match the real secret, giving a remote, unauthenticated attacker a byte-by-byte oracle to recover the secret without ever needing to know it in advance.

### Finding Description
Each of these handlers implements `try_parse_request` with the identical pattern: [1](#0-0) 

```
let Some(password) = &self.auth else { ... };
let Some(auth_header) = preamble.headers.get("authorization") else { ... };
if auth_header != password {
    return Err(Error::Http(401, "Unauthorized".into()));
}
```

The same pattern (client-supplied `String` compared to the stored `String` secret with `!=`) recurs verbatim in: [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) 

`String`/`&str` equality in Rust ultimately reduces to a byte-slice comparison that returns as soon as a mismatching byte is found (it is explicitly *not* specified or implemented as constant-time). This means the elapsed wall-clock time to reject a guess is proportional to the length of the correct prefix the attacker supplied. By repeatedly submitting HTTP requests with `authorization` headers that vary one byte at a time and measuring server response latency (or, more robustly, using multiple samples/median-based timing statistics to filter network jitter — a well-established technique for this exact bug class), a remote, completely unauthenticated attacker can incrementally recover the entire `auth_token` byte-by-byte, exactly as CVE-2016-0762 allowed recovery of valid usernames via processing-time discrepancies.

This breaks the fundamental "authenticated header equals stored secret" equality invariant the report's rule set calls out directly ("an equality (authenticated vs stored...)"), because the *comparison mechanism itself*, not just its boolean result, leaks information about the secret.

### Impact Explanation
Once the `auth_token` is recovered, the attacker gains full authentication bypass to every endpoint gated by it:
- `/v3/block_proposal` — submit arbitrary block proposals, forcing the node to perform full, CPU-intensive block validation on attacker-supplied data (compute DoS / resource exhaustion on an endpoint that is supposed to be restricted to the paired signer).
- `/v3/blocks` with `broadcast=1` — bypass the write-gate intended to restrict who may trigger block broadcast through this node.
- `/v3/blocks/replay/*`, `/v3/blocks/simulate/*`, `/v3/transactions/simulate`, `/v2/fast-call-read/*` — unauthorized access to endpoints meant to be restricted to the trusted miner/signer pairing, several of which perform non-trivial Clarity execution.

This matches the report's "Critical: … auth bypass" and "High: bounded compute DoS on a read endpoint" impact classes, since recovering the shared secret converts several intentionally-restricted, compute-heavy endpoints into open, unauthenticated ones.

### Likelihood Explanation
The attack requires no privileged access, no node secret, and no cooperation from another party — it only needs network reachability to the node's RPC port, matching the report's requirement of a remote, unprivileged path. Practical exploitation of such timing oracles over a network is noisier than a local timing attack, but is a documented and repeatedly demonstrated technique (this is precisely the class of vulnerability the referenced CVE-2016-0762 patch addressed), and the vulnerable comparison is present in every one of the RPC-auth-gated endpoints listed above, not a single isolated code path, increasing the number of measurable oracles available to an attacker and thus the practical feasibility of statistical timing extraction.

### Recommendation
Replace the `auth_header != password` string comparisons in `postblock_proposal.rs`, `blockreplay.rs`, `blocksimulate.rs`, `fastcallreadonly.rs`, `txsimulate.rs`, and `postblock_v3.rs` with a constant-time comparison (e.g., `subtle::ConstantTimeEq`, or a manual comparison that always processes the full length of both operands and only returns the final aggregated result), consistently applied wherever `self.auth`/`connection_options.auth_token` is checked against the request's `authorization` header.

### Proof of Concept
1. Configure a node with `connection_options.auth_token = "<secret>"`.
2. From a remote client, repeatedly send POST requests to `/v3/block_proposal` (or any of the other gated endpoints) with an `authorization` header consisting of a candidate prefix followed by random bytes, one byte position at a time.
3. Measure round-trip latency for each candidate byte value at each position, using enough samples per candidate to average out network jitter.
4. The candidate byte value that consistently yields marginally higher processing time (correct-prefix comparisons proceed one byte further before diverging) is the correct next byte of the token; repeat until the full secret is recovered.
5. Use the recovered `auth_token` to submit unauthenticated requests to the block-proposal/broadcast endpoints, demonstrating bypass of the intended access restriction.

### Citations

**File:** stackslib/src/net/api/postblock_proposal.rs (L1136-1144)
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

**File:** stackslib/src/net/api/blockreplay.rs (L578-583)
```rust
        let Some(auth_header) = preamble.headers.get("authorization") else {
            return Err(Error::Http(401, "Unauthorized".into()));
        };
        if auth_header != password {
            return Err(Error::Http(401, "Unauthorized".into()));
        }
```

**File:** stackslib/src/net/api/blocksimulate.rs (L156-161)
```rust
        let Some(auth_header) = preamble.headers.get("authorization") else {
            return Err(Error::Http(401, "Unauthorized".into()));
        };
        if auth_header != password {
            return Err(Error::Http(401, "Unauthorized".into()));
        }
```

**File:** stackslib/src/net/api/fastcallreadonly.rs (L105-110)
```rust
        let Some(auth_header) = preamble.headers.get("authorization") else {
            return Err(Error::Http(401, "Unauthorized".into()));
        };
        if auth_header != password {
            return Err(Error::Http(401, "Unauthorized".into()));
        }
```

**File:** stackslib/src/net/api/txsimulate.rs (L355-360)
```rust
        let Some(auth_header) = preamble.headers.get("authorization") else {
            return Err(Error::Http(401, "Unauthorized".into()));
        };
        if auth_header != password {
            return Err(Error::Http(401, "Unauthorized".into()));
        }
```

**File:** stackslib/src/net/api/postblock_v3.rs (L104-111)
```rust
        if let Some(password) = &self.auth {
            if let Some(auth_header) = preamble.headers.get("authorization") {
                if auth_header != password {
                    return Err(Error::Http(401, "Unauthorized".into()));
                }
                authenticated = true;
            }
        }
```
