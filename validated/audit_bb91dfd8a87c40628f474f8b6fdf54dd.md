### Title
False StackerDBChunkInv slot_versions can pin the download schedule to a lying peer, starving honest replicas - (File: stackslib/src/net/stackerdb/sync.rs)

### Summary
`make_chunk_request_schedule` selects, for each slot, the single highest `slot_version` advertised across all `StackerDBChunkInv` responses collected in `self.chunk_invs`, with no proof the claimed version corresponds to a real, validly-signed chunk. A peer that only needs to pass the trivial length-equality check (`chunk_inv.slot_versions.len() == local_slot_versions.len()`) can advertise `u32::MAX` for any slot it doesn't even own and permanently win that slot's request slot against honest peers who report their true (lower) versions.

### Finding Description
In `make_chunk_request_schedule` [1](#0-0) , for slot `i`, the code iterates all peers' `chunk_invs` and keeps only the entry with the strictly highest `remote_version`; ties add to `available`, but any peer whose claimed version is lower than the current max is silently dropped (`continue`), regardless of whether that lower version is real. Because `StackerDBChunkInv.slot_versions` is unauthenticated inventory data — a length check is the only validation performed both here and in `getchunksinv_try_finish` [2](#0-1)  — an attacker can claim `u32::MAX` for slots it does not own, guaranteeing it is the sole/preferred source recorded in `need_chunks` (lines 391-400) since `request.slot_version < *remote_version` will always hold against real, bounded chunk versions.

`getchunksinv_try_finish` then builds `chunk_fetch_priorities` directly from this schedule (line 1008-1011), and `getchunks_begin` sends `StackerDBGetChunkData` only to the neighbors listed for each slot (lines 1060-1085) — i.e., only to the attacker for the poisoned slot. If the attacker NACKs with a non-stale error code, or simply never replies, `getchunks_try_finish`'s NACK branch takes no corrective action beyond `continue` for generic error codes (lines 1133-1150), and non-replies eventually time out via the connection's request timeout, which only marks the *connection* dead — it does not record that the peer lied about its inventory, blacklist it, or lower the trust in its future `ChunkInv` claims. Because `self.chunk_invs` is rebuilt every round purely from whatever `StackerDBChunkInv` the peer chooses to send, the attacker can repeat the lie indefinitely on each sync round, keeping the schedule for that slot pinned to itself and preventing the schedule from ever including the honest peers that could serve the real update. This is because the algorithm at lines 391-405 discards all "loser" versions unconditionally instead of retaining a fallback set of prior real advertisers.

### Impact Explanation
For the specific slot(s) the attacker chooses to lie about, honest peers are excluded from `chunk_fetch_priorities`/the schedule every round, so the node's StackerDB replica for that contract never receives the real, validly-signed chunk update from any honest peer as long as the liar keeps re-advertising the same or higher fake version and keeps failing/NACKing the follow-up `StackerDBGetChunkData`. This is a bounded-compute, round-repeating denial of service against StackerDB sync (affecting Nakamoto signer-set / miner-info StackerDBs, which are security relevant), matching the "steering a node off canonical/expected inventory via false inventory, bounded per round" High category. The attacker cannot forge a validly-signed chunk for a slot it doesn't own, so it cannot inject false *data* — only starve the legitimate data from being fetched, which is consistent with the question's framing.

### Likelihood Explanation
Any p2p peer that can complete a handshake and respond to `StackerDBGetChunkInv` can mount this attack; no slot ownership or secret is required, matching the "unprivileged remote peer" threat model. The cost is a single crafted `StackerDBChunkInv` message per sync round plus repeated non-answers/NACKs to `StackerDBGetChunkData` — trivially cheap and repeatable indefinitely as long as the attacker's connection to the victim persists (peers can reconnect if dropped for a dead connection).

### Recommendation
Do not let a single peer's advertised version unconditionally evict previously-seen advertisers for a slot. Track, per slot, the maximum version *actually confirmed by a successfully validated chunk* (or at least retain a set of the top-N distinct advertisers so honest peers remain schedulable), and penalize/deprioritize (e.g., temporarily blacklist from being treated as an inventory source) any peer whose advertised version is not eventually corroborated by a validly-signed chunk within a bounded number of rounds, before it can be picked again as the sole authoritative source for a slot.

### Proof of Concept
Rust test in `stackslib::net::stackerdb::tests::sync`:
1. Set up a `StackerDBSync` with several honest mock peers whose `StackerDBChunkInv.slot_versions` reflect real, low version numbers matching chunks they can actually serve, plus one malicious peer.
2. Inject via `self.chunk_invs.insert(malicious_naddr, StackerDBChunkInvData { slot_versions: vec![u32::MAX; num_slots], .. })` alongside honest entries with real versions.
3. Call `make_chunk_request_schedule` and assert that, for the poisoned slot index, the returned schedule's neighbor list is `[malicious_naddr]` only (confirming exclusion of honest peers) — this is the "broken equality" assertion.
4. Extend the test to simulate several full sync rounds where the malicious peer's `StackerDBGetChunkData` request always returns a `Nack` (non-stale code) or times out, and assert that after N rounds the schedule for that slot never includes an honest peer and the honest peer's real chunk is never stored (`self.downloaded_chunks` stays empty for that slot), demonstrating the round-repeating starvation described.

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
