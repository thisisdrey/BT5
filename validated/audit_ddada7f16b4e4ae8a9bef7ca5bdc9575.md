### Title
`/v2/microblocks` relays microblocks anchored to an attacker-chosen, non-canonical staging tip via the `?tip=` query parameter - (File: `stackslib/src/net/api/postmicroblock.rs`)

### Summary
`RPCPostMicroblockRequestHandler::try_handle_request` derives the `consensus_hash`/`block_hash` used to build the relayed `MicroblocksData.index_anchor_block` from `node.load_stacks_chain_tip`, which honors the unauthenticated, attacker-supplied `tip=` query parameter (`TipRequest::SpecificTip`). The only server-side check on that hash, `SortitionDB::get_block_snapshot_consensus`, merely confirms that *some* sortition snapshot exists for the given consensus hash — it does not require that hash to be the currently canonical tip. This lets a remote, unauthenticated caller force the node to accept and relay a microblock keyed to a stale/orphaned block to the entire P2P network.

### Finding Description
The claimed equality — that the `consensus_hash`/`block_hash` used to key the relayed `MicroblocksData.index_anchor_block` equals the block that is actually canonical at the current sortition tip — is broken by design, not merely by a staging-DB race.

`try_handle_request` resolves the tip as follows: [1](#0-0) 

`load_stacks_chain_tip` dispatches on `contents.tip_request()`, which is populated straight from the HTTP query string's `tip=` parameter: [2](#0-1) [3](#0-2) [4](#0-3) 

Any syntactically valid 64-hex-char `StacksBlockId` is accepted verbatim as `TipRequest::SpecificTip(tip)` and returned unchanged — no canonical-tip check is performed at this stage, and no authentication is required for this endpoint (unlike e.g. `callreadonlyfunction`/`fastcallreadonly`, which check a password header). The handler then loads staging info for *that specific* `StacksBlockId`: [5](#0-4) 

and the only validation performed against the sortition DB, `SortitionDB::get_block_snapshot_consensus`, simply verifies a snapshot row exists for that consensus hash — it says nothing about whether that snapshot is the chain's current canonical tip: [6](#0-5) 

If `chainstate.preprocess_streamed_microblock(consensus_hash, block_hash, &microblock)` accepts the microblock (which only requires that it validly extends the staged microblock stream for that specific anchored block — it does not require that block to be canonical), the node builds `parent_block_id = StacksBlockHeader::make_index_block_hash(consensus_hash, block_hash)` from the attacker-supplied, non-canonical pair and relays it network-wide: [7](#0-6) 

**Exploit flow:** An attacker who knows (or can discover, e.g. via `/v2/info`, `/v2/blocks/...`, or by observing gossip) the consensus hash/block hash of any orphaned/non-canonical block for which the node still has both a sortition snapshot and staging block info (a normal condition on any node that has seen a fork) can:
1. Craft a microblock that validly extends that orphaned block's staging microblock stream.
2. `POST /v2/microblocks?tip=<orphaned_block_id_hex>` with the microblock body.
3. The node accepts it, builds `index_anchor_block` from the orphaned block's `consensus_hash`/`block_hash`, and calls `node.set_relay_message(...)`, causing it to be gossiped to all connected peers as a `StacksMessageType::Microblocks` message, re-serialized per `MicroblocksData`'s `StacksMessageCodec` implementation.

This does not require the "stale staging entry accidentally matching a fork" precondition originally hypothesized — the attacker directly and deterministically selects the non-canonical anchor via the query parameter, which is a stronger and more reliable version of the same underlying bug: nothing in this path enforces `index_anchor_block == ` the sortition-canonical tip.

### Impact Explanation
The node relays, to its entire peer set, a `MicroblocksData` message that presents a microblock as extending a non-canonical/orphaned Stacks block, using the node's own honest relay path (`set_relay_message`/`Relayer`) — i.e., forged/misleading chain-state data is propagated network-wide from a single unauthenticated HTTP POST. This matches the "High: serving non-canonical state as canonical" / network-wide propagation of forged data category. It is trivially repeatable per orphaned block the node has staged, and costs the attacker only knowledge of one orphaned block's `consensus_hash`/`block_hash` and a validly-signed microblock extending it (the attacker needs a microblock private key that matches the anchored block's expected signer, but that is attacker-controlled tooling, not a secret held by the node).

### Likelihood Explanation
- The endpoint `/v2/microblocks` requires no RPC secret/auth header (unlike `callreadonlyfunction`).
- `tip=` accepts any well-formed `StacksBlockId` hex string without any canonical check at resolution time (`TipRequest::SpecificTip` is a direct pass-through, per `stackslib/src/net/mod.rs:817`).
- The only DB check performed (`get_block_snapshot_consensus`) validates existence of a snapshot, not canonicity, so it does not block the attack.
- Preconditions: the target node must retain a sortition snapshot and staging block info for the chosen non-canonical block (normal after any fork/reorg or during microblock-era competing tenures), and the attacker must produce a microblock that `preprocess_streamed_microblock` accepts for that anchor.
- Fully remotely reachable over the standard RPC port with no privileged role, and repeatable at will.

### Recommendation
In `RPCPostMicroblockRequestHandler::try_handle_request` (`stackslib/src/net/api/postmicroblock.rs`), before accepting/relaying, verify that `ch_sn` (the snapshot for `consensus_hash`) is the *canonical* sortition tip (e.g., compare against `SortitionDB`'s canonical chain tip / `sortdb.index_handle`'s canonical view), or reject `TipRequest::SpecificTip` values for this endpoint entirely and only ever anchor microblocks to the node's own canonical tip (`TipRequest::UseLatestAnchoredTip`/`UseLatestUnconfirmedTip`), ignoring any client-supplied `tip=` for this write path.

### Proof of Concept
Rust test plan (net/api tests, mirroring `stackslib/src/net/api/tests/postmicroblock.rs` conventions and `TestRPC` harness used elsewhere, e.g. `getblockbyheight.rs`):
1. Set up a `TestRPC`/`TestPeer` chainstate with two competing anchored Stacks blocks at the same/adjacent sortition height (a fork): `canonical_block` (currently the sortition-canonical tip) and `orphan_block` (a valid, staged, but non-canonical block, both with valid `SortitionDB` snapshots).
2. Construct a valid `StacksMicroblock` that extends `orphan_block`'s staging state (signed appropriately so `preprocess_streamed_microblock` returns `Ok(true)`).
3. Build `StacksHttpRequest::new_post_microblock(host, mblock, TipRequest::SpecificTip(StacksBlockId::new(&orphan_block.consensus_hash, &orphan_block.block_hash())))` and send it through the RPC harness.
4. After `try_handle_request` runs, call `node.take_relay_message()` and assert that the resulting `StacksMessageType::Microblocks(MicroblocksData { index_anchor_block, .. })` equals `StacksBlockHeader::make_index_block_hash(&orphan_block.consensus_hash, &orphan_block.block_hash())` — i.e., **not** equal to `StacksBlockHeader::make_index_block_hash(&canonical_block.consensus_hash, &canonical_block.block_hash())` — proving the relay anchor is attacker-selected and non-canonical.
5. Assert HTTP response is `200 OK` (not `404`/`400`), confirming the request was accepted and would be relayed without any canonicity enforcement.

### Citations

**File:** stackslib/src/net/api/postmicroblock.rs (L129-147)
```rust
        let tip = match node.load_stacks_chain_tip(&preamble, &contents) {
            Ok(tip) => tip,
            Err(error_resp) => {
                return error_resp.try_into_contents().map_err(NetError::from);
            }
        };
        let data_resp = node.with_node_state(|_network, sortdb, chainstate, _mempool, _rpc_args| {
            let stacks_tip = match StacksChainState::load_staging_block_info(chainstate.db(), &tip) {
                Ok(Some(tip_info)) => tip_info,
                Ok(None) => {
                    return Err(StacksHttpResponse::new_error(&preamble, &HttpNotFound::new("No such stacks tip".into())));
                },
                Err(e) => {
                    return Err(StacksHttpResponse::new_error(&preamble, &HttpServerError::new(format!("Failed to load chain tip: {:?}", &e))));
                }
            };

            let consensus_hash = &stacks_tip.consensus_hash;
            let block_hash = &stacks_tip.anchored_block_hash;
```

**File:** stackslib/src/net/api/postmicroblock.rs (L149-163)
```rust
            // make sure we can accept this
            let ch_sn = match SortitionDB::get_block_snapshot_consensus(sortdb.conn(), consensus_hash) {
                Ok(Some(sn)) => sn,
                Ok(None) => {
                    return Err(StacksHttpResponse::new_error(&preamble, &HttpNotFound::new("No such snapshot for Stacks tip consensus hash".to_string())));
                }
                Err(e) => {
                    debug!("No block snapshot for consensus hash {}", &consensus_hash);
                    return Err(StacksHttpResponse::new_error(&preamble, &HttpBadRequest::new_json(ChainError::DBError(e).into_json())));
                }
            };

            let sort_handle = sortdb.index_handle(&ch_sn.sortition_id);
            let parent_block_snapshot = Relayer::get_parent_stacks_block_snapshot(&sort_handle, consensus_hash, block_hash)
                .map_err(|e| StacksHttpResponse::new_error(&preamble, &HttpServerError::new(format!("Failed to load parent block for Stacks tip: {:?}", &e))))?;
```

**File:** stackslib/src/net/api/postmicroblock.rs (L178-205)
```rust
            match chainstate.preprocess_streamed_microblock(consensus_hash, block_hash, &microblock) {
                Ok(accepted) => {
                    debug!("{} uploaded microblock {consensus_hash}/{block_hash}-{}",
                           if accepted { "Accepted" } else { "Did not accept" },
                           &microblock.block_hash()
                    );
                    Ok((accepted, StacksBlockHeader::make_index_block_hash(consensus_hash, block_hash)))
                },
                Err(e) => {
                    debug!("Failed to process microblock {}/{}-{}: {:?}", &consensus_hash, &block_hash, &microblock.block_hash(), &e);
                    Err(StacksHttpResponse::new_error(&preamble, &HttpBadRequest::new_json(e.into_json())))
                }
            }
        });

        let (accepted, parent_block_id, data_resp) = match data_resp {
            Ok((accepted, parent_block_id)) => (accepted, parent_block_id, microblock.block_hash()),
            Err(response) => {
                return response.try_into_contents().map_err(NetError::from);
            }
        };

        // don't forget to forward this to the p2p network!
        if accepted {
            node.set_relay_message(StacksMessageType::Microblocks(MicroblocksData {
                index_anchor_block: parent_block_id,
                microblocks: vec![microblock],
            }));
```

**File:** stackslib/src/net/mod.rs (L764-817)
```rust
    pub fn load_stacks_chain_tip(
        &mut self,
        preamble: &HttpRequestPreamble,
        contents: &HttpRequestContents,
    ) -> Result<StacksBlockId, StacksHttpResponse> {
        self.with_node_state(|_network, sortdb, chainstate, _mempool, _rpc_args| {
            let tip_req = contents.tip_request();
            match tip_req {
                TipRequest::UseLatestUnconfirmedTip => {
                    let unconfirmed_chain_tip_opt = match &mut chainstate.unconfirmed_state {
                        Some(unconfirmed_state) => {
                            match unconfirmed_state.get_unconfirmed_state_if_exists() {
                                Ok(res) => res,
                                Err(msg) => {
                                    return Err(StacksHttpResponse::new_error(
                                        preamble,
                                        &HttpNotFound::new(format!("No unconfirmed tip: {}", &msg)),
                                    ));
                                }
                            }
                        }
                        None => None,
                    };

                    if let Some(unconfirmed_chain_tip) = unconfirmed_chain_tip_opt {
                        Ok(unconfirmed_chain_tip)
                    } else {
                        match NakamotoChainState::get_canonical_block_header(
                            chainstate.db(),
                            sortdb,
                        ) {
                            Ok(Some(tip)) => Ok(StacksBlockId::new(
                                &tip.consensus_hash,
                                &tip.anchored_header.block_hash(),
                            )),
                            Ok(None) => {
                                return Err(StacksHttpResponse::new_error(
                                    preamble,
                                    &HttpNotFound::new("No such confirmed tip".to_string()),
                                ));
                            }
                            Err(e) => {
                                return Err(StacksHttpResponse::new_error(
                                    preamble,
                                    &HttpServerError::new(format!(
                                        "Failed to load chain tip: {:?}",
                                        &e
                                    )),
                                ));
                            }
                        }
                    }
                }
                TipRequest::SpecificTip(tip) => Ok(tip.clone()),
```

**File:** stackslib/src/net/httpcore.rs (L107-117)
```rust
impl From<&str> for TipRequest {
    fn from(s: &str) -> TipRequest {
        if s == "latest" {
            TipRequest::UseLatestUnconfirmedTip
        } else if let Ok(block_id) = StacksBlockId::from_hex(s) {
            TipRequest::SpecificTip(block_id)
        } else {
            TipRequest::UseLatestAnchoredTip
        }
    }
}
```

**File:** stackslib/src/net/httpcore.rs (L374-379)
```rust
    fn tip_request(&self) -> TipRequest {
        self.get_query_args()
            .get("tip")
            .map(|tip| tip.as_str().into())
            .unwrap_or(TipRequest::UseLatestAnchoredTip)
    }
```
