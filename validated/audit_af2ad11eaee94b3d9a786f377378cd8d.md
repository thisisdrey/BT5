### Title
Unauthenticated POST to `/v3/blocks/upload/` relays attacker's block to all P2P peers regardless of `broadcast=false` - ([File: stackslib/src/net/api/postblock_v3.rs])

### Summary
The `broadcast`/`authorization` gate in `try_parse_request` only controls whether the caller is required to authenticate; it does not actually gate the relay of the block to the P2P network. In `try_handle_request`, the decision to call `node.set_relay_message(...)` is based solely on `data_resp.accepted` (whether chainstate accepted the block), not on `self.broadcast`, so an unauthenticated caller who posts a valid/acceptable block with no `authorization` header and no `broadcast=1` still gets it relayed to every peer.

### Finding Description
In `try_parse_request` (`stackslib/src/net/api/postblock_v3.rs` lines 99-122), the handler requires authentication only when `broadcast=1` is requested: `if broadcast && !authenticated { return Err(...401...) }`. An unauthenticated request with no `broadcast` query parameter is permitted, setting `self.broadcast = Some(false)` at line 134.

In `try_handle_request`, `self.broadcast.unwrap_or(false)` is passed into `Relayer::process_new_nakamoto_block_ext` (line 172), which presumably affects some internal broadcast behavior of that function. But the relay-to-peers decision that actually matters for network propagation is made independently at lines 197-202:
```rust
// should set to relay...
if data_resp.accepted {
    node.set_relay_message(StacksMessageType::NakamotoBlocks(NakamotoBlocksData {
        blocks: vec![block],
    }));
}
```
This condition checks only `data_resp.accepted` (derived from `accepted.is_accepted()`), completely ignoring `self.broadcast`. Therefore, any block that chainstate accepts as valid — submitted by an unauthenticated, unprivileged caller — is queued via `node.set_relay_message` for relay to the node's P2P peers, exactly as if it had come through the authenticated `broadcast=1` path.

The intended invariant "blocks relayed to peers == blocks submitted via the broadcast-authenticated path only" is broken: acceptance alone, not authentication/broadcast intent, gates network-wide relay.

### Impact Explanation
Any accepted Nakamoto block posted by an unauthenticated remote attacker to `/v3/blocks/upload/` is relayed to the node's peers, causing network-wide propagation of a block that was never intended to be broadcast by an authorized party. This lets an unprivileged remote attacker use any node's RPC endpoint as a relay amplifier for arbitrary (but chainstate-valid) blocks, undermining the authenticated-broadcast gate's purpose and enabling repeatable network flooding/propagation of attacker-chosen blocks through unauthenticated victim nodes. This matches the "network-wide propagation of forged data" Critical category, since the `broadcast` authorization check is rendered meaningless for controlling P2P relay.

### Likelihood Explanation
Preconditions are minimal: the attacker needs only network access to a node's RPC port and a block that will pass chainstate acceptance checks (e.g., a legitimately-mined/valid block they already know about, or one they can construct that satisfies consensus validation). No authentication, secret, or privileged role is required — precisely the unauthenticated path the `broadcast` flag was meant to gate against. The attack is cheap and repeatable per accepted block.

### Recommendation
Gate the relay call on `self.broadcast` (or an explicit "authenticated" flag) in addition to `data_resp.accepted`, e.g.:
```rust
if data_resp.accepted && self.broadcast.unwrap_or(false) {
    node.set_relay_message(...);
}
```
so that relay to peers only occurs when the request was actually authenticated for broadcast, matching the intent of the `authorization`/`broadcast` gate in `try_parse_request`.

### Proof of Concept
Rust test in `stackslib/src/net/api/tests/postblock_v3.rs` (or new test):
1. Construct `RPCPostBlockRequestHandler::new(Some("secret".into()))`.
2. Build an HTTP POST request to `/v3/blocks/upload/` with no `authorization` header, no `broadcast` query param, and a valid `NakamotoBlock` body that will be accepted by a mocked/test chainstate (`Relayer::process_new_nakamoto_block_ext` returns an "accepted" result).
3. Call `try_parse_request` — assert it succeeds (no 401) and `handler.broadcast == Some(false)`.
4. Call `try_handle_request` with a `StacksNodeState` wired to a spy/mock for `set_relay_message`.
5. Assert: despite `broadcast == Some(false)` and no authentication, `node.set_relay_message` was invoked with `StacksMessageType::NakamotoBlocks` containing the posted block — confirming the relay-gate bypass at lines 197-202.