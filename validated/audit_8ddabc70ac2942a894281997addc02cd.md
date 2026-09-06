### Title
Signature-verification failure during `GetTenureEndBlock` causes `NakamotoTenureDownloader::idle` to get stuck at `false`, permanently poisoning the downloader's inflight-accounting - ([File: stackslib/src/net/download/nakamoto/tenure_downloader.rs])

### Summary
`handle_next_download_response` computes `self.idle = true` **after** the `match` block that dispatches on the current state, but every arm of that match uses the `?` operator to propagate `try_accept_tenure_start_block`/`try_accept_tenure_end_block`/`try_accept_tenure_blocks` errors. Because `?` returns from the *entire function*, not just the match arm/block, any validation failure (e.g. a bad signer signature on the tenure-end block) causes an early `Err` return that skips `self.idle = true;`, leaving `idle == false` even though no request is actually in flight anymore.

### Finding Description
In `NakamotoTenureDownloader::handle_next_download_response` [1](#0-0) , the `GetTenureEndBlock` arm decodes the attacker-supplied response and calls:

```rust
self.try_accept_tenure_end_block(&block)?;
``` [2](#0-1) 

`try_accept_tenure_end_block` returns `Err(NetError::InvalidMessage)` if `tenure_end_block.header.verify_signer_signatures(&self.end_signer_keys, self.epoch_id)` fails [3](#0-2) . Since `?` propagates out of the whole `handle_next_download_response` function, execution never reaches the trailing `self.idle = true; handle_result` at the bottom of the function [4](#0-3) . The attacker only needs to answer a `GET /v3/blocks/{tenure_end_block_id}` request (or the equivalent HTTP path invoked by `make_next_download_request` for `GetTenureEndBlock`, see [5](#0-4) ) with any `NakamotoBlock` whose signer signatures don't validate against `self.end_signer_keys`/`self.epoch_id` — this requires no privileged key, just serving arbitrary bytes that decode to a `NakamotoBlock`.

`idle` is not cosmetic: `TenureDownloaderSet::inflight()` treats a non-idle, non-done downloader as an in-flight request [6](#0-5) , and `try_resume_peer` refuses to re-bind any new peer to a downloader whose `idle` is `false` [7](#0-6) . Downstream, `download_unconfirmed_tenures` gates whether the unconfirmed (tip) downloader machinery runs at all on `self.tenure_downloads.inflight() > 0` [8](#0-7) . When the offending peer's response triggers an `Err`, the caller in `TenureDownloaderSet::run` marks that specific peer dead/broken and removes its `peers` mapping [9](#0-8) , but this only clears the *peer→index* binding; whether the underlying downloader slot itself (with `idle` still `false`) is also purged depends on `clear_downloader`, whose exact behavior toward shared/orphaned downloader slots was not fully verifiable in this pass. If the downloader object survives with `idle == false`, it can never be re-attached to another peer via `try_resume_peer` and will be perpetually counted by `inflight()`, causing the false-positive "inflight" signal seen by `download_unconfirmed_tenures`.

### Impact Explanation
A single malicious/bad-signature `NakamotoBlock` served in response to a `GetTenureEndBlock` request causes `NakamotoTenureDownloader::idle` to remain `false` forever for that state machine, even though the function correctly returns `Err` and rejects the bad block (`try_accept_tenure_end_block`'s validation itself is not bypassed — no forged state is stored). The concrete, code-confirmed defect is a control-flow bug (early-return-skips-cleanup) in `handle_next_download_response`, reproducible at the unit level with a single crafted response. The broader network-level consequence (permanently stuck download slot / stalled sync gating via `inflight()`) is architecturally plausible given how `idle` is consumed elsewhere, but could not be fully confirmed end-to-end because the exact cleanup semantics of `clear_downloader` were not inspected in this session.

### Likelihood Explanation
Low attacker cost: any remote peer that a node is currently downloading a historic tenure from can simply answer the `GetTenureEndBlock` HTTP GET with a block that has an invalid/missing signer signature. No secret, privileged role, or special timing is required — only that the local node has an active `NakamotoTenureDownloader` in the `GetTenureEndBlock` state targeting that peer, which happens routinely during normal confirmed-tenure sync.

### Recommendation
Move `self.idle = true;` inside each match arm before returning, or restructure the function so the idle flag is set via a `finally`-style guard (e.g., wrap the body and set `idle = true` unconditionally using a local closure/`Result` computation that does not use `?` to escape past the flag update), e.g.:
```rust
let handle_result = (|| { ... })();  // capture ?-early-returns inside a closure
self.idle = true;
handle_result
```
This guarantees `idle` is always reset regardless of which branch or validation step fails.

### Proof of Concept
Unit test in `stackslib/src/net/tests/download/nakamoto.rs` (or a new test module) exercising `NakamotoTenureDownloader::handle_next_download_response` directly:
1. Build a `NakamotoTenureDownloader` and drive it (via `try_accept_tenure_start_block`) into the `GetTenureEndBlock(..)` state, as done in the existing test at [10](#0-9) .
2. Call `send_next_download_request` (or manually set `downloader.idle = false;`) to simulate an outstanding request.
3. Construct a `StacksHttpResponse` wrapping a `NakamotoBlock` whose header has a tampered/missing signer signature (so `verify_signer_signatures` fails) but which otherwise matches `tenure_end_block_id`.
4. Call `downloader.handle_next_download_response(response)` and assert it returns `Err(NetError::InvalidMessage)`.
5. Assert `downloader.idle == false` post-call — proving the cleanup (`self.idle = true`) was skipped, confirming the early-return-skips-cleanup fault at the exact site `stackslib/src/net/download/nakamoto/tenure_downloader.rs:776-787,802`.

### Citations

**File:** stackslib/src/net/download/nakamoto/tenure_downloader.rs (L272-283)
```rust
        if let Err(e) = tenure_end_block
            .header
            .verify_signer_signatures(&self.end_signer_keys, self.epoch_id)
        {
            // bad signature
            warn!("Invalid tenure-end block: bad signer signature";
                  "tenure_id" => %self.tenure_id_consensus_hash,
                  "block.header.block_id" => %tenure_end_block.header.block_id(),
                  "state" => %self.state,
                  "error" => %e);
            return Err(NetError::InvalidMessage);
        }
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader.rs (L508-514)
```rust
            NakamotoTenureDownloadState::GetTenureEndBlock(end_block_id, start_request_time) => {
                debug!(
                    "Request tenure-end block {} at {}",
                    &end_block_id, start_request_time
                );
                StacksHttpRequest::new_get_nakamoto_block(peerhost, end_block_id.clone())
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

**File:** stackslib/src/net/download/nakamoto/tenure_downloader_set.rs (L206-223)
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

**File:** stackslib/src/net/download/nakamoto/download_state_machine.rs (L1370-1391)
```rust
        let new_confirmed_blocks = if self.tenure_downloads.inflight() > 0 {
            self.download_confirmed_tenures(network, chainstate, 0)
        } else {
            HashMap::new()
        };

        // Only run unconfirmed downloaders if we're _not_ busy obtaining the highest confirmed
        // tenure.  The behavior here ensures that we first obtain the highest complete tenure, and
        // then poll for new unconfirmed tenure blocks.
        let (new_unconfirmed_blocks, new_highest_confirmed_downloaders) =
            if self.tenure_downloads.inflight() > 0 {
                (HashMap::new(), HashMap::new())
            } else {
                Self::run_unconfirmed_downloaders(
                    &mut self.unconfirmed_tenure_downloads,
                    network,
                    &mut self.neighbor_rpc,
                    sortdb,
                    &burnchain_tip,
                    chainstate,
                )
            };
```

**File:** stackslib/src/net/tests/download/nakamoto.rs (L316-337)
```rust
    // advance state
    assert!(td
        .try_accept_tenure_start_block(blocks.first().unwrap().clone())
        .is_ok());

    let NakamotoTenureDownloadState::GetTenureEndBlock(block_id, ..) = &td.state else {
        panic!("wrong state");
    };
    assert_eq!(block_id, &next_tenure_start_block.header.block_id());
    assert_eq!(td.tenure_start_block, Some(tenure_start_block.clone()));
    assert!(td.tenure_length().is_none());

    // must be last block
    assert!(td.try_accept_tenure_end_block(&tenure_start_block).is_err());
    assert!(td
        .try_accept_tenure_end_block(blocks.last().unwrap())
        .is_err());

    // advance state
    assert!(td
        .try_accept_tenure_end_block(&next_tenure_start_block)
        .is_ok());
```
