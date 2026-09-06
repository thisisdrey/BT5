### Title
Non-constant-time comparison of the RPC `auth_token` secret in Stacks RPC endpoints allows timing-based secret recovery - ([File: stackslib/src/net/api/postblock_proposal.rs])

### Summary
Several privileged Stacks RPC endpoints authenticate inbound requests by comparing the client-supplied `Authorization` header directly against the node's configured `auth_token` secret using Rust's built-in `!=` operator on `String`/`&str`. This is a byte-wise comparison that returns as soon as a mismatching byte is found, which is the exact bug class described in the reference advisory (Jenkins CVE-2020-2101): a non-constant-time comparison of a secret used to authenticate an inbound connection, allowing an attacker to recover the secret via statistical/timing analysis.

### Finding Description
The comparison pattern `if auth_header != password { return Err(...401...) }` appears in the `try_parse_request` implementations of multiple RPC handlers: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) 

In each case, the secret `auth_token`/`password` (configured via `[connection_options] auth_token` as documented in `docs/signing.md`) is compared to attacker-controlled input (the HTTP `Authorization` header) using a naive string equality check, rather than a constant-time comparison such as `subtle::ConstantTimeEq` or an HMAC-based check. Rust/LLVM's default `PartialEq` for strings is not guaranteed constant time and commonly compiles to an early-exit `memcmp`-style comparison, making the comparison time dependent on the length of the matching prefix — the classic timing side channel this bug class targets.

This is directly analogous to the reported Jenkins issue, where the connection secret validating an inbound agent connection was compared non-constant-time, enabling recovery of the secret through repeated, timed guesses.

### Impact Explanation
The `auth_token` gates high-value, state-changing/compute endpoints, including:
- `/v3/block_proposal` (`RPCBlockProposalRequestHandler`) — submits block proposals for validation.
- `/v3/blocks/upload` (broadcast=1) / `postblock_v3.rs` — authenticated block broadcast.
- `/v3/blocks/simulate`, `/v3/blocks/replay`, `/v3/transactions/simulate`, `/v3/contracts/fast-call-read` — privileged compute/replay endpoints.

An attacker who recovers the `auth_token` via timing analysis gains unauthorized access to these privileged endpoints, potentially enabling unauthorized writes/interactions with node state or driving expensive replay/simulation operations without permission — meeting the "unauthenticated/unauthorized write to state" and "bounded compute DoS on a read endpoint" impact tiers defined in scope.

### Likelihood Explanation
Exploitation requires no privileges beyond network access to the RPC port and relies purely on statistical timing measurements over many requests (as acknowledged even in the original CVE, this typically needs many samples and can be affected by network jitter), which lowers practical likelihood somewhat, but the code path is remotely reachable and unauthenticated (the check itself is the auth boundary), matching CVSS AC:H/PR:L characteristics from the reference advisory.

### Recommendation
Replace the direct `!=` comparisons of `auth_header` and `password`/`auth_token` in `postblock_proposal.rs`, `blocksimulate.rs`, `blockreplay.rs`, `fastcallreadonly.rs`, `txsimulate.rs`, and `postblock_v3.rs` with a constant-time comparison (e.g., using the `subtle` crate's `ConstantTimeEq`, or comparing SHA-256/HMAC digests of the two values) to eliminate the timing side channel.

### Proof of Concept
1. Configure a node with `[connection_options] auth_token = "<secret>"`.
2. Send repeated POST requests to `/v3/block_proposal` (or any of the other affected endpoints) with `Authorization` headers that are incrementally guessed byte-by-byte.
3. Measure response latency for the 401 rejection path at `auth_header != password` in [1](#0-0) ; a longer-matching prefix causes marginally longer comparison time before divergence, letting an attacker statistically recover the token one byte at a time, analogous to CVE-2020-2101.

### Citations

**File:** stackslib/src/net/api/postblock_proposal.rs (L1139-1144)
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

**File:** stackslib/src/net/api/blockreplay.rs (L578-583)
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
