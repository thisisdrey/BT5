### Title
Race between `StackerDBChunkInv` advertisement and actual chunk commit lets a node advertise data it never durably stored - (File: `stackslib/src/net/stackerdb/mod.rs`)

### Summary
`PeerNetwork::handle_unsolicited_StackerDBPushChunk` validates an incoming pushed chunk (signature, size, version bounds) and immediately patches and replies with a `StackerDBChunkInv` that advertises the *new* slot version as accepted, before the chunk is ever durably written to the StackerDB replica. The actual, authoritative write (`try_replace_chunk`, which re-checks monotonic version ordering against the live DB state) happens later and asynchronously, once the message is handed off to the relayer. If that later write fails (e.g. a concurrent push for the same slot wins the race), the chunk is silently dropped, but the peer that sent it — and any neighbor that queried this node's inventory in the interim — has already been told the newer version is present. This is the "served vs. committed" equality violation: the advertised (served) inventory state does not match what is actually committed to storage.

### Finding Description
In `stackslib/src/net/stackerdb/mod.rs`, `handle_unsolicited_StackerDBPushChunk` builds a reply inventory via `make_StackerDBChunksInv_or_Nack`, then calls `validate_received_chunk` (checks size, expected signer/signature, and version bounds against an in-memory snapshot of `slot_versions`), and on success directly mutates the reply's `slot_versions` entry to the pushed chunk's version: [1](#0-0) 

This patched `StackerDBChunkInv` is what gets signed and sent back to the peer as the RPC/P2P reply (`send_p2p_message`) when `send_reply` is true: [2](#0-1) 

Crucially, at this point *nothing has been written to the StackerDB replica*. The comment on the function itself states the storage happens elsewhere: "Returns (x, true) if we should forward the message to the relayer, so it can be processed" — i.e. actual persistence is deferred to `Relayer::process_pushed_stacker_db_chunks` → `process_stacker_db_chunks`, which is where `try_replace_chunk` is actually invoked against the live database: [3](#0-2) 

`try_replace_chunk` re-validates the slot version strictly against the current stored `SlotValidation.version` at commit time: [4](#0-3) 

Because the version comparison used to build the advertised inventory (`validate_received_chunk` against a stale, previously-fetched `slot_versions` snapshot) is decoupled from the version comparison that actually gates the write (`try_replace_chunk`'s live DB read), two chunks for the same slot arriving close together (from the same or different peers) can both pass the advertisement-time check and each cause a `StackerDBChunkInv` to be sent out claiming the new version is accepted, while only one can actually win the store. The loser is dropped with only a debug log: [5](#0-4) 

No feedback is sent to the peer whose chunk lost the race and was silently dropped — that peer, and anyone who queried the node's `StackerDBChunkInv` in between, believes the node holds a version of the slot data that the node never actually committed.

### Impact Explanation
Neighbors use `StackerDBChunkInv`/inventory responses to decide what to fetch and to determine sync completeness for a StackerDB replica (e.g. signer message relay, `.miners` DB). A node advertising a slot version it does not actually possess causes downstream syncing peers to treat this replica as caught-up for that slot when it is not, i.e. non-canonical/absent state is momentarily served as canonical via the inventory response. This matches the explicitly in-scope High-impact category of "steering a node off the tip via false inventory" — peers may skip re-requesting genuinely missing/updated chunk data because the false inventory said it was already current.

### Likelihood Explanation
This requires no privileged access — any two unprivileged peers (or the same peer sending two closely-timed pushes, or one push racing with a concurrent local/HTTP write) can trigger the race, since `handle_unsolicited_StackerDBPushChunk` performs its acceptance/advertisement decision using an in-memory snapshot that is not atomic with the actual DB write that happens later on the relay thread. Under normal gossip/relay conditions with multiple StackerDB replicators pushing similar-timed updates, this window is realistically reachable.

### Recommendation
- **Short term:** Do not patch and advertise the `StackerDBChunkInv` as accepted until the chunk has actually been durably written via `try_replace_chunk` (or equivalent). Move the inventory-patching logic so it only occurs after a successful, synchronous store, and only advertise the version that was truly persisted.
- **Long term:** Make the "is this chunk acceptable" decision path and the actual commit path share the same live, atomic version check (e.g. perform validation and write within the same transaction/lock), so that the acknowledgement sent to peers can never diverge from what is durably stored. Add integration tests that push two competing chunk versions for the same slot in quick succession and assert that only the peer whose chunk was actually stored receives an "accepted" acknowledgment/inventory update.

### Proof of Concept
1. Node `N` runs a StackerDB replica containing slot `S` currently at version `v`.
2. Peer `A` sends `StackerDBPushChunk` for slot `S` at version `v+1`, correctly signed.
3. Concurrently (before `A`'s chunk is processed by the relayer/committed), peer `B` sends `StackerDBPushChunk` for the same slot `S`, also at version `v+1` (or `v+2`), correctly signed.
4. `handle_unsolicited_StackerDBPushChunk` is invoked for both: each call independently calls `make_StackerDBChunksInv_or_Nack` (snapshotting `get_slot_versions` at `v`) and `validate_received_chunk`, both succeed against the stale `v` snapshot, and both handlers patch their respective reply `StackerDBChunkInv` to advertise the new version, replying to `A` and `B` respectively that their chunk is accepted.
5. Both pushes are queued for the relayer; `process_stacker_db_chunks` processes them sequentially and calls `try_replace_chunk` for each. The first to commit succeeds; the second fails with `net_error::StaleChunk` and is dropped with only a debug log — no correction is sent to that peer or to anyone else who may have observed the advertised (but never durable) inventory update.
6. Peer whose chunk was dropped (or a third-party peer that queried `N`'s inventory in the interim) now has an inconsistent view: it believes `N` stores a version of slot `S` that `N` never actually committed.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L784-814)
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
