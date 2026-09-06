### Title
Unbounded growth of `prune_outbound_counts` map via repeated org-based outbound eviction with attacker-varied `NeighborKey` - (File: stackslib/src/net/prune.rs)

### Summary
`PeerNetwork::prune_frontier` records every neighbor pruned by `prune_frontier_outbound_orgs` into `self.prune_outbound_counts`, keyed by the peer's `NeighborKey`, with no cap on map size or eviction of stale entries. A remote peer that can repeatedly get itself accepted as an outbound neighbor of the victim (e.g., by being gossiped/discovered at the same IP but with varying advertised port) and then evicted for dominating its organization's neighbor share can force a new, distinct `NeighborKey` entry into this map on every cycle, growing it without bound.

### Finding Description
In `prune_frontier`, for every `(key, reason)` returned by `prune_frontier_outbound_orgs`, the code does: [1](#0-0) 
This unconditionally inserts a new entry into `self.prune_outbound_counts` keyed by the full `NeighborKey` (network_id, addrbytes, port, peer_version-independent identity) whenever that exact key hasn't been pruned before, and increments the counter otherwise. There is no upper bound on the number of distinct keys tracked and no expiry/trim logic anywhere in this file for `prune_outbound_counts`. `prune_frontier_outbound_orgs` selects candidates by org based on `peer.org` looked up from `PeerDB` by `(network_id, addrbytes, port)` [2](#0-1) , and evicts the least healthy/uptime members of over-represented orgs [3](#0-2) . `NeighborKey.port` is attacker-controlled (the port a discovered/advertised peer address uses); an attacker who can get itself walked/connected to as an outbound neighbor of the victim under many different ports from the same IP/org can be evicted and reconnected with a fresh `NeighborKey` on each cycle, each time adding a brand-new entry to `prune_outbound_counts` that is never removed. This exactly mirrors the parallel, already-identified issue with `prune_inbound_counts` populated at lines 416-421 of the same function.

### Impact Explanation
Each eviction/reconnect cycle with a distinct `NeighborKey` permanently grows `self.prune_outbound_counts` on the victim node. Over a sustained low-and-slow campaign this constitutes unbounded memory growth (a slow memory-exhaustion condition) rather than a single-message crash. This does not classify as Critical under the stated categories (it is not a crash from a few messages, nor an unauthenticated write to canonical state); it is best characterized as a bounded-severity, long-running resource-degradation issue rather than an immediately exploitable Critical/High finding, because it requires the victim to repeatedly establish real outbound TCP connections to attacker-controlled endpoints (rate-limited by the peer/neighbor-walk logic) rather than being achievable via a handful of crafted messages.

### Likelihood Explanation
Exploitation requires the victim's neighbor-walk/discovery logic to actually select and connect outbound to the attacker's advertised addresses repeatedly, get them evicted via org-dominance pruning, and reconnect with a new port — each cycle is throttled by the existing connection/walk pacing, not by attacker message volume alone. This bounds the growth rate significantly and requires sustained, long-lived attacker activity and favorable org/neighbor-table conditions on the victim (attacker's org must become "dominant" relative to `soft_max_neighbors_per_org`), making this a lower-likelihood, slow-burn issue rather than a trivially repeatable exploit.

### Recommendation
Bound `prune_outbound_counts` (and `prune_inbound_counts`) by capping their size (e.g., LRU eviction) or keying/aggregating by a stable identifier less trivially variable than the full `NeighborKey` (e.g., by IP/org only), and periodically expire stale entries.

### Proof of Concept
A Rust unit test in `stackslib/src/net/prune.rs` or `tests/convergence.rs` can construct a `PeerNetwork` with `soft_max_neighbors_per_org` set low, register many outbound "convos" for the same org but with incrementing port values, repeatedly call `prune_frontier` in a loop while re-adding a new outbound peer with the next port each iteration, and assert that `self.prune_outbound_counts.len()` grows linearly with iteration count without any cap, demonstrating unbounded map growth analogous to the known `prune_inbound_counts` issue.

### Citations

**File:** stackslib/src/net/prune.rs (L58-61)
```rust
                    let nk = convo.to_neighbor_key();
                    let peer_opt =
                        PeerDB::get_peer(peer_dbconn, nk.network_id, &nk.addrbytes, nk.port)
                            .map_err(net_error::DBError)?;
```

**File:** stackslib/src/net/prune.rs (L207-244)
```rust
        for org in orgs.iter() {
            match org_neighbors.get_mut(org) {
                None => {}
                Some(ref mut neighbor_infos) => {
                    if neighbor_infos.len() as u64 > self.connection_opts.soft_max_neighbors_per_org
                    {
                        debug!(
                            "Org {} has {} neighbors (more than {} soft limit)",
                            org,
                            neighbor_infos.len(),
                            self.connection_opts.soft_max_neighbors_per_org
                        );
                        let prune_count = neighbor_infos.len().saturating_sub(
                            self.connection_opts.soft_max_neighbors_per_org as usize,
                        );
                        let mut removed_count = 0;
                        for neighbor_info in neighbor_infos.iter().take(prune_count) {
                            let (neighbor_key, _) = neighbor_info.clone();

                            debug!(
                                "{:?}: Prune {:?} because its org ({}) dominates our peer table",
                                &self.local_peer, &neighbor_key, org
                            );

                            ret.push((neighbor_key, DropReason::OrgDominatesPeerTable));
                            removed_count += 1;

                            // don't prune too many
                            if num_outbound - (ret.len() as u64)
                                <= self.connection_opts.soft_num_neighbors
                            {
                                break;
                            }
                        }
                        for _ in 0..removed_count {
                            neighbor_infos.remove(0);
                        }
                    }
```

**File:** stackslib/src/net/prune.rs (L440-448)
```rust
            self.deregister_neighbor(key, reason.clone(), DropSource::PeerNetwork);

            if !self.prune_outbound_counts.contains_key(key) {
                self.prune_outbound_counts.insert(key.clone(), 1);
            } else {
                let c = self.prune_outbound_counts.get(key).unwrap().to_owned();
                self.prune_outbound_counts.insert(key.clone(), c + 1);
            }
        }
```
