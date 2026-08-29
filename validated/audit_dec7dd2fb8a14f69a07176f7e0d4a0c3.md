### Title
`get_child_congestion_info_not_finalized` iterates only current-layout shard ids, missing stale receiver-shard `ReceiptGroupsQueue` entries left by prior reshardings, causing a deterministic chain-halting assert panic - ([File: chain/chain/src/resharding/manager.rs])

### Summary
`get_child_congestion_info_not_finalized` subtracts buffered-receipt gas/bytes from the parent's `CongestionInfo` by iterating only `parent_shard_layout.shard_ids()`, then asserts the residual buffered gas is exactly zero. If the splitting shard's trie still contains a `ReceiptGroupsQueue` keyed by a receiver-shard id that has since been retired by an unrelated, earlier resharding of another shard, that entry is skipped by the loop (since retired ids are absent from `shard_ids()`), leaving a nonzero residual and tripping the `assert_eq!` on every node applying the split.

### Finding Description
`Self::get_child_congestion_info_not_finalized` (`chain/chain/src/resharding/manager.rs:327-365`) computes the right child's congestion info for a shard split (`RetainMode::Right`) by walking every id returned from `parent_shard_layout.shard_ids()`, loading `ReceiptGroupsQueue::load(parent_trie, shard_id)` for each, and subtracting `total_gas()`/`total_size()` from the aggregated `CongestionInfo`: [1](#0-0) 
It finishes with a hard invariant: [2](#0-1) 

The codebase itself documents that outgoing-buffer entries can persist keyed by a *retired* parent shard id after a prior, unrelated resharding, and explicitly special-cases this in the runtime's draining logic: [3](#0-2) 
`ReceiptSinkV2WithInfo` tracks a separate `parent_shard_ids: BTreeSet<ShardId>` (`runtime/runtime/src/congestion_control.rs:46`) precisely because such stale receiver-shard buffers are *not* enumerable from the current `ShardLayout::shard_ids()`: [4](#0-3) 

`get_child_congestion_info_not_finalized`, however, has no equivalent tracking of retired-but-still-referenced receiver shard ids — it relies solely on `parent_shard_layout.shard_ids()`, which by construction (`ShardLayoutV3::derive_impl`, `core/primitives/src/shard_layout/v3.rs:258-282`) excludes any shard id that was retired by a split, even though the split map/ancestor history is preserved elsewhere. Shard ids are never reused (`max_shard_id + 1`, `max_shard_id + 2`), so once shard C is retired by its own split, no future `shard_ids()` iteration will ever revisit id `C` — yet shard A's outgoing-buffer trie (a completely separate shard's state) may still hold an undrained `ReceiptGroupsQueue` keyed by `C` if A never had enough forwarding bandwidth to fully drain it (`TODO(resharding) - remove the parent outgoing buffer once it's empty`, `runtime/runtime/src/congestion_control.rs:337`).

Attack flow: an unprivileged user sends many cross-shard receipts destined for shard C such that congestion causes them to sit in shard A's outgoing buffer to C (`ReceiptSinkV2::buffer_receipt`) instead of being forwarded immediately. Later shard C undergoes an independent resharding (dynamic resharding is a protocol-level, trie-memory-driven process not controlled by the attacker, but which the attacker can influence by growing C's memory). Shard A's stale `ReceiptGroupsQueue` for the now-retired id `C` remains in A's trie, undrained, because A's own bandwidth to C was throttled. When A itself is later selected for a split, `get_child_congestion_info_not_finalized` iterates `parent_shard_layout.shard_ids()` (the layout at the time A splits, which no longer contains retired id `C`), so `ReceiptGroupsQueue::load(parent_trie, C)`'s gas/bytes are never subtracted, and `assert_eq!(congestion_info.buffered_receipts_gas(), 0)` fails deterministically on every node validating/applying A's split.

No existing signature/nonce/access-key/gas check prevents this — the attacker only needs ordinary transactions that create buffered (not immediately forwarded) cross-shard receipts, which is a normal congestion-control code path, not a privileged one.

### Impact Explanation
A failed `assert_eq!` in shard-apply logic reached deterministically by every honest node tracking the affected child shard causes a synchronized panic — a chain halt requiring manual intervention (a code fix and re-deployment), matching the "shard-halting panic" bounty category. Because the computation is on the state-transition/chunk-application hot path used both by chunk producers and stateless chunk validators (`chain/chain/src/stateless_validation/chunk_validation.rs`), the panic is consensus-breaking rather than merely local.

