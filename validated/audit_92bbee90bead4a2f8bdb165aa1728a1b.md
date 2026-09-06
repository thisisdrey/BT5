### Title
Malicious peer can permanently stall `NakamotoTenureDownloader::try_accept_tenure_blocks` by never delivering the tenure-start block, wedging the tenure download state machine forever - ([File: stackslib/src/net/download/nakamoto/tenure_downloader.rs])

### Summary
`try_accept_tenure_blocks` only advances to `Done` when `earliest_block.block_id() == tenure_start_block.block_id()`, but a malicious peer serving the `GetTenureBlocks` batch endpoint can respond with an empty block list (or a valid, non-terminal prefix) forever, causing an early `return Ok(None)` with no state change and no error. Because no timeout is ever enforced on the `GetTenureBlocks` state, the downloader (and the peer bound to it in `NakamotoTenureDownloaderSet`) is wedged on that tenure indefinitely, with no fallback to another peer.

### Finding Description
The state machine records the tenure-start block ID up front (fetched separately via `GetTenureStartBlock`, see [1](#0-0) ) and stores it in `self.tenure_start_block`. It then enters `GetTenureBlocks(cursor, start_request_time)` and repeatedly issues `new_get_nakamoto_tenure` requests, expecting to eventually receive a batch whose earliest block equals that pre-known tenure-start block: [2](#0-1) [3](#0-2) 

A malicious peer that owns the `GetTenureBlocks` HTTP handler can simply return an empty `Vec<NakamotoBlock>` (or any valid non-terminal prefix that never contains the block whose `block_id()` equals `tenure_start_block_id`). When `tenure_blocks.is_empty()`, the function returns `Ok(None)` immediately, before `self.state` is touched at all — the cursor and `start_request_time` are unchanged. When a non-empty but non-terminal batch is returned, all per-block checks (`consensus_hash`, cursor match, `verify_signer_signatures`, and the length cap in lines 409-426) can be satisfied trivially because the attacker controls real, validly signed historical blocks — they simply never include the one block that satisfies the terminal equality check, and instead can repeat identical/empty batches forever.

`handle_next_download_response` unconditionally sets `self.idle = true` regardless of the result [4](#0-3) , so `send_next_download_request` will issue a fresh request on the next call, and the same peer keeps servicing the identical request forever with no error path taken.

Critically, `WAIT_FOR_TENURE_END_BLOCK_TIMEOUT` is declared [5](#0-4)  but it is never read or compared against `start_request_time` anywhere in the codebase (confirmed via search — all matches for this constant and `start_request_time` are confined to this file's declarations/state constructions, with no timeout-enforcement logic). `NakamotoTenureDownloaderSet::run` only detects dead/broken TCP connections via `neighbor_rpc.is_dead_or_broken` [6](#0-5)  and only re-prioritizes/deprioritizes a peer when `send_next_download_request` or `handle_next_download_response` returns an `Err` [7](#0-6) . Since the attacker's responses are always well-formed and `Ok`, none of these guards trigger, so the peer is never marked dead/deprioritized and no other candidate peer for the tenure is tried.

### Impact Explanation
The tenure downloader (and by extension the peer slot bound to it in `NakamotoTenureDownloaderSet`) is wedged on this one tenure forever, since `is_done()` checks `self.state == Done` [8](#0-7)  which is never reached, and `inflight()`/`is_empty()` in the set continue to treat this machine as legitimately in-progress [9](#0-8) . Since tenure downloads are pipelined (the comment at the top of the file notes that "the N+1'st tenure needs to feed data into the Nth tenure"), stalling one tenure can hold back the node's ability to catch up to the canonical chain tip using that download path — matching the "steering a node off the tip" High-impact category. This is repeatable indefinitely and costs the attacker nothing beyond running a peer that responds to requests without ever completing them.

### Likelihood Explanation
The attacker only needs to be a normal, connectable P2P/RPC peer that the victim node has scheduled to serve a historic tenure (this happens naturally whenever the attacker's peer address appears in another peer's inventory as having the tenure). No privileged role, secret, or signing key is needed — the attacker just needs to control the response content of its own `GetTenureBlocks`/tenure-batch HTTP endpoint. This is a low-cost, remotely triggerable, indefinitely repeatable stall.

### Recommendation
Enforce `WAIT_FOR_TENURE_END_BLOCK_TIMEOUT` (or a similar bound) against `start_request_time` in `GetTenureBlocks` state, both in `try_accept_tenure_blocks`/`handle_next_download_response` and in `NakamotoTenureDownloaderSet::run`, so that a downloader that fails to make progress (or receives repeated empty/non-terminal batches) within the timeout is treated as failed, the peer deprioritized, and a different peer/tenure download attempted.

### Proof of Concept
Rust test outline in `stackslib/src/net/tests/download/nakamoto.rs` (or a new unit test colocated with `tenure_downloader.rs`):
1. Construct a `NakamotoTenureDownloader` and manually drive it through `GetTenureStartBlock`/`GetTenureEndBlock` to reach `GetTenureBlocks(cursor, t0)` with a known `tenure_start_block` set and `tenure_end_block` set.
2. Repeatedly call `try_accept_tenure_blocks(vec![])` (simulating the malicious peer's empty batch response) in a loop (e.g., 10,000 iterations).
3. Assert after every call that `matches!(downloader.state, NakamotoTenureDownloadState::GetTenureBlocks(..))` holds and `downloader.is_done()` is `false`.
4. Additionally show `get_epoch_time_ms()` elapsed far exceeds `WAIT_FOR_TENURE_END_BLOCK_TIMEOUT` (1 ms) with the state still unchanged, demonstrating the constant is not enforced anywhere in `try_accept_tenure_blocks` or in `NakamotoTenureDownloaderSet::run`.

### Citations

**File:** stackslib/src/net/download/nakamoto/tenure_downloader.rs (L52-52)
```rust
pub const WAIT_FOR_TENURE_END_BLOCK_TIMEOUT: u64 = 1;
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader.rs (L178-235)
```rust
    pub fn try_accept_tenure_start_block(
        &mut self,
        tenure_start_block: NakamotoBlock,
    ) -> Result<(), NetError> {
        let NakamotoTenureDownloadState::GetTenureStartBlock(..) = &self.state else {
            // not the right state for this
            warn!("Invalid state for this method";
                  "state" => %self.state);
            return Err(NetError::InvalidState);
        };

        if self.tenure_start_block_id != tenure_start_block.header.block_id() {
            // not the block we were expecting
            warn!("Invalid tenure-start block: unexpected";
                  "tenure_id" => %self.tenure_id_consensus_hash,
                  "tenure_id_start_block" => %self.tenure_start_block_id,
                  "tenure_start_block ID" => %tenure_start_block.header.block_id(),
                  "state" => %self.state);
            return Err(NetError::InvalidMessage);
        }

        if let Err(e) = tenure_start_block
            .header
            .verify_signer_signatures(&self.start_signer_keys, self.epoch_id)
        {
            // signature verification failed
            warn!("Invalid tenure-start block: bad signer signature";
                   "tenure_id" => %self.tenure_id_consensus_hash,
                   "block.header.block_id" => %tenure_start_block.header.block_id(),
                   "state" => %self.state,
                   "error" => %e);
            return Err(NetError::InvalidMessage);
        }

        debug!(
            "Accepted tenure-start block for tenure {} block={}",
            &self.tenure_id_consensus_hash,
            &tenure_start_block.block_id()
        );
        self.tenure_start_block = Some(tenure_start_block);

        if let Some(tenure_end_block) = self.tenure_end_block.take() {
            // we already have the tenure-end block, so immediately proceed to accept it.
            debug!(
                "Preemptively process tenure-end block {} for tenure {}",
                tenure_end_block.block_id(),
                &self.tenure_id_consensus_hash
            );
            self.try_accept_tenure_end_block(&tenure_end_block)?;
        } else {
            // need to get tenure_end_block.
            self.state = NakamotoTenureDownloadState::GetTenureEndBlock(
                self.tenure_end_block_id.clone(),
                get_epoch_time_ms(),
            );
        }
        Ok(())
    }
```

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

**File:** stackslib/src/net/download/nakamoto/tenure_downloader.rs (L759-804)
```rust
    pub fn handle_next_download_response(
        &mut self,
        response: StacksHttpResponse,
    ) -> Result<Option<Vec<NakamotoBlock>>, NetError> {
        let handle_result = match &self.state {
            NakamotoTenureDownloadState::GetTenureStartBlock(block_id, start_request_time) => {
                debug!(
                    "Got download response for tenure-start block {} in {}ms",
                    &block_id,
                    get_epoch_time_ms().saturating_sub(*start_request_time)
                );
                let block = response.decode_nakamoto_block().inspect_err(|e| {
                    warn!("Failed to decode response for a Nakamoto block: {e:?}")
                })?;
                self.try_accept_tenure_start_block(block)?;
                Ok(None)
            }
            NakamotoTenureDownloadState::GetTenureEndBlock(block_id, start_request_time) => {
                debug!(
                    "Got download response to tenure-end block {} in {}ms",
                    &block_id,
                    get_epoch_time_ms().saturating_sub(*start_request_time)
                );
                let block = response.decode_nakamoto_block().inspect_err(|e| {
                    warn!("Failed to decode response for a Nakamoto block: {e:?}")
                })?;
                self.try_accept_tenure_end_block(&block)?;
                Ok(None)
            }
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

**File:** stackslib/src/net/download/nakamoto/tenure_downloader.rs (L806-808)
```rust
    pub fn is_done(&self) -> bool {
        self.state == NakamotoTenureDownloadState::Done
    }
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader_set.rs (L206-238)
```rust
    /// Count up the number of in-flight messages, based on the states of each instantiated
    /// downloader.
    pub fn inflight(&self) -> usize {
        let mut cnt = 0;
        for downloader_opt in self.downloaders.iter() {
            let Some(downloader) = downloader_opt else {
                continue;
            };
            if downloader.idle {
                continue;
            }
            if downloader.is_done() {
                continue;
            }
            cnt += 1;
        }
        cnt
    }

    /// Determine if this downloader set is empty -- i.e. there's no in-progress downloaders.
    pub fn is_empty(&self) -> bool {
        for downloader_opt in self.downloaders.iter() {
            let Some(downloader) = downloader_opt else {
                continue;
            };
            if downloader.is_done() {
                continue;
            }
            debug!("TenureDownloadSet::is_empty(): have downloader for tenure {:?} assigned to {} in state {}", &downloader.tenure_id_consensus_hash, &downloader.naddr, &downloader.state);
            return false;
        }
        true
    }
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader_set.rs (L567-647)
```rust
            match downloader.send_next_download_request(network, neighbor_rpc) {
                Ok(true) => {}
                Ok(false) => {
                    // this downloader is dead or broken
                    finished.push(naddr.clone());
                    continue;
                }
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
        }

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
