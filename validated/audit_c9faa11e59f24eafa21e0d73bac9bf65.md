### Title
Non-constant-time authorization comparison enables timing side-channel auth bypass - (File: stackslib/src/net/api/postblock_proposal.rs)

### Summary
`RPCBlockProposalRequestHandler::try_parse_request` compares the client-supplied `Authorization` header against the configured secret using Rust's standard `!=` operator on `String`/`str`, which is not constant-time. An attacker who can send repeated `POST /v3/block_proposal` requests can, in principle, use response-timing differences to recover the secret byte-by-byte without ever knowing it, since the underlying comparison short-circuits on the first mismatching byte/word.

### Finding Description
In `RPCBlockProposalRequestHandler::try_parse_request`:
```rust
let Some(auth_header) = preamble.headers.get("authorization") else {
    return Err(Error::Http(401, "Unauthorized".into()));
};
if auth_header != password {
    return Err(Error::Http(401, "Unauthorized".into()));
}
``` [1](#0-0) 

`auth_header != password` invokes `str`'s `PartialEq`, which is implemented via byte-slice comparison (`memcmp`-style) that returns as soon as a mismatch is found. This means the time to reject an incorrect guess is correlated with the length of the correct prefix shared with the real secret — the classic non-constant-time secret comparison flaw (CWE-208). No `subtle`/`ConstantTimeEq` or manual constant-time comparison utility exists anywhere in the codebase, confirmed by searching for `constant_time`, `subtle::`, `ConstantTimeEq`, `ct_eq` (no matches). The same vulnerable pattern (`auth_header != password`) is repeated verbatim across `blockreplay.rs`, `blocksimulate.rs`, `fastcallreadonly.rs`, `postblock_v3.rs`, and `txsimulate.rs`, so this is a systemic, not one-off, issue.

The comparison is the sole authorization gate for this endpoint — there is no other rate-limiting, constant-time padding, or additional secret-independent delay applied before the `Err(Error::Http(401, ...))` is returned. Once the header is correctly guessed, the caller obtains full access to the block-proposal validation endpoint, which spawns a validation thread that reads sortition/chain state and performs static+execution validation of an attacker-supplied Nakamoto block — a resource-intensive operation gated specifically by this auth check.

### Impact Explanation
An attacker who successfully exploits the timing channel obtains the operator's RPC auth secret and can then submit arbitrary block proposals to `/v3/block_proposal` as an authorized caller (normally reserved for the signer set), triggering repeated resource-intensive validation (`NakamotoBlockProposal::validate`) — a bounded compute DoS — and generally bypassing the intended access control for a privileged endpoint. This matches the "auth bypass" / "bounded compute DoS on a bounded endpoint" categories in the Critical/High impact list.

### Likelihood Explanation
Preconditions: the node must be configured with `Some(auth)` (i.e., block-proposal validation enabled with a password) — this is the normal operating configuration for miner/signer nodes running this endpoint. The attacker needs only network reachability to the RPC port and no prior knowledge of the secret, satisfying the "unprivileged remote attacker" model. However, exploiting this in practice requires extracting a reliable, low-noise timing signal over a network path across many (often thousands per byte) trials to statistically distinguish single extra-byte-of-match timing differences against typical network jitter and OS scheduling noise — this is nontrivial but is an established, previously demonstrated class of attack (e.g., Lucky 13-style network timing attacks), and is exactly the fault pattern named in the question.

### Recommendation
Replace the `!=`/`==` comparison on `auth_header`/`password` with a constant-time comparison, e.g. using the `subtle` crate's `ConstantTimeEq`/`ct_eq` on the byte representations of both strings (after first checking/normalizing lengths in a way that doesn't leak timing), or a manual XOR-accumulate comparison over fixed-length byte buffers. Apply the same fix to the identical pattern in `blockreplay.rs`, `blocksimulate.rs`, `fastcallreadonly.rs`, `postblock_v3.rs`, and `txsimulate.rs`.

### Proof of Concept
Add a test under `stackslib/src/net/api/tests/postblock_proposal.rs` that:
1. Constructs a `RPCBlockProposalRequestHandler::new(Some(secret.clone()))` where `secret` is a long fixed string (e.g. 64 bytes).
2. For varying "correct-prefix-length" guesses (0, 16, 32, 48, 63 correct bytes, remainder wrong), repeatedly calls `try_parse_request` (or directly invokes the `auth_header != password` comparison logic extracted into a helper) via `Instant::now()`-timed loops, averaging over many iterations to reduce noise.
3. Asserts that the mean comparison time for higher matching-prefix lengths is not statistically indistinguishable from lower matching-prefix lengths — i.e. assert that duration scales with prefix length, demonstrating the leak (this documents the fault rather than requiring literal network exploitation, per the proof idea in the question). [2](#0-1)

### Citations

**File:** stackslib/src/net/api/postblock_proposal.rs (L1128-1144)
```rust
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
