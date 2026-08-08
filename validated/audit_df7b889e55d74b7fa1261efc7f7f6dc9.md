## Finding Verdict

Based on the code in `core/src/repair/repair_weight.rs`, the claim is **valid** as a real algorithmic-complexity issue in `get_best_unknown_last_index` and `get_best_closest_completion`.

### Title
`get_best_unknown_last_index` / `get_best_closest_completion` iterate all orphan trees regardless of repair caps, allowing attacker-controlled tree count to drive blockstore I/O cost per repair tick - (File: `core/src/repair/repair_weight.rs`)

### Summary
`get_best_unknown_last_index` and `get_best_closest_completion` both loop over `self.trees.values()`, checking the `max_new_repairs`/`limit` cap only *before* processing each tree, not based on total work done. If each tree yields zero (or few) new repairs — e.g., because slots are already in `processed_slots` or have no missing-last-index/incomplete slots to report — the loop keeps iterating through every entry in `self.trees`, invoking `get_unknown_last_index`/`get_closest_completion` (each doing `GenericTraversal` plus `blockstore.meta_repair`/`get_index` calls per unseen slot) once per tree.

### Finding Description
In `RepairWeight::get_best_unknown_last_index` [1](#0-0) :
```rust
for tree in self.trees.values() {
    if repairs.len() >= max_new_repairs {
        break;
    }
    let new_repairs = get_unknown_last_index(...);
    repairs.extend(new_repairs);
}
```
and `RepairWeight::get_best_closest_completion` [2](#0-1)  have the identical pattern. The cap (`max_new_repairs`) only gates whether the *next* tree is visited, but does nothing to bound the number of trees visited when each tree contributes 0 new repairs. Each per-tree call (`get_unknown_last_index`/`get_closest_completion` in `core/src/repair/repair_generic_traversal.rs` lines 52-94 and 126-221) performs a `GenericTraversal` over that tree's slots and, for any slot not already in `processed_slots`, issues a `blockstore.meta_repair(slot)` lookup (and potentially `blockstore.get_index(slot)`), i.e., real RocksDB/blockstore I/O [3](#0-2) [4](#0-3) .

Orphan trees are created and tracked in `self.trees` whenever an unrooted orphan slot is discovered (via `split_off`/`update_orphan_ancestors`/`get_best_orphans`'s `blockstore.orphans_iterator`) [5](#0-4) . An unprivileged remote sender can push minimal shreds for slots whose parent is unknown/absent to the leader's TPU/turbine-adjacent shred-insert path, causing blockstore to register them as orphans; each distinct orphan slot becomes its own tree root entry in `self.trees` [6](#0-5) . Because these trees can be shallow (one slot each) and cost is capped by orphan caps only in `get_best_orphans` (`max_new_orphans`), the resulting tree *count* in `self.trees` is not itself capped by `max_new_orphans`/`max_unknown_last_index_repairs`/`max_closest_completion_repairs` — those parameters only limit produced *repairs*, not how many map entries can accumulate over time or how many are scanned per call.

Consequently, once many single-slot orphan trees exist (each already fully "processed" or each contributing 0 usable repairs, e.g. because their single slot is already in `processed_slots` from an earlier phase, or has no `last_index` info that satisfies the branch conditions), `get_best_unknown_last_index`/`get_best_closest_completion` degrade to O(number of trees) work per call — i.e., O(attacker-created-tree-count) blockstore lookups — instead of being bounded by the caps intended to bound this cost.

### Impact Explanation
This is a CPU/I/O amplification in the repair hot path (`RepairWeight::get_best_weighted_repairs`, called once per repair tick) [7](#0-6) . Each repair tick's `get_unknown_last_index_us`/`get_closest_completion_us` cost (tracked in `RepairMetrics`) scales with attacker-created tree count rather than with `max_unknown_last_index_repairs`/`max_closest_completion_repairs`, degrading validator responsiveness on the repair path under sustained, cheap orphan-shred spam. This matches an unbounded-cost / algorithmic-complexity DoS category rather than direct block-invalidity or fund-loss impact.

### Likelihood Explanation
Feasibility depends on how cheap it is for an unstaked remote sender to get many single-slot orphan entries recognized and retained in `self.trees` over many repair ticks, and on how quickly `set_root`/pruning reclaims stale trees below root — this repo context does not show `self.trees` pruning/eviction logic in the excerpts examined, so I cannot fully confirm the growth rate is unbounded across ticks versus self-limiting via the `max_new_orphans` cap in `get_best_orphans`. This uncertainty leaves the ultimate severity and exact tree-count growth rate unconfirmed without further tracing of `split_off`/`update_orphan_ancestors`/root-advancement code and shred-ingress rate limiting (dedup, shred sigverify, per-slot orphan caps) that I was unable to fully inspect within the given iterations.

### Recommendation
Bound the total scan-and-lookup work in `get_best_unknown_last_index`/`get_best_closest_completion` independent of `self.trees.len()`, e.g., cap the number of trees visited per call, track a "no-progress" counter and bail out after a fixed number of empty-result trees, or amortize tree traversal across ticks (round-robin) so per-tick cost is bounded by a constant plus the repair caps rather than by attacker-controlled `self.trees` size.

### Proof of Concept
Rust unit-test plan added to `core/src/repair/repair_weight.rs` tests module:
1. Build a blockstore and `RepairWeight` with `root = 0`.
2. Insert N (e.g., 5,000) single-shred orphan slots each with a distinct, unknown parent slot, so each becomes its own single-slot entry in `self.trees` (simulate via `split_off`/`add_voters`+`blockstore.add_tree` used in existing tests, e.g. `test_split_off_multi_dump_repair`).
3. Ensure every one of these slots is already `is_full()`/already present in `processed_slots` before calling the target functions, so each tree yields 0 new repairs.
4. Call `repair_weight.get_best_unknown_last_index(...)` and `get_best_closest_completion(...)` with small `max_new_repairs` (e.g., 1), and assert the number of `blockstore.meta_repair`/`get_index` calls made (instrument via a call counter or by measuring wall-clock/`Measure` cost) scales with N rather than remaining O(max_new_repairs). Compare against a control run with a small fixed number of trees to show the cost differential grows linearly with N.

### Citations

**File:** core/src/repair/repair_weight.rs (L232-326)
```rust
    ) -> Vec<ShredRepairType> {
        let mut repairs = vec![];
        let mut processed_slots = AHashSet::from([self.root]);
        let mut slot_meta_cache = AHashMap::default();

        let mut get_best_orphans_us = Measure::start("get_best_orphans_us");
        // Find the best orphans in order from heaviest stake to least heavy
        self.get_best_orphans(
            blockstore,
            &mut processed_slots,
            &mut repairs,
            epoch_stakes,
            epoch_schedule,
            max_new_orphans,
            outstanding_repairs,
        );
        // Subtract 1 because the root is not processed as an orphan
        let num_orphan_slots = processed_slots.len() - 1;
        let num_orphan_repairs = repairs.len();
        get_best_orphans_us.stop();

        let mut get_best_shreds_us = Measure::start("get_best_shreds_us");
        let mut best_shreds_repairs = Vec::default();
        // Find the best incomplete slots in rooted subtree
        self.get_best_shreds(
            blockstore,
            &mut slot_meta_cache,
            &mut best_shreds_repairs,
            max_new_shreds,
            repair_eligibility,
            outstanding_repairs,
        );
        let num_best_shreds_repairs = best_shreds_repairs.len();
        let repair_slots_set: HashSet<Slot> =
            best_shreds_repairs.iter().map(|r| r.slot()).collect();
        let num_best_shreds_slots = repair_slots_set.len();
        processed_slots.extend(repair_slots_set);
        repairs.extend(best_shreds_repairs);
        get_best_shreds_us.stop();

        // Although we have generated repairs for orphan roots and slots in the rooted subtree,
        // if we have space we should generate repairs for slots in orphan trees in preparation for
        // when they are no longer rooted. Here we generate repairs for slots with unknown last
        // indices as well as slots that are close to completion.

        let mut get_unknown_last_index_us = Measure::start("get_unknown_last_index_us");
        let pre_num_slots = processed_slots.len();
        let unknown_last_index_repairs = self.get_best_unknown_last_index(
            blockstore,
            &mut slot_meta_cache,
            &mut processed_slots,
            max_unknown_last_index_repairs,
            outstanding_repairs,
        );
        let num_unknown_last_index_repairs = unknown_last_index_repairs.len();
        let num_unknown_last_index_slots = processed_slots.len() - pre_num_slots;
        repairs.extend(unknown_last_index_repairs);
        get_unknown_last_index_us.stop();

        let mut get_closest_completion_us = Measure::start("get_closest_completion_us");
        let pre_num_slots = processed_slots.len();
        let (closest_completion_repairs, total_slots_processed) = self.get_best_closest_completion(
            blockstore,
            &mut slot_meta_cache,
            &mut processed_slots,
            max_closest_completion_repairs,
            repair_eligibility,
            outstanding_repairs,
        );
        let num_closest_completion_repairs = closest_completion_repairs.len();
        let num_closest_completion_slots = processed_slots.len() - pre_num_slots;
        let num_closest_completion_slots_path =
            total_slots_processed.saturating_sub(num_closest_completion_slots);
        repairs.extend(closest_completion_repairs);
        get_closest_completion_us.stop();

        repair_metrics.best_repairs_stats.update(
            num_orphan_slots as u64,
            num_orphan_repairs as u64,
            num_best_shreds_slots as u64,
            num_best_shreds_repairs as u64,
            num_unknown_last_index_slots as u64,
            num_unknown_last_index_repairs as u64,
            num_closest_completion_slots as u64,
            num_closest_completion_slots_path as u64,
            num_closest_completion_repairs as u64,
            self.trees.len() as u64,
        );
        repair_metrics.timing.get_best_orphans_us += get_best_orphans_us.as_us();
        repair_metrics.timing.get_best_shreds_us += get_best_shreds_us.as_us();
        repair_metrics.timing.get_unknown_last_index_us += get_unknown_last_index_us.as_us();
        repair_metrics.timing.get_closest_completion_us += get_closest_completion_us.as_us();

        repairs
    }
```

**File:** core/src/repair/repair_weight.rs (L339-398)
```rust
    pub fn split_off(&mut self, slot: Slot) -> HashSet<Slot> {
        assert!(slot >= self.root);
        if slot == self.root {
            error!("Trying to orphan root of repair tree {slot}");
            return HashSet::new();
        }
        match self.slot_to_tree.get(&slot).copied() {
            Some(TreeRoot::Root(subtree_root)) => {
                if subtree_root == slot {
                    info!("{slot} is already orphan, skipping");
                    return HashSet::new();
                }
                let subtree = self
                    .trees
                    .get_mut(&subtree_root)
                    .expect("`self.slot_to_tree` and `self.trees` must be in sync");
                let orphaned_tree = subtree.split_off(&(slot, Hash::default()));
                self.rename_tree_root(&orphaned_tree, TreeRoot::Root(slot));
                self.trees.insert(slot, orphaned_tree);
                self.trees.get(&slot).unwrap().slots_iter().collect()
            }
            Some(TreeRoot::PrunedRoot(subtree_root)) => {
                // Even if these orphaned slots were previously pruned, they should be added back to
                // `self.trees` as we are no longer sure of their ancestry.
                // After they are repaired there is a chance that they  are now part of the rooted path.
                // This is possible for a duplicate slot with multiple ancestors, if the
                // version we had pruned before had the wrong ancestor, and the correct version is
                // descended from the rooted path.
                // If not they will once again be attached to the pruned set in
                // `update_orphan_ancestors`.

                info!("Dumping pruned slot {slot} of tree {subtree_root} in repair");
                let mut subtree = self
                    .pruned_trees
                    .remove(&subtree_root)
                    .expect("`self.slot_to_tree` and `self.pruned_trees` must be in sync");

                if subtree_root == slot {
                    // In this case we simply unprune the entire subtree by adding this subtree
                    // back into the main set of trees, self.trees
                    self.rename_tree_root(&subtree, TreeRoot::Root(subtree_root));
                    self.trees.insert(subtree_root, subtree);
                    self.trees
                        .get(&subtree_root)
                        .unwrap()
                        .slots_iter()
                        .collect()
                } else {
                    let orphaned_tree = subtree.split_off(&(slot, Hash::default()));
                    self.pruned_trees.insert(subtree_root, subtree);
                    self.rename_tree_root(&orphaned_tree, TreeRoot::Root(slot));
                    self.trees.insert(slot, orphaned_tree);
                    self.trees.get(&slot).unwrap().slots_iter().collect()
                }
            }
            None => {
                warn!("Trying to split off slot {slot} which doesn't currently exist in repair");
                HashSet::new()
            }
        }
```

**File:** core/src/repair/repair_weight.rs (L555-627)
```rust
    fn get_best_orphans(
        &mut self,
        blockstore: &Blockstore,
        processed_slots: &mut AHashSet<Slot>,
        repairs: &mut Vec<ShredRepairType>,
        epoch_stakes: &HashMap<Epoch, VersionedEpochStakes>,
        epoch_schedule: &EpochSchedule,
        max_new_orphans: usize,
        outstanding_repairs: &mut HashMap<ShredRepairType, u64>,
    ) {
        // Sort each tree in `self.trees`, by the amount of stake that has voted on each,
        // tiebreaker going to earlier slots, thus prioritizing earlier slots on the same fork
        // to ensure replay can continue as soon as possible.
        let mut stake_weighted_trees: Vec<(Slot, u64)> = self
            .trees
            .iter()
            .map(|(slot, tree)| {
                (
                    *slot,
                    tree.stake_voted_subtree(&(*slot, Hash::default()))
                        .expect("Tree must have weight at its own root"),
                )
            })
            .collect();

        // Heavier, smaller slots come first
        Self::sort_by_stake_weight_slot(&mut stake_weighted_trees);
        let mut new_best_orphan_requests = 0;
        for (heaviest_tree_root, _) in stake_weighted_trees {
            if new_best_orphan_requests >= max_new_orphans {
                break;
            }
            if processed_slots.contains(&heaviest_tree_root) {
                continue;
            }
            // Ignore trees that were merged in a previous iteration
            if self.trees.contains_key(&heaviest_tree_root) {
                let new_orphan_root = self.update_orphan_ancestors(
                    blockstore,
                    heaviest_tree_root,
                    epoch_stakes,
                    epoch_schedule,
                );
                if let Some(new_orphan_root) = new_orphan_root
                    && new_orphan_root != self.root
                    && let Some(repair_request) = RepairService::request_repair_if_needed(
                        outstanding_repairs,
                        ShredRepairType::Orphan(new_orphan_root),
                    )
                {
                    repairs.push(repair_request);
                    processed_slots.insert(new_orphan_root);
                    new_best_orphan_requests += 1;
                }
            }
        }

        // If there are fewer than `max_new_orphans`, just grab the next
        // available ones.
        for new_orphan in blockstore.orphans_iterator(self.root + 1).unwrap() {
            if new_best_orphan_requests >= max_new_orphans {
                break;
            }
            if let Some(repair_request) = RepairService::request_repair_if_needed(
                outstanding_repairs,
                ShredRepairType::Orphan(new_orphan),
            ) {
                repairs.push(repair_request);
                processed_slots.insert(new_orphan);
                new_best_orphan_requests += 1;
            }
        }
    }
```

**File:** core/src/repair/repair_weight.rs (L631-655)
```rust
    fn get_best_unknown_last_index(
        &mut self,
        blockstore: &Blockstore,
        slot_meta_cache: &mut AHashMap<Slot, Option<SlotMetaRepair>>,
        processed_slots: &mut AHashSet<Slot>,
        max_new_repairs: usize,
        outstanding_repairs: &mut HashMap<ShredRepairType, u64>,
    ) -> Vec<ShredRepairType> {
        let mut repairs = Vec::default();
        for tree in self.trees.values() {
            if repairs.len() >= max_new_repairs {
                break;
            }
            let new_repairs = get_unknown_last_index(
                tree,
                blockstore,
                slot_meta_cache,
                processed_slots,
                max_new_repairs - repairs.len(),
                outstanding_repairs,
            );
            repairs.extend(new_repairs);
        }
        repairs
    }
```

**File:** core/src/repair/repair_weight.rs (L661-690)
```rust
    fn get_best_closest_completion(
        &mut self,
        blockstore: &Blockstore,
        slot_meta_cache: &mut AHashMap<Slot, Option<SlotMetaRepair>>,
        processed_slots: &mut AHashSet<Slot>,
        max_new_repairs: usize,
        repair_eligibility: &mut RepairEligibility,
        outstanding_repairs: &mut HashMap<ShredRepairType, u64>,
    ) -> (Vec<ShredRepairType>, /* processed slots */ usize) {
        let mut repairs = Vec::default();
        let mut total_processed_slots = 0;
        for tree in self.trees.values() {
            if repairs.len() >= max_new_repairs {
                break;
            }
            let (new_repairs, new_processed_slots) = get_closest_completion(
                tree,
                blockstore,
                self.root,
                slot_meta_cache,
                processed_slots,
                max_new_repairs - repairs.len(),
                repair_eligibility,
                outstanding_repairs,
            );
            repairs.extend(new_repairs);
            total_processed_slots += new_processed_slots;
        }
        (repairs, total_processed_slots)
    }
```

**File:** core/src/repair/repair_generic_traversal.rs (L60-81)
```rust
    let iter = GenericTraversal::new(tree);
    let mut unknown_last = Vec::new();
    for slot in iter {
        if processed_slots.contains(&slot) {
            continue;
        }
        let slot_meta = slot_meta_cache
            .entry(slot)
            .or_insert_with(|| blockstore.meta_repair(slot).unwrap());
        if let Some(slot_meta) = slot_meta
            && slot_meta.last_index.is_none()
        {
            let shred_index = blockstore.get_index(slot).unwrap();
            let num_processed_shreds = if let Some(shred_index) = shred_index {
                shred_index.data().num_shreds() as u64
            } else {
                slot_meta.consumed
            };
            unknown_last.push((slot, slot_meta.received, num_processed_shreds));
            processed_slots.insert(slot);
        }
    }
```

**File:** core/src/repair/repair_generic_traversal.rs (L136-188)
```rust
    let mut slot_dists: Vec<(Slot, u64)> = Vec::default();
    let iter = GenericTraversal::new(tree);
    for slot in iter {
        if processed_slots.contains(&slot) {
            continue;
        }
        let slot_meta = slot_meta_cache
            .entry(slot)
            .or_insert_with(|| blockstore.meta_repair(slot).unwrap());
        if let Some(slot_meta) = slot_meta {
            if slot_meta.is_full() {
                continue;
            }
            if let Some(last_index) = slot_meta.last_index {
                let shred_index = blockstore.get_index(slot).unwrap();
                let dist = if let Some(shred_index) = shred_index {
                    let shred_count = shred_index.data().num_shreds() as u64;
                    if last_index.saturating_add(1) < shred_count {
                        datapoint_error!(
                            "repair_generic_traversal_error",
                            (
                                "error",
                                format!(
                                    "last_index + 1 < shred_count. last_index={last_index} \
                                     shred_count={shred_count}",
                                ),
                                String
                            ),
                        );
                    }
                    last_index.saturating_add(1).saturating_sub(shred_count)
                } else {
                    if last_index < slot_meta.consumed {
                        datapoint_error!(
                            "repair_generic_traversal_error",
                            (
                                "error",
                                format!(
                                    "last_index < slot_meta.consumed. last_index={} \
                                     slot_meta.consumed={}",
                                    last_index, slot_meta.consumed,
                                ),
                                String
                            ),
                        );
                    }
                    last_index.saturating_sub(slot_meta.consumed)
                };
                slot_dists.push((slot, dist));
            }
        }
    }
    slot_dists.sort_by_key(|(_, d)| *d);
```
