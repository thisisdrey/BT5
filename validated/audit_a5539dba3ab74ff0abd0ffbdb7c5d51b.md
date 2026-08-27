[1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** runtime/runtime/src/lib.rs (L891-913)
```rust
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
```

**File:** runtime/runtime/src/lib.rs (L954-977)
```rust
        // Going to check balance covers account's storage.
        if result.result.is_ok() {
            if let Some(ref account) = account {
                match check_storage_stake(account, account.amount(), &apply_state.config) {
                    Ok(()) => {
                        set_account(state_update, account_id.clone(), account);
                    }
                    Err(StorageStakingError::LackBalanceForStorageStaking(amount)) => {
                        result.set_error(ActionError {
                            index: None,
                            kind: ActionErrorKind::LackBalanceForState {
                                account_id: account_id.clone(),
                                amount,
                            },
                        });
                    }
                    Err(StorageStakingError::StorageError(err)) => {
                        return Err(RuntimeError::StorageError(
                            StorageError::StorageInconsistentState(err),
                        ));
                    }
                }
            }
        }
```

**File:** runtime/runtime/src/actions.rs (L426-435)
```rust
/// Clears the contract storage usage based on type for an account.
pub(crate) fn clear_account_contract_storage_usage(
    state_update: &TrieUpdate,
    account_id: &AccountId,
    account: &mut Account,
) -> Result<(), StorageError> {
    let contract_storage = get_contract_storage_usage(state_update, account_id, account)?;
    account.set_storage_usage(account.storage_usage().saturating_sub(contract_storage));
    Ok(())
}
```
