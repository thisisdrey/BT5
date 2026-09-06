### Title
Malicious StackerDB replica can advertise `u32::MAX` slot versions to monopolize `need_chunks` scheduling and starve fetches from honest peers - (File: `stackslib/src/net/stackerdb/sync.rs`)

### Summary
`StackerDBSync::make_chunk_request_schedule` trusts the `slot_versions` claimed in a peer's `StackerDBChunkInv` without any check that the peer can actually produce a validly-signed chunk at that version. A malicious replica that legitimately holds a session for the DB can claim `u32::MAX` for every slot, which always wins the `request.slot_version < *remote_version` comparison and overwrites/clears the `available` neighbor list for every slot, so only the malicious peer is ever scheduled for those chunks. Since it cannot forge a validly-signed chunk at that version (unless it actually owns the slot), it simply NACKs/times out the follow-up `GetChunk`, causing that sync round to fail to fetch any real update for those slots from honest peers.

### Finding Description
In `make_chunk_request_schedule` [1](#0-0) , for every slot `i`, the code iterates `self.chunk_invs` and compares `local_version` to each peer's claimed `remote_version` with no verification that the peer can back this claim with a validly-signed chunk. When a new peer's claimed version is strictly greater than the currently tracked "best" claim, the code does:
```
available.clear();
available.push(naddr.clone());
*request = StackerDBGetChunkData { ..., slot_version: *remote_version };
```
A malicious replica claiming `u32::MAX` for every slot will always satisfy `request.slot_version < *remote_version`, so it unconditionally evicts any honestly-reported (lower, real) version's neighbor list and becomes the sole listed neighbor for every slot in `need_chunks`. This happens even though nothing here checks that a chunk at version `u32::MAX` actually exists or can be legitimately signed.

`getchunks_begin` [2](#0-1)  then walks `chunk_fetch_priorities` and sends `StackerDBGetChunk` to the (now singular) listed neighbor for each such slot, removing that neighbor from the list after sending. Because the malicious peer is the only listed neighbor, the honest peers that reported the true (lower) versions are never contacted for those slots in this round.

If the malicious peer NACKs or lets the request time out, `getchunks_try_finish` [3](#0-2)  just records the NACK/absence and the slot is not resolved. No fallback to the honest peers is attempted for that slot in the current round because the neighbor list for that priority entry was cleared/replaced.

Downstream, `validate_downloaded_chunk` [4](#0-3)  calls `network.validate_received_chunk(...)`, which enforces signature verification against the slot's actual owner key — the malicious peer cannot forge a valid signature for a slot it doesn't own, so it cannot actually serve a bogus chunk at `u32::MAX`. Its only options are to NACK/timeout the `GetChunk`, confirming that data integrity is protected downstream, but scheduling itself is not protected from the inflated-version claim.

### Impact Explanation
The impact is not forged/stored data (signature verification blocks that) but an availability/liveness degradation: a single malicious StackerDB replica can, per sync round, monopolize the "need_chunks" scheduling entries for every slot by claiming implausibly high versions, causing the node to skip requesting from honest peers that actually have newer data. Since `self.chunk_invs` is repopulated each round from `getchunksinv_try_finish` [5](#0-4)  and the malicious peer can re-send the same inflated inventory every round, this starvation is repeatable across sync rounds, degrading (but not permanently blocking, since NACKs/timeouts are eventually processed and the state machine proceeds) the node's ability to converge on real StackerDB data for the affected contract. This matches "bounded compute DoS on a read endpoint" (High), not Critical, since no forged/unauthenticated write or crash occurs.

### Likelihood Explanation
Preconditions: the attacker must be a peer the node has connected to as a StackerDB replica for the targeted contract (reachable via ordinary P2P handshake/session establishment, no special privilege or secret required, matching the "unprivileged remote peer running their own node" threat model) [6](#0-5) . The attacker cost is a single crafted `StackerDBChunkInv` per round with the correct `slot_versions.len()` (enforced at line 953) but adversarial version values; this is cheap and repeatable indefinitely for as long as the node keeps this peer as a connected replica.

### Recommendation
Do not let a single peer's claimed `remote_version` unconditionally evict/replace the entire `available` neighbor set for a slot. When aggregating in `make_chunk_request_schedule`, track the actual maximum only among peers whose inv exceeds the local version, but retain (or fall back to) neighbors with lower-but-still-newer claims if a higher-claiming peer fails, and treat implausible bulk `u32::MAX` claims with suspicion (e.g., don't let a peer that has never previously demonstrated write activity dominate all slots at once). Alternatively, add a lightweight per-slot round-robin/tie-breaking so that when the "winning" peer for a chunk fails to serve it (NACK/timeout), the schedule retries with the next-best claimed version instead of abandoning the slot for the round.

### Proof of Concept
Rust test in `stackerdb/sync.rs` test module: construct a `StackerDBSync` with `num_slots = N`, seed `self.chunk_invs` with one honest peer reporting `slot_versions = [1; N]` and one malicious peer reporting `slot_versions = [u32::MAX; N]`, set local versions to `[0; N]`. Call `make_chunk_request_schedule` and assert that for every slot, the `available` neighbor list contains only the malicious peer's `NeighborAddress` and the honest peer's is absent (asserting the eviction at lines 391-400). Then simulate `getchunks_begin`/`getchunks_try_finish` with the malicious peer responding with `Nack` for every request, and assert the honest peer is never sent a `StackerDBGetChunk` for those slots in that round, and that `self.downloaded_chunks` remains empty despite the honest peer holding real newer data.

### Citations

**File:** stackslib/src/net/stackerdb/sync.rs (L354-405)
```rust
            for (naddr, chunk_inv) in self.chunk_invs.iter() {
                if chunk_inv.slot_versions.len() != local_slot_versions.len() {
                    // remote peer and our DB are out of sync, so just skip this
                    continue;
                }

                let Some(remote_version) = chunk_inv.slot_versions.get(i) else {
                    // remote peer isn't tracking this chunk
                    continue;
                };

                if local_version >= remote_version {
                    // remote peer has same view as local peer, or stale
                    continue;
                }

                let (request, available) = if let Some(x) = need_chunks.get_mut(&i) {
                    // someone has this chunk already
                    x
                } else {
                    // haven't seen anyone with this data yet.
                    // Add a record for it
                    need_chunks.insert(
                        i,
                        (
                            StackerDBGetChunkData {
                                contract_id: self.smart_contract_id.clone(),
                                rc_consensus_hash: rc_consensus_hash.clone(),
                                slot_id: i as u32,
                                slot_version: *remote_version,
                            },
                            vec![naddr.clone()],
                        ),
                    );
                    continue;
                };

                if request.slot_version < *remote_version {
                    // this peer has a newer view
                    available.clear();
                    available.push(naddr.clone());
                    *request = StackerDBGetChunkData {
                        contract_id: self.smart_contract_id.clone(),
                        rc_consensus_hash: rc_consensus_hash.clone(),
                        slot_id: i as u32,
                        slot_version: *remote_version,
                    };
                } else if request.slot_version == *remote_version {
                    // this peer has the same view as a prior peer.
                    // just track how many times we see this
                    available.push(naddr.clone());
                }
```

**File:** stackslib/src/net/stackerdb/sync.rs (L538-558)
```rust
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

**File:** stackslib/src/net/stackerdb/sync.rs (L720-798)
```rust
    pub fn connect_begin(&mut self, network: &mut PeerNetwork) -> Result<bool, net_error> {
        if self.replicas.is_empty() {
            // find some from the peer DB
            let replicas = self.find_qualified_replicas(network)?;
            self.replicas = replicas;
        }
        debug!(
            "{:?}: {}: connect_begin: establish StackerDB sessions to {} neighbors (out of {} p2p peers)",
            network.get_local_peer(),
            &self.smart_contract_id,
            self.replicas.len(),
            network.get_num_p2p_convos();
            "replicas" => ?self.replicas
        );
        if self.replicas.is_empty() {
            // nothing to do
            return Err(net_error::NoSuchNeighbor);
        }

        let naddrs = mem::replace(&mut self.replicas, HashSet::new());
        for naddr in naddrs.into_iter() {
            if self.comms.is_neighbor_connecting(network, &naddr) {
                debug!(
                    "{:?}: {}: connect_begin: already connecting to StackerDB peer {:?}",
                    network.get_local_peer(),
                    &self.smart_contract_id,
                    &naddr
                );
                self.replicas.insert(naddr);
                continue;
            }
            if self.comms.has_neighbor_session(network, &naddr) {
                debug!(
                    "{:?}: {}: connect_begin: already connected to StackerDB peer {:?}",
                    network.get_local_peer(),
                    &self.smart_contract_id,
                    &naddr
                );
                self.connected_replicas.insert(naddr);
                continue;
            }

            debug!(
                "{:?}: {}: connect_begin: Send Handshake to StackerDB peer {:?}",
                network.get_local_peer(),
                &self.smart_contract_id,
                &naddr
            );
            match self.comms.neighbor_session_begin(network, &naddr) {
                Ok(true) => {
                    // connected!
                    debug!(
                        "{:?}: {}: connect_begin: connected to StackerDB peer {:?}",
                        network.get_local_peer(),
                        &self.smart_contract_id,
                        &naddr
                    );
                    self.num_attempted_connections += 1;
                    self.num_connections += 1;
                    self.connected_replicas.insert(naddr);
                }
                Ok(false) => {
                    // need to retry
                    self.num_attempted_connections += 1;
                    self.replicas.insert(naddr);
                }
                Err(_e) => {
                    debug!(
                        "{:?}: {}: Failed to begin session with {:?}: {:?}",
                        &network.get_local_peer(),
                        &self.smart_contract_id,
                        &naddr,
                        &_e
                    );
                }
            }
        }
        Ok(!self.connected_replicas.is_empty())
    }
```

**File:** stackslib/src/net/stackerdb/sync.rs (L946-1001)
```rust
    pub fn getchunksinv_try_finish(
        &mut self,
        network: &mut PeerNetwork,
    ) -> Result<bool, net_error> {
        for (naddr, message) in self.comms.collect_replies(network).into_iter() {
            let chunk_inv_opt = match message.payload {
                StacksMessageType::StackerDBChunkInv(data) => {
                    if data.slot_versions.len() != self.num_slots {
                        info!("{:?}: {}: Received malformed StackerDBChunkInv from {:?}: expected {} chunks, got {}", network.get_local_peer(), &self.smart_contract_id, &naddr, self.num_slots, data.slot_versions.len());
                        None
                    } else {
                        Some(data)
                    }
                }
                StacksMessageType::Nack(data) => {
                    debug!(
                        "{:?}: {}: remote peer {:?} NACK'ed our StackerDBGetChunksInv with code {}",
                        network.get_local_peer(),
                        &self.smart_contract_id,
                        &naddr,
                        data.error_code
                    );
                    if data.error_code == NackErrorCodes::StaleView
                        || data.error_code == NackErrorCodes::FutureView
                    {
                        self.connected_replicas.remove(&naddr);
                        self.stale_neighbors.insert(naddr);
                    } else {
                        self.unpin_connected_replica(network, &naddr);
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
                    self.unpin_connected_replica(network, &naddr);
                    continue;
                }
            };
            debug!(
                "{:?}: {}: getchunksinv_try_finish: Received StackerDBChunkInv from {:?}: {:?}",
                network.get_local_peer(),
                &self.smart_contract_id,
                &naddr,
                &chunk_inv_opt
            );

            if let Some(chunk_inv) = chunk_inv_opt {
                self.chunk_invs.insert(naddr.clone(), chunk_inv);
                self.connected_replicas.insert(naddr);
            }
        }
```

**File:** stackslib/src/net/stackerdb/sync.rs (L1044-1102)
```rust
        for _i in 0..self.request_capacity {
            if self.comms.count_inflight() >= self.request_capacity {
                break;
            }
            let cur_fetch_priority = self
                .chunk_fetch_priorities
                .get_mut(cur_priority)
                .ok_or_else(|| {
                    error!(
                        "Error setting chunk fetch priories. Priority index out of bounds";
                        "cur_priority" => cur_priority,
                    );
                    net_error::InvalidState
                })?;

            let chunk_request = cur_fetch_priority.0.clone();
            let selected_neighbor_opt = cur_fetch_priority
                .1
                .iter()
                .enumerate()
                .find(|(_i, naddr)| !self.comms.has_inflight(naddr));

            let (idx, selected_neighbor) = if let Some(x) = selected_neighbor_opt {
                x
            } else {
                continue;
            };

            debug!(
                "{:?}: {}: getchunks_begin: Send StackerDBGetChunk(id={},ver={}) at {} to {}",
                &network.get_local_peer(),
                &self.smart_contract_id,
                chunk_request.slot_id,
                chunk_request.slot_version,
                &chunk_request.rc_consensus_hash,
                &selected_neighbor
            );

            if let Err(e) = self.comms.neighbor_send(
                network,
                selected_neighbor,
                StacksMessageType::StackerDBGetChunk(chunk_request.clone()),
            ) {
                info!(
                    "{:?}: {} Failed to request chunk {} from {:?}: {:?}",
                    network.get_local_peer(),
                    &self.smart_contract_id,
                    chunk_request.slot_id,
                    selected_neighbor,
                    &e
                );
                unpin.insert(selected_neighbor.clone());
                continue;
            }

            requested += 1;

            // don't ask this neighbor again
            cur_fetch_priority.1.remove(idx);
```

**File:** stackslib/src/net/stackerdb/sync.rs (L1130-1187)
```rust
        for (naddr, message) in self.comms.collect_replies(network).into_iter() {
            let data = match message.payload {
                StacksMessageType::StackerDBChunk(data) => data,
                StacksMessageType::Nack(data) => {
                    debug!(
                        "{:?}: {}: remote peer {:?} NACK'ed our StackerDBGetChunk with code {}",
                        network.get_local_peer(),
                        &self.smart_contract_id,
                        &naddr,
                        data.error_code
                    );
                    if data.error_code == NackErrorCodes::StaleView
                        || data.error_code == NackErrorCodes::FutureView
                    {
                        self.stale_neighbors.insert(naddr);
                    } else if data.error_code == NackErrorCodes::StaleVersion {
                        // try again immediately, without throttling
                        self.stale_inv = true;
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
                    self.unpin_connected_replica(network, &naddr);
                    continue;
                }
            };

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

            // update bookkeeping
            debug!(
                "{:?}: {}, getchunks_try_finish: Received StackerDBChunk from {:?}",
                network.get_local_peer(),
                &self.smart_contract_id,
                &naddr
            );
            self.add_downloaded_chunk(naddr, data);
        }

        Ok(self.comms.count_inflight() == 0)
    }
```
