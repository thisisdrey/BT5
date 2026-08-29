### Title
Stale postponed/yield receipt state survives `DeleteAccount`, letting the old owner inject actions (e.g. `AddKey`) into a recreated account of the same name - ([File: runtime/runtime/src/actions.rs])

### Summary
`action_delete_account` removes the `Account`, `AccessKey`/`GasKeyNonce`, `ContractCode`, and `ContractData` trie entries for an account, but never removes `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, `ReceivedData`, or the `PromiseYield*` entries keyed by that same `account_id`. Because `CreateAccount` only checks that the account currently does not exist, an attacker can delete an account that has an in-flight self-targeted postponed/yield receipt, have the name recreated (e.g. by selling/transferring a sub-account they control), and later trigger the dangling data dependency so the stale receipt executes against the *new* account, passing `check_actor_permissions` because that check only compares `actor_id == account_id` for a self receipt.

### Finding Description
`TrieKey::PostponedReceiptId`, `TrieKey::PendingDataCount`, and `TrieKey::PostponedReceipt` are all keyed purely by `receiver_id`/`account_id` plus a `data_id`/`receipt_id` [1](#0-0)  — they are not tied to any account-generation counter, so they alias across a delete/recreate cycle of the same name.

`action_delete_account` delegates cleanup entirely to `remove_account`, and that function only strips `Account`, `ContractCode`, access/gas keys, and `ContractData`: [2](#0-1) [3](#0-2) . It never scans or removes `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, `ReceivedData`, or the Promise-Yield family of keys for the deleted `account_id`.

