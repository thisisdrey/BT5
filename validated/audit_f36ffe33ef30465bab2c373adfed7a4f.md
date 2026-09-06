### Title
Non-constant-time authentication comparison enables timing side-channel to guess the block-proposal RPC secret - (File: `stackslib/src/net/api/postblock_proposal.rs`, `stackslib/src/net/api/blocksimulate.rs`)

### Summary
The `/v3/block_proposal` and `/v3/blocks/simulate/:block_id` RPC endpoints authenticate callers by comparing the `Authorization` header against the configured secret (`connection_options.auth_token`) using a plain `!=` string comparison. Rust's `String`/`&str` equality is a byte-wise comparison that short-circuits at the first mismatching byte, which is not constant-time. A remote, unauthenticated attacker with network access to the RPC port can exploit response-time differences to recover the shared secret byte-by-byte and then submit authenticated block proposals or replay/simulate requests.

### Finding Description
Both handlers gate access to their otherwise-privileged endpoints with the same pattern: [1](#0-0) [2](#0-1) 

`password` is set from the node's `[connection_options] auth_token`, which is meant to be a shared secret matching the signer's `auth_password` [3](#0-2) . The check `if auth_header != password` breaks the intended equality property "only the party holding the exact secret is authenticated" into "the party whose guessed prefix matches the secret for the longest byte run gets measurably faster/slower rejection," because standard slice/string equality in Rust returns as soon as a mismatch is found (and also short-circuits on length mismatch), rather than comparing in constant time. No `subtle::ConstantTimeEq` or similar constant-time comparison primitive is used anywhere in the codebase for this check.

### Impact Explanation
This falls squarely into the "auth bypass" Critical bucket: an attacker who can measure timing on the RPC port can incrementally recover the `auth_token`/`auth_password` secret and then submit forged/attacker-controlled requests to `/v3/block_proposal` (used by stackers/signers to validate a proposed Nakamoto block) or the block-simulation/replay endpoint, both of which are otherwise access-controlled precisely because they can drive expensive validation/mining-adjacent state-mutating work on the node.

### Likelihood Explanation
Exploitation requires only network reachability to the RPC endpoint and the ability to send repeated authenticated requests while measuring response latency — no privileged position, node secret, or other party's key is needed, and it does not rely on volumetric traffic to succeed (a byte-at-a-time online timing attack, feasible over many requests). The `auth` string length is attacker-observable via response behavior differences at each character boundary, which is a well-known class of exploitable password/token timing oracle.

### Recommendation
Replace the direct `!=` comparisons in `RPCBlockProposalRequestHandler::try_parse_request` and `RPCNakamotoBlockSimulateRequestHandler::try_parse_request` with a constant-time comparison (e.g., `subtle::ConstantTimeEq`, or a manual XOR-accumulate comparison over the full length regardless of early mismatches), and ensure header/secret length mismatches don't leak timing signal either. Apply the same fix everywhere else the same pattern occurs for these auth-guarded RPC handlers.

### Proof of Concept
1. Configure a node with `[connection_options] auth_token = "<secret>"` and enable `/v3/block_proposal`.
2. From a remote host, repeatedly POST to `/v3/block_proposal` with `Authorization` headers that are candidate prefixes of the secret, measuring response latency for the 401 rejection path at `stackslib/src/net/api/postblock_proposal.rs:1142`.
3. Statistically distinguish correct-prefix guesses (marginally slower due to more bytes compared before mismatch) from incorrect ones, and iterate character-by-character to reconstruct `auth_token` without ever needing local access to the node's config file.

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

**File:** sample/conf/signer/mainnet-signer-conf.toml (L45-50)
```text
# REQUIRED: Authorization password for the node's block proposal endpoint.
#
# WARNING: This MUST match the `auth_token` in the stacks-node's
# [connection_options] section. If they do not match, the signer
# cannot communicate with the node and will fail silently.
auth_password = "<YOUR_AUTH_PASSWORD>"
```
