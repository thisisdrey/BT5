### Title
Non-constant-time comparison of the RPC `authorization` header enables a timing side-channel that can leak the node's `auth_token` - (File: `stackslib/src/net/api/postblock_proposal.rs`)

### Summary
`RPCBlockProposalRequestHandler::try_parse_request` gates the privileged `/v3/block_proposal` endpoint with a plain Rust string comparison, `auth_header != password`, instead of a constant-time comparison. The same anti-pattern is repeated verbatim across several other privileged/administrative RPC handlers (`blockreplay.rs`, `blocksimulate.rs`, `fastcallreadonly.rs`, `postblock_v3.rs`, `txsimulate.rs`). This is the same bug class as the reported pysrp `calculate_x` issue (CWE-203/CWE-208): an early-exit equality check leaks information about the correct secret via observable timing discrepancy.

### Finding Description
`str`/`String` equality in Rust is implemented via byte-wise comparison and returns as soon as a mismatch is found (`memcmp`-like short-circuiting), so the wall-clock time taken to reject an incorrect `Authorization` header value is proportional to the length of the matching prefix with the true secret token. [1](#0-0) 

This breaks the intended equality guarantee that "authenticated" and "not authenticated" are the only two observable outcomes for a would-be attacker — instead there is a third, exploitable channel: relative comparison time reveals how many leading bytes of the attacker-supplied token match the real `auth_token` configured via `connection_options.auth_token`.

Because Rust's `!=` for strings does not run in constant time (unlike a proper `subtle::ConstantTimeEq`/HMAC-based check), an attacker can repeatedly probe the endpoint with candidate header values and use statistical timing measurements to recover the secret token byte-by-byte, analogous to how pysrp's non-constant-time `calculate_x` leaked information through timing discrepancies.

### Impact Explanation
The `/v3/block_proposal` endpoint (and the sibling endpoints using the identical pattern) is explicitly gated behind this password because it is meant to be reachable only by the node operator/signer infrastructure — the code comments confirm the endpoint is "not enabled" without a password. Successfully recovering the `auth_token` via the timing side-channel gives a remote, unauthenticated attacker the ability to submit forged/attacker-controlled requests to these privileged endpoints as if they held the correct credential, i.e., an auth bypass on an RPC endpoint that is otherwise supposed to be restricted. This matches the "auth bypass" criterion.

### Likelihood Explanation
The vulnerability is reachable by any remote, unprivileged network peer that can send HTTP requests to the node's RPC port — no prior authentication or node secret is required (matching the report's `AV:N/AC:L/PR:N/UI:N` vector). Exploitation requires collecting a sufficient number of timing samples per byte position to overcome network jitter, but this class of attack (byte-at-a-time timing side channel against a naive string comparison) is well-established and does not require any special access beyond network connectivity to the RPC endpoint.

### Recommendation
Replace the direct `!=`/`==` string comparisons against `auth_token`/`password` in `postblock_proposal.rs`, `blockreplay.rs`, `blocksimulate.rs`, `fastcallreadonly.rs`, `postblock_v3.rs`, and `txsimulate.rs` with a constant-time comparison, e.g. using the `subtle` crate's `ConstantTimeEq`, or by comparing HMAC/hash digests of both values rather than raw bytes, so that the elapsed time does not depend on where the first mismatching byte occurs.

### Proof of Concept
1. Configure a node with `connection_options.auth_token` set to a fixed secret and expose `/v3/block_proposal`.
2. From a remote unauthenticated client, send repeated POST requests with `authorization` headers made of a fixed correct prefix plus a varying next byte over the full byte-value range, measuring response latency for the `401 Unauthorized` result at line 1140-1144 of `postblock_proposal.rs`.
3. The candidate byte producing the largest average latency (indicating comparison advanced one byte further before mismatching) is inferred as the correct next byte of the secret; repeat per byte position to reconstruct the full token, then use it to submit unauthorized/forged data to the endpoint. [1](#0-0)

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
