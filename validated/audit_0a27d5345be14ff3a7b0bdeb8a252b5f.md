### Title
Broadcast/auth flag is not enforced when relaying newly-accepted Nakamoto blocks - ([File: stackslib/src/net/api/postblock_v3.rs])

### Summary
`RPCPostBlockRequestHandler::try_handle_request` for `/v3/blocks/upload` calls `node.set_relay_message(...)` solely based on `data_resp.accepted`, without checking `self.broadcast`/`authenticated`. The `broadcast` flag is only forwarded into `Relayer::process_new_nakamoto_block_ext` as `force_broadcast`, which only affects the *already-stored* block branch — it plays no role in whether a genuinely new, valid block gets relayed. As a result, an unauthenticated, non-broadcast upload of a new, validly-signed block is unconditionally gossiped to the whole P2P network.

### Finding Description
The intended invariant (per `docs/rpc/openapi.yaml` and the `try_parse_request` auth gate) is: relay to the network should require `broadcast=1` AND a valid `Authorization` header: [1](#0-0) 

But in `try_handle_request`, `self.broadcast` is only passed to `Relayer::process_new_nakamoto_block_ext` as `force_broadcast`: [2](#0-1) 

and the relay side-effect is gated only on `data_resp.accepted`, not on `self.broadcast`: [3](#0-2) 

Inside `process_new_nakamoto_block_ext`, `force_broadcast` (i.e. `self.broadcast`) is consulted only in the "we already have this block" branch, to force `Accepted` for an already-known block so it gets re-relayed: [4](#0-3) 

For a block the node does *not* already have, acceptance is decided purely by `NakamotoChainState::accept_block` (signature/tenure validation) — `force_broadcast`/`self.broadcast` has zero effect on this path: [5](#0-4) 

Because `try_parse_request` only enforces the auth check when `broadcast=1` is requested (`if broadcast && !authenticated { return Err(401) }`), an unauthenticated caller can simply omit `broadcast=1` entirely and still reach `try_handle_request` with `self.broadcast = Some(false)`. If the posted block is new and validly signed (e.g., replayed from another peer/observed on the network — no forgery needed), `accept_block` returns `true`, `data_resp.accepted` is `true`, and `node.set_relay_message` fires unconditionally — causing the p2p thread to broadcast the block to the whole network via `NetworkRequest::Broadcast` handling in `dispatch_request`: [6](#0-5) [7](#0-6) 

The equality the code should maintain — relay ⟺ (`broadcast == Some(true)` ∧ `authenticated == true`) — is broken: relay actually occurs whenever `accepted == true`, independent of `broadcast`/`authenticated`.

### Impact Explanation
Any unprivileged remote party who can reach a node's RPC port can turn an unauthenticated, "silent" (`broadcast` unset) block upload into a network-wide broadcast, defeating the endpoint's documented access control that gates propagation behind authentication. While the attacker cannot forge a block's contents (miner/signer signatures are still validated by `accept_block`), they can force any node to act as an unauthenticated relay/amplifier for legitimately-signed blocks the attacker has merely observed or replayed, bypassing the explicit `broadcast`-authentication gate described in the API contract. This is a real auth-bypass on a propagation side-effect (matches "network-wide propagation ... auth bypass" in the Critical bucket), even though it does not corrupt consensus data since blocks are still validated.

### Likelihood Explanation
- Preconditions: attacker only needs network access to the node's RPC port (`/v3/blocks/upload`) and a validly-signed Nakamoto block that the target node does not yet have (e.g., an already-broadcast block from elsewhere, replayed to a lagging/isolated node).
- No RPC secret, no peer key, and no privileged role are required — `broadcast` defaults to `false` if omitted, which bypasses the `authorization` check entirely in `try_parse_request`.
- Fully repeatable per new (previously-unseen-by-that-node) block; the attacker can replay any block they can observe to any node they can reach.

### Recommendation
Gate the `node.set_relay_message(...)` call in `try_handle_request` on `self.broadcast == Some(true)` (and thus on `authenticated`, since `try_parse_request` already ensures `broadcast ⇒ authenticated`), not merely on `data_resp.accepted`:
```rust
if data_resp.accepted && self.broadcast.unwrap_or(false) {
    node.set_relay_message(...);
}
```
Additionally, reconsider whether `force_broadcast` should be renamed/documented to make clear it only affects already-known blocks, since it currently has no bearing on new-block relay at all — the actual relay gate lives in the HTTP handler, not the relayer.

### Proof of Concept
Rust test in `stackslib/src/net/api/tests/postblock_v3.rs` (or a new test module), using the existing test harness patterns for `RPCPostBlockRequestHandler` (`TestRPC`/mock `StacksNodeState` used elsewhere in this file):
1. Construct a `RPCPostBlockRequestHandler::new(Some("secret".to_string()))` (node has an RPC password configured).
2. Build a `StacksHttpRequest::new_post_block_v3(host, &block)` (no `broadcast` query arg, no `Authorization` header) with a *new*, validly-signed `NakamotoBlock` the mock chainstate does not yet have.
3. Run `try_parse_request` — assert it succeeds (no 401), confirming `self.broadcast == Some(false)` and no auth was required.
4. Run `try_handle_request` against a mock/test `StacksNodeState` wired to a chainstate where `Relayer::process_new_nakamoto_block_ext` accepts the block (`accepted.is_accepted() == true`).
5. Assert on the equality that should hold: `node.take_relay_message()` should be `None` here (unauthenticated, non-broadcast request), i.e. assert `set_relay_message` was NOT called.
6. Current behavior: `take_relay_message()` returns `Some(StacksMessageType::NakamotoBlocks(...))` even though `broadcast == Some(false)` and `authenticated == false` — the assertion in step 5 fails, proving `data_resp.accepted` alone (not the intended `broadcast && authenticated` conjunction) controls relay.

Note: I could not fully verify the exact mock-harness helper names for driving `try_handle_request` end-to-end with a stubbed `StacksNodeState`/`Relayer` in this environment (index size limits truncated some of `postblock_v3.rs`'s test helpers and `net/mod.rs`'s `StacksNodeState` definition); a full Devin session with file access would be needed to wire the exact mock and confirm the runnable assertion syntax.

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

**File:** stackslib/src/net/relay.rs (L935-959)
```rust
        // do we have this block?  don't lock the DB needlessly if so.
        if chainstate
            .nakamoto_blocks_db()
            .has_nakamoto_block_with_index_hash(&block.header.block_id())
            .inspect_err(|e| {
                warn!(
                    "Failed to determine if we have Nakamoto block {}/{}: {e:?}",
                    &block.header.consensus_hash,
                    &block.header.block_hash()
                );
            })?
        {
            if force_broadcast {
                // it's possible that the signer sent this block to us, in which case, we should
                // broadcast it
                debug!(
                    "Already have Nakamoto block {}, but treating a new anyway so we can broadcast it",
                    &block.header.block_id()
                );
                return Ok(BlockAcceptResponse::Accepted);
            } else {
                debug!("Already have Nakamoto block {}", &block.header.block_id());
                return Ok(BlockAcceptResponse::AlreadyStored);
            }
        }
```

**File:** stackslib/src/net/relay.rs (L1036-1055)
```rust
        let accepted = NakamotoChainState::accept_block(
            chainstate,
            block,
            sort_handle,
            &reward_set,
            obtained_method,
        )?;

        if accepted {
            info!("{}", &accept_msg);
            if let Some(coord_comms) = coord_comms {
                if !coord_comms.announce_new_stacks_block() {
                    return Err(chainstate_error::NetError(net_error::CoordinatorClosed));
                }
            }
            Ok(BlockAcceptResponse::Accepted)
        } else {
            info!("{reject_msg}");
            Ok(BlockAcceptResponse::AlreadyStored)
        }
```

**File:** stackslib/src/net/p2p.rs (L1611-1646)
```rust
            NetworkRequest::Broadcast(relay_hints, msg) => {
                // pick some neighbors. Note that only some messages can be broadcasted.
                let neighbor_keys = match msg {
                    StacksMessageType::Blocks(ref data) => {
                        // send to each neighbor that needs one
                        let mut all_neighbors = HashSet::new();
                        for BlocksDatum(_, block) in data.blocks.iter() {
                            let neighbors = self.sample_broadcast_peers(&relay_hints, block)?;
                            for nk in neighbors.into_iter() {
                                all_neighbors.insert(nk);
                            }
                        }
                        Ok(all_neighbors.into_iter().collect())
                    }
                    StacksMessageType::Microblocks(ref data) => {
                        // send to each neighbor that needs at least one
                        let mut all_neighbors = HashSet::new();
                        for mblock in data.microblocks.iter() {
                            let neighbors = self.sample_broadcast_peers(&relay_hints, mblock)?;
                            for nk in neighbors.into_iter() {
                                all_neighbors.insert(nk);
                            }
                        }
                        Ok(all_neighbors.into_iter().collect())
                    }
                    StacksMessageType::NakamotoBlocks(ref data) => {
                        // send to each neighbor that needs one
                        let mut all_neighbors = HashSet::new();
                        for nakamoto_block in data.blocks.iter() {
                            let neighbors =
                                self.sample_broadcast_peers(&relay_hints, nakamoto_block)?;

                            all_neighbors.extend(neighbors);
                        }
                        Ok(all_neighbors.into_iter().collect())
                    }
```

**File:** stackslib/src/net/rpc.rs (L217-245)
```rust
    pub fn handle_request(
        &mut self,
        req: StacksHttpRequest,
        node: &mut StacksNodeState,
    ) -> Result<Option<StacksMessageType>, net_error> {
        node.set_http_peer_addr(self.peer_addr);
        // NOTE: This may set node.relay_message
        let keep_alive = req.preamble().keep_alive;
        let (mut response_preamble, response_body) =
            self.connection.protocol.try_handle_request(req, node)?;

        let mut reply = self.connection.make_relay_handle(self.conn_id)?;
        let relay_msg_opt = node.take_relay_message();

        // All successful RPC responses MUST include the canonical stacks tip height header.
        if response_preamble.is_success() {
            response_preamble
                .set_canonical_stacks_tip_height(Some(node.canonical_stacks_tip_height()));
        }

        // make sure content-length is properly set, based on how we're about to stream data back
        response_preamble.content_length = response_body.content_length();

        // buffer up response headers into the reply handle
        response_preamble.consensus_serialize(&mut reply)?;
        self.reply_streams
            .push_back((reply, response_body, keep_alive));
        Ok(relay_msg_opt)
    }
```
