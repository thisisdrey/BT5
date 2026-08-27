Confirmed: at line 853, `let mut account = get_account(state_update, account_id)?;` is executed fresh for **each** receipt, reading the current state of `state_update` at that point. Receipts are processed strictly **sequentially**, not concurrently: `process_local_receipts` (`runtime/runtime/src/lib.rs:2380-2455`) iterates the local receipts one at a time, fully executing and committing (or rolling back) each one via `apply_action_receipt` before the next receipt is even started, and this happens entirely before `process_incoming_receipts` begins (`runtime/runtime/src/lib.rs:2694-2715`). [1](#0-0) [2](#0-1) 

There is no "race" between the local and the incoming receipt for the same not-yet-created implicit account: the local receipt runs to completion first (either committing the newly created account via `set_account` at line 959 after `check_storage_stake` passes, or rolling back the whole receipt on failure), and only then does the incoming receipt's `apply_action_receipt` call `get_account` again — at which point it will observe the account as already existing (`account.is_some()`). In `action_transfer_or_implicit_account_creation` (`runtime/runtime/src/lib.rs:2910-2958`), the `Some(account)` branch is taken, which calls plain `action_transfer(account, deposit)` (crediting balance only, no new key, no storage-usage recomputation) rather than `action_implicit_account_creation_transfer`. [3](#0-2) 

This is exactly the behavior verified by the existing integration test `transfer_tokens_to_implicit_account` in `integration-tests/src/tests/standard_cases/mod.rs:495-594`, which sends two sequential transfers to the same not-yet-created implicit account and asserts that only the first creates the account/key (charged the `extra_account_creation_charge`) while the second is a plain credit, with final balance equal to the sum of both transfers and no double key-creation. [4](#0-3) 

There is no attacker-controlled mechanism to make two receipts targeting the same shard/receiver execute concurrently or out-of-order such that both see `account = None` simultaneously — `Runtime::apply` is a single-threaded, deterministic state-transition function per chunk (`docs/RuntimeSpec/Components/RuntimeCrate.md:64-106`), and account reads/writes for a receiver are always serialized through the same `TrieUpdate`/`state_update`. [5](#0-4) 

#No vulnerability found for this question.

### Citations

**File:** runtime/runtime/src/lib.rs (L847-854)
```rust
        // state_update might already have some updates so we need to make sure we commit it before
        // executing the actual receipt
        state_update.commit(StateChangeCause::ActionReceiptProcessingStarted {
            receipt_hash: receipt.get_hash(),
        });

        let mut account = get_account(state_update, account_id)?;
        let account_did_not_exist = account.is_none();
```

**File:** runtime/runtime/src/lib.rs (L2693-2715)
```rust
        // We first process local receipts. They contain staking, local contract calls, etc.
        self.process_local_receipts(
            processing_state,
            receipt_sink,
            compute_limit,
            &mut validator_proposals,
        )?;

        // Then we process the delayed receipts. It's a backlog of receipts from the past blocks.
        self.process_delayed_receipts(
            processing_state,
            receipt_sink,
            compute_limit,
            &mut validator_proposals,
        )?;

        // And then we process the new incoming receipts. These are receipts from other shards.
        self.process_incoming_receipts(
            processing_state,
            receipt_sink,
            compute_limit,
            &mut validator_proposals,
        )?;
```

**File:** runtime/runtime/src/lib.rs (L2921-2957)
```rust
    Ok(if let Some(account) = account.as_mut() {
        let is_gas_refund = is_refund && action_receipt.signer_id() == receipt.receiver_id();
        // For gas refunds, try to refund to the gas key first. If the signer key is a gas key,
        // the refund goes to the gas key balance and we skip crediting the account balance.
        if is_gas_refund
            && try_refund_gas_key_balance(
                state_update,
                receipt.receiver_id(),
                &action_receipt.signer_public_key(),
                deposit,
            )?
        {
            return Ok(());
        }
        action_transfer(account, deposit)?;
        if is_gas_refund {
            try_refund_allowance(
                state_update,
                receipt.receiver_id(),
                &action_receipt.signer_public_key(),
                deposit,
            )?;
        }
    } else {
        debug_assert!(!is_refund);
        action_implicit_account_creation_transfer(
            state_update,
            &apply_state,
            &apply_state.config.fees,
            account,
            actor_id,
            receipt.receiver_id(),
            deposit,
            apply_state.block_height,
            epoch_info_provider,
        );
    })
```

**File:** integration-tests/src/tests/standard_cases/mod.rs (L559-594)
```rust
    let transaction_result =
        node_user.send_money(account_id.clone(), receiver_id.clone(), tokens_used).unwrap();

    assert_eq!(transaction_result.status, FinalExecutionStatus::SuccessValue(Vec::new()));
    assert_eq!(transaction_result.receipts_outcome.len(), 1 + extra_refund_outcomes());
    let new_root = node_user.get_state_root();
    assert_ne!(root, new_root);
    assert_eq!(node_user.get_access_key_nonce_for_signer(account_id).unwrap(), 2);

    // Only the first transfer creates the account, so only that one carries the
    // AccountCostIncrease account_creation_charge. The second transfer goes to the
    // existing account and only pays the gas portion (which still includes
    // `create_account.exec` because `transfer_exec_fee` keys off the receiver's
    // account-id format, not whether the account exists).
    let second_transfer_cost =
        transfer_cost.checked_sub(fee_helper.extra_account_creation_charge()).unwrap();
    let AccountView { amount, locked, .. } = node_user.view_account(account_id).unwrap();
    assert_eq!(
        (amount, locked),
        (
            TESTING_INIT_BALANCE
                .checked_sub(tokens_used.checked_mul(2).unwrap())
                .unwrap()
                .checked_sub(TESTING_INIT_STAKE)
                .unwrap()
                .checked_sub(transfer_cost)
                .unwrap()
                .checked_sub(second_transfer_cost)
                .unwrap(),
            TESTING_INIT_STAKE
        )
    );

    let AccountView { amount, locked, .. } = node_user.view_account(&receiver_id).unwrap();
    assert_eq!((amount, locked), (tokens_used.checked_mul(2).unwrap(), Balance::ZERO));
}
```

**File:** docs/RuntimeSpec/Components/RuntimeCrate.md (L64-82)
```markdown
## Receipt processing

Receipts are processed one by one in the following order:

1. Previously delayed receipts from the state.
1. New local receipts.
1. New incoming receipts.

After each processed receipt, we compare total gas burnt (so far) with the gas limit.
When the total gas burnt reaches or exceeds the gas limit, the processing stops.
The remaining receipts are considered delayed and stored into the state.

### Delayed receipts

Delayed receipts are stored as a persistent queue in the state.
Initially, the first unprocessed index and the next available index are initialized to 0.
When a new delayed receipt is added, it's written under the next available index in to the state and the next available index is incremented by 1.
When a delayed receipt is processed, it's read from the state using the first unprocessed index and the first unprocessed index is incremented.
At the end of the receipt processing, the all remaining local and incoming receipts are considered to be delayed and stored to the state in their respective order.
```
