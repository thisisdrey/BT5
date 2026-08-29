Based on the code, `Action::TransferToGasKey` is handled directly — unlike `Transfer`, there's no separate account-balance credit step; the deposit is applied *only* by `action_transfer_to_gas_key` which increments `gas_key_info.balance` in the access key in the trie, and never touches `account.amount()` at all. [1](#0-0) [2](#0-1) 

This means the deposit attached to a `TransferToGasKeyAction` receipt is deducted from the sender's balance at receipt-creation time (`deduct_balance` in the host function, mirroring `promise_batch_action_transfer`), and on arrival it is written *directly* into the target gas key's `GasKeyInfo.balance` in the trie — it is never transiently reflected in `account.amount()`. [3](#0-2) 

Since all actions within a single `ActionReceipt` are executed sequentially against the *same* mutable `TrieUpdate`/`Account` in memory (no separate "transient" state), a subsequent `DeleteAccount` action in the same batch sees the trie *already* updated by the preceding `TransferToGasKey` action. `compute_gas_key_balance_sum` at deletion time reads the current (post-`TransferToGasKey`) trie state, so it counts the newly-funded gas key exactly once — there is no separate place where the same yoctoNEAR is counted both as part of `account_balance` (`amount()`) and as gas-key balance to burn, because `TransferToGasKey` never credited `account.amount()` in the first place. [4](#0-3) 

The existing test `test_delete_account_burns_gas_key_balances` confirms exactly this accounting: gas keys funded via `transfer_to_gas_key`, then `DeleteAccount` correctly reports `tokens_burnt == sum of all gas key balances`, with no double count and no reduction of `account_balance`. [5](#0-4) 

The premise of the question — that `TransferToGasKey`'s handler credits `amount` to the account balance transiently, separate from the gas key balance — does not match the actual implementation. There is no code path where the deposited amount is added to `account.amount()`. The `MAX_BALANCE_TO_BURN` cap (1 NEAR) on `compute_gas_key_balance_sum` also bounds any possible discrepancy to a value the attacker cannot exceed by funding beyond that cap (deletion fails with `GasKeyBalanceTooHigh` otherwise), and gas-key-balance overflow is guarded with `checked_add`. [6](#0-5) 

#No vulnerability found for this question.

### Citations

**File:** runtime/runtime/src/access_keys.rs (L257-288)
```rust
pub(crate) fn action_transfer_to_gas_key(
    state_update: &mut TrieUpdate,
    result: &mut ActionResult,
    account_id: &AccountId,
    action: &TransferToGasKeyAction,
) -> Result<(), RuntimeError> {
    let Some(mut access_key) = get_access_key(state_update, account_id, &action.public_key)? else {
        result.result = Err(ActionErrorKind::GasKeyDoesNotExist {
            account_id: account_id.clone(),
            public_key: Box::new(action.public_key.clone()),
        }
        .into());
        return Ok(());
    };
    let Some(gas_key_info) = access_key.gas_key_info_mut() else {
        // Key exists but is not a gas key
        result.result = Err(ActionErrorKind::GasKeyDoesNotExist {
            account_id: account_id.clone(),
            public_key: Box::new(action.public_key.clone()),
        }
        .into());
        return Ok(());
    };

    gas_key_info.balance = gas_key_info.balance.checked_add(action.deposit).ok_or_else(|| {
        RuntimeError::StorageError(StorageError::StorageInconsistentState(
            "gas key balance integer overflow".to_string(),
        ))
    })?;
    set_access_key(state_update, account_id.clone(), action.public_key.clone(), &access_key);
    Ok(())
}
```

**File:** runtime/runtime/src/access_keys.rs (L715-756)
```rust
    #[test]
    fn test_delete_account_burns_gas_key_balances() {
        let (account_id, public_key, access_key) = test_account_keys();
        let public_keys: Vec<PublicKey> = (0..3)
            .map(|i| PublicKey::from_seed(KeyType::ED25519, &format!("gas_key_{i}")))
            .collect();
        let mut state_update = setup_account(&account_id, &public_key, &access_key);
        let mut account = get_account(&state_update, &account_id).unwrap().unwrap();
        for public_key in &public_keys {
            add_gas_key_to_account(&mut state_update, &mut account, &account_id, public_key);
        }

        // Fund each gas key with different amounts
        let deposit_amounts = [
            Balance::from_yoctonear(100_000),
            Balance::from_yoctonear(200_000),
            Balance::from_yoctonear(300_000),
        ];
        for (public_key, amount) in public_keys.iter().zip(deposit_amounts.iter()) {
            transfer_to_gas_key(&mut state_update, &account_id, public_key, *amount);
        }
        state_update.commit(StateChangeCause::InitialState);

        let action_result = test_delete_account(
            &account_id,
            AccountContract::from_local_code_hash(CryptoHash::default()),
            100,
            PROTOCOL_VERSION,
            &mut state_update,
        );
        assert!(action_result.result.is_ok());

        // Verify total burned balance equals sum of all gas key balances
        let expected_burnt =
            deposit_amounts.iter().fold(Balance::ZERO, |acc, x| acc.checked_add(*x).unwrap());
        assert_eq!(action_result.tokens_burnt, expected_burnt);
        let expected_compute: u64 = public_keys
            .iter()
            .map(|pk| expected_nonce_remove_compute(&account_id, pk, TEST_NUM_NONCES as usize))
            .sum();
        assert_eq!(action_result.compute_usage, expected_compute);
    }
```

**File:** runtime/runtime/src/lib.rs (L749-757)
```rust
            Action::TransferToGasKey(transfer_to_gas_key) => {
                metrics::ACTION_CALLED_COUNT.transfer_to_gas_key.inc();
                action_transfer_to_gas_key(
                    state_update,
                    &mut result,
                    account_id,
                    transfer_to_gas_key,
                )?;
            }
```

**File:** runtime/near-vm-runner/src/logic/logic.rs (L3242-3244)
```rust
        self.result_state.deduct_balance(amount)?;
        self.ext.append_action_transfer_to_gas_key(receipt_idx, public_key_res?, amount);
        Ok(())
```

**File:** runtime/runtime/src/actions.rs (L354-375)
```rust
    let gas_key_balance_to_burn = compute_gas_key_balance_sum(state_update, account_id)?;
    if gas_key_balance_to_burn > GasKeyInfo::MAX_BALANCE_TO_BURN {
        result.result = Err(ActionErrorKind::GasKeyBalanceTooHigh {
            account_id: account_id.clone(),
            public_key: None,
            balance: gas_key_balance_to_burn,
        }
        .into());
        return Ok(());
    }
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
```
