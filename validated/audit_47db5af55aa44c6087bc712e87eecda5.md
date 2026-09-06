Confirmed: `RPCPostStackerDBChunkRequestHandler::try_handle_request` in `poststackerdbchunk.rs` calls `tx.try_replace_chunk` directly and, on acceptance, immediately relays the chunk to the P2P network via `node.set_relay_message(StacksMessageType::StackerDBPushChunk(...))` [1](#0-0) . `try_replace_chunk` (`stackslib/src/net/stackerdb/db.rs:400-438`) only checks chunk size, signer validity, version staleness, and `max_writes` — it never checks `write_freq` (minimum wall-clock time between writes) [2](#0-1) . The `write_freq` gate is documented as "minimum wall-clock time between writes to the same slot" in `StackerDBConfig` [3](#0-2) , but it is enforced only inside the sync-pull scheduler (`sync.rs::getchunks_inv`, deciding what to *fetch*) [4](#0-3) , and `validate_received_chunk`/`handle_unsolicited_StackerDBPushChunk` explicitly document that write-frequency is *not* checked there, relying instead on other paths' throttling [5](#0-4) [6](#0-5) .

### Title
StackerDB write-frequency limit is not enforced on the HTTP chunk-write path, allowing unrestricted network-wide amplification of writes - (File: stackslib/src/net/api/poststackerdbchunk.rs, stackslib/src/net/stackerdb/db.rs)

### Summary
`StackerDBConfig.write_freq` is designed as the rate-limit that bounds how often a valid slot signer may write to a given slot, similar to how `NotOnOrAfter` is meant to bound the validity of a SAML LogoutRequest. Just as `validatePostRequestAsync` never checked the current timestamp against `NotOnOrAfter`, the actual chunk-acceptance function used by the `POST /v2/stackerdb/.../chunks` RPC endpoint (`StackerDBTx::try_replace_chunk`) never checks `write_freq`. The check exists only in the pull-based sync scheduler, which decides whether to *request* a chunk from peers — it is not enforced at the point where a chunk is actually written and accepted.

### Finding Description
`try_replace_chunk` validates chunk size, signer/signature, version monotonicity (`StaleChunk`), and `max_writes`, but omits any comparison against `write_time`/`write_freq` [2](#0-1) . The only consumer of `get_slot_write_timestamps`/`write_freq` is the sync state machine's chunk-fetch prioritization, which skips *requesting* chunks written too recently [7](#0-6) . `validate_received_chunk` (shared by both the pull-sync `validate_downloaded_chunk` and the P2P-push handler `handle_unsolicited_StackerDBPushChunk`) explicitly notes it "does not check write frequency, since the caller has different ways of doing this" [8](#0-7) , and the push handler's own comment states write frequency is skipped because "the ConversationP2P... will have already bandwidth-throttled the remote peer" [6](#0-5) . However, the RPC-write path (`poststackerdbchunk.rs`) is not a `ConversationP2P` message and has no equivalent bandwidth throttle for repeated writes — it calls `try_replace_chunk` directly with only per-connection HTTP limits, and on success unconditionally re-broadcasts the accepted chunk as a `StackerDBPushChunk` P2P message to the network [1](#0-0) .

### Impact Explanation
A remote actor who is a legitimate signer for even one StackerDB slot (this requires only their own private key — no admin or node secret) can post new chunk versions to a node's RPC endpoint at an unbounded rate (bounded only by `max_writes` and HTTP connection limits, not by `write_freq`). Each accepted write is relayed via `set_relay_message`, which triggers `process_stacker_db_chunks`/`process_pushed_stacker_db_chunks` and a P2P broadcast to all subscribing peers [9](#0-8) . This produces network-wide propagation of writes at a rate the protocol's own rate-limit (`write_freq`) was designed to prevent, defeating the anti-spam/anti-flood control across every replicating node in the network — a bounded-compute/write-amplification DoS on StackerDB replicas that is reachable by any authorized-but-unprivileged signer, not just the attacker's own node.

### Likelihood Explanation
Any address that already holds a StackerDB slot (e.g., a registered signer) can exploit this with no additional privilege — they just call the existing `POST /v2/stackerdb/{addr}/{contract}/chunks` endpoint repeatedly with incrementing slot versions signed by their own key, up to `max_writes`. No race condition or timing precision is required.

### Recommendation
Enforce `write_freq` inside `StackerDBTx::try_replace_chunk` (or equivalently in `RPCPostStackerDBChunkRequestHandler::try_handle_request` before calling it) by comparing the slot's stored `write_time` against `get_epoch_time_secs()`, rejecting writes that arrive before `write_time + write_freq`, consistent with how the sync scheduler already treats this as a hard constraint.

### Proof of Concept
1. Register a signer key/slot for a StackerDB contract with `write_freq > 0`.
2. Repeatedly call `POST /v2/stackerdb/{signer-addr}/{contract}/chunks` with correctly-signed chunks of strictly increasing `slot_version`, sent faster than `write_freq` seconds apart.
3. Observe that `try_replace_chunk` accepts every write (bounded only by `max_writes`), and each accepted write is broadcast via `StackerDBPushChunk` to the whole network in `process_stacker_db_chunks`, even though the design intent (enforced elsewhere via the sync scheduler) is to rate-limit such writes to no more than once per `write_freq` seconds.

### Citations

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L315-324)
```rust
        if ack_resp.accepted {
            let push_chunk_data = StackerDBPushChunkData {
                contract_id: contract_identifier,
                rc_consensus_hash: node.with_node_state(|network, _, _, _, _| {
                    network.get_chain_view().rc_consensus_hash.clone()
                }),
                chunk_data: stackerdb_chunk,
            };
            node.set_relay_message(StacksMessageType::StackerDBPushChunk(push_chunk_data));
        }
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

**File:** stackslib/src/net/stackerdb/mod.rs (L238-240)
```rust
    pub signers: Vec<(StacksAddress, u32)>,
    /// minimum wall-clock time between writes to the same slot.
    pub write_freq: u64,
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

**File:** stackslib/src/net/stackerdb/mod.rs (L731-733)
```rust
    /// The write frequency is not checked for this chunk. This is because the `ConversationP2P` on
    /// which this chunk arrived will have already bandwidth-throttled the remote peer, and because
    /// messages can be arbitrarily delayed (and bunched up) by the network anyway.
```

**File:** stackslib/src/net/stackerdb/sync.rs (L335-352)
```rust
        for ((i, local_version), write_ts) in local_slot_versions
            .iter()
            .enumerate()
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

**File:** stackslib/src/net/relay.rs (L2445-2452)
```rust
                        let msg = StacksMessageType::StackerDBPushChunk(StackerDBPushChunkData {
                            contract_id: sc.clone(),
                            rc_consensus_hash: rc_consensus_hash.clone(),
                            chunk_data: chunk,
                        });
                        if let Err(e) = self.p2p.broadcast_message(vec![], msg) {
                            warn!("Failed to broadcast StackerDB chunk: {e:?}");
                        }
```
