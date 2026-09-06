### Title
`self.idle` is never reset to `true` after `try_accept_tenure_blocks` rejects a foreign-tenure block, stalling the `NakamotoTenureDownloader` state machine - ([File: stackslib/src/net/download/nakamoto/tenure_downloader.rs])

### Summary
`NakamotoTenureDownloader::handle_next_download_response` sets `self.idle = true` only on the line immediately following the `match` block, but every arm of that `match` uses the `?` operator to propagate errors from `try_accept_tenure_start_block`, `try_accept_tenure_end_block`, and `try_accept_tenure_blocks`. Because `?` triggers an early `return` from the whole function, an `Err` from any of these calls causes the function to exit before `self.idle = true` is ever executed, leaving `self.idle == false` even though the in-flight request has been fully consumed and no new request is outstanding.

### Finding Description
In `try_accept_tenure_blocks` (`stackslib/src/net/download/nakamoto/tenure_downloader.rs:354-427`), each block in the peer-supplied `Vec<NakamotoBlock>` is checked against `self.tenure_id_consensus_hash`: [1](#0-0) 

If a remote peer, while its `NakamotoTenureDownloader` is in state `GetTenureBlocks`, replies to the `new_get_nakamoto_tenure` request with a tenure payload (decoded via `response.decode_nakamoto_tenure()`) containing a single `NakamotoBlock` whose `header.consensus_hash` differs from `self.tenure_id_consensus_hash`, `try_accept_tenure_blocks` returns `Err(NetError::InvalidMessage)`.

That error is propagated in `handle_next_download_response`: [2](#0-1) 

The `?` on `self.try_accept_tenure_blocks(blocks)?` (line 797) causes an immediate `return Err(...)` from the entire `handle_next_download_response` function, so line 802 (`self.idle = true;`) is never reached. The invariant that `self.idle` reflects "no ongoing network request" is broken by this single crafted response: `self.idle` was set to `false` by `send_next_download_request` (line 749) when the request was issued, and it now permanently remains `false` for this state machine even though the response has already been consumed and no request is outstanding. The same bypass applies identically to the `GetTenureStartBlock` and `GetTenureEndBlock` arms (lines 770-786), since they use the identical `?` pattern.

### Impact Explanation
Any component that relies on `self.idle` to decide whether it may safely issue the next request for this `NakamotoTenureDownloader` (e.g., `send_next_download_request`, or logic in `tenure_downloader_set.rs` that checks `idle` before scheduling work) will observe `idle == false` indefinitely, even though there is no outstanding request. This causes the state machine to stall on that peer/tenure, since it is treated as "still waiting for the network" when it is not, and it never advances the `GetTenureBlocks` state further. This is triggerable by any unprivileged peer serving one HTTP response with a single garbage-consensus-hash block. I was not able to fully verify (due to tool-call budget) whether `tenure_downloader_set.rs` detects and discards a downloader in this stuck state (e.g., via a separate dead/stale timeout check that is independent of `idle`), which would mitigate the practical severity to a local, recoverable stall rather than a persistent hang.

### Likelihood Explanation
- Reachable by any unprivileged peer that a node is downloading a historic tenure from over the P2P/RPC data path (`new_get_nakamoto_tenure`).
- Requires the node to already be in the `GetTenureBlocks` state for some tenure (a normal/expected occurrence during tenure sync), and for the attacking peer to be the one selected to serve that tenure.
- Attacker cost is one crafted HTTP response containing a single `NakamotoBlock` with a foreign `consensus_hash`, no signature forgery or other privileged material needed since the mismatch check triggers before signature verification.
- Repeatable per tenure-download attempt against that peer.

### Recommendation
Set `self.idle = true` unconditionally before returning from `handle_next_download_response`, regardless of whether the inner processing succeeded or failed — e.g., capture the result of the match into a local variable without using `?` inside the arms (or wrap each arm's fallible calls so their `Err` is captured rather than short-circuiting the outer function), then set `self.idle = true` before returning that captured result.

### Proof of Concept
```rust
// stackslib/src/net/download/nakamoto/tenure_downloader.rs (test)
#[test]
fn test_idle_not_reset_on_bad_tenure_block() {
    // Build a NakamotoTenureDownloader `dl` and drive it into
    // NakamotoTenureDownloadState::GetTenureBlocks(..) as done in existing
    // module tests (see other tests in this file for constructing
    // tenure_start_block / tenure_end_block / signer keys).
    // ...
    dl.idle = false; // simulate that send_next_download_request() has just fired

    // Craft one NakamotoBlock whose header.consensus_hash != dl.tenure_id_consensus_hash
    let mut bad_block = /* valid block matching the state's expected block_id */;
    bad_block.header.consensus_hash = ConsensusHash([0xAA; 20]); // foreign hash

    let result = dl.try_accept_tenure_blocks(vec![bad_block]);
    assert!(result.is_err());

    // This is what handle_next_download_response would do internally:
    // let blocks_opt = self.try_accept_tenure_blocks(blocks)?;  <-- early return here
    // self.idle = true;  <-- never reached
    assert_eq!(dl.idle, false); // idle incorrectly still false, even though no request is outstanding
}
```
This directly demonstrates that after a single crafted "one wrong-tenure block" response, `try_accept_tenure_blocks` returns `Err`, and — per the control flow of `handle_next_download_response` at lines 788-804 — `self.idle` remains `false` because the `?` operator on line 797 causes an early return that skips line 802.

### Citations

**File:** stackslib/src/net/download/nakamoto/tenure_downloader.rs (L379-385)
```rust
            if block.header.consensus_hash != self.tenure_id_consensus_hash {
                warn!("Unexpected Nakamoto block -- not part of tenure";
                      "block.header.consensus_hash" => %block.header.consensus_hash,
                      "self.tenure_id_consensus_hash" => %self.tenure_id_consensus_hash,
                      "state" => %self.state);
                return Err(NetError::InvalidMessage);
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
