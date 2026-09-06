The scan found a valid analog: a network-facing password/authorization comparison done with Rust's default (non-constant-time) string equality operator (`!=`), repeated across six RPC endpoint handlers in `stackslib/src/net/api/`. This is the same bug class as CVE-2018-1000119 (CWE-203, timing-based secret exposure via naive equality check).

### Title
Timing side-channel in RPC `authorization` header comparison allows remote recovery of the node's `auth_token` - (File: `stackslib/src/net/api/postblock_v3.rs`, also `postblock_proposal.rs`, `blockreplay.rs`, `blocksimulate.rs`, `txsimulate.rs`, `fastcallreadonly.rs`)

### Summary
Six RPC request handlers under `stackslib/src/net/api/` authenticate privileged requests by comparing the client-supplied `authorization` header directly against the node's configured secret (`connection_options.auth_token`) using Rust's built-in `String`/`&str` `PartialEq` (`!=`), which is a byte-by-byte, early-exit comparison rather than a constant-time comparison.

### Finding Description
Each of these handlers extracts the `authorization` header and does a plain inequality check against the shared secret: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) 

Rust's default `str`/`String` equality (used to implement `!=`/`==`) compares byte-by-byte and returns as soon as a mismatch is found (it also short-circuits immediately on length mismatch). This makes the comparison time a function of how many leading bytes of the attacker-supplied header match the true secret — precisely the bug class described in GHSA-688c-3x49-6rqj / CVE-2018-1000119, where Sinatra's `rack-protection` CSRF check leaked timing signal the same way. A remote, unauthenticated attacker can send repeated requests with candidate header values and use response-time measurements to recover the `auth_token` byte-by-byte (or byte-block-by-byte, with sufficient statistical averaging over the network), eventually forging a valid `authorization` header. This codebase already avoids `subtle`/constant-time comparison primitives entirely (no `ct_eq`/`subtle::` usage found anywhere in the repo), confirming this is not otherwise mitigated.

### Impact Explanation
`auth_token` gates several privileged, unauthenticated-by-default RPC surfaces reachable from the network:
- `/v3/block_proposal` (`postblock_proposal.rs`): accepting attacker-forged block proposals for validation.
- `/v2/blocks?broadcast=1` (`postblock_v3.rs`): the same token also authorizes forced network broadcast of a submitted block — [6](#0-5) .
- `/v3/blocks/replay` and `/v3/blocks/simulate` and `/v3/transactions/simulate`: sensitive read/compute endpoints.

Recovering this shared secret via timing analysis lets an attacker bypass the auth gate entirely, enabling unauthorized write/broadcast of forged block/transaction data into the network via the authenticated `broadcast=1` path, which matches the "Critical: unauthenticated/unauthorized write to state..., network-wide propagation of forged data" impact bucket.

### Likelihood Explanation
Exploitation requires only unauthenticated network access to the node's RPC port and the ability to send many timed requests — no special privileges. Real-world exploitation of remote timing side channels over typical network jitter is nontrivial and requires many samples / statistical averaging, so likelihood is lower than for a direct logic flaw, but the primitive itself is provably non-constant-time and directly matches the reported CVE's root cause.

### Recommendation
Replace all six `auth_header != password` (and the equivalent `==`) checks with a constant-time comparison, e.g. using the `subtle` crate's `ConstantTimeEq`/`ct_eq` over the header and secret bytes (first normalizing/padding lengths to avoid leaking length via early return), consistently across `postblock_v3.rs`, `postblock_proposal.rs`, `blockreplay.rs`, `blocksimulate.rs`, `txsimulate.rs`, and `fastcallreadonly.rs`.

### Proof of Concept
1. Configure a node with `connection_options.auth_token = "<secret>"`.
2. From a remote unauthenticated client, repeatedly POST to `/v3/block_proposal` (or any of the other five endpoints) with an `authorization` header guess, measuring response latency to the point where the 401 is returned.
3. Because `auth_header != password` short-circuits at the first mismatching byte, correct-prefix guesses take measurably longer (extra byte comparisons) than incorrect-prefix guesses; iterating over candidate bytes at each position and keeping the slowest response recovers the token byte by byte, exactly as in the referenced Sinatra `rack-protection` CSRF-token timing attack. [7](#0-6)

### Citations

**File:** stackslib/src/net/api/postblock_v3.rs (L99-122)
```rust
        // if broadcast=1 is set, then the requester must be authenticated
        let mut broadcast = false;
        let mut authenticated = false;

        // look for authorization header
        if let Some(password) = &self.auth {
            if let Some(auth_header) = preamble.headers.get("authorization") {
                if auth_header != password {
                    return Err(Error::Http(401, "Unauthorized".into()));
                }
                authenticated = true;
            }
        }

        // see if broadcast=1 is set
        for (key, value) in form_urlencoded::parse(query.as_ref().unwrap_or(&"").as_bytes()) {
            if key == "broadcast" {
                broadcast = broadcast || value == "1";
            }
        }

        if broadcast && !authenticated {
            return Err(Error::Http(401, "Unauthorized".into()));
        }
```

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
