### Title
Non-constant-time comparison of the HTTP `authorization` shared secret enables remote timing side-channel and eventual auth bypass on privileged v3 endpoints - (File: stackslib/src/net/api/postblock_proposal.rs, blockreplay.rs, blocksimulate.rs, fastcallreadonly.rs, postblock_v3.rs, txsimulate.rs)

### Summary
Several `/v3/*` RPC endpoint handlers authenticate the caller by comparing the raw `authorization` HTTP header value against the node's configured `auth_token` using Rust's standard, variable-time `String`/`&str` `PartialEq` (`!=`). This is the exact bug class described in the external report: an equality check on a secret whose comparison time depends on the position of the first differing byte, letting a remote attacker recover the secret via repeated timing measurements, ultimately producing an auth bypass on endpoints that gate block submission/broadcast, block simulation, block replay, and fast/uncosted read-only calls.

### Finding Description
`RPCBlockProposalRequestHandler`, `RPCNakamotoBlockReplayRequestHandler`, `RPCNakamotoBlockSimulateRequestHandler`, `RPCFastCallReadOnlyRequestHandler`, `RPCPostBlockRequestHandler`, and `RPCTransactionSimulateRequestHandler` each store the configured shared secret in `self.auth: Option<String>` and check the request in `try_parse_request` with:

```rust
let Some(auth_header) = preamble.headers.get("authorization") else {
    return Err(Error::Http(401, "Unauthorized".into()));
};
if auth_header != password {
    return Err(Error::Http(401, "Unauthorized".into()));
}
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) 

Rust's `str`/`String` `PartialEq` implementation compares length first and then bytes left-to-right, returning as soon as a mismatch is found (memcmp-style short-circuiting) — the very same class of "not constant time" defect illustrated by the external report's `common_ancestor_distance_of_peers` (which used `leading_zeros`/`bsr` with an early-exit `je` on equality). Here the equivalent early-exit occurs on the comparison of `auth_header` vs. the operator-configured `auth_token`/password: the process time for a wrong guess grows with the number of correctly-guessed leading bytes, exactly the observable signal the Trail of Bits report warns about for ORAM/side-channel-sensitive code.

Unlike the internal cryptographic signature checks used elsewhere in this codebase (e.g., StackerDB chunk signature verification via secp256k1 recovery in `libstackerdb/src/libstackerdb.rs` `SlotMetadata::verify`, and P2P handshake signature verification in `stackslib/src/net/chat.rs::validate_handshake`, both of which are based on public-key cryptography and do not compare a shared secret byte-for-byte), these five/six HTTP handlers implement classic **shared-secret/password authentication** guarding the node's `/v3/*` control endpoints, and the comparison is naive, non-constant-time string equality performed directly on attacker-supplied network input against the secret.

### Impact Explanation
An attacker who does not know the `auth_token` can send repeated unauthenticated requests to any of these endpoints (`/v3/block_proposal`, `/v3/blocks/replay/*`, `/v3/blocks/simulate/*`, `/v3/contracts/fast-call-read/*`, `/v3/blocks/upload/?broadcast=1`, `/v3/transactions/simulate`) with candidate `authorization` header values and use timing measurements to recover the token byte-by-byte. Once the token is recovered, the attacker gains authenticated access to endpoints intended to be restricted to the paired `stacks-signer`/miner, i.e. an **auth bypass**, one of the impact categories explicitly listed as Critical in scope for this analysis. Depending on the endpoint reached, consequences include forcing block broadcast (`broadcast=1` on `postblock_v3.rs`), submitting spoofed block proposals, or driving expensive block/transaction simulation — a resource-consumption vector as well.

### Likelihood Explanation
Exploitability is nontrivial but realistic: byte-at-a-time timing side-channel recovery of shared secrets over HTTP has been demonstrated in numerous real-world settings (statistical timing attacks against string comparisons), especially when the attacker can issue a large number of requests and average out network jitter, or when running from a low-latency vantage point (e.g., same host/LAN). The check happens early in a widely reused code path (`try_parse_request`), is reachable by any unauthenticated remote peer able to reach the node's RPC port, and requires no prior state or privileged access — matching the "remote, unprivileged" and "auth-gate" criteria. It is a genuine, root-caused equality defect (not merely traffic volume), though practical exploitation requires many timed samples to overcome network noise, which somewhat lowers likelihood relative to a trivial fail-open bug.

### Recommendation
Replace all `auth_header != password` string comparisons on these handlers with a constant-time comparison (e.g., using a constant-time byte-compare utility, such as `subtle::ConstantTimeEq`, or by first hashing both sides with a keyed MAC/HMAC and comparing digests) so that comparison time is independent of where the strings first differ. Apply the fix uniformly across `postblock_proposal.rs`, `blockreplay.rs`, `blocksimulate.rs`, `fastcallreadonly.rs`, `postblock_v3.rs`, and `txsimulate.rs`, ideally by centralizing the check into one constant-time helper function used by every `auth`-gated handler.

### Proof of Concept
1. Configure a node with `connection_options.auth_token = "<secret>"`.
2. As a remote, unauthenticated client, send repeated requests to `/v3/blocks/simulate/<block_id>` (or any of the other listed endpoints) with `authorization` header values that are systematically varied one byte at a time from a base guess.
3. Measure per-request round-trip time; because `auth_header != password` short-circuits on the first mismatching byte, correct-prefix guesses will show a statistically detectable, small but measurable increase in average processing time compared to guesses that mismatch at the very first byte.
4. Iterate byte-by-byte (as in classic timing side-channel attacks against `memcmp`), amplifying the signal using multiple samples per candidate byte, to fully recover `auth_token` without ever needing to know it, then use the recovered token to submit `/v3/blocks/upload/?broadcast=1` or `/v3/block_proposal` requests as an authenticated caller.

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
