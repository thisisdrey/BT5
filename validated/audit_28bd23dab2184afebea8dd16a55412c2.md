### Title
Unauthenticated block upload endpoint always relays accepted blocks to the network regardless of the `broadcast` flag/auth gate - ([File: stackslib/src/net/api/postblock_v3.rs])

### Summary
`RPCPostBlockRequestHandler::try_parse_request` only enforces the `authorization` check when `broadcast=1` is requested, and with no auth configured (`self.auth == None`) that check can never succeed. However, `try_handle_request` unconditionally calls `node.set_relay_message(...)` whenever the posted block is accepted, completely independent of `self.broadcast`/authentication status, so the "broadcast" authorization gate is cosmetic — any accepted block is queued for network relay regardless.

### Finding Description
In `try_parse_request` (stackslib/src/net/api/postblock_v3.rs:99-122), `authenticated` is only ever set inside `if let Some(password) = &self.auth`, so with `self.auth = None` it stays `false` for every request. The 401 gate at line 120 (`if broadcast && !authenticated`) only rejects requests where the caller explicitly set `broadcast=1`. A request with `broadcast` absent or `0` passes this gate unconditionally (auth or not), and `self.broadcast` is set to `false`. [1](#0-0) 

Execution then proceeds to `try_handle_request` (lines 147-207), which decodes the block, invokes `Relayer::process_new_nakamoto_block_ext(... self.broadcast.unwrap_or(false))` (passing `false`), and — critically — if the returned `accepted` status is true, calls `node.set_relay_message(StacksMessageType::NakamotoBlocks(...))` **unconditionally**, with no check on `self.broadcast` or `authenticated` at all: [2](#0-1) 

So the `broadcast` query parameter and its auth gate only affect the internal flag passed into `process_new_nakamoto_block_ext` (which presumably governs some internal signaling/coordinator behavior), but the actual network relay of the block via `node.set_relay_message` is performed for every accepted block, whether or not `broadcast` was requested and whether or not the caller was authenticated. This breaks the intended equality: "broadcast is a privileged action requiring auth" — in reality, *any* accepted-block upload (unauthenticated, `broadcast` omitted) results in the same relay action that `broadcast=1` was supposed to gate.

### Impact Explanation
An unprivileged remote attacker who can reach the node's RPC port can POST a validly-signed Nakamoto block to `/v3/blocks/upload/` without `broadcast=1` and without any authorization header. If chainstate processing (`Relayer::process_new_nakamoto_block_ext`) accepts the block, the node will queue it for peer-to-peer relay via `node.set_relay_message`, exactly as if the (auth-gated) `broadcast=1` path had been used. This defeats the purpose of the auth gate: the intended access-control boundary (only authenticated callers may cause the node to relay an uploaded block to the network) does not actually hold. This is an auth-bypass of an access-control gate on a state-affecting/network-propagating action, matching the Critical category of "request smuggling or auth bypass" / "unauthenticated ... write to state" (the block is stored/staged and queued for relay).

### Likelihood Explanation
No special preconditions are needed beyond RPC-port reachability and a validly-formed Nakamoto block that would independently pass chainstate acceptance criteria (this is the same criterion that would let it in via `broadcast=1` if auth had been set). The attacker needs no secret, no peer identity, and no privileged role — only the ability to POST bytes to the RPC endpoint, which is remotely reachable and repeatable per accepted block.

### Recommendation
Gate the `node.set_relay_message(...)` call itself on `self.broadcast.unwrap_or(false)` (and, when auth is configured, on `authenticated`), not merely on `data_resp.accepted`. If the intent is that any accepted, validly-processed block should always propagate regardless of the uploader's `broadcast` flag, then remove the misleading `broadcast`/`authorization` distinction and document/enforce that this endpoint always relays valid blocks (in which case the auth gate should protect the whole endpoint, not just the `broadcast=1` branch).

### Proof of Concept
Rust test in `stackslib/src/net/api/postblock_v3.rs` (or a new integration test module):
1. Construct `RPCPostBlockRequestHandler::new(None)` (no auth configured).
2. Build an `HttpRequestPreamble` for `POST /v3/blocks/upload/` with `content-type: application/octet-stream`, no `broadcast` query parameter, and no `authorization` header.
3. Serialize a valid `NakamotoBlock` (one that will pass `Relayer::process_new_nakamoto_block_ext`'s acceptance checks against a prepared `StacksNodeState`/chainstate fixture) as the body.
4. Call `try_parse_request` — assert it returns `Ok(..)` (no 401), and `self.broadcast == Some(false)`.
5. Call `try_handle_request` against a `StacksNodeState` wired to a `NetworkResult`/relay queue; assert `data_resp.accepted == true` and that `node.set_relay_message` was invoked (e.g., assert the relay queue/`NetworkResult` now contains a `StacksMessageType::NakamotoBlocks` entry for the uploaded block) — demonstrating relay occurred despite `broadcast=false` and no authentication.

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
