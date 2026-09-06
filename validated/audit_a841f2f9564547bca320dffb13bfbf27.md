### Title
Unsolicited StackerDB push-chunk handler advertises a slot version as locally held before the chunk is actually committed to storage - ([File: stackslib/src/net/stackerdb/mod.rs])

### Summary
`PeerNetwork::handle_unsolicited_StackerDBPushChunk` validates an unsolicited `StackerDBPushChunkData` and, if valid, immediately patches the outgoing `StackerDBChunkInv` reply to claim the new `slot_version` is now held — but the actual write to the StackerDB replica (`try_replace_chunk`) only happens later, asynchronously, when the message is forwarded to the relayer.

### Finding Description
In `handle_unsolicited_StackerDBPushChunk` [1](#0-0) , the function calls `validate_received_chunk` (signature, size, freshness, max-writes checks) and, on success, patches the in-memory `data.slot_versions` inventory vector to the new version and prepares to send this `StackerDBChunkInv` back to the peer: [2](#0-1) 

This reply is signed and transmitted synchronously (when `send_reply` is true) at the end of the function: [3](#0-2) 

However, at this point the chunk has **not** been written to the local StackerDB replica. The function only returns `(false, true)`, signalling the caller (`handle_unsolicited_stacks_message` in `unsolicited.rs`) to forward the message to the relayer for actual processing/storage — which happens later in `Relayer::process_stacker_db_chunks` via `tx.try_replace_chunk` [4](#0-3) , or in `Relayer::process_new_data` depending on the pipeline stage. `try_replace_chunk` can still fail for reasons `validate_received_chunk` does not check identically — e.g. a race where two chunks for the same slot arrive from different peers between the validation-time read and the commit-time transaction, causing `StaleChunk` (since the DB read in `try_replace_chunk` re-checks `slot_validation.version` at commit time, not at push-time) [5](#0-4) .

This breaks the equality between "advertised inventory" and "actually stored chunk": a remote unauthenticated peer can trigger this node to broadcast/reply that it possesses a chunk version it does not actually have (or may lose to a race before commit). Because `StackerDBChunkInv` is exactly the artifact other nodes use to decide what to download next (rarest-first / newest-first scheduling per the module doc [6](#0-5) ), this false inventory entry can cause neighbors to skip re-requesting that slot/version from other peers (believing it's already replicated here), while this node in fact drops it as stale/failed and never stores it — a "false inventory" served as canonical.

### Impact Explanation
This falls under "High - steering a node off the tip via false inventory": a remote peer, without any special privilege, can cause a node to report inventory that doesn't reflect committed state for a StackerDB slot (e.g., signer messages, miner coordination), potentially causing gaps in chunk propagation across the network for time-sensitive data such as Nakamoto miner/signer StackerDB contracts.

### Likelihood Explanation
Likelihood is moderate: it requires a race between concurrent unsolicited pushes/validations for the same slot from different senders, or a validate-then-commit gap being exploited by an attacker sending a burst of conflicting slot updates for a slot it can produce a valid signature for indirectly is not required — since `validate_received_chunk` already enforces the signer check, but the race is purely about *timing* between validating against the current expected_versions view and the actual commit through the relayer pipeline, which any remote peer holding a legitimately-signed higher-version chunk (or a legitimate signer key) can trigger by pipelining conflicting version pushes. This does not require the node's own key or an admin role, only network-level timing.

### Recommendation
Do not patch/broadcast the advertised inventory version until `try_replace_chunk` has actually succeeded (i.e., move the inventory patch and reply construction to occur after the relayer has confirmed storage), or make `validate_received_chunk`'s freshness check and the actual commit atomic under the same lock/transaction so the two cannot diverge.

### Proof of Concept
1. Two neighbors N1 and N2 both hold valid signing keys/versions? — not required. A single attacker-controlled peer sends `StackerDBPushChunkData` for slot X, version V (validly signed), to node A. Concurrently, before A's relayer commits it, a second `StackerDBPushChunkData` for slot X, version V' > V (also validly signed, e.g. by the legitimate application signer replaying a stale message) arrives via a different conversation.
2. Both pass `validate_received_chunk` at push time (since `expected_versions` used for validation is read at push-time only, not under the write-lock used in `try_replace_chunk`), so both handler invocations patch their respective `StackerDBChunkInv` replies to advertise their own claimed version as stored.
3. Only one of the two writes actually succeeds in `try_replace_chunk` (the other gets `StaleChunk` and is silently dropped) [7](#0-6) .
4. The peer whose push lost the race has already received an inventory reply from A claiming the higher version is stored, when in fact it was dropped — creating a mismatch between advertised inventory and actual stored state that can misdirect subsequent chunk-sync scheduling for that slot.

### Citations

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

**File:** stackslib/src/net/stackerdb/mod.rs (L742-792)
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
```

**File:** stackslib/src/net/stackerdb/mod.rs (L794-815)
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

**File:** stackslib/src/net/stackerdb/db.rs (L398-437)
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
```
