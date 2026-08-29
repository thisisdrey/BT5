No vulnerability found for this question.

The concern is that adversarial key selection (zero common-prefix collisions forcing repeated Leaf→Branch conversions in `GenericTrieUpdateInsertDelete::generic_insert`) could let an attacker create more trie nodes per byte written than the recorded storage-proof metering accounts for, letting a receipt slip past `per_receipt_storage_proof_size_limit`/`main_storage_proof_size_soft_limit`. Tracing the actual mechanics shows this isn't possible:

- `ensure_updated` in `core/store/src/trie/trie_storage_update.rs` (and the memtrie/interface equivalents) only performs an actual storage read (and thus a `TrieRecorder::record` call) when the child reference is `GenericNodeOrIndex::Old` — i.e., the first time a pre-existing on-disk node is touched in the update session. [1](#0-0)  Any subsequent split/branch-conversion performed on a node that was already brought into the in-memory update this receipt (via `take_node`/`place_node`) touches purely in-memory structures and adds no additional storage-proof bytes. [2](#0-1) 
- So crafting many brand-new adversarial keys under the attacker's own account only costs one real (recorded) read per distinct pre-existing on-disk node along each key's path (bounded by existing trie depth); repeated in-session splitting of already-updated nodes is "free" in proof-size terms, not inflated.
- The system already measures and enforces the *actual* accumulated proof growth, not a per-byte estimate: `apply_action_receipt` snapshots `recorded_storage_size_upper_bound()` before the receipt and checks the delta against `per_receipt_storage_proof_size_limit` after every action when `EnforceStorageProofLimitForAllActions`/`EnforcePerReceiptStorageProofLimit` are active. [3](#0-2)  For `FunctionCall` storage host calls specifically, `RecordedStorageCounter::observe_size` performs the same real-growth check after every trie operation inside the VM. [4](#0-3) 

Because the metering is driven by the true recorded proof size (real reads of pre-existing on-disk nodes) rather than a value-byte estimate, any extra nodes an attacker forces to be split/created either don't add real proof bytes (in-session nodes) or do add real bytes and are then correctly counted and enforced against the same limit — there is no totality gap that would let a receipt exceed `per_receipt_storage_proof_size_limit`/`main_storage_proof_size_soft_limit` undetected. Worst case, the attacker's own receipt fails with `RecordedStorageExceeded`/`ActionErrorKind::ReceiptStorageProofSizeExceeded`, which is the intended liveness/DoS-prevention behavior, not a bypass.

### Citations

**File:** core/store/src/trie/trie_storage_update.rs (L92-103)
```rust
    fn ensure_updated(
        &mut self,
        node: GenericNodeOrIndex<TrieStorageNodePtr>,
        opts: AccessOptions,
    ) -> Result<UpdatedNodeId, StorageError> {
        match node {
            GenericNodeOrIndex::Old(node_hash) => {
                self.trie.move_node_to_mutable(self, &node_hash, opts).map(|handle| handle.0)
            }
            GenericNodeOrIndex::Updated(node_id) => Ok(node_id),
        }
    }
```

**File:** core/store/src/trie/ops/insert_delete.rs (L123-155)
```rust
                    } else if common_prefix == 0 {
                        // Convert the leaf to an equivalent branch. We are not adding
                        // the new branch yet; that will be done in the next iteration.
                        let mut children = Box::<[_; NUM_CHILDREN]>::default();
                        let children_memory_usage;
                        let branch_node = if existing_key.is_empty() {
                            // Existing key being empty means the old value now lives at the branch.
                            children_memory_usage = 0;
                            GenericUpdatedTrieNode::Branch { children, value: Some(old_value) }
                        } else {
                            let branch_idx = existing_key.at(0) as usize;
                            let new_extension = existing_key.mid(1).encoded(true).into_vec();
                            let new_node = GenericUpdatedTrieNode::Leaf {
                                extension: new_extension.into_boxed_slice(),
                                value: old_value,
                            };
                            let memory_usage = new_node.memory_usage_direct();
                            children_memory_usage = memory_usage;
                            let new_node_id = self.place_node(GenericUpdatedTrieNodeWithSize {
                                node: new_node,
                                memory_usage,
                            });
                            children[branch_idx] = Some(GenericNodeOrIndex::Updated(new_node_id));
                            GenericUpdatedTrieNode::Branch { children, value: None }
                        };
                        let memory_usage =
                            branch_node.memory_usage_direct() + children_memory_usage;
                        self.place_node_at(
                            node_id,
                            GenericUpdatedTrieNodeWithSize { node: branch_node, memory_usage },
                        );
                        path.pop();
                        continue;
```

**File:** runtime/runtime/src/lib.rs (L870-945)
```rust
        } else {
            let storage_proof_size_before_receipt =
                if ProtocolFeature::EnforcePerReceiptStorageProofLimit
                    .enabled(apply_state.current_protocol_version)
                {
                    Some(state_update.trie.recorded_storage_size_upper_bound())
                } else {
                    None
                };
            // The in-VM `RecordedStorageCounter` only bounds `FunctionCall` actions.
            let storage_proof_limit_for_all_actions =
                ProtocolFeature::EnforceStorageProofLimitForAllActions
                    .enabled(apply_state.current_protocol_version)
                    .then(|| {
                        apply_state
                            .config
                            .wasm_config
                            .limit_config
                            .per_receipt_storage_proof_size_limit
                    });

            // Executing actions one by one
            for (action_index, action) in action_receipt.actions().iter().enumerate() {
                let action_hash = create_action_hash_from_receipt_id(
                    receipt.receipt_id(),
                    apply_state.block_height,
                    action_index,
                );
                let mut new_result = self.apply_action(
                    action,
                    state_update,
                    apply_state,
                    preparation_pipeline,
                    &mut account,
                    &mut actor_id,
                    receipt,
                    &action_receipt,
                    Arc::clone(&promise_results),
                    &action_hash,
                    action_index,
                    &action_receipt.actions(),
                    epoch_info_provider,
                    storage_proof_size_before_receipt,
                )?;
                if new_result.result.is_ok() {
                    if let Err(e) = new_result.new_receipts.iter().try_for_each(|receipt| {
                        validate_receipt(
                            &apply_state.config.wasm_config.limit_config,
                            receipt,
                            apply_state.current_protocol_version,
                            ValidateReceiptMode::NewReceipt,
                        )
                    }) {
                        new_result.result =
                            Err(ActionErrorKind::NewReceiptValidationError(e).into());
                    }
                }
                result.merge(new_result)?;
                if let (true, Some(size_before), Some(limit)) = (
                    result.result.is_ok(),
                    storage_proof_size_before_receipt,
                    storage_proof_limit_for_all_actions,
                ) {
                    let recorded_by_receipt = state_update
                        .trie
                        .recorded_storage_size_upper_bound()
                        .saturating_sub(size_before);
                    if recorded_by_receipt > limit {
                        result.set_error(
                            ActionErrorKind::ReceiptStorageProofSizeExceeded {
                                limit: limit as u64,
                            }
                            .into(),
                        );
                    }
                }
```

**File:** runtime/near-vm-runner/src/logic/recorded_storage_counter.rs (L17-33)
```rust
    /// Update the latest observed storage proof size and check if it exceeds the limit.
    /// Should be called after every trie operation.
    pub fn observe_size(&mut self, latest_storage_proof_size: usize) -> Result<(), VMLogicError> {
        self.last_observed_storage_size = latest_storage_proof_size;

        let current_size = self.get_storage_size()?;
        if current_size > self.size_limit {
            let limit_u64 = self.size_limit.try_into().map_err(|_| {
                VMLogicError::InconsistentStateError(InconsistentStateError::IntegerOverflow)
            })?;
            return Err(VMLogicError::HostError(HostError::RecordedStorageExceeded {
                limit: ByteSize::b(limit_u64),
            }));
        }

        Ok(())
    }
```
