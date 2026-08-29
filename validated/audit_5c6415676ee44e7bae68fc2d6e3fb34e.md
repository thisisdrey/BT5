### Title
Resharding double-counts delayed receipt gas in both child shards' `CongestionInfo`, causing spurious transaction rejection on both children - ([File: chain/chain/src/resharding/manager.rs])

### Finding Description
`ReshardingManager::get_child_congestion_info_not_finalized` derives each child's post-split `CongestionInfo` from the parent's. For `RetainMode::Left` it returns the parent's `CongestionInfo` verbatim, including `delayed_receipts_gas`: [1](#0-0) 

For `RetainMode::Right`, it only subtracts the parent's *buffered* receipt gas/bytes (via `ReceiptGroupsQueue`) but performs no adjustment to `delayed_receipts_gas` at all: [2](#0-1) 

This matches the underlying trie layout: `DELAYED_RECEIPT_OR_INDICES` keys are copied verbatim into both children's tries via `get_interval_for_copy_to_both_children`, since delayed receipts have no account-id prefix to split on: [3](#0-2) 

As a result, both the left and right children end up reporting the parent's full `delayed_receipts_gas` value in their `CongestionInfo`, even though the actual delayed receipts destined for each child will later be filtered by `receiver_shard_id` when the delayed queue is processed. The sum of the two children's reported `delayed_receipts_gas` is therefore `2x` the true combined workload immediately after the split.

