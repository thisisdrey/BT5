### Title
Unauthenticated peer-supplied `StackerDBChunkInv` versions poison the freshness gate used to validate downloaded chunks - (File: stackslib/src/net/stackerdb/sync.rs)

### Summary
The bug pattern is the same as the `BondingTax` slippage flaw: a security check's "expected/reference" value is derived dynamically from the very same untrusted channel that supplies the data being checked, instead of from an independently trusted source. In `BondingTax`, `minOutput` was computed from the manipulable on-chain spot price at call time. In `StackerDBSync`, the `expected_versions` array used to gate "staleness" of downloaded `StackerDB` chunks is, in one code path, computed directly from unauthenticated, remote-peer-supplied `StackerDBChunkInvData.slot_versions` fields rather than from the node's own authenticated/local ledger of slot versions.

### Finding Description
`StackerDBSync::validate_downloaded_chunk` delegates freshness validation to `PeerNetwork::validate_received_chunk`, which rejects a chunk when `data.slot_version < *expected_version`: [1](#0-0) 

Normally, `expected_versions` is populated from the node's own database (`self.stackerdbs.get_slot_versions(...)`), a trusted source: [2](#0-1) 

However, `StackerDBSync::recalculate_chunk_request_schedule` — invoked when a push-chunk round indicates the state machine needs to resync (`need_resync == true`) — recomputes `expected_versions` purely from `self.chunk_invs`, i.e., from the `slot_versions` vectors that remote (and possibly malicious) neighbors self-reported in their `StackerDBChunkInvData` replies, taking the maximum value per slot across all received inventories: [3](#0-2) 

`StackerDBChunkInvData` is unauthenticated wire data (a plain vector of `u32` versions in the message payload) — there is no signature or cryptographic binding tying a slot's claimed version number to the actual signer of that slot. Any peer that replies to a `StackerDBGetChunksInv`/pushed-chunk exchange can populate `slot_versions[i]` with an arbitrary value (e.g., `u32::MAX`).

Because `recalculate_chunk_request_schedule` folds in every currently-tracked `chunk_invs` entry (not just the entry from the peer that triggered `need_resync`), a single malicious replica can poison `self.expected_versions[i]` to an unreachable value. This poisoned value is then fed directly into `validate_downloaded_chunk` → `validate_received_chunk` for the subsequent `GetChunks` round in the same sync pass: [4](#0-3) 

Any subsequently-downloaded chunk from any (including entirely honest) replica — even one with a valid signature and a legitimately-higher version than what the local DB has — will now fail the "must be current or newer version" check and be treated as invalid, causing the honest replica to be unpinned/disconnected: [5](#0-4) 

This is structurally the same defect class as the `BondingTax` report: the code trusts a value derived from the same manipulable/untrusted input stream it is meant to validate against, so the check can be forced to reject (or, in other contexts, accept) data regardless of its actual validity.

### Impact Explanation
This lets an unprivileged remote peer forge unsigned inventory data (`StackerDBChunkInvData`) to poison the freshness gate for a `StackerDB` slot, causing the victim node to spuriously reject legitimately signed, newer chunks from honest replicas and to unpin/disconnect those honest neighbors during the affected sync round. This matches the "steering a node off the tip via false inventory" class of impact: forged, unauthenticated gossip data is used to override the node's local, trustworthy view of chunk freshness, degrading its ability to converge on the correct/latest `StackerDB` state (e.g., signer-message or miner-coordination StackerDBs) for at least the current sync cycle, and repeatedly if the attacker keeps re-triggering `need_resync`.

### Likelihood Explanation
The attack requires no privileged key or role — only that the attacker's node can participate in `StackerDB` replication (handshake + respond to `StackerDBGetChunksInv`/pushed-chunk exchanges), which is available to any p2p neighbor. It requires triggering a push-chunk round that sets `need_resync = true` and having an entry in `self.chunk_invs`, both of which are reachable through normal, low-cost protocol interactions. The poisoning is bounded to the "recalculate" branch of the sync state machine and is corrected on the next full sync pass (`GetChunksInvFinish` re-reads from the local DB), so it is a transient but easily and repeatedly triggerable disruption rather than a permanent one.

### Recommendation
Do not derive the version-freshness gate (`expected_versions`) used by `validate_received_chunk`/`validate_downloaded_chunk` from unauthenticated `StackerDBChunkInvData` supplied by remote peers. Continue to source `expected_versions` from the node's own authenticated state (`self.stackerdbs.get_slot_versions`) for all validation purposes, and reserve peer-reported inventories exclusively for prioritizing *which* chunks to request/push — never for deciding whether a downloaded chunk is acceptable. If inventory-driven expectations must inform request scheduling, keep that value fully separate from `self.expected_versions`, and validate/re-fetch based only on the locally known state.

### Proof of Concept
1. Attacker node establishes itself as a `StackerDB` replica neighbor of the victim and, in a normal push-chunk exchange, gets included in the victim's `self.chunk_invs` map (e.g., by having any active chunk inventory entry, honest or not).
2. Attacker replies to a `StackerDBPushChunk`/`StackerDBGetChunksInv` round with a `StackerDBChunkInvData` whose `slot_versions[i] = u32::MAX` for a target slot `i` (see `pushchunks_try_finish` handling of `StackerDBChunkInv`): [6](#0-5) 
3. This causes `need_resync = true` via `add_pushed_chunk`'s comparison logic, and the state machine calls `recalculate_chunk_request_schedule`, which computes `expected_versions[i] = u32::MAX` purely from the attacker's unsigned inventory data: [7](#0-6) 
4. In the immediately following `GetChunks` state, any honest replica's correctly-signed chunk for slot `i` (with a real, much-lower `slot_version`) is fetched and passed to `validate_downloaded_chunk`, which fails the freshness check in `validate_received_chunk` (`data.slot_version < *expected_version`), causing the chunk to be discarded and the honest neighbor to be unpinned: [5](#0-4) 

Note: I was unable to fully inspect `StackerDBSync::add_pushed_chunk` (the function that decides `need_resync` and how `chunk_invs` entries are populated/replaced) in this pass due to running out of tool iterations before its body could be retrieved; the exact triggering condition for `need_resync` should be double-checked in that function to fully confirm the end-to-end trigger sequence, though the core defect — `expected_versions` being sourced from unauthenticated peer inventory data in `recalculate_chunk_request_schedule` — is confirmed directly from the cited code.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L699-706)
```rust
        // validate -- must be the current or newer version
        if data.slot_version < *expected_version {
            info!(
                "Received StackerDBChunk for {} ID {} version {}, which is stale (expected {})",
                smart_contract_id, data.slot_id, data.slot_version, *expected_version
            );
            return Ok(false);
        }
```

**File:** stackslib/src/net/stackerdb/sync.rs (L537-558)
```rust
    /// Validate a downloaded chunk
    pub fn validate_downloaded_chunk(
        &self,
        network: &PeerNetwork,
        config: &StackerDBConfig,
        data: &StackerDBChunkData,
    ) -> Result<bool, net_error> {
        // validate -- must be a valid chunk
        if !network.validate_received_chunk(
            &self.smart_contract_id,
            config,
            data,
            &self.expected_versions,
        )? {
            return Ok(false);
        }

        // no need to validate the timestamp, because we already skipped requesting it if it was
        // written too recently.

        Ok(true)
    }
```

**File:** stackslib/src/net/stackerdb/sync.rs (L1007-1013)
```rust
        // got everything. Calculate download priority
        let priorities = self.make_chunk_request_schedule(network, None)?;
        let expected_versions = self.stackerdbs.get_slot_versions(&self.smart_contract_id)?;

        self.chunk_fetch_priorities = priorities;
        self.expected_versions = expected_versions;
        Ok(true)
```

**File:** stackslib/src/net/stackerdb/sync.rs (L1163-1174)
```rust
            // validate
            if !self.validate_downloaded_chunk(network, config, &data)? {
                info!(
                    "{:?}: {}: Remote neighbor {:?} served an invalid chunk for ID {}",
                    network.get_local_peer(),
                    &self.smart_contract_id,
                    &naddr,
                    data.slot_id
                );
                self.unpin_connected_replica(network, &naddr);
                continue;
            }
```

**File:** stackslib/src/net/stackerdb/sync.rs (L1304-1338)
```rust
    pub fn pushchunks_try_finish(&mut self, network: &mut PeerNetwork) -> bool {
        for (naddr, message) in self.comms.collect_replies(network).into_iter() {
            let new_chunk_inv = match message.payload {
                StacksMessageType::StackerDBChunkInv(data) => data,
                StacksMessageType::Nack(data) => {
                    debug!(
                        "{:?}: {}: remote peer {:?} NACK'ed our StackerDBChunk with code {}",
                        network.get_local_peer(),
                        &self.smart_contract_id,
                        &naddr,
                        data.error_code
                    );
                    if data.error_code == NackErrorCodes::StaleView
                        || data.error_code == NackErrorCodes::FutureView
                    {
                        self.stale_neighbors.insert(naddr);
                    }
                    continue;
                }
                x => {
                    info!(
                        "{:?}: {}: Received unexpected message {:?}",
                        network.get_local_peer(),
                        &self.smart_contract_id,
                        &x
                    );
                    continue;
                }
            };

            // must be well-formed
            if new_chunk_inv.slot_versions.len() != self.num_slots {
                info!("{:?}: {}: Received malformed StackerDBChunkInv from {:?}: expected {} chunks, got {}", network.get_local_peer(), &self.smart_contract_id, &naddr, self.num_slots, new_chunk_inv.slot_versions.len());
                continue;
            }
```

**File:** stackslib/src/net/stackerdb/sync.rs (L1364-1387)
```rust
    /// Recalculate the download schedule based on chunkinvs received on push
    pub fn recalculate_chunk_request_schedule(
        &mut self,
        network: &PeerNetwork,
    ) -> Result<(), net_error> {
        // figure out the new expected versions
        let mut expected_versions = vec![0u32; self.num_slots];
        for (_, chunk_inv) in self.chunk_invs.iter() {
            for (slot_version, expected_version) in chunk_inv
                .slot_versions
                .iter()
                .zip(expected_versions.iter_mut())
            {
                *expected_version = (*slot_version).max(*expected_version);
            }
        }

        let priorities =
            self.make_chunk_request_schedule(network, Some(expected_versions.clone()))?;

        self.chunk_fetch_priorities = priorities;
        self.expected_versions = expected_versions;
        Ok(())
    }
```
