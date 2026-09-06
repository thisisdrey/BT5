### Title
Non-constant-time auth-token comparison enables remote timing side-channel recovery of the RPC `authorization` secret - ([File: stackslib/src/net/api/postblock_proposal.rs])

### Summary
Every privileged `/v3/*` RPC handler in this repo authenticates by comparing the client-supplied `authorization` header against the node's configured secret using plain Rust string inequality (`auth_header != password`). This is a naive, early-exiting byte comparison rather than a constant-time comparison, so the time to reject a wrong guess leaks how many leading bytes of the guess matched the real secret — the same class of weakness as GHSA-pj2c-h76w-vv6f (tokens insufficiently protected so they can be recovered by an attacker), just manifesting as a timing side-channel instead of weak encryption.

### Finding Description
The following handlers all gate privileged/high-impact endpoints with the identical pattern: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) 

In each case:
```rust
let Some(auth_header) = preamble.headers.get("authorization") else {
    return Err(Error::Http(401, "Unauthorized".into()));
};
if auth_header != password {
    return Err(Error::Http(401, "Unauthorized".into()));
}
```
`auth_header != password` invokes `String`/`&str` `PartialEq`, which is implemented via a byte-slice comparison that short-circuits (returns as soon as a mismatching byte/chunk is found, and also short-circuits on length mismatch). This means the elapsed wall-clock time for a 401 response is correlated with the length of the correct-prefix shared between the guess and the real `connection_options.auth_token` secret. This secret is a single static, unrotated string (`docs/rpc/openapi.yaml`, `sample/conf/mainnet-signer.toml`) that gates the signer-facing block-proposal endpoint (`/v3/block_proposal`), replay/simulate endpoints, and the authenticated write-path for `/v2/blocks?broadcast=1` in `postblock_v3.rs`. An attacker who can measure response timing over many repeated requests (averaging out network jitter) can incrementally recover the secret byte-by-byte, analogous to classical padding/token timing attacks, ultimately achieving full auth bypass on these endpoints (most notably `/v3/block_proposal`, which lets a caller submit block proposals for signer validation, and `/v2/blocks?broadcast=1`, which lets a caller force block broadcast). [7](#0-6) 

### Impact Explanation
If successfully exploited, this breaks the "authenticated vs stored" equality the report class targets: an attacker who statistically recovers `auth_token` gains the ability to authenticate to the block-proposal and broadcast RPCs as if they held the shared secret, i.e. unauthenticated/unauthorized write access to node behavior that is otherwise gated (submitting fabricated block proposals to the signer pipeline, forcing broadcast of arbitrary blocks). This aligns with the "High"/"Critical" impact bar (auth bypass, unauthenticated write) defined in scope.

### Likelihood Explanation
Exploitation requires many repeated network round-trips to statistically extract a timing signal through jitter, which is harder than a purely local timing attack but is a well-documented, practical technique for HTTP-exposed secret comparisons (particularly effective against long-lived, unrotated tokens like `auth_token`). It requires no privileged access, no node secret key, and no insider role — only network reachability to the RPC-bound port, satisfying the "remote, unprivileged" bar. Likelihood is moderate, since it needs sustained low-jitter access and many samples, but the affected secret is static and long-lived, giving an attacker unlimited attempts.

### Recommendation
Replace all `auth_header != password` comparisons in `stackslib/src/net/api/{postblock_proposal.rs, blockreplay.rs, blocksimulate.rs, fastcallreadonly.rs, txsimulate.rs, postblock_v3.rs}` with a constant-time comparison, e.g. hash both operands with a keyed MAC and compare digests, or use a constant-time-equal primitive from a vetted crate (e.g. `subtle::ConstantTimeEq`), on top of enforcing a minimum-length secret and rate limiting/backoff on repeated 401s from the same peer.

### Proof of Concept
1. Configure a node with `connection_options.auth_token = "<secret>"` and expose `/v3/block_proposal`.
2. From a remote client, issue many `POST /v3/block_proposal` requests, each with a different guessed `authorization` header value that varies only in one byte position at a time, measuring response latency to the 401.
3. Aggregate/average timings per candidate byte across many trials to filter out jitter; the candidate exhibiting a small but consistent latency increase (due to comparing one additional matching byte before divergence) indicates the correct byte at that position.
4. Repeat position-by-position to reconstruct the full `auth_token`, then use it to authenticate as a legitimate signer/miner against `/v3/block_proposal` or to force `/v2/blocks?broadcast=1`.

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

**File:** stackslib/src/net/api/blockreplay.rs (L575-583)
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

**File:** stackslib/src/net/api/blocksimulate.rs (L153-161)
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

**File:** stackslib/src/net/api/fastcallreadonly.rs (L102-110)
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

**File:** stackslib/src/net/api/txsimulate.rs (L352-360)
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
