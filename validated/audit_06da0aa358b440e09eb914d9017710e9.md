## Title
StackerDB `write_freq` write-rate limit is never enforced at the chunk-storage layer, only advisory on the fetch path - ([File: stackslib/src/net/stackerdb/db.rs])

### Summary
The RocketPool finding is about a configured rate parameter (the inflation APD/interval) that is supposed to gate state transitions but, due to a bookkeeping fault, the enforcement point drifts from the configured value, letting the real behavior diverge from what the DAO configured. The Stacks analog is the StackerDB `write-freq` contract parameter: it is parsed and validated as a hard per-DB rate limit [1](#0-0)  and is even given a dedicated rejection error, `Error::TooFrequentSlotWrites` [2](#0-1) , but that error is never actually constructed anywhere in the codebase — the write path that stores a chunk (`try_replace_chunk`/`insert_chunk`) performs no check against `write_freq` or the slot's last write timestamp at all.

### Finding Description
`StackerDBTx::try_replace_chunk` is the sole gate that decides whether a submitted chunk is accepted into a slot. It validates chunk size, slot existence, signer authenticity, staleness (version must be `>` current), and `max_writes`, but never inspects `config.write_freq` or the slot's `write_time` column: [3](#0-2) 

The only place `write_freq` is consulted at all is in the *fetch scheduling* logic of the sync state machine, which skips requesting a chunk from a peer if it was written too recently — this only throttles what the local node decides to download, not what it or anyone else may write: [4](#0-3) 

Both the local write endpoint and the network gossip path funnel into this same unguarded `try_replace_chunk`:
- The RPC handler `RPCPostStackerDBChunkRequestHandler::try_handle_request` calls `tx.try_replace_chunk` directly on any HTTP POST of a chunk [5](#0-4) .
- `handle_unsolicited_StackerDBPushChunk` explicitly documents that write frequency is *not* checked for gossip-pushed chunks, reasoning that bandwidth throttling elsewhere covers it: [6](#0-5) . Once such a chunk is accepted, the node patches its own inventory and continues to advertise/relay it to further peers [7](#0-6) .

So the equality that should hold — "a slot's chunk cannot be replaced more than once per `write_freq` seconds, as configured by the controlling smart contract" — is broken: the configured value is parsed and carried around (`StackerDBSync.write_freq`, `StackerDBConfig.write_freq`) but the actual storage mutation path has no corresponding check.

### Impact Explanation
Any party holding the private key for a slot signer (a legitimate but potentially malicious StackerDB participant, e.g. one signer among many) can write new chunk versions at an arbitrary rate far exceeding the DAO/contract-configured `write_freq`. Because every accepted write is broadcast/relayed via the StackerDB push/pull gossip protocol to all replicating peers network-wide [8](#0-7) , this can be used to force repeated network-wide chunk propagation and repeated SQLite writes on every replica, well beyond what the write-frequency throttle was designed to bound. This is a network-wide amplification/DoS vector stemming from an un-enforced protocol invariant, not simple traffic-volume flooding — the throttle exists specifically to bound this class of activity and is silently bypassed.

### Likelihood Explanation
Any authorized slot signer for any StackerDB (e.g., a Nakamoto signer set member) can trigger this immediately and repeatedly with no additional privilege beyond their own existing slot key, using either the plain HTTP POST endpoint or by injecting `StackerDBPushChunk` messages over the P2P protocol.

### Recommendation
Enforce `write_freq` inside `StackerDBTx::try_replace_chunk` (or an equivalent gate shared by both the RPC write path and gossip acceptance path) by comparing the current time against the slot's stored `write_time` before allowing a version bump, returning `Error::TooFrequentSlotWrites` when violated (the variant already exists but is dead code). Apply this uniformly regardless of whether the chunk arrived via HTTP POST or unsolicited P2P push.

### Proof of Concept
1. Configure a StackerDB contract with a nonzero `write-freq` (e.g., 600 seconds).
2. As the owner of slot `N`, sign and POST a chunk at version `V` via `poststackerdbchunk` — accepted per `try_replace_chunk` [3](#0-2) .
3. Immediately sign and POST another chunk at version `V+1` for the same slot. Because `try_replace_chunk` only checks `slot_version > slot_validation.version` and `max_writes`, this succeeds instantly, with no `write_freq` wait enforced, and the node relays/gossips both versions to its peers.

### Citations

**File:** stackslib/src/net/stackerdb/config.rs (L422-437)
```rust
        let write_freq = config_tuple
            .get("write-freq")
            .expect("FATAL: missing 'write-freq'")
            .clone()
            .expect_u128()?;
        if write_freq > u64::MAX as u128 {
            let reason = format!(
                "Contract {} stipulates a write frequency beyond u64::MAX",
                contract_id
            );
            warn!("{}", &reason);
            return Err(NetError::InvalidStackerDBContract(
                contract_id.clone(),
                reason,
            ));
        }
```

**File:** stackslib/src/net/mod.rs (L244-245)
```rust
    /// too frequent writes to a slot
    TooFrequentSlotWrites(u64),
```

**File:** stackslib/src/net/stackerdb/db.rs (L400-437)
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
```

**File:** stackslib/src/net/stackerdb/sync.rs (L338-352)
```rust
            .zip(local_write_timestamps.iter())
        {
            if self.write_freq > 0 && write_ts + self.write_freq > now {
                debug!(
                    "{:?}: {}: Chunk {} was written too frequently ({} + {} > {}) in {}, so will not fetch chunk",
                    network.get_local_peer(),
                    &self.smart_contract_id,
                    i,
                    write_ts,
                    self.write_freq,
                    now,
                    &self.smart_contract_id,
                );
                continue;
            }
```

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L197-201)
```rust
                if let Err(e) = tx.try_replace_chunk(
                    &contract_identifier,
                    &stackerdb_chunk.get_slot_metadata(),
                    &stackerdb_chunk.data,
                ) {
```

**File:** stackslib/src/net/stackerdb/mod.rs (L85-96)
```rust
/// `StackerDBGetChunkInData` messages.
///
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

**File:** stackslib/src/net/stackerdb/mod.rs (L731-734)
```rust
    /// The write frequency is not checked for this chunk. This is because the `ConversationP2P` on
    /// which this chunk arrived will have already bandwidth-throttled the remote peer, and because
    /// messages can be arbitrarily delayed (and bunched up) by the network anyway.
    ///
```

**File:** stackslib/src/net/stackerdb/mod.rs (L794-814)
```rust
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
```
