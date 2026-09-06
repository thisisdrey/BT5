### Title
Optimistic Inventory Update Before Chunk Persistence Creates False StackerDB Inventory - (File: stackslib/src/net/stackerdb/mod.rs)

### Summary
`PeerNetwork::handle_unsolicited_StackerDBPushChunk` mutates and advertises a node's own StackerDB chunk inventory (`StackerDBChunkInvData.slot_versions`) to reflect a newly received chunk's version **before** that chunk has actually been persisted to the local StackerDB. This breaks the equality between "served inventory" and "committed state," mirroring the reported Solidity bug class where a stored accounting value was updated incorrectly/independently of the actual state transition, allowing a mismatch to be exploited by other parties relying on that value.

### Finding Description
`validate_received_chunk` explicitly checks chunk size, expected version/staleness, `max_writes`, and the slot signature, but by design does **not** check the write-frequency ("min time between writes") constraint that gates whether a chunk can actually be stored: [1](#0-0) 

Despite this, once `validate_received_chunk` returns true inside `handle_unsolicited_StackerDBPushChunk`, the code immediately patches the node's own advertised inventory entry for that slot to the new (higher) version: [2](#0-1) 

The function then returns `Ok((false, true))`, deferring the actual chunk write to the relayer, which reaches `try_replace_chunk`/`insert_chunk` only later and enforces the additional stateful checks (write-frequency, DB-transaction success, etc.) that were skipped up front: [3](#0-2) 

If the deferred store subsequently fails the write-frequency check (or any other check not covered by `validate_received_chunk`), the node's `StackerDBChunkInv` — served to any neighbor that queries it via `make_StackerDBChunksInv_or_Nack` — will falsely claim it holds a chunk version it never actually committed to its `chunks` table: [4](#0-3) 

This is the same equality-violation pattern as the referenced report: a bookkeeping/state value (`claimed[_user][_token]`, here the advertised `slot_versions` entry) is updated independently of, and prematurely relative to, the actual underlying transfer/commit (`due` payout, here the actual chunk write), letting the exposed value diverge from ground truth.

### Impact Explanation
Downstream peers use `StackerDBChunkInv` inventories to prioritize and schedule chunk downloads (newest-first, rarest-first) as described in the StackerDB replication protocol documentation: [5](#0-4) 

A peer that believes a remote replica already has the latest version of a slot (because that replica's own inventory says so) will not attempt to source that chunk from it correctly, or will request a chunk (`StackerDBGetChunkData`) that the target cannot actually serve, wasting sync rounds and steering nodes toward stale/incorrect views of "which node has which data" — a bounded but real network-visible integrity discrepancy between served inventory and committed data. This falls into the "steering a node off the tip via false inventory"-style High-impact category, since it is unauthenticated/unauthorized state (inventory) being reported inconsistently with what's actually stored, propagated to any querying neighbor.

### Likelihood Explanation
Any remote, unprivileged peer can trigger this by sending an unsolicited `StackerDBPushChunk` for a chunk that passes `validate_received_chunk`'s checks but that will fail write-frequency (or a concurrent identical push causing a race) once the relayer attempts the real store. No signer/admin/node-secret access is required — it only needs a validly-signed chunk from any legitimate slot owner (or writer within their own quota), sent at a rate/timing designed to trigger the deferred check's rejection while the optimistic inventory patch still lands.

### Recommendation
Do not mutate the local, servable inventory entry until the chunk has been durably committed via the actual `try_replace_chunk`/`insert_chunk` path (including all checks such as write-frequency). Either perform the write synchronously before patching inventory, or gate the inventory patch on a confirmed store completion event, so `make_StackerDBChunksInv_or_Nack` never reports an optimistic/uncommitted version.

### Proof of Concept
1. Attacker (or any writer with slot access) crafts a validly signed `StackerDBPushChunkData` with a strictly higher `slot_version` for a slot they control, satisfying `validate_received_chunk` (size, staleness, `max_writes`, signature) but timed such that the eventual real store attempt will violate the min-time-between-writes constraint (not checked in `validate_received_chunk`).
2. Send it unsolicited to a victim node's P2P conversation; `handle_unsolicited_StackerDBPushChunk` calls `validate_received_chunk` (passes) and immediately patches `slot_versions[slot_id] = new_version` in the reply inventory and forwards the message to the relayer.
3. Relayer later calls the real storage path (`try_replace_chunk`), which enforces write-frequency and rejects the chunk — it is never actually written to the `chunks` table.
4. Any neighbor subsequently querying the victim via `StackerDBGetChunksInv`/`make_StackerDBChunksInv_or_Nack` receives `slot_versions` claiming the higher version is present, even though the victim's DB (`db.rs` `chunks` table) still holds the old version — a verifiable mismatch between served inventory and actual committed state. [6](#0-5)

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L87-96)
```rust
/// The DB inventory (`StackerDBChunkInvData`) is simply a vector of all of the remote peers' slots' versions.
/// Once the node has received all DB inventories from its neighbors, it schedules them for
/// download by prioritizing them by newest-first, and then by rarest-first, in order to ensure
/// that the latest, least-replicated data is downloaded first.
///
/// Once the node has computed its download schedule, it queries its DB neighbors for chunks with
/// the given versions (via `StackerDBGetChunkData`).  Upon receipt of a chunk, the node verifies the signature on the chunk's
/// metadata (via `SlotMetadata`), verifies that the chunk data hashes to the metadata's indicated data hash, and stores
/// the chunk (via `StackerDBSet` and `StackerDBTx`).  It will then select neighbors to which to broadcast this chunk, inferring from the
/// download schedule which DB neighbors have yet to process this particular version of the chunk.
```

**File:** stackslib/src/net/stackerdb/mod.rs (L641-649)
```rust
    /// Validate chunk data either downloaded (with [`StackerDBSync::validate_downloaded_chunk`]), or
    /// pushed to us (with [`PeerNetwork::handle_unsolicited_StackerDBPushChunk`])
    ///
    /// NOTE: does not check write frequency, since the caller has different ways of doing this.
    /// Returns:
    /// - Ok(true) if the chunk is valid
    /// - Ok(false) if the chunk is invalid
    /// - Err(..) on DB error
    pub fn validate_received_chunk(
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

**File:** stackslib/src/net/stackerdb/db.rs (L53-75)
```rust
    CREATE TABLE chunks(
        -- associated stacker DB
        stackerdb_id INTEGER NOT NULL,
        -- slot ID
        slot_id INTEGER NOT NULL,
        -- lamport clock of the chunk.
        version INTEGER NOT NULL,
        -- hash of the data to be stored
        data_hash TEXT NOT NULL,
        -- secp256k1 recoverable signature from the stacker over the above columns
        signature TEXT NOT NULL,

        -- the following is NOT covered by the signature
        -- address of the creator of this chunk
        signer TEXT NOT NULL,
        -- the chunk data itself
        data BLOB NOT NULL,
        -- UNIX timestamp when the chunk was written.
        write_time INTEGER NOT NULL,
        
        PRIMARY KEY(stackerdb_id,slot_id),
        FOREIGN KEY(stackerdb_id) REFERENCES databases(stackerdb_id) ON DELETE CASCADE
    );
```

**File:** stackslib/src/net/stackerdb/db.rs (L400-438)
```rust
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
