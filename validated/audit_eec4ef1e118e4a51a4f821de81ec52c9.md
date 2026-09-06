Based on the code I retrieved from `stackslib/src/net/api/postblock_v3.rs`, the vulnerability claim is confirmed by direct inspection of `try_handle_request`.

### Title
Unauthenticated broadcast of arbitrary blocks via unconditional `set_relay_message` call bypassing the `broadcast=1`/auth gate - (File: stackslib/src/net/api/postblock_v3.rs)

### Summary
`RPCPostBlockRequestHandler::try_parse_request` correctly enforces that `broadcast=1` requires a valid `authorization` header, storing the result in `self.broadcast`. However, `try_handle_request` never consults `self.broadcast` when deciding whether to relay the block to the network; it calls `node.set_relay_message(StacksMessageType::NakamotoBlocks(...))` solely based on `data_resp.accepted`, so any accepted block is queued for network-wide relay regardless of authentication or the broadcast flag.

### Finding Description
The intended invariant is: *block relayed to the network == request carried `broadcast=1` AND the correct auth header*. This is enforced in `try_parse_request`: [1](#0-0) 
which sets `authenticated`/`broadcast` and rejects with 401 only if `broadcast && !authenticated`. This flag is stored into `self.broadcast` at line 134 and later passed into `Relayer::process_new_nakamoto_block_ext` as the `broadcast` argument at line 172, controlling internal broadcast/coordinator behavior for that call.

But separately, in `try_handle_request`, after the block is processed and found accepted, the handler unconditionally does: [2](#0-1) 
This code path checks only `data_resp.accepted`, never `self.broadcast`. `node.set_relay_message` queues the message to be relayed to the P2P network (as used identically by the authenticated broadcast paths in `postblock.rs`, `postmicroblock.rs`, `poststackerdbchunk.rs`, `posttransaction.rs`). Consequently, an unauthenticated, non-broadcast POST to `/v3/blocks/upload/` that is accepted by chainstate validation still results in the block being queued for network relay — exactly the same effect as a properly-authenticated `broadcast=1` request.

The `self.auth` gate only blocks requests that explicitly request `broadcast=1`; a request with no `authorization` header and no `broadcast=1` query arg sails through `try_parse_request` (since `broadcast` stays `false`, so the `broadcast && !authenticated` check never fires) and reaches `try_handle_request`, which then relays it anyway.

### Impact Explanation
Any remote unprivileged party — no RPC secret, no auth header — can craft a well-formed `NakamotoBlock` (that would independently pass `Relayer::process_new_nakamoto_block_ext`'s acceptance checks, i.e. a chain-valid block for the current tip/consensus hash) and POST it to `/v3/blocks/upload/` with no `authorization` header and no `broadcast` query parameter. If accepted, the node will call `set_relay_message`, causing the block to be relayed to the node's peers exactly as if the request had been authenticated with `broadcast=1`. This is a network-wide propagation bypass of the auth gate that was specifically designed to control who can trigger relay — matching the Critical category "network-wide propagation of forged data / auth bypass." The action is repeatable per accepted block.

### Likelihood Explanation
Preconditions: the node must have `self.auth = Some(password)` configured (i.e., an RPC secret is set, presumably to restrict broadcast triggering), and the attacker needs a block that chainstate will actually accept (i.e., a validly-signed/committed Nakamoto block for a known sortition — this is not "any bytes," it must pass `Relayer::process_new_nakamoto_block_ext`'s validation, which is out-of-scope consensus internals, but simply relaying an already-valid/known block without authorization is still the bypass here). Attacker cost is a single POST with a public `/v3/blocks/upload/` HTTP endpoint, requiring only network connectivity to the node's RPC port — no secret, no privileged role, remotely reachable, fully repeatable per valid block.

### Recommendation
Gate the `set_relay_message` call on `self.broadcast` in addition to `data_resp.accepted`:
```rust
if data_resp.accepted && self.broadcast.unwrap_or(false) {
    node.set_relay_message(...)
}
```
so relay only occurs when the request was both accepted and explicitly authenticated with `broadcast=1`, restoring the intended equality.

### Proof of Concept
In `stackslib/src/net/api/tests::postblock_v3` (or a new test module), construct a `RPCPostBlockRequestHandler` with `auth = Some("secret".to_string())`. Build an HTTP POST to `/v3/blocks/upload/` with no `authorization` header and no `broadcast` query string, body containing a serialized `NakamotoBlock` that the mocked/stub `with_node_state`/`process_new_nakamoto_block_ext` path will report as accepted (mock `StacksNodeState` similar to existing tests in this file's test module). Call `try_parse_request` (assert it succeeds, since `broadcast=false` and `authenticated=false` don't trigger the 401 branch) then `try_handle_request`, and assert that `node`'s recorded relay message (via a spy on `set_relay_message`, e.g., checking `StacksNodeState`'s internal relay-message field afterward) equals `StacksMessageType::NakamotoBlocks(NakamotoBlocksData { blocks: vec![block] })`, despite `self.broadcast == Some(false)` — demonstrating the broken equality directly at line 198-202 of `stackslib/src/net/api/postblock_v3.rs`.

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

**File:** stackslib/src/net/api/postblock_v3.rs (L197-202)
```rust
        // should set to relay...
        if data_resp.accepted {
            node.set_relay_message(StacksMessageType::NakamotoBlocks(NakamotoBlocksData {
                blocks: vec![block],
            }));
        }
```
