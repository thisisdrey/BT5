### Title
Advertised StackerDB chunk inventory can diverge from actually-persisted chunk data - ([File: stackslib/src/net/stackerdb/mod.rs])

### Summary
`PeerNetwork::handle_unsolicited_StackerDBPushChunk` builds a `StackerDBChunkInv` reply and **patches** the in-memory `slot_versions` entry to reflect the pushed chunk's `slot_version` as soon as signature/version/size validation passes, and sends that patched inventory back to the peer as an acknowledgement — *before* the chunk is actually durably written into the StackerDB SQLite store. The real write happens later, out-of-band, when the chunk is forwarded to the relayer (`Ok((false, true))` return path) and processed via `StackerDBTx::try_replace_chunk`. This breaks the equality "advertised/acknowledged slot version == version actually committed to the database."

### Finding Description
`validate_received_chunk` [1](#0-0)  only checks chunk size, signer signature, staleness against the caller-supplied `expected_versions` snapshot, and max-write count — it never touches the database, so it cannot guarantee the chunk will actually be persisted afterward.

In `handle_unsolicited_StackerDBPushChunk`, once `validate_received_chunk` returns `true`, the code immediately mutates the in-flight `StackerDBChunkInv` payload's `slot_versions` entry to the new (unwritten) version and only afterward forwards the chunk to the relayer for actual storage: [2](#0-1) 

If `send_reply` is true, this patched inventory is signed and sent straight back to the peer as the ack, synchronously, while the actual DB write (`try_replace_chunk`, which enforces its own independent checks — signer, staleness against the DB's authoritative `slot_validation.version`, and max-writes) happens later and asynchronously via the relay path: [3](#0-2) [4](#0-3) 

Because `validate_received_chunk`'s staleness check is performed against a snapshot (`expected_versions`) passed in by the caller rather than atomically against the database at write time, two chunks for the same slot arriving from different peers in the same round can both pass this pre-check (each sees the same "old" expected version), both get acknowledged with the new version patched into their respective `StackerDBChunkInv` replies, yet only the first one that reaches `try_replace_chunk` will actually be persisted — the second will fail with `net_error::StaleChunk` and be silently dropped, exactly analogous to how the Solidity report's `claimable[_gauge]` bookkeeping is mutated independently of (and inconsistently with) the real, later-checked state (`totalWeight`)/gauge liveness. The peer whose chunk lost the race is told its data was accepted (via the patched inventory ack) when, in fact, nothing was written.

### Impact Explanation
This is a self-reported inventory desynchronization: a remote, unprivileged peer can end up believing (from a valid, signed ack) that a given node holds the newest version of a StackerDB slot when the node's actual persisted store does not. Since StackerDB sync (`StackerDBSync`) and other peers make fetch/skip decisions based on advertised `slot_versions`, this can cause peers to treat stale/missing data as up-to-date and skip re-fetching the real latest chunk from a node that actually has it, delaying propagation of legitimate signer/miner messages. The divergence is self-correcting on the *next* freshly-computed `StackerDBChunkInv` (which is always derived from the DB via `make_StackerDBChunksInv_or_Nack`), so it does not create a permanent fork of state, limiting the severity below "non-canonical state served as canonical" in the strict sense used for higher-severity StackerDB write-authorization bugs.

### Likelihood Explanation
Triggering the race requires two peers (or a single attacker controlling two connections) to push valid, signed chunks for the same slot with the same next `slot_version` in a short window, which an attacker who controls the target slot's signing key can trigger deterministically by racing two connections against the victim node. No privileged role or secret key of another party is required beyond the attacker's own slot ownership.

### Recommendation
Only patch/send the `StackerDBChunkInv` acknowledgement after the chunk has actually been durably committed via `try_replace_chunk` (or gate the ack synchronously on the storage result), rather than optimistically reflecting the pushed version before the write is confirmed. Alternatively, perform the staleness check against the authoritative DB value at write time and re-derive the ack from the post-write DB state rather than from a pre-write in-memory patch.

### Proof of Concept
1. Attacker controls the private key for slot `S` in StackerDB contract `C`, currently at version `V`.
2. Attacker opens two P2P connections to victim node `N`.
3. Attacker sends two `StackerDBPushChunk` messages for slot `S`, each with `slot_version = V+1`, valid signatures, over both connections nearly simultaneously.
4. `handle_unsolicited_StackerDBPushChunk` runs `validate_received_chunk` for both against the same stale `expected_versions` (both showing `V`), both pass, and both connections receive a `StackerDBChunkInv` ack claiming slot `S` is now at version `V+1` [5](#0-4) .
5. Both chunks are forwarded to the relayer for actual storage; only the first to reach `try_replace_chunk` succeeds, the second fails with `StaleChunk` and its data is discarded [6](#0-5) .
6. The peer whose chunk was discarded nonetheless holds a validly-signed ack from `N` claiming version `V+1` was accepted, even though `N`'s DB never stored that peer's payload.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L649-718)
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
    }
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
