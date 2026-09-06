### Title
Empty `tenure_blocks` response from a peer causes indefinite `Ok(None)` and permanent stall of `NakamotoTenureDownloader` without peer being marked dead - ([File: stackslib/src/net/download/nakamoto/tenure_downloader.rs])

### Summary
`NakamotoTenureDownloader::try_accept_tenure_blocks` returns `Ok(None)` immediately, without any state transition, whenever the decoded tenure-blocks payload is empty. Since `NakamotoTenureDownloaderSet::download_supporting_attachments`/response-handling loop in `tenure_downloader_set.rs` only treats an `Err` as cause to deprioritize/kill a peer, a peer can keep responding with an empty (but syntactically valid) tenure-blocks body forever, wedging that tenure's downloader on this peer indefinitely.

### Finding Description
`try_accept_tenure_blocks` starts with: [1](#0-0) 
which unconditionally returns `Ok(None)` for an empty block list, without validating the cursor, without erroring, and without advancing `self.state` from `GetTenureBlocks(block_cursor, ...)`. `handle_next_download_response` propagates this `Ok(None)` straight through: [2](#0-1) 
and marks the downloader `idle = true`, allowing `send_next_download_request` to immediately re-issue the identical request for the same unchanged `end_block_id` cursor: [3](#0-2) 

In the scheduling loop in `tenure_downloader_set.rs`, the response-handling `match` only escalates to `mark_failed_and_deprioritize_peer`/`add_dead` on `Err`; `Ok(None)` simply `continue`s to the next response without any bookkeeping: [4](#0-3) 
The downloader is only cleared from its slot when it is dead/broken (via `neighbor_rpc.is_dead_or_broken`) or `finished` (i.e., `is_done()` is true): [5](#0-4) 
Neither condition is ever reached if the peer keeps answering with an empty tenure-blocks body: no error is raised (so it's not marked dead), and `is_done()` never becomes true (state never reaches `Done` because the cursor never advances).

This differs from the exact-duplicate-block case: if a peer resends a specific already-consumed block, `try_accept_tenure_blocks`'s cursor check (`block.header.block_id() != expected_block_id`) correctly rejects it with `Err(NetError::InvalidMessage)`, triggering the dead/deprioritize path: [6](#0-5) 
The gap is specifically the empty-list short-circuit at line 366-369, which bypasses all cursor/signature validation and returns a "successful, no-op" `Ok(None)`.

### Impact Explanation
A malicious peer that is assigned a tenure downloader can respond to every `GetTenureBlocks` request with an HTTP 200 response that decodes to zero blocks. Each such reply is treated as legitimate, non-erroring progress, so the peer is never deprioritized or marked dead, and the downloader for that tenure is never cleared/reassigned to another neighbor. As long as this peer keeps being selected/assigned for that tenure's downloader slot, the node cannot make forward progress fetching that tenure, even if other honest peers could serve it — a liveness/DoS on the tenure-download pipeline that requires only a handful of low-cost, well-formed responses to sustain indefinitely.

### Likelihood Explanation
Preconditions: attacker just needs to run/control an ordinary P2P peer that gets selected by the victim node as a source for a given tenure (this is the normal, unprivileged peer-selection path — no secret, no signer role, no StackerDB privilege needed). The attacker only needs a functioning HTTP responder for the `GET tenure` endpoint that returns an empty (but valid) body; cost per message is trivial and the behavior is fully repeatable across polling passes since the state machine's cursor never advances.

### Recommendation
Treat an empty `tenure_blocks` response in `GetTenureBlocks` state as an anomalous/invalid condition (or track and cap the number of consecutive no-progress responses per peer/tenure), returning `Err(NetError::InvalidMessage)` (or otherwise flagging/deprioritizing the peer) once a bound is exceeded, so that `tenure_downloader_set.rs`'s existing `mark_failed_and_deprioritize_peer`/`add_dead` path is triggered and the tenure can be reassigned to another neighbor.

### Proof of Concept
Rust test plan in `stackslib/src/net/download/nakamoto/tenure_downloader.rs` (or the `tenure_downloader_set` test module):
1. Construct a `NakamotoTenureDownloader` in state `GetTenureBlocks(end_block_id, t0)` with a known `tenure_end_block`/`tenure_length`.
2. Craft a `StacksHttpResponse` whose `decode_nakamoto_tenure()` yields an empty `Vec<NakamotoBlock>`.
3. Call `handle_next_download_response` N times with this same crafted response; assert every call returns `Ok(None)`, `self.state` remains `GetTenureBlocks(end_block_id, _)` unchanged (cursor identical), and `is_done()` is `false` after all N calls.
4. At the `NakamotoTenureDownloaderSet` level, wrap this downloader with a mock `NeighborRPC`/`PeerNetwork` that always returns the same empty-block response for the assigned peer; run the polling loop (`download_state_machine`) N times and assert the peer is never passed to `add_dead`/`mark_failed_and_deprioritize_peer`, the downloader slot for that tenure is never cleared, and `completed_tenures` never contains the tenure — demonstrating the stall.

### Citations

**File:** stackslib/src/net/download/nakamoto/tenure_downloader.rs (L366-369)
```rust
        if tenure_blocks.is_empty() {
            // nothing to do
            return Ok(None);
        }
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader.rs (L386-393)
```rust

            if &block.header.block_id() != expected_block_id {
                warn!("Unexpected Nakamoto block -- does not match cursor";
                      "expected_block_id" => %expected_block_id,
                      "block_id" => %block.header.block_id(),
                      "state" => %self.state);
                return Err(NetError::InvalidMessage);
            }
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader.rs (L708-751)
```rust
    pub fn send_next_download_request(
        &mut self,
        network: &mut PeerNetwork,
        neighbor_rpc: &mut NeighborRPC,
    ) -> Result<bool, NetError> {
        if neighbor_rpc.has_inflight(&self.naddr) {
            debug!("Peer {} has an inflight request", &self.naddr);
            return Ok(true);
        }
        if neighbor_rpc.is_dead_or_broken(network, &self.naddr) {
            return Err(NetError::PeerNotConnected(format!(
                "Failed to send next download request to {:?}: connection is dead or broken",
                &self.naddr
            )));
        }

        let Some(peerhost) = NeighborRPC::get_peer_host(network, &self.naddr) else {
            // no conversation open to this neighbor
            neighbor_rpc.add_dead(
                network,
                &self.naddr,
                DropReason::DeadConnection("No authenticated connection open".into()),
                DropSource::NakamotoTenureDownloader,
            );
            return Err(NetError::PeerNotConnected(format!(
                "No authenticated connection open to {:?} for tenure download",
                &self.naddr
            )));
        };

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

**File:** stackslib/src/net/download/nakamoto/tenure_downloader_set.rs (L596-609)
```rust
        // clear dead, broken, and done
        for naddr in addrs.iter() {
            if neighbor_rpc.is_dead_or_broken(network, naddr) {
                debug!("Remove dead/broken downloader for {naddr}");
                self.clear_downloader(naddr);
            }
        }
        for done_naddr in finished.drain(..) {
            debug!("Remove finished downloader for {done_naddr}");
            self.clear_downloader(&done_naddr);
        }
        for done_tenure in finished_tenures.drain(..) {
            self.completed_tenures.insert(done_tenure);
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
