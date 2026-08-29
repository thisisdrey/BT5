### No vulnerability found for this question.

**Analysis summary:**

The resharding-specific path (`ReshardingManager::get_child_congestion_info` → `finalize_allowed_shard` → `CongestionInfo::finalize_allowed_shard` → `get_new_allowed_shard`) is internally consistent with respect to the *new* (child) shard layout at every step:

- `all_shards` is always derived from `child_shard_layout.shard_ids()`, and `own_shard_index` is always computed via `child_shard_layout.get_shard_index(own_shard)` — both always reference the new layout, never a mix of old/new numbering. [1](#0-0) 

- `get_new_allowed_shard` computes the index via `congestion_seed.checked_rem(all_shards.len())`, which guarantees `index < all_shards.len()` before the `.get(index)` call, making the subsequent `.expect(...)` provably unreachable (it's a defensive assertion, not an exploitable panic path). [2](#0-1) 

Because the returned `allowed_shard` is always selected from `all_shards`, which is itself built from the new shard layout's `shard_ids()`, the value returned is guaranteed to be a valid shard id in the new layout — there is no code path where an "old-layout shard_index" leaks into this computation. The comment in `finalize_allowed_shard` acknowledges the seed differs from normal runtime operation (sum of shard index + block height) but this only affects *which* shard is picked deterministically, not whether the picked shard is valid. [3](#0-2) 

Separately, `bootstrap_congestion_info` in `congestion_control.rs` is an unrelated fallback path used when no congestion info exists in the chunk header (e.g. genesis/IO fallback), not part of the resharding child-congestion-info construction, so it is not on the path described in the question.

Since the indexing logic is provably safe by construction (`checked_rem` bounds the index before use) and the shard-id source is always consistent with the new layout, there is no reachable index-out-of-bounds panic here, and this is not something an unprivileged transaction-submitting attacker could trigger through congestion manipulation alone — congestion levels affect *which* valid shard is chosen, not *whether* a valid shard exists.

### Citations

**File:** chain/chain/src/resharding/manager.rs (L367-388)
```rust
    fn finalize_allowed_shard(
        child_shard_layout: &ShardLayout,
        child_shard_uid: &ShardUId,
        congestion_info: &mut CongestionInfo,
    ) -> Result<(), Error> {
        let all_shards = child_shard_layout.shard_ids().collect_vec();
        let own_shard = child_shard_uid.shard_id();
        let own_shard_index = child_shard_layout
            .get_shard_index(own_shard)?
            .try_into()
            .expect("ShardIndex must fit in u64");
        // Please note that the congestion seed used during resharding is
        // different than the one used during normal operation. In runtime the
        // seed is set to the sum of shard index and block height. The block
        // height isn't easily available on all call sites which is why the
        // simplified seed is used. This is valid because it's deterministic and
        // resharding is a very rare event. However in a perfect world it should
        // be the same.
        // TODO - Use proper congestion control seed during resharding.
        let congestion_seed = own_shard_index;
        congestion_info.finalize_allowed_shard(own_shard, &all_shards, congestion_seed);
        Ok(())
```

**File:** core/primitives/src/congestion_info.rs (L370-384)
```rust
    fn get_new_allowed_shard(
        own_shard: ShardId,
        all_shards: &[ShardId],
        congestion_seed: u64,
    ) -> ShardId {
        if let Some(index) = congestion_seed.checked_rem(all_shards.len() as u64) {
            // round robin for other shards based on the seed
            return *all_shards
                .get(index as usize)
                .expect("`checked_rem` should have ensured array access is in bound");
        }
        // checked_rem failed, hence all_shards.len() is 0
        // own_shard is the only choice.
        return own_shard;
    }
```
