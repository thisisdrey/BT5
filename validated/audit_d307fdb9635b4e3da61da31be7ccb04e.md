### Title
Unauthenticated block relay: `node.set_relay_message` fires on chainstate acceptance regardless of `broadcast` authorization - ([File: stackslib/src/net/api/postblock_v3.rs])

### Summary
`try_parse_request` correctly requires authentication when `broadcast=1` is requested, but `try_handle_request` never actually uses that authorization state to gate the relay action. The relay call `node.set_relay_message(...)` is triggered solely by `data_resp.accepted` (i.e., whether the block passed chainstate validation and got stored), completely independent of `self.broadcast`/authentication. The `broadcast` flag is only forwarded into `Relayer::process_new_nakamoto_block_ext` as a processing parameter, not used to condition the relay decision at the HTTP handler level.

### Finding Description
In `stackslib/src/net/api/postblock_v3.rs`:
- `try_parse_request` (lines 99-122) enforces that an unauthenticated caller cannot set `broadcast=1`; if unauthenticated, `self.broadcast` ends up `Some(false)`.
- `try_handle_request` (lines 159-173) calls `Relayer::process_new_nakamoto_block_ext(..., self.broadcast.unwrap_or(false))`, passing the (possibly false/unauthenticated) broadcast flag into chainstate processing.
- The result is captured as `data_resp.accepted = accepted.is_accepted()` (line 188), which reflects only whether the block passed chainstate validation and got persisted — not whether the request was authorized to broadcast.
- Lines 197-202: `if data_resp.accepted { node.set_relay_message(StacksMessageType::NakamotoBlocks(...)) }` — this is the sole gate for triggering network-wide relay, and it checks only `data_resp.accepted`, never `self.broadcast` or the `authenticated` state computed in `try_parse_request`.

So the code conflates "accepted-and-stored-locally" with "authorized-to-relay-network-wide," exactly as hypothesized: `node.set_relay_message` fires whenever the block is chainstate-valid, whether or not the caller supplied `broadcast=1` with valid authorization. Any unauthenticated peer who submits a chainstate-valid, already-signed Nakamoto block (e.g., one they independently obtained/observed rather than one they forged, since forging a block that passes signer/chainstate validation is out of scope) to this endpoint without any `authorization` header and without `broadcast=1` will still cause the node to schedule that block for relay to its p2p peers.

### Impact Explanation
This breaks the intended access-control semantics of the `broadcast` parameter/auth-gate: the auth requirement on `broadcast=1` becomes a no-op with respect to actual network propagation, since propagation is driven only by chainstate acceptance. An unprivileged remote party can use any node's unauthenticated `/v3/blocks/upload/` endpoint as a forced relay/amplification vector for any chainstate-valid block, bypassing the operator's intent to restrict who can trigger network-wide relay through this RPC surface. This is a real authorization-bypass on the relay decision (network-wide propagation triggered without required authorization), matching the "network-wide propagation ... / auth bypass" Critical category, though it is bounded to already chainstate-valid blocks (not arbitrary forged data) — forging a signer-valid block itself is out of scope per the rules.

### Likelihood Explanation
Preconditions: the target node's RPC port must be reachable (default for any Stacks node), no admin secret/role is required, and the attacker just needs a chainstate-valid Nakamoto block (which they can obtain by observing the network or from any peer) and can submit it with a bare `POST /v3/blocks/upload/` request, no `authorization` header, no `broadcast` query arg. This is repeatable for every block the attacker can obtain, at zero cost per request beyond a normal HTTP POST.

### Recommendation
Gate the `node.set_relay_message(...)` call in `try_handle_request` on `self.broadcast == Some(true)` (i.e., only relay when the caller was both authenticated and explicitly requested broadcast), in addition to `data_resp.accepted`. Chainstate acceptance should continue to drive local storage, but the network-wide relay decision must respect the authorization already enforced in `try_parse_request`.

### Proof of Concept
Rust test plan in the `stackslib` net API test harness (pattern similar to existing tests in `stackslib/src/net/api/postblock_v3.rs`'s module or `stackslib/src/net/tests/relay/`):
1. Construct an `RPCPostBlockRequestHandler::new(Some("secret".into()))` (auth configured).
2. Build a chainstate-valid `NakamotoBlock` (via existing test helpers used elsewhere for Nakamoto block construction/signing).
3. Call `try_parse_request` with no `authorization` header and no `broadcast` query string — assert it succeeds (no 401) and `self.broadcast == Some(false)`.
4. Call `try_handle_request` against a `StacksNodeState` wired to a chainstate/sortdb fixture where the block will be accepted by `Relayer::process_new_nakamoto_block_ext`.
5. Instrument/inspect `node.set_relay_message` (or the underlying relay-message queue/state) and assert it was invoked with `StacksMessageType::NakamotoBlocks(...)` for this block, despite `self.broadcast == Some(false)` and no authentication — demonstrating the broken equality: relay fired without `self.broadcast == Some(true)` and `authenticated == true`. [1](#0-0) [2](#0-1)

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

**File:** stackslib/src/net/api/postblock_v3.rs (L159-202)
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

        let data_resp = match response {
            Ok(accepted) => {
                debug!(
                    "Received POSTed Nakamoto block {}/{}: {:?}",
                    &block.header.consensus_hash,
                    &block.header.block_hash(),
                    &accepted
                );
                StacksBlockAcceptedData {
                    accepted: accepted.is_accepted(),
                    stacks_block_id: block.block_id(),
                }
            }
            Err(e) => {
                return e.try_into_contents().map_err(NetError::from);
            }
        };

        // should set to relay...
        if data_resp.accepted {
            node.set_relay_message(StacksMessageType::NakamotoBlocks(NakamotoBlocksData {
                blocks: vec![block],
            }));
        }
```
