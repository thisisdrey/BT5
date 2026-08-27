### Title
Hard-coded static callback gas in the eth-implicit Wallet Contract can permanently brick a user's account and strand attached funds - (File: `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs`)

### Summary
The Wallet Contract that is deployed to every ETH-implicit account uses fixed, hard-coded `Gas` constants (`RLP_EXECUTE_CALLBACK_GAS`, `ADDRESS_CHECK_CALLBACK_GAS`, `NEP_141_STORAGE_BALANCE_CALLBACK_GAS`, etc.) to schedule the callback that clears the contract's `has_in_flight_tx` re-entrancy guard [1](#0-0) . This is the same bug class as the reported Solidity issue: code assumes a fixed gas allowance will always be sufficient to run a piece of logic that must complete for the contract/account to remain usable. In Solidity, `transfer()`'s fixed 2300-gas stipend became insufficient after Istanbul repriced `SLOAD`, permanently stranding ETH. In the Wallet Contract, the fixed static gas attached to the callback that resets `has_in_flight_tx` and refunds the caller can likewise become insufficient (e.g., after a future protocol fee/parameter change raises the cost of the operations the callback performs), causing the callback receipt to fail. Because the guard flag is set to `false` only inside that callback, a failed/rolled-back callback leaves `has_in_flight_tx == true` forever, permanently locking the account out of further use and, in some paths, permanently stranding the deposit/refund that was supposed to flow back to the caller.

