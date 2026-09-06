### Title
Malicious peer can indefinitely stall an unconfirmed-tenure download slot by always answering with an empty block list - ([File: stackslib/src/net/download/nakamoto/tenure_downloader_unconfirmed.rs])

### Summary
`try_accept_unconfirmed_tenure_blocks` treats an empty `tenure_blocks` vector as a no-op (`Ok(None)`) without changing `self.state`, so `send_next_download_request` will keep re-issuing the exact same `GetUnconfirmedTenureBlocks` request to the same peer forever. A remote peer that always answers `GET /v3/tenures/:id` with a zero-length body can keep this state machine's slot for that peer permanently stuck in the `GetUnconfirmedTenureBlocks` state.

### Finding Description
In `try_accept_unconfirmed_tenure_blocks`, when `tenure_blocks.is_empty()` the function returns `Ok(None)` immediately at [1](#0-0)  without touching `self.state`, which remains `GetUnconfirmedTenureBlocks(last_block_id)`. `handle_next_download_response` simply forwards this `Ok(None)` result up the call chain [2](#0-1) . Since `make_next_download_request`/`send_next_download_request` only look at `self.state` to build the next request [3](#0-2) [4](#0-3) , an unchanged state means the identical request (same `tip_block_id`) is resent to the same peer on every subsequent scheduling pass in `run_unconfirmed_downloaders` [5](#0-4) .

Nothing in this response path marks the peer dead, deprioritizes it, or raises an error: `Ok(None)` is treated as "still working, call again" per the function's own doc comment [6](#0-5) , and the caller in `run_unconfirmed_downloaders` only removes/penalizes downloaders on `Err` or on `is_dead_or_broken`/finished, not on repeated empty-but-`Ok` responses [7](#0-6) .

### Impact Explanation
This affects only the per-peer downloader entry keyed by `NeighborAddress` in `unconfirmed_tenure_downloads: HashMap<NeighborAddress, NakamotoUnconfirmedTenureDownloader>`; other peers get their own independent downloader entries and are unaffected, so the node's overall unconfirmed-tenure sync is not blocked as long as other honest peers serve real data. The concrete effect is that one peer connection's download slot is wasted indefinitely with request/response round trips that cost the victim node one HTTP request parse and a handful of function calls per response — a bounded, low-cost compute/slot-occupation issue rather than a crash or state corruption. No forged data is stored, no canonical state is corrupted, and no unauthenticated write occurs.

### Likelihood Explanation
Preconditions: the attacker peer only needs to be a normal, unprivileged, connectable P2P peer that the victim node has selected to try unconfirmed-tenure downloads from, and it must keep answering `GET /v3/tenures/:id` with `200 OK` and an empty body. Cost to the attacker is trivial (one static empty response repeated). This is fully remotely reachable with no special role, and the condition is trivially repeatable for as long as the peer connection remains open and is not otherwise dropped by unrelated liveness/timeout mechanisms elsewhere in the P2P layer (which were not evaluated here).

### Recommendation
Treat repeated empty `tenure_blocks` responses in the `GetUnconfirmedTenureBlocks` state as a failure/stall condition: e.g., count consecutive empty responses per downloader and return `Err(NetError::InvalidMessage)` (or similar) after a small threshold so the peer is marked dead/deprioritized and the slot is freed for another peer, mirroring the existing dead/broken/deprioritization handling used elsewhere in `NakamotoTenureDownloaderSet`.

### Proof of Concept
Rust unit test in `tenure_downloader_unconfirmed.rs` test module (or `net/tests/download/nakamoto.rs`):
1. Construct a `NakamotoUnconfirmedTenureDownloader` and drive it via `try_accept_tenure_info`/`try_accept_unconfirmed_tenure_start_block` into `GetUnconfirmedTenureBlocks(tip_block_id)` state.
2. Call `try_accept_unconfirmed_tenure_blocks(vec![])` N times (e.g., N=1000).
3. Assert `matches!(downloader.state, NakamotoUnconfirmedDownloadState::GetUnconfirmedTenureBlocks(id) if id == tip_block_id)` still holds after all N calls, and that every call returned `Ok(None)` — confirming no forward progress, no error, and no state transition ever occurs, so `make_next_download_request` keeps producing the identical request forever.

### Citations

**File:** stackslib/src/net/download/nakamoto/tenure_downloader_unconfirmed.rs (L486-490)
```rust
        if tenure_blocks.is_empty() {
            // nothing to do
            debug!("No tenure blocks obtained");
            return Ok(None);
        }
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader_unconfirmed.rs (L776-782)
```rust
            NakamotoUnconfirmedDownloadState::GetUnconfirmedTenureBlocks(tip_block_id) => {
                return Some(StacksHttpRequest::new_get_nakamoto_tenure(
                    peerhost,
                    tip_block_id.clone(),
                    self.highest_processed_block_id.clone(),
                ));
            }
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader_unconfirmed.rs (L835-871)
```rust
    pub fn send_next_download_request(
        &self,
        network: &mut PeerNetwork,
        neighbor_rpc: &mut NeighborRPC,
    ) -> Result<(), NetError> {
        if neighbor_rpc.has_inflight(&self.naddr) {
            debug!("Peer {} has an inflight request", &self.naddr);
            return Ok(());
        }
        if neighbor_rpc.is_dead_or_broken(network, &self.naddr) {
            return Err(NetError::PeerNotConnected(format!("Failed to send next unconfirmed download request to {:?}: connection is dead or broken", &self.naddr)));
        }

        let Some(peerhost) = NeighborRPC::get_peer_host(network, &self.naddr) else {
            // no conversation open to this neighbor
            neighbor_rpc.add_dead(
                network,
                &self.naddr,
                DropReason::DeadConnection("No authenticated connection open".into()),
                DropSource::NakamotoUnconfirmedTenureDownloader,
            );
            return Err(NetError::PeerNotConnected(format!(
                "No authenticated connection open to {:?} for unconfirmed tenure download",
                &self.naddr
            )));
        };

        let Some(request) = self.make_next_download_request(peerhost) else {
            // treat this downloader as still in-flight since the overall state machine will need
            // to keep it around long enough to convert it into a tenure downloader for the highest
            // complete tenure.
            return Ok(());
        };

        neighbor_rpc.send_request(network, self.naddr.clone(), request)?;
        Ok(())
    }
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader_unconfirmed.rs (L876-879)
```rust
    /// Returns Ok(Some(blocks)) if we finished downloading the unconfirmed tenure
    /// Returns Ok(None) if we're still working, in which case the caller should call
    /// `send_next_download_request()`
    /// Returns Err(..) on unrecoverable failure to advance state
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader_unconfirmed.rs (L908-914)
```rust
            NakamotoUnconfirmedDownloadState::GetUnconfirmedTenureBlocks(..) => {
                debug!("Got unconfirmed tenure blocks response");
                let blocks = response.decode_nakamoto_tenure()?;
                let accepted_opt = self.try_accept_unconfirmed_tenure_blocks(blocks)?;
                debug!("Got unconfirmed tenure blocks"; "complete" => accepted_opt.is_some());
                Ok(accepted_opt)
            }
```

**File:** stackslib/src/net/download/nakamoto/download_state_machine.rs (L1197-1216)
```rust
            debug!(
                "Send request to {} for tenure {:?} (state {})",
                &naddr,
                &downloader.unconfirmed_tenure_id(),
                &downloader.state
            );
            if let Err(e) = downloader.send_next_download_request(network, neighbor_rpc) {
                debug!(
                    "Downloader for {} failed; this peer is dead: {:?}",
                    &naddr, &e
                );
                neighbor_rpc.add_dead(
                    network,
                    naddr,
                    DropReason::DeadConnection(format!("Failed to send download request: {e}")),
                    DropSource::NakamotoDownloadStateMachine,
                );
                continue;
            };
        }
```

**File:** stackslib/src/net/download/nakamoto/download_state_machine.rs (L1236-1265)
```rust
            let blocks_opt = match downloader.handle_next_download_response(
                response,
                sortdb,
                sort_tip,
                chainstate,
                &network.current_reward_sets,
            ) {
                Ok(blocks_opt) => blocks_opt,
                Err(NetError::StaleView) => {
                    neighbor_rpc.add_dead(
                        network,
                        &naddr,
                        DropReason::DeadConnection("Stale view".into()),
                        DropSource::NakamotoDownloadStateMachine,
                    );
                    continue;
                }
                Err(e) => {
                    debug!("Failed to handle next download response from unconfirmed downloader for {:?} in state {:?}: {:?}", &naddr, &downloader.state, &e);
                    neighbor_rpc.add_dead(
                        network,
                        &naddr,
                        DropReason::DeadConnection(format!(
                            "Failed to handle next download response: {e}"
                        )),
                        DropSource::NakamotoDownloadStateMachine,
                    );
                    continue;
                }
            };
```
