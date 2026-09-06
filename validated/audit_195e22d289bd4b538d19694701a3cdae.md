I have sufficient evidence now. All five HTTP RPC handlers in `stackslib/src/net/api/` (`postblock_proposal.rs`, `blockreplay.rs`, `fastcallreadonly.rs`, `postblock_v3.rs`, `blocksimulate.rs`, `txsimulate.rs`) authenticate requests by directly comparing the client-supplied `authorization` header against the configured secret using Rust's `!=`/`==` operator on `String`/`str`, which is a short-circuiting, non-constant-time comparison — the exact bug class described in the Keycloak JWS HMAC advisory (CVE-2017-2585).

### Title
Non-constant-time comparison of the `authorization` header enables timing attacks against the node's RPC `auth_token` - (File: `stackslib/src/net/api/postblock_proposal.rs`)

### Summary
Several privileged Stacks RPC endpoints (`/v3/block_proposal`, `/v3/blocks/replay/*`, `/v3/contracts/fast-call-read/*`, `/v3/blocks/upload/` with `broadcast=1`, and the read-only call-simulation endpoints) protect themselves with a single shared-secret string (`connection_options.auth_token`), compared against the client's `authorization` header using Rust's built-in string equality operator.

### Finding Description
In `try_parse_request` for `RPCBlockProposalRequestHandler`, the check is: [1](#0-0) 
```
let Some(auth_header) = preamble.headers.get("authorization") else { ... };
if auth_header != password {
    return Err(Error::Http(401, "Unauthorized".into()));
}
```
The same pattern (`auth_header != password` / `auth_header ==` ) recurs verbatim in: [2](#0-1) [3](#0-2) [4](#0-3) 
and (per grep) in `blocksimulate.rs` and `txsimulate.rs`.

Rust's `PartialEq` for `String`/`&str` ultimately delegates to a byte-slice comparison that first checks length, then compares bytes with early exit on the first mismatch (`slice::eq` is not constant-time; it is optimized by the compiler/LLVM to bail out as soon as a differing byte is found). This means the time taken by `auth_header != password` leaks information about how many leading bytes of the guess match the real secret — the same class of defect as the Keycloak HMAC verification bug in the advisory (CVE-2017-2585): a byte-wise, early-exit comparison of a security-critical secret against attacker-controlled input.

An unauthenticated remote attacker can send crafted `authorization` header values and, given enough measurements (mitigated but not eliminated by network jitter), incrementally recover the correct `auth_token` byte-by-byte, since matching more leading bytes causes marginally more comparison work before the mismatch is detected. Once recovered, the attacker gains write access to `/v3/block_proposal` (submitting block proposals) and `/v2/blocks?broadcast=1`/`/v3/blocks/upload/?broadcast=1` (forcing the node to broadcast an attacker-supplied block), which is the exact secret this token is meant to gate: [5](#0-4) 

### Impact Explanation
If successfully exploited, an attacker recovers the shared `auth_token` and can then submit forged/attacker-controlled block proposals to the miner's validation endpoint and force broadcast of attacker-chosen blocks via the authenticated `broadcast=1` path — an unauthorized write/auth-bypass on node state, matching the "High"/"Critical" bar (auth bypass via a broken secret-comparison equality). However, this requires the attacker to perform a large number of precisely-timed network round-trips against a remote HTTP server, where noise from TLS/HTTP parsing, OS scheduling, and network jitter typically dominates the sub-microsecond timing signal from a short string comparison. This makes practical exploitation over a real network difficult, consistent with the original advisory's "Medium" severity and `AC:H` (high attack complexity) CVSS vector.

### Likelihood Explanation
Low-to-medium. The vulnerable code path is reachable by any unauthenticated remote party with network access to the RPC port (no other credential needed), satisfying the "remote, unprivileged" requirement. However, remote timing-attack exploitation against an HTTP endpoint (as opposed to a local/co-located oracle) is noisy and requires substantial statistical measurement, so real-world exploitation likelihood is lower than for a direct logic flaw.

### Recommendation
Replace the direct `!=`/`==` string comparisons in `postblock_proposal.rs`, `blockreplay.rs`, `fastcallreadonly.rs`, `postblock_v3.rs`, `blocksimulate.rs`, and `txsimulate.rs` with a constant-time comparison (e.g., `subtle::ConstantTimeEq`, or a manual fixed-time byte-wise XOR-accumulate comparison) for the `auth_header` vs. configured `auth_token`/password check.

### Proof of Concept
1. Configure a node with `connection_options.auth_token = "<secret>"`.
2. From a remote host, repeatedly POST to `/v3/block_proposal` (or GET `/v3/blocks/replay/<id>`) with an `authorization` header guess, measuring response latency for the 401 rejection path in `try_parse_request` at `stackslib/src/net/api/postblock_proposal.rs:1142` (`if auth_header != password`).
3. Statistically, guesses whose leading bytes match more of the real token take marginally longer to reject (comparison proceeds further before the mismatched byte causes early exit), allowing incremental byte-by-byte recovery of `auth_token`.

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

**File:** stackslib/src/net/api/postblock_v3.rs (L103-111)
```rust
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

**File:** stackslib/src/config/mod.rs (L3802-3816)
```rust
    /// HTTP auth password to use when communicating with stacks-signer binary.
    ///
    /// This token is used in the `Authorization` header for certain requests.
    /// Primarily, it secures the communication channel between this node and a
    /// connected `stacks-signer` instance.
    ///
    /// It is also used to authenticate requests to `/v2/blocks?broadcast=1`.
    /// ---
    /// @default: `None` (authentication disabled for relevant endpoints)
    /// @notes:
    ///   - This field **must** be configured if the node needs to receive
    ///     block proposals from a configured `stacks-signer` [[events_observer]]
    ///     via the `/v3/block_proposal` endpoint.
    ///   - The value must match the token configured on the signer.
    pub auth_token: Option<String>,
```
