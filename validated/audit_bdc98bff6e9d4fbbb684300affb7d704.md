### Title
Non-constant-time `Authorization` header comparison enables timing side-channel on `auth_token`-protected RPC endpoints - (File: `stackslib/src/net/api/postblock_proposal.rs`, `postblock_v3.rs`, `blockreplay.rs`, `blocksimulate.rs`, `fastcallreadonly.rs`, `txsimulate.rs`)

### Summary
Every privileged RPC endpoint that gates access with `connection_options.auth_token` validates the client-supplied `Authorization` header using Rust's default `!=` string comparison instead of a constant-time comparison. This is the exact bug class described in the OctoPrint advisory (CWE-208): a character-based comparison that short-circuits on the first mismatching byte, allowing a remote, unauthenticated attacker to recover the secret `auth_token` byte-by-byte via response-timing measurements.

### Finding Description
The shared pattern, repeated identically across all `auth_token`-protected handlers, is:

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
``` [1](#0-0) [2](#0-1) [3](#0-2) 

`auth_header != password` compares two `String`/`&str` values using Rust's standard `PartialEq`, which internally performs a length check followed by a byte-wise comparison that returns as soon as a mismatch is found (it does not run in constant time regardless of where the mismatch occurs). This is functionally identical to the OctoPrint flaw: "using character based comparison that short-circuits on the first mismatched character during API key validation, rather than a cryptographical method with static runtime."

The `auth_token` is loaded from `ConnectionOptions` into `StacksHttp` and passed to each handler's `self.auth` field: [4](#0-3) . It protects the block-proposal endpoint (`/v3/block_proposal`), the broadcast-block endpoint (`/v3/blocks/upload/`), block replay, block/tx simulation, and fast-call-read-only endpoints — all of which are explicitly designed to be reachable over the network from a `stacks-signer` or other client, per the config docs: [5](#0-4) .

The same non-constant-time check recurs verbatim in `postblock_v3.rs` (guarding block broadcast) and `blocksimulate.rs`, `postblock_proposal.rs` — confirmed by the identical `auth_header != password` grep hits in all six files.

### Impact Explanation
An attacker with network access to the node's RPC port can measure response latency for repeated guesses of the `auth_token` value, character by character, and eventually recover the full secret. Once recovered, they can submit forged/malicious block proposals to `/v3/block_proposal`, or broadcast attacker-supplied blocks via `/v3/blocks/upload/?broadcast=1`, effectively bypassing an authentication gate meant to restrict these sensitive write endpoints to the trusted `stacks-signer`. This matches the "auth-gate that fails open" / "auth bypass" category called for in scope, since a successful timing attack directly defeats the intended authentication control on state-mutating endpoints.

### Likelihood Explanation
Exploitability is bounded by the same caveats noted in the original OctoPrint advisory: success depends heavily on network latency/noise, and no concrete proof-of-concept extraction was demonstrated even by the original discoverer for the OctoPrint case ("theoretical timing attack... proof of concept was not achieved"). The Stacks RPC server processes many other operations per request (parsing, routing, JSON handling) that likely dominate and mask the sub-microsecond timing differential from a byte-array comparison over a typically short token, making practical exploitation difficult especially over the public Internet, though feasible on a low-latency/low-jitter LAN.

### Recommendation
Replace `auth_header != password` with a constant-time comparison (e.g., `subtle::ConstantTimeEq::ct_eq`, or hashing both values with a keyed MAC and comparing the MACs) in all affected handlers: `postblock_proposal.rs`, `postblock_v3.rs`, `blockreplay.rs`, `blocksimulate.rs`, `fastcallreadonly.rs`, and `txsimulate.rs`. Centralizing this check into a single shared helper in `httpcore.rs` (rather than duplicating it per-handler) would also reduce the risk of the fix being missed in one of the six call sites.

### Proof of Concept
Not independently reproduced (per report caveats, doing so requires a low-noise network environment). Conceptually: an attacker sends repeated `POST /v3/block_proposal` (or `/v3/blocks/upload/?broadcast=1`, etc.) requests with `Authorization` headers that share an increasingly long correct prefix of the real `auth_token`, statistically measuring response latency to detect the point where the byte comparison in `auth_header != password` begins to short-circuit later, revealing one more correct token byte per iteration until the full token is recovered.

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

**File:** stackslib/src/net/httpcore.rs (L1033-1036)
```rust
            maximum_call_argument_size: conn_opts.maximum_call_argument_size,
            read_only_call_limit: conn_opts.read_only_call_limit.clone(),
            auth_token: conn_opts.auth_token.clone(),
            allow_arbitrary_response: false,
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
