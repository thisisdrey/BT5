### Title
Missing `write_freq` Throttle Enforcement on the StackerDB HTTP Chunk-Upload Path Enables Write/Relay Amplification - (File: `stackslib/src/net/stackerdb/db.rs`)

### Summary
`StackerDBTx::try_replace_chunk`, which is the sole write-gate used by the unauthenticated `POST /v2/stackerdb/:address/:contract/chunks` HTTP endpoint, enforces per-chunk size, signature ownership, version-staleness, and `max_writes`, but never enforces the StackerDB's configured `write_freq` wall-clock throttle. Any holder of a slot's signing key can therefore submit valid, correctly-signed chunks at an unbounded rate, and every accepted write is immediately relayed to the node's sampled StackerDB-subscribing neighbors, turning a single write into a fan-out of signature verifications, DB writes, and network sends across the replica set.

### Finding Description
`StackerDBConfig` defines `write_freq` explicitly as a rate limiter for slot writes [1](#0-0) , and it is consulted by the gossip/pull-push sync logic in `sync.rs`. However, `try_replace_chunk` — the function that actually performs the authenticated write to the `chunks` table — only checks:
- chunk size vs `config.chunk_size`
- slot ownership via `slot_desc.verify(&slot_validation.signer)`
- staleness (`slot_version <= slot_validation.version`)
- `slot_version > config.max_writes`

and never checks `write_freq` or `write_time` before calling `insert_chunk`: [2](#0-1) 

The HTTP endpoint handler calls this same `try_replace_chunk` directly on every POST, with no additional rate limiting of its own: [3](#0-2) 

On acceptance, the handler immediately schedules the chunk for p2p relay: [4](#0-3) 

which is dispatched via `NetworkRequest::Broadcast` → `sample_broadcast_peers` → `broadcast_message`, fanning the single accepted write out to up to `MAX_BROADCAST_OUTBOUND_RECEIVERS` + `MAX_BROADCAST_INBOUND_RECEIVERS` peers, each of which will re-verify the signature and persist the chunk to its own replica: [5](#0-4) [6](#0-5) 

The only remaining brake against unbounded writes is `max_writes`, which is a *count* cap on total chunk versions per slot, not a *time* throttle — so a slot owner can burn through the entire `max_writes` budget in a tight loop with no wall-clock delay, and each such write causes fan-out validation/storage/network work on every subscribing peer. This directly parallels the Wings/Pterodactyl advisory's bug class: a resource-consumption control that is defined in configuration (`write_freq`, analogous to a PID limit) but not actually enforced on one of the write paths, letting an authorized-but-unprivileged actor (a slot key holder, not an admin) consume disproportionate downstream resources.

### Impact Explanation
Each locally-accepted chunk write is relayed to multiple neighbors, each of which performs signature verification and a SQLite write. Because `write_freq` is not enforced on the HTTP ingestion path, an attacker who legitimately controls a StackerDB slot key (e.g., a registered signer) can submit chunks as fast as the HTTP server can process them, up to `max_writes` versions, causing amplified CPU (signature recovery), disk I/O, and network bandwidth consumption across every peer that replicates that StackerDB — a bounded-compute DoS vector consistent with the "High" impact tier (asymmetric resource consumption / amplification via a write endpoint whose per-request cost is multiplied by fan-out relay).

### Likelihood Explanation
Exploitability requires possession of a valid slot-signing key, which is the normal precondition for participating as a StackerDB signer (not an admin/node secret). Any registered signer for any StackerDB instance (e.g. the `.signers-0-X` contracts used by Nakamoto signers) can trigger this without any additional privilege, making the likelihood moderate-to-high in any deployment with StackerDB signer registration.

### Recommendation
Enforce `config.write_freq` (mirroring the check already performed in `sync.rs`) inside `try_replace_chunk`/`insert_chunk` in `stackslib/src/net/stackerdb/db.rs`, comparing the candidate write's timestamp against `slot_validation.write_time + write_freq`, and reject (or queue) writes that arrive too soon — regardless of whether they originate from the HTTP upload endpoint or the p2p sync path.

### Proof of Concept
1. Register as a signer for a StackerDB slot (obtain the slot's private key as intended by protocol design).
2. Repeatedly `POST /v2/stackerdb/:address/:contract/chunks` with monotonically increasing `slot_version` and a valid signature, with no delay between requests.
3. Observe that `try_replace_chunk` accepts every well-formed, non-stale, correctly-signed request regardless of submission rate (bounded only by `max_writes`), and that each acceptance triggers `node.set_relay_message` → broadcast to sampled neighbors, multiplying local write cost into network-wide verification/storage/bandwidth cost, confirmed by the absence of any `write_freq`/`write_time` check in `try_replace_chunk` at `stackslib/src/net/stackerdb/db.rs:398-439`.

### Citations

**File:** stackslib/src/net/stackerdb/config.rs (L1-1)
```rust
// Copyright (C) 2013-2020 Blockstack PBC, a public benefit corporation
```

**File:** stackslib/src/net/stackerdb/db.rs (L398-439)
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
}
```

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L197-201)
```rust
                if let Err(e) = tx.try_replace_chunk(
                    &contract_identifier,
                    &stackerdb_chunk.get_slot_metadata(),
                    &stackerdb_chunk.data,
                ) {
```

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

**File:** stackslib/src/net/p2p.rs (L1254-1267)
```rust
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
```

**File:** stackslib/src/net/p2p.rs (L1611-1659)
```rust
            NetworkRequest::Broadcast(relay_hints, msg) => {
                // pick some neighbors. Note that only some messages can be broadcasted.
                let neighbor_keys = match msg {
                    StacksMessageType::Blocks(ref data) => {
                        // send to each neighbor that needs one
                        let mut all_neighbors = HashSet::new();
                        for BlocksDatum(_, block) in data.blocks.iter() {
                            let neighbors = self.sample_broadcast_peers(&relay_hints, block)?;
                            for nk in neighbors.into_iter() {
                                all_neighbors.insert(nk);
                            }
                        }
                        Ok(all_neighbors.into_iter().collect())
                    }
                    StacksMessageType::Microblocks(ref data) => {
                        // send to each neighbor that needs at least one
                        let mut all_neighbors = HashSet::new();
                        for mblock in data.microblocks.iter() {
                            let neighbors = self.sample_broadcast_peers(&relay_hints, mblock)?;
                            for nk in neighbors.into_iter() {
                                all_neighbors.insert(nk);
                            }
                        }
                        Ok(all_neighbors.into_iter().collect())
                    }
                    StacksMessageType::NakamotoBlocks(ref data) => {
                        // send to each neighbor that needs one
                        let mut all_neighbors = HashSet::new();
                        for nakamoto_block in data.blocks.iter() {
                            let neighbors =
                                self.sample_broadcast_peers(&relay_hints, nakamoto_block)?;

                            all_neighbors.extend(neighbors);
                        }
                        Ok(all_neighbors.into_iter().collect())
                    }
                    StacksMessageType::StackerDBPushChunk(ref data) => {
                        Ok(self.sample_broadcast_peers(&relay_hints, data)?)
                    }
                    StacksMessageType::Transaction(ref data) => {
                        self.sample_broadcast_peers(&relay_hints, data)
                    }
                    _ => {
                        // not suitable for broadcast
                        return Err(net_error::InvalidMessage);
                    }
                }?;
                self.broadcast_message(neighbor_keys, relay_hints, msg);
                Ok(())
```
