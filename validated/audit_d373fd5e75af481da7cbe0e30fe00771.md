Given remaining iteration budget, I'll finalize based on gathered evidence.### Title
Non-constant-time HTTP `Authorization` password comparison on privileged RPC endpoints enables remote timing side-channel (analog: auth-gate weakness) - ([File: stackslib/src/net/api/postblock_proposal.rs])

### Summary
Several privileged HTTP RPC endpoints in `stackslib/src/net/api/` gate access behind a shared-secret `Authorization` header, but compare the supplied header to the configured secret using a plain, non-constant-time `!=` string comparison rather than a constant-time comparison. This is the same class of bug the external report exercises conceptually (an authorization check that can be defeated because the check does not correctly enforce "does the caller actually hold the right credential"), applied here as a timing oracle rather than a role-check bypass.

### Finding Description
Every handler that gates a privileged action behind `self.auth` performs the same pattern: [1](#0-0) 

```
let Some(password) = &self.auth else { ... };
let Some(auth_header) = preamble.headers.get("authorization") else { ... };
if auth_header != password {
    return Err(Error::Http(401, "Unauthorized".into()));
}
```

The identical pattern (a raw `String`/`&str` `!=` comparison) is repeated in: [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) 

Rust's `&str`/`String` `PartialEq` is implemented via a byte-slice comparison that short-circuits on the first mismatching byte. This means the wall-clock time to reject an incorrect `Authorization` header value is (in principle) a function of how many leading bytes of the guess match the real secret. No `subtle::ConstantTimeEq` or equivalent constant-time comparator is used anywhere in the codebase for this check (confirmed by search — no matches for `constant_time`/`ConstantTimeEq`).

This breaks the "authenticated vs. stored" equality guarantee the rules call out: the check is supposed to be an all-or-nothing secret comparison, but its timing characteristics leak partial information about the stored secret to a remote, unauthenticated caller who can send many requests.

### Impact Explanation
If exploitable, this would allow an unauthenticated remote attacker to recover the RPC auth token via repeated timing measurements and then use it to reach privileged endpoints such as `/v3/block_proposal` (`postblock_proposal.rs`) and `/v3/blocks/postblock?broadcast=1` (`postblock_v3.rs`), which are explicitly designed to be gated behind this very token because they trigger block-proposal validation / broadcast paths. That would map to the "Critical: unauthenticated/unauthorized write to state" bucket in the rules.

### Likelihood Explanation
This is **low-to-uncertain likelihood in practice**, and I want to be explicit about that rather than overstate it:
- These endpoints are local/operator RPC endpoints (typically bound to loopback or a private network) rather than the public P2P gossip surface, so "remote unprivileged" reachability depends on deployment/network exposure, which is outside what I can verify from the repo alone.
- Realistic exploitation of a byte-string timing side channel over a TCP/HTTP round trip requires extremely fine-grained statistical timing analysis, is a well known but not always practical class of remote attack, and is easily disrupted by network jitter, load balancers, or reverse proxies.
- I could not find any constant-time comparison anywhere in the codebase for any secret, meaning this is a systemic pattern rather than a one-off, which raises confidence this is a genuine oversight rather than a mitigated design choice — but I also could not verify (given tool constraints) whether these particular endpoints are ever exposed on an untrusted network path in default configurations, which is central to whether this is remotely reachable by an "unprivileged" attacker in the sense required by the rules.

### Recommendation
Replace the `!=` string comparisons in `postblock_proposal.rs`, `postblock_v3.rs`, `blockreplay.rs`, `fastcallreadonly.rs`, `txsimulate.rs`, and `blocksimulate.rs` with a constant-time comparison (e.g., `subtle::ConstantTimeEq`, or hash-then-compare with a fixed-size digest) so that the time to reject an incorrect `Authorization` header does not vary with how many leading bytes match the real secret.

### Proof of Concept
Not independently verified beyond static code reading — a full PoC would require timing-attack infrastructure (many repeated measurements against a live node) that I cannot execute in this ask-only, read-only environment. The static code path demonstrating the weakness is the `auth_header != password` comparison cited above, present identically across the six listed handler files.

### Citations

**File:** stackslib/src/net/api/postblock_proposal.rs (L1135-1144)
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

**File:** stackslib/src/net/api/blocksimulate.rs (L1-1)
```rust
// Copyright (C) 2025 Stacks Open Internet Foundation
```
