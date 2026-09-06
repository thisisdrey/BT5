### Title
No rate limiting or lockout on `authorization` header verification for privileged HTTP RPC endpoints, combined with non-constant-time comparison, enables remote brute-force/timing recovery of `auth_token` - (File: stackslib/src/net/api/postblock_proposal.rs)

### Summary
Several privileged Stacks-node HTTP RPC endpoints (block proposal, block replay, block simulate, fast-call-read-only, transaction simulate, and the authenticated broadcast path of post-block-v3) protect state-changing/expensive operations with a single static shared secret (`connection_options.auth_token`) compared against the incoming `authorization` header. There is no attempt counter, delay, or lockout after repeated failed comparisons, and the comparison itself is a plain (non-constant-time) string inequality check, so an unauthenticated remote peer can send unlimited authentication attempts against these endpoints without penalty.

### Finding Description
Each of the following handlers implements the identical pattern in `try_parse_request`: [1](#0-0) 

```
let Some(password) = &self.auth else { return Err(Error::Http(400, ...)); };
let Some(auth_header) = preamble.headers.get("authorization") else { return Err(Error::Http(401, "Unauthorized".into())); };
if auth_header != password { return Err(Error::Http(401, "Unauthorized".into())); }
```

The identical gate appears verbatim in: [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) 

The documented threat model treats this token as a genuine credential: the OpenAPI spec states the header must be a "plain-text secret value that must exactly equal the node's configured password," and both signer and miner sample configs (`auth_token`) are explicitly described as secrets shared between the miner and signer. [7](#0-6) 

Two compounding weaknesses exist:
1. **No rate limiting / lockout on failed authorization attempts.** I searched the P2P/HTTP connection-handling code (`stackslib/src/net/connection.rs`, `stackslib/src/net/httpcore.rs`, `stackslib/src/net/api/mod.rs`) for any mechanism that tracks or throttles repeated authentication failures per-connection or per-peer; none of the `auth_token`-related code paths implement attempt counting, exponential backoff, or IP/connection banning tied specifically to failed `authorization` header checks — only generic connection/request-volume limits (`num_clients`, `max_http_clients`) exist, which are unrelated to credential-guessing protection.
2. **Non-constant-time comparison.** `auth_header != password` is a standard Rust `String`/`&str` inequality, which short-circuits on the first differing byte. This creates a timing side channel that, combined with unlimited retries, allows a remote attacker to recover the secret token byte-by-byte far faster than a full keyspace brute force, rather than needing outright volumetric flooding.

This is a direct analog of GHSA-9g3v-v24q-jj5p / CVE-2022-3273: a password-like credential check reachable by an unauthenticated remote party with no attempt-limiting control, breaking the intended equality "only the true secret should ever grant access within a bounded number of tries."

### Impact Explanation
Successful brute force / side-channel recovery of `auth_token` grants an unauthenticated remote attacker access to the same privileged surface as the legitimate miner/signer:
- `POST /v3/block_proposal` and `POST /v3/blocks/simulate/:block_id` — invoke block-validation/proposal machinery, a heavyweight compute path (bounded compute DoS / potential unauthorized triggering of validation cycles).
- `POST /v3/blocks/broadcast` (postblock_v3 authenticated path) — unauthorized write/broadcast of blocks bypassing the intended miner-only gate.
- `GET /v3/blocks/replay/:block_id`, `POST /v3/transactions/simulate`, `POST /v3/contracts/fast-call-read/...` — unauthorized invocation of expensive read/simulation endpoints.

This falls under "unauthenticated/unauthorized write to state" and "auth bypass" impact categories once the secret is recovered, and independently under "bounded compute DoS on a read/simulation endpoint" even absent full secret recovery, since unlimited guesses can be interleaved with resource-intensive requests.

### Likelihood Explanation
Likelihood is moderate: exploitation requires the operator to have configured `auth_token` (these endpoints return 400 and are effectively disabled if unset), and requires many network round-trips to exploit the timing channel or exhaust the keyspace. However, no privileged access, node secret key, or third-party key is needed — only network reachability to the node's RPC port, which is explicitly the deployment model described in the sample miner/signer configs.

### Recommendation
- Add per-source (or per-connection) failed-attempt counters for the `authorization` header check across all `auth_token`-gated handlers, with exponential backoff or temporary lockout after N consecutive failures.
- Replace the plain `!=` string comparison with a constant-time comparison (e.g., `subtle::ConstantTimeEq`) to eliminate the timing side channel.
- Consider rate-limiting authentication failures independently of general connection/request throttling (`num_clients`/`max_http_clients`), since those limits are not designed to mitigate credential-guessing.

### Proof of Concept
Conceptual (cannot be executed from this read-only environment):
1. Point repeated `authorization: <guess>` HTTP requests at `POST /v3/block_proposal` (or any of the six endpoints listed).
2. Observe that every request is answered independently — no attempt counter, delay, or connection ban occurs regardless of the number of failed guesses.
3. Measure response latency in `if auth_header != password` across guesses sharing successively longer correct prefixes; because Rust string inequality short-circuits at the first mismatched byte, correct-prefix guesses can be distinguished from incorrect ones by timing, allowing byte-at-a-time secret recovery instead of full-keyspace brute force — with no rate limit in place to stop the attempt loop.

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

**File:** stackslib/src/net/api/postblock_v3.rs (L103-111)
```rust
        // look for authorization header
        if let Some(password) = &self.auth {
            if let Some(auth_header) = preamble.headers.get("authorization") {
                if auth_header != password {
                    return Err(Error::Http(401, "Unauthorized".into()));
                }
                authenticated = true;
            }
        }
```

**File:** docs/rpc/openapi.yaml (L38-47)
```yaml
components:
  securitySchemes:
    rpcAuth:
      type: apiKey
      in: header
      name: authorization
      description: |
        Plain-text secret value that must exactly equal the node's
        configured password, which is set as `connection_options.auth_token`
        in the node's configuration file.
```
