### Title
Account deletion leaves dangling PostponedReceipt/PostponedReceiptId/PendingDataCount entries, enabling authorization escalation on a re-created account of the same name - (File: core/store/src/utils/mod.rs / runtime/runtime/src/actions.rs)

### Summary
`remove_account` only clears `TrieKey::Account`, `TrieKey::ContractCode`, access keys/gas keys, and contract data for the deleted account, but never removes `TrieKey::PostponedReceiptId`, `TrieKey::PendingDataCount`, or `TrieKey::PostponedReceipt` entries that may exist for that account. `action_delete_account` calls `remove_account` unconditionally, with no check that the account has no outstanding postponed receipts waiting on unresolved `DataReceipt`s. This allows a still-pending postponed `ActionReceipt` to survive the deletion and later execute against a freshly re-created account of the same name.

### Finding Description
`remove_account` (core/store/src/utils/mod.rs:505) removes:
- `TrieKey::Account`
- `TrieKey::ContractCode`
- access keys and gas key nonces (via `get_raw_prefix_for_access_keys`)
- contract data (via `get_raw_prefix_for_contract_data`) [1](#0-0) 

It never iterates or removes `TrieKey::PostponedReceiptId`, `TrieKey::PendingDataCount`, or `TrieKey::PostponedReceipt` prefixes for the account. `action_delete_account` (runtime/runtime/src/actions.rs:314) checks only storage-usage size and gas-key balance limits before calling `remove_account`, with no validation that the account has zero pending/postponed data-dependent receipts: [2](#0-1) 

Exploit flow: attacker's contract X issues a self-callback `FunctionCall` (`predecessor_id == X`, `receiver_id == X`) that creates an unresolved `input_data_id`, causing `process_action_receipt` to postpone the receipt and write `PostponedReceiptId`, `PendingDataCount`, and the `PostponedReceipt` blob keyed to X. Before the matching `DataReceipt` is delivered, the attacker submits a `DeleteAccountAction` transaction on X (a normal owner action, not requiring elevated privilege) which reaches `action_delete_account` → `remove_account`, deleting the account record but leaving the postponed-receipt trie entries intact. Any party can then submit `CreateAccount` for X again with a new key. When the deferred `DataReceipt` eventually arrives (or is replayed), `process_receipt` finds the surviving `PendingDataCount`, decrements it to zero, fetches the surviving `PostponedReceipt`, and executes its actions (`AddKey`/`DeployContract`/`Transfer`, all carrying `predecessor_id == X`) against the newly created account — since receipt execution trusts the receiver-account identity and does not re-validate signatures/access keys for internal receipts, this grants the pre-deletion receipt owner-level control over the new account incarnation without the new owner's consent.

No existing check (signature, nonce, access-key permission, storage-staking) rejects this: `DeleteAccountAction` validation only concerns storage size/gas-key balance, and account-recreation (`CreateAccount`) has no dependency on whether stale postponed data exists for that name.

### Impact Explanation
This is an authorization-escalation-across-accounts/promises bug: the resurrected postponed receipt executes privileged actions (`AddKey`, `DeployContract`, `Transfer`) on the re-created account without any authorization from its new owner, matching NEAR's "authorization exactness" invariant violation. Depending on the postponed receipt's action list, an attacker could implant a backdoor key or steal funds transferred into the new account, i.e., theft or compromise of a legitimately re-created account.

### Likelihood Explanation
The attacker needs only ordinary account/contract deployment ability (no special privilege): deploy a self-calling contract that creates a postponed receipt, then submit a normal `DeleteAccountAction`. Precise timing (delete between postponement and data-receipt delivery, across shard/block boundaries) is required and depends on the receiving chunk's processing order, which raises the difficulty but is feasible for a determined attacker who controls both the postponing transaction and the delete transaction and can also control/delay when the dependent data receipt executes (e.g., via chained cross-shard calls). The account-name reuse also depends on some third party re-creating account X, or the attacker doing so themselves, which is straightforward since NEAR account-name reuse after deletion is otherwise permitted.

### Recommendation
`remove_account` should also enumerate and remove all `TrieKey::PostponedReceiptId`, `TrieKey::PendingDataCount`, and `TrieKey::PostponedReceipt` entries for the account before allowing deletion to complete, and/or `action_delete_account` should reject deletion (returning an `ActionErrorKind` such as `DeleteAccountWithPendingReceipts`) whenever the account still has outstanding postponed action receipts or pending data counts.

### Proof of Concept
Integration/apply-path test (test-loop or runtime apply test) plan:
1. Deploy a self-callback contract to account X (e.g., using `call_promise`/cross-contract-call pattern) that issues a batch: `FunctionCall` to X plus a promise dependent on an unresolved `input_data_id`, forcing `process_action_receipt` to write `PostponedReceiptId`/`PendingDataCount`/`PostponedReceipt` for X (assert these trie keys exist via `TrieUpdate`/state inspection after the apply).
2. In the same or next block, submit a `DeleteAccountAction` transaction from X's owner key; assert the account record is gone (`view_account_query` returns `UnknownAccount`) but the `PostponedReceipt`/`PendingDataCount` trie entries for X still exist (read directly via `core/store` utils).
3. Submit `CreateAccount` (with a new `AddKey` for a new owner) re-creating X.
4. Deliver the previously-outstanding `DataReceipt` targeting X's postponed receipt.
5. Assert that the postponed receipt's actions (e.g., an `AddKey` or `Transfer`) executed against the *new* incarnation of X, and that the new owner's key set/balance/contract reflects unauthorized changes originating from the pre-deletion receipt — demonstrating authorization escalation across the account-name boundary.

### Citations

**File:** core/store/src/utils/mod.rs (L505-524)
```rust
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
```

**File:** runtime/runtime/src/actions.rs (L364-389)
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
        let compute = storage_removes_compute(
            &config.wasm_config.ext_costs,
            remove_result.gas_key_nonce_count,
            remove_result.gas_key_nonce_total_key_bytes,
            AccessKey::NONCE_VALUE_LEN * remove_result.gas_key_nonce_count,
        );
        result.compute_usage = safe_add_compute(result.compute_usage, compute).map_err(|_| {
            StorageError::StorageInconsistentState("compute_usage overflow".to_string())
        })?;
    }
    *actor_id = receipt.predecessor_id().clone();
    *account = None;
    Ok(())
```
