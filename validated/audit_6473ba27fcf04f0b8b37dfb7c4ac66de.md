#No vulnerability found for this question.

The claimed connection between attacker-controlled state growth spanning a state-part boundary and `to_children` in `core/store/src/trie/mem/flexible_data/children.rs` does not exist in the code. `to_children` is a pure in-memory arena helper that converts a `ChildrenView` (mask + pointers already stored in the memtrie arena) into a `Children` array of child hashes by iterating the fixed 16-slot mask and reading pointers/hashes that were already computed when the node was inserted [1](#0-0) . It has no notion of state-part boundaries, key length, extension-node length, or DFS memory thresholds — it only decodes whichever children pointers were encoded for that node via `mask.count_ones()`-based indexing, and all accesses are bounds-checked (`assert!(i < NUM_CHILDREN)` in `get`, and the mask-driven loop in `to_children` only touches bits actually set in a 16-bit mask) [2](#0-1) .

State-part boundary computation and reassembly correctness are implemented and tested in an entirely separate module, `core/store/src/trie/state_parts.rs`, via `find_state_part_boundary` (DFS by prefix-sum of `memory_usage`), `get_trie_nodes_for_part_with_flat_storage`, `apply_state_part`/`validate_state_part`, and `combine_state_parts_naive`, which operate on `TrieStorageNode`/`RawTrieNodeWithSize` from disk storage, not on the memtrie's `ChildrenView::to_children` [3](#0-2) [4](#0-3) . These boundary/partition invariants are explicitly covered by existing tests (`test_combine_state_parts`, `check_combine_state_parts`, `boundary_is_state_key`) that assert combined-part `TrieChanges` and state roots match regardless of trie shape, key length, or extension-node structure [5](#0-4) [6](#0-5) .

Since `to_children` performs no boundary-sensitive logic and does not participate in state-part generation, splitting, or validation, there is no reachable path from attacker-controlled `storage_write`/`storage_remove`/`storage_read` calls, state size, or extension-node length that could cause `to_children` to produce an inconsistent `Children` set or break the state-part reassembly invariant. The premise of the question conflates two unrelated subsystems (in-memory trie node hashing vs. disk-based state-part partitioning), and no code path supports the described exploit.

### Citations

**File:** core/store/src/trie/mem/flexible_data/children.rs (L71-83)
```rust
impl<'a, M: ArenaMemory> ChildrenView<'a, M> {
    /// Gets the child at a specific index (0 to 15).
    pub fn get(&self, i: usize) -> Option<MemTrieNodePtr<'a, M>> {
        assert!(i < NUM_CHILDREN);
        let bit = 1 << (i as ChildrenMask);
        if self.mask & bit == 0 {
            None
        } else {
            let lower_mask = self.mask & (bit - 1);
            let index = lower_mask.count_ones() as usize;
            Some(MemTrieNodePtr::from(self.children.read_ptr_at(index * size_of::<usize>())))
        }
    }
```

**File:** core/store/src/trie/mem/flexible_data/children.rs (L86-117)
```rust
    pub fn to_children(&self) -> Children {
        let mut nodes = [None; NUM_CHILDREN];
        if self.mask == 0 {
            return Children(nodes);
        };

        // cspell:words ptrs
        let mut node_ptrs = [None; NUM_CHILDREN];
        let mut j = size_of::<usize>() * self.mask.count_ones() as usize;
        // Execute all `read_ptr_at` in reverse to avoid repeat bound checks.
        // Additionally, issue reads for the node kinds before moving on to compute sha256 hashes,
        // thus hopefully giving CPU more time to load the relevant lines into the cache.
        for i in (0..NUM_CHILDREN).rev() {
            let bit = self.mask & (1 << i);
            if bit != 0 {
                j -= size_of::<usize>();
                let ptr = MemTrieNodePtr::from(self.children.read_ptr_at(j));
                let kind = ptr.get_kind();
                node_ptrs[i] = Some((ptr, kind));
            }
        }

        for (node, node_ptr) in std::iter::zip(nodes.iter_mut().rev(), node_ptrs.into_iter().rev())
        {
            if let Some((node_ptr, kind)) = node_ptr {
                let node_view = node_ptr.view_kind(kind);
                *node = Some(node_view.node_hash());
            }
        }

        Children(nodes)
    }
```

**File:** core/store/src/trie/state_parts.rs (L52-74)
```rust
    fn find_state_part_boundary(
        &self,
        part_id: u64,
        num_parts: u64,
    ) -> Result<Option<Vec<u8>>, StorageError> {
        if part_id > num_parts {
            return Err(StorageError::StorageInternalError);
        }
        if part_id == 0 {
            return Ok(Some(vec![]));
        }
        if part_id == num_parts {
            return Ok(None);
        }
        let root_node = self.retrieve_storage_node(&self.root)?;
        let total_size = root_node.memory_usage;
        let size_start = total_size / num_parts * part_id + part_id.min(total_size % num_parts);
        if root_node.memory_usage <= size_start {
            return Ok(None);
        }
        let nibbles = self.find_node_in_dfs_order(&root_node, size_start)?;
        Ok(Some(NibbleSlice::nibbles_to_bytes(&nibbles)))
    }
```

**File:** core/store/src/trie/state_parts.rs (L423-468)
```rust
    fn apply_state_part_impl(
        state_root: &StateRoot,
        part_id: PartId,
        part: PartialState,
    ) -> Result<ApplyStatePartResult, StorageError> {
        if state_root == &Trie::EMPTY_ROOT {
            return Ok(ApplyStatePartResult {
                trie_changes: TrieChanges::empty(Trie::EMPTY_ROOT),
                flat_state_delta: Default::default(),
                contract_codes: vec![],
            });
        }
        let trie = Trie::from_recorded_storage(PartialStorage { nodes: part }, *state_root, false);
        let path_begin = trie.find_state_part_boundary(part_id.idx, part_id.total)?;
        let path_end = trie.find_state_part_boundary(part_id.idx + 1, part_id.total)?;
        let mut iterator = trie.disk_iter()?;
        let trie_traversal_items =
            iterator.visit_nodes_interval(path_begin.as_deref(), path_end.as_deref())?;
        let mut refcount_changes = TrieRefcountDeltaMap::new();
        let mut flat_state_delta = FlatStateChanges::default();
        let mut contract_codes = Vec::new();
        for TrieTraversalItem { hash, key } in trie_traversal_items {
            let value = trie.retrieve_value(&hash, AccessOptions::DEFAULT)?;
            refcount_changes.add(hash, value.to_vec(), 1);
            if let Some(trie_key) = key {
                let flat_state_value = FlatStateValue::on_disk(&value);
                flat_state_delta.insert(trie_key.clone(), Some(flat_state_value));
                if is_contract_code_key(&trie_key) {
                    contract_codes.push(ContractCode::new(value.to_vec(), None));
                }
            }
        }
        let (insertions, deletions) = refcount_changes.into_changes();
        Ok(ApplyStatePartResult {
            trie_changes: TrieChanges {
                old_root: Trie::EMPTY_ROOT,
                new_root: *state_root,
                insertions,
                deletions,
                memtrie_changes: None,
                children_memtrie_changes: Default::default(),
            },
            flat_state_delta,
            contract_codes,
        })
    }
```

**File:** core/store/src/trie/state_parts.rs (L591-638)
```rust
    impl Trie {
        /// Combines all parts and returns TrieChanges that can be applied to storage.
        ///
        /// # Input
        /// parts[i] has trie nodes for part i
        ///
        /// # Errors
        /// StorageError if data is inconsistent. Should never happen if each part was validated.
        pub fn combine_state_parts_naive(
            state_root: &StateRoot,
            parts: &[PartialState],
        ) -> Result<TrieChanges, StorageError> {
            let nodes = PartialState::TrieValues(
                parts
                    .iter()
                    .flat_map(|PartialState::TrieValues(nodes)| nodes.iter())
                    .cloned()
                    .collect(),
            );
            let trie = Trie::from_recorded_storage(PartialStorage { nodes }, *state_root, false);
            let mut insertions = <HashMap<CryptoHash, (Vec<u8>, u32)>>::new();
            trie.traverse_all_nodes(|hash| {
                if let Some((_bytes, rc)) = insertions.get_mut(hash) {
                    *rc += 1;
                } else {
                    let bytes = trie.retrieve_value(hash, AccessOptions::DEFAULT).unwrap();
                    insertions.insert(*hash, (bytes.to_vec(), 1));
                }
                Ok(())
            })?;
            let mut insertions = insertions
                .into_iter()
                .map(|(k, (v, rc))| TrieRefcountAddition {
                    trie_node_or_value_hash: k,
                    trie_node_or_value: v,
                    rc: std::num::NonZeroU32::new(rc).unwrap(),
                })
                .collect::<Vec<_>>();
            insertions.sort();
            Ok(TrieChanges {
                old_root: Default::default(),
                new_root: *state_root,
                insertions,
                deletions: vec![],
                memtrie_changes: None,
                children_memtrie_changes: Default::default(),
            })
        }
```

**File:** core/store/src/trie/state_parts.rs (L870-938)
```rust
    #[test]
    fn test_combine_state_parts() {
        let mut rng = rand::thread_rng();
        for _ in 0..2000 {
            let tries = TestTriesBuilder::new().build();
            let trie_changes = gen_changes(&mut rng, 20);
            let state_root = test_populate_trie(
                &tries,
                &Trie::EMPTY_ROOT,
                ShardUId::single_shard(),
                trie_changes.clone(),
            );
            let trie = tries.get_trie_for_shard(ShardUId::single_shard(), state_root);
            let root_memory_usage = trie.retrieve_root_node().unwrap().memory_usage;
            {
                // Test that combining all parts gets all nodes
                let num_parts = rng.gen_range(2..10);
                let parts = (0..num_parts)
                    .map(|part_id| {
                        trie.get_trie_nodes_for_part_without_flat_storage(PartId::new(
                            part_id, num_parts,
                        ))
                        .unwrap()
                    })
                    .collect::<Vec<_>>();

                let trie_changes = check_combine_state_parts(trie.get_root(), num_parts, &parts);

                let mut nodes = <HashMap<CryptoHash, Arc<[u8]>>>::new();
                let sizes_vec = parts
                    .iter()
                    .map(|PartialState::TrieValues(nodes)| {
                        nodes.iter().map(|node| node.len()).sum::<usize>()
                    })
                    .collect::<Vec<_>>();

                for part in parts {
                    let PartialState::TrieValues(part_nodes) = part;
                    for node in part_nodes {
                        nodes.insert(hash(&node), node);
                    }
                }
                let all_nodes = nodes.into_iter().map(|(_hash, node)| node).collect::<Vec<_>>();
                assert_eq!(all_nodes.len(), trie_changes.insertions.len());
                let size_of_all = all_nodes.iter().map(|node| node.len()).sum::<usize>();
                let num_nodes = all_nodes.len();
                assert_eq!(
                    Trie::validate_state_part(
                        trie.get_root(),
                        PartId::new(0, 1),
                        PartialState::TrieValues(all_nodes),
                    ),
                    Ok(())
                );

                let sum_of_sizes = sizes_vec.iter().sum::<usize>();
                // Manually check that sizes are reasonable
                println!("------------------------------");
                println!("Number of nodes: {:?}", num_nodes);
                println!("Sizes of parts: {:?}", sizes_vec);
                println!(
                    "All nodes size: {:?}, sum_of_sizes: {:?}, memory_usage: {:?}",
                    size_of_all, sum_of_sizes, root_memory_usage
                );
                // borsh serialize should be about this size
                assert!(size_of_all + 8 * num_nodes <= root_memory_usage as usize);
            }
        }
    }
```
