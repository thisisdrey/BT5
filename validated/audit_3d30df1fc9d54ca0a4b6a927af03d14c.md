## Title
Unsolicited StackerDB chunk-push handler advertises chunk acceptance in its `StackerDBChunkInv` reply before the chunk is actually committed, allowing served inventory state to diverge from stored state - (File: `stackslib/src/net/stackerdb/mod.rs`)

### Summary
`PeerNetwork::handle_unsolicited_StackerDBPushChunk` validates an incoming `StackerDBPushChunk` and then patches the `slot_version` field of the outgoing `StackerDBChunkInv` reply to claim the chunk has been accepted, *before* the chunk has actually been written to the local StackerDB replica. The real write (`StackerDBTx::try_replace_chunk`) happens later, asynchronously, in the relayer. Because the "accepted" inventory reply is computed from a point-in-time snapshot and sent synchronously, while the durable write is deferred and can still fail (e.g. lose a race to a concurrent write to the same slot), the node can tell a peer "I have version V" for a chunk it never actually stores — an instance of "served" state diverging from "committed" state, the exact equality class called out in the prompt.

### Finding Description
In `stackslib/src/net/stackerdb/mod.rs`, `handle_unsolicited_StackerDBPushChunk` does the following on receipt of an unsolicited `StackerDBPushChunk` message:

