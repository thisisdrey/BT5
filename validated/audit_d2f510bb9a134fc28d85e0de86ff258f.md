## Finding: Confirmed — stale `PromiseYieldReceipt`/`PromiseYieldTimeout` survive `remove_account` and execute against a recreated account

I traced the full path and the code confirms the described vulnerability.

**Root cause:** `remove_account` (`core/store/src/utils/mod.rs:504-575`) removes `TrieKey::Account`, `TrieKey::ContractCode`, access keys/gas-key nonces, and `TrieKey::ContractData` — but it never touches `TrieKey::PromiseYieldReceipt { receiver_id, data_id }` entries, nor the `TrieKey::PromiseYieldTimeout { index }` queue, which is a per-shard singleton FIFO indexed independently of any account (`core/primitives/src/trie_key.rs:233-241`, `core/store/src/utils/mod.rs:181-198`). [1](#0-0) [2](#0-1) 

`resolve_promise_yield_timeouts` (`runtime/runtime/src/lib.rs:3009-3116`) only checks `state_update.contains_key(&promise_yield_key)` for `{receiver_id: queue_entry.account_id, data_id: queue_entry.data_id}` — with no check that the account is the same incarnation that created the yield — and on expiry synthesizes a `PromiseResume{predecessor_id: account_id, receiver_id: account_id, data: None}` receipt. [3](#0-2) 

That synthetic `PromiseResume` is then processed by `process_receipt` (`runtime/runtime/src/lib.rs:1500-1568`), which fetches the parked `yield_receipt` via `get_promise_yield_receipt` and runs it with `apply_action_receipt` — again with no account-incarnation check. [4](#0-3) 

The parked yield receipt's actions are exactly the `FunctionCall(method_name, arguments, gas, gas_weight)` chosen by the original caller at `promise_yield_create` time, with `deposit == Balance::ZERO` hardcoded, and `predecessor_id == receiver_id == account_id` (a self-call). [5](#0-4) [6](#0-5) 

### Title
Stale PromiseYield timeout survives account deletion and executes attacker-chosen self-call against a recreated account - (File: runtime/runtime/src/lib.rs, core/store/src/utils/mod.rs)

### Summary
`remove_account` deletes an account's `Account`, code, keys, and contract data, but leaves any pending `PromiseYieldReceipt` and the account-agnostic `PromiseYieldTimeout` queue entry untouched. If the account name is later reused by a different party, the protocol-driven timeout expiry synthesizes and executes a `PromiseResume` self-call carrying attacker-chosen `method_name`/`arguments` against the new account's contract, with `predecessor_id == receiver_id` — spoofing the "self-call" invariant that most contracts (`assert_self`/`#[private]`) rely on for callback authorization.

### Finding Description
An unprivileged attacker funds and controls account `A`, deploys a contract, and calls `promise_yield_create(method_name, arguments, gas, gas_weight)`, which enqueues a `PromiseYieldTimeout{account_id: A, data_id, expires_at}` entry (`runtime/runtime/src/function_call.rs:160-169`) and stores a `PromiseYieldReceipt` keyed by `(receiver_id=A, data_id)` (`core/store/src/utils/mod.rs:200-212`). The attacker then submits `DeleteAccount` on `A`. `action_delete_account` → `remove_account` clears the account, code, keys, and contract data (`runtime/runtime/src/actions.rs:371`, `core/store/src/utils/mod.rs:504-575`) but does **not** remove the `PromiseYieldReceipt` or the queued `PromiseYieldTimeout`, since neither is enumerated by the access-key/contract-data prefix iterators used there.

A third party (or the attacker themselves under a different identity) later executes `CreateAccount`+`AddKey`+`DeployContract` for the same account id `A`, deploying an unrelated contract. Once `apply_state.block_height` passes `expires_at`, `resolve_promise_yield_timeouts` finds the stale `PromiseYieldReceipt` still present via `contains_key` (`runtime/runtime/src/lib.rs:3047-3051`) and forwards a synthetic `PromiseResume{predecessor_id: A, receiver_id: A, data: None}` (`:3060-3068`). When delivered, `process_receipt`'s `PromiseResume` arm finds the yield receipt and unconditionally executes it via `apply_action_receipt` (`runtime/runtime/src/lib.rs:1511-1562`), running the original `FunctionCall(method_name, arguments)` against `A`'s new contract with `predecessor_id == receiver_id == A` — i.e., appearing as a legitimate self-call, exactly the pattern contracts use to gate privileged callbacks (e.g., NEP-141 `ft_resolve_transfer`, custom `#[private]` methods).

No existing check verifies that the account's "incarnation" (creation nonce/epoch) at resolution time matches the incarnation active when the yield was created; deletion does not invalidate parked yields.

### Impact Explanation
This is an authorization-escalation bug: a stale, protocol-synthesized receipt is delivered as a self-call to a semantically distinct (recreated) account, bypassing the `predecessor_id == current_account_id` authorization idiom most contracts use to protect internal callbacks. If the newly deployed contract on the reused account name happens to expose a self-call-gated method matching the attacker's pre-chosen `method_name`/`arguments` (e.g., common standard callback names), this can corrupt that contract's internal state or accounting without any new attacker transaction after the account is recreated — matching the "authorization escalation across accounts or promises" bounty category. Note `promise_yield_create` always attaches zero deposit, so no funds are moved directly by this receipt itself; impact is via state corruption/misuse of a spoofed self-call, contingent on the new contract's method surface.

### Likelihood Explanation
Preconditions are cheap and fully attacker-controlled: fund `A`, deploy any contract, call `promise_yield_create`, then `DeleteAccount`. The attacker chooses `method_name`/`arguments` in advance, but cannot control what contract, if any, will later reuse the account name, nor guarantee a matching self-call method exists — this significantly limits reliable exploitation against an arbitrary unaware victim, though it is deterministic if the attacker also controls (or colludes on) the recreation step, and repeatable across many account-name/timeout attempts at negligible cost (`yield_timeout_length_in_blocks` is a bounded, known config value).

### Recommendation
On `remove_account`, either (a) also remove any `PromiseYieldReceipt` entries for that `account_id` (would require an indexed lookup or an account-scoped index of pending data_ids), or (b) bind `PromiseYieldTimeout`/`PromiseYieldReceipt` resolution to an account "incarnation" identifier (e.g., account creation height/nonce) so `resolve_promise_yield_timeouts` and the `PromiseResume` handler refuse to execute a parked yield whose incarnation doesn't match the current account's.

### Proof of Concept
Runtime apply-path integration test:
1. Create account `A`, deploy a contract exposing a self-call-gated method `M` guarded by `assert_self()`.
2. Submit a receipt causing `A` to call `promise_yield_create("M", args, gas, weight)`; assert `PromiseYieldTimeout`/`PromiseYieldReceipt` are stored.
3. Submit `DeleteAccountAction` for `A`; assert `Account`/keys/code removed but (via direct trie inspection) the `PromiseYieldReceipt` key still exists.
4. Submit `CreateAccount`+`AddKey`+`DeployContract` for `A` with a different, unrelated contract that also happens to expose a method named `M` performing a sensitive state change gated by `assert_self()`.
5. Advance `apply_state.block_height` past `expires_at`; run `apply` through the point where `resolve_promise_yield_timeouts` fires and the resulting `PromiseResume` is delivered.
6. Assert method `M` executed on the new `A` (state change observed) even though the new owner never called it — demonstrating the spoofed self-call landed on the recreated account.

### Citations

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

**File:** core/primitives/src/trie_key.rs (L238-247)
```rust
    /// The queue is unique per shard.
    PromiseYieldTimeout {
        index: u64,
    } = col::PROMISE_YIELD_TIMEOUT,
    /// Used to store the postponed promise yield receipt `primitives::receipt::Receipt`
    /// for a given receiver's `AccountId` and a given `data_id`.
    PromiseYieldReceipt {
        receiver_id: AccountId,
        data_id: CryptoHash,
    } = col::PROMISE_YIELD_RECEIPT,
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

**File:** runtime/runtime/src/lib.rs (L3042-3068)
```rust
        if queue_entry.expires_at > apply_state.block_height {
            break;
        }

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
```

**File:** runtime/near-vm-runner/src/wasmtime_runner/logic.rs (L4025-4032)
```rust
    ctx.ext.append_action_function_call_weight(
        new_receipt_idx,
        method_name,
        arguments,
        Balance::ZERO,
        Gas::from_gas(gas),
        GasWeight(gas_weight),
    )?;
```

**File:** runtime/runtime/src/function_call.rs (L160-169)
```rust
                // If the newly created receipt is a PromiseYield, enqueue a timeout for it
                if receipt.is_promise_yield {
                    enqueue_promise_yield_timeout(
                        state_update,
                        &mut promise_yield_indices,
                        account_id.clone(),
                        receipt.input_data_ids[0],
                        apply_state.block_height
                            + config.wasm_config.limit_config.yield_timeout_length_in_blocks,
                    );
```
