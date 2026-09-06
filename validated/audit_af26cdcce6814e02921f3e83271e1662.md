### Title
Non-constant-time secret comparison in `RPCBlockProposalRequestHandler::try_parse_request` enables timing side-channel recovery of the block-proposal auth token - (File: stackslib/src/net/api/postblock_proposal.rs)

### Summary
`try_parse_request` gates the `POST /v3/block_proposal` endpoint by comparing the client-supplied `authorization` header against the configured secret using Rust's default `String`/`&str` `PartialEq`, i.e. `auth_header != password`. This comparison is a byte-wise comparison that returns as soon as it finds a mismatching byte (or differing length), so the time taken to reject an incorrect header leaks information about how many leading bytes of the guess matched the real secret.

### Finding Description
In `RPCBlockProposalRequestHandler::try_parse_request`: [1](#0-0) 
the handler reads the `authorization` header from the untrusted `HttpRequestPreamble` and compares it to `self.auth` (the configured secret) with `auth_header != password`. `str`'s `PartialEq` implementation is defined in terms of slice/byte comparison, which in practice compares bytes sequentially and can return `false` (mismatch) as soon as the first differing byte is found, rather than always processing the full length. This is the classic non-constant-time secret comparison pattern (CWE-208). An attacker who does not know the secret can send repeated `POST /v3/block_proposal` requests with candidate header values that share progressively longer correct prefixes with the real secret, and measure response latency for the 401 rejection path. On average, guesses whose prefix matches more of the real secret take (however slightly) longer to reject than guesses that diverge earlier, because the comparison walks further before returning `false`. No secret, signature, or privileged role is needed to reach this code path — the endpoint is reachable by any unprivileged remote client that can open an HTTP connection to the node's RPC port, and the comparison executes before any further validation of the block proposal body. The same pattern is repeated in `blockreplay.rs`, `blocksimulate.rs`, and `txsimulate.rs`, but this question scopes to `postblock_proposal.rs`.

### Impact Explanation
If exploitable, a successful timing attack would let a remote unprivileged attacker recover the RPC secret (`auth_token`) byte-by-byte without ever needing it, ultimately granting unauthorized write access to `/v3/block_proposal` (and, since the same secret typically gates other privileged endpoints, potentially broader RPC auth bypass). This matches the "auth bypass" Critical category cited in the rules. However, in practice, wall-clock string comparison timing differences (nanoseconds to low microseconds for short strings) are extremely difficult to distinguish over a real network due to jitter, OS scheduling noise, TLS/TCP stack overhead, and the enclosing HTTP request-parsing costs that dominate total handler latency — the theoretical signal is present in the code but the practical signal-to-noise ratio for remote exploitation is very poor, and no concrete demonstrated end-to-end secret-recovery exploit exists in this codebase or its tests.

### Likelihood Explanation
The attacker only needs network access to the node's RPC port, no credentials, and no privileged role — reachability is trivial. However, extracting a full secret via this channel requires an extremely large number of precisely-timed requests per byte to overcome network/system noise, and the node's `/v3/block_proposal` handler additionally does non-trivial preamble parsing before the comparison, adding uncontrolled latency variance that would need to be isolated statistically. This makes real-world exploitation low-likelihood despite the code-level non-constant-time comparison being real.

### Recommendation
Use a constant-time comparison for the secret check, e.g. compare using a crate like `subtle::ConstantTimeEq` (`ct_eq`) over the byte representations of `auth_header` and `password`, ensuring the comparison time does not depend on where the first mismatching byte occurs, applied consistently in `postblock_proposal.rs`, `blockreplay.rs`, `blocksimulate.rs`, and `txsimulate.rs`.

### Proof of Concept
A Rust test in `stackslib::net::api::postblock_proposal` would construct `RPCBlockProposalRequestHandler::new(Some(secret.clone()))`, then repeatedly call `try_parse_request` (via `StacksHttp::handle_try_parse_request`) with (a) an `authorization` header sharing 0 bytes of prefix with `secret` and (b) one sharing `secret.len()-1` bytes of prefix, timing each call with `std::time::Instant` over many iterations (e.g. tens of thousands), and asserting `mean(time_b) > mean(time_a)` with statistical significance (e.g. via a t-test or simple threshold on averaged samples) — this is the "proof idea" from the question. Note that due to noise inherent in short-string comparisons and surrounding request-parsing overhead, such a test is likely to be flaky/inconclusive on real hardware, reflecting the low practical exploitability noted above.

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
