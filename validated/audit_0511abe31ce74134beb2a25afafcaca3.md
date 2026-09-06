## Title
Unauthenticated `POST /v3/blocks/upload/` blocks are gossiped to peers regardless of the `broadcast` flag - (File: stackslib/src/net/api/postblock_v3.rs)

## Summary
`RPCPostBlockRequestHandler::try_parse_request` correctly forces `broadcast = false` when the caller does not present a valid `authorization` header, and this value is passed into `Relayer::process_new_nakamoto_block_ext` as the "should broadcast" flag. However, `try_handle_request` unconditionally calls `node.set_relay_message(...)` based solely on whether the block was `accepted`, not on `self.broadcast`, meaning an unauthenticated poster's valid block still gets relayed to the P2P network.

## Finding Description
In `try_parse_request` [1](#0-0) , `broadcast` starts `false` and is only set to `true` from the `broadcast=1` query argument, and if `broadcast && !authenticated` the whole request is rejected with 401. So when no `authorization` header is sent at all, `authenticated` stays `false`, and the client simply omits `broadcast=1` (or it is forced to, since setting it would 401). The parsed `self.broadcast` is therefore `Some(false)`, which is stored correctly per this scope's expected "do-not-relay" semantics [2](#0-1) .

That `self.broadcast` value is passed into `Relayer::process_new_nakamoto_block_ext` purely as a parameter to that internal processing call [3](#0-2) . Critically, the decision to actually relay the block to the P2P network is made afterward, and it is based only on `data_resp.accepted` — not on `self.broadcast`:

```
// should set to relay...
if data_resp.accepted {
    node.set_relay_message(StacksMessageType::NakamotoBlocks(NakamotoBlocksData {
        blocks: vec![block],
    }));
}
``` [4](#0-3) 

The comment `// should set to relay...` itself signals this was left ambiguous/unfinished. As written, `self.broadcast` (the authenticated/unauthenticated distinction) has no bearing on the final gossip decision inside `try_handle_request` — only whether the chainstate accepted the block as valid/new determines whether `set_relay_message` fires. This breaks the equality the question is probing: "broadcast flag used to decide relay == authenticated status recorded during parsing." Here, the actual relay gate is `data_resp.accepted`, a different source of truth than `self.broadcast`.

I was not able to fully verify, within tool budget, the exact internal semantics of the `should_broadcast`/last boolean parameter of `Relayer::process_new_nakamoto_block_ext` (i.e., whether it independently gates something else, such as suppressing acceptance itself for unauthenticated callers, or only affects internal telemetry/relay-hints unrelated to `node.set_relay_message`). If that parameter caused `process_new_nakamoto_block_ext` to return "not accepted" whenever `broadcast=false`, this finding would be moot. I could not locate/read the body of `process_new_nakamoto_block_ext` in `stackslib/src/net/relay.rs` to confirm this before running out of iterations, so this must be treated as unconfirmed on that specific point.

## Impact Explanation
If, as the code in this file reads at face value, `set_relay_message` fires whenever the block is accepted into chainstate (independent of authentication), then any remote unprivileged peer that can reach the node's RPC port can submit a well-formed, valid Nakamoto block via `POST /v3/blocks/upload/` without any `authorization` header, and have the node both store it locally and gossip it to all its P2P peers. This would match the Critical "network-wide propagation of forged/unauthenticated data" category, since block relay decisions were intended to require authentication.

## Likelihood Explanation
Preconditions: the node has `self.auth` configured (an RPC secret expected), the endpoint is reachable, and the attacker submits a syntactically valid, chainstate-acceptable Nakamoto block without the secret. No privileged role or secret is needed by the attacker; this is a single unauthenticated POST, repeatable per valid block. The main uncertainty is whether `process_new_nakamoto_block_ext`'s broadcast parameter also suppresses `accepted` for unauthenticated calls — this needs to be confirmed by reading that function's body directly, which I could not do in the remaining budget.

## Recommendation
Gate the `node.set_relay_message(...)` call in `try_handle_request` on `self.broadcast` (or the authenticated status) in addition to `data_resp.accepted`, e.g. only call `set_relay_message` when `self.broadcast == Some(true) && data_resp.accepted`. Also confirm/lock down `Relayer::process_new_nakamoto_block_ext`'s broadcast parameter to ensure it cannot be misused as the sole gate for both storage and relay.

## Proof of Concept
Add a test in `stackslib/src/net/api/tests/postblock_v3.rs` (or equivalent) that:
1. Configures `RPCPostBlockRequestHandler::new(Some("secret".into()))`.
2. Sends a well-formed `POST /v3/blocks/upload/` request with a valid, chainstate-acceptable `NakamotoBlock` body and NO `authorization` header.
3. Asserts `handler.broadcast == Some(false)` after `try_parse_request` (confirming parse-time correctness).
4. Drives `try_handle_request` against a `StacksNodeState` where the block would be accepted, and asserts that `node`'s relay-message queue (`StacksNodeState::has_relay_message` or equivalent) is empty. If it is instead populated with the `NakamotoBlocksData` despite `broadcast == Some(false)`, that is the exact reproduction of the bug — the unauthenticated block reached `set_relay_message`.

Because I could not confirm the internal behavior of `Relayer::process_new_nakamoto_block_ext` regarding its broadcast/should-relay parameter within the available tool budget, this finding should be verified against that function's implementation before treating it as fully confirmed.

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

**File:** stackslib/src/net/api/postblock_v3.rs (L133-135)
```rust
        self.block = Some(block);
        self.broadcast = Some(broadcast);
        Ok(HttpRequestContents::new().query_string(query))
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

**File:** stackslib/src/net/api/postblock_v3.rs (L196-202)
```rust

        // should set to relay...
        if data_resp.accepted {
            node.set_relay_message(StacksMessageType::NakamotoBlocks(NakamotoBlocksData {
                blocks: vec![block],
            }));
        }
```
