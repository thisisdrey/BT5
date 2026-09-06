### Title
Rejected empty-tenure-change GetTenureEndBlock response leaves `idle` desynced via early `?` return - ([File: stackslib/src/net/download/nakamoto/tenure_downloader.rs])

### Summary
`try_accept_tenure_end_block` correctly rejects a tenure-end block lacking a tenure-change transaction (`try_get_tenure_change_payload()` returning `None`) with `Err(NetError::InvalidMessage)` before mutating `self.tenure_end_block`, preserving `self.tenure_end_block == None`. However, this correct rejection propagates through `handle_next_download_response`'s `?` operator, which bypasses the `self.idle = true` statement at the end of the function, leaving `self.idle` stuck at `false` even though the in-flight request has completed.

### Finding Description
In `NakamotoTenureDownloader::try_accept_tenure_end_block` [1](#0-0) , when a peer's tenure-end block response decodes successfully but `try_get_tenure_change_payload()` returns `None` (e.g. a coinbase-only block with no tenure-change tx, but still passing the earlier `is_wellformed_tenure_start_block()` check at lines 288-298), the function returns `Err(NetError::InvalidMessage)` strictly before line 322 (`self.tenure_end_block = Some(...)`). This means `self.tenure_end_block` correctly remains `None` on this path - the immediate check is functioning as intended.

The fault is in the caller, `handle_next_download_response` [2](#0-1) . The `GetTenureEndBlock` arm calls `self.try_accept_tenure_end_block(&block)?` directly inside the match expression that is assigned to `handle_result` [3](#0-2) . Because the `?` is evaluated in the body of `handle_next_download_response` itself (not inside a nested closure), an `Err` here causes an immediate return from the whole function - skipping `self.idle = true` at line 802 [4](#0-3) . Since `self.idle` was set to `false` when the request was sent (`send_next_download_request`, line 749) [5](#0-4) , it remains `false` after this rejected response is fully processed, even though there is no longer an outstanding request for this state machine.

### Impact Explanation
This is a state-desync bug rather than a memory-safety or forged-data issue: the tenure downloader state machine believes it still has an in-flight request (`idle == false`) after the request/response round-trip has actually completed and been rejected. Combined with the downstream inflight-bookkeeping logic (`neighbor_rpc.has_inflight`), this can cause a permanently stuck-but-not-idle machine for that tenure, preventing legitimate retries or requeuing, effectively stalling that tenure's download until some external timeout/cleanup logic (if any) intervenes. This matches a bounded compute/availability degradation on the download path rather than a forged-state or unauthenticated-write issue, since no data is stored or relayed as canonical.

### Likelihood Explanation
Any remote peer serving as a data source for `NakamotoTenureDownloader` (no special privilege required beyond being queried for this tenure) can trigger this by responding to a `GetTenureEndBlock` request with a decodable `NakamotoBlock` that passes `is_wellformed_tenure_start_block()` but has no tenure-change transaction (e.g., coinbase-only). This is a single malformed HTTP response, trivially repeatable, requiring no signature forgery or other cryptographic material - the block is rejected for missing content, not for a failed signature check.

### Recommendation
Restructure `handle_next_download_response` so `self.idle = true` is set unconditionally regardless of whether the inner match arm results in `Ok` or `Err` - e.g., capture the `Result` from each arm without using `?` inside the match (matching on the decode/accept results explicitly, or wrapping the whole match in a closure and always executing `self.idle = true` after it via `let result = (|| { ... })(); self.idle = true; result`).

### Proof of Concept
Rust test in `stackslib/src/net/tests/download/nakamoto.rs` (or a new unit test colocated with `tenure_downloader.rs`):
1. Construct a `NakamotoTenureDownloader` and drive it to state `GetTenureEndBlock(...)`, ensuring `tenure_start_block` is set (so the `InvalidState` early return at line 255-258 is not hit) and calling `send_next_download_request`-equivalent so `self.idle = false`.
2. Build a `NakamotoBlock` that has a coinbase tx as block 0 and no tenure-change transaction, matching the tenure-start block ID so it passes the block-id check (line 260) and signature check (line 272), and passes `is_wellformed_tenure_start_block()`.
3. Wrap this block into a `StacksHttpResponse` (via the same encoding path used by `decode_nakamoto_block()`), and call `downloader.handle_next_download_response(response)`.
4. Assert:
   - The call returns `Err(NetError::InvalidMessage)`.
   - `downloader.tenure_end_block == None` (unmutated, confirming the immediate check is correct).
   - `downloader.idle == false` (bug: should be `true` since the request/response cycle completed).

### Citations

**File:** stackslib/src/net/download/nakamoto/tenure_downloader.rs (L300-304)
```rust
        let Some(tc_payload) = tenure_end_block.try_get_tenure_change_payload() else {
            warn!("Invalid tenure-end block: no tenure-change transaction";
                  "block_id" => %tenure_end_block.block_id());
            return Err(NetError::InvalidMessage);
        };
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader.rs (L748-750)
```rust
        neighbor_rpc.send_request(network, self.naddr.clone(), request)?;
        self.idle = false;
        Ok(true)
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader.rs (L776-804)
```rust
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
