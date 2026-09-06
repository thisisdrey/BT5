### Title
Non-constant-time comparison of the signer authorization token allows timing-based credential recovery - (File: `stackslib/src/net/api/postblock_proposal.rs`)

### Summary
The `/v3/block_proposal` endpoint, which gates unauthenticated block-validation requests from a `stacks-signer`, authenticates callers by comparing the `Authorization` header directly against the configured secret using Rust's `!=` operator (a short-circuiting, non-constant-time byte comparison), rather than a constant-time comparison.

### Finding Description
`RPCBlockProposalRequestHandler::try_parse_request` reads the configured secret (`self.auth`, sourced from `connection_options.auth_token`) and compares it to the client-supplied `authorization` header with a plain equality check: [1](#0-0) 

This is the same class of flaw as CVE-2020-10727 (GHSA-q9g8-9hpp-xc82): a shared secret intended to gate a privileged network operation is handled with insufficient protection — here, an early-exit string comparison whose timing leaks information about how many leading bytes of the guess match the real secret. `str`/`String` `PartialEq` in Rust compares byte-by-byte and returns as soon as a mismatch is found, so the elapsed time of a request correlates with the number of correct leading bytes of the token, enabling a remote attacker to incrementally recover the secret via repeated measurement (a timing side channel), rather than needing to already know it.

`connection_options.auth_token` is explicitly documented as securing both `/v3/block_proposal` and `/v2/blocks?broadcast=1`: [2](#0-1) 

meaning recovery of this token via the `/v3/block_proposal` timing channel would also grant the attacker the ability to authenticate to the broadcast-blocks path.

### Impact Explanation
If an unprivileged remote attacker can statistically recover the `auth_token` via timing measurements against `/v3/block_proposal`, they gain the ability to submit forged block proposals and/or authenticate to `/v2/blocks?broadcast=1`, i.e., unauthorized write access to node/network state that should require the shared signer secret. This matches the "unauthenticated/unauthorized write to state" / "auth bypass" impact tier, since the barrier being broken is precisely the equality check between "attacker-supplied token" and "the node's configured secret."

### Likelihood Explanation
Exploitation requires many repeated network requests with statistical timing analysis to distinguish per-byte comparison timing over noisy network conditions, which is a nontrivial, higher-effort attack (classic remote timing side channels on password comparisons are known to be exploitable but require careful measurement and many samples). This is a real defect but the practical exploitation bar is higher than a single-shot logic bypass; likelihood is best characterized as low-to-moderate depending on network latency and jitter.

### Recommendation
Replace the direct `!=` comparison with a constant-time comparison, e.g., `subtle::ConstantTimeEq` (`ct_eq`) or an equivalent implementation that compares all bytes regardless of an early mismatch, for both the length check and the byte comparison. Apply the same fix everywhere `auth_token`/`auth_password` is compared to caller-supplied values (e.g., the `/v2/blocks?broadcast=1` gate referenced by the same `auth_token` field).

### Proof of Concept
1. Configure a node with `connection_options.auth_token = "S3CR3T-LONG-TOKEN"`.
2. Send repeated POST requests to `/v3/block_proposal` with `Authorization` header guesses that share increasingly longer correct prefixes (e.g., `"X..."`, `"S..."`, `"S3..."`, etc.), each many times, and measure server response latency for the 401-Unauthorized rejection path at `stackslib/src/net/api/postblock_proposal.rs:1142-1144`.
3. Because the comparison exits at the first mismatched byte, guesses with a longer correct prefix take measurably longer on average (more bytes compared before divergence) than in a constant-time implementation, allowing the attacker to iteratively reconstruct the token one byte at a time.

Note: I was unable to fully explore `stackslib/src/net/httpcore.rs`'s 3 `auth_token`-related matches or the full `/v2/blocks?broadcast=1` gate implementation before the tool budget ran out, so I cannot confirm with certainty whether that second consumer of `auth_token` uses the same or a different (possibly already constant-time) comparison — this should be verified as part of remediation.

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
