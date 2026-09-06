Confirmed: there is no misbehavior/reputation/ban tracking anywhere in `stackslib/src/net/stackerdb/**` (no matches for deny/misbehav/ban/score/reputation). The only per-round penalty is `unpin_connected_replica`, which just drops the peer from `self.connected_replicas` for the *current* sync object, but on the very next `reset()` call `find_new_replicas` re-queries `find_qualified_replicas` against the `PeerDB`, which has no notion of "this peer previously served an unverifiable chunk" — so a malicious peer that is otherwise a legitimately-connected, non-private neighbor will be re-selected indefinitely.

### Title
Unbounded repeated signature-verification DoS via poisoned StackerDB chunk-inventory advertisement - (File: `stackslib/src/net/stackerdb/sync.rs`, `stackslib/src/net/stackerdb/mod.rs`)

### Summary
`StackerDBSync::make_chunk_request_schedule` schedules a fetch whenever a neighbor's advertised `slot_version` in `StackerDBChunkInvData` exceeds the locally stored version, with no requirement that the advertising peer or any other peer has ever produced a verifiable chunk at that version. A remote, unprivileged peer can advertise an ever-increasing bogus version for a slot it does not control, forcing the victim to repeatedly issue `StackerDBGetChunkData` requests and run full signature-verification (`validate_received_chunk`/`SlotMetadata::verify`) each round, and the peer is not durably down-ranked afterward.

