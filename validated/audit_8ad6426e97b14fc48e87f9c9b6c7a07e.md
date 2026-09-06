### Title
Empty `GetTenureBlocks` response causes unbounded re-request to the same unhelpful peer without failure/deprioritization - ([File: stackslib/src/net/download/nakamoto/tenure_downloader.rs])

### Summary
A remote peer that is the sole provider of a tenure `CH` can respond to every `GetTenureBlocks` request with an empty block list. `try_accept_tenure_blocks` treats an empty vector as "nothing to do" and returns `Ok(None)` without advancing the block cursor or erroring, so `NakamotoTenureDownloaderSet::run` never calls `mark_failed_and_deprioritize_peer` for this tenure. The downloader is simply marked idle and re-scheduled to the same peer, requesting the same `GetTenureBlocks(block_cursor, ...)` forever.

### Finding Description
In `try_accept_tenure_blocks` [1](#0-0) , an empty `tenure_blocks` vector short-circuits with `Ok(None)` and does **not** update `self.state`'s cursor (`GetTenureBlocks(block_cursor, ...)` is left unchanged). Compare this to the non-empty "need more" path, which does advance the cursor [2](#0-1) .

`handle_next_download_response` unconditionally sets `self.idle = true` after processing any response, including the empty-blocks `Ok(None)` case [3](#0-2) .

In `NakamotoTenureDownloaderSet::run`, the response-handling loop only calls `Self::mark_failed_and_deprioritize_peer` on an `Err` from `handle_next_download_response`; the `Ok(None)` branch simply `continue`s with no bookkeeping at all [4](#0-3) . Since `attempt_failed_tenures` is only incremented in `mark_failure`, called exclusively from `mark_failed_and_deprioritize_peer` [5](#0-4) , no failure count is ever recorded for a peer that repeatedly answers "empty."

On the next scheduling pass, the peer (marked idle, not dead/broken, not deprioritized) is reattached to the *same* downloader via `try_resume_peer`, which just looks for any idle downloader and reassigns the peer to it [6](#0-5) ; if it's the only peer known to serve `ch` per `available_tenures`, `make_tenure_downloaders` has no alternative peer to substitute (`has_downloader_for_tenure` short-circuits reassignment) [7](#0-6) . `make_next_download_request` then reissues the identical `GetTenureBlocks(end_block_id, ...)` request since the state was never advanced [8](#0-7) . There is no timeout check based on `start_request_time` anywhere in `run`/`tenure_downloader_set.rs` that would eventually force a failure — the `start_request_time` field is only used for debug logging, not enforcement.

This confirms the fault as described: the number of `run` passes before this tenure is marked failed/deprioritized/rescheduled is unbounded, because an `Ok(None)` outcome from an all-empty response is indistinguishable from legitimate "waiting for chainstate" progress and never touches `attempt_failed_tenures` or `deprioritized_peers`.

### Impact Explanation
A single hostile/unhelpful peer that is the (or the last remaining) advertised source for a specific historic tenure can indefinitely stall that tenure's downloader by returning empty tenure-block payloads. Because tenure downloaders are chained (the N+1'st tenure's start-block feeds the Nth tenure's downloader), stalling one tenure download can stall the confirmed-tenure download pipeline for that peer/schedule slot, preventing the node from completing sync/catch-up for that tenure indefinitely, without the peer ever being flagged as dead, broken, or deprioritized. This is a liveness/DoS condition on the block-download subsystem, reachable by any unprivileged remote peer who is selected as a tenure's block-server, at zero cost (a single tiny message repeated).

### Likelihood Explanation
Preconditions: the attacker must be selected/known (via `available_tenures`) as a server of the target tenure — this requires no privileged role, merely being connected as a peer and advertising an inventory that names them as a holder of tenure `CH`. The attack cost is negligible: return an empty (but well-formed, HTTP 200) tenure-blocks response to every `GetTenureBlocks` request. It is fully repeatable and requires no signature forgery, no secret, and no additional peer compromise. The main limiting factor is that another honest peer serving the same tenure would allow `make_tenure_downloaders` to eventually pick a different peer if the schedule advances — but as stated in the question, this specific downloader is never marked failed/rescheduled, only externally-driven re-scheduling (e.g., new schedule entries) could rescue it, which is not guaranteed in the described sole-peer scenario.

### Recommendation
Treat repeated empty `GetTenureBlocks` responses as a failure signal rather than "no-op" success. Specifically, in `try_accept_tenure_blocks`, either (a) track a per-request "no progress" counter and return `Err(NetError::InvalidMessage)` (or a stall-specific error) once an empty response is received for a given cursor a bounded number of times, or (b) have `NakamotoTenureDownloaderSet::run` inspect whether `handle_next_download_response` returned `Ok(None)` with an unchanged state/cursor for the same peer more than N times, and in that case call `mark_failed_and_deprioritize_peer` to force rescheduling to another peer/backoff.

### Proof of Concept
Add a net test in `stackslib/src/net/tests/download/nakamoto.rs`:
1. Construct a `NakamotoTenureDownloaderSet` with a single downloader in `GetTenureBlocks(end_block_id, t0)` state, assigned to a mock peer `naddr`.
2. Loop `run_downloads`/`run()` N=1000 times, each time synthesizing a `StacksHttpResponse` for `GetTenureBlocks` whose decoded body is an empty `Vec<NakamotoBlock>`.
3. After each iteration, assert:
   - `downloader.state` is still `GetTenureBlocks(end_block_id, t0)` (cursor never advances),
   - `self.attempt_failed_tenures.get(&ch)` is `None`/`0` for all 1000 iterations,
   - `self.deprioritized_peers.get(&naddr)` is `None`.
4. Assert the loop completes 1000 iterations without ever transitioning the tenure to failed/deprioritized/reassigned, demonstrating the unbounded stall at `tenure_downloader.rs:366-369` / `tenure_downloader_set.rs:623-625`.

### Citations

**File:** stackslib/src/net/download/nakamoto/tenure_downloader.rs (L366-369)
```rust
        if tenure_blocks.is_empty() {
            // nothing to do
            return Ok(None);
        }
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader.rs (L462-475)
```rust
        if earliest_block.block_id() != tenure_start_block.block_id() {
            // still have more blocks to download
            let next_block_id = earliest_block.header.parent_block_id.clone();
            debug!(
                "Need more blocks for tenure {} (went from {} to {}, next is {})",
                &self.tenure_id_consensus_hash,
                &block_cursor,
                &earliest_block.block_id(),
                &next_block_id
            );
            self.state =
                NakamotoTenureDownloadState::GetTenureBlocks(next_block_id, *start_request_time);
            return Ok(None);
        }
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader.rs (L515-521)
```rust
            NakamotoTenureDownloadState::GetTenureBlocks(end_block_id, start_request_time) => {
                debug!(
                    "Downloading tenure ending at {} at {}",
                    &end_block_id, start_request_time
                );
                StacksHttpRequest::new_get_nakamoto_tenure(peerhost, end_block_id.clone(), None)
            }
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader.rs (L788-804)
```rust
            NakamotoTenureDownloadState::GetTenureBlocks(end_block_id, start_request_time) => {
                debug!(
                    "Got download response for tenure blocks ending at {} in {}ms",
                    &end_block_id,
                    get_epoch_time_ms().saturating_sub(*start_request_time)
                );
                let blocks = response.decode_nakamoto_tenure().inspect_err(|e| {
                    warn!("Failed to decode response for a Nakamoto tenure: {e:?}")
                })?;
                let blocks_opt = self.try_accept_tenure_blocks(blocks)?;
                Ok(blocks_opt)
            }
            NakamotoTenureDownloadState::Done => Err(NetError::InvalidState),
        };
        self.idle = true;
        handle_result
    }
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader_set.rs (L100-129)
```rust
    fn mark_failure(attempt_failed_tenures: &mut HashMap<ConsensusHash, u64>, ch: &ConsensusHash) {
        if let Some(failures) = attempt_failed_tenures.get_mut(ch) {
            *failures = (*failures).saturating_add(1);
        } else {
            attempt_failed_tenures.insert(ch.clone(), 1);
        }
    }

    /// Mark a peer as deprioritized
    /// Implemented statically to appease the borrow checker.
    fn mark_deprioritized(
        deprioritized_peers: &mut HashMap<NeighborAddress, u64>,
        peer: &NeighborAddress,
    ) {
        deprioritized_peers.insert(
            peer.clone(),
            get_epoch_time_secs() + PEER_DEPRIORITIZATION_TIME_SECS,
        );
    }

    /// Mark a peer and its tenure as dead and failed
    fn mark_failed_and_deprioritize_peer(
        attempted_failed_tenures: &mut HashMap<ConsensusHash, u64>,
        deprioritized_peers: &mut HashMap<NeighborAddress, u64>,
        ch: &ConsensusHash,
        peer: &NeighborAddress,
    ) {
        Self::mark_failure(attempted_failed_tenures, ch);
        Self::mark_deprioritized(deprioritized_peers, peer);
    }
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader_set.rs (L246-275)
```rust
    pub fn try_resume_peer(&mut self, naddr: NeighborAddress) -> bool {
        debug!("Try resume {}", &naddr);
        if let Some(idx) = self.peers.get(&naddr) {
            let Some(Some(_downloader)) = self.downloaders.get(*idx) else {
                return false;
            };

            debug!(
                "Peer {naddr} already bound to downloader for {}",
                &_downloader.tenure_id_consensus_hash
            );
            return true;
        }
        for (i, downloader_opt) in self.downloaders.iter_mut().enumerate() {
            let Some(downloader) = downloader_opt else {
                continue;
            };
            if !downloader.idle {
                continue;
            }
            debug!(
                "Assign peer {naddr} to work on downloader for {} in state {}",
                &downloader.tenure_id_consensus_hash, &downloader.state
            );
            downloader.naddr = naddr.clone();
            self.peers.insert(naddr, i);
            return true;
        }
        return false;
    }
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader_set.rs (L405-411)
```rust
            if self.try_resume_peer(naddr.clone()) {
                continue;
            };
            if self.has_downloader_for_tenure(ch) {
                schedule.pop_front();
                continue;
            }
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader_set.rs (L623-647)
```rust
            let blocks = match downloader.handle_next_download_response(response) {
                Ok(Some(blocks)) => blocks,
                Ok(None) => continue,
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
            };
```
