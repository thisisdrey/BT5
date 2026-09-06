### Title
Peer-controlled empty `GetTenureBlocks` responses permanently wedge a confirmed tenure download without failover - (File: stackslib/src/net/download/nakamoto/tenure_downloader_set.rs)

### Summary
`NakamotoTenureDownloader::try_accept_tenure_blocks` treats an empty block list as a no-op that leaves the `GetTenureBlocks` cursor unchanged, and its caller `handle_next_download_response` unconditionally sets `idle = true` and returns `Ok(None)` without signaling failure. `NakamotoTenureDownloaderSet::run` treats this `Ok(None)` as "still working" (`continue`), never invoking `mark_failed_and_deprioritize_peer`/`add_dead`, so the malicious peer keeps its exclusive claim on that tenure's downloader slot forever via `has_downloader_for_tenure`, which blocks any other candidate peer from being tried for the same `ConsensusHash`.

### Finding Description
The relevant state machine is `NakamotoTenureDownloadState::GetTenureBlocks(block_cursor, start_request_time)`. When a response arrives, `handle_next_download_response` (stackslib/src/net/download/nakamoto/tenure_downloader.rs:788-798, 802-803) decodes the tenure body and calls `try_accept_tenure_blocks`, then unconditionally sets `self.idle = true` regardless of the outcome: [1](#0-0) 

`try_accept_tenure_blocks` explicitly short-circuits on an empty vector, leaving `self.state` (and therefore the download cursor) untouched: [2](#0-1) 

Back in `NakamotoTenureDownloaderSet::run`, the `Ok(None)` case is handled with a plain `continue` — no call to `mark_failed_and_deprioritize_peer`, no `neighbor_rpc.add_dead`, unlike the `Err(e)` branch just above it: [3](#0-2) 

On the next call to `run`, since the downloader is not `is_done()` and the peer isn't dead/broken, `send_next_download_request` fires again, re-deriving the exact same request from the unchanged `GetTenureBlocks(end_block_id, ..)` cursor (stackslib/src/net/download/nakamoto/tenure_downloader.rs:515-521), and resets `self.idle = false` when it sends: [4](#0-3) 

Crucially, `make_tenure_downloaders` refuses to create a second downloader for a tenure that already has one assigned, regardless of whether that downloader is stalled: [5](#0-4) 

An attacker only needs to (1) advertise via inv gossip that it holds tenure X so it lands in `available_tenures`, (2) be selected as the peer for tenure X's downloader (peer selection is `neighbors.pop()`, LIFO, so a freshly-added malicious peer is favored), and then (3) reply with HTTP 200 and an empty tenure body to every `/v3/tenure/<block_id>` request. Because the response is well-formed and returns promptly, the connection is never flagged dead/broken by `neighbor_rpc`, and because the code path is `Ok(None)` (not `Err`), no failure/deprioritization bookkeeping ever fires. The tenure's `ConsensusHash` is already popped from `tenure_download_schedule` at assignment time, so no other peer is ever retried for that specific tenure while this downloader instance exists.

### Impact Explanation
The confirmed-tenure downloader slot for tenure X is wedged indefinitely on the malicious peer, and because it is never marked failed, no other of the honest peers holding the canonical tenure data is ever substituted in. Since Nakamoto tenure downloads are chained (later tenures' processing depends on earlier ones being fully fetched and stored), this can permanently stall the victim node's ability to advance its confirmed chain download for that historical/queued tenure, using only trivial, repeatable, low-cost 200-OK/empty-body responses from a single connection (not bandwidth flooding). This is a liveness denial-of-service against a legitimate, unprivileged remote peer connection — it prevents the node from ever completing sync of that tenure via the confirmed downloader path.

### Likelihood Explanation
Preconditions are modest: the attacker must run a normal peer that gossips/serves an inventory claiming it has tenure X (a standard, unprivileged P2P capability) and be selected in the peer-to-tenure assignment (`Vec::pop()`, LIFO order favors recently-added candidates, and it is trivially retryable across reconnects/several offered candidate addresses to eventually land the assignment). Once assigned, the attacker sends only empty response bodies — no cryptographic material, no signed data, and no privileged access are required. The behavior is deterministic and fully repeatable per the code paths shown, and it requires no exploitation of memory-safety or protocol parsing bugs.

### Recommendation
Treat repeated empty `GetTenureBlocks` responses (or any response that does not advance the `GetTenureBlocks` cursor) as a failure condition: track an attempt/staleness counter tied to `start_request_time` or a "no-progress" counter in `NakamotoTenureDownloader`, and once a bound is exceeded, return an `Err` (or otherwise signal failure) from `handle_next_download_response` so that `NakamotoTenureDownloaderSet::run` calls `mark_failed_and_deprioritize_peer`/`add_dead` and re-adds the tenure back into `tenure_download_schedule` so it can be retried against another `available_tenures` entry.

### Proof of Concept
Rust test plan (extending `stackslib/src/net/tests/download/nakamoto.rs` patterns using `NakamotoTenureDownloaderSet`):
1. Construct a `NakamotoTenureDownloaderSet` and a `tenure_block_ids`/`available` map where tenure `ch` is served by two `NeighborAddress`es: `honest_peer` and `evil_peer`.
2. Call `make_tenure_downloaders` so that `evil_peer` (pushed last, so popped first) is assigned to tenure `ch`; assert `downloaders.has_downloader_for_tenure(&ch)` is true and it is bound to `evil_peer`.
3. Drive `run()` in a loop, using a mocked `NeighborRPC`/`PeerNetwork` where `evil_peer`'s HTTP responses are always `StacksHttpResponse` wrapping an empty Nakamoto-tenure body (so `decode_nakamoto_tenure()` yields `vec![]`).
4. After N (e.g., 50) iterations, assert that: (a) `downloader.state` for `ch` is still the same `GetTenureBlocks(cursor, ..)` as at assignment (no progress), (b) `self.attempt_failed_tenures.get(&ch)` is still `0`/absent, (c) `self.deprioritized_peers.contains_key(&evil_peer)` is `false`, and (d) `honest_peer` was never invoked/assigned a downloader for `ch`. This demonstrates the perpetual reselection of the same non-cooperative peer and the absence of failover, confirming the wedge.

### Citations

**File:** stackslib/src/net/download/nakamoto/tenure_downloader.rs (L354-369)
```rust
    pub fn try_accept_tenure_blocks(
        &mut self,
        mut tenure_blocks: Vec<NakamotoBlock>,
    ) -> Result<Option<Vec<NakamotoBlock>>, NetError> {
        let NakamotoTenureDownloadState::GetTenureBlocks(block_cursor, start_request_time) =
            &self.state
        else {
            warn!("Invalid state for this method";
                  "state" => %self.state);
            return Err(NetError::InvalidState);
        };

        if tenure_blocks.is_empty() {
            // nothing to do
            return Ok(None);
        }
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader.rs (L738-751)
```rust
        let request = match self.make_next_download_request(peerhost) {
            Ok(Some(request)) => request,
            Ok(None) => {
                return Ok(true);
            }
            Err(_) => {
                return Ok(false);
            }
        };

        neighbor_rpc.send_request(network, self.naddr.clone(), request)?;
        self.idle = false;
        Ok(true)
    }
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader.rs (L788-803)
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

**File:** stackslib/src/net/download/nakamoto/tenure_downloader_set.rs (L611-647)
```rust
        // handle responses
        for (naddr, response) in neighbor_rpc.collect_replies(network) {
            let Some(index) = self.peers.get(&naddr) else {
                debug!("No downloader for {naddr}");
                continue;
            };
            let Some(Some(downloader)) = self.downloaders.get_mut(*index) else {
                debug!("No downloader for {naddr}");
                continue;
            };
            debug!("Got response from {naddr}");

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
