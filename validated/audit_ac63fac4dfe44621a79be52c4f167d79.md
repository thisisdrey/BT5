### Title
Malicious peer can permanently monopolize a tenure's sole downloader slot via repeated empty-block responses, starving all other advertising peers - ([File: stackslib/src/net/download/nakamoto/tenure_downloader_set.rs])

### Summary
`NakamotoTenureDownloaderSet::make_tenure_downloaders` only ever instantiates one `NakamotoTenureDownloader` per tenure (`has_downloader_for_tenure` short-circuits further assignment), and the only paths that free a slot for a different peer are an outright `Err` from `send_next_download_request`/`handle_next_download_response` (via `mark_failed_and_deprioritize_peer`) or the downloader reaching `Done`. A peer that keeps returning syntactically valid but semantically empty responses in the `GetTenureBlocks` state can keep the downloader in an indefinite `Ok(None)` loop without ever erroring, monopolizing that tenure's slot while other honest peers advertising the same tenure are never tried.

### Finding Description
In `tenure_downloader.rs::try_accept_tenure_blocks`, an empty `tenure_blocks` vector is accepted immediately and unconditionally: `if tenure_blocks.is_empty() { return Ok(None); }` [1](#0-0)  — this happens before any cursor/signature validation, so it costs the attacker nothing and requires no valid signer signatures. `handle_next_download_response` always sets `self.idle = true` regardless of whether progress was made, so the downloader is always eligible to be re-driven by the same round-robin logic, and it never returns `Err` for this case [2](#0-1) .

In the caller, `run()` treats `Ok(None)` as "nothing more to do this round" and simply `continue`s — no failure counter is incremented, no peer is deprioritized: [3](#0-2) . Only the `Err(e)` branch calls `mark_failed_and_deprioritize_peer` [4](#0-3) .

Crucially, `make_tenure_downloaders` never creates a second downloader for a tenure that already has one in progress: `if self.has_downloader_for_tenure(ch) { schedule.pop_front(); continue; }` [5](#0-4) . Because the malicious peer's downloader is never `Done` (state stays stuck in `GetTenureBlocks` forever) and never errors, this check is always true, so any other honest peer listed in `available[ch]` is never used to instantiate a competing/replacement downloader for that tenure — its entry is popped from the neighbor list via `neighbors.pop()` on each attempt [6](#0-5)  and discarded without ever being attempted, since the `has_downloader_for_tenure` short-circuit occurs after the pop.

### Impact Explanation
The tenure remains permanently unfetched from this node's perspective as long as the malicious peer keeps responding with empty/no-progress `GetTenureBlocks` bodies once per round; `attempted_tenures`/`attempt_failed_tenures`/`deprioritized_peers` bookkeeping is never updated because no `Err` is ever produced, so there is no cool-down or peer-rotation logic to intervene. This is a liveness/availability stall on the block downloader for a specific tenure driven entirely by one unprivileged remote peer, at the cost of one lightweight HTTP response per round; it does not itself forge or serve incorrect canonical data, but it can indefinitely block the node from completing a wanted tenure's download while it keeps the sole slot occupied.

### Likelihood Explanation
The attacker only needs to (a) be listed as a peer that advertises the tenure in its inventory so it gets selected by `make_tenure_downloaders`, and (b) respond to `GET`-tenure-blocks requests with a decodable but empty block list. No handshake secret, signing key, or elevated role is required — any peer the node is connected to and which is picked from `available[ch]` can do this. The main precondition is that the malicious peer must be selected before other honest peers happen to be tried (order depends on `neighbors.pop()`/vector ordering), and it must remain connected without triggering the network layer's own connection/idle timeouts (which are outside this file and not verified here).

### Recommendation
Track empty/no-progress responses (e.g., a per-downloader round counter) and treat repeated `Ok(None)` results without cursor advancement in `GetTenureBlocks` as a failure, invoking `mark_failed_and_deprioritize_peer` and freeing the slot after a bounded number of rounds. Additionally, allow `make_tenure_downloaders` to schedule a competing/backup downloader for a tenure whose current downloader has been idle/non-progressing for too long, instead of unconditionally short-circuiting on `has_downloader_for_tenure`.

### Proof of Concept
Rust test in `tenure_downloader_set.rs`/`tenure_downloader.rs` test harness:
1. Construct a `NakamotoTenureDownloader` in `GetTenureBlocks` state and call `try_accept_tenure_blocks(vec![])` repeatedly; assert it returns `Ok(None)` every time and `self.state` never changes (stuck forever, no `Err`).
2. Build a `NakamotoTenureDownloaderSet` with `available[ch] = [malicious_peer, honest_peer]`; call `make_tenure_downloaders` once to attach `malicious_peer`. Then call it again (simulating a re-scheduled `ch`) and assert `has_downloader_for_tenure(ch)` is `true`, causing `honest_peer` to be popped from `available[ch]` and discarded via `schedule.pop_front()` without ever having a downloader created for it — assert `self.peers` never contains `honest_peer` and `self.downloaders` never contains a downloader bound to `honest_peer`, even though `honest_peer` was present in `available[ch]`.

### Citations

**File:** stackslib/src/net/download/nakamoto/tenure_downloader.rs (L366-369)
```rust
        if tenure_blocks.is_empty() {
            // nothing to do
            return Ok(None);
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

**File:** stackslib/src/net/download/nakamoto/tenure_downloader_set.rs (L392-396)
```rust
            let Some(naddr) = neighbors.pop() else {
                debug!("No more neighbors can serve tenure {ch}");
                schedule.pop_front();
                continue;
            };
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader_set.rs (L408-411)
```rust
            if self.has_downloader_for_tenure(ch) {
                schedule.pop_front();
                continue;
            }
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader_set.rs (L623-625)
```rust
            let blocks = match downloader.handle_next_download_response(response) {
                Ok(Some(blocks)) => blocks,
                Ok(None) => continue,
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
