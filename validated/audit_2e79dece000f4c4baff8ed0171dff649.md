### Title
Missing `write_freq` (minimum write-interval) enforcement in `try_replace_chunk` allows unthrottled StackerDB chunk floods that trigger network-wide broadcast — (File: `stackslib/src/net/stackerdb/db.rs`)

### Summary
The `StackerDBConfig::write_freq` field is intended to rate-limit how often a slot owner can write a new chunk to a StackerDB replica. It is parsed from the on-chain contract config [1](#0-0)  and is only consumed by the peer-to-peer sync state machine to decide when to re-fetch/re-push data from *remote* peers [2](#0-1) . It is never enforced at the actual write-acceptance point, `StackerDBTx::try_replace_chunk`, which only checks chunk size, signer validity, version staleness, and the `max_writes` cap — but never checks elapsed time since the last write [3](#0-2) .

### Finding Description
`try_replace_chunk` is the single gate used both by the local HTTP write path (`POST /v2/stackerdb/{principal}/{contract}/chunks`) and by the p2p sync path when storing chunks obtained from peers [4](#0-3) . Its checks are:
- chunk size ≤ `config.chunk_size`
- `slot_desc.verify(&slot_validation.signer)` (must be signed by the authorized slot owner)
- `slot_version > slot_validation.version` (must be a newer version, else `StaleChunk`)
- `slot_version <= config.max_writes` (else `TooManySlotWrites`) [5](#0-4) 

There is no check against `config.write_freq` anywhere in this function or in `insert_chunk`. Any principal holding one of the slot signing keys for a given StackerDB contract (e.g. a registered `.signers` participant for the current reward cycle — something an attacker can legitimately obtain by registering/staking like a normal participant, analogous to the Allora reporter registering their own reputer/stake in the referenced report) can therefore submit a rapid sequence of validly-signed, strictly-increasing-version chunks with no minimum time between writes, bounded only by `max_writes`.

Every accepted write in `poststackerdbchunk.rs` immediately queues a `StackerDBPushChunk` relay message [6](#0-5) , which the relayer later broadcasts to every connected peer via `self.p2p.broadcast_message(vec![], msg)` [7](#0-6) . Each receiving peer must then run full chunk validation — signature recovery, slot lookup, and versioning checks — in `validate_received_chunk`/`handle_unsolicited_StackerDBPushChunk` before deciding whether to store or forward it further [8](#0-7) [9](#0-8) .

This is the same bug class as the referenced Sherlock finding: the absence of a minimum-interval/batching restriction on a cheap, individually-authorized operation lets a single principal drive an unbounded, tight loop of writes that each fan out into expensive, network-wide processing (signature verification + broadcast relay) on every peer — degrading throughput rather than being limited to the attacker's own node.

### Impact Explanation
This maps to the "High — bounded compute DoS on a read/write endpoint" and partially to "network-wide propagation" impact tier: a single authorized-but-malicious signer can force every peer on the network to repeatedly perform ECDSA signature recovery and StackerDB writes/broadcasts at a rate limited only by `max_writes` and network bandwidth, with no server-side minimum-interval throttle as the `write_freq` config field was evidently designed to provide. This can slow down block/transaction relay processing on all nodes that are also busy validating/broadcasting these chunks, similar in spirit to the referenced report's chain-slowdown/halt impact, though contained to the StackerDB message-processing pipeline rather than consensus itself.

### Likelihood Explanation
Requires only: (1) being a legitimate slot signer for some StackerDB contract (obtainable the same way any signer/stacker/miner participant obtains a slot — not another party's key), and (2) sending POST requests to the node's own RPC endpoint as fast as possible. No special privilege beyond normal signer registration is needed, and the resulting broadcast reaches the whole network, not just the attacker's own node.

### Recommendation
Enforce `config.write_freq` inside `try_replace_chunk` (or an equivalent gate reached by both the HTTP POST path and sync path) by tracking the last-write timestamp per slot and rejecting/erroring (e.g., a new `TooFrequentWrites` error) any chunk submitted before `write_freq` seconds have elapsed since the slot's last accepted write, mirroring the way `max_writes` is already enforced as a hard cap.

### Proof of Concept
1. Register/obtain a signer key `sk` that owns slot `S` in a StackerDB contract `C` with a configured `write_freq > 0`.
2. Repeatedly submit `POST /v2/stackerdb/{C}/chunks` requests signed by `sk`, incrementing `slot_version` each time (per `StacksHttpRequest::new_post_stackerdb_chunk`, see `stackslib/src/net/api/poststackerdbchunk.rs` lines 346-373), with no delay between requests.
3. Observe that `try_replace_chunk` accepts every request as fast as it is sent (bounded only by `max_writes`), since no time-based check exists in `stackslib/src/net/stackerdb/db.rs` lines 398-439.
4. Each accepted write triggers a `StackerDBPushChunk` broadcast to all peers (`poststackerdbchunk.rs` lines 315-324 → `relay.rs` lines 2445-2452), forcing every peer to perform signature verification and storage/relay work in `validate_received_chunk`/`handle_unsolicited_StackerDBPushChunk` (`stackerdb/mod.rs` lines 649-815) at the attacker's chosen rate.

*Note: I was unable to find any code path elsewhere in the indexed `stackslib/src/net/**` tree that enforces `write_freq` at write-acceptance time; my search for `write_freq` usages returned only `sync.rs` (p2p sync scheduling), `config.rs`/`mod.rs` (config parsing/defaults), and test files. If such enforcement exists outside the indexed portions of the repo, it would invalidate this finding — a full-repo grep via a Devin session would be needed to rule this out with certainty.*

### Citations

**File:** stackslib/src/net/stackerdb/config.rs (L496-503)
```rust
        Ok(StackerDBConfig {
            chunk_size: chunk_size as u64,
            signers,
            write_freq: write_freq as u64,
            max_writes: max_writes as u32,
            hint_replicas,
            max_neighbors: max_neighbors as usize,
        })
```

**File:** stackslib/src/net/stackerdb/sync.rs (L42-60)
```rust
impl<NC: NeighborComms> StackerDBSync<NC> {
    pub fn new(
        smart_contract: QualifiedContractIdentifier,
        config: &StackerDBConfig,
        comms: NC,
        stackerdbs: StackerDBs,
    ) -> StackerDBSync<NC> {
        let mut dbsync = StackerDBSync {
            state: StackerDBSyncState::ConnectBegin,
            rc_consensus_hash: None,
            smart_contract_id: smart_contract,
            num_slots: config.num_slots() as usize,
            write_freq: config.write_freq,
            chunk_invs: HashMap::new(),
            chunk_fetch_priorities: vec![],
            chunk_push_priorities: vec![],
            chunk_push_receipts: HashMap::new(),
            next_chunk_fetch_priority: 0,
            next_chunk_push_priority: 0,
```

**File:** stackslib/src/net/stackerdb/db.rs (L398-439)
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
}
```

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L197-209)
```rust
                if let Err(e) = tx.try_replace_chunk(
                    &contract_identifier,
                    &stackerdb_chunk.get_slot_metadata(),
                    &stackerdb_chunk.data,
                ) {
                    test_debug!(
                        "Failed to replace chunk {}.{} in {}: {:?}",
                        stackerdb_chunk.slot_id,
                        stackerdb_chunk.slot_version,
                        &contract_identifier,
                        &e
                    );
                    // Classify the rejection directly from the error. `StaleChunk` is the
```

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L315-324)
```rust
        if ack_resp.accepted {
            let push_chunk_data = StackerDBPushChunkData {
                contract_id: contract_identifier,
                rc_consensus_hash: node.with_node_state(|network, _, _, _, _| {
                    network.get_chain_view().rc_consensus_hash.clone()
                }),
                chunk_data: stackerdb_chunk,
            };
            node.set_relay_message(StacksMessageType::StackerDBPushChunk(push_chunk_data));
        }
```

**File:** stackslib/src/net/relay.rs (L2445-2452)
```rust
                        let msg = StacksMessageType::StackerDBPushChunk(StackerDBPushChunkData {
                            contract_id: sc.clone(),
                            rc_consensus_hash: rc_consensus_hash.clone(),
                            chunk_data: chunk,
                        });
                        if let Err(e) = self.p2p.broadcast_message(vec![], msg) {
                            warn!("Failed to broadcast StackerDB chunk: {e:?}");
                        }
```

**File:** stackslib/src/net/stackerdb/mod.rs (L649-717)
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
```

**File:** stackslib/src/net/stackerdb/mod.rs (L742-815)
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
            StacksMessageType::StackerDBChunkInv(ref mut data) => {
                // this message corresponds to an existing DB, and comes from the same view of the
                // stacks chain tip
                let stackerdb_config = if let Some(config) =
                    self.get_stacker_db_configs().get(&chunk_data.contract_id)
                {
                    config
                } else {
                    // not for this DB
                    info!(
                        "StackerDBChunk for {} ID {} is not available locally",
                        &chunk_data.contract_id, chunk_data.chunk_data.slot_id
                    );
                    return Ok((false, false));
                };

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

                // wake up the state machine -- force it to begin a new sync if it's asleep
                if let Some(stackerdb_syncs) = self.stacker_db_syncs.as_mut() {
                    if let Some(stackerdb_sync) = stackerdb_syncs.get_mut(&chunk_data.contract_id) {
                        stackerdb_sync.wakeup();
                    }
                }
            }
```
