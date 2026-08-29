### Title
Stale PromiseYieldReceipt/PromiseYieldTimeout entries survive account deletion, letting a self-privileged callback fire against a differently-owned account that later reclaims the same name - ([File: runtime/runtime/src/lib.rs], [File: core/store/src/utils/mod.rs])

### Summary
`remove_account` (called from `action_delete_account`) never removes `TrieKey::PromiseYieldReceipt`, `TrieKey::PromiseYieldTimeout`, `TrieKey::PromiseYieldStatus`, or `YieldIdToDataId`/`DataIdToYieldId` entries belonging to the deleted account. Because a `PromiseYield` receipt is always self-targeted (receiver_id = predecessor_id = the creating account), its parked callback keeps `actor_id == account_id`, which satisfies `check_actor_permissions`'s "self" check for privileged actions (AddKey, DeleteKey, Stake, DeleteAccount, etc.) for whatever account later occupies that same account name.

### Finding Description
- `promise_yield_create` always sets the yield receipt's `receiver_id` to `self.context.current_account_id.clone()` [1](#0-0) , so the parked `PromiseYield` receipt's `predecessor_id` and `receiver_id` are always the same account ("victim.near").
- On `process_receipt`, a `PromiseYield` receipt is simply stored under `TrieKey::PromiseYieldReceipt { receiver_id, data_id }` and a `TrieKey::PromiseYieldTimeout` entry is queued; nothing else references this data other than the receiver's account id and the `data_id` [2](#0-1) .
- `action_delete_account` burns the account balance and calls `remove_account`, then sets `actor_id = receipt.predecessor_id()` and clears the `Account` [3](#0-2) .
- `remove_account` removes only `TrieKey::Account`, `TrieKey::ContractCode`, all `AccessKey`/gas-key entries, and `ContractData` [4](#0-3) . It does **not** touch `col::PROMISE_YIELD_RECEIPT`, `col::PROMISE_YIELD_TIMEOUT`, or `col::PROMISE_YIELD_STATUS`.
- After the account is deleted and later recreated under the same name (e.g. a top-level `.near` account re-registered by the registrar for a new, unrelated owner, or a subaccount recreated by the same parent), the leftover `PromiseYieldTimeout` queue entry is still processed by `resolve_promise_yield_timeouts`, which checks `state_update.contains_key(&promise_yield_key)` — true because the receipt was never removed — and synthesizes an internal `PromiseResume` receipt destined to the same account, with no external caller required [5](#0-4) .
- When that `PromiseResume` receipt is processed, `process_receipt`'s `PromiseResume` branch fetches the stale `yield_receipt` via `get_promise_yield_receipt`, removes it/its status, and immediately executes it via `apply_action_receipt` against whatever `Account` now exists at that name [6](#0-5) .
- Because the stale receipt's `predecessor_id == receiver_id == "victim.near"` (fixed at creation time), `check_actor_permissions` sees `actor_id == account_id` and allows privileged actions such as `AddKey`/`DeleteKey`/`Stake`/`DeleteAccount` to be issued from a FunctionCall action inside that callback, without any access-key check [7](#0-6) .
- The attacker fully controls the callback's `method_name` and `arguments` at yield-creation time (`promise_yield_create`'s `method_name`/`arguments` parameters) [8](#0-7) ; the actual code executed, however, is whatever contract is deployed on the account at execution time, since `FunctionCall` dispatches by method name against the currently-installed WASM. Full self-privileged escalation (e.g. `promise_batch_action_add_key` succeeding) therefore additionally requires the new account's deployed contract to expose a matching, exploitable method name — true for accounts that reuse a standardized/attacker-controlled contract (e.g. lockup contracts, or an attacker recreating their own subaccount) but not guaranteed for an arbitrary unrelated third party's contract. Independent of that caveat, the underlying invariant violation — privileged, self-scoped protocol state outliving `DeleteAccountAction` and being dispatched against a differently-owned account occupying the same name — is a real defect regardless of whether a given exploitation attempt succeeds in installing a key.
- No existing check (signature, nonce, access-key permission, storage-staking, or size limit) intercepts this: the resume/timeout path is purely internal receipt processing, triggered automatically by block height, and never re-validates that the account executing the callback is "the same" logical account that created the yield.

### Impact Explanation
This is an authorization-exactness violation: state that is supposed to be scoped strictly to one account's lifetime (a parked, self-privileged callback) survives `DeleteAccountAction` and is later dispatched against an unrelated account that merely happens to reuse the same account id. Where exploitable (i.e., where the new occupant's contract exposes a matching method, e.g., a standardized template contract, or where the attacker controls both the deletion and the recreation such as within their own subaccount tree), this allows adding a full-access key or other owner-privileged action on an account the attacker does not otherwise control — this falls under "authorization escalation across accounts or promises."

### Likelihood Explanation
Preconditions: attacker must (1) deploy a contract exposing a method that calls `promise_yield_create` with attacker-chosen callback method/arguments, (2) invoke it to park a yield on their own account, (3) submit a `DeleteAccountAction`, and (4) have the exact same account name recreated later (either by an unrelated party choosing the same freed top-level name, or, more reliably, by the attacker themselves via their own parent-account-controlled subaccount, or by relying on a standardized contract being redeployed to the reused name) while the `PromiseYieldTimeout` has not yet fired. All steps use only unprivileged, ordinary transactions (contract deploy, function call, delete account) — no validator/node access required. The cross-owner variant (arbitrary third party recreating the name) is opportunistic and depends on account-name reuse and matching contract deployment, lowering but not eliminating real-world likelihood; the same-owner (subaccount re-creation) variant is fully attacker-controlled and reliably repeatable.

### Recommendation
Have `remove_account` (or `action_delete_account`) also enumerate and remove all `TrieKey::PromiseYieldReceipt`, `TrieKey::PromiseYieldStatus`, `TrieKey::YieldIdToDataId`, and `TrieKey::DataIdToYieldId` entries for the account being deleted (and, ideally, invalidate/skip the corresponding `PromiseYieldTimeout` queue entries, e.g., by checking account existence/creation-height in `resolve_promise_yield_timeouts` before dispatching, or by tagging yield receipts with the account's incarnation/creation nonce and verifying it on resume).

### Proof of Concept
Test-loop integration test plan:
1. Deploy a test contract to account `A` (e.g. `victim.near`) exposing a method `call_yield_create_and_add_key` that calls `promise_yield_create` with callback method name matching a method also present in the contract to be redeployed later (or simply reuse the same test contract to make the PoC deterministic), where the callback calls `promise_batch_action_add_key` targeting `env::current_account_id()` with an attacker-supplied public key.
2. Submit that function call transaction; confirm via `get_yield_data_ids_in_state`/`TrieKey::PromiseYieldReceipt` iteration that the yield receipt and timeout entry are persisted under account `A`.
3. Submit a `DeleteAccountAction` for `A` (beneficiary = some other account) in the next block; confirm `view_account(A)` returns `UnknownAccount`, but directly inspect trie state to confirm `TrieKey::PromiseYieldReceipt`/`TrieKey::PromiseYieldTimeout` for `A` are still present (not removed).
4. Recreate account `A` (e.g. via `CreateAccount` + `AddKey` + `DeployContract` from the appropriate parent/registrar) with a fresh access key and the same (or a compatible) contract, before the yield timeout height is reached.
5. Advance blocks to `yield_timeout_height` so `resolve_promise_yield_timeouts` automatically synthesizes and delivers a `PromiseResume` to `A`.
6. Assert that the callback executes against the new incarnation of `A` and that `promise_batch_action_add_key` succeeds — i.e., `view_access_key(A, attacker_public_key)` now returns `FullAccess`, despite the new key holder of `A` never having authorized this, proving cross-incarnation privilege leakage.

### Citations

**File:** runtime/near-vm-runner/src/logic/logic.rs (L3660-3709)
```rust
    pub fn promise_yield_create(
        &mut self,
        method_name_len: u64,
        method_name_ptr: u64,
        arguments_len: u64,
        arguments_ptr: u64,
        gas: u64,
        gas_weight: u64,
        register_id: u64,
    ) -> Result<u64> {
        self.result_state.gas_counter.pay_base(base)?;
        if self.context.is_view() {
            return Err(HostError::ProhibitedInView {
                method_name: "promise_yield_create".to_string(),
            }
            .into());
        }
        self.result_state.gas_counter.pay_base(yield_create_base)?;

        let method_name = get_memory_or_register!(self, method_name_ptr, method_name_len)?;
        if method_name.is_empty() {
            return Err(HostError::EmptyMethodName.into());
        }
        let arguments = get_memory_or_register!(self, arguments_ptr, arguments_len)?;
        let method_name = method_name.into_owned();
        let arguments = arguments.into_owned();

        // Input can't be large enough to overflow, WebAssembly address space is 32-bits.
        let num_bytes = method_name.len() as u64 + arguments.len() as u64;
        self.result_state.gas_counter.pay_per(yield_create_byte, num_bytes)?;
        // Prepay gas for the callback so that it cannot be used for this execution any longer.
        self.result_state.gas_counter.prepay_gas(Gas::from_gas(gas))?;

        // Here we are creating a receipt with a single data dependency which will then be
        // resolved by the resume call.
        self.pay_gas_for_new_receipt(true, &[true])?;
        let (new_receipt_idx, data_id) =
            self.ext.create_promise_yield_receipt(self.context.current_account_id.clone())?;

        let new_promise_idx = self.checked_push_promise(Promise::Receipt(new_receipt_idx))?;
        self.pay_action_base(ActionCosts::function_call_base, true)?;
        self.pay_action_per_byte(ActionCosts::function_call_byte, num_bytes, true)?;
        self.ext.append_action_function_call_weight(
            new_receipt_idx,
            method_name,
            arguments,
            Balance::ZERO,
            Gas::from_gas(gas),
            GasWeight(gas_weight),
        )?;
```

**File:** runtime/runtime/src/lib.rs (L1495-1499)
```rust
            VersionedReceiptEnum::PromiseYield(_) => {
                // Received a new PromiseYield receipt. We simply store it and await
                // the corresponding PromiseResume receipt.
                set_promise_yield_receipt(state_update, receipt);
            }
```

**File:** runtime/runtime/src/lib.rs (L1511-1562)
```rust
                // Received a new PromiseResume receipt delivering input data for a PromiseYield.
                // It is guaranteed that the PromiseYield has exactly one input data dependency
                // and that it arrives first, so we can simply find and execute it.
                if let Some(yield_receipt) =
                    get_promise_yield_receipt(state_update, account_id, data_receipt.data_id)?
                {
                    // Remove the receipt from the state
                    remove_promise_yield_receipt(state_update, account_id, data_receipt.data_id);

                    // Clear the PromiseYield status
                    remove_promise_yield_status(state_update, account_id, data_receipt.data_id);

                    // Clean up yield_id <-> data_id mappings if this was created by yield_create_with_id
                    if ProtocolFeature::YieldWithId.enabled(apply_state.current_protocol_version) {
                        if let Some(yield_id) = get_yield_id_for_data_id(
                            state_update,
                            account_id,
                            data_receipt.data_id,
                        )? {
                            remove_yield_id_mappings(
                                state_update,
                                account_id,
                                yield_id,
                                data_receipt.data_id,
                            );
                        }
                    }

                    // Save the data into the state keyed by the data_id
                    set_received_data(
                        state_update,
                        account_id.clone(),
                        data_receipt.data_id,
                        &ReceivedData { data: data_receipt.data.clone() },
                    );

                    // Execute the PromiseYield receipt. It will read the input data and clean it
                    // up from the state.
                    return self
                        .apply_action_receipt(
                            state_update,
                            apply_state,
                            pipeline_manager,
                            &yield_receipt,
                            receipt_sink,
                            instant_receipts,
                            validator_proposals,
                            stats,
                            epoch_info_provider,
                            receipt_to_tx,
                        )
                        .map(Some);
```

**File:** runtime/runtime/src/lib.rs (L3046-3097)
```rust
        // Check if the yielded promise still needs to be resolved
        let promise_yield_key = TrieKey::PromiseYieldReceipt {
            receiver_id: queue_entry.account_id.clone(),
            data_id: queue_entry.data_id,
        };
        if state_update.contains_key(&promise_yield_key, AccessOptions::DEFAULT)? {
            let new_receipt_id = create_receipt_id_from_receipt_id(
                &queue_entry.data_id,
                apply_state.block_height,
                new_receipt_index,
            );
            new_receipt_index += 1;

            // Create a PromiseResume receipt to resolve the timed-out yield.
            let resume_receipt = Receipt::V0(ReceiptV0 {
                predecessor_id: queue_entry.account_id.clone(),
                receiver_id: queue_entry.account_id.clone(),
                receipt_id: new_receipt_id,
                receipt: ReceiptEnum::PromiseResume(DataReceipt {
                    data_id: queue_entry.data_id,
                    data: None,
                }),
            });

            // Record a ReceiptToTx entry for the new resume receipt. The parent is the
            // yield receipt that is being timed out.
            if processing_state.apply_state.save_receipt_to_tx {
                let yield_receipt: Receipt = get_pure(state_update, &promise_yield_key)?
                    .expect("promise yield receipt should exist since contains_key was true");
                processing_state.receipt_to_tx.push((
                    new_receipt_id,
                    ReceiptToTxInfo::V1(ReceiptToTxInfoV1 {
                        origin: ReceiptOrigin::FromReceipt(ReceiptOriginReceipt {
                            parent_receipt_id: *yield_receipt.receipt_id(),
                            parent_predecessor_id: yield_receipt.predecessor_id().clone(),
                        }),
                        receiver_account_id: queue_entry.account_id.clone(),
                        shard_id: processing_state.apply_state.shard_id,
                    }),
                ));
            }

            // The receipt is destined for the local shard and will be placed in the outgoing
            // receipts buffer. It is possible that there is already an outgoing receipt resolving
            // this yield if `yield_resume` was invoked by some receipt which was processed in
            // the current chunk. The ordering will be maintained because the receipts are
            // destined for the same shard; the timeout will be processed second and discarded.
            receipt_sink.forward_or_buffer_receipt(
                resume_receipt,
                apply_state,
                &mut state_update,
            )?;
```

**File:** runtime/runtime/src/actions.rs (L364-388)
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
```

**File:** runtime/runtime/src/actions.rs (L739-768)
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
        Action::DeleteAccount(_) => {
            if actor_id != account_id {
                return Err(ActionErrorKind::ActorNoPermission {
                    account_id: account_id.clone(),
                    actor_id: actor_id.clone(),
                }
                .into());
            }
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