### Finding Description
`rlp_execute` sets `self.has_in_flight_tx = true` and returns a `Promise` chain ending in `rlp_execute_callback` (or `address_check_callback` / `nep_141_storage_balance_callback`, which themselves clear the flag and then chain to `rlp_execute_callback`) [2](#0-1) .

The flag is only cleared as the *first* statement inside these `#[private]` callback methods: [3](#0-2) [4](#0-3) 

All of these callbacks are scheduled with a **hard-coded, fixed `Gas` budget** via `with_static_gas`: [5](#0-4) [6](#0-5) [7](#0-6) 

If a receipt runs out of prepaid/attached gas partway through execution, the whole receipt fails and all state mutations made during that execution — including `self.has_in_flight_tx = false;` — are rolled back, per the runtime's failed-receipt atomicity guarantee (`state_update.rollback()` on `Err`) [8](#0-7) . Because these gas budgets are fixed Rust constants (`Gas::from_tgas(5)` and small additions thereof) rather than computed from "all remaining gas" (e.g. via `GasWeight`), any future increase in the underlying cost of the operations performed in the callback (base function-call fee, promise-creation fee, cross-contract-call base fee, etc., all of which are protocol parameters that can and do change across protocol versions, e.g. `AccountCostIncrease` at PV 85 already raised several action fees [9](#0-8) ) can push actual gas usage above the hard-coded static budget.

This is structurally identical to the reported bug: `transfer()`'s 2300-gas stipend was a fixed constant that later became insufficient once `SLOAD`'s price rose under Istanbul, and any contract logic depending on that fixed stipend running to completion could get "stuck." Here, the fixed static-gas callback is the analogous single point of failure: if it fails to complete, the guard is never reset.

### Impact Explanation
If the guard-clearing callback fails due to gas exhaustion (or any other failure that also prevents `rlp_execute_callback`'s refund-transfer promise from being created, see `rlp_execute_callback`'s failure-refund path at [10](#0-9) ), two consequences follow:
1. `has_in_flight_tx` remains `true` permanently. Every subsequent call to `rlp_execute` on that account will short-circuit and return the "transaction already in progress" error [11](#0-10) , since `rlp_execute` is the *only* entry point into the Wallet Contract — there is no owner/admin/relayer method to reset the flag. This permanently bricks the ETH-implicit account, since ETH-implicit accounts "can only be used by calling the methods of the Wallet Contract... It cannot be deleted, nor can a full access key be added" [12](#0-11) .
2. Any NEAR/attached deposit that was part of the in-flight transaction (including the `caller_deposit` intended to be refunded on failure) becomes permanently unreachable, since the only mechanism to move funds out of the account is through the now-permanently-blocked `rlp_execute` entry point.

This matches the required impact bar: permanent freezing of funds (the account's balance becomes forever inaccessible) reachable purely by an ordinary ETH-implicit-account user/relayer performing a normal `rlp_execute` transaction.

### Likelihood Explanation
Today the fixed constants (5 Tgas per hop) are likely calibrated to be sufficient under the current fee schedule, so this is not immediately triggerable under present-day parameters — I could not verify actual current gas consumption of `rlp_execute_callback`/`address_check_callback` against the 5 Tgas budget from static analysis alone. However, the likelihood becomes non-negligible over the life of the protocol: NEAR runtime/action fees are governed by versioned `RuntimeConfig` parameters that are changed across protocol upgrades (the codebase itself documents multiple such fee increases, e.g. `AccountCostIncrease` raising `create_account`/action fees at PV 85 [13](#0-12) ). Because the Wallet Contract's callback gas is a hard-coded constant baked into deployed WASM rather than derived dynamically (e.g., via `GasWeight`/"use all remaining gas"), any future fee increase to the base function-call/promise-creation/`FUNCTION_CALL_BASE`-style costs risks silently under-provisioning these callbacks for all already-deployed ETH-implicit accounts, exactly as Istanbul's `SLOAD` repricing silently broke `transfer()`-based contracts.

### Recommendation
- Do not hard-code fixed `Gas` constants for the guard-clearing / refund callback. Instead, attach all (or a large weighted fraction of) remaining prepaid gas to `rlp_execute_callback`, `address_check_callback`, and `nep_141_storage_balance_callback` using `GasWeight` so the callback's gas budget scales with whatever the caller actually attaches, insulating it from future changes in the underlying host-function/action costs.
- Alternatively/additionally, split the guard-reset from the potentially-gas-variable refund logic so that `has_in_flight_tx = false` is guaranteed to be set even in low-gas conditions (e.g., perform the flag reset in a minimal, first, cheap statement inside a receipt that is virtually guaranteed to have enough gas, and treat refund failures as a separate, retryable failure mode rather than one that can also revert the flag reset).
- Add a recovery/admin-independent mechanism (e.g., a time-locked self-service "force unlock" method) so `has_in_flight_tx` cannot become permanently stuck even if a callback receipt fails for reasons outside the contract's control (e.g., temporary congestion, other host errors).

### Proof of Concept
Conceptual reproduction (not verified against current mainnet gas costs, since exact per-op gas costs are protocol parameters not fully enumerable from static code review):
1. A relayer submits an `rlp_execute` transaction for an ETH-implicit account, triggering the default fallback path: `action_to_promise(target, action)?.then(ext.rlp_execute_callback(caller_deposit))` with `ext` static-gas-limited to `RLP_EXECUTE_CALLBACK_GAS` (`Gas::from_tgas(5)`) [14](#0-13) .
2. `self.has_in_flight_tx = true` is committed as part of the outer receipt [15](#0-14) .
3. Following a hypothetical future protocol-version fee increase to relevant action/function-call base costs (analogous to Istanbul's `SLOAD` repricing), the 5 Tgas budget attached to `rlp_execute_callback` becomes insufficient to complete execution.
4. The callback receipt fails with a gas-exhaustion error; per `runtime/runtime/src/lib.rs`'s failed-receipt handling, all state changes from that receipt — including `self.has_in_flight_tx = false` — are rolled back [8](#0-7) .
5. `has_in_flight_tx` remains `true` in contract state forever. Any subsequent `rlp_execute` call to this account immediately returns the "transaction already in progress" error [11](#0-10)  and no funds can ever be moved out of the account again.

Note: I was not able to execute the actual gas-metering numbers for `rlp_execute_callback` against the 5 Tgas constant, nor find an existing admin/relayer path to reset `has_in_flight_tx`, from the indexed portions of the repository alone; if such a reset path exists elsewhere in `runtime/near-wallet-contract`, it was not surfaced by search. A full Devin session with complete file access would be needed to confirm the exact current gas headroom and rule out any unindexed recovery mechanism.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L34-41)
```rust
const NEP_141_STORAGE_DEPOSIT_GAS: Gas = Gas::from_tgas(5);
const NEP_141_STORAGE_BALANCE_OF_GAS: Gas = Gas::from_tgas(5);
const REGISTRAR_LOOKUP_GAS: Gas = Gas::from_tgas(5);
const RLP_EXECUTE_CALLBACK_GAS: Gas = Gas::from_tgas(5);
const ADDRESS_CHECK_CALLBACK_GAS: Gas = Gas::from_tgas(5).saturating_add(RLP_EXECUTE_CALLBACK_GAS);
const NEP_141_STORAGE_BALANCE_CALLBACK_GAS: Gas = Gas::from_tgas(5)
    .saturating_add(NEP_141_STORAGE_DEPOSIT_GAS)
    .saturating_add(RLP_EXECUTE_CALLBACK_GAS);
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L89-128)
```rust
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L133-140)
```rust
    #[private]
    pub fn address_check_callback(
        &mut self,
        target: AccountId,
        action: near_action::Action,
        caller_deposit: Option<CallerDeposit>,
    ) -> PromiseOrValue<ExecuteResponse> {
        self.has_in_flight_tx = false;
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L178-192)
```rust
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L219-273)
```rust
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L276-285)
```rust
    pub fn rlp_execute_callback(
        &mut self,
        caller_deposit: Option<CallerDeposit>,
    ) -> ExecuteResponse {
        self.has_in_flight_tx = false;
        let n = env::promise_results_count();

        if n == 0 {
            // `rlp_execute_callback` is called directly in the case of an emulated self-transfer.
            return ExecuteResponse { success: true, success_value: None, error: None };
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L296-317)
```rust
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

**File:** protocol-model/spec/runtime-execution.md (L126-128)
```markdown
| `EnforcePerReceiptStorageProofLimit` | **86** (`version.rs:576`) | Snapshots per-receipt storage-proof upper bound before executing a receipt's actions (`runtime/runtime/src/lib.rs:838`). This is the sole PV-86 feature on 2.13.0. |
| `AccountCostIncrease` | 85 (`version.rs:574`) | `gas_burn_price = min(purchase, current)`, price-surplus refund, and `create_account_charge` (`runtime/runtime/src/lib.rs:920`, `:1234`). |
| `ClampOutgoingGasAdmission` | 85 (`version.rs:573`) | Clamps outgoing-receipt gas admission in the receipt sink (`runtime/runtime/src/congestion_control.rs:443`). See [cross-shard congestion](cross-shard-congestion.md). |
```

**File:** protocol-model/spec/runtime-execution.md (L146-149)
```markdown
## Invariants & failure modes

- **Gas ordering**: `merge` asserts `gas_burnt_for_function_call <= gas_burnt <= gas_used` per action (`runtime/runtime/src/lib.rs:440`).
- **Failed receipt atomicity**: a receipt whose result is `Err` triggers `state_update.rollback()`, so no state changes persist except the outcome/gas accounting (`runtime/runtime/src/lib.rs:967`). `set_error` additionally clears queued receipts, proposals, and burnt/subsidized amounts (`runtime/runtime/src/lib.rs:487`).
```

**File:** docs/DataStructures/Account.md (L121-122)
```markdown
An ETH-implicit account can only be used by calling the methods of the [Wallet Contract](#wallet-contract). It cannot be deleted, nor can a full access key be added.
The primary purpose of ETH-implicit accounts is to enable seamless integration of existing Ethereum tools (such as wallets) with the NEAR blockchain.
```

**File:** protocol-model/spec/economics.md (L84-84)
```markdown
| `AccountCostIncrease` | PV 85 (`version.rs:574`) | Gas now burnt at `min(purchase, block)` price; the surplus from a *dropped* gas price is refunded to the signer instead of retained as burnt; contract reward priced at the burn price; a `create_account_charge` (`account_creation_charge`, mainnet `0.007 N`) is levied on account creation. Also raises the `create_account` action fee and sets `min_gas_purchase_price = 1e9 yN` (`core/parameters/res/runtime_configs/85.yaml`). Gated at `lib.rs:920`, `lib.rs:978`, `lib.rs:998`, `lib.rs:1202`, `lib.rs:1243`. |
```
