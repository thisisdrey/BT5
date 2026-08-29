### Title
Stale self-actor authorization on delayed receipts allows re-created accounts to be hijacked - ([File: runtime/runtime/src/actions.rs])

### Summary
`check_actor_permissions` authorizes self-privileged actions (`AddKey`, `DeleteKey`, `Stake`, `DeployContract`, `DeployGlobalContract`, `UseGlobalContract`, `WithdrawFromGasKey`, `DeleteAccount`) purely by comparing `actor_id` (the receipt's `predecessor_id`) against `account_id` (the receiver) as plain `AccountId` strings, with no concept of account identity/continuity across deletion and re-creation. Because `remove_account` correctly leaves the global `DelayedReceipt{index}` queue untouched, and `validate_receipt` on dequeue only checks size/format (not receiver ownership or existence continuity), a self-addressed action receipt created before an account is deleted can be replayed with full "self" privilege against a completely different, newly re-created account bearing the same name.

### Finding Description
`Runtime::apply_action_receipt` fetches the receiver account fresh at execution time: `let mut account = get_account(state_update, account_id)?;` [1](#0-0)  and initializes `actor_id` from the receipt's stored `predecessor_id`, not from anything tied to the account's current identity.

`apply_action` then calls `check_account_existence` (existence only) and `check_actor_permissions`, which for the self-privileged action set (`AddKey`, `DeleteKey`, `Stake`, `DeployContract`, `DeployGlobalContract`, `UseGlobalContract`, `WithdrawFromGasKey`, `DeleteAccount`) simply requires `actor_id == account_id`: [2](#0-1)  — i.e. it is satisfied whenever `predecessor_id` string-equals `receiver_id` string, regardless of which real-world owner currently controls that account name.

Delayed receipts are stored keyed only by a shard-global monotonic index (`TrieKey::DelayedReceipt { index }`), unrelated to `account_id` [3](#0-2)  and are explicitly account-agnostic (`get_account_id()` returns `None` for this key) [4](#0-3) . `remove_account` deletes `Account`, `ContractCode`, access/gas keys, and contract data for an `account_id`, but never touches `DelayedReceipt`/`DelayedReceiptIndices` rows [5](#0-4) , which is correct since that queue is protocol-global, not account-scoped.

When a delayed receipt is finally dequeued in `process_delayed_receipts`, the only re-validation performed is `validate_receipt(..., ValidateReceiptMode::ExistingReceipt)`, which checks size limits, `AccountId` string well-formedness, and action-list structural limits — it does **not** check that the receiver account still exists in the same ownership state, nor that `predecessor_id` is meaningfully tied to the account's current owner [6](#0-5)  and [7](#0-6) . The receipt then flows into `process_receipt` → `process_action_receipt` → `apply_action_receipt`, which re-fetches the account fresh from current trie state (i.e., whatever account currently exists under that name) and executes the action against it.

**Exploit flow:**
1. Attacker (owner of `d.near`) submits `TX1`: a self-addressed transaction (`signer_id == receiver_id == "d.near"`) containing a privileged self-action, e.g. `AddKey(attacker_pubkey, FullAccess)` or `DeployContract(malicious_wasm)`. Since `signer_id == receiver_id`, this becomes a *local receipt* with `predecessor_id == receiver_id == "d.near"`.
2. The attacker (or ambient chunk congestion) causes the chunk's `compute_limit`/proof-size limit to be exceeded before this local receipt is processed, so it is pushed to the delayed queue instead of executing immediately (`process_local_receipts` overflow path) rather than being tied to the account at all — it lands in `DelayedReceipt{index}` under the shard-global counter.
3. Attacker submits `TX2` (a later transaction/receipt): `DeleteAccount` on `d.near` with `actor_id == account_id == "d.near"` (self-authorized per the same check), refunding balance to a beneficiary and calling `remove_account`, which clears `Account`, keys, and contract data — but leaves the previously-enqueued delayed `AddKey`/`DeployContract` receipt sitting untouched in the global queue.
4. `d.near` is later re-created (by any party entitled to create that name) with entirely new keys/contract — a fresh, unrelated `Account`.
5. When chunk processing later drains the delayed queue and reaches the stale receipt, `validate_receipt` passes (it only checks structural validity), the account is fetched fresh (the *new* `d.near`), and `check_actor_permissions` passes because `actor_id` (`"d.near"`, from the old receipt) string-equals `account_id` (`"d.near"`, the new account). The stale `AddKey`/`DeployContract` action then executes with full self-privilege against the new owner's account, inserting an attacker-controlled key or replacing the new owner's contract.

None of the existing checks (signature/nonce validation happens only at the original transaction, not at delayed-receipt replay; `check_account_existence` only checks presence, not identity continuity; `check_actor_permissions` is a pure string comparison) catch this, because the protocol has no concept of "account generation" or epoch tied to `CreateAccount`/`DeleteAccount` cycles — `AccountId` is the sole identity anchor for self-authorization.

### Impact Explanation
This is an authorization escalation across accounts via the delayed/congestion receipt queue: an attacker who previously owned an account name can plant a privileged self-action that later executes with full "self" trust against a different, unrelated owner who re-creates the same account name. Concrete outcomes include arbitrary `FullAccess` key injection or malicious contract deployment onto the victim's re-created account, enabling theft of any funds subsequently deposited into that account — matching the "authorization escalation across accounts or promises" / "theft of user funds" bounty categories.

### Likelihood Explanation
Preconditions: the attacker must (a) control the account before deletion, (b) get a self-addressed privileged-action receipt delayed by congestion (requires exceeding the chunk's compute/proof-size limit before the receipt is processed — achievable by an ordinary funded account submitting enough gas-heavy work in the same or nearby chunks, a normal congestion-control scenario, not requiring any special privilege), (c) delete the account itself (self-authorized, cheap), and (d) have the account name re-created by a third party afterward. Step (d) is naming-scheme dependent (subaccount creation rules restrict who may create `X.Y`), which reduces but does not eliminate real-world reachability — an attacker could target account names it knows will be re-created (e.g., names it controls the parent of, or common squatting/re-registration patterns for implicit or registrar-issued names). The attack is repeatable and low-cost for the attacker (just gas + one round of congestion), but timing/synchronization with a victim's re-creation event is required, which is the main feasibility constraint.

### Recommendation
Tie self/actor authorization to account identity continuity rather than bare `AccountId` string equality. Options: (1) invalidate/purge any delayed or postponed receipts destined for `account_id` at `DeleteAccount` time (scanning the delayed queue is expensive, so alternatively) (2) introduce an account "generation"/nonce that increments on `CreateAccount` after a `DeleteAccount`, and require self-privileged actions to additionally match the account's generation captured at receipt-creation time. At minimum, `validate_receipt`/`process_delayed_receipts` should re-verify that the receiver account existed continuously (i.e., was not deleted and recreated) since the receipt was enqueued before granting self-actor privilege.

### Proof of Concept
Integration test plan (runtime/runtime/src or integration-tests using the runtime test-loop / congestion-control harness):
1. Set up a shard with a low `compute_limit`/gas limit to make congestion easy to induce.
2. Create account `d.near` with a known key K1.
3. Submit tx T1 from `d.near` to `d.near` containing `AddKey(K_attacker, FullAccess)` (or `DeployContract(malicious_wasm)`).
4. In the same/adjacent chunk, submit enough filler transactions/receipts to other accounts to exceed `compute_limit` before T1's receipt executes, forcing it into `DelayedReceiptIndices`/`DelayedReceipt{index}`. Assert via state inspection that the `TrieKey::DelayedReceipt` row exists and no `Account{d.near}` mutation from T1 occurred yet.
5. Submit tx T2 from `d.near` to `d.near`: `DeleteAccount { beneficiary_id: attacker }`. Apply the chunk; assert `Account{d.near}` is removed (`get_account` returns `None`), while the delayed receipt row from step 4 still exists (untouched by `remove_account`).
6. Submit tx T3 creating `d.near` afresh, controlled by key K_victim (simulate the naming-rule-eligible creator).
7. Continue chunk processing so the previously delayed receipt (T1's `AddKey`/`DeployContract`) is drained from `DelayedReceiptIndices` and processed.
8. Assert: `d.near`'s access keys now include `K_attacker` (or its contract hash equals the malicious wasm), proving the stale self-privileged receipt executed against the new account despite the intervening delete/recreate — i.e., `check_actor_permissions` passed based on stale `predecessor_id == receiver_id` string equality rather than any continuity of the real account owner.

### Citations

**File:** runtime/runtime/src/lib.rs (L853-855)
```rust
        let mut account = get_account(state_update, account_id)?;
        let account_did_not_exist = account.is_none();
        let mut actor_id = receipt.predecessor_id().clone();
```

**File:** runtime/runtime/src/lib.rs (L2522-2534)
```rust
            // Validating the delayed receipt. If it fails, it's likely the state is inconsistent.
            validate_receipt(
                &processing_state.apply_state.config.wasm_config.limit_config,
                &receipt,
                protocol_version,
                ValidateReceiptMode::ExistingReceipt,
            )
            .map_err(|e| {
                StorageError::StorageInconsistentState(format!(
                    "Delayed receipt {:?} in the state is invalid: {}",
                    receipt, e
                ))
            })?;
```

**File:** runtime/runtime/src/actions.rs (L750-776)
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

**File:** core/primitives/src/trie_key.rs (L220-227)
```rust
    /// Used to store indices of the delayed receipts queue (`node-runtime::DelayedReceiptIndices`).
    /// NOTE: It is a singleton per shard.
    DelayedReceiptIndices = col::DELAYED_RECEIPT_OR_INDICES,
    /// Used to store a delayed receipt `primitives::receipt::Receipt` for a given index `u64`
    /// in a delayed receipt queue. The queue is unique per shard.
    DelayedReceipt {
        index: u64,
    } = 8,
```

**File:** core/primitives/src/trie_key.rs (L600-601)
```rust
            TrieKey::DelayedReceiptIndices => None,
            TrieKey::DelayedReceipt { .. } => None,
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

**File:** runtime/runtime/src/verifier.rs (L526-571)
```rust
/// Validates a given receipt. Checks validity of the Action or Data receipt.
pub(crate) fn validate_receipt(
    limit_config: &LimitConfig,
    receipt: &Receipt,
    current_protocol_version: ProtocolVersion,
    mode: ValidateReceiptMode,
) -> Result<(), ReceiptValidationError> {
    if mode == ValidateReceiptMode::NewReceipt {
        let receipt_size: u64 =
            borsh::object_length(receipt).unwrap().try_into().expect("Can't convert usize to u64");
        if receipt_size > limit_config.max_receipt_size {
            return Err(ReceiptValidationError::ReceiptSizeExceeded {
                size: receipt_size,
                limit: limit_config.max_receipt_size,
            });
        }
    }

    // We retain these checks here as to maintain backwards compatibility
    // with AccountId validation since we illegally parse an AccountId
    // in near-vm-logic/logic.rs#fn(VMLogic::read_and_parse_account_id)
    AccountId::validate(receipt.predecessor_id().as_ref()).map_err(|_| {
        ReceiptValidationError::InvalidPredecessorId {
            account_id: receipt.predecessor_id().to_string(),
        }
    })?;
    AccountId::validate(receipt.receiver_id().as_ref()).map_err(|_| {
        ReceiptValidationError::InvalidReceiverId { account_id: receipt.receiver_id().to_string() }
    })?;

    match receipt.versioned_receipt() {
        VersionedReceiptEnum::Action(action_receipt)
        | VersionedReceiptEnum::PromiseYield(action_receipt) => validate_action_receipt(
            limit_config,
            action_receipt,
            receipt.receiver_id(),
            current_protocol_version,
            mode,
        ),
        VersionedReceiptEnum::Data(data_receipt)
        | VersionedReceiptEnum::PromiseResume(data_receipt) => {
            validate_data_receipt(limit_config, &data_receipt)
        }
        VersionedReceiptEnum::GlobalContractDistribution(_) => Ok(()), // Distribution receipt can't be issued without a valid contract
    }
}
```
