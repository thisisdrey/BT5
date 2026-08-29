I have enough evidence to answer confidently.

Key finding: `promise_batch_action_transfer` calls `self.result_state.deduct_balance(amount)?` at receipt-creation time (see `runtime/near-vm-runner/src/logic/logic.rs:3175`), which deducts the deposit from the *creating* account's balance immediately when the promise/receipt is generated — before that receipt is postponed, forwarded, or executed. This is the crux of why the described attack doesn't mint tokens.This confirms the analysis. The documentation explicitly states the balance-checker model that governs this scenario: `docs/RuntimeSpec/Components/RuntimeCrate.md:119` says "Initial balances for the postponed receipts... the data receipts are 0 cost, because they are completely prepaid when issued." This directly refutes the premise that a stale postponed `Transfer` represents un-backed value.

### Title
No vulnerability — postponed self-Transfer receipts are pre-funded at creation time, not at delivery - ([File: core/store/src/utils/mod.rs])

### Summary
The scenario assumes a postponed `ActionReceipt` carrying a `TransferAction` represents tokens that have not yet been deducted from any balance until delivery, so that surviving account deletion would let the transfer mint new value on redelivery. This is false: NEAR's receipt model deducts the transfer `deposit` from the *creating* account's balance at the moment the receipt (promise) is generated, not at the moment it is delivered/executed. By the time the account is deleted and its current balance is paid out to the beneficiary, the deposit for the pending self-receipt has already left the account's balance — it is "in flight" exactly like any cross-shard or cross-block receipt.

