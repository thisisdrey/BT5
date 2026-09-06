### Title
Unauthenticated block POST triggers network-wide relay regardless of `broadcast` flag - ([File: stackslib/src/net/api/postblock_v3.rs])

### Summary
`RPCPostBlockRequestHandler::try_handle_request` decides whether to queue a `NakamotoBlocksData` message for P2P-wide relay based solely on `data_resp.accepted`, ignoring `self.broadcast`. Since `try_parse_request` only requires authentication when `broadcast=1` is explicitly requested, an unauthenticated caller can submit a block with no `broadcast` query parameter and no `authorization` header, and if the block is accepted by the chainstate logic, it is still queued via `node.set_relay_message` for propagation to the whole network.

### Finding Description
In `try_parse_request` (lines 99-122), authentication is only enforced `if broadcast && !authenticated`; a plain POST with no `broadcast=1` query arg bypasses the auth check entirely, setting `self.broadcast = Some(false)` [1](#0-0) . That `broadcast` flag is passed into `Relayer::process_new_nakamoto_block_ext` only as an internal processing hint [2](#0-1) , but the actual relay-to-network decision in `try_handle_request` is:
```rust
if data_resp.accepted {
    node.set_relay_message(StacksMessageType::NakamotoBlocks(NakamotoBlocksData {
        blocks: vec![block],
    }));
}
``` [3](#0-2) 

This check uses only the *acceptance result* of block validation, not `self.broadcast`. Any remote, unauthenticated peer that can craft a validly-formed `NakamotoBlock` accepted by `process_new_nakamoto_block_ext` (e.g., a block that passes chainstate validation but the operator never intended to gossip, or simply any legitimately valid block relayed prematurely/out-of-band) causes the node to queue it for propagation to the entire P2P network via `node.set_relay_message`, exactly as if the request had been an authenticated `broadcast=1` request.

### Impact Explanation
This allows an unprivileged remote party to make an operator's node act as a relay/gossip amplifier for blocks it never authorized for broadcast, bypassing the `broadcast`/auth gate that was clearly intended to distinguish "just record this block locally" (unauthenticated upload) from "broadcast this to the whole network" (authenticated, explicit intent). It results in network-wide propagation of remotely-submitted data without the configured secret ever being consulted for the relay decision, matching the "network-wide propagation of forged/unintended data" Critical category. It does not require any privileged role, secret, or non-remote access — only that the submitted block is accepted by chainstate validation logic.

### Likelihood Explanation
Preconditions: RPC endpoint reachable (default configuration), a `NakamotoBlock` that will be accepted by `process_new_nakamoto_block_ext` (this can be a legitimately valid block for the current tip, which is trivial to obtain — the attacker need not forge chainstate-invalid data, since the flaw is in relay-gating, not block validation). Attacker cost is a single unauthenticated HTTP POST to `/v3/blocks/upload/` with no `broadcast` query string; the flag `self.broadcast=Some(false)` is set exactly as designed for this path [4](#0-3) , yet relay still occurs. This is repeatable per accepted block and requires no privileged role.

### Recommendation
Gate the `node.set_relay_message` call on both `data_resp.accepted` **and** `self.broadcast == Some(true)`, e.g.:
```rust
if data_resp.accepted && self.broadcast.unwrap_or(false) {
    node.set_relay_message(...)
}
```
This restores the intended equality that only authenticated/explicit-broadcast requests should trigger network-wide propagation.

### Proof of Concept
Rust test plan (net test, e.g. in `stackslib/src/net/api/tests/postblock_v3.rs` or an integration test using `TestPeer`):
1. Set up two `TestPeer`s connected via P2P, with `RPCPostBlockRequestHandler::new(Some(secret))` configured on peer_1 (auth configured but not enforced for non-broadcast requests).
2. Craft/mine a valid `NakamotoBlock` that will be accepted by peer_1's chainstate.
3. Build the request via `StacksHttpRequest::new_post_block_v3(host, &block)` (no `broadcast=1`, no `authorization` header — asserting handler ends up with `self.broadcast == Some(false)`).
4. Send the request to peer_1's RPC endpoint and assert `StacksBlockAcceptedData::accepted == true`.
5. Drive peer_1's P2P networking (e.g., `poll`/`step`) and assert that peer_2 receives a `StacksMessageType::NakamotoBlocks(NakamotoBlocksData{..})` message containing the block, proving `set_relay_message` fired and network-wide relay occurred despite `broadcast == Some(false)` and no authorization header being sent — i.e., `relay_occurred == accepted`, not `relay_occurred == authenticated_broadcast`.

### Citations

**File:** stackslib/src/net/api/postblock_v3.rs (L99-122)
```rust
        // if broadcast=1 is set, then the requester must be authenticated
        let mut broadcast = false;
        let mut authenticated = false;

        // look for authorization header
        if let Some(password) = &self.auth {
            if let Some(auth_header) = preamble.headers.get("authorization") {
                if auth_header != password {
                    return Err(Error::Http(401, "Unauthorized".into()));
                }
                authenticated = true;
            }
        }

        // see if broadcast=1 is set
        for (key, value) in form_urlencoded::parse(query.as_ref().unwrap_or(&"").as_bytes()) {
            if key == "broadcast" {
                broadcast = broadcast || value == "1";
            }
        }

        if broadcast && !authenticated {
            return Err(Error::Http(401, "Unauthorized".into()));
        }
```

**File:** stackslib/src/net/api/postblock_v3.rs (L159-174)
```rust
        let response = node
            .with_node_state(|network, sortdb, chainstate, _mempool, rpc_args| {
                let mut handle_conn = sortdb.index_handle_at_tip();
                let stacks_tip = network.stacks_tip.block_id();
                Relayer::process_new_nakamoto_block_ext(
                    &network.burnchain,
                    sortdb,
                    &mut handle_conn,
                    chainstate,
                    &stacks_tip,
                    &block,
                    rpc_args.coord_comms,
                    NakamotoBlockObtainMethod::Uploaded,
                    self.broadcast.unwrap_or(false),
                )
            })
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
