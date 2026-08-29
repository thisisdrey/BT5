This confirms the exploit path is viable: `check_actor_permissions` at `runtime/runtime/src/actions.rs:753` and `check_account_existence` at `:846` only test string equality of `account_id` and whether the (freshly-fetched) `account` `Option` is `Some`/`None` — neither check has any notion of "same underlying account instance." When the stale postponed `Stake` receipt resumes, `account_id` matches the newly created account's `account_id`, so `check_account_existence` sees `account.is_some()` (true, it's the new account) and `check_actor_permissions` sees `actor_id == account_id` (true, both are the string `A`), and the action proceeds against the new account's balance.

### Title
Stale postponed action receipt executes against a reused top-level account name, forging a validator stake proposal without owner consent - (File: runtime/runtime/src/lib.rs, core/store/src/utils/mod.rs)

### Summary
`process_action_receipt` stores a data-dependent `Stake` self-call as a postponed receipt under `TrieKey::PostponedReceiptId`/`PendingDataCount`/`PostponedReceipt`, all keyed only by the receiver's `AccountId` string. `remove_account` (invoked by `action_delete_account`) deletes the `Account`, `ContractCode`, access keys, gas keys, and contract data, but never removes these postponed-receipt bookkeeping keys. If the account is a long top-level name (≥ `min_allowed_top_level_account_length`), anyone can recreate an account with the identical name after deletion; when the withheld `Data` receipt later arrives, `process_receipt`'s `Data` branch resumes and executes the stale `Stake` action against the new, unrelated account, because `check_actor_permissions`/`check_account_existence` only compare account-id strings with no binding to the account's "generation."

