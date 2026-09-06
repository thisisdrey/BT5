### Title
Unbounded, non-constant-time authentication check on `/v3/block_proposal` allows unlimited remote password-guessing / timing-based auth bypass - (File: stackslib/src/net/api/postblock_proposal.rs)

### Summary
`RPCBlockProposalRequestHandler::try_parse_request` authenticates the `/v3/block_proposal` endpoint by comparing the client-supplied `Authorization` header directly against the configured `auth_token` using a plain string `!=` comparison, with no attempt counting, delay, or lockout on repeated failures. [1](#0-0) 

### Finding Description
The equality check `auth_header != password` is a standard (non-constant-time) byte/string comparison rather than a constant-time comparison. Rust/`std::cmp` string equality short-circuits on the first differing byte, so the time taken to reject an incorrect token leaks information about how many leading bytes matched. Because this endpoint has no failed-attempt counter, exponential backoff, or IP/account lockout (unlike the P2P layer's peer-ban logic in `stackslib/src/net/p2p.rs::process_bans`, which does implement escalating deny periods for misbehaving peers [2](#0-1) ), a remote, unauthenticated attacker can send an unbounded number of guesses to recover the `auth_token` byte-by-byte via a timing side channel, or simply brute-force it directly since there is no restriction on attempt volume.

This is the same bug class as the reported CVE: an authentication gate that permits unlimited automated attempts without any restriction, undermining the secrecy of the shared secret that gates a privileged write-capable endpoint (`/v3/block_proposal` accepts and processes attacker-supplied block proposals, and the same `auth_token` also gates `/v2/blocks?broadcast=1` per its doc comment) [3](#0-2) . The identical pattern also exists in the sibling block-replay/simulate handler. [4](#0-3) 

### Impact Explanation
If an attacker recovers the `auth_token` (via timing side-channel analysis or brute force, since there is no rate limiting), they gain the ability to submit `/v3/block_proposal` requests and, per the documented use of the same token, potentially broadcast blocks via `/v2/blocks?broadcast=1`. This is an unauthenticated-to-authenticated privilege escalation onto a state-changing/mining-control endpoint — matching the "unauthenticated/unauthorized write to state" and "auth bypass" impact categories called out in scope.

### Likelihood Explanation
Exploitation requires only network access to the node's RPC port and the ability to send repeated HTTP POST requests with varying `Authorization` header values — no privileged credentials, no consensus-layer capability, and no volumetric flooding (a timing attack needs statistically many but not "volumetric" requests, and is a targeted, purposeful use of network access rather than bandwidth exhaustion). The lack of any attempt-limiting logic on this specific code path makes exhaustive/timing-based guessing straightforward to automate, similar to how the referenced advisory shows that automating the full multistep flow defeats an app-level attempt-limit that was never actually enforced at the right layer.

### Recommendation
- Replace the direct `!=` string comparison with a constant-time comparison (e.g., `subtle::ConstantTimeEq` or an HMAC-based comparison) so that response timing does not leak information about how many characters matched.
- Add attempt-based mitigations for this endpoint: a per-source-IP failed-attempt counter with exponential backoff/lockout (mirroring the pattern already used for P2P peer bans in `stackslib/src/net/p2p.rs`), and/or minimum entropy requirements/rotation guidance for `auth_token`.
- Apply the same fix to `blocksimulate.rs`, which shares the identical vulnerable comparison pattern.

### Proof of Concept
1. Configure a node with `auth_token = "<secret>"` enabling `/v3/block_proposal`.
2. From a remote unauthenticated client, repeatedly POST to `/v3/block_proposal` with varying `Authorization` header values, incrementally guessing one character at a time and measuring response latency for the 401 rejection at `stackslib/src/net/api/postblock_proposal.rs:1142-1144`.
3. Because the comparison short-circuits on the first mismatched byte and no attempt limit exists, an attacker can statistically distinguish correct-prefix guesses from incorrect ones and iteratively recover the full token, or simply exhaust the token space directly since no lockout is ever triggered.
4. Once the token is recovered, the attacker can submit arbitrary block proposals (and, per the shared-token design, broadcast blocks via `/v2/blocks?broadcast=1`) as an "authenticated" signer/miner client.

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

**File:** stackslib/src/net/p2p.rs (L1719-1723)
```rust
    /// Process ban requests.  Update the deny in the peer database.  Return the vec of event IDs to disconnect from.
    fn process_bans(&mut self) -> Result<Vec<DropPeer>, net_error> {
        if cfg!(test) && self.connection_opts.disable_network_bans {
            return Ok(vec![]);
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
