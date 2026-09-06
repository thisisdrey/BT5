### Title
Unbounded `tenure_blocks` growth via attacker-inflated `previous_tenure_blocks` bound in `try_accept_tenure_blocks` - ([File: stackslib/src/net/download/nakamoto/tenure_downloader.rs])

### Summary
`NakamotoTenureDownloader::try_accept_tenure_blocks` bounds the number of buffered blocks using `self.tenure_length()`, which is derived entirely from `tc_payload.previous_tenure_blocks` inside the attacker-supplied tenure-end block. If the attacker can get an arbitrary `tenure_end_block` accepted (via the sentinel `tenure_end_block_id == StacksBlockId([0x00;32])` bypass path in `try_accept_tenure_end_block`), the "cap" becomes attacker-chosen and effectively unbounded (e.g. `u32::MAX`), defeating the intended anti-DoS bound on `tenure_blocks`.

### Finding Description
The invariant that should hold is: `tenure_blocks.len()` is bounded by the *true* number of blocks in the canonical tenure. Instead, the bound used at [1](#0-0) 
is `tenure_length()`, computed purely from `tc_payload.previous_tenure_blocks` taken from the `tenure_end_block` that this same peer supplied [2](#0-1) 
. The only structural checks performed on `tenure_end_block` in `try_accept_tenure_end_block` are: block-id match against `self.tenure_end_block_id` *unless it equals the sentinel* `StacksBlockId([0x00;32])`, signer-signature verification, `is_wellformed_tenure_start_block`, and that `tc_payload.prev_tenure_consensus_hash` matches the tenure-start block's consensus hash [3](#0-2) 
. None of these checks constrain `tc_payload.previous_tenure_blocks` to the real tenure length — it is an attacker-chosen field inside a validly-signed block, and signer signatures do not authenticate the *numeric accuracy* of that count, only the block's other contents per the signing protocol.

Given this, in `try_accept_tenure_blocks`, the guard [4](#0-3) 
becomes `blocks.len() + count > u32::MAX + 1`, which is never true for any realistically sized batch. Each subsequent chunk of contiguous, correctly-signed, same-`consensus_hash` blocks streamed by the malicious peer (matched via `expected_block_id`/`parent_block_id` cursor, lines 372–407) is accepted and appended into `self.tenure_blocks` with no effective ceiling until the peer finally supplies the real tenure-start block (matched by `tenure_start_block_id` at lines 462–475).

### Impact Explanation
The primary, well-scoped effect is memory/compute growth in `tenure_blocks: Option<Vec<NakamotoBlock>>` proportional to how many blocks the malicious peer chooses to stream, rather than to the tenure's real length — this is a bounded-compute-DoS becoming unbounded due to the broken cap, consistent with the "High" severity bucket (bounded compute DoS on a read/sync path). It requires the attacker to control the answer to the *previous* question (getting an unauthenticated `tenure_end_block` accepted via the sentinel bypass) as a precondition; absent that, `self.tenure_end_block_id` would be a network/chainstate-derived, non-attacker-chosen block ID and the peer could not substitute an arbitrary tenure-change payload.

### Likelihood Explanation
Exploitability depends fully on whether the sentinel-bypass precondition from the referenced prior question is reachable in practice — i.e., whether `self.tenure_end_block_id` can actually be `StacksBlockId([0x00;32])` in a downloader instance reachable from an untrusted peer's HTTP response. That is a separate, unverified claim; I was unable to confirm within this session (tool budget exhausted) whether any live construction path (`NakamotoTenureDownloader::new` callers in `download_state_machine.rs` / the unconfirmed-tenure machinery) actually sets `tenure_end_block_id` to the zero sentinel in a state reachable by a remote, unprivileged peer, or whether it's reserved for internal/local bookkeeping only. Without confirming that precondition holds for a remote attacker, this finding cannot be independently substantiated as reachable, and the underlying premise ("attacker controls tenure_end_block_id's content via the sentinel bypass") from the referenced prior question was not verified here either.

### Recommendation
Do not use peer-supplied `tc_payload.previous_tenure_blocks` as the sole bound. Bound `tenure_blocks` growth via an independent, protocol-level ceiling (e.g., configuration constant for max blocks per tenure, or reconciling against the tenure's real height range derivable from sortition/burnchain data) in addition to (not instead of) the declared count, and reject/disconnect a peer whose declared tenure length is inconsistent with what's cryptographically/structurally knowable.

### Proof of Concept
Not constructed — the prerequisite for this finding (remote-reachable sentinel bypass of `tenure_end_block_id`) was not confirmed. A conclusive PoC would need to first demonstrate, via a `stackslib` net test, that a `NakamotoTenureDownloader` in a state reachable through the `stacks-node`/HTTP response path can have `tenure_end_block_id == StacksBlockId([0x00;32])`, then feed a crafted `tenure_end_block` with `previous_tenure_blocks = u32::MAX` and a large contiguous batch of valid blocks into `try_accept_tenure_blocks`, asserting `self.tenure_blocks.len()` grows past any legitimate tenure-length bound before the true tenure-start block is supplied.

### Citations

**File:** stackslib/src/net/download/nakamoto/tenure_downloader.rs (L260-322)
```rust
        if self.tenure_end_block_id != tenure_end_block.header.block_id()
            && self.tenure_end_block_id != StacksBlockId([0x00; 32])
        {
            // not the block we asked for
            warn!("Invalid tenure-end block: unexpected";
                  "tenure_id" => %self.tenure_id_consensus_hash,
                  "tenure_id_end_block" => %self.tenure_end_block_id,
                  "block.header.block_id" => %tenure_end_block.header.block_id(),
                  "state" => %self.state);
            return Err(NetError::InvalidMessage);
        }

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

        // extract the needful -- need the tenure-change payload (which proves that the tenure-end
        // block is the tenure-start block for the next tenure) and the parent block ID (which is
        // the next block to download).
        let Ok(valid) = tenure_end_block.is_wellformed_tenure_start_block() else {
            warn!("Invalid tenure-end block: failed to validate tenure-start";
                  "block_id" => %tenure_end_block.block_id());
            return Err(NetError::InvalidMessage);
        };

        if !valid {
            warn!("Invalid tenure-end block: not a well-formed tenure-start block";
                  "block_id" => %tenure_end_block.block_id());
            return Err(NetError::InvalidMessage);
        }

        let Some(tc_payload) = tenure_end_block.try_get_tenure_change_payload() else {
            warn!("Invalid tenure-end block: no tenure-change transaction";
                  "block_id" => %tenure_end_block.block_id());
            return Err(NetError::InvalidMessage);
        };

        // tc_payload must point to the tenure-start block's header
        if tc_payload.prev_tenure_consensus_hash != tenure_start_block.header.consensus_hash {
            warn!("Invalid tenure-end block: tenure-change does not point to tenure-start block";
                  "start_block_id" => %tenure_start_block.block_id(),
                  "end_block_id" => %tenure_end_block.block_id(),
                  "tc_payload.prev_tenure_consensus_hash" => %tc_payload.prev_tenure_consensus_hash,
                  "tenure_start.consensus_hash" => %tenure_start_block.header.consensus_hash);
            return Err(NetError::InvalidMessage);
        }

        debug!(
            "Accepted tenure-end block for tenure {} block={}; expect {} blocks",
            &self.tenure_id_consensus_hash,
            &tenure_end_block.block_id(),
            tc_payload.previous_tenure_blocks
        );
        self.tenure_end_block = Some(tenure_end_block.clone());
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader.rs (L332-340)
```rust
    pub fn tenure_length(&self) -> Option<u64> {
        self.tenure_end_block.as_ref().and_then(|tenure_end_block| {
            let Some(tc_payload) = tenure_end_block.try_get_tenure_change_payload() else {
                return None;
            };

            Some(u64::from(tc_payload.previous_tenure_blocks))
        })
    }
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader.rs (L407-426)
```rust
            expected_block_id = &block.header.parent_block_id;
            count += 1;
            if self
                .tenure_blocks
                .as_ref()
                .map(|blocks| blocks.len())
                .unwrap_or(0)
                .saturating_add(count)
                > self.tenure_length().unwrap_or(0).saturating_add(1) as usize
            // + 1 due to the inclusion of the tenure-end block
            {
                // there are more blocks downloaded than indicated by the end-blocks tenure-change
                // transaction.
                warn!("Invalid blocks: exceeded {} tenure blocks", self.tenure_length().unwrap_or(0);
                      "tenure_id" => %self.tenure_id_consensus_hash,
                      "count" => %count,
                      "tenure_length" => self.tenure_length().unwrap_or(0),
                      "num_blocks" => tenure_blocks.len());
                return Err(NetError::InvalidMessage);
            }
```
