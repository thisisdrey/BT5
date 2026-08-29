## Analysis Result

I confirmed the mechanics of the described attack are real and unmitigated in this codebase.

### Title
Postponed action receipts survive account deletion and are resolved against the recreated account, allowing cross-incarnation privilege escalation via `AddKey` - (File: `runtime/runtime/src/lib.rs`, `core/store/src/utils/mod.rs`)

### Summary
A postponed `ActionReceipt` is stored under `TrieKey::PostponedReceipt{receiver_id, receipt_id}`, keyed only by the account *name*, with no incarnation/creation-nonce binding. `remove_account` (called by `action_delete_account`) never clears `PostponedReceipt`, `PendingDataCount`, or `PostponedReceiptId` entries, so a self-targeted postponed receipt containing `AddKey(attacker_pubkey, FullAccess)` survives `DeleteAccount`. When the account name is later recreated by its parent and the missing `DataReceipt` finally arrives, the runtime resolves the stale receipt and executes it against the brand-new account, because `check_actor_permissions` only compares `actor_id == account_id` by string equality, and `actor_id` is seeded from the original receipt's `predecessor_id` (itself, `sub.parent.near`), which trivially matches the new incarnation's name too.

### Finding Description
1. Attacker owns `sub.parent.near` and, via a normal cross-contract-call/promise chain from their own deployed contract, causes a self-addressed `ActionReceipt{predecessor_id: sub.parent.near, receiver_id: sub.parent.near, actions: [AddKey(attacker_pubkey, FullAccess)], input_data_ids: [D]}` to be created, where `D` is not yet satisfied. This is stored via `process_action_receipt` → `set_postponed_receipt` at `TrieKey::PostponedReceipt{receiver_id: sub.parent.near, receipt_id}` [1](#0-0) , plus a `PostponedReceiptId` link and `PendingDataCount` counter.
2. The attacker submits `DeleteAccount` from `sub.parent.near` on itself (passes `check_actor_permissions` trivially since `actor_id == account_id` and locked stake is zero) [2](#0-1) . This calls `remove_account`, which only removes `Account`, `ContractCode`, access keys, gas-key nonces, and contract data — it never touches `PostponedReceipt`, `PendingDataCount`, or `PostponedReceiptId` [3](#0-2) .
3. `parent.near` later creates a fresh `sub.parent.near` for a new owner via `action_create_account`, which only validates namespace/top-level rules and initializes a blank `Account` — it performs no check for leftover postponed-receipt bookkeeping under that account name [4](#0-3) .
4. When the `DataReceipt` for `D` eventually arrives, `process_receipt`'s `Data` branch looks up `PostponedReceiptId`/`PendingDataCount` and, once satisfied, fetches `get_postponed_receipt(state_update, account_id, receipt_id)` — purely by name and receipt hash — and executes it via `apply_action_receipt` [5](#0-4) .
5. Inside `apply_action_receipt`, `actor_id` is re-seeded from the *stored* (stale) receipt's `predecessor_id` (`sub.parent.near`) [6](#0-5) , and `check_actor_permissions` for `AddKey` only checks `actor_id != account_id` by name [7](#0-6) . Since both are the string `sub.parent.near`, the check passes and `action_add_key` grants the attacker's public key `FullAccess` on the new owner's account.

The root cause is that `TrieKey::PostponedReceipt` (and its sibling keys) are keyed solely by `AccountId` with no incarnation identifier [8](#0-7) , so the trie cannot distinguish "the old `sub.parent.near`" from "the new `sub.parent.near`", and no code path invalidates postponed state on deletion or recreation.

### Impact Explanation
This breaks the authorization-exactness invariant: a promise created and authorized entirely by the account's *previous* owner is executed with full privileges against the account's *new* owner, without the new owner ever consenting. Concretely, the attacker obtains a `FullAccess` key on the recreated account, enabling theft of any funds deposited into it and full control (deploy contracts, transfer, stake, delete). This matches the "authorization escalation across accounts" / "theft of user funds" bounty categories.

### Likelihood Explanation
- Attacker only needs to be an unprivileged owner of a normal (non-top-level) account and to deploy their own contract to construct a self-targeted promise chain with an unresolved data dependency — both ordinary, permissionless operations.
- No signature, nonce, access-key, or size-limit check touches this path; `check_actor_permissions` and `check_account_existence` are name-based only.
- The scenario does not require same-chunk timing; postponed receipts persist across blocks, so the attacker has ample time between `DeleteAccount` and re-creation to trigger resolution.
- The only external precondition is that a third party (e.g. `parent.near`, a registrar/faucet contract) recreates the exact same account name after deletion — a common real-world pattern (subaccount reuse/registration flows).
- Repeatable per victim account name; cost is only gas for a few transactions.

### Recommendation
Bind postponed-receipt bookkeeping (`PostponedReceipt`, `PendingDataCount`, `PostponedReceiptId`) to the account's lifetime: either (a) have `remove_account` (or `action_delete_account`) enumerate and purge all postponed-receipt state for the deleted account, refunding/failing those in-flight receipts, or (b) include an account "incarnation" identifier (e.g., a creation nonce/height) in the trie keys and in the stored receipt's execution context, and have `check_actor_permissions`/resolution reject execution if the current account incarnation differs from the one the receipt was created against.

### Proof of Concept
Runtime apply-path integration test (test-loop or `runtime/runtime/src/tests/apply.rs` style), spanning multiple `apply()` calls to simulate blocks:
1. Set up `parent.near` and `sub.parent.near` (owned by attacker key A).
2. Chunk 1: submit two receipts targeting `sub.parent.near`: (a) an `ActionReceipt` with `input_data_ids: [D]`, `actions: [AddKey(attacker_pubkey, FullAccess)]`, `predecessor_id/receiver_id = sub.parent.near` (gets postponed); (b) nothing else yet — assert `get_postponed_receipt(state, sub.parent.near, receipt_id)` is `Some`.
3. Chunk 2: submit `DeleteAccount` receipt from `sub.parent.near` to itself (beneficiary = attacker). Assert account no longer exists but `get_postponed_receipt` for the same key is still `Some` (proving the leak).
4. Chunk 3: submit `CreateAccount` receipt from `parent.near` to `sub.parent.near`, plus an `AddKey(new_owner_pubkey, FullAccess)` from the new owner.
5. Chunk 4: submit the missing `DataReceipt` for `D` targeting `sub.parent.near`.
6. Assert: after chunk 4, `get_access_key(state, sub.parent.near, attacker_pubkey)` returns `Some(FullAccess)` — demonstrating the escalation — while the invariant expected by the question ("only new owner's keys exist") is violated.

### Citations

**File:** runtime/runtime/src/lib.rs (L853-856)
```rust
        let mut account = get_account(state_update, account_id)?;
        let account_did_not_exist = account.is_none();
        let mut actor_id = receipt.predecessor_id().clone();
        let mut result = ActionReceiptResult::new();
```

**File:** runtime/runtime/src/lib.rs (L1395-1455)
```rust
                // Check if there is already a receipt that was postponed and was awaiting for the
                // given data_id.
                // If we don't have a postponed receipt yet, we don't need to do anything for now.
                if let Some(receipt_id) = get(
                    state_update,
                    &TrieKey::PostponedReceiptId {
                        receiver_id: account_id.clone(),
                        data_id: data_receipt.data_id,
                    },
                )? {
                    // There is already a receipt that is awaiting for the just received data.
                    // Removing this pending data_id for the receipt from the state.
                    state_update.remove(TrieKey::PostponedReceiptId {
                        receiver_id: account_id.clone(),
                        data_id: data_receipt.data_id,
                    });
                    // Checking how many input data items is pending for the receipt.
                    let pending_data_count: u32 = get(
                        state_update,
                        &TrieKey::PendingDataCount { receiver_id: account_id.clone(), receipt_id },
                    )?
                    .ok_or_else(|| {
                        StorageError::StorageInconsistentState(
                            "pending data count should be in the state".to_string(),
                        )
                    })?;
                    if pending_data_count == 1 {
                        // It was the last input data pending for this receipt. We'll cleanup
                        // some receipt related fields from the state and execute the receipt.

                        // Removing pending data count from the state.
                        state_update.remove(TrieKey::PendingDataCount {
                            receiver_id: account_id.clone(),
                            receipt_id,
                        });
                        // Fetching the receipt itself.
                        let ready_receipt =
                            get_postponed_receipt(state_update, account_id, receipt_id)?
                                .ok_or_else(|| {
                                    StorageError::StorageInconsistentState(
                                        "pending receipt should be in the state".to_string(),
                                    )
                                })?;
                        // Removing the receipt from the state.
                        remove_postponed_receipt(state_update, account_id, receipt_id);
                        // Executing the receipt. It will read all the input data and clean it up
                        // from the state.
                        return self
                            .apply_action_receipt(
                                state_update,
                                apply_state,
                                pipeline_manager,
                                &ready_receipt,
                                receipt_sink,
                                instant_receipts,
                                validator_proposals,
                                stats,
                                epoch_info_provider,
                                receipt_to_tx,
                            )
                            .map(Some);
```

**File:** runtime/runtime/src/lib.rs (L1608-1655)
```rust
        let mut pending_data_count: u32 = 0;
        for data_id in action_receipt.input_data_ids() {
            if !has_received_data(state_update, account_id, *data_id)? {
                pending_data_count += 1;
                // The data for a given data_id is not available, so we save a link to this
                // receipt_id for the pending data_id into the state.
                set(
                    state_update,
                    TrieKey::PostponedReceiptId {
                        receiver_id: account_id.clone(),
                        data_id: *data_id,
                    },
                    receipt.receipt_id(),
                )
            }
        }

        if pending_data_count == 0 {
            // All input data is available. Executing the receipt. It will cleanup
            // input data from the state.
            return self
                .apply_action_receipt(
                    state_update,
                    apply_state,
                    pipeline_manager,
                    receipt,
                    receipt_sink,
                    instant_receipts,
                    validator_proposals,
                    stats,
                    epoch_info_provider,
                    receipt_to_tx,
                )
                .map(Some);
        } else {
            // Not all input data is available now.
            // Save the counter for the number of pending input data items into the state.
            set(
                state_update,
                TrieKey::PendingDataCount {
                    receiver_id: account_id.clone(),
                    receipt_id: *receipt.receipt_id(),
                },
                &pending_data_count,
            );
            // Save the receipt itself into the state.
            set_postponed_receipt(state_update, receipt);
        }
```

**File:** runtime/runtime/src/actions.rs (L167-210)
```rust
pub(crate) fn action_create_account(
    fee_config: &RuntimeFeesConfig,
    account_creation_config: &AccountCreationConfig,
    account: &mut Option<Account>,
    actor_id: &mut AccountId,
    account_id: &AccountId,
    predecessor_id: &AccountId,
    result: &mut ActionResult,
) {
    if account_id.is_top_level() {
        if account_id.len() < account_creation_config.min_allowed_top_level_account_length as usize
            && predecessor_id != &account_creation_config.registrar_account_id
        {
            // A short top-level account ID can only be created registrar account.
            result.result = Err(ActionErrorKind::CreateAccountOnlyByRegistrar {
                account_id: account_id.clone(),
                registrar_account_id: account_creation_config.registrar_account_id.clone(),
                predecessor_id: predecessor_id.clone(),
            }
            .into());
            return;
        } else {
            // OK: Valid top-level Account ID
        }
    } else if !account_id.is_sub_account_of(predecessor_id) {
        // The sub-account can only be created by its root account. E.g. `alice.near` only by `near`
        result.result = Err(ActionErrorKind::CreateAccountNotAllowed {
            account_id: account_id.clone(),
            predecessor_id: predecessor_id.clone(),
        }
        .into());
        return;
    } else {
        // OK: Valid sub-account ID by proper predecessor.
    }

    *actor_id = account_id.clone();
    *account = Some(Account::new(
        Balance::ZERO,
        Balance::ZERO,
        AccountContract::None,
        fee_config.storage_usage_config.num_bytes_account,
    ));
}
```

**File:** runtime/runtime/src/actions.rs (L739-760)
```rust
pub(crate) fn check_actor_permissions(
    action: &Action,
    account: &Option<Account>,
    actor_id: &AccountId,
    account_id: &AccountId,
) -> Result<(), ActionError> {
    match action {
        Action::DeployContract(_)
        | Action::Stake(_)
        | Action::AddKey(_)
        | Action::DeleteKey(_)
        | Action::DeployGlobalContract(_)
        | Action::UseGlobalContract(_)
        | Action::WithdrawFromGasKey(_) => {
            if actor_id != account_id {
                return Err(ActionErrorKind::ActorNoPermission {
                    account_id: account_id.clone(),
                    actor_id: actor_id.clone(),
                }
                .into());
            }
        }
```

**File:** runtime/runtime/src/actions.rs (L761-776)
```rust
        Action::DeleteAccount(_) => {
            if actor_id != account_id {
                return Err(ActionErrorKind::ActorNoPermission {
                    account_id: account_id.clone(),
                    actor_id: actor_id.clone(),
                }
                .into());
            }
            let account = account.as_ref().unwrap();
            if !account.locked().is_zero() {
                return Err(ActionErrorKind::DeleteAccountStaking {
                    account_id: account_id.clone(),
                }
                .into());
            }
        }
```

**File:** core/store/src/utils/mod.rs (L504-513)
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
```

**File:** core/primitives/src/trie_key.rs (L214-219)
```rust
    /// Used to store the postponed receipt `primitives::receipt::Receipt` for a given receiver's
    /// `AccountId` and a given `receipt_id` of the receipt.
    PostponedReceipt {
        receiver_id: AccountId,
        receipt_id: CryptoHash,
    } = col::POSTPONED_RECEIPT,
```