This inflated value feeds directly into `CongestionControl::shard_accepts_transactions`, which computes `incoming_congestion` from `delayed_receipts_gas` and rejects transactions once a threshold is exceeded: [4](#0-3) 

Since congestion info is part of the committed chunk state (`ChunkExtra`) that is set from `get_child_congestion_info`/`get_child_congestion_info_not_finalized` output at split time, this discrepancy is deterministic and visible to all nodes/RPCs immediately following the split, not a transient local artifact.

An attacker (unprivileged, ordinary account) can, prior to a scheduled resharding boundary, flood the target shard with many receipts destined to accounts on that shard, driving them into the delayed receipt queue and inflating the parent's `delayed_receipts_gas`. When the split occurs, both resulting child shards inherit this same inflated value in full, doubling the perceived total delayed backlog versus the real per-child backlog that will only become apparent once the delayed queue is actually drained and filtered by `receiver_shard_id`.

No existing check corrects this: `finalize_allowed_shard` only sets the "allowed shard" field, not `delayed_receipts_gas`, and there is no subtraction logic for delayed gas anywhere in the resharding path.

### Impact Explanation
The bug causes ordinary users' transactions destined to either child shard to be incorrectly rejected by `shard_accepts_transactions` (`RejectTransactionReason::IncomingCongestion`) for a period following resharding, even though the true per-shard delayed backlog is much lower. This is a temporary, network-wide availability/DoS effect against both post-split shards for all senders, timed and amplified by an attacker who understands the real (lower) congestion and can selectively continue transacting while others are rejected. It does not cause fund loss, inflation, or consensus divergence — congestion info is deterministically computed by all nodes the same way, so it does not cause a state-root/chain split, only degraded liveness/availability for transaction acceptance.

### Likelihood Explanation
Preconditions require the attacker to know (or guess) the timing of a scheduled resharding boundary account/epoch and to flood the pre-split shard with enough receipts to push a meaningful amount into the delayed queue, which costs ordinary attached gas/fees for the flooding transactions. Resharding events are rare and typically publicly scheduled/observable in advance (protocol upgrade voting), making the timing predictable. The flooding itself uses only standard unprivileged transaction submission — no special access is needed. Given knowledge of scheduling, this is straightforward and repeatable for each resharding event.

### Recommendation
When computing `get_child_congestion_info_not_finalized`, split `delayed_receipts_gas` (and correspondingly `delayed_receipts_bytes`/count metadata, if tracked) between the two children based on which receipts in the delayed queue actually target each child's shard (using the same `receiver_shard_id` splitting logic the runtime later applies in `DelayedReceiptQueueWrapper::pop`), instead of copying the parent value verbatim to both sides. At minimum, avoid keeping the full parent value on both children; deduct the counterpart's share so the totals reflect the true combined workload rather than double-counting it.

### Proof of Concept
Unit test in `chain/chain/src/resharding/manager.rs` (or a dedicated test module):
1. Construct a parent trie state with N delayed receipts, roughly split by destination account between what will become the left and right child shard ranges after a chosen boundary account.
2. Construct a `parent_congestion_info` with `delayed_receipts_gas` equal to the sum of gas of all N receipts.
3. Call `ReshardingManager::get_child_congestion_info` for `RetainMode::Left` and `RetainMode::Right` with the same parent congestion info and trie.
4. Assert `left.delayed_receipts_gas() == parent.delayed_receipts_gas()` and `right.delayed_receipts_gas() == parent.delayed_receipts_gas()`, i.e. `left.delayed_receipts_gas() + right.delayed_receipts_gas() == 2 * parent.delayed_receipts_gas()`, demonstrating the double count instead of the expected split matching each child's actual delayed receipts (verifiable by separately filtering the N receipts by `receiver_shard_id` per `DelayedReceiptQueueWrapper::pop` semantics and summing their gas per child).
5. Optionally extend with an integration/test-loop scenario (`test-loop-tests/src/tests/*resharding*`) that performs an actual split and then checks `shard_accepts_transactions` on both children immediately post-split against known real per-child delayed gas, showing spurious `IncomingCongestion` rejections.

### Citations

**File:** chain/chain/src/resharding/manager.rs (L333-337)
```rust
        // The left child contains all the delayed and buffered receipts from the
        // parent so it should have identical congestion info.
        if retain_mode == RetainMode::Left {
            return Ok(parent_congestion_info);
        }
```

**File:** chain/chain/src/resharding/manager.rs (L339-364)
```rust
        // The right child contains all the delayed receipts from the parent but it
        // has no buffered receipts. It's info needs to be computed by subtracting
        // the parent's buffered receipts from the parent's congestion info.
        let mut congestion_info = parent_congestion_info;
        for shard_id in parent_shard_layout.shard_ids() {
            let receipt_groups = ReceiptGroupsQueue::load(parent_trie, shard_id)?;
            let Some(receipt_groups) = receipt_groups else {
                continue;
            };

            let bytes = receipt_groups.total_size();
            let gas = receipt_groups.total_gas();

            congestion_info
                .remove_buffered_receipt_gas(gas)
                .expect("Buffered gas must not exceed congestion info buffered gas");
            congestion_info
                .remove_receipt_bytes(bytes)
                .expect("Buffered size must not exceed congestion info buffered size");
        }

        // The right child does not inherit any buffered receipts. The
        // congestion info must match this invariant.
        assert_eq!(congestion_info.buffered_receipts_gas(), 0);

        Ok(congestion_info)
```

**File:** core/store/src/trie/ops/resharding.rs (L100-108)
```rust
// This function generates the range of keys that need to be retained in the both children.
// This includes trie keys related to delayed receipts, promise yield and bandwidth scheduler.
//
// Suppose the trie key has u8 value K
// Left child interval: [K, K+1)
// Right child interval: [K, K+1)
fn get_interval_for_copy_to_both_children(prefix: u8) -> Range<Vec<u8>> {
    vec![prefix]..vec![prefix + 1]
}
```

**File:** core/primitives/src/congestion_info.rs (L123-151)
```rust
    pub fn shard_accepts_transactions(&self) -> ShardAcceptsTransactions {
        let incoming_congestion = self.incoming_congestion();
        let outgoing_congestion = self.outgoing_congestion();
        let memory_congestion = self.memory_congestion();
        let missed_chunks_congestion = self.missed_chunks_congestion();

        let congestion_level = incoming_congestion
            .max(outgoing_congestion)
            .max(memory_congestion)
            .max(missed_chunks_congestion);

        // Convert to NotNan here, if not possible, the max above is already meaningless.
        let congestion_level =
            NotNan::new(congestion_level).unwrap_or_else(|_| NotNan::new(1.0).unwrap());
        if *congestion_level < self.config.reject_tx_congestion_threshold {
            return ShardAcceptsTransactions::Yes;
        }

        let reason = if missed_chunks_congestion >= *congestion_level {
            RejectTransactionReason::MissedChunks { missed_chunks: self.missed_chunks_count }
        } else if incoming_congestion >= *congestion_level {
            RejectTransactionReason::IncomingCongestion { congestion_level }
        } else if outgoing_congestion >= *congestion_level {
            RejectTransactionReason::OutgoingCongestion { congestion_level }
        } else {
            RejectTransactionReason::MemoryCongestion { congestion_level }
        };
        ShardAcceptsTransactions::No(reason)
    }
```