1. It builds a `StackerDBChunkInv`/Nack reply from the current on-disk slot versions via `make_StackerDBChunksInv_or_Nack`. [1](#0-0) 
2. It calls `validate_received_chunk`, which only checks size, expected-version freshness, signer correctness, and max-writes — it does **not** write anything to the database. [2](#0-1) 
3. Having passed validation, it directly mutates the *in-memory reply* to assert the new, higher `slot_version` as if the chunk were already stored: [3](#0-2) 
4. This patched, "accepted" inventory reply is then signed and sent back to the peer immediately: [4](#0-3) 
5. The function returns `(false, true)`, meaning "forward the message to the relayer" — the *actual* durable write only happens later, out-of-band, in `Relayer`/`PeerNetwork::process_stacker_db_chunks`, which calls `StackerDBTx::try_replace_chunk`: [5](#0-4) 

`try_replace_chunk` re-checks the slot version against whatever is *currently* in the DB at write time and rejects (`StaleChunk`) if a concurrent write already advanced the version past what this chunk supplies: [6](#0-5) 

Because the "accept" decision announced to the network (step 3/4) is made from a stale, pre-write snapshot, and the true acceptance decision (step 5) happens later against the live DB state, two legitimately-signed, increasing-version chunk pushes for the same slot arriving via two different connections/event handlers race each other: both can pass `validate_received_chunk` against the snapshot taken before either write lands, so both trigger a patched, "I now have version V" reply sent back to their respective senders — but only one `try_replace_chunk` call can actually win; the other is silently dropped in `process_stacker_db_chunks` as a `StaleChunk` (logged at `debug` level only): [7](#0-6) 

The result: the node has told a peer/neighbor "I have accepted and now hold slot version V" via the immediate `StackerDBChunkInv` reply, but its actually-committed replica may contain a different chunk/version. Any subsequent, legitimate `StackerDBGetChunkInv` query is answered from the true, committed slot versions (via `self.stackerdbs.get_slot_versions`), which will disagree with what was promised moments earlier. [8](#0-7) 

This is directly analogous to the reported Solidity issue: the “genesisValidators”/weight bookkeeping in `depositWithConfirm`/`withdrawWithConfirm` is updated out of sync with the real collateral/weight state, letting a stale or wrong value be treated as canonical. Here, the StackerDB inventory reply is updated out of sync with the real, durably-committed chunk state, letting a stale/incorrect "accepted" acknowledgment be treated as canonical by the network.

### Impact Explanation
Peers that rely on this node's advertised inventory (whether from the direct reply or from later `StackerDBChunkInv` responses gossiped/queried by other neighbors) may believe the node already possesses the newest chunk for a slot when it does not, and thus decline to re-fetch/re-push that data to it (the sync/push-scheduling logic in `stackslib/src/net/stackerdb/sync.rs` only requests/pushes chunks whose local version is behind a neighbor's advertised version). This can create a durable gap in StackerDB replication for the affected slot — most importantly for `.signers`/`.miners` StackerDBs used to gossip block-commit signatures — potentially delaying or stalling propagation of signer messages needed for block confirmation, i.e. serving non-canonical replication state as canonical to peers making relay/sync decisions.

### Likelihood Explanation
The race requires two chunk pushes for the same slot to be handled by the node close together via two different event/connection contexts (e.g. the legitimate signer pushing an update while it is also being echoed/relayed back by another peer, which is an explicitly acknowledged scenario elsewhere in this codebase — see the related fix note about “stackerdb-uploaded-chunk-event-loss”, which documents that the relayer can fall behind or race a peer's echo of the same chunk). No attacker private keys beyond the normal slot signer are required to trigger it in principle; it can happen as ordinary network jitter/reordering between the signer's own direct upload and a neighbor's rebroadcast of the same or a newer chunk. [9](#0-8) 

### Recommendation
Do not compute or send an "accepted" `StackerDBChunkInv` patch in `handle_unsolicited_StackerDBPushChunk` until the chunk has actually been durably written via `try_replace_chunk`. Either perform the write synchronously (inside the P2P handler, with the transaction) before replying, or defer the reply/inventory update until the relayer confirms the write succeeded, re-querying the true on-disk slot versions rather than patching an in-memory snapshot.

### Proof of Concept
1. Configure a StackerDB slot owned by a signer key.
2. Establish two separate P2P connections to the victim node.
3. From connection A, send `StackerDBPushChunk` for slot 0 with `slot_version = N` (validly signed).
4. Immediately, before A's chunk is durably committed by the relayer, from connection B send `StackerDBPushChunk` for slot 0 with `slot_version = N+1` (validly signed, e.g. an updated chunk from the legitimate signer relayed by another peer).
5. Because both calls to `handle_unsolicited_StackerDBPushChunk` read the same pre-write slot-version snapshot via `make_StackerDBChunksInv_or_Nack`, both pass `validate_received_chunk` and both immediately reply with a patched `StackerDBChunkInv` claiming acceptance of their respective versions.
6. When the relayer processes both in `process_stacker_db_chunks`, whichever `try_replace_chunk` call executes second observes the already-advanced version and fails with `StaleChunk`, and is silently dropped.
7. Querying the node's true inventory via `StackerDBGetChunkInv` afterward (`make_stacker_db_getchunkinv_response` → `get_slot_versions`) shows a version that disagrees with what was promised to the loser's sender in step 5, confirming served/committed divergence.

### Citations

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

**File:** stackslib/src/net/stackerdb/mod.rs (L761-792)
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
```

**File:** stackslib/src/net/stackerdb/mod.rs (L794-807)
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

**File:** stackslib/src/net/stackerdb/db.rs (L411-429)
```rust
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
```

**File:** stackslib/src/net/chat.rs (L1907-1922)
```rust
    /// Handle an inbound StackerDBGetChunkInv request.
    /// Generates a StackerDBChunkInv response from the target database table, if we have it.
    /// Generates a Nack if we don't have this DB, or if the request's consensus hash is invalid.
    fn make_stacker_db_getchunkinv_response(
        network: &PeerNetwork,
        naddr: NeighborAddress,
        chainstate: &mut StacksChainState,
        getchunkinv: &StackerDBGetChunkInvData,
    ) -> StacksMessageType {
        network.make_StackerDBChunksInv_or_Nack(
            naddr,
            chainstate,
            &getchunkinv.contract_id,
            &getchunkinv.rc_consensus_hash,
        )
    }
```

**File:** changelog.d/stackerdb-uploaded-chunk-event-loss.fixed (L1-1)
```text
Fixed a StackerDB chunk uploaded over HTTP being stored and acknowledged but never announced to the node's event observers, which could stall consensus indefinitely when the lost chunk was a signer's block pre-commit. The chunk's pending event notification was discarded whenever the relayer fell behind and a peer echoed the same chunk back, or whenever the node's own view advanced before the relayer processed the upload.
```