### Finding Description
`make_chunk_request_schedule` (`stackslib/src/net/stackerdb/sync.rs:308-408`) compares `local_version >= remote_version` per slot per neighbor inventory and schedules a `StackerDBGetChunkData` whenever `remote_version` is strictly greater [1](#0-0) . This decision is made purely from the unauthenticated `slot_versions` vector in `StackerDBChunkInvData`, which is accepted at face value in `getchunksinv_try_finish` as long as its length matches `num_slots` [2](#0-1) . No signature or ownership check is performed on the inventory advertisement itself — only the eventual chunk fetch is verified.

When the scheduled `StackerDBGetChunk` is sent and a reply arrives, `getchunks_try_finish` calls `validate_downloaded_chunk`/verification, and on failure calls `self.unpin_connected_replica(network, &naddr)` [3](#0-2) . This only removes the peer from `self.connected_replicas` and unpins its connection for the *current* sync pass [4](#0-3) .

On the next round, `reset()` rebuilds the replica set via `find_new_replicas`, which starts from the *previous* `connected_replicas` (now missing the evicted peer) but then re-queries `find_qualified_replicas` against `PeerDB::find_stacker_db_replicas` [5](#0-4) . That lookup has no concept of "this peer previously served an unverifiable chunk" — it only filters on address type/network/privacy settings [6](#0-5) . There is no ban list, misbehavior score, or backoff timer anywhere in `stackslib/src/net/stackerdb/` (confirmed by exhaustive search for deny/ban/score/reputation terms — none exist). Consequently the malicious peer is re-admitted to `self.replicas` in a subsequent round, its (still-advertised, ever-increasing) fake inventory is fetched again via `getchunksinv_begin`, `make_chunk_request_schedule` schedules the fetch again (since local storage never advanced past its own valid version), and `validate_received_chunk`/`SlotMetadata::verify` (`stackslib/src/net/stackerdb/mod.rs:649-717`, `libstackerdb/src/libstackerdb.rs:181-193`) run again only to fail again.

### Impact Explanation
Each round costs the victim: one inventory round-trip, a scheduling pass over all slots, one chunk fetch, and one full ECDSA public-key-recovery + hash comparison in `SlotMetadata::verify`. This is a bounded-per-message compute cost, but it is unauthenticated and repeatable indefinitely from a single cheap, low-bandwidth inventory message (the attacker need not resend the fake chunk with a different signature each time — they can simply keep advertising an incrementing version in the inventory and let the fetch/verify cycle repeat). Because there is no down-ranking or backoff for this specific pattern, the attacker gets sustained "free" CPU amplification on the victim's StackerDB sync scheduler for as long as the connection/neighbor-set membership persists. This matches "bounded compute DoS on a read endpoint" — impact is limited to CPU cycles wasted on the sync state machine, not to state corruption, so it is a High rather than Critical finding.

### Likelihood Explanation
Preconditions are modest and match the unprivileged-attacker model: the attacker's peer only needs to be a normal, otherwise-well-behaved P2P neighbor that participates in StackerDB gossip for a contract the victim replicates (`find_qualified_replicas` requires the peer be a non-private, non-anynet address known to `PeerDB` as a StackerDB replica for that contract — reachable simply by running a normal node and registering interest). No secret, no slot ownership, and no privileged role is required. The attacker's cost per round is a single crafted `StackerDBChunkInvData` message and one crafted-but-invalid `StackerDBChunkData` reply, both cheap to produce. Repeatability is high because eviction from `connected_replicas` is transient and the `PeerDB`-driven re-discovery has no memory of past bad behavior.

### Recommendation
Track per-neighbor-address failure counts for chunk-verification failures inside `StackerDBSync` (or in `PeerDB`) and exclude/penalize peers that repeatedly serve unverifiable chunks from `find_qualified_replicas`/`find_new_replicas` for an escalating cooldown period, rather than only unpinning for the current round. Additionally, consider not re-scheduling a fetch for the same (slot_id, advertised_version) from a peer that has already failed to produce a verifiable chunk at that exact version in the current epoch, to avoid immediately retrying the identical poisoned advertisement.

### Proof of Concept
Rust test plan in `stackslib::net::stackerdb::tests::sync`:
1. Set up two `TestPeer`s (`peer_attacker`, `peer_victim`) sharing a StackerDB contract with `setup_stackerdb`, with `peer_attacker` holding no valid slot-signer key for slot 0.
2. Drive `peer_victim`'s `StackerDBSync` through `getchunksinv_begin`/`getchunksinv_try_finish` with a crafted `StackerDBChunkInvData` from `peer_attacker` reporting `slot_versions[0] = N` (N increasing each round), while the real signer never writes past version 0.
3. Assert `make_chunk_request_schedule` schedules a `StackerDBGetChunkData` for slot 0 targeting `peer_attacker` each round (`slot_version` equal to the advertised fake value) — confirms the scheduling side has no cross-check.
4. Have `peer_attacker` reply with a `StackerDBChunkData` signed with a random unrelated key at version N; assert `getchunks_try_finish` rejects it (`validate_downloaded_chunk` returns false) and calls `unpin_connected_replica`.
5. Run several full sync rounds via `run_once`/`reset`, incrementing the attacker's fake advertised version each round; assert (expected to FAIL against current code) that after some fixed number of rounds `peer_attacker`'s `NeighborAddress` is excluded from `self.replicas`/`find_qualified_replicas` — this assertion will not hold, demonstrating the peer is repeatedly re-admitted and the fetch/verify cycle repeats indefinitely.

### Citations

**File:** stackslib/src/net/stackerdb/sync.rs (L96-125)
```rust
        while found.len() < self.max_neighbors {
            let peers_iter = PeerDB::find_stacker_db_replicas(
                network.peerdb_conn(),
                network.get_local_peer().network_id,
                &self.smart_contract_id,
                min_age,
                self.max_neighbors,
            )?
            .into_iter()
            .map(|neighbor| {
                (
                    NeighborAddress::from_neighbor(&neighbor),
                    neighbor.last_contact_time,
                )
            })
            .filter(|(naddr, _)| {
                if naddr.addrbytes.is_anynet() {
                    return false;
                }
                if naddr.public_key_hash == local_naddr.public_key_hash {
                    // don't talk to us by another address
                    return false;
                }
                if !network.get_connection_opts().private_neighbors
                    && naddr.addrbytes.is_in_private_range()
                {
                    return false;
                }
                true
            });
```

**File:** stackslib/src/net/stackerdb/sync.rs (L148-169)
```rust
    fn find_new_replicas(
        &self,
        mut connected_replicas: HashSet<NeighborAddress>,
        network: Option<&PeerNetwork>,
        config: &StackerDBConfig,
    ) -> Result<HashSet<NeighborAddress>, net_error> {
        // keep all connected replicas, and replenish from config hints and the DB as needed
        let mut peers = config.hint_replicas.clone();
        if let Some(network) = network {
            let extra_peers = self.find_qualified_replicas(network)?;
            peers.extend(extra_peers);
        }

        peers.shuffle(&mut thread_rng());

        for peer in peers {
            if connected_replicas.len() >= config.max_neighbors {
                break;
            }
            connected_replicas.insert(peer);
        }
        Ok(connected_replicas)
```

**File:** stackslib/src/net/stackerdb/sync.rs (L286-292)
```rust
    pub fn unpin_connected_replica(&mut self, network: &PeerNetwork, naddr: &NeighborAddress) {
        let nk = naddr.to_neighbor_key(network);
        if let Some(event_id) = network.get_event_id(&nk) {
            self.comms.unpin_connection(event_id);
        }
        self.connected_replicas.remove(naddr);
    }
```

**File:** stackslib/src/net/stackerdb/sync.rs (L360-368)
```rust
                let Some(remote_version) = chunk_inv.slot_versions.get(i) else {
                    // remote peer isn't tracking this chunk
                    continue;
                };

                if local_version >= remote_version {
                    // remote peer has same view as local peer, or stale
                    continue;
                }
```

**File:** stackslib/src/net/stackerdb/sync.rs (L951-959)
```rust
            let chunk_inv_opt = match message.payload {
                StacksMessageType::StackerDBChunkInv(data) => {
                    if data.slot_versions.len() != self.num_slots {
                        info!("{:?}: {}: Received malformed StackerDBChunkInv from {:?}: expected {} chunks, got {}", network.get_local_peer(), &self.smart_contract_id, &naddr, self.num_slots, data.slot_versions.len());
                        None
                    } else {
                        Some(data)
                    }
                }
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
