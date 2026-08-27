## Title
Wallet-Contract `has_in_flight_tx` lock can get permanently stuck if a callback panics after clearing it, freezing the eth-implicit wallet's funds forever - ([File: runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs])

### Summary
This maps to the reported bug class: an external, uncontrollable failure of an asynchronous "settlement" step (in the Solidity report, an ERC20 blacklist causing an outbound transfer to revert) permanently DOSes a shared piece of state that governs whether new operations for that position/account can proceed. The nearcore analog is the `WalletContract::has_in_flight_tx` reentrancy-style lock used by the eth-implicit `near-wallet-contract` global contract: this lock is cleared at the *start* of each async callback, but if the callback subsequently panics (e.g. insufficient statically-allocated callback gas, or any other host-level trap), NEAR's atomic receipt semantics discard *all* state changes from that receipt — including the clearing of the lock — leaving it permanently `true` and the wallet permanently unusable.

### Finding Description
`WalletContract::rlp_execute` is the sole public, unprivileged entry point of the eth-implicit wallet contract. It refuses to start a new transaction while `has_in_flight_tx` is `true`: [1](#0-0) 

Each asynchronous callback in the promise chain (`address_check_callback`, `nep_141_storage_balance_callback`, `rlp_execute_callback`) resets the lock as its **very first statement**, before doing any further work that itself creates new cross-contract promises / performs additional host calls: [2](#0-1) [3](#0-2) [4](#0-3) 

Each of these callbacks is invoked with a fixed, small statically-allocated gas budget computed once when the promise chain is built (e.g. `RLP_EXECUTE_CALLBACK_GAS`, `ADDRESS_CHECK_CALLBACK_GAS`, `NEP_141_STORAGE_BALANCE_CALLBACK_GAS`): [5](#0-4) 

If a callback's actual execution (building `action_to_promise`, chaining another `.then()`, issuing `promise_batch_create`/`promise_batch_action_transfer`, or any other host call) exceeds that fixed static-gas allocation, or otherwise panics, the callback receipt fails with a hard host error (e.g. "Exceeded the prepaid gas."). Per nearcore's runtime execution model, a failed action receipt causes the entire receipt's state mutations to be rolled back atomically: [6](#0-5) 

Because `self.has_in_flight_tx = false;` was the first (and now rolled-back) state write in that same receipt, the flag reverts to its pre-receipt value of `true` (it was set to `true` by the parent call that spawned this callback). The contract itself already demonstrates that gas-exceeded panics are a real, documented failure mode of this contract: [7](#0-6) 

but that existing test only exercises the failure in the initial `rlp_execute` call (before the lock is ever set), so the contract "is still usable afterwards" — the very outcome that is broken once the same class of failure hits a callback instead, since the lock has already been latched to `true` for that receipt tree.

### Impact Explanation
Once `has_in_flight_tx` is stuck `true`, `rlp_execute` unconditionally short-circuits with `"transaction already in progress, please try again later"` for every future call, forever: [8](#0-7) 

There is no method to reset `has_in_flight_tx` from outside a successful callback resolution. This permanently freezes the eth-implicit account's ability to move any of its NEAR balance or NEP-141/ERC-20 balances it holds via the wallet contract — a permanent freezing-of-funds outcome, directly analogous to the reported BendDAO issue where a stuck transfer state made the corresponding position permanently un-actionable.

### Likelihood Explanation
This does not require a malicious actor with special privileges: it can be triggered by an ordinary owner/relayer of the eth-implicit wallet submitting a normal ERC-20/NEP-141 transfer (routed through `nep_141_storage_balance_callback`) or a base-token transfer requiring address-registrar lookup (routed through `address_check_callback`), where subsequent nested cross-contract call setup work in the callback body exceeds the small, hardcoded static-gas budgets. Since these budgets are fixed constants independent of the complexity of the nested promise the callback must build (chained `storage_deposit` + transfer function calls, or ban-relayer promise creation), it is a purely mechanical/gas-accounting risk reachable by an unprivileged signer's normal use of the contract, not by an external malicious dependency.

### Recommendation
Do not clear `has_in_flight_tx` (or perform any other critical state write) as the first statement of a callback that subsequently performs further gas-consuming host calls; instead clear the lock only immediately before returning `PromiseOrValue::Value(...)` on every exit path, or set it unconditionally in a way that is not itself vulnerable to being undone by a later panic in the same receipt (e.g., split the "clear-and-return" and "clear-and-continue" logic so that any panic-prone continuation happens only after the flag has already been durably committed, or provision generous/parametrized static gas for callback continuations with a hard fallback path that guarantees the lock is released even under gas pressure).

### Proof of Concept
1. Deploy the wallet contract for an eth-implicit account and fund it.
2. Register a NEP-141 token and have the wallet hold a balance.
3. Submit an RLP-encoded ERC-20 transfer whose `receiver_id` requires the `storage_deposit` + `ft_transfer` chained-promise path in `nep_141_storage_balance_callback`, attaching only just enough gas that the outer `rlp_execute` call succeeds in spawning the `storage_balance_of` promise but leaves `nep_141_storage_balance_callback` with gas at/below `NEP_141_STORAGE_BALANCE_CALLBACK_GAS` needed to build+chain the two nested function-calls and the trailing `.then()`.
4. Observe the callback receipt fails with `Exceeded the prepaid gas`, and because `self.has_in_flight_tx = false;` was rolled back with the rest of the receipt, `has_in_flight_tx` remains `true`.
5. Any subsequent call to `rlp_execute` on this wallet account now unconditionally returns `"transaction already in progress, please try again later."`, permanently, freezing all funds held by that eth-implicit wallet.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L33-41)
```rust
const NEP_141_STORAGE_DEPOSIT_AMOUNT: NearToken = NearToken::from_yoctonear(1_250 * MICRO_NEAR);
const NEP_141_STORAGE_DEPOSIT_GAS: Gas = Gas::from_tgas(5);
const NEP_141_STORAGE_BALANCE_OF_GAS: Gas = Gas::from_tgas(5);
const REGISTRAR_LOOKUP_GAS: Gas = Gas::from_tgas(5);
const RLP_EXECUTE_CALLBACK_GAS: Gas = Gas::from_tgas(5);
const ADDRESS_CHECK_CALLBACK_GAS: Gas = Gas::from_tgas(5).saturating_add(RLP_EXECUTE_CALLBACK_GAS);
const NEP_141_STORAGE_BALANCE_CALLBACK_GAS: Gas = Gas::from_tgas(5)
    .saturating_add(NEP_141_STORAGE_DEPOSIT_GAS)
    .saturating_add(RLP_EXECUTE_CALLBACK_GAS);
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L88-128)
```rust
    #[payable]
    pub fn rlp_execute(
        &mut self,
        target: AccountId,
        tx_bytes_b64: String,
    ) -> PromiseOrValue<ExecuteResponse> {
        // To ensure user actions are executed in the desired order,
        // having multiple transactions in flight at the same time is
        // not allowed.
        if self.has_in_flight_tx {
            return PromiseOrValue::Value(ExecuteResponse {
                success: false,
                success_value: None,
                error: Some(
                    "Error: transaction already in progress, please try again later.".into(),
                ),
            });
        }
        let current_account_id = env::current_account_id();
        let predecessor_account_id = env::predecessor_account_id();
        let result = inner_rlp_execute(
            current_account_id.clone(),
            predecessor_account_id,
            target,
            tx_bytes_b64,
            &mut self.nonce,
        );

        match result {
            Ok(promise) => {
                self.has_in_flight_tx = true;
                PromiseOrValue::Promise(promise)
            }
            Err(Error::Relayer(_)) if env::signer_account_id() == current_account_id => {
                let promise = create_ban_relayer_promise(current_account_id);
                self.has_in_flight_tx = true;
                PromiseOrValue::Promise(promise)
            }
            Err(e) => PromiseOrValue::Value(e.into()),
        }
    }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L133-192)
```rust
    #[private]
    pub fn address_check_callback(
        &mut self,
        target: AccountId,
        action: near_action::Action,
        caller_deposit: Option<CallerDeposit>,
    ) -> PromiseOrValue<ExecuteResponse> {
        self.has_in_flight_tx = false;
        let maybe_account_id: Option<AccountId> = match env::promise_result(0) {
            PromiseResult::Failed => {
                return PromiseOrValue::Value(ExecuteResponse {
                    success: false,
                    success_value: None,
                    error: Some("Call to Address Registrar contract failed".into()),
                });
            }
            PromiseResult::Successful(value) => match serde_json::from_slice(&value) {
                Ok(x) => x,
                Err(_) => {
                    return PromiseOrValue::Value(ExecuteResponse {
                        success: false,
                        success_value: None,
                        error: Some("Unexpected response from account registrar".into()),
                    });
                }
            },
        };
        let current_account_id = env::current_account_id();
        let promise = if maybe_account_id.is_some() {
            // We intentionally do not increment the nonce in this case because the
            // error is caused by a faulty relayer, not the user. An honest relayer
            // may still be able to successfully send the user's intended transaction.
            if env::signer_account_id() == current_account_id {
                create_ban_relayer_promise(current_account_id)
            } else {
                return PromiseOrValue::Value(ExecuteResponse {
                    success: false,
                    success_value: None,
                    error: Some("Invalid target: target is address corresponding to existing named account_id".into()),
                });
            }
        } else {
            // We must increment the nonce at this point to prevent replay of the transaction.
            // Recall that the nonce was not incremented in `inner_rlp_execute` in the case that
            // the registrar contract was called (i.e. in the case we end up inside this callback).
            self.nonce = self.nonce.saturating_add(1);
            let ext =
                WalletContract::ext(current_account_id).with_static_gas(RLP_EXECUTE_CALLBACK_GAS);
            match action_to_promise(target, action)
                .map(|p| p.then(ext.rlp_execute_callback(caller_deposit)))
            {
                Ok(p) => p,
                Err(e) => {
                    return PromiseOrValue::Value(e.into());
                }
            }
        };
        self.has_in_flight_tx = true;
        PromiseOrValue::Promise(promise)
    }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L194-273)
```rust
    #[private]
    pub fn nep_141_storage_balance_callback(
        &mut self,
        token_id: AccountId,
        receiver_id: AccountId,
        action: near_action::Action,
        caller_deposit: Option<CallerDeposit>,
    ) -> PromiseOrValue<ExecuteResponse> {
        self.has_in_flight_tx = false;
        let maybe_storage_balance: Option<StorageBalance> = match env::promise_result(0) {
            PromiseResult::Failed => {
                return PromiseOrValue::Value(ExecuteResponse {
                    success: false,
                    success_value: None,
                    error: Some(format!("Call to NEP-141 {token_id}::storage_balance_of failed")),
                });
            }
            PromiseResult::Successful(value) => match serde_json::from_slice(&value) {
                Ok(x) => x,
                Err(_) => {
                    return PromiseOrValue::Value(ExecuteResponse {
                        success: false,
                        success_value: None,
                        error: Some("Unexpected response from NEP-141 storage_balance_of".into()),
                    });
                }
            },
        };
        let current_account_id = env::current_account_id();
        let ext = WalletContract::ext(current_account_id).with_static_gas(RLP_EXECUTE_CALLBACK_GAS);
        let promise = match maybe_storage_balance {
            Some(_) => {
                // receiver_id is registered so we can send the transfer
                // without additional actions. Note: in the standard NEP-141
                // implementation it is impossible to have `Some` storage balance,
                // but have it be insufficient to transact.
                match action_to_promise(token_id, action)
                    .map(|p| p.then(ext.rlp_execute_callback(caller_deposit)))
                {
                    Ok(p) => p,
                    Err(e) => {
                        return PromiseOrValue::Value(e.into());
                    }
                }
            }
            None => {
                // receiver_id is not registered so we must call `storage_deposit` first.
                let storage_deposit_args =
                    format!(r#"{{"account_id": "{receiver_id}"}}"#).into_bytes();
                let transfer_function_call = match action {
                    near_action::Action::FunctionCall(x) => x,
                    _ => {
                        return PromiseOrValue::Value(ExecuteResponse {
                            success: false,
                            success_value: None,
                            error: Some(
                                "Expected function call action to perform NEP-141 transfer".into(),
                            ),
                        });
                    }
                };
                Promise::new(token_id)
                    .function_call(
                        "storage_deposit".into(),
                        storage_deposit_args,
                        NEP_141_STORAGE_DEPOSIT_AMOUNT,
                        NEP_141_STORAGE_DEPOSIT_GAS,
                    )
                    .function_call(
                        transfer_function_call.method_name,
                        transfer_function_call.args,
                        transfer_function_call.deposit,
                        transfer_function_call.gas,
                    )
                    .then(ext.rlp_execute_callback(caller_deposit))
            }
        };
        self.has_in_flight_tx = true;
        PromiseOrValue::Promise(promise)
    }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L275-317)
```rust
    #[private]
    pub fn rlp_execute_callback(
        &mut self,
        caller_deposit: Option<CallerDeposit>,
    ) -> ExecuteResponse {
        self.has_in_flight_tx = false;
        let n = env::promise_results_count();

        if n == 0 {
            // `rlp_execute_callback` is called directly in the case of an emulated self-transfer.
            return ExecuteResponse { success: true, success_value: None, error: None };
        } else if n > 1 {
            return ExecuteResponse {
                success: false,
                success_value: None,
                error: Some(format!(
                    "Invariant violation: this callback comes after a single promise. n={n}"
                )),
            };
        }

        match env::promise_result(0) {
            PromiseResult::Failed => {
                // The cross-contract call failed, refund the caller if needed
                if let Some(CallerDeposit { account_id, yocto_near }) = caller_deposit {
                    let refund_promise = env::promise_batch_create(&account_id);
                    env::promise_batch_action_transfer(
                        refund_promise,
                        NearToken::from_yoctonear(yocto_near.into()),
                    );
                }

                ExecuteResponse {
                    success: false,
                    success_value: None,
                    error: Some("Failed Near promise".into()),
                }
            }
            PromiseResult::Successful(value) => {
                ExecuteResponse { success: true, success_value: Some(value), error: None }
            }
        }
    }
```

**File:** protocol-model/spec/runtime-execution.md (L69-70)
```markdown
6. **Refunds** (see below): system-predecessor receipts (refund receipts) are free — no refund generated, and a failed refund burns its deposit into `other_burnt_amount` (`runtime/runtime/src/lib.rs:929`). Otherwise `refund_unspent_gas_and_deposits` runs (`:943`).
7. **Commit or rollback**: success commits with `ReceiptProcessing`; failure calls `state_update.rollback()`, discarding all state changes from the receipt (`runtime/runtime/src/lib.rs:961`-`970`).
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/tests/sanity.rs (L34-78)
```rust
#[tokio::test]
async fn test_insufficient_gas() -> anyhow::Result<()> {
    let TestContext { worker, wallet_contract, wallet_sk, .. } = TestContext::new().await?;

    // If not enough gas is attached to the `rlp_execute` call then the action fails.
    let target = "some.account.near".to_string();
    let action = Action::FunctionCall {
        receiver_id: target.clone(),
        method_name: "greet".into(),
        args: br#"{"name": "Aurora"}"#.to_vec(),
        gas: 5_000_000_000_000,
        yocto_near: 0,
    };
    let signed_transaction = utils::create_signed_transaction(
        0,
        &target.parse().unwrap(),
        Wei::zero(),
        action,
        &wallet_sk,
    );

    let error = wallet_contract
        .inner
        .call(crate::tests::RLP_EXECUTE)
        .args_json(serde_json::json!({
            "target": target,
            "tx_bytes_b64": codec::encode_b64(&codec::rlp_encode(&signed_transaction))
        }))
        .gas(near_gas::NearGas::from_tgas(7))
        .transact()
        .await
        .unwrap()
        .raw_bytes()
        .unwrap_err();

    assert!(
        error.to_string().contains("Exceeded the prepaid gas."),
        "Error should be that there was not enough gas"
    );

    // But the contract is still usable afterwards.
    utils::deploy_and_call_hello(&worker, &wallet_contract, &wallet_sk, 0).await?;

    Ok(())
}
```
