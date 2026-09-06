### Title
Unauthenticated block upload triggers network-wide relay of `NakamotoBlocks` regardless of `broadcast` flag - ([File: stackslib/src/net/api/postblock_v3.rs])

### Summary
`RPCPostBlockRequestHandler::try_handle_request` gates the call to `node.set_relay_message(StacksMessageType::NakamotoBlocks(...))` solely on `data_resp.accepted`, not on `self.broadcast`/authentication state. An attacker can POST a valid Nakamoto block to `/v3/blocks/upload/` with no `broadcast` query parameter (and thus no authorization header required), and if the block is independently accepted by `Relayer::process_new_nakamoto_block_ext`, the node will still queue it for relay to the rest of the network.

### Finding Description
`try_parse_request` correctly enforces that `broadcast=1` requires authentication: it returns `401 Unauthorized` if `broadcast && !authenticated` [1](#0-0) . It then stores `self.broadcast = Some(broadcast)`, which is `false` when the query string omits `broadcast=1` or sets it to anything else [2](#0-1) .

However, in `try_handle_request`, `self.broadcast` is only used to control whether `Relayer::process_new_nakamoto_block_ext` treats the block as "obtained via upload" for local processing/storage purposes [3](#0-2) . The decision to actually relay the message onward to the P2P network is made independently, based only on whether the block was accepted:

```rust
// should set to relay...
if data_resp.accepted {
    node.set_relay_message(StacksMessageType::NakamotoBlocks(NakamotoBlocksData {
        blocks: vec![block],
    }));
}
``` [4](#0-3) 

This means an unauthenticated caller (no `authorization` header, `broadcast` unset or `0`) can still cause `set_relay_message` to fire as long as the posted block passes `process_new_nakamoto_block_ext`'s acceptance criteria (i.e., it is a valid, well-formed Nakamoto block that can be stored). The intended invariant — that only authenticated requests with `broadcast=1` cause network propagation — is broken because the relay decision equals `data_resp.accepted == true` instead of `data_resp.accepted == true AND self.broadcast == Some(true) AND authenticated == true`.

### Impact Explanation
Any remote, unauthenticated party who can reach the node's RPC port can cause the node to relay an arbitrary (but validly-formed/accepted) Nakamoto block to its peers via the gossip layer, bypassing the intended authentication gate designed to restrict who can trigger network-wide propagation from this endpoint. This is a network-wide propagation of data via an unauthenticated request, matching the Critical category ("network-wide propagation of forged data" / unauthenticated bypass of an intended auth gate). It is repeatable per accepted block.

### Likelihood Explanation
No privileged role or secret is required — the endpoint is reachable by any peer with RPC access, `broadcast` simply needs to be omitted or `0` to skip the auth check entirely at `try_parse_request`, and the only requirement is that the block be independently "accepted" (i.e., a legitimately-constructed/valid block from the attacker's perspective, or a block the attacker relays from elsewhere) by chainstate logic. The cost to the attacker is a single HTTP POST.

### Recommendation
Gate the `set_relay_message` call on both acceptance and the authenticated broadcast intent, e.g.:
```rust
if data_resp.accepted && self.broadcast.unwrap_or(false) {
    node.set_relay_message(...)
}
```
Since `try_parse_request` already guarantees `broadcast == true` implies `authenticated == true`, checking `self.broadcast` at the relay site restores the intended equality.

### Proof of Concept
Add a test in `stackslib/src/net/api/tests/postblock_v3.rs` (or the module referenced by the audit) that:
1. Constructs `RPCPostBlockRequestHandler` with `auth = Some("secret")`.
2. Sends a well-formed `NakamotoBlock` via `try_parse_request` with `query = None` (no `broadcast` param) and no `authorization` header — this must succeed (200, unauthenticated allowed) because `broadcast` is `false`.
3. Calls `try_handle_request` with a `StacksNodeState` mock/stub where `process_new_nakamoto_block_ext` returns an "accepted" result.
4. Asserts that `node.set_relay_message` is **not** invoked (e.g., by checking `node`'s internal relay-message slot remains `None`).
5. Currently this assertion fails, because `set_relay_message` is called whenever `data_resp.accepted` is true — demonstrating the bypass.

### Citations

**File:** stackslib/src/net/api/postblock_v3.rs (L99-134)
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

        if Some(HttpContentType::Bytes) != preamble.content_type || preamble.content_type.is_none()
        {
            return Err(Error::DecodeError(
                "Invalid Http request: PostBlock takes application/octet-stream".to_string(),
            ));
        }

        let block = Self::parse_postblock_octets(body)?;

        self.block = Some(block);
        self.broadcast = Some(broadcast);
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
