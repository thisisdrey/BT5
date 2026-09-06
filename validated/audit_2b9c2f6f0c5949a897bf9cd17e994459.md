### Title
Non-constant-time authorization token comparison enables remote timing side-channel to brute-force `auth_token` - (File: stackslib/src/net/api/postblock_proposal.rs)

### Summary
The RPC HTTP handlers that gate privileged endpoints behind a shared-secret `Authorization` header compare the attacker-supplied header to the node's configured `auth_token` using Rust's standard `!=` string comparison, which is not constant-time. This is a direct analog of the reported ezpublish-kernel timing-attack advisory (GHSA-xfqg-p48g-hh94): an "authenticated vs. stored" equality check leaks timing information proportional to the number of correctly-guessed prefix bytes, letting a remote, unauthenticated attacker recover the secret token byte-by-byte.

### Finding Description
Several `HttpRequest::try_parse_request` implementations extract the `authorization` header and compare it directly to the configured secret with `!=`: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) 

Rust's `String`/`&str` `PartialEq` (used by `!=`) first compares lengths, then compares bytes sequentially and returns as soon as a mismatch is found (it lowers to a `memcmp`-equivalent, not a constant-time comparison). This means the time taken to reject a guess is a function of how many leading bytes of the guess match the real secret — exactly the class of defect described in the ezpublish-kernel advisory ("random execution time ... was found to not be good enough ... fix replaces this with constant-time functionality"). Because `auth_token` is compared as a whole string (not hashed, not HMAC'd, no length-independent constant-time compare like `subtle::ConstantTimeEq`), an attacker who can make many requests can measure per-byte response-time differences to reconstruct the token incrementally, byte-by-byte, rather than needing the full keyspace at once.

This same pattern is used to gate `/v3/block_proposal`, `/v2/blocks?broadcast=1` (via `postblock_v3.rs`), `/v3/transactions/simulate`, `/v3/blocks/replay/:block_id`, and `/v3/contracts/fast-call-read/...` — all reachable, unauthenticated-until-this-check RPC endpoints in `stackslib/src/net/api/**`.

### Impact Explanation
Successfully recovering `auth_token` breaks the authentication boundary between the node and its paired `stacks-signer`/miner, since this single token is documented as the sole authentication mechanism for these endpoints: [6](#0-5) 

Bypassing it grants a remote, unprivileged attacker the ability to submit forged block proposals for validation, broadcast blocks via `/v2/blocks?broadcast=1`, or invoke the other authenticated RPC surfaces without holding any legitimate credential — an unauthenticated write / auth-bypass class impact per the scoping rules.

### Likelihood Explanation
Timing side-channels over a network are noisy and require many samples/averaging to extract signal, and the marginal timing difference per differing byte in a short in-memory string comparison is small. This raises the practical bar compared to a local timing attack, but it is a genuine, remotely-triggerable defect requiring no credentials, no privileged role, and reaches a real equality-check fault site — matching the reported bug class directly rather than a mere theoretical note.

### Recommendation
Replace all `auth_header != password` checks in `stackslib/src/net/api/{postblock_proposal.rs, postblock_v3.rs, txsimulate.rs, blockreplay.rs, fastcallreadonly.rs}` with a constant-time comparison (e.g. the `subtle` crate's `ConstantTimeEq`, or an HMAC-based comparison of the header against the configured secret) so that comparison time is independent of where the first mismatching byte occurs and independent of the guess's correct-prefix length.

### Proof of Concept
1. Configure a node with `auth_token = "<secret>"` and expose `/v3/block_proposal`.
2. From a remote client, send repeated POST requests with candidate `authorization` headers that share an increasing correct prefix of the real secret (e.g., brute-force byte 0, then byte 1, etc.), measuring server response latency for the 401 rejection path at [1](#0-0) .
3. Average many samples per candidate byte to distinguish the small but consistent timing increase when a guess byte matches versus mismatches, incrementally reconstructing `auth_token` without ever knowing it in advance.

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

**File:** stackslib/src/net/api/txsimulate.rs (L355-360)
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
