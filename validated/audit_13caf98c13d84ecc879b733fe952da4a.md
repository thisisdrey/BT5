### Title
Unconditional P2P relay of uploaded Nakamoto blocks bypasses RPC secret gating - (File: stackslib/src/net/api/postblock_v3.rs)

### Summary
In `RPCPostBlockRequestHandler::try_parse_request`, the local `authenticated` flag is used only to gate whether `broadcast` may be set to `true`; it is never stored on `self` and has no other effect. In `try_handle_request`, the only use of `self.broadcast` is to control an internal chainstate-processing parameter, while the unconditional relay of the accepted block via `node.set_relay_message` happens regardless of authentication or broadcast status.

### Finding Description
`try_parse_request` computes `authenticated` and `broadcast` as independent local booleans [1](#0-0) . The only cross-check is `if broadcast && !authenticated { return Err(...) }`, which prevents `broadcast=true` from being set without a correct password, but does nothing else — `authenticated` is discarded after this point and never written to `self`. `self.broadcast` is set unconditionally to whatever `broadcast` resolved to (`self.broadcast = Some(broadcast);`) [2](#0-1) .

In `try_handle_request`, `self.broadcast.unwrap_or(false)` is passed as the sole authenticated-privilege-derived argument to `Relayer::process_new_nakamoto_block_ext` [3](#0-2) . Crucially, after this call, if the block is accepted (`data_resp.accepted`), the handler unconditionally queues the block for P2P relay via `node.set_relay_message(...)` — with no check of `self.broadcast`, `authenticated`, or any other auth-derived state [4](#0-3) .

Therefore, for a request with `authorization: password` (byte-exact correct) but no `broadcast=1` query argument: `authenticated=true`, `broadcast=false` — identical downstream behavior to an unauthenticated request with no `broadcast` argument (`authenticated=false`, `broadcast=false`), since `broadcast` is the only value threaded into any decision (the `process_new_nakamoto_block_ext` call), and relay-on-accept is unconditional either way. The equality holds: `authenticated` never leaks into any privilege decision distinct from `broadcast`.

### Impact Explanation
Any accepted uploaded block — whether the request was unauthenticated or authenticated-without-broadcast — is relayed to the P2P network via `set_relay_message`, matching the network-wide propagation of unauthenticated/unauthorized data described in the base Critical finding this question reinforces. The password's entire security value on this endpoint is limited to gating the `broadcast` bool passed into chainstate processing; it does not gate P2P relay at all.

### Likelihood Explanation
Preconditions: node configured with `self.auth=Some(password)` (RPC secret configured); attacker needs no secret to reach this code path and trigger relay — they only need the block to be accepted by chainstate validation (consensus-valid block), which is a normal, repeatable, remote-reachable POST to `/v3/blocks/upload/`.

### Recommendation
Gate `node.set_relay_message(...)` in `try_handle_request` on `self.broadcast` (or on true authentication), not solely on `data_resp.accepted`, so that unauthenticated/non-broadcast uploads are processed locally but not automatically relayed to the P2P network.

### Proof of Concept
Add a test in `stackslib::net::api::tests::postblock_v3` that constructs two requests against a handler with `auth = Some("password".into())`:
(a) headers include `authorization: password`, query string empty (no `broadcast`);
(b) no `authorization` header, query string empty.
Call `try_parse_request` for both and assert `handler.broadcast == Some(false)` in both cases. Then call `try_handle_request` for both against a mocked `StacksNodeState` where the block is accepted, and assert that `node.set_relay_message` is invoked identically in both cases (e.g., by inspecting the relay message queued on the mock `StacksNodeState`), demonstrating that authentication without `broadcast=1` provides no relay-gating benefit over no authentication at all.

### Citations

**File:** stackslib/src/net/api/postblock_v3.rs (L100-122)
```rust
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

**File:** stackslib/src/net/api/postblock_v3.rs (L133-136)
```rust
        self.block = Some(block);
        self.broadcast = Some(broadcast);
        Ok(HttpRequestContents::new().query_string(query))
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
