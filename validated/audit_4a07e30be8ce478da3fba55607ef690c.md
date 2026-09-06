### Title
Unauthenticated POST to `/v3/blocks/upload/` triggers P2P relay via `set_relay_message`, bypassing the `broadcast&&authenticated` gate - ([File: stackslib/src/net/api/postblock_v3.rs])

### Summary
`RPCPostBlockRequestHandler::try_parse_request` correctly rejects a request that sets `broadcast=1` without authentication, but this gate only protects the `self.broadcast` flag passed into `process_new_nakamoto_block_ext`. The subsequent relay-to-peers decision in `try_handle_request` is keyed solely on `data_resp.accepted`, never on `authenticated` or even on `self.broadcast`, so any accepted block is relayed to the network regardless of whether the uploader supplied the secret.

### Finding Description
In `try_parse_request` [1](#0-0) , `authenticated` is a local variable that is discarded after the function returns — it is not stored on `self` and never reaches `try_handle_request`. The only enforcement using it is the check `if broadcast && !authenticated { return Err(...) }`, which prevents `self.broadcast` from being `true` unless the caller supplied the correct `authorization` header.

In `try_handle_request`, the block is processed via `Relayer::process_new_nakamoto_block_ext(..., self.broadcast.unwrap_or(false))` [2](#0-1) . Whatever that function returns as "accepted" is unconditionally used to decide relay:
```rust
if data_resp.accepted {
    node.set_relay_message(StacksMessageType::NakamotoBlocks(NakamotoBlocksData {
        blocks: vec![block],
    }));
}
``` [3](#0-2) 

This `if` statement does not reference `self.broadcast` or any authentication state at all. So an attacker who omits both the `authorization` header and the `broadcast` query parameter still has `self.broadcast = Some(false)` passed into `process_new_nakamoto_block_ext`, but if that internal function's own acceptance logic (which is chain-validity based, not access-control based) reports the block as accepted, `try_handle_request` relays it to the peer network exactly as if the request had been an authenticated broadcast request.

The intended invariant — relay to peers should require `broadcast && authenticated` — is broken because the relay trigger at lines 198-201 depends only on `accepted`.

### Impact Explanation
Any remote, unauthenticated client that can reach the RPC port can cause the node to gossip a `NakamotoBlocksData` message to its P2P peers without ever supplying the node's configured secret. While the block content itself must still pass `process_new_nakamoto_block_ext`'s validity checks (so arbitrary forged garbage would not be "accepted"), the access-control intent of the `auth` field — that only holders of the secret may cause this node to relay/broadcast blocks via this endpoint — is bypassed entirely. This turns any node with `auth` configured into an unauthenticated relay/amplification point for any valid Nakamoto block the attacker can obtain (e.g., one it saw on the wire or a colluding miner supplied off-band), which matches the "network-wide propagation" pattern called out as Critical, since the intended authorization boundary for triggering propagation is not actually enforced by the code that performs the propagation.

### Likelihood Explanation
Preconditions: the node has `auth = Some(password)` configured (secret set), and the RPC port is remotely reachable — both realistic, default-adjacent configurations. The attacker needs no credentials, no peer identity, and no StackerDB slot — only a valid, already-well-formed Nakamoto block to submit (which does not need to be self-forged; it can be a legitimately signed block obtained through any channel). The request is trivial to construct and repeatable for every block the attacker can obtain, at zero cost beyond a single HTTP POST.

### Recommendation
Store the `authenticated` result on `self` (or thread it into the returned `HttpRequestContents`) during `try_parse_request`, and in `try_handle_request` gate the `node.set_relay_message(...)` call on `self.broadcast.unwrap_or(false) && authenticated`, not merely on `data_resp.accepted`. This restores the intended equality that relay-to-peers only happens when the uploader passed the `broadcast && authenticated` check.

### Proof of Concept
Rust test in `stackslib/src/net/api/postblock_v3.rs` test module (or an integration test using `StacksNodeState`):
1. Construct `RPCPostBlockRequestHandler::new(Some("12345".to_string()))`.
2. Build a request via `StacksHttpRequest::new_post_block_v3(host, &block)` (no `authorization` header, no `broadcast` query arg) using a `block` that is known to be acceptable by `process_new_nakamoto_block_ext` under the test's chainstate fixture (e.g. a properly signed Nakamoto block in the test harness used elsewhere in `postblock_v3.rs`'s existing tests).
3. Call `try_parse_request` and confirm it succeeds with `self.broadcast == Some(false)`.
4. Call `try_handle_request` against a `StacksNodeState` whose `process_new_nakamoto_block_ext` returns an accepted result.
5. Assert that `node`'s outgoing relay message set now contains a `StacksMessageType::NakamotoBlocks` entry (i.e., `set_relay_message` was invoked) — demonstrating relay occurred despite `authenticated == false` and `broadcast == false`.

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

**File:** stackslib/src/net/api/postblock_v3.rs (L159-173)
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