`action_create_account`/`check_account_existence` only verify the account does not currently exist; they do nothing to purge or invalidate leftover receipt-queue state for that name: [4](#0-3) .

When a subsequently-arriving `Data` receipt satisfies the outstanding dependency, `process_receipt` looks up the postponed link purely by `(receiver_id, data_id)`, decrements `PendingDataCount`, fetches the stored `PostponedReceipt`, and executes it against whatever account currently exists at that name: [5](#0-4) . The original receipt was created (and stored) exactly the same way when it was first postponed, independent of the account's later deletion: [6](#0-5) .

Crucially, `check_actor_permissions` for privileged actions (`AddKey`, `DeleteKey`, `DeployContract`, `Stake`, ...) only requires `actor_id == account_id`, with no re-validation against the account's current owner/keys: [7](#0-6) . For a self-receipt (`predecessor_id == receiver_id == account_id`), `actor_id` is initialized to `predecessor_id`, so this check trivially passes regardless of whether the account was deleted and recreated with a new owner in between.

Exploit flow:
1. Attacker controls parent account `attacker.near` and its sub-account `sub.attacker.near`.
2. From a contract deployed on `sub.attacker.near`, attacker schedules a self-callback action receipt containing `AddKey(attacker_backdoor_key)` that depends on an `input_data_id` from a cross-contract call the attacker fully controls (e.g., to a contract that only responds when the attacker triggers it). This receipt is stored as `PostponedReceipt`/`PendingDataCount`/`PostponedReceiptId` keyed by `sub.attacker.near`.
3. Attacker submits `DeleteAccount` on `sub.attacker.near` (allowed — `DeleteAccountAction` only checks locked stake and storage size, not pending receipts) and transfers/sells the name, having `attacker.near` recreate `sub.attacker.near` via `CreateAccount` for a new owner with a fresh access key (allowed since the account no longer exists).
4. At a time of the attacker's choosing, the attacker triggers the awaited cross-contract call to produce the missing `Data` receipt.
5. `process_receipt` resolves the stale `PostponedReceiptId`/`PendingDataCount` for `sub.attacker.near`, executes the old `AddKey(attacker_backdoor_key)` receipt against the new account, and `check_actor_permissions` passes because it is still a self-receipt.
6. The attacker now has an access key on the "sold" account without the new owner's consent — deterministic, reproducible authorization escalation.

### Impact Explanation
This is an authorization-escalation-across-accounts bug: an unprivileged former owner can regain (or grant themselves) a full-access key, deploy a contract, or perform other privileged self-actions on an account now controlled by a different, unsuspecting owner, purely via transaction/receipt timing. This maps to the NEAR bounty category of authorization escalation / theft of account control, and can lead to theft of funds subsequently deposited into the "purchased" account.

### Likelihood Explanation
The precondition is that the attacker controls the parent of the reused name (always true for their own sub-accounts) and can arrange a self-targeted postponed or yield receipt before deletion — both achievable with an ordinary deployed contract and standard promise/cross-contract-call APIs, no validator or node access required. The attacker also fully controls the timing of the data-dependency resolution, making the attack reliably repeatable and requiring only the standard gas/storage cost of a few transactions.

### Recommendation
When deleting an account, `remove_account` (or `action_delete_account`) should also purge all `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, `ReceivedData`, `PromiseYieldReceipt`, `PromiseYieldTimeout`/`PromiseYieldIndices`, and `YieldIdToDataId`/`DataIdToYieldId` entries keyed by that `account_id`, discarding or refunding any in-flight receipts. Alternatively/additionally, `check_actor_permissions` should be strengthened so a resumed postponed/yield self-receipt cannot execute privileged actions unless the account's identity/generation matches what existed when the receipt was created.

### Proof of Concept
Test-loop integration test across two (or more) chunks:
1. Create `sub.attacker.near` and deploy a contract that, on call, issues a cross-contract call to a second controlled contract and schedules a `promise_then` self-callback batch containing `AddKey(pk_backdoor, full_access)`, so a `PostponedReceipt` is stored for `sub.attacker.near` waiting on the second contract's response.
2. In the same or next chunk, submit `DeleteAccount(sub.attacker.near, beneficiary=attacker.near)` and succeed.
3. In a later chunk, submit `CreateAccount(sub.attacker.near)` + `Transfer` + `AddKey(pk_new_owner)` signed by `attacker.near`, simulating transfer to a new owner; assert the account now exists with only `pk_new_owner`.
4. Trigger the second controlled contract to produce its response, resolving the pending `data_id`.
5. Assert that after this chunk, `sub.attacker.near`'s access-key list unexpectedly contains `pk_backdoor` in addition to `pk_new_owner` — demonstrating the old owner reacquired access without the new owner's authorization.

### Citations

**File:** core/primitives/src/trie_key.rs (L200-219)
```rust
    /// Used to store receipt ID `primitives::hash::CryptoHash` for a given receiver's `AccountId`
    /// of the receipt and a given `data_id` (the unique identifier for the required input data).
    /// NOTE: This receipt ID indicates the postponed receipt. We store `receipt_id` for performance
    /// purposes to avoid deserializing the entire receipt.
    PostponedReceiptId {
        receiver_id: AccountId,
        data_id: CryptoHash,
    } = col::POSTPONED_RECEIPT_ID,
    /// Used to store the number of still missing input data `u32` for a given receiver's
    /// `AccountId` and a given `receipt_id` of the receipt.
    PendingDataCount {
        receiver_id: AccountId,
        receipt_id: CryptoHash,
    } = col::PENDING_DATA_COUNT,
    /// Used to store the postponed receipt `primitives::receipt::Receipt` for a given receiver's
    /// `AccountId` and a given `receipt_id` of the receipt.
    PostponedReceipt {
        receiver_id: AccountId,
        receipt_id: CryptoHash,
    } = col::POSTPONED_RECEIPT,
```

**File:** core/store/src/utils/mod.rs (L505-519)
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
```

**File:** core/store/src/utils/mod.rs (L551-574)
```rust
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

**File:** runtime/runtime/src/actions.rs (L794-817)
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
```

**File:** runtime/runtime/src/lib.rs (L1396-1443)
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
