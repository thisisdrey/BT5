### Title
False StackerDB inventory advertised before chunk is durably stored, permitting a canonical-vs-actual state mismatch to propagate — (File: `stackslib/src/net/stackerdb/mod.rs`)

### Summary
`PeerNetwork::handle_unsolicited_StackerDBPushChunk` patches and immediately replies with a `StackerDBChunkInv` that claims a pushed chunk's `slot_version` is now the node's inventory state, before that chunk has actually been durably written into the StackerDB. This is the same "state marked done before the corresponding effect is applied" bug class as the `TreasuryVesting.batchRelease()` report (state update separated from and assumed to precede the actual write), but manifested here as a remote, unauthenticated equality break between *advertised* inventory and *actually stored* data.

### Finding Description
In `handle_unsolicited_StackerDBPushChunk` [1](#0-0) , upon receiving a pushed chunk the code only runs `validate_received_chunk` — a signature/size/staleness/max-writes sanity check — and, on success, directly mutates the reply payload's inventory vector to reflect the new version: [2](#0-1) 

This mutated `StackerDBChunkInv` is then signed and sent back to the peer as this node's authoritative inventory state [3](#0-2) . Crucially, the chunk itself is *not* written to the StackerDB in this code path — it is merely forwarded to the relayer (`Ok((false, true))`/`Ok((true, false))` semantics), and actual persistence happens later, asynchronously, in `process_stacker_db_chunks` via `tx.try_replace_chunk` [4](#0-3) .

The function's own doc comment concedes that write-frequency is intentionally skipped here: "The write frequency is not checked for this chunk... because messages can be arbitrarily delayed" [5](#0-4) . `validate_received_chunk` performs only size/signer/staleness/max-writes checks and never touches `write_freq` [6](#0-5) , and neither does `try_replace_chunk` in `db.rs` [7](#0-6) . So in principle write-frequency enforcement (if/when applied elsewhere, e.g. in the sync path `sync.rs`) can diverge between the "advertise" step and the "actually store" step: the node tells its peer "I have version N," but the deferred write can still fail (stale version race against a concurrently-arriving chunk, DB error, relayer processing changing state in between, or any future write-frequency gate applied at store time) — leaving the advertised inventory permanently ahead of the actually-stored data for that slot.

This breaks the intended equality "advertised inventory == stored data," the same class of bug as the batchRelease report where "assumed effect" state was recorded before the effect (the transfer / the durable write) actually occurred.

### Impact Explanation
Neighbors that trust this inflated `StackerDBChunkInv` will believe the node already possesses `slot_version = N` and will not re-request/re-relay that chunk to it, while the node's own storage may still be at an older version (or never receive it if the deferred store subsequently fails). This is exactly the "serving non-canonical state as canonical" / "steering a node off the tip via false inventory" category called out as in-scope High impact: it can stall replication of legitimate signer/StackerDB messages network-wide because peers stop offering data the node (falsely) claims to already have.

### Likelihood Explanation
This path is reachable by any unauthenticated remote peer able to open a P2P conversation and send an unsolicited `StackerDBPushChunk` — no signer key or admin privilege is required, only a validly-signed chunk for a slot the attacker controls (which any StackerDB participant can produce for their own slot). The race window (between advertising and the deferred write via the relayer) is a normal consequence of the code's own asynchronous design, so it does not require unusual timing manipulation, just steady legitimate participation combined with conditions that make the deferred store fail (e.g., another chunk for the same slot racing in via `process_stacker_db_chunks`, or a temporary DB error).

### Recommendation
Do not mutate/report the `StackerDBChunkInv` slot version as accepted until the chunk has actually been durably written (mirror the checks-effects-interactions fix from the report: combine "decide acceptance" and "commit state" into one atomic step, or defer sending any updated inventory until after the relayer's `try_replace_chunk` succeeds). At minimum, the ack should reflect the DB's true post-write state rather than a locally patched/speculative value.

### Proof of Concept
Conceptually mirrors the existing test `test_handle_unsolicited_stackerdb_push_chunk_future_view_validation` [8](#0-7) , but instead of the FutureView/Nack branch, exercise the `StackerDBChunkInv` success branch: send a validly-signed `StackerDBPushChunkData` to `handle_unsolicited_StackerDBPushChunk`, observe the returned `StackerDBChunkInv`'s `slot_versions[slot_id]` already reflects the new version [9](#0-8) , then show (e.g. by making `process_stacker_db_chunks`'s `try_replace_chunk` fail — for instance racing two chunks for the same slot through the relayer so one hits `StaleChunk`) that the node's actual `get_slot_metadata`/`get_slot_versions` for that slot never reaches the version it already advertised to the peer.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L649-718)
```rust
    pub fn validate_received_chunk(
        &self,
        smart_contract_id: &QualifiedContractIdentifier,
        config: &StackerDBConfig,
        data: &StackerDBChunkData,
        expected_versions: &[u32],
    ) -> Result<bool, net_error> {
        // validate -- must not exceed this replica's configured chunk size.
        if (data.data.len() as u64) > config.chunk_size {
            info!(
                "Received StackerDBChunk for {} ID {}, which is oversized: {} bytes (max {} bytes)",
                smart_contract_id,
                data.slot_id,
                data.data.len(),
                config.chunk_size
            );
            return Ok(false);
        }

        // validate -- must be a valid chunk
        let Some(expected_version) = expected_versions.get(data.slot_id as usize) else {
            info!(
                "Received StackerDBChunk for {} ID {}, which is too big ({})",
                smart_contract_id,
                data.slot_id,
                expected_versions.len()
            );
            return Ok(false);
        };

        // validate -- must be signed by the expected author
        let addr = match self
            .stackerdbs
            .get_slot_signer(smart_contract_id, data.slot_id)?
        {
            Some(addr) => addr,
            None => {
                return Ok(false);
            }
        };

        let slot_metadata = data.get_slot_metadata();
        if !slot_metadata.verify(&addr)? {
            info!(
                "StackerDBChunk for {} ID {} is not signed by {}",
                smart_contract_id, data.slot_id, &addr
            );
            return Ok(false);
        }

        // validate -- must be the current or newer version
        if data.slot_version < *expected_version {
            info!(
                "Received StackerDBChunk for {} ID {} version {}, which is stale (expected {})",
                smart_contract_id, data.slot_id, data.slot_version, *expected_version
            );
            return Ok(false);
        }

        // validate -- must not exceed max writes
        if data.slot_version > config.max_writes {
            info!(
                "Write count exceeded for StackerDBChunk for {} ID {} version {} (max is {})",
                smart_contract_id, data.slot_id, data.slot_version, config.max_writes
            );
            return Ok(false);
        }

        Ok(true)
    }
```

**File:** stackslib/src/net/stackerdb/mod.rs (L731-734)
```rust
    /// The write frequency is not checked for this chunk. This is because the `ConversationP2P` on
    /// which this chunk arrived will have already bandwidth-throttled the remote peer, and because
    /// messages can be arbitrarily delayed (and bunched up) by the network anyway.
    ///
```

**File:** stackslib/src/net/stackerdb/mod.rs (L742-767)
```rust
    pub fn handle_unsolicited_StackerDBPushChunk(
        &mut self,
        chainstate: &mut StacksChainState,
        event_id: usize,
        preamble: &Preamble,
        chunk_data: &StackerDBPushChunkData,
        send_reply: bool,
    ) -> Result<(bool, bool), net_error> {
        let Some(naddr) = self
            .get_p2p_convo(event_id)
            .map(|convo| convo.to_neighbor_address())
        else {
            debug!(
                "Drop unsolicited StackerDBPushChunk: event ID {} is not connected",
                event_id
            );
            return Ok((false, false));
        };

        let mut payload = self.make_StackerDBChunksInv_or_Nack(
            naddr,
            chainstate,
            &chunk_data.contract_id,
            &chunk_data.rc_consensus_hash,
        );
        match payload {
```

**File:** stackslib/src/net/stackerdb/mod.rs (L784-808)
```rust
                // sanity check
                if !self.validate_received_chunk(
                    &chunk_data.contract_id,
                    stackerdb_config,
                    &chunk_data.chunk_data,
                    &data.slot_versions,
                )? {
                    return Ok((false, false));
                }

                // patch inventory -- we'll accept this chunk
                let Some(slot_version) = data
                    .slot_versions
                    .get_mut(chunk_data.chunk_data.slot_id as usize)
                else {
                    error!(
                        "Chunk not accepted with slot_id {}, which is greater than our slot_versions array {} in {}",
                        chunk_data.chunk_data.slot_id,
                        data.slot_versions.len(),
                        chunk_data.contract_id
                    );
                    return Ok((false, false));
                };
                *slot_version = chunk_data.chunk_data.slot_version;

```

**File:** stackslib/src/net/stackerdb/mod.rs (L858-870)
```rust
        if !send_reply {
            return Ok((false, true));
        }

        // this is a reply to the pushed chunk, and we can store it right now (so don't buffer it)
        let resp = self.sign_for_p2p_reply(event_id, preamble.seq, payload)?;
        let handle = self.send_p2p_message(
            event_id,
            resp,
            self.connection_opts.neighbor_request_timeout,
        )?;
        self.add_relay_handle(event_id, handle);
        Ok((false, true))
```

**File:** stackslib/src/net/relay.rs (L2406-2437)
```rust
        for (sc, sync_results) in sync_results_map.into_iter() {
            if let Some(config) = stackerdb_configs.get(&sc) {
                let tx = self.stacker_dbs.tx_begin(config.clone())?;
                for sync_result in sync_results.into_iter() {
                    for (origin, chunk) in sync_result.chunks_to_store.into_iter() {
                        let md = chunk.get_slot_metadata();
                        if let Err(e) = tx.try_replace_chunk(&sc, &md, &chunk.data) {
                            if matches!(e, Error::StaleChunk { .. }) {
                                // This is a common and expected message, so log it as a debug and with a sep message
                                // to distinguish it from other message types.
                                debug!(
                                    "Dropping stale StackerDB chunk";
                                    "stackerdb_contract_id" => %sync_result.contract_id,
                                    "slot_id" => md.slot_id,
                                    "slot_version" => md.slot_version,
                                    "num_bytes" => chunk.data.len(),
                                    "error" => %e
                                );
                            } else {
                                warn!(
                                    "Failed to store chunk for StackerDB";
                                    "stackerdb_contract_id" => %sync_result.contract_id,
                                    "slot_id" => md.slot_id,
                                    "slot_version" => md.slot_version,
                                    "num_bytes" => chunk.data.len(),
                                    "error" => %e
                                );
                            }
                            continue;
                        } else {
                            log_stored_stackerdb_chunk(&sync_result.contract_id, &chunk, &origin);
                        }
```

**File:** stackslib/src/net/stackerdb/db.rs (L398-438)
```rust
    /// Add or replace a chunk for a given reward cycle, if it is valid
    /// Otherwise, this errors out with Error::StaleChunk
    pub fn try_replace_chunk(
        &self,
        smart_contract: &QualifiedContractIdentifier,
        slot_desc: &SlotMetadata,
        chunk: &[u8],
    ) -> Result<(), net_error> {
        // Check per-replica chunk-size cap.
        if (chunk.len() as u64) > self.config.chunk_size {
            return Err(net_error::StackerDBChunkTooBig(chunk.len()));
        }

        let slot_validation = self
            .get_slot_validation(smart_contract, slot_desc.slot_id)?
            .ok_or(net_error::NoSuchSlot(
                smart_contract.clone(),
                slot_desc.slot_id,
            ))?;

        if !slot_desc.verify(&slot_validation.signer)? {
            return Err(net_error::BadSlotSigner(
                slot_validation.signer,
                slot_desc.slot_id,
            ));
        }
        if slot_desc.slot_version <= slot_validation.version {
            return Err(net_error::StaleChunk {
                supplied_version: slot_desc.slot_version,
                latest_version: slot_validation.version,
            });
        }
        if slot_desc.slot_version > self.config.max_writes {
            return Err(net_error::TooManySlotWrites {
                supplied_version: slot_desc.slot_version,
                latest_version: slot_validation.version,
                max_writes: self.config.max_writes,
            });
        }
        self.insert_chunk(smart_contract, slot_desc, chunk)
    }
```

**File:** stackslib/src/net/tests/relay/nakamoto.rs (L1159-1247)
```rust
/// Verify that the FutureView path in [`PeerNetwork::handle_unsolicited_StackerDBPushChunk`]
/// validates chunks before buffering them.
#[test]
fn test_handle_unsolicited_stackerdb_push_chunk_future_view_validation() {
    let observer = TestEventObserver::new();
    let bitvecs = vec![vec![
        true, true, true, true, true, true, true, true, true, true,
    ]];

    let (mut peer, _followers) =
        make_nakamoto_peers_from_invs(function_name!(), &observer, 10, 5, bitvecs, 1);

    // Register a conversation for event_id 1 so get_p2p_convo() succeeds
    let convo = peer.make_client_convo();
    peer.network.peers.insert(1, convo);

    // Create a test StackerDB with known signers.
    let signer_privk = StacksPrivateKey::from_seed(&[42]);
    let signer_addr = StacksAddress::p2pkh(false, &StacksPublicKey::from_private(&signer_privk));
    let contract_id =
        QualifiedContractIdentifier::parse("ST000000000000000000002AMW42H.test-stackerdb").unwrap();

    // Create the StackerDB in the database with slot 0 owned by our signer
    let slots: Vec<(StacksAddress, u32)> = vec![(signer_addr.clone(), 1)];
    {
        let tx = peer
            .network
            .stackerdbs
            .tx_begin(StackerDBConfig::noop())
            .unwrap();
        tx.create_stackerdb(&contract_id, &slots).unwrap();
        tx.commit().unwrap();
    }

    peer.network.stacker_db_configs.insert(
        contract_id.clone(),
        StackerDBConfig {
            chunk_size: 4096,
            signers: slots.clone(),
            write_freq: 0,
            max_writes: u32::MAX,
            hint_replicas: vec![],
            max_neighbors: 8,
        },
    );

    let mut stacks_node = peer.chain.stacks_node.take().unwrap();

    let preamble = Preamble {
        peer_version: 1,
        network_id: 1,
        seq: 0,
        burn_block_height: 1,
        burn_block_hash: BurnchainHeaderHash([0x01; 32]),
        burn_stable_block_height: 0,
        burn_stable_block_hash: BurnchainHeaderHash([0x00; 32]),
        additional_data: 0,
        signature: MessageSignature::empty(),
        payload_len: 0,
    };

    // Use a bogus rc_consensus_hash that doesn't match the network's view and isn't known
    // in the chain state, which triggers the FutureView Nack path.
    let future_consensus_hash = ConsensusHash([0xfe; 20]);

    // --- Test 1: Properly signed chunk should be BUFFERED on the FutureView path ---
    let mut good_chunk_data = StackerDBPushChunkData {
        contract_id: contract_id.clone(),
        rc_consensus_hash: future_consensus_hash.clone(),
        chunk_data: StackerDBChunkData::new(0, 1, vec![1, 2, 3, 4, 5]),
    };
    good_chunk_data.chunk_data.sign(&signer_privk).unwrap();

    let result = peer
        .network
        .handle_unsolicited_StackerDBPushChunk(
            &mut stacks_node.chainstate,
            1,
            &preamble,
            &good_chunk_data,
            false,
        )
        .unwrap();

    assert_eq!(
        result,
        (true, false),
        "chunk with valid signature must be buffered on FutureView path"
    );
```
