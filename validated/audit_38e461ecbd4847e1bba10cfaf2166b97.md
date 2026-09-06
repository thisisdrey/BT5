### Title
Non-constant-time comparison of the RPC `authorization` password enables timing side-channel token recovery - (File: `stackslib/src/net/api/postblock_proposal.rs`, `blockreplay.rs`, `blocksimulate.rs`, `txsimulate.rs`, `postblock_v3.rs`, `fastcallreadonly.rs`)

### Summary
Several `stackslib/src/net/api/*` HTTP request handlers authenticate privileged endpoints (`/v3/block_proposal`, `/v3/blocks/replay/*`, `/v3/blocks/simulate/*`, `/v3/transactions/simulate`, `/v3/blocks/upload` broadcast, and the fast-call-readonly endpoint) by comparing the client-supplied `authorization` header against the node's configured secret using the standard Rust `!=`/`==` `String` comparison, which short-circuits on the first mismatching byte. This is the same bug class as the reported GHSA-vg5x-6q66-rvgx (Barzahlen `Webhook::verify` using a non-constant-time compare for an HMAC/signature check): an attacker who can send many requests and measure response latency can incrementally recover the shared secret (`connection_options.auth_token`) byte-by-byte.

### Finding Description
In `RPCBlockProposalRequestHandler::try_parse_request`: [1](#0-0) 
the code does:
```
let Some(auth_header) = preamble.headers.get("authorization") else { ... };
if auth_header != password { return Err(...401...); }
```
The identical pattern appears in:
- `RPCNakamotoBlockReplayRequestHandler::try_parse_request` [2](#0-1) 
- `RPCNakamotoBlockSimulateRequestHandler::try_parse_request` [3](#0-2) 
- `RPCTransactionSimulateRequestHandler::try_parse_request` [4](#0-3) 
- `RPCPostBlockRequestHandler::try_parse_request` (broadcast auth path) [5](#0-4) 
- `fastcallreadonly.rs` (same 2-match pattern).

`String`/`&str` equality in Rust (`PartialEq`) is implemented via a byte-length check followed by `memcmp`-style comparison, which is *not* constant-time and returns as soon as a mismatch is found. Because the compared value (`password`) is the node operator's secret `auth_token`, this is directly analogous to the Barzahlen `Webhook::verify` flaw: an equality check that should protect a secret is implemented with a data-dependent-time comparison, breaking the "constant-time authenticated-vs-forged" property that a security-relevant compare must uphold.

### Impact Explanation
An unauthenticated remote attacker can repeatedly send HTTP requests to these endpoints with candidate `authorization` header values and use timing measurements to determine how many leading bytes match the true secret, incrementally reconstructing the full `auth_token`. Once obtained, the attacker gains full authenticated access to sensitive/privileged RPC surfaces (`/v3/block_proposal`, block replay/simulate, tx simulate, and authenticated block broadcast), i.e., an auth bypass / unauthorized access to privileged node functionality — matching the "auth bypass" criterion for High/Critical impact in this analysis's rules.

### Likelihood Explanation
Exploitability depends on how measurable the timing signal is over a network path (jitter, TLS/TCP stack noise), and typically requires many repeated requests (statistical timing analysis), similar to the original Barzahlen finding's classification as Medium severity. It requires no authentication, no privileged position, and no data beyond normal HTTP access to the RPC port, so it is remotely reachable by any unprivileged party who can reach the node's RPC interface and knows/guesses that these endpoints exist.

### Recommendation
Replace the direct `!=`/`==` string comparisons of `auth_header` against `password` with a constant-time comparison (e.g., using `subtle::ConstantTimeEq` on the byte representations, or first hashing both sides with a keyed MAC and comparing those in constant time) in all affected handlers: `postblock_proposal.rs`, `blockreplay.rs`, `blocksimulate.rs`, `txsimulate.rs`, `postblock_v3.rs`, and `fastcallreadonly.rs`. Centralizing this check into a single shared helper in `httpcore.rs` would prevent recurrence.

### Proof of Concept
1. Configure a stacks-node with `connection_options.auth_token = "<secret>"`.
2. From a remote client, send repeated POST requests to `/v3/block_proposal` with an `authorization` header value that varies one byte at a time (e.g., brute-forcing byte 0 first, then byte 1, etc.), holding the rest fixed.
3. Measure round-trip time for the 401 rejection at `stackslib/src/net/api/postblock_proposal.rs:1142`. Because the comparison short-circuits at the first mismatched byte, correct-byte-prefix guesses will (on average, over many samples) take measurably longer to reject than incorrect first-byte guesses, since more bytes must be compared before the mismatch is found.
4. Statistically distinguish the timing distributions per candidate byte to recover the token incrementally, repeating until the full `auth_token` is reconstructed.

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

**File:** stackslib/src/net/api/blocksimulate.rs (L156-161)
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
