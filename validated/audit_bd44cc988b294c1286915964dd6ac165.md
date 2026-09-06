## Title
Non-constant-time comparison of the RPC `authorization` header enables a remote timing attack to recover `auth_token` - (File: `stackslib/src/net/api/postblock_proposal.rs`, `blockreplay.rs`, `blocksimulate.rs`, `fastcallreadonly.rs`, `txsimulate.rs`, `postblock_v3.rs`)

### Summary
Every privileged RPC endpoint that gates access with the node's shared secret (`connection_options.auth_token`) compares the client-supplied `authorization` header against the configured password using Rust's standard `!=` operator on `String`/`&str`, e.g. `if auth_header != password { ... }`. This is a byte-wise, early-exiting comparison, not a constant-time comparison, which is the exact bug class described in CVE-2012-5507 (Zope2/Plone `AuthEncoding.py` password comparison timing discrepancy).

### Finding Description
The `authorization` header check is duplicated verbatim across six HTTP request handlers: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) 

All of these compare the header value to `self.auth`, which is populated directly from `connection_options.auth_token`: [7](#0-6) 

Rust's `PartialEq` for `str`/`String` slices delegates to a length check followed by a byte-slice equality routine that returns as soon as a mismatch is found (it is explicitly documented as not constant-time and is commonly compiled to `memcmp`-style early-exit code). This means the time taken by `auth_header != password` leaks the length of the longest correct prefix supplied by the attacker. An attacker with only network access (no valid token) can:
1. Send many requests with different guessed prefixes for the token.
2. Measure response latency for each guess.
3. Statistically distinguish "one more correct byte" (slightly slower) from "wrong at this position" (faster), incrementally recovering the full `auth_token` byte-by-byte — exactly the attack pattern described in the CVE-2012-5507 advisory for Zope2/Plone.

This is remote and unauthenticated: the whole premise of the attack is defeating the authentication check to *find* the secret, so no credential is required to mount it. The `auth_token` is meant to be the sole gate protecting the block-proposal endpoint (`/v3/block_proposal`), broadcast-write endpoint (`/v2/blocks?broadcast=1`), block replay/simulate endpoints, and the fast-read-only-call and tx-simulate endpoints, as documented: [8](#0-7) 

### Impact Explanation
Recovering `auth_token` via the timing side channel and then defeating authentication on `/v3/block_proposal` lets a remote unauthenticated attacker submit malicious/attacker-controlled block proposals directly to a signer-integrated node, and lets them authenticate to `/v2/blocks?broadcast=1` to force broadcast of blocks. This constitutes unauthenticated write access to node functionality gated specifically by this shared secret, matching the "unauthenticated/unauthorized write to state" impact category. It also undermines the signer/miner coordination flow described in the docs, where `auth_token`/`auth_password` are the only thing distinguishing an authorized signer from an arbitrary remote peer: [9](#0-8) 

### Likelihood Explanation
Exploitation requires many network round-trips and statistical timing analysis, which is noisier over the internet than in a local lab, but timing side-channel attacks on password-equality checks are a well-established and practical technique (this is precisely why CVE-2012-5507 was assigned high severity and standard practice is to use constant-time comparisons for shared secrets, e.g. `subtle::ConstantTimeEq` or HMAC-based comparison). The vulnerability requires no prior access — only network reachability to one of the six endpoints — and the same flawed pattern is repeated in six independent call sites, increasing the number of measurable oracles an attacker can use to average out network jitter.

### Recommendation
Replace all six instances of `auth_header != password` with a constant-time comparison (e.g., using the `subtle` crate's `ConstantTimeEq`, or by comparing HMAC-SHA256 digests of the header value and the secret with a constant-time equality check) so that the response latency does not depend on the position of the first mismatched byte. Centralize the auth-check logic in one shared helper (e.g., in `stackslib/src/net/httpcore.rs`) to avoid re-introducing the same flaw at future call sites.

### Proof of Concept
1. Configure `connection_options.auth_token = "SECRETTOKEN..."` on a node exposing `/v3/block_proposal`.
2. From a remote unauthenticated client, repeatedly send `POST /v3/block_proposal` with `authorization: <guess>` headers where `<guess>` varies only in one byte position at a time (e.g., brute-forcing each byte of the token while keeping already-confirmed prefix bytes fixed).
3. Measure response latency for the `401 Unauthorized` response for each candidate byte at that position, aggregating many samples per candidate to reduce noise.
4. The candidate byte yielding a statistically longer average latency (indicating the comparison in `auth_header != password` matched one additional byte before diverging) is likely correct; repeat position-by-position to reconstruct the full `auth_token`.
5. Once recovered, use the token to submit forged block proposals or authenticate on `/v2/blocks?broadcast=1`.

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

**File:** docs/signing.md (L42-59)
```markdown
```toml
stacks_private_key = "<YOUR_SIGNER_PRIVATE_KEY_HEX>"
node_host = "127.0.0.1:20443"
endpoint = "0.0.0.0:30000"
network = "mainnet"
auth_password = "your-secret-token"
db_path = "/var/lib/stacks-signer/signerdb.sqlite"
```

### 3. Verify Coordination

These settings **must** match between the node and signer configs:

| Signer Config   | Node Config                       | Must Match                    |
| --------------- | --------------------------------- | ----------------------------- |
| `auth_password` | `[connection_options] auth_token` | Exact string match            |
| `endpoint`      | `[[events_observer]] endpoint`    | Same host:port                |
| `node_host`     | `[node] rpc_bind`                 | Signer connects to node's RPC |
```
