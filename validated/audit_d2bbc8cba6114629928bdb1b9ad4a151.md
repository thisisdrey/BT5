### Title
Auth-gated `broadcast` flag is not enforced when queuing accepted blocks for network relay - ([File: stackslib/src/net/api/postblock_v3.rs])

### Summary
`RPCPostBlockRequestHandler::try_parse_request` only requires the RPC secret when the client explicitly sets `broadcast=1` in the query string, but `try_handle_request`'s trailing relay logic queues the block for network-wide gossip whenever the block is `accepted`, with no check of `self.broadcast` or the authenticated flag. This means the "requires secret to broadcast" guard does not actually gate whether the block gets relayed to other peers.

### Finding Description
In `try_parse_request` [1](#0-0) , `broadcast` defaults to `false` and authentication is only enforced `if broadcast && !authenticated`. An unauthenticated request with no `authorization` header and no `broadcast=1` query arg passes this check unimpeded (`broadcast=false`, `authenticated=false`), and `self.broadcast` is stored as `Some(false)`.

In `try_handle_request`, the block is passed to `Relayer::process_new_nakamoto_block_ext` with `self.broadcast.unwrap_or(false)` as one of its parameters [2](#0-1) . Regardless of what that internal `broadcast` flag controls inside `process_new_nakamoto_block_ext`, the outer relay-queueing logic is independent and unconditional:

```rust
// should set to relay...
if data_resp.accepted {
    node.set_relay_message(StacksMessageType::NakamotoBlocks(NakamotoBlocksData {
        blocks: vec![block],
    }));
}
``` [3](#0-2) 

This check only tests `data_resp.accepted` (whether chainstate/consensus accepted the block), never `self.broadcast` or `authenticated`. Thus any remote, unprivileged peer that submits a well-formed, chain-valid Nakamoto block to `/v3/blocks/upload/` — with no `authorization` header and no `broadcast=1` — will, if the block is accepted by `process_new_nakamoto_block_ext`, have it unconditionally queued via `node.set_relay_message`, which downstream code in `stackslib/src/net/mod.rs`/`rpc.rs` picks up to broadcast the message to the node's peers. The comment "if broadcast=1 is set, then the requester must be authenticated" implies the intended design was that only authenticated/broadcast-flagged submissions should be relayed onward, but the code that actually performs the relay-queue action does not check either condition. The auth gate at parse time therefore only prevents the caller from *requesting* the internal `broadcast` semantics of `process_new_nakamoto_block_ext` (whatever internal effect that flag has, e.g. immediate microblock/announcement side effects) — it does not prevent the handler from queuing the block for peer-to-peer relay.

### Impact Explanation
Any accepted (chain-valid) block submitted without the secret still causes the node to gossip it to its peers via the P2P relay queue, exactly matching the "requires secret to broadcast" guard being bypassed for the actual network propagation effect. This lets an unprivileged remote party use any single node's RPC endpoint as a relay amplifier for a block it already possesses, without needing the operator's configured secret — an unauthenticated write with network-wide propagation effect, repeatable per accepted block.

### Likelihood Explanation
The attacker needs only remote reachability to a node's RPC port and a validly-formed Nakamoto block (which the block-accept path validates via chainstate/consensus rules such as signer signatures, so it can't be a purely forged/garbage block, but the attacker does not need the RPC secret or any privileged role to trigger relay). This is trivially repeatable for every block the attacker wants relayed.

### Recommendation
Condition the `node.set_relay_message(...)` call on `self.broadcast.unwrap_or(false)` (and/or the `authenticated` state established in `try_parse_request`), so that unauthenticated submissions are only stored/validated locally and are never queued for outbound P2P relay. Alternatively, only allow relay-queueing when explicitly requested via the authenticated `broadcast=1` path, consistent with the intent of the existing auth gate.

### Proof of Concept
Add a test in `stackslib::net::api::tests::postblock_v3` that:
1. Constructs an `RPCPostBlockRequestHandler` with `auth = Some("secret".into())`.
2. Builds a valid `HttpRequestPreamble`/body for a well-formed, chain-acceptable `NakamotoBlock`, with no `authorization` header and query string `None` (no `broadcast=1`).
3. Calls `try_parse_request(...)` and asserts it succeeds (no 401), and that `handler.broadcast == Some(false)`.
4. Calls `try_handle_request(...)` against a `StacksNodeState` set up so `process_new_nakamoto_block_ext` accepts the block; assert the returned `StacksBlockAcceptedData.accepted == true`.
5. Assert that `node.get_relay_message()` (or equivalent relay-queue accessor in `StacksNodeState`) is `Some(StacksMessageType::NakamotoBlocks(NakamotoBlocksData { blocks: vec![block], .. }))`, proving relay queuing occurred despite no authentication and no `broadcast=1`.

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
