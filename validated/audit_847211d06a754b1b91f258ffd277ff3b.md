### Title
Non-constant-time authorization token comparison enables remote timing side-channel attack - (File: stackslib/src/net/api/postblock_v3.rs)

### Summary
`RPCPostBlockRequestHandler::try_parse_request` compares the attacker-supplied `authorization` header against the node's configured secret using the standard Rust `!=` operator on `String`/`&str`, which is not constant-time. A remote, unauthenticated attacker hitting `/v3/blocks/upload/?broadcast=1` can exploit per-byte comparison short-circuiting to incrementally recover the secret and eventually satisfy the auth check without ever holding it.

### Finding Description
At [1](#0-0) , the code does:
```rust
if let Some(password) = &self.auth {
    if let Some(auth_header) = preamble.headers.get("authorization") {
        if auth_header != password {
            return Err(Error::Http(401, "Unauthorized".into()));
        }
        authenticated = true;
    }
}
```
`auth_header != password` invokes `PartialEq` for `String`, whose default implementation compares lengths and then performs a byte-wise comparison that returns as soon as a mismatching byte is found (equivalent to `memcmp`-style short-circuiting). This means the time taken to reject a request is a function of how many leading bytes of `auth_header` match `password` — the more correct leading bytes an attacker supplies, the longer the comparison takes before failing.

A remote attacker with no knowledge of the secret can:
1. Send repeated POST requests to `/v3/blocks/upload/?broadcast=1` (a route that is remotely reachable and requires no other privilege), each with a crafted `authorization` header guessing one additional byte at a time.
2. Measure response latency (with enough repetitions to average out network jitter) to determine when a guessed byte is correct (longer processing before the 401) versus incorrect (faster 401).
3. Repeat byte-by-byte until the entire secret is reconstructed, then submit `authorization: <password>` to pass the check at line 106, setting `authenticated = true` and satisfying the `broadcast` gate at [2](#0-1) .

None of the existing guards (`Content-Length` checks, `MAX_PAYLOAD_LEN` check, content-type check) address this — they run either before or independently of the auth comparison and do not make the comparison constant-time. This same pattern (`auth_header != password`) is reused across other RPC handlers such as `postblock_proposal.rs`, `blocksimulate.rs`, `txsimulate.rs`, and `fastcallreadonly.rs`, and no constant-time comparison utility (e.g. `subtle::ConstantTimeEq`) is used anywhere in the codebase.

### Impact Explanation
Successful recovery of the secret allows an unauthenticated remote attacker to authenticate as a trusted operator on the `broadcast=1` path, letting them force the node to treat their submitted block as validated-and-authorized-for-relay input, i.e., an unauthenticated auth bypass leading to the ability to trigger `node.set_relay_message` and propagate attacker-supplied `NakamotoBlocksData` into the network relay path at [3](#0-2) . This matches the Critical category "auth bypass" / unauthenticated write leading to network-wide propagation of data via the relay mechanism, since it defeats the operator-configured secret gate meant to restrict who can trigger broadcast.

### Likelihood Explanation
The attacker needs no privileged role, no config, and no prior secret knowledge — only reachability to the node's RPC port, which is explicitly in-scope as remotely reachable. The attack is repeatable per byte and the endpoint accepts unlimited retries with no rate limiting or lockout visible in `try_parse_request`. Practical exploitation cost depends on distinguishing sub-microsecond/microsecond timing differences over a network, which requires many samples per byte to overcome jitter, but this is a well-documented feasible class of attack (remote timing side-channels), especially for local-network or co-located adversaries, and the number of guessable bytes (secret length) bounds total effort linearly rather than exponentially.

### Recommendation
Replace the `!=` comparison of the secret with a constant-time comparison, e.g., using the `subtle` crate's `ConstantTimeEq` on the byte representations of `auth_header` and `password`, or a manually implemented fixed-time byte-wise XOR-and-accumulate comparison that always processes the full length of the longer string regardless of mismatches. Apply the same fix consistently to all other handlers performing this pattern (`postblock_proposal.rs`, `blocksimulate.rs`, `txsimulate.rs`, `fastcallreadonly.rs`).

### Proof of Concept
Rust test plan in `stackslib::net::api::postblock_v3` tests module:
1. Construct an `RPCPostBlockRequestHandler` with `auth = Some("supersecretpassword".to_string())`.
2. Build a `HttpRequestPreamble` with varying `authorization` header values: one with 0 correct leading bytes, one with N/2 correct leading bytes, one with N-1 correct leading bytes (all still wrong overall), and time-average (over many repeated calls) the duration of `try_parse_request` reaching the `Err(Error::Http(401, ...))` branch at line 107.
3. Assert (or, since CI timing is noisy, statically assert via code-path reflection) that elapsed time increases monotonically with the number of correct leading bytes, demonstrating that `auth_header != password` at [4](#0-3)  is not constant-time — the reachable defect to fix.

### Citations

**File:** stackslib/src/net/api/postblock_v3.rs (L104-110)
```rust
        if let Some(password) = &self.auth {
            if let Some(auth_header) = preamble.headers.get("authorization") {
                if auth_header != password {
                    return Err(Error::Http(401, "Unauthorized".into()));
                }
                authenticated = true;
            }
```

**File:** stackslib/src/net/api/postblock_v3.rs (L120-122)
```rust
        if broadcast && !authenticated {
            return Err(Error::Http(401, "Unauthorized".into()));
        }
```

**File:** stackslib/src/net/api/postblock_v3.rs (L197-202)
```rust
        // should set to relay...
        if data_resp.accepted {
            node.set_relay_message(StacksMessageType::NakamotoBlocks(NakamotoBlocksData {
                blocks: vec![block],
            }));
        }
```
