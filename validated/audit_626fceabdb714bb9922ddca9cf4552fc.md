### Title
StackerDB push-chunk handler advertises a chunk version as locally held before it is actually persisted, allowing an unauthenticated remote peer to make a node broadcast a false inventory claim - ([File: stackslib/src/net/stackerdb/mod.rs])

### Summary
`PeerNetwork::handle_unsolicited_StackerDBPushChunk` validates an incoming `StackerDBPushChunkData` and then immediately patches the *reply* `StackerDBChunkInv` to claim the new `slot_version` as locally available, before the chunk is ever written to the StackerDB (`StackerDBTx::try_replace_chunk`/`insert_chunk`). The actual storage happens later, asynchronously, via the relayer path (the function returns `(false, true)` to forward the chunk for storage). This breaks the equality that a served/advertised inventory should equal the state actually committed to the database, mirroring the "authenticated vs stored" mismatch class from the Elytra report (there: a tracked accounting variable diverged from actual pool balance; here: an advertised StackerDB inventory diverges from actual persisted slot state).

### Finding Description
In `handle_unsolicited_StackerDBPushChunk`: [1](#0-0) 

the function builds a chunk inventory reply via `make_StackerDBChunksInv_or_Nack`, which reads the *current* on-disk slot versions: [2](#0-1) 

It then runs `validate_received_chunk` (signature, size, staleness, max-writes checks — but no DB write): [3](#0-2) 

and, having validated the chunk only cryptographically/structurally, directly mutates the in-memory `data.slot_versions` entry to the *new*, not-yet-stored version: [4](#0-3) 

This patched `StackerDBChunkInv` is then signed and sent back to the sender as the node's official inventory reply: [5](#0-4) 

Meanwhile, the actual persistence of the chunk into the sqlite-backed store only happens later, via `StackerDBTx::try_replace_chunk` / `insert_chunk`, which is a separate DB transaction typically driven by the relay path once `(false, true)` is returned to the caller: [6](#0-5) 

Between validating the chunk in `handle_unsolicited_StackerDBPushChunk` and the deferred storage step, the node has already told the remote peer (and, transitively, anyone else who queries it or who relays this data further) that it possesses `slot_version = N` for that slot. If storage subsequently doesn't happen — because the relay path drops it, encounters a DB error, the node restarts, or the tx is rolled back for any reason (e.g., a concurrent write races and makes it stale by the time `try_replace_chunk` actually executes) — the node's advertised inventory is now permanently wrong until the next real sync recomputes it from `get_slot_versions`. Any peer that trusted this inventory will believe this node is a valid source for that chunk version and will not seek it elsewhere (per the "rarest-first"/newest-first prioritization logic in `sync.rs`), effectively serving non-canonical (unstored) state as canonical.

This exactly parallels the Elytra bug class: a bookkeeping value (`totalAssetDepositsTracked`, here the advertised `slot_versions` inventory) is updated optimistically without confirming the corresponding real state change (pool balance / DB commit) actually took place, and no revert/rollback path corrects the tracked value if the real update doesn't happen.

### Impact Explanation
This meets the "High" bar for the class: "steering a node off the tip via false inventory." An unauthenticated remote peer can cause a node to broadcast a StackerDB chunk inventory that does not correspond to its actual persisted state. Downstream peers that rely on this inventory for rarest/newest-first chunk-fetch scheduling will mark this slot as satisfied and skip fetching it from other, actually-correct sources, causing that chunk version to fail to propagate through the network from this path (a partial DoS on StackerDB chunk convergence / freshness), and it undermines the eventual-consistency guarantee the StackerDB module document explicitly claims to provide.

### Likelihood Explanation
Likelihood is Low-to-Medium: it requires a race or failure between chunk validation and eventual storage (relay drop, DB error, restart, or a concurrent stale write beating the deferred store), which is plausible but not trivially reproducible on every push. It requires no privileged access — any peer able to send a `StackerDBPushChunk` message (any properly connected p2p neighbor) can trigger the validation/reply path.

### Recommendation
Do not patch/advertise the new `slot_version` in the `StackerDBChunkInv` reply until the chunk has actually been durably written via `try_replace_chunk`/`insert_chunk`. Either perform the store synchronously before constructing/sending the reply, or omit optimistic patching and instead re-read `get_slot_versions` after storage completes (deferring the reply until the write is confirmed, or sending a reply only for the previously-confirmed version and letting a subsequent sync round pick up the truly-stored version).

### Proof of Concept
1. A remote peer with a valid, correctly-signed `StackerDBChunkData` for slot `i` at version `v+1` sends an unsolicited `StackerDBPushChunk` to a target node.
2. The target's `PeerNetwork::handle_unsolicited_StackerDBPushChunk` calls `validate_received_chunk`, which passes (signature/size/staleness/max-writes are all fine).
3. The function patches `data.slot_versions[i] = v+1` and immediately signs/sends this `StackerDBChunkInv` back to the sender, returning `(false, true)` to forward the chunk to the relay path for actual storage.
4. Before the relay path actually calls `try_replace_chunk` (e.g., due to a transient DB error, a race with another writer causing `StaleChunk`/`BadSlotSigner` rejection, or a node crash/restart in between), the chunk is never persisted.
5. The target node's on-disk `get_slot_versions` for slot `i` still reports `v`, but it already advertised `v+1` to at least one peer, which will treat the target as an up-to-date source for that slot and deprioritize fetching it elsewhere — the false inventory has already propagated.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L583-639)
```rust
    /// Create a StackerDBChunksInv, or a Nack if the requested DB isn't replicated here.
    /// Runs in response to a received StackerDBGetChunksInv or a StackerDBPushChunk
    pub fn make_StackerDBChunksInv_or_Nack(
        &self,
        naddr: NeighborAddress,
        chainstate: &mut StacksChainState,
        contract_id: &QualifiedContractIdentifier,
        rc_consensus_hash: &ConsensusHash,
    ) -> StacksMessageType {
        // N.B. check that the DB exists first, since we want to report StaleView only if the DB
        // exists
        let slot_versions = match self.stackerdbs.get_slot_versions(contract_id) {
            Ok(versions) => versions,
            Err(e) => {
                debug!(
                    "{:?}: failed to get chunk versions for {}: {:?}",
                    self.get_local_peer(),
                    contract_id,
                    &e
                );

                // most likely indicates that this DB doesn't exist
                return StacksMessageType::Nack(NackData::new(NackErrorCodes::NoSuchDB));
            }
        };

        // this DB exists, but is the view of this message recent?
        if &self.get_chain_view().rc_consensus_hash != rc_consensus_hash {
            // is there a Stacks block (or tenure) with this consensus hash?
            let tip_block_id = self.stacks_tip.block_id();
            if let Ok(Some(_)) = NakamotoChainState::get_tenure_start_block_header(
                &mut chainstate.index_conn(),
                &tip_block_id,
                rc_consensus_hash,
            ) {
                debug!("{:?}: NACK StackerDBGetChunksInv / StackerDBPushChunk from {} since {} != {} (remote is stale)", self.get_local_peer(), &naddr, &self.get_chain_view().rc_consensus_hash, rc_consensus_hash);
                return StacksMessageType::Nack(NackData::new(NackErrorCodes::StaleView));
            } else {
                debug!("{:?}: NACK StackerDBGetChunksInv / StackerDBPushChunk from {} since {} != {} (local is potentially stale)", self.get_local_peer(), &naddr, &self.get_chain_view().rc_consensus_hash, rc_consensus_hash);
                return StacksMessageType::Nack(NackData::new(NackErrorCodes::FutureView));
            }
        }

        let num_outbound_replicas = self.count_outbound_stackerdb_replicas(contract_id) as u32;

        debug!(
            "{:?}: inventory for {} has {} outbound replicas; versions are {:?}",
            self.get_local_peer(),
            contract_id,
            num_outbound_replicas,
            &slot_versions
        );
        StacksMessageType::StackerDBChunkInv(StackerDBChunkInvData {
            slot_versions,
            num_outbound_replicas,
        })
    }
```

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

**File:** stackslib/src/net/stackerdb/mod.rs (L761-815)
```rust
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

**File:** stackslib/src/net/stackerdb/db.rs (L371-438)
```rust
    /// Insert a chunk into the DB.
    /// It must be authenticated, and its lamport clock must be higher than the one that's already
    /// there.  These will not be checked.
    fn insert_chunk(
        &self,
        smart_contract: &QualifiedContractIdentifier,
        slot_desc: &SlotMetadata,
        chunk: &[u8],
    ) -> Result<(), net_error> {
        let stackerdb_id = self.get_stackerdb_id(smart_contract)?;
        let sql = "UPDATE chunks SET version = ?1, data_hash = ?2, signature = ?3, data = ?4, write_time = ?5 WHERE stackerdb_id = ?6 AND slot_id = ?7";
        let mut stmt = self.sql_tx.prepare(sql)?;

        let args = params![
            slot_desc.slot_version,
            Sha512Trunc256Sum::from_data(chunk),
            slot_desc.signature,
            chunk,
            u64_to_sql(get_epoch_time_secs())?,
            stackerdb_id,
            slot_desc.slot_id,
        ];

        stmt.execute(args)?;
        Ok(())
    }

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
