### Title
Non-constant-time HTTP `authorization` header comparison enables remote timing side-channel recovery of the block-proposal `auth_token` - (File: stackslib/src/net/api/postblock_proposal.rs)

### Summary
The external report concerns Jenkins storing plugin credentials in plain text where an authorized-but-lower-privileged user could read them (CWE-522, "Insufficiently Protected Credentials"). The closest reachable analog in this repo's scope is not disk storage but an equally-classed credential-protection defect: the `/v3/block_proposal` endpoint compares the caller-supplied `authorization` header against the configured secret using ordinary string/byte equality, which is not constant-time. This is a remote, unauthenticated primitive that leaks information about the secret via response timing, one of the accepted "equality" defects in scope (authenticated vs stored comparison done unsafely).

### Finding Description
`RPCBlockProposalRequestHandler::try_parse_request` performs the auth check as: [1](#0-0) 

```
let Some(password) = &self.auth else { ... };
let Some(auth_header) = preamble.headers.get("authorization") else { ... };
if auth_header != password {
    return Err(Error::Http(401, "Unauthorized".into()));
}
```

`auth_header != password` is a `String`/`str` comparison, which Rust's standard `PartialEq` for strings implements as a byte-wise comparison that typically short-circuits on the first mismatching byte (via `memcmp`/slice equality). This is not a constant-time comparison. The same secret (`connection_options.auth_token`) is documented as also gating `/v2/blocks?broadcast=1` and is shared verbatim with the `postblock_v3.rs` handler, which is wired up identically in `stackslib/src/net/api/mod.rs`: [2](#0-1) 

An unauthenticated remote attacker who can reach the node's RPC port can send repeated POSTs with guessed `authorization` header values and use response-time differences (proportional to the number of matching leading bytes before a mismatch) to incrementally recover the secret token, byte-by-byte. Once recovered, this token is the exact value that gates block-proposal submission and validation acceptance — i.e., an authentication secret protecting write access into the signer coordination path.

The config documents this token as the only gate: [3](#0-2) 

### Impact Explanation
If successfully exploited, an attacker who recovers `auth_token` gains the ability to submit forged/attacker-controlled block proposals to `/v3/block_proposal`, impersonating the legitimate miner/signer relationship. This crosses the "authenticated vs stored" equality that the auth gate is supposed to enforce, matching the report's rules for an in-scope analog ("an auth-gate that fails open" via broken secret comparison). This qualifies as High/Critical depending on practical exploitability of the timing channel (typically difficult over noisy networks, but the primitive is real and remotely reachable without any credentials).

### Likelihood Explanation
Timing side-channels over a network are noisy and require many samples to extract each byte, so practical exploitation is nontrivial but not theoretical-only — this is a well-known class of vulnerability (CWE-208) whose exploitability depends on network jitter, and can be made more tractable if the attacker is on a low-latency path (e.g., LAN, same datacenter, or via HTTP/1.1 keep-alive with many requests). The endpoint is reachable pre-authentication (that's the entire point of the check), and no rate limiting or constant-time comparison is present.

### Recommendation
Replace the direct `!=` comparison with a constant-time comparison, e.g. via `subtle::ConstantTimeEq` or a manual constant-time compare that always processes the full length of both strings regardless of a mismatch, applied identically in `postblock_proposal.rs` and `postblock_v3.rs`. Additionally, consider adding basic request throttling/backoff on auth failures to reduce timing-channel resolution.

### Proof of Concept
Not independently runnable in this environment (no filesystem/terminal access), but conceptually:
1. Configure a node with `connection_options.auth_token = "<secret>"`.
2. From a client with low, stable network latency to the node, repeatedly POST to `/v3/block_proposal` with `authorization` headers that are systematically varied byte-by-byte (e.g. all combinations of the first byte, keeping subsequent bytes constant), measuring response latency for the 401 rejection path.
3. Statistically distinguish that guesses whose prefix matches more of the true secret take a distinguishable amount of additional comparison time (extremely small per-byte, but measurable with enough samples: this is the same class of leak documented in numerous string-equality password-check CVEs).
4. Repeat per byte position to recover the full token, then use it to submit an unauthorized block proposal. [4](#0-3)

### Citations

**File:** stackslib/src/net/api/postblock_proposal.rs (L1097-1144)
```rust
impl RPCBlockProposalRequestHandler {
    pub fn new(auth: Option<String>) -> Self {
        Self {
            block_proposal: None,
            auth,
        }
    }

    /// Decode a JSON-encoded block proposal
    fn parse_json(body: &[u8]) -> Result<NakamotoBlockProposal, Error> {
        serde_json::from_slice(body)
            .map_err(|e| Error::DecodeError(format!("Failed to parse body: {e}")))
    }
}

/// Decode the HTTP request
impl HttpRequest for RPCBlockProposalRequestHandler {
    fn verb(&self) -> &'static str {
        "POST"
    }

    fn path_regex(&self) -> Regex {
        Regex::new(r#"^/v3/block_proposal$"#).unwrap()
    }

    fn metrics_identifier(&self) -> &str {
        "/v3/block_proposal"
    }

    /// Try to decode this request.
    /// There's nothing to load here, so just make sure the request is well-formed.
    fn try_parse_request(
        &mut self,
        preamble: &HttpRequestPreamble,
        _captures: &Captures,
        query: Option<&str>,
        body: &[u8],
    ) -> Result<HttpRequestContents, Error> {
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

**File:** stackslib/src/net/api/mod.rs (L164-170)
```rust
        self.register_rpc_endpoint(postblock::RPCPostBlockRequestHandler::new());
        self.register_rpc_endpoint(postblock_proposal::RPCBlockProposalRequestHandler::new(
            self.auth_token.clone(),
        ));
        self.register_rpc_endpoint(postblock_v3::RPCPostBlockRequestHandler::new(
            self.auth_token.clone(),
        ));
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
