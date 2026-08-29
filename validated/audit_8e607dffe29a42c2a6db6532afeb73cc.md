This confirms the actual mechanism: the delayed receipt queue is duplicated wholesale into both child shards at split time, and each child independently walks its own copy via `DelayedReceiptQueueWrapper::pop`, discarding (permanently, from its own copy) any receipt whose `receiver_shard_id` (computed via the deterministic `shard_layout` account→shard mapping) doesn't match its own `shard_id`. This is exactly-once by construction of a pure function over the receiver's `AccountId`, independent of account *existence* — it does not matter whether the beneficiary account currently exists, was just created, or is racing with another action in the same chunk. `compute_receipt_congestion_gas`/`receipt_congestion_gas` recomputation only affects each child's own `CongestionInfo` gas/byte accounting (`DelayedReceiptQueueWrapper::apply_congestion_changes`), not receipt delivery or balance transfer — it cannot cause a receipt to be executed twice. [1](#0-0) [2](#0-1) 

The premise also mischaracterizes `validate_action_account_id`: it only checks `beneficiary_id` syntax (`AccountId::validate`), and this is intentional — `DeleteAccountAction.beneficiary_id` is not required to already exist; `action_delete_account` pushes a `Receipt::new_balance_refund` unconditionally to whatever id is given, and the balance transfer/refund executes as a normal receipt on whichever single shard that id maps to. There is no code path where `remove_result.gas_key_nonce_count`-driven compute charging interacts with congestion-gas recomputation to cause duplicate delivery — those are unrelated concerns (per-account gas-key removal compute cost vs. per-shard congestion-queue accounting). [3](#0-2) [4](#0-3) 

I found no code path by which a single `Receipt::new_balance_refund` produced by `action_delete_account` could be delivered to and executed by two child shards after a resharding split. The `receipt_filter_fn` guarantees exactly-once delivery deterministically by account id, unrelated to account existence races, and there's an existing repro/regression test pattern for delayed-receipt resharding correctness (`test-loop-tests/src/utils/resharding.rs`, `delayed_receipts_repro_missing_trie_value`) confirming this is an actively tested invariant, not an open gap. [5](#0-4) 

#No vulnerability found for this question.

### Citations

**File:** runtime/runtime/src/congestion_control.rs (L868-910)
```rust
    // With ReshardingV3, it's possible for a chunk to have delayed receipts that technically
    // belong to the sibling shard before a resharding event.
    // Here, we filter all the receipts that don't belong to the current shard_id.
    //
    // The function follows the guidelines of standard iterator filter function
    // We return true if we should retain the receipt and false if we should filter it.
    fn receipt_filter_fn(&self, receipt: &ReceiptOrStateStoredReceipt) -> bool {
        let shard_layout = self.epoch_info_provider.shard_layout(&self.epoch_id).unwrap();
        let receipt_shard_id = receipt.get_receipt().receiver_shard_id(&shard_layout).unwrap();
        receipt_shard_id == self.shard_id
    }

    pub(crate) fn pop(
        &mut self,
        trie_update: &mut TrieUpdate,
        config: &RuntimeConfig,
    ) -> Result<Option<ReceiptOrStateStoredReceipt<'_>>, RuntimeError> {
        // While processing receipts, we need to keep track of the gas and bytes
        // even for receipts that may be filtered out due to a resharding event
        loop {
            // Check proof size limit before each receipt is popped.
            if trie_update.trie.check_proof_size_limit_exceed() {
                break;
            }
            let Some(receipt) = self.queue.pop_front(trie_update)? else {
                break;
            };
            let delayed_gas = receipt_congestion_gas(&receipt, &config)?;
            let delayed_bytes = receipt_size(&receipt)? as u64;
            self.removed_delayed_gas =
                self.removed_delayed_gas.checked_add(delayed_gas).ok_or(IntegerOverflowError)?;
            self.removed_delayed_bytes = self
                .removed_delayed_bytes
                .checked_add(delayed_bytes)
                .ok_or(IntegerOverflowError)?;

            // Track gas and bytes for receipt above and return only receipt that belong to the shard.
            if self.receipt_filter_fn(&receipt) {
                return Ok(Some(receipt));
            }
        }
        Ok(None)
    }
```

**File:** runtime/runtime/src/congestion_control.rs (L922-929)
```rust
    /// This function returns the maximum length of the delayed receipt queue.
    /// The only time the real number of delayed receipts differ from the returned value is right
    /// after a resharding event. During resharding, we duplicate the delayed receipt queue across
    /// both child shards, which means it's possible that the child shards contain delayed receipts
    /// that don't belong to them.
    pub(crate) fn upper_bound_len(&self) -> u64 {
        self.queue.len()
    }
```

**File:** runtime/runtime/src/action_validation.rs (L399-403)
```rust
fn validate_delete_action(action: &DeleteAccountAction) -> Result<(), ActionsValidationError> {
    validate_action_account_id(&action.beneficiary_id)?;

    Ok(())
}
```

**File:** runtime/runtime/src/actions.rs (L364-376)
```rust
    // We use current amount as a pay out to beneficiary.
    let account_balance = account_ref.amount();
    if account_balance > Balance::ZERO {
        result
            .new_receipts
            .push(Receipt::new_balance_refund(&delete_account.beneficiary_id, account_balance));
    }
    let remove_result = remove_account(state_update, account_id)?;
    result.tokens_burnt =
        result.tokens_burnt.checked_add(gas_key_balance_to_burn).ok_or_else(|| {
            StorageError::StorageInconsistentState("tokens_burnt overflow".to_string())
        })?;
    if remove_result.gas_key_nonce_count > 0 {
```

**File:** test-loop-tests/src/utils/resharding.rs (L1315-1320)
```rust
/// Repro case for the issue of 'Missing TrieValue' after GC period for refcounted trie nodes
/// that are duplicated to both children during resharding.
/// This scenario tests a particular combination of contract calls, in order to create
/// delayed receipts only in one child, and verifies that the other child shard is not left
/// with missing trie values.
pub(crate) fn delayed_receipts_repro_missing_trie_value(
```