### Likelihood Explanation
Exploitation requires no privileged access — only ordinary transactions/receipts causing sustained congestion toward a target shard. However, it is not trivially reliable: it requires (1) a receiver shard C to accumulate enough congestion-driven buffering from sender shard A that the queue is not fully drained, (2) shard C's own resharding to occur (a dynamic, non-attacker-controlled decision based on trie memory thresholds and cooldowns), and (3) shard A's own subsequent resharding to occur before A finishes draining the stale queue to the now-retired id. These are compounding, timing-dependent, network-wide conditions outside full attacker control, making the scenario plausible under dynamic resharding but not trivially/cheaply reproducible on demand; it depends on natural resharding cadence and cross-shard congestion dynamics that the attacker can only nudge, not force deterministically.

### Recommendation
When computing `get_child_congestion_info_not_finalized`, do not rely solely on `parent_shard_layout.shard_ids()`. Instead, either (a) explicitly track/persist the set of all receiver-shard ids that ever had a `ReceiptGroupsQueue` created for a given sending shard (independent of shard-layout membership), and iterate that persisted set when computing the residual, or (b) walk the full split/ancestor history reachable from the parent layout (similar to `ReceiptSinkV2Info::parent_shard_ids`) to also load and subtract queues keyed by retired ancestor shard ids before asserting the invariant.

### Proof of Concept
Unit test in `chain/chain/src/resharding/manager.rs` (or `core/store/src/trie/outgoing_metadata.rs`):
1. Build a `TrieUpdate`/`ParentTrie` for a shard `A`.
2. Directly seed a `ReceiptGroupsQueue` for a `receiver_shard` id `C` (using `ReceiptGroupsQueue::new(C)` + `update_on_receipt_pushed`) and commit it into the trie, simulating a stale buffer left over from a prior, unrelated resharding.
3. Construct a `parent_shard_layout` (e.g. via `ShardLayout::multi_shard`/`derive_v3`) whose `shard_ids()` does **not** include `C` (simulating that `C` was already retired by an earlier split of a different shard).
4. Set `parent_congestion_info.buffered_receipts_gas()` to reflect the seeded gas from queue `C`.
5. Call `ChainStore::get_child_congestion_info_not_finalized`-equivalent (or the public wrapper `get_child_congestion_info`) with `RetainMode::Right`.
6. Expected (buggy) result: the `assert_eq!(congestion_info.buffered_receipts_gas(), 0)` at `manager.rs:362` panics because queue `C`'s gas was never subtracted, confirming the residual-accounting mismatch.

### Citations

**File:** chain/chain/src/resharding/manager.rs (L342-358)
```rust
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
```

**File:** chain/chain/src/resharding/manager.rs (L360-362)
```rust
        // The right child does not inherit any buffered receipts. The
        // congestion info must match this invariant.
        assert_eq!(congestion_info.buffered_receipts_gas(), 0);
```

**File:** runtime/runtime/src/congestion_control.rs (L242-266)
```rust

        // There mustn't be any shard ids in both the parents and the current
        // shard ids. If this happens the same buffer will be processed twice.
        debug_assert!(
            self.info
                .parent_shard_ids
                .intersection(&self.info.shard_layout.shard_ids().collect())
                .count()
                == 0
        );

        let mut all_buffers_empty = true;

        // First forward any receipts that may still be in the outgoing buffers
        // of the parent shards.
        for &shard_id in &self.info.parent_shard_ids {
            self.sink.forward_from_buffer_to_shard(
                shard_id,
                state_update,
                apply_state,
                &self.info.shard_layout,
            )?;
            let is_buffer_empty = self.sink.outgoing_buffers.to_shard(shard_id).len() == 0;
            all_buffers_empty &= is_buffer_empty;
        }
```

**File:** runtime/runtime/src/congestion_control.rs (L328-337)
```rust
impl ReceiptSinkV2 {
    /// Forward receipts from the outgoing buffer of buffer_shard_id to the
    /// outgoing receipts as much as the limits allow.
    ///
    /// Please note that the buffer shard id may be different than the target
    /// shard if for a short period of time after resharding. That is because
    /// some shards may have receipts for the parent shard that no longer exists
    /// and those receipts need to be forwarded to either of the child shards.
    ///
    /// TODO(resharding) - remove the parent outgoing buffer once it's empty.
```
