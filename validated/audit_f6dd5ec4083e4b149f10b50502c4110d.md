### Title
Unauthenticated block upload triggers unconditional P2P relay, bypassing the `broadcast`/auth gate - ([File: stackslib/src/net/api/postblock_v3.rs])

### Summary
`RPCPostBlockRequestHandler::try_handle_request` decides whether to call `node.set_relay_message(...)` (which ultimately causes network-wide broadcast of the uploaded block) solely based on `data_resp.accepted`, not on `self.broadcast`/authentication status established in `try_parse_request`. The `broadcast` flag passed into `Relayer::process_new_nakamoto_block_ext` (as `force_broadcast`) only changes behavior for the "we already have this block" case; for a brand-new, chainstate-acceptable block it has no effect on the return value, so any unauthenticated uploader of a fresh valid block gets it relayed to the whole P2P network exactly as if they had supplied an authenticated `broadcast=1`.

### Finding Description
In `try_parse_request` [1](#0-0) , an unauthenticated request is only rejected if it explicitly requests `broadcast=1`; if `broadcast` is omitted, the request is accepted with `self.broadcast = Some(false)` and no authentication required.

In `try_handle_request`, this `self.broadcast` value is forwarded as the `force_broadcast` argument to `Relayer::process_new_nakamoto_block_ext` [2](#0-1) . Examining that function [3](#0-2) , `force_broadcast` only matters in the branch where the node **already has** the block (it changes `AlreadyStored` into `Accepted` so a duplicate submission can still be re-broadcast). For a **new** block that chainstate accepts, the function proceeds through normal validation and returns `BlockAcceptResponse::Accepted` completely independent of `force_broadcast`.

Back in `try_handle_request`, the relay decision is:
```
if data_resp.accepted {
    node.set_relay_message(StacksMessageType::NakamotoBlocks(NakamotoBlocksData { blocks: vec![block] }));
}
``` [4](#0-3) 
This check uses only `data_resp.accepted` — it never inspects `self.broadcast` or whether the request was authenticated. So for a new, valid block uploaded with no `Authorization` header and no `broadcast=1`, `data_resp.accepted` is `true`, and `set_relay_message` fires anyway.

The message set here is picked up by `RPCHandlerArgs`/`ConversationHttp::handle_request` via `node.take_relay_message()` [5](#0-4) , returned up through `chat()` [6](#0-5) , and merged into `network_result` via `consume_http_uploads`, which pushes the block into `network_result.uploaded_nakamoto_blocks` [7](#0-6) . `Relayer::process_new_nakamoto_blocks` then drains this list, confirms the block is now stored, and unconditionally includes it in `accepted_nakamoto_blocks_and_relayers` [8](#0-7) , which is subsequently broadcast to sampled peers via `relay_epoch3_blocks` → `self.p2p.broadcast_message(...)` [9](#0-8) .

Nowhere along this path is the original `broadcast`/authentication decision re-checked. The equality "network relay happens" == "caller was authenticated and asked for broadcast" is broken: relay is keyed purely on chainstate acceptance of a *new* block, not on the authenticated intent captured in `try_parse_request`.

### Impact Explanation
An unprivileged, unauthenticated remote attacker who can reach the node's `/v3/blocks/upload/` RPC endpoint can cause the node to broadcast an arbitrary (but chainstate-valid) Nakamoto block to its P2P peers, even though the node operator configured `self.auth` (an RPC secret) specifically to gate `broadcast=1` behind authentication per the documented API contract [10](#0-9) . This is network-wide propagation of data through an auth-bypassed path — matching the "network-wide propagation of forged/uncontrolled data" Critical category. It's repeatable per distinct valid block the attacker can produce or obtain (e.g., blocks it can observe elsewhere but which have not yet reached this particular victim node), letting an attacker use any victim node's authenticated broadcast capability without knowing the RPC secret.

### Likelihood Explanation
Preconditions: the node has `/v3/blocks/upload/` reachable (standard v3 RPC endpoint, remotely reachable, no privileged role needed), and `self.auth` may or may not be configured — it doesn't matter, since the omission of `broadcast=1` skips the auth check entirely by design, and the relay still fires. The only requirement is that the uploaded block be one that `process_new_nakamoto_block_ext` deems acceptable (valid tenure, correct epoch, not "problematic," and not already known to that node). This is easily satisfiable by relaying a legitimately-produced block (e.g., a real signed block the attacker observed on the network but which hasn't yet propagated to the target node) — no forgery of consensus signatures is required, just re-submission timing. Attacker cost is a single HTTP POST; fully repeatable.

### Recommendation
Gate the `set_relay_message` call in `try_handle_request` on `self.broadcast.unwrap_or(false)` (i.e., only relay when the caller explicitly requested and was authenticated for `broadcast=1`), not solely on `data_resp.accepted`. For example:
```rust
if data_resp.accepted && self.broadcast.unwrap_or(false) {
    node.set_relay_message(...);
}
```
This restores the intended equality that P2P relay only happens for authenticated broadcast requests, while unauthenticated/no-broadcast uploads still get stored locally but not rebroadcast.

### Proof of Concept
Rust test outline (in `stackslib/src/net/api/tests/postblock_v3.rs` or an integration-style RPC test):
1. Configure `RPCPostBlockRequestHandler::new(Some("secret".to_string()))` to simulate a node with `self.auth` set.
2. Build a request to `/v3/blocks/upload/` with no `Authorization` header and no `broadcast` query parameter, body = a well-formed `NakamotoBlock` that chainstate will accept (fresh tenure, valid signer signatures per existing test helpers e.g. `make_nakamoto_tenure`).
3. Call `try_parse_request` — assert it succeeds (no 401), and `self.broadcast == Some(false)`.
4. Call `try_handle_request` with a `StacksNodeState` wired to accept the block (mirroring existing tests using `Relayer::process_new_nakamoto_block_ext`).
5. Assert `data_resp.accepted == true`.
6. Assert (revealing the bug) that `node.take_relay_message()` returns `Some(StacksMessageType::NakamotoBlocks(...))` containing the block — i.e., the block was queued for P2P relay despite the request being fully unauthenticated and not requesting broadcast.
7. The correct/fixed behavior would assert `node.take_relay_message()` returns `None` in this scenario.

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

**File:** stackslib/src/net/relay.rs (L905-959)
```rust
    pub fn process_new_nakamoto_block_ext(
        burnchain: &Burnchain,
        sortdb: &SortitionDB,
        sort_handle: &mut SortitionHandleConn,
        chainstate: &mut StacksChainState,
        stacks_tip: &StacksBlockId,
        block: &NakamotoBlock,
        coord_comms: Option<&CoordinatorChannels>,
        obtained_method: NakamotoBlockObtainMethod,
        force_broadcast: bool,
    ) -> Result<BlockAcceptResponse, chainstate_error> {
        info!(
            "Handle incoming Nakamoto block {}/{} obtained via {}",
            &block.header.consensus_hash,
            &block.header.block_hash(),
            &obtained_method;
            "block_id" => %block.header.block_id(),
        );
        if block.is_shadow_block() {
            // drop, since we can get these from ourselves when downloading a tenure that ends in
            // a shadow block.
            return Ok(BlockAcceptResponse::AlreadyStored);
        }

        if fault_injection::ignore_block(block.header.chain_length, &burnchain.working_dir) {
            return Ok(BlockAcceptResponse::Rejected(
                "Fault injection: ignoring block".into(),
            ));
        }

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

**File:** stackslib/src/net/relay.rs (L2062-2096)
```rust
        let mut http_uploaded_blocks = vec![];
        for block in network_result.uploaded_nakamoto_blocks.drain(..) {
            let block_id = block.block_id();
            let have_block = chainstate
                .nakamoto_blocks_db()
                .has_nakamoto_block_with_index_hash(&block_id)
                .unwrap_or_else(|e| {
                    warn!(
                        "Failed to determine if we have Nakamoto block";
                        "stacks_block_id" => %block_id,
                        "err" => ?e
                    );
                    false
                });
            if have_block {
                debug!(
                    "Received http-uploaded nakamoto block";
                    "stacks_block_id" => %block_id,
                );
                http_uploaded_blocks.push(block);
            }
        }
        if !http_uploaded_blocks.is_empty() {
            coord_comms.inspect(|comm| {
                comm.announce_new_stacks_block();
            });
        }

        accepted_nakamoto_blocks_and_relayers.extend(pushed_blocks_and_relayers);
        accepted_nakamoto_blocks_and_relayers.push(AcceptedNakamotoBlocks {
            relayers: vec![],
            blocks: http_uploaded_blocks,
        });
        Ok((accepted_nakamoto_blocks_and_relayers, bad_neighbors))
    }
```

**File:** stackslib/src/net/relay.rs (L2718-2737)
```rust
            for block in relay_blocks.iter() {
                debug!(
                    "{:?}: Forward Nakamoto block {}/{}",
                    _local_peer,
                    &block.header.consensus_hash,
                    &block.header.block_hash()
                );
                self.recently_sent_nakamoto_blocks.insert(
                    block.block_id(),
                    (block.header.consensus_hash.clone(), get_epoch_time_ms()),
                );
            }

            let msg = StacksMessageType::NakamotoBlocks(NakamotoBlocksData {
                blocks: relay_blocks,
            });
            if let Err(e) = self.p2p.broadcast_message(relayers, msg) {
                warn!("Failed to broadcast Nakamoto blocks: {:?}", &e);
            }
        }
```

**File:** stackslib/src/net/rpc.rs (L217-230)
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

```

**File:** stackslib/src/net/rpc.rs (L480-526)
```rust
    pub fn chat(
        &mut self,
        node: &mut StacksNodeState,
    ) -> Result<Vec<StacksMessageType>, net_error> {
        // handle in-bound HTTP request(s)
        let num_inbound = self.connection.inbox_len();
        let mut ret = vec![];
        test_debug!("{:?}: {} HTTP requests pending", &self, num_inbound);

        for _i in 0..num_inbound {
            let Some(msg) = self.connection.next_inbox_message() else {
                continue;
            };

            match msg {
                StacksHttpMessage::Request(req) => {
                    // new request that we can handle
                    self.total_request_count += 1;
                    self.last_request_timestamp = get_epoch_time_secs();
                    let latency = req.duration_ms();
                    let start_time = Instant::now();
                    let verb = req.verb().to_string();
                    let request_path = req.request_path().to_string();
                    let msg_opt = monitoring::instrument_http_request_handler(
                        self,
                        req,
                        |conv_http, req| conv_http.handle_request(req, node),
                    )?;

                    let msg_opt_log = if let Some(ref msg) = msg_opt {
                        msg.get_message_description()
                    } else {
                        "None".into()
                    };
                    info!("Handled StacksHTTPRequest";
                          "verb" => %verb,
                          "path" => %request_path,
                          "processing_time_ms" => start_time.elapsed().as_millis(),
                          "latency_ms" => latency,
                          "conn_id" => self.conn_id,
                          "peer_addr" => &self.peer_addr,
                          "p2p_msg" => msg_opt_log);

                    if let Some(msg) = msg_opt {
                        ret.push(msg);
                    }
                }
```

**File:** stackslib/src/net/mod.rs (L2269-2293)
```rust
    pub fn consume_http_uploads(&mut self, msgs: Vec<StacksMessageType>) {
        for msg in msgs.into_iter() {
            match msg {
                StacksMessageType::Transaction(tx_data) => {
                    self.uploaded_transactions.push(tx_data);
                }
                StacksMessageType::Blocks(block_data) => {
                    self.uploaded_blocks.push(block_data);
                }
                StacksMessageType::Microblocks(mblock_data) => {
                    self.uploaded_microblocks.push(mblock_data);
                }
                StacksMessageType::StackerDBPushChunk(chunk_data) => {
                    self.uploaded_stackerdb_chunks.push(chunk_data);
                }
                StacksMessageType::NakamotoBlocks(data) => {
                    self.uploaded_nakamoto_blocks.extend(data.blocks);
                }
                _ => {
                    // drop
                    warn!("Dropping unknown HTTP message");
                }
            }
        }
    }
```

**File:** docs/rpc/openapi.yaml (L2362-2380)
```yaml
      # Two alternative security requirements:
      #   - With `rpcAuth` when broadcast=1
      #   - No auth when broadcast is absent
      security:
        - rpcAuth: []
        - {}
      operationId: uploadNakamotoBlock
      description: |
        Upload a Nakamoto block to the node for processing.

        - **Body** - must be the binary (SIP-003) serialization of a `NakamotoBlock`.
        - **Authentication** - only required when the query parameter `broadcast=1` is supplied.
          In that case the caller **must** include an `Authorization` header.
      parameters:
        - name: broadcast
          in: query
          description: |
            If set to `"1"` the node will broadcast the uploaded block to peers.
            When present the request must include a valid `Authorization` header.
```
