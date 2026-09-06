### Title
Auth/broadcast gate is decorative — `set_relay_message` fires on `accepted` alone, letting unauthenticated posts propagate to the P2P network - ([File: stackslib/src/net/api/postblock_v3.rs])

### Summary
`RPCPostBlockRequestHandler::try_parse_request` only rejects the request when `broadcast=1` is requested without valid `authorization` (lines 99-122), and the string comparison at line 116 (`value == "1"`) means any other value (e.g. `broadcast=true`) is silently treated as `false`. `try_handle_request` at line 198 then relays the block to the P2P network purely based on `data_resp.accepted`, with no check of `self.broadcast` or `authenticated` at all.

### Finding Description
In `try_parse_request` (stackslib/src/net/api/postblock_v3.rs:99-122), `broadcast` is computed from the query string using an exact `=="1"` match, and `authenticated` is set only if an `authorization` header matches the configured secret. The only enforcement is: if `broadcast==true` AND `authenticated==false`, return `401`. Critically, if the attacker simply omits the `broadcast` param (or sends `broadcast=true`, `broadcast=yes`, etc., which fail the `=="1"` check and evaluate as `false`), `broadcast` stays `false`, `authenticated` stays `false`, and the `401` gate at line 120 is never triggered — the request proceeds unauthenticated.

The parsed block is then processed in `try_handle_request` (lines 147-207) via `Relayer::process_new_nakamoto_block_ext(..., self.broadcast.unwrap_or(false))` (line 172), passing `broadcast=false` into chainstate processing. Regardless of that flag's value or of `authenticated`, the relay decision at line 198 is:
```rust
if data_resp.accepted {
    node.set_relay_message(StacksMessageType::NakamotoBlocks(NakamotoBlocksData { blocks: vec![block] }));
}
```
This condition checks only `data_resp.accepted` — a field derived solely from whether chainstate/`process_new_nakamoto_block_ext` accepted the block, not from `self.broadcast` or from `authenticated`. There is no `self.broadcast` or `authenticated` check anywhere in `try_handle_request`. The `broadcast`/`authenticated` variables computed in `try_parse_request` are used only to gate the `401` response for the *explicit* `broadcast=1` request path; they do not gate the relay call itself.

Therefore the documented invariant — "blocks relayed via `set_relay_message` == blocks that are both accepted AND authenticated-for-broadcast" — does not hold in this code. The actual invariant enforced is "blocks relayed == blocks accepted by chainstate," independent of authentication.

### Impact Explanation
Any unprivileged remote party who can reach the node's RPC port can POST a block to `/v3/blocks/upload/` with no `authorization` header and no (or a non-`"1"`) `broadcast` query parameter. If chainstate accepts the block (e.g., it is a validly-formed, validly-signed-per-consensus-rules Nakamoto block for a reachable chain tip — note that "accepted" here means chainstate/staging acceptance, not full validation bypass), `node.set_relay_message` unconditionally queues the block for propagation to the node's P2P peers, which then gossip it further. This is unauthenticated write/propagation of chain data to the network via an endpoint that is nominally gated by an authorization secret for broadcast — matching "network-wide propagation of forged/unauthorized data" and "unauthenticated write to state," Critical severity. It's repeatable per accepted block submission.

### Likelihood Explanation
No privileged role, secret, or special peer state is required — only that the node's RPC port is reachable (same precondition as any legitimate use of this endpoint) and that the submitted block passes chainstate's ordinary acceptance checks (which is the same bar a legitimate, properly-authorized submitter must also clear — the flaw is specifically that authorization is not required for the block to be relayed, only for it to be accepted; whether "accepted" alone is achievable by a fully attacker-crafted, unsigned block depends on chainstate/staging validation performed inside `process_new_nakamoto_block_ext`, which is out of scope per the audit rules — but regardless of how hard it is to get a block "accepted," the code confirms that once accepted, relay bypasses the authorization/broadcast gate entirely). Attacker cost is a single HTTP POST; this is trivially repeatable.

### Recommendation
Gate the relay call on both `authenticated` and the caller's requested `broadcast` intent, not solely on `accepted`. E.g., store `self.authenticated` alongside `self.broadcast` in `try_parse_request`, and change line 198 to `if data_resp.accepted && self.broadcast.unwrap_or(false) { ... }`, ensuring the `401` gate at line 120 is the only way to reach `broadcast==true`, so that unauthenticated requests can never result in relay to peers.

### Proof of Concept
```rust
// stackslib/src/net/api/tests/postblock_v3.rs (extend existing test module)
#[test]
fn test_unauthenticated_post_triggers_relay() {
    // 1. Construct RPCPostBlockRequestHandler with Some(secret) configured (auth required).
    // 2. Build a StacksHttpRequest POST to /v3/blocks/upload/ with:
    //    - NO "authorization" header
    //    - NO "broadcast" query param (or "broadcast=true")
    //    - body = a NakamotoBlock crafted/staged so that chainstate's
    //      process_new_nakamoto_block_ext will return Accepted (use test harness
    //      fixtures that already produce an accepted block in other postblock_v3 tests).
    // 3. Call handler.try_parse_request(...) -> assert Ok (no 401), and assert
    //    handler.broadcast == Some(false).
    // 4. Call handler.try_handle_request(preamble, contents, &mut node_state).
    // 5. Assert: node_state's relay message queue (StacksNodeState::set_relay_message
    //    side effect) DOES contain a StacksMessageType::NakamotoBlocks entry for this block,
    //    despite authenticated == false and broadcast == false.
    //
    // Expected per documented contract: relay message must NOT be set.
    // Actual (bug): relay message IS set, because try_handle_request's guard at line 198
    // only checks `data_resp.accepted`.
}
```