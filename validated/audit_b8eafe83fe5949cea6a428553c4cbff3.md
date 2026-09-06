### Title
StackerDB push-chunk handler advertises a chunk version as "in inventory" without ever persisting it, breaking the *advertised vs. stored* equality - ([File: stackslib/src/net/stackerdb/mod.rs])

### Summary
`PeerNetwork::handle_unsolicited_StackerDBPushChunk` patches the *outgoing* `StackerDBChunkInv` reply's `slot_versions` entry to the pushed chunk's version as soon as `validate_received_chunk` passes, but it never calls `StackerDBTx::try_replace_chunk`/`insert_chunk` to actually write the chunk into the StackerDB. This is the same bug class as the yAxis `Controller.setCap` finding: an accounting value (here, the advertised slot-version inventory) is mutated to reflect a state that the underlying storage does not actually have, so the "balance"/inventory and the real stored data diverge.

### Finding Description
`validate_received_chunk` at [1](#0-0)  only checks the chunk's size, signer signature, and version bounds — it performs no database write. Yet in `handle_unsolicited_StackerDBPushChunk`, after this check succeeds, the code directly patches the local reply's inventory vector to claim the new version is now held: [2](#0-1) 

The function's own doc-comment confirms that no chunk is actually stored here — the *only* mutation permitted is waking the sync state machine: [3](#0-2) 

When `send_reply` is true, the function immediately signs and transmits this patched `StackerDBChunkInv` (claiming possession of the new version) back to the peer, without any interposed call to `try_replace_chunk`: [4](#0-3) 

Ground truth for "do we actually have this chunk/version" lives in the SQLite-backed store, exposed via `get_slot_versions`/`try_replace_chunk`, which independently tracks `SlotValidation.version` and enforces staleness/signature/size/write-count rules per write: [5](#0-4) 

Other nodes' sync logic trusts a peer's advertised `slot_versions` as an accurate proxy for what that peer can actually serve, and will skip pushing/fetching a chunk whenever the peer's claimed version is already current: [6](#0-5) 

So the equality that should hold — "a peer's advertised slot version == a version it can actually serve from `try_replace_chunk`-committed storage" — is broken: the advertisement is derived purely from signature/bounds validation of an unsolicited push, decoupled from the actual commit.

### Impact Explanation
This is a "serving non-canonical state as canonical" style issue: a remote, unauthenticated peer can cause the local node to broadcast an inventory entry for a chunk/version it has not actually stored. Because peer-to-peer sync (`sync.rs`) treats "local_version <= remote_version" as "peer already has it, skip pushing," other replicas can be steered into believing this data is already replicated when it is not, stalling propagation of that StackerDB slot/version across the network (used for Nakamoto signer message coordination) — a network-wide false-inventory effect analogous to how the `Controller.setCap` bug corrupted internal balance accounting and caused downstream operations (`withdrawAll`) to fail/lock. No data is corrupted at rest, but the network's shared view of chunk availability becomes inconsistent with actual storage, which can starve legitimate replication of an update the sender genuinely produced.

### Likelihood Explanation
Reachable by any connected, unauthenticated peer that can send an unsolicited `StackerDBPushChunk` message with a well-formed signature over a fresh version number for a slot it owns (or even a signature the node's `validate_received_chunk` accepts) — no privileged role or secret key of the node itself is required, only a valid StackerDB signer keypair for the target slot, and per code comments this handler is explicitly invoked outside the normal, write-frequency-throttled request path.

### Recommendation
Only patch/advertise the `StackerDBChunkInv` slot version after the chunk has actually been durably committed via `try_replace_chunk` (or immediately re-derive the advertised inventory from `stackerdbs.get_slot_versions` after a successful, synchronous store), rather than from the pass/fail result of `validate_received_chunk` alone.

### Proof of Concept
1. Establish a P2P connection to a node and Identify a StackerDB contract/slot for which you hold (or forge, if signature checking is otherwise satisfiable) a valid signing key at a fresh `slot_version`.
2. Send an unsolicited `StackerDBPushChunk` message for that slot/version with `send_reply=true` semantics triggered.
3. Observe (via debug logging or a follow-up `StackerDBGetChunkInv`/`StackerDBGetChunk` from a third peer) that the node's advertised `StackerDBChunkInv.slot_versions[slot_id]` already reflects the new version, while a `StackerDBGetChunk` request for that exact slot/version at that node returns no chunk / fails, proving the inventory was advanced without a corresponding stored chunk.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L649-656)
```rust
    pub fn validate_received_chunk(
        &self,
        smart_contract_id: &QualifiedContractIdentifier,
        config: &StackerDBConfig,
        data: &StackerDBChunkData,
        expected_versions: &[u32],
    ) -> Result<bool, net_error> {
        // validate -- must not exceed this replica's configured chunk size.
```

**File:** stackslib/src/net/stackerdb/mod.rs (L727-734)
```rust
    /// Note that this can happen *during* a StackerDB sync's execution, so be very careful about
    /// modifying a state machine's contents!  The only modification possible here is to wakeup
    /// the state machine in case it's asleep (i.e. blocked on waiting for the next sync round).
    ///
    /// The write frequency is not checked for this chunk. This is because the `ConversationP2P` on
    /// which this chunk arrived will have already bandwidth-throttled the remote peer, and because
    /// messages can be arbitrarily delayed (and bunched up) by the network anyway.
    ///
```

**File:** stackslib/src/net/stackerdb/mod.rs (L784-815)
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

**File:** stackslib/src/net/stackerdb/sync.rs (L444-461)
```rust
        // who needs data we can serve?
        for (i, local_version) in local_slot_versions.iter().enumerate() {
            let mut local_chunk = None;
            for (naddr, chunk_inv) in self.chunk_invs.iter() {
                if chunk_inv.slot_versions.len() != local_slot_versions.len() {
                    // remote peer and our DB are out of sync, so just skip this
                    continue;
                }

                let Some(remote_version) = chunk_inv.slot_versions.get(i) else {
                    // remote peer isn't tracking this chunk
                    continue;
                };

                if local_version <= remote_version {
                    // remote peer has same or newer view than local peer
                    continue;
                }
```
