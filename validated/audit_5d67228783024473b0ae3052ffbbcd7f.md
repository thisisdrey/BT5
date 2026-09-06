### Title
Non-constant-time authorization token comparison enables timing side-channel token recovery - (File: `stackslib/src/net/api/postblock_proposal.rs`, `postblock_v3.rs`, `blockreplay.rs`, `blocksimulate.rs`, `fastcallreadonly.rs`, `txsimulate.rs`)

### Summary
Every privileged RPC handler in `stackslib/src/net/api/` that guards an endpoint with the node's `auth_token` compares the client-supplied `Authorization` header against the configured secret using Rust's native string inequality operator (`auth_header != password`), which is a plain byte comparison with no constant-time guarantee. This is the same bug class as the Liferay CVE-2025-43754 report (CWE-208, Observable Timing Discrepancy): an unauthenticated remote attacker can use processing-time differences to incrementally recover a secret it does not know, one byte at a time, instead of a valid/invalid username. Here the recoverable secret is the shared `auth_token`, which gates several high-value write/compute endpoints.

### Finding Description
The following handlers all perform the identical unsafe pattern: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) 

In each case, `auth_header != password` invokes `String`'s `PartialEq`, which is implemented as a byte-slice comparison. Standard byte-slice equality implementations compare bytes sequentially and can return as soon as a mismatch is found (this is an implementation detail of the underlying memcmp, not a documented constant-time contract). Because the equality check is on attacker-controlled input compared against a secret held only by the server, an attacker measuring round-trip response latency across many requests can infer how many leading bytes of a guessed token match the real `auth_token`, and incrementally brute-force the full secret using a byte-at-a-time timing oracle — exactly analogous to how the Liferay report used server processing time to distinguish "exists" vs "does not exist" instead of an outright correct/incorrect signal.

A grep across the whole net/api tree confirms there is no constant-time comparison utility (`subtle`, `ConstantTimeEq`, or a manual constant-time compare) used anywhere for these checks — every one of the six handlers uses the same fragile `!=` pattern.

### Impact Explanation
The `auth_token` gates: `/v3/block_proposal` (block proposal submission normally reserved for the configured signer), `/v2/blocks?broadcast=1` (authenticated block broadcast) via `postblock_v3.rs`, `/v3/blocks/replay`, `/v3/blocks/simulate`, `/v3/contracts/fast-call-read/...`, and `/v3/transactions/simulate`. An attacker who recovers this token via the timing oracle gains the ability to submit forged block proposals, force authenticated block broadcasts, or invoke compute-heavy read/simulate endpoints without authorization — an unauthenticated-to-authenticated privilege escalation on remote, unprivileged network-facing RPC surface. This aligns with the "auth bypass" impact category described as Critical.

### Likelihood Explanation
Exploitation requires only unauthenticated network access to the node's RPC/API port and the ability to send many timed HTTP requests — no node secret, private key, or admin role is needed beforehand, matching the report's remote, unprivileged threat model. Real-world exploitability depends on the token length/entropy and network jitter (statistical averaging over repeated requests is typically required), which is consistent with the Medium severity of the CVE this pattern is analogous to.

### Recommendation
Replace all `auth_header != password` checks in the listed handlers with a constant-time comparison (e.g. via the `subtle` crate's `ConstantTimeEq`, or a manual XOR-accumulate compare over fixed-length byte buffers) so that comparison time is independent of how many bytes match the secret.

### Proof of Concept
1. Configure `connection_options.auth_token` on a target node.
2. Send repeated `POST /v3/block_proposal` (or any of the other five endpoints) requests with varying `Authorization` header guesses, incrementing one byte at a time.
3. Measure response latency for the 401 rejection path in `try_parse_request`; statistically average many samples per candidate byte to filter noise.
4. Bytes that produce a systematically larger mean processing time before the 401 is returned indicate a longer common prefix match with the true token, allowing incremental reconstruction of `auth_token` without ever knowing it in advance.

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
