### Title
Non-responding peers are never deprioritized in tenure download scheduling, allowing a dishonest peer to be repeatedly re-selected and stall canonical tenure fetches - (File: stackslib/src/net/download/nakamoto/tenure_downloader_set.rs)

### Summary
`NakamotoTenureDownloaderSet::run` only calls `mark_failed_and_deprioritize_peer` when `send_next_download_request`/`handle_next_download_response` return an explicit `Err`, but a peer that silently never responds is instead detected via `neighbor_rpc.is_dead_or_broken` and simply cleared with `clear_downloader`, bypassing deprioritization entirely. Because `make_tenure_downloaders` only checks `self.deprioritized_peers` before assigning a peer, a peer that accepts a connection/request but never replies can be re-selected for the same tenure (or others it falsely advertises) on every subsequent scheduling round, indefinitely delaying progress toward an honest peer.

### Finding Description
The scheduling loop pops a candidate neighbor from `available.get_mut(ch)` and checks `self.deprioritized_peers` before assigning it a `NakamotoTenureDownloader`: [1](#0-0) 

Deprioritization is only ever set from `run()` when an explicit RPC error occurs: [2](#0-1) [3](#0-2) 

However, connections that go dead/broken purely due to timeout (a peer that accepts the request and simply never answers) are handled by a separate branch that only clears the downloader, without calling `mark_failed_and_deprioritize_peer`: [4](#0-3) [5](#0-4) 

Because `self.peers` maps a `NeighborAddress` to a single downloader slot, a malicious peer cannot occupy multiple slots simultaneously (`add_downloader` reuses the existing slot for a known peer), so the "many ConsensusHashes at once" framing in the question is not accurate — the peer only ever drives one downloader at a time. But since a silent-timeout peer is never inserted into `deprioritized_peers`, once its slot is cleared it remains immediately eligible again the next time `make_tenure_downloaders` runs with a refreshed `schedule`/`available` (which will still include it, since its advertised inventory bit for the tenure is unchanged). This lets a single non-responding peer occupy one of the bounded `count` concurrent downloader slots on every scheduling round, without ever tripping the `PEER_DEPRIORITIZATION_TIME_SECS` cooldown, each time incrementing `attempted_tenures` for `ch`: [6](#0-5) 

### Impact Explanation
An unprivileged remote peer can advertise (via inventory/tenure metadata) that it holds a tenure, accept the resulting HTTP-over-P2P download request, and never respond. This peer will keep being handed one of the `count` concurrent downloader slots on every re-scheduling round because the failure path for pure timeouts does not deprioritize it, unlike explicit RPC errors. This delays fetching the canonical tenure data from a legitimate/honest peer, matching a "High - availability stall" bounded-compute/IO impact: no data is corrupted or forged, but the download scheduler wastes slots/attempts on a non-serving peer instead of the caps working as intended (`PEER_DEPRIORITIZATION_TIME_SECS` cooldown never applies to this class of failure).

### Likelihood Explanation
Preconditions are low-cost and fully within reach of an unprivileged remote actor: run a peer that connects, advertises tenure inventory it doesn't actually serve, and simply drops/ignores the resulting block-download HTTP request instead of sending a malformed response or closing the connection abruptly (either of which would route through the `Err` branches and get deprioritized). Repeated indefinitely at zero cost to the attacker, bounded only by the P2P/RPC-level request timeout, without ever hitting `PEER_DEPRIORITIZATION_TIME_SECS`.

### Recommendation
Route the `is_dead_or_broken`/timeout detection paths in `run()` (lines 596-602 and 674-680) through `Self::mark_failed_and_deprioritize_peer` as well, so that any peer whose connection goes dead/broken — whether via explicit error or via timeout — is deprioritized identically. This closes the gap that lets a silently non-responding peer bypass the cooldown entirely.

### Proof of Concept
Rust test plan (extending existing tests in `stackslib/src/net/tests/download/nakamoto.rs`, which reference `attempted_tenures`/`deprioritized_peers`):
1. Construct a `NakamotoTenureDownloaderSet`, a `schedule` containing one `ConsensusHash` `ch`, and an `available` map with two `NeighborAddress` entries for `ch`: one "dishonest" peer `naddr_bad` and one "honest" peer `naddr_good`, both with matching plausible `AvailableTenures` entries.
2. Call `make_tenure_downloaders` repeatedly across simulated rounds, driving `run()` with a mocked `NeighborRPC`/`PeerNetwork` where `naddr_bad`'s socket is opened but never produces a reply until `neighbor_rpc.is_dead_or_broken` reports it dead purely by timeout (no `Err` returned from `send_next_download_request`/`handle_next_download_response`).
3. Assert that after each round `self.deprioritized_peers.get(&naddr_bad)` remains `None` (never set), while `self.attempted_tenures.get(&ch)` keeps incrementing.
4. Assert that `naddr_good` is only assigned to `ch` after the dishonest peer has been retried `N` times, and that this delay persists beyond a simulated `PEER_DEPRIORITIZATION_TIME_SECS` window (60s) with no cooldown ever applied to `naddr_bad`, demonstrating the missing-deprioritization gap at lines `stackslib/src/net/download/nakamoto/tenure_downloader_set.rs:596-602`.

### Citations

**File:** stackslib/src/net/download/nakamoto/tenure_downloader_set.rs (L392-411)
```rust
            let Some(naddr) = neighbors.pop() else {
                debug!("No more neighbors can serve tenure {ch}");
                schedule.pop_front();
                continue;
            };
            if get_epoch_time_secs() < *self.deprioritized_peers.get(&naddr).unwrap_or(&0) {
                debug!(
                    "Peer {} is deprioritized until {naddr}",
                    self.deprioritized_peers.get(&naddr).unwrap_or(&0)
                );
                continue;
            }

            if self.try_resume_peer(naddr.clone()) {
                continue;
            };
            if self.has_downloader_for_tenure(ch) {
                schedule.pop_front();
                continue;
            }
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader_set.rs (L465-467)
```rust
            let attempt_count = *self.attempted_tenures.get(ch).unwrap_or(&0);
            self.attempted_tenures
                .insert(ch.clone(), attempt_count.saturating_add(1));
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader_set.rs (L574-593)
```rust
                Err(e) => {
                    info!(
                        "Downloader for tenure {} to {naddr} failed; this peer is dead",
                        &downloader.tenure_id_consensus_hash,
                    );
                    Self::mark_failed_and_deprioritize_peer(
                        &mut self.attempt_failed_tenures,
                        &mut self.deprioritized_peers,
                        &downloader.tenure_id_consensus_hash,
                        naddr,
                    );
                    neighbor_rpc.add_dead(
                        network,
                        naddr,
                        DropReason::DeadConnection(format!("Download request failed: {e}")),
                        DropSource::NakamotoTenureDownloader,
                    );
                    continue;
                }
            };
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader_set.rs (L596-602)
```rust
        // clear dead, broken, and done
        for naddr in addrs.iter() {
            if neighbor_rpc.is_dead_or_broken(network, naddr) {
                debug!("Remove dead/broken downloader for {naddr}");
                self.clear_downloader(naddr);
            }
        }
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader_set.rs (L626-646)
```rust
                Err(e) => {
                    info!(
                        "Failed to handle response from {naddr} on tenure {}: {e}",
                        &downloader.tenure_id_consensus_hash,
                    );
                    Self::mark_failed_and_deprioritize_peer(
                        &mut self.attempt_failed_tenures,
                        &mut self.deprioritized_peers,
                        &downloader.tenure_id_consensus_hash,
                        &naddr,
                    );
                    neighbor_rpc.add_dead(
                        network,
                        &naddr,
                        DropReason::DeadConnection(format!(
                            "Failed to handle download response: {e}"
                        )),
                        DropSource::NakamotoTenureDownloader,
                    );
                    continue;
                }
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader_set.rs (L674-680)
```rust
        // clear dead, broken, and done
        for naddr in addrs.iter() {
            if neighbor_rpc.is_dead_or_broken(network, naddr) {
                debug!("Remove dead/broken downloader for {naddr}");
                self.clear_downloader(naddr);
            }
        }
```