### Finding Description
`promise_batch_action_transfer` (`runtime/near-vm-runner/src/logic/logic.rs:3139-3178`) calls `self.result_state.deduct_balance(amount)?` immediately when the `Transfer` action is appended to a new outgoing receipt [1](#0-0) . This happens during the *creating* receipt's execution — before the new receipt is even assigned a receipt ID, forwarded, or postponed. Thus the `deposit` value has already left the creator's on-trie balance the moment the self-targeted receipt is spawned, exactly as with any other outgoing transfer.

`action_delete_account` (`runtime/runtime/src/actions.rs:314-390`) pays out only `account_ref.amount()` — the account's *current* balance at deletion time — to the beneficiary [2](#0-1) . Since the pending self-Transfer's deposit was already subtracted from that balance when the receipt was created, it is correctly excluded from the beneficiary payout — there is no double counting.

The protocol's balance-checker model documents this explicitly: postponed receipts' balances are treated as already-prepaid "incoming balance" entries that must reconcile against final state, exactly like any other in-flight receipt [3](#0-2) . When the postponed receipt is later executed via `apply_action_receipt` → `action_transfer_or_implicit_account_creation` → `action_transfer`, it simply credits the (recreated) account by `deposit` [4](#0-3) , which is the balancing entry for the earlier deduction — not new value.

`remove_account` (`core/store/src/utils/mod.rs:505-575`) only removes the `Account`, `ContractCode`, access keys, and contract data trie entries; it never touches `PostponedReceipt`/`PendingDataCount`/`ReceivedData` keys tied to `receipt_id`s or `data_id`s [5](#0-4) . This asymmetry is real and intentional — postponed receipts are keyed by `receiver_id` + `receipt_id`/`data_id` independent of whether an `Account` record exists — but it does not create a minting bug because the value was already accounted for outside the account's balance field at creation time.

### Impact Explanation
No impact. The scenario does not cause token minting or any balance-conservation violation. The `deposit` for the self-Transfer is deducted from the account at receipt-creation time (before deletion), so paying out the account's current balance to the beneficiary at deletion and later crediting the recreated account when the stale receipt executes is balance-neutral — value simply moves from "in-flight receipt" bookkeeping to the recreated account, matching how any normal in-flight receipt (cross-shard, cross-block, or postponed-on-data-dependency) is already handled and asserted by NEAR's balance checker.

### Likelihood Explanation
Not applicable — no exploitable condition exists.

### Recommendation
No fix needed for the scenario as described. If there is separate concern about `remove_account` not cleaning up `PostponedReceipt`/`PendingDataCount`/`ReceivedData`/`PostponedReceiptId` trie entries on account deletion (a storage-hygiene/dangling-key question, not a fund-safety one), that would need to be evaluated independently against the "reject speculative resource-hygiene claims" rule, and is out of scope for this token-conservation question.

### Proof of Concept
Not applicable — the described PoC (asserting `sum(balances)` increases by the deposit amount with no compensating deduction) would fail because the deduction already occurred at `promise_batch_action_transfer` time; a corrected integration test would show `sum(balances)` unchanged across delete + recreate + stale-receipt-execution, consistent with existing tests like `runtime/runtime/src/tests/apply.rs:5442-5449` (`supply leak` assertion pattern) and the account-cost balance-delta checks in `test-loop-tests/src/tests/account_cost_increase_diff.rs:489-495`.

### Citations

**File:** runtime/near-vm-runner/src/logic/logic.rs (L3170-3177)
```rust
        self.result_state.gas_counter.pay_action_accumulated(
            send_fee,
            use_gas,
            ActionCosts::transfer,
        )?;
        self.result_state.deduct_balance(amount)?;
        self.ext.append_action_transfer(receipt_idx, amount);
        Ok(())
```

**File:** runtime/runtime/src/actions.rs (L160-165)
```rust
pub(crate) fn action_transfer(account: &mut Account, deposit: Balance) -> Result<(), StorageError> {
    account.set_amount(account.amount().checked_add(deposit).ok_or_else(|| {
        StorageError::StorageInconsistentState("Account balance integer overflow".to_string())
    })?);
    Ok(())
}
```

**File:** runtime/runtime/src/actions.rs (L364-370)
```rust
    // We use current amount as a pay out to beneficiary.
    let account_balance = account_ref.amount();
    if account_balance > Balance::ZERO {
        result
            .new_receipts
            .push(Receipt::new_balance_refund(&delete_account.beneficiary_id, account_balance));
    }
```

**File:** docs/RuntimeSpec/Components/RuntimeCrate.md (L118-122)
```markdown
- Balances for the processed delayed receipts.
- Initial balances for the postponed receipts. Postponed receipts are receipts from the previous blocks that were processed, but were not executed.
  They are action receipts with some expected incoming data. Usually for a callback on top of awaited promise.
  When the expected data arrives later than the action receipt, then the action receipt is postponed.
  Note, the data receipts are 0 cost, because they are completely prepaid when issued.
```

**File:** core/store/src/utils/mod.rs (L504-575)
```rust
/// Removes account, code and all access keys and gas keys associated to it.
pub fn remove_account(
    state_update: &mut TrieUpdate,
    account_id: &AccountId,
) -> Result<RemoveAccountResult, StorageError> {
    state_update.remove(TrieKey::Account { account_id: account_id.clone() });
    state_update.remove(TrieKey::ContractCode { account_id: account_id.clone() });

    let mut gas_key_nonce_count: usize = 0;
    let mut gas_key_nonce_total_key_bytes: usize = 0;

    // Removing access keys and gas key nonces
    let lock = state_update.trie().lock_for_iter();
    let mut keys_to_remove: Vec<TrieKey> = Vec::new();
    for raw_key in state_update
        .locked_iter(&trie_key_parsers::get_raw_prefix_for_access_keys(account_id), &lock)?
    {
        let raw_key = raw_key?;
        let key_handle = trie_key_parsers::parse_key_handle_from_access_key_key(
            &raw_key, account_id,
        )
        .map_err(|_e| {
            StorageError::StorageInconsistentState(
                "Can't parse key handle from raw key for AccessKey".to_string(),
            )
        })?;
        let nonce_index =
            trie_key_parsers::parse_nonce_index_from_gas_key_key(&raw_key, account_id, &key_handle)
                .map_err(|_e| {
                    StorageError::StorageInconsistentState(
                        "Can't parse nonce index from raw key for AccessKey".to_string(),
                    )
                })?;
        if let Some(index) = nonce_index {
            gas_key_nonce_count += 1;
            gas_key_nonce_total_key_bytes += raw_key.len();
            keys_to_remove.push(TrieKey::gas_key_nonce(
                account_id.clone(),
                key_handle.clone(),
                index,
            ));
        } else {
            keys_to_remove.push(TrieKey::access_key(account_id.clone(), key_handle.clone()));
        }
    }
    drop(lock);

    for trie_key in keys_to_remove {
        state_update.remove(trie_key);
    }

    // Removing contract data
    let lock = state_update.trie().lock_for_iter();
    let data_keys = state_update
        .locked_iter(&trie_key_parsers::get_raw_prefix_for_contract_data(account_id, &[]), &lock)?
        .map(|raw_key| {
            trie_key_parsers::parse_data_key_from_contract_data_key(&raw_key?, account_id)
                .map_err(|_e| {
                    StorageError::StorageInconsistentState(
                        "Can't parse data key from raw key for ContractData".to_string(),
                    )
                })
                .map(Vec::from)
        })
        .collect::<Result<Vec<_>, _>>()?;
    drop(lock);

    for key in data_keys {
        state_update.remove(TrieKey::ContractData { account_id: account_id.clone(), key });
    }
    Ok(RemoveAccountResult { gas_key_nonce_count, gas_key_nonce_total_key_bytes })
}
```
