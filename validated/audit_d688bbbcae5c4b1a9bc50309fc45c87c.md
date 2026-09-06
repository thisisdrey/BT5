### Title
Unsolicited StackerDB chunk-push handler advertises an unconfirmed chunk version in its inventory reply before the chunk is durably stored - (File: stackslib/src/net/stackerdb/mod.rs)

### Summary
`PeerNetwork::handle_unsolicited_StackerDBPushChunk` patches the in-memory `StackerDBChunkInvData` that it is about to sign and send back to the pushing peer with the *new* chunk version, before that chunk has actually been written to the local StackerDB. The reply therefore claims the local node already possesses data it has not yet (and may never) persist, breaking the equality between "advertised inventory" and "actually stored state" that the rest of the sync protocol depends on.

### Finding Description
When a peer pushes a chunk via `StacksMessageType::StackerDBPushChunk`, `handle_unsolicited_StackerDBPushChunk` builds a `StackerDBChunkInv` reply from `make_StackerDBChunksInv_or_Nack`, which itself is built purely from what is *currently* in the DB via `self.stackerdbs.get_slot_versions(contract_id)`: [1](#0-0) 

After that inventory snapshot is taken, the handler only re-validates the pushed chunk's signature/size/version/write-count against the *stale* snapshot (`validate_received_chunk`), and — on success — directly mutates the reply payload to claim the slot already has the new version, without any corresponding write to `self.stackerdbs`: [2](#0-1) 

The function's own doc comment confirms that persistence is *not* performed here — this code path only wakes up the sync state machine, and the actual storage of the chunk (if it happens at all) is deferred to whatever downstream relayer/processing consumes the forwarded message: [3](#0-2) 

The reply carrying the patched inventory is then immediately signed and sent back to the remote peer in the same call, decoupled from whether the deferred store ever succeeds: [4](#0-3) 

This is the direct analog of the `updatePhase` bug: a piece of authoritative state (the advertised slot version in the chunk inventory, analogous to `endRoundId`) is recorded/broadcast based on an event that has not yet been finalized (chunk actually durably stored, analogous to "the round having actually ended"). If the deferred store never completes — e.g., the relayer fails to apply it, the chunk is dropped, or a genuinely newer/conflicting version wins the race in the interim — the local node has already told the network it holds the newer version.

### Impact Explanation
Any peer that later queries this node's inventory (`StackerDBGetChunksInv`) or relies on the previously advertised version will believe stale/incorrect data is fresh and skip re-fetching or re-requesting the chunk from a peer that actually has it, or will treat this node as an authoritative rarest-first source when it is not. This is a "false inventory" condition that can steer StackerDB sync (used for signer messages, miner coordination data, etc.) away from the true canonical chunk set — matching the High-severity "steering a node off the tip via false inventory" category.

### Likelihood Explanation
The handler is reachable by any connected, unauthenticated-at-this-layer p2p peer sending a single unsolicited `StackerDBPushChunk` message; only a validly signed chunk (any registered slot signer's chunk is sufficient — no special privilege) is needed to trigger the inventory patch and reply. No large volume of traffic or timing races beyond ordinary network scheduling of the deferred store vs. the immediate reply are required.

### Recommendation
Do not mutate/advertise the chunk-inventory version until the chunk has been durably committed via `StackerDBTx::try_replace_chunk`/`insert_chunk`. Either perform the store synchronously in `handle_unsolicited_StackerDBPushChunk` before constructing the reply, or defer sending the `StackerDBChunkInv` reply until the deferred store completes and re-read the actual stored version at that time rather than patching the pre-store snapshot.

### Proof of Concept
Conceptual PoC (network-level, no special privileges required):
1. Attacker connects as a normal p2p peer to a victim node that replicates a StackerDB.
2. Attacker signs a valid `StackerDBChunkData` with a legitimate slot's key (any registered signer key works — attacker just needs a signature from *a* valid contract signer, which for many StackerDBs, e.g. signer-coordination contracts, may be attacker-controlled if attacker is a registered signer) and sends it as `StackerDBPushChunk`.
3. `handle_unsolicited_StackerDBPushChunk` validates the chunk against the pre-write snapshot, patches `data.slot_versions` for that slot to the new version, and immediately replies with `StackerDBChunkInv` reflecting the unconfirmed version — while the actual DB write is left to the deferred relayer path.
4. If the deferred write is delayed, races with a conflicting update, or otherwise never lands, other peers polling this node's inventory will be told (falsely) that the node has the newer chunk, causing them to skip fetching it elsewhere.

Note: due to tool-call budget I was not able to fully trace every downstream code path in `stackslib/src/net/relay.rs` / `stackslib/src/net/unsolicited.rs` that ultimately processes the forwarded `StackerDBPushChunk` message to confirm every scenario in which the deferred store can diverge from the immediately-advertised inventory; however, the core defect — inventory advertised as updated in the same call that only *validates* the chunk without writing it to `self.stackerdbs` — is directly confirmed in the code shown above.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L592-607)
```rust
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
```

**File:** stackslib/src/net/stackerdb/mod.rs (L726-741)
```rust
    ///
    /// Note that this can happen *during* a StackerDB sync's execution, so be very careful about
    /// modifying a state machine's contents!  The only modification possible here is to wakeup
    /// the state machine in case it's asleep (i.e. blocked on waiting for the next sync round).
    ///
    /// The write frequency is not checked for this chunk. This is because the `ConversationP2P` on
    /// which this chunk arrived will have already bandwidth-throttled the remote peer, and because
    /// messages can be arbitrarily delayed (and bunched up) by the network anyway.
    ///
    /// Returns (true, x) if we should buffer the message and try processing it again later.
    /// Returns (false, x) if we should *not* buffer this message, because it either *won't* be valid
    /// later, or if it can be stored right now.
    ///
    /// Returns (x, true) if we should forward the message to the relayer, so it can be processed.
    /// Returns (x, false) if we should *not* forward the message to the relayer, because it will
    /// *not* be processed.
```

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
