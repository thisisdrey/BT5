### Title
Buffered receipts stranded forever when a shard's outgoing buffer to a retired parent shard fails to drain before a second, unrelated resharding overwrites `ShardLayoutV3::last_split` - ([File: runtime/runtime/src/congestion_control.rs])

### Finding Description
`ReceiptSinkV2Info::new` computes the set of "parent" shard ids that must still be drained every chunk as `shard_layout.get_split_parent_shard_ids()` [1](#0-0) . `ReceiptSinkV2WithInfo::forward_from_buffer` then only ever calls `forward_from_buffer_to_shard` for shard ids in `self.info.parent_shard_ids` plus the current epoch's `shard_layout.shard_ids()` [2](#0-1) . Any receiver shard id that is neither a "current" shard nor in `parent_shard_ids` is never drained again.

For `ShardLayoutV3`, `get_split_parent_shard_ids()` does **not** return the full historical set of retired parent shards - it returns only the single most recent split's parent (`last_split`):
```
ShardLayout::V3(v3) => return BTreeSet::from([v3.last_split]),
``` [3](#0-2) 
`last_split` is overwritten every time *any* shard in the network splits (`derive_impl` sets `last_split` to the newly split shard) [4](#0-3) .

An outgoing receipt buffer entry is keyed only by the destination shard id at the time it was buffered: `TrieKey::BufferedReceipt { receiving_shard, index }` [5](#0-4) . If shard A has receipts buffered toward shard B, and B splits into B1/B2, A keeps forwarding out of the old `receiving_shard = B` bucket via the `parent_shard_ids` mechanism, exactly as the code comment documents: "the buffer shard id may be different than the target shard if for a short period of time after resharding" [6](#0-5) . This is a *transient* mechanism that assumes the buffer drains before it's needed again — confirmed by the still-open `TODO(resharding) - remove the parent outgoing buffer once it's empty` [7](#0-6) .

If B remains bandwidth/congestion-limited long enough that A's buffer-to-B is not fully drained before a **second, unrelated** shard split occurs anywhere in the network (which overwrites `last_split` to point at the new split's parent, e.g. shard C), then in the very next epoch `parent_shard_ids` becomes `{C}` only — B drops out of the set entirely, and B is also absent from `shard_layout.shard_ids()` since it's retired. From that point on, no code path ever calls `forward_from_buffer_to_shard(B, ...)` on shard A again. The receipts (and any attached NEAR deposits) sitting under `TrieKey::BufferedReceipt{receiving_shard: B, ...}` in shard A's own trie state become permanently unreachable — they are never forwarded, never refunded, and never physically removed (shard A's own state is unaffected by `gc_parent_shard_after_resharding`, which only deletes the *retired shard's own* trie prefix, not other shards' buffers referencing it) [8](#0-7) . In addition, `own_congestion_info.buffered_receipts_gas()`/`receipt_bytes` for shard A are permanently inflated by the stranded entries since the decrement only happens inside `forward_from_buffer_to_shard`'s `Forwarded` branch [9](#0-8) , which never runs for B again — a lingering false congestion signal on shard A.

An unprivileged attacker's contribution is simply to keep a targeted receiver shard congested/bandwidth-limited (e.g. via cheap large receipts or exploiting a temporarily fully-congested `allowed_shard`) long enough that some of the deposits they sent land in another shard's outgoing buffer at the moment that receiver shard splits, and remain undrained until a second, unrelated split happens on the network. No validator/node privileges are required — only ordinary funded transactions with deposits targeting a soon-to-split, congested shard.

### Impact Explanation
This is a permanent freezing-of-funds bug: attacker (or any user's) deposits attached to receipts stuck in a stale `receiving_shard` buffer bucket are never delivered, never refunded, and never garbage collected — they are simply orphaned in the sender shard's trie forever, unreachable by any code path. It also corrupts the sender shard's `CongestionInfo` permanently (inflated `buffered_receipts_gas`/`receipt_bytes`), degrading throughput. This matches the "permanent freezing of user funds" bounty category. It is a protocol/runtime correctness bug, not a validator-misconfiguration or epoch-manager-only issue, since the root cause is `ShardLayoutV3::get_split_parent_shard_ids()`'s single-slot `last_split` tracking used by `congestion_control.rs`.

### Likelihood Explanation
This requires: (1) dynamic resharding to be active (already the case at stable protocol version per repo docs), (2) a receiver shard to be persistently congested/bandwidth-starved for long enough that some cross-shard buffered receipts are not drained before that shard splits, and (3) a second, entirely unrelated shard split elsewhere in the network to occur before the stale buffer fully drains. Preconditions (2) and (3) are outside direct attacker control (dynamic resharding timing depends on organic shard memory growth across the whole network), making this a systemic/timing-dependent bug rather than an instantly-repeatable exploit. However, as the network grows and shards split more frequently, the window in which condition (3) can occur before (2) resolves shrinks, and a resourceful attacker can amplify (2) by flooding a target shard with buffered receipts (paying only gas + the deposit amount they intend to eventually strand) to make full drainage before the next unrelated split less likely. The attack path is entirely reachable from an ordinary account submitting standard funded transactions; no validator/protocol-level access is needed.

### Recommendation
Change `ShardLayoutV3::get_split_parent_shard_ids()` (or the call site in `ReceiptSinkV2Info::new`) to return the full set of ancestor shard ids still referenced by any live buffer, not just `last_split`. `ShardLayoutV3` already tracks full lineage via `shards_ancestor_map`/`ancestor_uids`; `parent_shard_ids` for congestion-control draining purposes should be derived from the accumulated `shards_split_map` (or `ancestor_uids` of every current shard) rather than a single most-recent-split scalar, so that `forward_from_buffer` keeps attempting to drain every historically-retired `receiving_shard` bucket until it is actually empty, matching the intent of the still-open TODO at `congestion_control.rs:337`.

### Proof of Concept
Test-loop integration test outline:
1. Build a 3+ shard `ShardLayoutV3` network; deploy a contract on account in shard A.
2. Configure receiver shard B to be persistently bandwidth/congestion-limited (as in `slow_test_resharding_v3_large_receipts_towards_splitted_shard` / `slow_test_resharding_v3_buffered_receipts_towards_splitted_shard`, e.g. via `limit_outgoing_gas(true)` and large cross-shard receipts), and from shard A send several large deposit-carrying receipts toward accounts in B such that some remain in A's outgoing buffer keyed `receiving_shard = B`.
3. Trigger the resharding boundary that splits B into B1/B2 while A's buffer-to-B is still non-empty (assert via `ShardsOutgoingReceiptBuffer::load(&trie).to_shard(B).len() > 0` right after the split, similar to `check_receipts_presence_after_resharding_block`).
4. Instead of letting B1/B2 fully drain, force a **second, independent** resharding split elsewhere in the layout (e.g. split an unrelated shard C) before A's buffer-to-B empties, so that the new epoch's `ShardLayoutV3::last_split` becomes C.
5. Run additional epochs (well past `gc_num_epochs_to_keep`) and assert:
   - `ShardsOutgoingReceiptBuffer::load(&trie_for_A).to_shard(B).len()` remains non-zero forever (never reaches 0), proving the stale receipts are never forwarded.
   - The deposit amounts attached to those buffered receipts never appear as a balance change on any account in B1/B2 (i.e., funds are neither delivered nor refunded).
   - `own_congestion_info.buffered_receipts_gas()` for shard A remains permanently non-zero even though no new receipts are buffered, confirming the accounting divergence.

### Citations

**File:** runtime/runtime/src/congestion_control.rs (L222-230)
```rust
impl ReceiptSinkV2Info {
    pub(crate) fn new(
        epoch_id: EpochId,
        epoch_info_provider: &dyn EpochInfoProvider,
    ) -> Result<Self, near_primitives::errors::EpochError> {
        let shard_layout = epoch_info_provider.shard_layout(&epoch_id)?;
        let parent_shard_ids = shard_layout.get_split_parent_shard_ids();
        Ok(ReceiptSinkV2Info { epoch_id, shard_layout, parent_shard_ids })
    }
```

**File:** runtime/runtime/src/congestion_control.rs (L253-279)
```rust
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

        // Then forward receipts from the outgoing buffers of the shard in the
        // current shard layout.
        for shard_id in self.info.shard_layout.shard_ids() {
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

**File:** runtime/runtime/src/congestion_control.rs (L328-338)
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
    fn forward_from_buffer_to_shard(
```

**File:** runtime/runtime/src/congestion_control.rs (L367-373)
```rust
                ReceiptForwarding::Forwarded => {
                    self.own_congestion_info.remove_receipt_bytes(size)?;
                    self.own_congestion_info.remove_buffered_receipt_gas(gas.as_gas().into())?;
                    if should_update_outgoing_metadatas {
                        // Can't update metadatas immediately because state_update is borrowed by iterator.
                        outgoing_metadatas_updates.push((ByteSize::b(size), gas));
                    }
```

**File:** core/primitives/src/shard_layout/mod.rs (L400-424)
```rust
    /// Returns all the shards from the previous shard layout that were
    /// split into multiple shards in this shard layout.
    pub fn get_split_parent_shard_ids(&self) -> BTreeSet<ShardId> {
        // V3 doesn't store shards which weren't split in the map, so we can return early.
        // Using explicit match to force handling a new shard layout version when it's added.
        match self {
            ShardLayout::V0(_) | ShardLayout::V1(_) | ShardLayout::V2(_) => {}
            ShardLayout::V3(v3) => return BTreeSet::from([v3.last_split]),
        }

        let mut parent_shard_ids = BTreeSet::new();
        for shard_id in self.shard_ids() {
            let parent_shard_id = self
                .try_get_parent_shard_id(shard_id)
                .expect("shard_id belongs to the shard layout");
            let Some(parent_shard_id) = parent_shard_id else {
                continue;
            };
            if parent_shard_id == shard_id {
                continue;
            }
            parent_shard_ids.insert(parent_shard_id);
        }
        parent_shard_ids
    }
```

**File:** core/primitives/src/shard_layout/v3.rs (L258-282)
```rust
    fn derive_impl(
        mut shard_ids: Vec<ShardId>,
        mut boundary_accounts: Vec<AccountId>,
        new_boundary_account: AccountId,
        mut shards_split_map: ShardsSplitMapV3,
    ) -> Result<Self, ShardLayoutError> {
        let Err(new_boundary_idx) = boundary_accounts.binary_search(&new_boundary_account) else {
            return Err(ShardLayoutError::DuplicateBoundaryAccount {
                account_id: new_boundary_account,
            });
        };
        boundary_accounts.insert(new_boundary_idx, new_boundary_account);

        let max_shard_id =
            *shard_ids.iter().max().expect("there should always be at least one shard");
        let new_shards = vec![max_shard_id + 1, max_shard_id + 2];

        let [last_split] = shard_ids
            .splice(new_boundary_idx..new_boundary_idx + 1, new_shards.clone())
            .collect_array()
            .expect("should only splice one shard");
        shards_split_map.insert(last_split, new_shards);

        Ok(Self::new(boundary_accounts, shard_ids, shards_split_map, last_split))
    }
```

**File:** core/store/src/trie/receipts_column_helper.rs (L309-333)
```rust
impl TrieQueue for OutgoingReceiptBuffer<'_> {
    type Item<'a> = ReceiptOrStateStoredReceipt<'a>;

    fn load_indices(&self, trie: &dyn TrieAccess) -> Result<TrieQueueIndices, StorageError> {
        let all_indices: BufferedReceiptIndices =
            get(trie, &TrieKey::BufferedReceiptIndices)?.unwrap_or_default();
        let indices = all_indices.shard_buffers.get(&self.shard_id).cloned().unwrap_or_default();
        Ok(indices)
    }

    fn indices(&self) -> TrieQueueIndices {
        self.parent.shards_indices.shard_buffers.get(&self.shard_id).cloned().unwrap_or_default()
    }

    fn indices_mut(&mut self) -> &mut TrieQueueIndices {
        self.parent.shards_indices.shard_buffers.entry(self.shard_id).or_default()
    }

    fn write_indices(&self, state_update: &mut TrieUpdate) {
        self.parent.write_indices(state_update);
    }

    fn trie_key(&self, index: u64) -> TrieKey {
        TrieKey::BufferedReceipt { index, receiving_shard: self.shard_id }
    }
```

**File:** chain/chain/src/garbage_collection.rs (L1199-1243)
```rust
fn gc_parent_shard_after_resharding(
    chain_store_update: &mut ChainStoreUpdate,
    epoch_manager: &dyn EpochManagerAdapter,
    block_hash: &CryptoHash,
) -> Result<(), Error> {
    // Clear out state for the parent shard. Note that this function is called at every epoch boundary,
    // even if there is no resharding.
    // It's fine to do that as after the first call to `trie_store_update.delete_shard_uid_prefixed_state`
    // the rest of the calls in future epochs are no-ops.
    if !epoch_manager.is_last_block_in_finished_epoch(block_hash)? {
        return Ok(());
    }

    tracing::debug!(target: "garbage_collection", ?block_hash, "resharding state cleanup");
    // Given block_hash is the resharding block, shard_layout is the shard layout of the next epoch
    // Important: We are not allowed to call `epoch_manager.get_shard_layout_from_prev_block()` as
    // the function relies on `self.get_block_info(block_info.epoch_first_block())` but epoch_first_block
    // has already been cleaned up.
    // We instead need to rely on chain_store to get the next block hash and use the block_info to get
    // the next epoch id and shard layout.
    let store = chain_store_update.store();
    let next_block_hash = store.chain_store().get_next_block_hash(block_hash)?;
    let next_epoch_id = epoch_manager.get_epoch_id(&next_block_hash)?;
    let shard_layout = epoch_manager.get_shard_layout(&next_epoch_id)?;
    let mut trie_store_update = store.trie_store().store_update();
    for parent_shard_uid in shard_layout.get_split_parent_shard_uids() {
        // Check if any child shard still map to this parent shard
        let children_shards =
            shard_layout.get_children_shards_uids(parent_shard_uid.shard_id()).unwrap();
        let has_active_mapping = children_shards.into_iter().any(|child_shard_uid| {
            let mapped_shard_uid = maybe_get_shard_uid_mapping(&store, child_shard_uid);
            mapped_shard_uid.as_ref() == Some(&parent_shard_uid)
        });
        if !has_active_mapping {
            // Delete the state of the parent shard
            tracing::debug!(target: "garbage_collection", ?parent_shard_uid, "resharding state cleanup for shard");
            trie_store_update.delete_shard_uid_prefixed_state(parent_shard_uid);
        } else {
            tracing::debug!(target: "garbage_collection", ?parent_shard_uid, "skipping parent shard cleanup - active mappings exist");
        }
    }

    chain_store_update.merge(trie_store_update.into());
    Ok(())
}
```
