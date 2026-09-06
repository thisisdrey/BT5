### Title
Non-constant-time comparison of RPC bearer tokens enables timing-based token recovery - (File: `stackslib/src/net/api/fastcallreadonly.rs`, `stackslib/src/net/api/blockreplay.rs`, `stackslib/src/net/api/blocksimulate.rs`, `stackslib/src/net/api/postblock_v3.rs`, `stackslib/src/net/api/txsimulate.rs`, `stackslib/src/net/api/postblock_proposal.rs`)

### Summary
Several privileged HTTP RPC endpoints in `stackslib/src/net/api/` authenticate the caller by comparing the `authorization` header verbatim against a configured secret token using Rust's default `String`/`&str` `PartialEq`, e.g. `if auth_header != password { ... }`. This is the same bug class as GHSA-8ch4-58qp-g3mp / CVE-2021-33880 (websockets `basic_auth_protocol_factory`): a byte-by-byte, short-circuiting equality check on a secret creates an observable timing side channel that a remote, unauthenticated attacker can exploit to recover the token via repeated measurement.

### Finding Description
The following handlers gate access to sensitive node RPCs with a shared-secret token and compare it directly with `!=`:
- `RPCFastCallReadOnlyRequestHandler::try_parse_request` [1](#0-0) 
- `RPCNakamotoBlockReplayRequestHandler::try_parse_request` [2](#0-1) 
- `RPCNakamotoBlockSimulateRequestHandler::try_parse_request` [3](#0-2) 
- `RPCPostBlockRequestHandler::try_parse_request` (broadcast auth) [4](#0-3) 
- `RPCTransactionSimulateRequestHandler::try_parse_request` [5](#0-4) 

`str`'s `PartialEq` implementation (via `memcmp`/byte iteration) short-circuits on the first mismatching byte, so the comparison time is a function of how many leading bytes of the guessed token match the real secret. This lets a remote attacker measure per-request latency over many attempts to reconstruct the token byte-by-byte — precisely the pattern flagged in the reported advisory (HTTP Basic Auth password compared non-constant-time on the server).

Unlike the upstream advisory (which concerned a `Basic` auth password), these tokens gate high-privilege actions: fast-call read-only VM execution, block replay/simulate, transaction simulation, and authenticated block broadcast. All of these `try_parse_request` implementations execute before any consensus/signature/state loading, and are reachable from any TCP client that can open an HTTP/RPC connection to the node's exposed API port — the check runs unconditionally on every request carrying an `authorization` header, so timing is fully attacker-controllable and repeatable at negligible cost.

### Impact Explanation
Successful recovery of the shared token would let an unauthenticated remote attacker call: `fast-call-read` (arbitrary read-only Clarity function execution under a bespoke, possibly relaxed cost-tracker budget), `/v3/blocks/replay/simulate` (server-side block execution against arbitrary block IDs, resource consuming), `/v3/transactions/simulate` (arbitrary transaction execution against the current tip), and the authenticated `broadcast=1` path of `/v3/blocks/upload/` (forcing block broadcast without going through the normal p2p relay gate). This matches the "bounded compute DoS on a read endpoint" / unauthorized-write-adjacent impact tier for a High-severity analog, since these are auth-gated maintenance/debug RPCs, not the core consensus-critical StackerDB/relay path (which correctly uses cryptographic signature verification, not string comparisons — see `SlotMetadata::verify` in `libstackerdb/src/libstackerdb.rs:181-193`, which is unaffected).

### Likelihood Explanation
Exploitability requires: (1) the operator has configured one of these auth tokens (they are opt-in — omitting `auth` disables the endpoint with `400 Bad Request`), and (2) sufficiently stable network timing to distinguish sub-comparison timing over repeated attempts, which is a well-documented, practical technique for remote HTTP timing side-channels, especially against tokens of realistic length. This is a real but non-trivial remote attack requiring statistical measurement rather than a single request; it is not a trivial one-shot bypass.

### Recommendation
Replace all `auth_header != password` (and equivalent) comparisons in the listed handlers with a constant-time comparison, e.g. using the `subtle` crate's `ConstantTimeEq` (`subtle::ConstantTimeEq::ct_eq`) on the byte representations, or an existing constant-time helper if present elsewhere in `stacks-common`. Apply this uniformly to every handler in `stackslib/src/net/api/` that performs this bearer-token check to avoid reintroducing the same defect elsewhere.

### Proof of Concept
1. Configure a node with `auth = Some("<long-secret-token>")` for the fast-call-read-only endpoint (or any of the affected handlers).
2. From a remote client, send repeated `POST /v3/contracts/fast-call-read/...` requests with the `authorization` header set to guesses that share an increasing number of correct leading bytes with the real token (e.g., binary/byte-search strategy).
3. Measure response latency for the `401 Unauthorized` short-circuit path; guesses whose prefix matches more bytes of the true token exhibit a longer comparison time before the branch triggers.
4. Iterating this measurement per byte/position allows statistically reconstructing the full token without ever needing to observe it directly, mirroring the technique described in CVE-2021-33880.

### Citations

**File:** stackslib/src/net/api/fastcallreadonly.rs (L101-110)
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
