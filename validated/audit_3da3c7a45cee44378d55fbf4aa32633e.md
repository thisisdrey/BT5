### Title
StackerDB push-chunk handler advertises a chunk as accepted in its inventory reply before the chunk is actually committed to storage - (File: stackslib/src/net/stackerdb/mod.rs)

### Summary
`PeerNetwork::handle_unsolicited_StackerDBPushChunk` optimistically patches the in-memory `StackerDBChunkInv.slot_versions` array to reflect a pushed chunk's new version *before* the chunk data is actually written to the `StackerDBs` on-disk store, then immediately signs and sends that inventory as an authoritative ack to the peer.

### Finding Description
When a peer pushes an unsolicited `StackerDBPushChunk`, the handler builds a `StackerDBChunkInv` reply via `make_StackerDBChunksInv_or_Nack`, runs `validate_received_chunk` (signature/size/version checks only, no DB write), and on success directly mutates the reply's `slot_versions` entry to the new version: [1](#0-0) 

It then signs and sends this patched inventory back to the sender as a normal reply, and separately forwards the chunk to the relayer for actual storage: [2](#0-1) 

The actual commit of the chunk into the StackerDB (via the signer/version/write-count checks in `try_replace_chunk`) happens later, out-of-band, in the relay path: [3](#0-2) 

This mirrors the StRSR bug class: a *derived/cached statement of state* (`rsrRewardsAtLastPayout`, here the advertised `slot_versions` sent in the inventory ack) is updated speculatively based on an operation that has not yet been durably applied, and nothing guarantees the two stay in sync if the deferred apply (relay processing) fails, is dropped, races with a concurrent write to the same slot, or the process crashes between the ack and the actual `try_replace_chunk` call.

### Impact Explanation
If the deferred store in the relay path fails or never completes (e.g., a concurrent chunk for the same slot lands first and bumps the version past this one, the relayer errors out, or the node restarts before processing), the node has already told its neighbor "I have version N of this slot" while its actual on-disk replica still holds an older version. Downstream StackerDB sync logic on both sides consumes `slot_versions` as ground truth for what a replica holds, so this false inventory can cause neighbors to skip re-fetching/re-pushing the real data to this node, delaying propagation of the chunk to it. This is a "false inventory" class issue analogous to steering a node off correct state via stale/incorrect advertised versions, though bounded to StackerDB chunk replication rather than block inventory/tip data.

### Likelihood Explanation
Requires only an unprivileged remote peer to push a validly-signed `StackerDBPushChunk`; no privileged role or secret key is needed. However, the window in which the advertised version and the actually-stored version diverge is narrow (bounded by relay processing time) and self-heals on the next successful chunk sync/inventory exchange, similar to how the original StRSR bug self-corrects on the next `_payoutRewards` call. This limits severity to a transient, low-impact false-inventory report rather than a persistent state corruption or unauthorized write.

### Recommendation
Only patch and send the `slot_versions` entry in the reply after the chunk has been durably written via `try_replace_chunk` (or equivalent), rather than optimistically before the deferred relay-based store. Alternatively, have the relayer report success/failure back so the previously-sent ack can be corrected/invalidated on failure.

### Proof of Concept
Not independently reproducible end-to-end from static analysis alone: confirming actual impact requires tracing the relay code path (`stackslib/src/net/relay.rs`) that consumes the forwarded `StackerDBPushChunk` and calls `try_replace_chunk`, to determine concrete failure/race conditions (e.g., version collisions with a second push chunk for the same slot arriving before relay processing completes) that would cause the deferred store to diverge from the already-sent inventory ack. This tracing was not completed before the tool budget was exhausted, so the exact trigger conditions for a persistent (not just momentary) mismatch remain unverified.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L784-807)
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
