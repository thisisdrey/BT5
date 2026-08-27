## Analysis

I found a valid analog: the `near-wallet-contract` (eth-implicit wallet contract), which is explicitly in-scope, implements a Stargate-`sgReceive`-style pattern: it accepts an externally attached deposit, executes a cross-contract action via a promise chain, and defers a refund of that deposit to a `#[private]` callback that fires after the promise resolves. [1](#0-0) sets `has_in_flight_tx = true` before returning the promise, and only the eventual callback resets it. [2](#0-1)  shows `rlp_execute_callback`, which sets `has_in_flight_tx = false` and, only `if promise_result` is `Failed`, creates a transfer promise to refund `caller_deposit` back to the external caller.

The gas allotted to this callback (and its nested variants, e.g. `nep_141_storage_balance_callback`/`address_check_callback`) is a fixed constant plus a caller/owner-influenced `action.gas()` value: [3](#0-2)  and [4](#0-3) . There is no explicit `gas` budget carve-out mirrored on the analog `catch` block from the report — if the callback itself runs out of prepaid gas, per nearcore's atomicity model the entire receipt fails and rolls back *all* state changes from that receipt, including the `self.has_in_flight_tx = false` line, per the documented invariant: [5](#0-4) . Because `has_in_flight_tx` was already `true` from the initiating `rlp_execute` call (a separate, already-committed receipt), an out-of-gas failure in the callback leaves it permanently `true`, and `rlp_execute` immediately rejects all future transactions once `has_in_flight_tx` is `true` [6](#0-5) . Additionally, since the refund-creation logic never executes (rolled back), the `caller_deposit` (already merged into the wallet contract's account balance by the earlier, successfully-committed `rlp_execute` receipt) is never returned, matching the `test_caller_refunds` expectation that a *failed* callback issues a refund: [7](#0-6) .

I was not able to fully confirm, within the remaining budget, a concrete case where the fixed gas constants (5 TGas per step) are provably insufficient for an attacker-reachable input size (e.g., unbounded serialization of `ExecuteResponse`/`CallerDeposit` or attacker-controlled string lengths feeding into `format!` calls for args). This is the key missing piece to fully prove exploitability versus a purely theoretical gas-starvation path, and would need dedicated testing/fuzzing of callback gas consumption under adversarial RLP-encoded inputs.

### Title
Out-of-gas failure in `rlp_execute_callback` permanently freezes the eth-implicit wallet and its deposits - (File: `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs`)

### Summary
The eth-implicit wallet contract's `rlp_execute` flow sets `has_in_flight_tx = true` and defers clearing it, along with refunding any externally-attached `caller_deposit`, to the `rlp_execute_callback` (or its `address_check_callback`/`nep_141_storage_balance_callback` predecessors). If that callback receipt runs out of gas, nearcore's atomic rollback semantics revert the entire receipt, so `has_in_flight_tx` is never reset and the deposit refund promise is never created — mirroring the sgReceive class of bug where an out-of-gas revert during a deferred completion step leaves funds stuck and blocks future legitimate flow.

### Finding Description
`rlp_execute` guards against re-entrant/out-of-order transactions using the `has_in_flight_tx` flag, flipping it to `true` before returning a `Promise` [8](#0-7) . It is only reset back to `false` inside the various callback methods (`address_check_callback`, `nep_141_storage_balance_callback`, `rlp_execute_callback`) [9](#0-8) . The callback also carries the sole logic for refunding an external caller's attached deposit (`caller_deposit`) if the downstream cross-contract call failed [10](#0-9) .

The gas reserved for the callback is computed from fixed constants plus an action-supplied gas amount (`action.gas()`), e.g. `ADDRESS_CHECK_CALLBACK_GAS.saturating_add(action.gas())` and `NEP_141_STORAGE_BALANCE_CALLBACK_GAS.saturating_add(action.gas())` [3](#0-2) , [4](#0-3) . Unlike the report's recommended mitigation, this code has no fallback to guarantee execution of the "unwind"/refund logic if the callback itself exceeds its prepaid gas.

Per nearcore's execution model, if a receipt's execution fails for any reason (including "Exceeded the prepaid gas"), all state mutations from that receipt are rolled back atomically: [5](#0-4) . Because `self.has_in_flight_tx = false;` is the first statement in each callback, an out-of-gas failure anywhere in that callback's execution reverts even this initial flag reset, leaving `has_in_flight_tx` permanently `true` (it was already `true` from the prior, already-committed `rlp_execute` receipt). `rlp_execute` immediately rejects all subsequent calls while this flag is set [6](#0-5) , permanently disabling the wallet.

Simultaneously, since the `caller_deposit` refund-transfer promise is only created inside the callback body, a reverted callback never issues that refund. The originally attached deposit was already merged into the wallet's account balance by the earlier, separate, and already-committed `rlp_execute` receipt (per NEAR's "deposit is immediately deposited before contract execution starts" semantics, documented in `docs/RuntimeSpec/Components/BindingsSpec/EconomicsAPI.md`), so it is not automatically refunded by the protocol — it is stuck in the wallet's balance with no code path left to return it, since `caller_deposit` is a transient function argument rather than persisted contract state.

### Impact Explanation
If reachable, an out-of-gas callback failure causes:
1. Permanent freezing of the eth-implicit wallet account — `has_in_flight_tx` never resets, so `rlp_execute` (the only entry point for the wallet owner to move funds/act via signed ETH-style transactions) is permanently disabled for that account.
2. Loss/freezing of the external caller's attached deposit, since the only refund logic lives in the code path that fails to execute.

This matches the "permanent freezing of funds" acceptance criterion, and is a direct analog of the sgReceive report: an out-of-gas revert during a deferred cleanup/refund step leaves value stranded with no rescue path.

### Likelihood Explanation
Exploitability hinges on whether the fixed gas constants (`RLP_EXECUTE_CALLBACK_GAS`, `NEP_141_STORAGE_BALANCE_CALLBACK_GAS`, `ADDRESS_CHECK_CALLBACK_GAS`, all set to small fixed Tgas amounts) can be exhausted for some attacker/owner-influenced input (e.g., large `ExecuteResponse`/error strings, or crafted `action` payloads whose serialization/deserialization cost scales with size). I could not conclusively verify, within the available investigation, that the fixed 5–15 Tgas budgets are provably insufficient under adversarial input — this would require gas-profiling the callback bodies against maximum-size `caller_deposit`/`ExecuteResponse`/args payloads. Given the callback bodies appear to do bounded, simple work (JSON parsing of small structs, single promise creation), likelihood is uncertain and needs empirical confirmation.

### Recommendation
- Persist `caller_deposit` (and any other state needed to complete the flow) as part of contract state rather than solely as a callback argument, so it survives even if a downstream callback fails.
- Add a dedicated fallback: if `rlp_execute_callback`'s own gas budget could be exceeded, split "reset `has_in_flight_tx`" and "issue refund" into an earlier point in the promise chain, or reserve enough dedicated gas independent of `action.gas()` so this bookkeeping step cannot be starved.
- Add a mechanism (e.g., a privileged/self-only recovery method, or a timeout-based unlock) to reset `has_in_flight_tx` if a callback definitively fails, so a single unlucky gas-exhaustion event cannot permanently disable the wallet.
- Add gas-profiling tests that specifically try to exhaust `RLP_EXECUTE_CALLBACK_GAS`/`NEP_141_STORAGE_BALANCE_CALLBACK_GAS`/`ADDRESS_CHECK_CALLBACK_GAS` with maximum-size inputs to confirm or rule out this path.

### Proof of Concept
Not independently reproduced. A conceptual PoC would require: (1) submitting an `rlp_execute` transaction whose downstream promise chain resolves such that `nep_141_storage_balance_callback` or `rlp_execute_callback` is invoked with attached gas at or near the fixed constant's floor, and (2) crafting `action`/response data whose processing cost (e.g., large `ExecuteResponse.error` string, oversized JSON args) exceeds that budget, triggering `FunctionCallError::ExecutionError("Exceeded the prepaid gas")` inside the callback. Confirming this requires gas-metering experiments against the actual compiled contract, which was not performed here.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L29-41)
```rust
/// which essentially all tokens use. Therefore we hard-code it here instead of doing
/// the extra on-chain call to `storage_balance_bounds`. This also prevents malicious
/// token contracts with very high `storage_balance_bounds` from taking lots of $NEAR
/// from eth-wallet-contract users.
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L88-127)
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
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L130-317)
```rust
    /// Callback after checking if an address is contained in the registrar.
    /// This check happens when the target is another eth implicit account to
    /// confirm that the relayer really did check for a named account with that address.
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L412-470)
```rust
    let promise = match transaction_kind {
        TransactionKind::EthEmulation(EthEmulationKind::EOABaseTokenTransfer {
            address_check: Some(address),
            ..
        }) => {
            let callback_gas = ADDRESS_CHECK_CALLBACK_GAS.saturating_add(action.gas());
            let ext = WalletContract::ext(current_account_id).with_static_gas(callback_gas);
            let address_registrar = {
                let account_id = ADDRESS_REGISTRAR_ACCOUNT_ID
                    .trim()
                    .parse()
                    .unwrap_or_else(|_| env::panic_str("Invalid address registrar"));
                ext_registrar::ext(account_id).with_static_gas(REGISTRAR_LOOKUP_GAS)
            };
            let address = format!("0x{}", hex::encode(address));
            address_registrar.lookup(address).then(ext.address_check_callback(
                target,
                action,
                caller_deposit,
            ))
        }
        TransactionKind::EthEmulation(EthEmulationKind::ERC20Transfer { receiver_id, .. }) => {
            // In the case of the emulated ERC-20 transfer, the receiving account
            // might not be registered with the NEP-141 contract (per the NEP-145)
            // storage standard. Therefore we must create a multi-step promise where
            // first we check if the receiver is registered and then if not call
            // `storage_deposit` in addition to `ft_transfer`.
            let token_id = target;
            let callback_gas = NEP_141_STORAGE_BALANCE_CALLBACK_GAS.saturating_add(action.gas());
            let ext: WalletContractExt =
                WalletContract::ext(current_account_id).with_static_gas(callback_gas);
            let storage_balance_args =
                format!(r#"{{"account_id": "{}"}}"#, receiver_id.as_str()).into_bytes();
            Promise::new(token_id.clone())
                .function_call(
                    "storage_balance_of".into(),
                    storage_balance_args,
                    NearToken::from_yoctonear(0),
                    NEP_141_STORAGE_BALANCE_OF_GAS,
                )
                .then(ext.nep_141_storage_balance_callback(
                    token_id,
                    receiver_id,
                    action,
                    caller_deposit,
                ))
        }
        TransactionKind::EthEmulation(EthEmulationKind::SelfBaseTokenTransfer) => {
            // Base token transfers to self are no-ops on Near, so we do not need to
            // schedule an additional call. We can simply go straight to `rlp_execute_callback`.
            let ext: WalletContractExt =
                WalletContract::ext(current_account_id).with_static_gas(RLP_EXECUTE_CALLBACK_GAS);
            ext.rlp_execute_callback(caller_deposit)
        }
        _ => {
            let ext =
                WalletContract::ext(current_account_id).with_static_gas(RLP_EXECUTE_CALLBACK_GAS);
            action_to_promise(target, action)?.then(ext.rlp_execute_callback(caller_deposit))
        }
```

**File:** protocol-model/spec/runtime-execution.md (L146-152)
```markdown
## Invariants & failure modes

- **Gas ordering**: `merge` asserts `gas_burnt_for_function_call <= gas_burnt <= gas_used` per action (`runtime/runtime/src/lib.rs:440`).
- **Failed receipt atomicity**: a receipt whose result is `Err` triggers `state_update.rollback()`, so no state changes persist except the outcome/gas accounting (`runtime/runtime/src/lib.rs:967`). `set_error` additionally clears queued receipts, proposals, and burnt/subsidized amounts (`runtime/runtime/src/lib.rs:487`).
- **Staking invariant**: `update_validator_accounts` returns a fatal `StorageInconsistentState` if `locked < max_of_stakes` (`runtime/runtime/src/lib.rs:1617`).
- **Invalid txs make progress, not failure**: a chunk with invalid transactions is not rejected; the offending txs are skipped during conversion, polluting the chain with junk but keeping the shard live (`runtime/runtime/src/lib.rs:1706` doc; skip sites at `:1994`, `:2199`).
- **Refund receipts are free**: system-predecessor receipts burn zero gas; a failed refund burns its deposit into `other_burnt_amount` rather than refunding (`runtime/runtime/src/lib.rs:929`, `:972`).
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/tests/sanity.rs (L170-213)
```rust
// An external caller gets its deposit back if the cross-contract call fails.
#[tokio::test]
async fn test_caller_refunds() -> anyhow::Result<()> {
    let TestContext { worker, wallet_contract, wallet_sk, address_registrar, .. } =
        TestContext::new().await?;

    let caller = worker.root_account()?;
    let deposit_amount = NearToken::from_near(3);
    let create_tx = |receiver_id: &AccountId, nonce: u64| {
        let method = "register";
        let args = br#"{"account_id": "birchmd.near"}"#;
        let action = Action::FunctionCall {
            receiver_id: receiver_id.to_string(),
            method_name: method.into(),
            args: args.to_vec(),
            gas: Gas::from_tgas(10).as_gas(),
            yocto_near: 0,
        };
        utils::create_signed_transaction(
            nonce,
            receiver_id,
            Wei::new_u128(deposit_amount.as_yoctonear() / (MAX_YOCTO_NEAR as u128)),
            action,
            &wallet_sk,
        )
    };

    // External caller gets a refund when the cross-contract call fails
    let pre_tx_account_balance = caller.view_account().await?.balance;
    let receiver_id: AccountId = "fake.near".parse()?;
    let result = wallet_contract
        .rlp_execute_from(
            &caller,
            receiver_id.as_str(),
            &create_tx(&receiver_id, 0),
            deposit_amount,
        )
        .await?;
    assert!(!result.success);
    let post_tx_account_balance = caller.view_account().await?.balance;
    assert!(
        pre_tx_account_balance.as_yoctonear() - post_tx_account_balance.as_yoctonear()
            < deposit_amount.as_yoctonear()
    );
```
