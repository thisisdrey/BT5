## Confirmed: `slot_versions` inventory is patched to reflect an "accepted" chunk before it is actually committed to storage

### Title
StackerDB push-chunk handler advertises an accepted slot version to the peer before the chunk is actually persisted, breaking the served-vs-committed invariant — ([File: stackslib/src/net/stackerdb/mod.rs])

### Summary
`PeerNetwork::handle_unsolicited_StackerDBPushChunk` builds a `StackerDBChunkInv` reply from the node's *actual, freshly-queried* on-disk slot versions (`make_StackerDBChunksInv_or_Nack` → `self.stackerdbs.get_slot_versions(...)`), then, after only *validating* (not storing) the incoming chunk via `validate_received_chunk`, unconditionally patches that in-memory copy of the inventory to claim the new chunk version is already present (`*slot_version = chunk_data.chunk_data.slot_version;`), and sends this patched inventory straight back to the sender as the ack — all before the chunk is ever written to the StackerDB (`try_replace_chunk` happens later, in the relayer's `process_stacker_db_chunks`, or not at all for the `send_reply=true`/immediate-ack branch). [1](#0-0) [2](#0-1) 

### Finding Description
Compare the two sides of the equality "what we tell the peer we have" vs "what we actually persisted":

- **Served side**: `data.slot_versions` starts life as the *true* DB state (`get_slot_versions`), [3](#0-2) , but is then unconditionally overwritten in-memory with the pushed chunk's claimed version as soon as `validate_received_chunk` passes: [4](#0-3) . This mutated `data` is what gets sent back to the peer as the `StackerDBChunkInv` ack, `send_p2p_message(event_id, resp, ...)` [5](#0-4) .
- **Committed side**: the function's own doc-comment states the write frequency (and, implicitly, actual persistence) is *not* checked here, and the function returns `(false, true)` — i.e., "don't buffer, forward to the relayer" — meaning the *actual* `try_replace_chunk` call happens later, asynchronously, in a different code path (`Relayer::process_stacker_db_chunks` in `relay.rs`, which calls `tx.try_replace_chunk` and only broadcasts/emits events on success) [6](#0-5) .

Because `validate_received_chunk` only re-implements version/size/signature checks against a snapshot (`expected_versions`) taken at call time — it does not perform the actual atomic compare-and-swap that `StackerDBTx::try_replace_chunk` performs under a DB transaction (which also enforces `TooManySlotWrites` and re-checks `slot_version <= slot_validation.version` under lock) [7](#0-6)  — there is a genuine TOCTOU/consistency gap: the node can ACK "I now have slot version V" to the pushing peer while the relayer's subsequent, decoupled `try_replace_chunk` call fails (e.g., `StaleChunk` due to a race with a concurrent write to the same slot, or any other transactional rejection), silently `continue`s past the error, and never actually stores the data [8](#0-7) .

This is the network-protocol analog of the Bribe.sol flaw: in Bribe.sol, `deposit()` unconditionally bumps a bookkeeping counter (`totalVoting`) that is *supposed* to mirror committed voting weight, but the paired `withdraw()` path does not correspondingly reconcile it, so the externally-observable accounting (`totalVoting`, read by `earned()`) drifts from the true committed state (`balanceOf`/`totalSupply`). Here, the externally-observable inventory advertisement (`StackerDBChunkInv.slot_versions`, read/trusted by the remote peer to decide whether to keep re-pushing/re-gossiping that chunk) is speculatively bumped without a guarantee that the underlying committed store (`chunks` table) actually reflects that version.

### Impact Explanation
The practical effect: a remote, unprivileged peer that legitimately (or maliciously, by racing two pushes for the same slot from different connections) triggers this ordering gap causes the receiving node to falsely advertise it holds a given `(slot_id, slot_version)` chunk it does not actually have stored. Any third party that later queries this node's real inventory will see the correct (unpatched, freshly-queried) version and re-request, so the primary victim is the *original pusher*, which — believing its chunk was accepted — will stop retransmitting it (per the "already relayed" bookkeeping in `broadcast_message`/relay-hint logic) [9](#0-8) . In the worst case this can cause a chunk (e.g. a Nakamoto miner/signer StackerDB message) to be dropped from the propagation graph on this path with no compensating re-fetch, since the sender has no signal that the "acceptance" was hollow. This is a data-availability/propagation-correctness defect rather than a memory-safety or auth-bypass bug, and it requires a specific race between the immediate-ack path and the asynchronous relayer commit path to actually manifest a stored-vs-served mismatch. I could not fully verify, given the exploration budget, whether `try_replace_chunk` can realistically fail after `validate_received_chunk` already succeeded in the same handler invocation for the *non-buffered* immediate-ack branch (the two checks are largely duplicated), so the practical window may be narrow (concurrent racing pushes to the same slot on different connections, or a slightly stale `expected_versions` snapshot).

### Likelihood Explanation
Low-to-moderate. It requires either (a) genuine concurrency — two peers or two connections pushing conflicting versions to the same slot at nearly the same time — or (b) some other divergence between the `validate_received_chunk` check and the `try_replace_chunk` transactional check (e.g., `max_writes`/write-frequency enforcement differences, since write-frequency is explicitly *not* checked in the push-handler by design). It does not require attacker-held secrets, is remotely triggerable by any StackerDB-writing peer, but the resulting damage (one dropped chunk relay) is much less severe than the funds-freezing impact in the original report.

### Recommendation
Do not mutate/patch the advertised `slot_versions` in `handle_unsolicited_StackerDBPushChunk` until the chunk has actually been durably stored (i.e., move the ack construction to *after* a successful `try_replace_chunk`, or have the relayer's post-commit path be the sole source of the ack/ChunkInv sent for this event). Alternatively, only send the "accepted" ack after the deferred commit succeeds, and send a NACK/negative ack if the deferred `try_replace_chunk` fails.

### Proof of Concept
Conceptual (not executed): a peer node could race two `StackerDBPushChunkData` messages for the same `(contract_id, slot_id)` on two different connections/event_ids from two signers who might both hold write authority in edge configurations, or exploit a delay between `validate_received_chunk`'s use of a `data.slot_versions` snapshot and the actual DB write in `relay.rs::process_stacker_db_chunks`, such that: connection A's push is ACKed with the *patched* higher version, but by the time the relayer processes and calls `try_replace_chunk`, another already-committed higher version (from connection B) makes A's write a `StaleChunk` — silently dropped in `relay.rs` — even though A was already told its write succeeded via the earlier `StackerDBChunkInv` ack.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L594-607)
```rust
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
```

**File:** stackslib/src/net/stackerdb/mod.rs (L783-814)
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
```

**File:** stackslib/src/net/stackerdb/mod.rs (L858-871)
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
    }
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

**File:** stackslib/src/net/p2p.rs (L1251-1293)
```rust
    /// Broadcast a message to a list of neighbors.
    /// Neighbors in the `relay_hints` vec will *not* receive data, since they were the one(s) that
    /// sent this peer the message in the first place.
    pub fn broadcast_message(
        &mut self,
        neighbor_keys: Vec<NeighborKey>,
        relay_hints: Vec<RelayData>,
        message_payload: StacksMessageType,
    ) {
        debug!(
            "{:?}: Will broadcast '{}' to up to {} neighbors; relayed by {:?}",
            &self.local_peer,
            message_payload.get_message_description(),
            neighbor_keys.len(),
            &relay_hints
        );
        for nk in neighbor_keys.into_iter() {
            if let Some(event_id) = self.events.get(&nk) {
                let event_id = *event_id;
                if let Some(convo) = self.peers.get_mut(&event_id) {
                    if !convo.is_authenticated() {
                        continue;
                    }
                    // safety check -- don't send to someone who has already been a relayer
                    let mut do_relay = true;
                    if let Some(pubkey) = convo.ref_public_key() {
                        let pubkey_hash = Hash160::from_node_public_key(pubkey);
                        for rhint in relay_hints.iter() {
                            if rhint.peer.public_key_hash == pubkey_hash {
                                do_relay = false;
                                break;
                            }
                        }
                    }
                    if !do_relay {
                        debug!(
                            "{:?}: Do not broadcast '{}' to {:?}: it has already relayed it",
                            &self.local_peer,
                            message_payload.get_message_description(),
                            &nk
                        );
                        continue;
                    }
```
