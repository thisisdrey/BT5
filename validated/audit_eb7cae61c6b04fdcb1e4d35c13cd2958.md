### Title
Unauthenticated POST to `/v3/blocks/upload/` triggers network-wide block relay regardless of `broadcast` flag or authentication - (File: stackslib/src/net/api/postblock_v3.rs)

### Summary
`RPCPostBlockRequestHandler::try_handle_request` calls `node.set_relay_message(StacksMessageType::NakamotoBlocks(...))` solely based on `data_resp.accepted`, with no check on `self.broadcast` or the authentication state established in `try_parse_request`. As a result, a remote unauthenticated caller can POST a validly-formed block without `broadcast=1` and without an `authorization` header, have it locally accepted, and still trigger relay to the rest of the network.

### Finding Description
`try_parse_request` only enforces authentication when the caller explicitly requests `broadcast=1`: [1](#0-0) 
If the caller omits `broadcast` (or sets it to something other than `1`), `broadcast` stays `false` and `authenticated` is never checked, so the request passes with no credentials at all. `self.broadcast` is then stored as `Some(false)`: [2](#0-1) 

In `try_handle_request`, `self.broadcast` is passed into `Relayer::process_new_nakamoto_block_ext` as the `obtain`/broadcast-intent flag: [3](#0-2) 

But the decision to call `node.set_relay_message(...)` — which queues the block for relay to the P2P network — is made independently, gated only on `data_resp.accepted`: [4](#0-3) 

There is no re-check of `self.broadcast` or of whether the request was authenticated at this point. Any block that chainstate validation considers locally acceptable (i.e., cryptographically/structurally valid and extending known chainstate) will be relayed, whether or not the poster ever asked for broadcast or authenticated. This breaks the intended equality that network propagation should require `broadcast == Some(true) && authenticated == true`; instead, propagation only requires `data_resp.accepted == true`.

### Impact Explanation
An unprivileged remote attacker who can reach the node's RPC port can submit a block they did not need to authenticate for, and have the node relay it to its P2P peers as if it came from an authenticated/broadcast-intended source. This is an unauthenticated write causing network-wide propagation of data the operator did not intend to broadcast (e.g., an operator running a private/staging node that only wants to accept blocks locally for validation, not relay them). This matches the "network-wide propagation of forged data" / "unauthenticated write" Critical impact category, since the relay gate that was supposed to require authentication is bypassed entirely.

### Likelihood Explanation
The only preconditions are: the node has an open `/v3/blocks/upload/` RPC endpoint (standard for Nakamoto-capable nodes), and the attacker can construct a `NakamotoBlock` that chainstate accepts (extends a stacks tip the node knows, is well-formed, and passes `process_new_nakamoto_block_ext`'s validation). No RPC secret, peer key, or StackerDB slot is needed. This is directly reachable by any remote peer with no privileged role and is repeatable per valid block.

### Recommendation
Gate `node.set_relay_message(...)` on both `data_resp.accepted` and `self.broadcast.unwrap_or(false)` (which is already only `true` when `try_parse_request` verified authentication), e.g.:
```rust
if data_resp.accepted && self.broadcast.unwrap_or(false) {
    node.set_relay_message(...);
}
```

### Proof of Concept
Rust test plan in `stackslib/src/net/api/postblock_v3.rs` tests module:
1. Construct a `RPCPostBlockRequestHandler::new(Some("secret".into()))`.
2. Build a `HttpRequestPreamble` with no `authorization` header and query string without `broadcast=1`.
3. Call `try_parse_request` with a validly-serialized `NakamotoBlock` body — confirm it succeeds (no 401) since `broadcast=false` skips the auth check, and `self.broadcast == Some(false)`.
4. Call `try_handle_request` against a `StacksNodeState` fixture where `process_new_nakamoto_block_ext` returns an "accepted" result.
5. Assert that `node.set_relay_message` was invoked despite `self.broadcast == Some(false)` and no authentication — demonstrating the relay gate is bypassed. The assertion should fail on a fixed version (where relay is additionally gated on `self.broadcast.unwrap_or(false)`) but pass (bug present) on current code at stackslib/src/net/api/postblock_v3.rs lines 197-202.

### Citations

**File:** stackslib/src/net/api/postblock_v3.rs (L113-122)
```rust
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

**File:** stackslib/src/net/api/postblock_v3.rs (L131-136)
```rust
        let block = Self::parse_postblock_octets(body)?;

        self.block = Some(block);
        self.broadcast = Some(broadcast);
        Ok(HttpRequestContents::new().query_string(query))
    }
```

**File:** stackslib/src/net/api/postblock_v3.rs (L159-177)
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
            .map_err(|e| {
                StacksHttpResponse::new_error(&preamble, &HttpError::new(400, e.to_string()))
            });
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
