### Title
StackerDB inventory acknowledgment is sent before the chunk write is actually committed, allowing an advertised inventory to diverge from real stored state - (File: `stackslib/src/net/stackerdb/mod.rs`)

### Summary
`PeerNetwork::handle_unsolicited_StackerDBPushChunk` validates an incoming `StackerDBPushChunkData`, optimistically patches an in-memory `StackerDBChunkInv` to say the new slot version is now accepted, and immediately signs and sends that inventory back to the sender — all *before* the chunk is actually persisted. The real persistence (`StackerDBTx::try_replace_chunk`) happens later and independently, in the relay layer. Because the two steps are decoupled, a remote, unprivileged peer can trigger a state where our node acknowledges a chunk as "accepted" into its StackerDB inventory that is never actually committed, so our advertised inventory diverges from our real on-disk StackerDB state.

### Finding Description
The equality being broken is: *advertised inventory (what we tell a peer we have) == actually committed StackerDB slot state (what we would serve if asked)*.

In `handle_unsolicited_StackerDBPushChunk`: [1](#0-0) 
the code only calls `validate_received_chunk` (signature/version/size checks against a snapshot of `slot_versions`) and then patches the in-memory `data.slot_versions` entry to the new version, before any DB write occurs: [2](#0-1) 

The patched inventory is then signed and sent straight back to the peer as a real reply: [3](#0-2) 

The actual write only happens later, decoupled from this reply, via `Relayer::process_stacker_db_chunks`, which calls `tx.try_replace_chunk` for each chunk that was forwarded to it: [4](#0-3) 

`try_replace_chunk` re-validates against the *actual* current DB state (freshest signer/version check) and will reject the write with `StaleChunk` if a different, higher-versioned write for the same slot won the race in the meantime: [5](#0-4) 

If that happens, the failure is only logged and silently dropped — no correction is sent to the peer that already received the "accepted" inventory patch: [6](#0-5) 

Because `handle_unsolicited_StackerDBPushChunk` is invoked once per unsolicited message during `dispatch_network`'s synchronous message-processing pass, and the actual persistence in `process_stacker_db_chunks` happens afterward as a batch, two racing pushes to the same slot (e.g. from two different connections, which any peer holding a valid StackerDB signer key can open) can each independently pass `validate_received_chunk` against the same stale snapshot, each get an "accepted" inventory reply sent back immediately, but only one of the two underlying writes can actually succeed in `try_replace_chunk`. The loser's sender is told (via a signed `StackerDBChunkInv`) that its chunk is now part of our inventory, when in fact it was silently dropped.

### Impact Explanation
This is a "served vs. committed" mismatch: an unprivileged, unauthenticated-with-respect-to-write-outcome party (any peer legitimately possessing a StackerDB slot signer key, which is a normal, non-privileged network participant) can cause our node to serve inventory data (`StackerDBChunkInv`) that is not backed by its actual stored data. Downstream consequences:
- Peers relying on our advertised inventory to decide whether to (re)push or re-fetch a chunk may conclude the network already has the latest version via us and stop retrying, causing that chunk update to silently vanish from replication with no error surfaced to any party.
- Our own inventory (used for further sync scheduling and gossip decisions elsewhere in `stackerdb/sync.rs`/`mod.rs`) becomes internally inconsistent with the DB, which can steer StackerDB sync state machines and neighbors based on a false view of what data is actually held.

This falls under "serving non-canonical state as canonical" (High impact category), since a P2P peer receives a cryptographically signed acknowledgment about data that the node does not actually hold.

### Likelihood Explanation
Triggering it only requires being a legitimate signer for at least one StackerDB slot (which is intentionally a low/no-permission role in this design — any registered app/participant), and racing two pushes for the same slot from two separate connections/event IDs in the same processing tick, which is straightforward to arrange from a network client. No node secret or elevated privilege is required.

### Recommendation
Do not send the patched `StackerDBChunkInv` acknowledgment until after the corresponding chunk has actually been persisted via `try_replace_chunk` (or otherwise defer/re-verify the ack against the just-committed DB state), so that any advertised inventory value is only ever sent after the underlying write is confirmed to have succeeded.

### Proof of Concept
1. A peer holding a valid signer key for slot `S` in stackerdb contract `C` opens two connections/event IDs to the target node.
2. It sends `StackerDBPushChunkData` for slot `S`, version `V1` on connection A, and version `V2 > V1` on connection B, in quick succession, before the node's relay layer has had a chance to persist either.
3. `handle_unsolicited_StackerDBPushChunk` is invoked for each message; both pass `validate_received_chunk` against the same (stale) `slot_versions` snapshot obtained via `make_StackerDBChunksInv_or_Nack`, and each connection independently receives a signed `StackerDBChunkInv` reply patched to show its own submitted version as accepted (`stackslib/src/net/stackerdb/mod.rs:794-815, 858-870`).
4. Both chunks are forwarded to the relayer; `Relayer::process_stacker_db_chunks` calls `try_replace_chunk` for each — only the write for `V2` succeeds; the write for `V1` fails with `StaleChunk` and is silently dropped (`stackslib/src/net/relay.rs:2406-2437`).
5. The peer on connection A already received a signed inventory claiming version `V1` was accepted, even though the node's actual StackerDB state never stored `V1` for slot `S`.

### Citations

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