### Finding Description
`process_action_receipt` (`runtime/runtime/src/lib.rs:1593-1658`) writes, for each unmet `input_data_id`:
- `TrieKey::PostponedReceiptId { receiver_id, data_id } -> receipt_id`
- `TrieKey::PendingDataCount { receiver_id, receipt_id } -> count`
- the receipt itself via `set_postponed_receipt` under `TrieKey::PostponedReceipt { receiver_id, receipt_id }` [1](#0-0) 

None of these keys embed any "epoch" or "generation" of the account — only its `AccountId`. `action_delete_account` (`runtime/runtime/src/actions.rs:314-389`) requires `locked == 0` and calls `remove_account`: [2](#0-1) 

which removes only `Account`, `ContractCode`, access/gas keys, and contract data — leaving any `PostponedReceiptId`/`PendingDataCount`/`PostponedReceipt` entries for that account untouched in the trie.

Because `min_allowed_top_level_account_length = 32` on mainnet/testnet, an ordinary top-level account name of ≥32 characters can be created by *anyone*, without registrar permission, per `action_create_account` (`runtime/runtime/src/actions.rs:167-210`, check at `:176-190`). `check_account_existence`'s `CreateAccount` branch only rejects if `account.is_some()` — once the old account is deleted, this is `None`, so a completely different actor can create a fresh account under the same name. [3](#0-2) 

When the withheld `Data` receipt finally arrives, `process_receipt`'s `Data` branch (`runtime/runtime/src/lib.rs:1386-1455`) looks up `PostponedReceiptId`/`PendingDataCount`/`PostponedReceipt` purely by the receiver `AccountId`, finds the stale entries still present, and calls `apply_action_receipt` on the stale `Stake` receipt: [4](#0-3) 

`apply_action_receipt` then fetches the *current* account under that id (`get_account(state_update, account_id)`, `runtime/runtime/src/lib.rs:853`) — the newly created, unrelated account — and dispatches the `Stake` action. `check_actor_permissions` for `Stake` only asserts `actor_id == account_id` (both being the string `A`, since the stale receipt's `predecessor_id == receiver_id == A`): [5](#0-4) 

There is no check tying the receipt's `predecessor_id`/`receiver_id` to the specific account "instance" that existed when the receipt was postponed — the whole authorization model is name-based, and the name was legitimately reassigned in between.

### Impact Explanation
This produces a forged `ValidatorStake` proposal for an account whose real owner never signed a `StakeAction`, moving `amount` into `locked` on their behalf and submitting them as a validator candidate without consent. This is an authorization-escalation across accounts/promises (a self-call authorization check that is supposed to guarantee "the account authorized this against itself" is defeated by identity/name reuse), and it corrupts subsequent epoch reward/inflation accounting for the wrongly-staked account, matching the NEP scoped impact described.

### Likelihood Explanation
Preconditions: the parked account must be a top-level account of length ≥ `min_allowed_top_level_account_length` (32 chars) so it can be freely recreated by a third party; the original account owner must never have staked (`locked == 0`) so it can self-delete; and the attacker must be able to control/delay the arrival of the single `Data` receipt satisfying the postponed `Stake` receipt's `input_data_id` long enough to delete and have the name recreated. All of this is achievable by an ordinary funded account: deploy a contract on the long-named account A that issues a cross-contract call whose callback batch attaches a `Stake` action (`promise_batch_then` + `promise_batch_action_stake`), then send a separate `DeleteAccount` receipt to A before the callback's data resolves. The "different validator-candidate recreating the same name" step depends on an independent third party choosing that exact freed name, which is a probabilistic/timing-dependent precondition rather than something the attacker fully controls end-to-end — this substantially lowers real-world likelihood/repeatability even though the underlying code defect (no cleanup of postponed-receipt trie keys on account deletion) is concrete and reproducible in a unit test.

### Recommendation
On `action_delete_account`/`remove_account`, also purge all `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, and `ReceivedData` trie entries whose `receiver_id` equals the deleted account (mirroring the existing iteration used for access keys and contract data in `remove_account`, `core/store/src/utils/mod.rs:504-575`). Alternatively/additionally, reject `DeleteAccount` when any postponed receipt or pending data-count entry still exists for that account, similar to the existing `locked != 0` guard.

### Proof of Concept
Runtime `apply` test (in `runtime/runtime/src/tests/apply.rs` style, using `setup_runtime`/`create_receipt_with_actions` helpers):
1. Create long top-level account `A` (len ≥ 32) funded, with a deployed contract.
2. Submit a receipt/tx causing `A` to issue a two-receipt promise chain: an outer call to `B`, and a `then` receipt back to `A` containing `Action::Stake` with `input_data_ids = [pending_data_id]` — apply this chunk and assert `get_postponed_receipt(state, &A, receipt_id)` is `Some` and `TrieKey::PendingDataCount{A, receipt_id}` exists, while the `B`-receipt/its `Data` reply has not yet been applied.
3. In the same or next chunk, submit `DeleteAccount{beneficiary_id: attacker2}` for `A` (predecessor==receiver==A, locked==0). Apply and assert `get_account(state, &A)` is `None`, but `get_postponed_receipt(state, &A, receipt_id)` is still `Some` (bug confirmed).
4. Submit a `CreateAccount` + `AddKey` + `Transfer` receipt recreating `A` under a different signer/owner; apply and assert the account exists with fresh balance/`locked == 0`.
5. Deliver the pending `Data` receipt (matching `pending_data_id`) to `A`; apply and assert a `ValidatorStake` proposal appears in `apply_result.validator_proposals` for account `A`, and that `A`'s `locked` balance changed, despite no `StakeAction` ever being signed by `A`'s real (new) owner's key.

### Citations

**File:** runtime/runtime/src/lib.rs (L1396-1455)
```rust
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

**File:** core/store/src/utils/mod.rs (L504-510)
```rust
/// Removes account, code and all access keys and gas keys associated to it.
pub fn remove_account(
    state_update: &mut TrieUpdate,
    account_id: &AccountId,
) -> Result<RemoveAccountResult, StorageError> {
    state_update.remove(TrieKey::Account { account_id: account_id.clone() });
    state_update.remove(TrieKey::ContractCode { account_id: account_id.clone() });
```

**File:** runtime/runtime/src/actions.rs (L750-760)
```rust
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

**File:** runtime/runtime/src/actions.rs (L794-818)
```rust
    match action {
        Action::CreateAccount(_) => {
            if account.is_some() {
                return Err(ActionErrorKind::AccountAlreadyExists {
                    account_id: account_id.clone(),
                }
                .into());
            } else {
                if account_is_implicit(account_id, config.wasm_config.eth_implicit_accounts) {
                    // If the account doesn't exist and it's implicit, then you
                    // should only be able to create it using single transfer action.
                    // Because you should not be able to add another access key to the account in
                    // the same transaction.
                    // Otherwise you can hijack an account without having the private key for the
                    // public key. We've decided to make it an invalid transaction to have any other
                    // actions on the implicit hex accounts.
                    // The easiest way is to reject the `CreateAccount` action.
                    // See https://github.com/nearprotocol/NEPs/pull/71
                    return Err(ActionErrorKind::OnlyImplicitAccountCreationAllowed {
                        account_id: account_id.clone(),
                    }
                    .into());
                }
            }
        }
```
