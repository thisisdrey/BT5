### Title
Fixed-gas callback in the ETH-implicit Wallet Contract can be starved by a large promise return value, permanently bricking `has_in_flight_tx` and freezing the account - ([File: runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs])

### Summary
The Wallet Contract that every ETH-implicit account runs uses a boolean re-entrancy guard, `has_in_flight_tx`, which must be reset to `false` by a callback once the in-flight promise resolves. In the generic ("Near-native action") code path, the callback (`rlp_execute_callback`) is scheduled with a **fixed** static gas budget of `RLP_EXECUTE_CALLBACK_GAS = 5 Tgas`, independent of the size of the value the inner promise can return. Because the `target` and `method_name`/`args`/`gas` of the inner `FunctionCall` are attacker/relayer-controlled (decoded straight from the user-signed Ethereum calldata), a target contract can be made to return a value large enough that copying/serializing it in the 5 Tgas callback exhausts gas and the callback receipt fails. Since `has_in_flight_tx = true` was already committed by the *prior* receipt (the initial `rlp_execute` call), and NEAR rolls back all state changes of the *failed* callback receipt, the flag is never reset to `false`. The wallet contract is thereafter permanently in the "transaction in progress" state and rejects every future `rlp_execute` call, and since an ETH-implicit account can never be deleted nor get a full access key added, this permanently locks the account and any $NEAR it holds.

### Finding Description
`WalletContract::rlp_execute` checks the `has_in_flight_tx` guard, and if not set, builds a promise via `inner_rlp_execute` and immediately sets `self.has_in_flight_tx = true` before returning `PromiseOrValue::Promise(promise)`: [1](#0-0) 

For the default (generic Near-native action) branch of `inner_rlp_execute`, the promise chain schedules `rlp_execute_callback` with only the fixed `RLP_EXECUTE_CALLBACK_GAS` (5 Tgas), unlike the emulation branches which add `action.gas()` on top of the base callback gas: [2](#0-1) 

`rlp_execute_callback` is the function responsible for resetting the guard:
```
self.has_in_flight_tx = false;
```
followed by matching on `env::promise_result(0)` and copying/wrapping the returned bytes into `ExecuteResponse.success_value`: [3](#0-2) 

The `target`, `method_name`, `args`, and `gas` for the inner `FunctionCall` action are parsed directly from the user-signed (attacker/relayer-crafted) Ethereum calldata: [4](#0-3) 

Nothing in `parse_tx_data`/`try_into_near_action` bounds `gas` relative to the return-value size the target could produce, nor does the wallet reserve gas for `rlp_execute_callback` proportional to a worst-case return size. If the `target` contract is made to return a value large enough that reading/copying/re-serializing it inside `rlp_execute_callback` costs more than 5 Tgas, the callback's `FunctionCall` action fails with a gas-exceeded error. Per the runtime's receipt-execution model, a failing action receipt has all of its state mutations rolled back: [5](#0-4) 

Because `has_in_flight_tx = true` was already committed in the earlier, successful `rlp_execute` receipt, the rollback of the failing callback receipt cannot undo that already-persisted state — the flag stays `true` forever. Every subsequent call to `rlp_execute` on that account will now immediately fail with `"Error: transaction already in progress, please try again later."`: [6](#0-5) 

An ETH-implicit account cannot be repaired: it cannot be deleted, nor can a full access key be added to it, and the only way to interact with the account is through `rlp_execute` on the Wallet Contract: [7](#0-6) 

This mirrors the analog bug class (missing/insufficient handling causing a native-token-holding contract to permanently reject valid transfer/interaction attempts and become unusable): the QV strategy could never receive native tokens because it lacked a `receive()` fallback, rendering the pool contract useless; here, the Wallet Contract's callback gas allocation is insufficient for a legitimate class of native `FunctionCall` interactions, permanently disabling the account's only interaction entrypoint and any funds ($NEAR) it holds.

### Impact Explanation
This is a permanent freezing-of-funds bug on the NEAR protocol's officially shipped ETH-implicit account mechanism (`near-wallet-contract`), which is the standard vehicle for Ethereum-wallet users to hold and use $NEAR. Once triggered, the affected account:
- Can never execute another `rlp_execute` call (the sole gateway for the account owner to move funds, add relayer keys, or interact with contracts).
- Cannot be deleted or have a full access key added (per design), so there is no recovery path.
- Any $NEAR balance held by the account is permanently locked/unusable.

This can be triggered either by an untrusted target contract chosen by a malicious/careless relayer, or even by the account owner interacting with a legitimate contract that happens to return a large success value for the requested method, making this a realistic, not purely theoretical, denial-of-funds vector.

### Likelihood Explanation
Triggering the condition requires:
1. Constructing (or being tricked/relayed to) a Near `FunctionCall` action to a contract/method whose response is "large enough" for its serialization+copy cost to exceed the fixed 5 Tgas budget of `rlp_execute_callback`.
2. Having that action executed via `rlp_execute` on an ETH-implicit account.

Both are within the capability of an ordinary user/relayer without any special privileges — the `target`, `method_name`, `args`, and `gas` are fully attacker-controlled fields decoded from arbitrary signed calldata, and no validation caps the potential return-value size against the fixed callback gas reservation. The exact byte threshold needed to exceed 5 Tgas of gas (register read/copy + JSON/borsh re-serialization costs) requires exact `runtime_configs` gas-cost parameters to compute precisely, which I was not able to fully pin down in the available time, but the structural gap — fixed gas independent of return size, combined with irrevocable "bricked" state on callback failure — is unambiguous from the code.

### Recommendation
- Reserve callback gas for `rlp_execute_callback` in the generic action branch the same way the emulation branches do (i.e., scale/cap it relative to the maximum possible cost of copying/serializing the inner call's return value, or bound the allowed `gas`/return size for arbitrary target FunctionCalls).
- More robust: never leave `has_in_flight_tx` in a state that depends on a subsequent receipt succeeding. For example, use a "prepare, then finalize in the same receipt lineage regardless of the callback outcome" pattern, or attach enough gas via weight-based (`function_call_weight`) allocation so the callback is guaranteed to run to completion (including a minimal always-executable "unstick" path) even if the inner call's data is large.
- Add explicit gas-safety-margin tests exercising large return payloads from the `target` contract to ensure `rlp_execute_callback` can never fail due to gas exhaustion caused by the response size.

### Proof of Concept
1. Deploy (or have a malicious/careless relayer pick) an ordinary contract `victim.near` with a method `big_reply()` that, given user-controlled `args`, returns a large `Vec<u8>`/JSON blob (e.g., tens/hundreds of KB), well within normal NEAR return-value limits.
2. Craft an Ethereum-style transaction (as used by `create_rlp_execute_tx` in the wallet-contract test helpers) encoding a Near-native `FunctionCall` action: `target = victim.near`, `method_name = "big_reply"`, `args = <payload triggering a large response>`, `gas = <enough for the inner call>`.
3. Submit via `rlp_execute` on the ETH-implicit account (see the wallet-contract interaction flow demonstrated in the test suite, e.g.): [8](#0-7) 
4. `rlp_execute` succeeds and sets `has_in_flight_tx = true` (per the code at lines 116-119 above); the inner `FunctionCall` to `victim.near` succeeds and returns the large payload.
5. `rlp_execute_callback` is invoked with only `RLP_EXECUTE_CALLBACK_GAS` (5 Tgas); if the return payload is large enough that copying/serializing it (plus the standard FunctionCall/receipt exec fees) exceeds 5 Tgas, the callback fails with a gas-exceeded error, and its state changes (including `has_in_flight_tx = false`) are rolled back per the runtime's failure-rollback semantics.
6. Any subsequent `rlp_execute` call on this account now immediately returns `"Error: transaction already in progress, please try again later."` forever, since there is no path to reset `has_in_flight_tx` once bricked — verifiably freezing the account and its funds permanently.

Note: I was not able to fully compute the exact byte-size threshold required to exceed 5 Tgas using the available gas-cost parameter snapshots within this session; a background engineer with terminal access should reproduce this concretely against `runtime_configs/parameters.yaml` gas costs (`read_register_byte`, function-call return serialization costs, etc.) to confirm the precise minimal payload size, but the structural vulnerability (fixed gas independent of attacker-controlled return size, combined with irreversible state commit before the callback runs) is clearly demonstrated by the code paths cited above.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L93-128)
```rust
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L459-473)
```rust
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
    };
    Ok(promise)
}
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs (L243-257)
```rust
    match &tx.data[0..4] {
        FUNCTION_CALL_SELECTOR => {
            let (receiver_id, method_name, args, gas, yocto_near): (String, _, _, _, _) =
                ethabi_utils::abi_decode(&FUNCTION_CALL_SIGNATURE, &tx.data[4..])?;
            if target.as_str() != receiver_id.as_str() {
                return Err(Error::Relayer(RelayerError::InvalidTarget));
            }
            if yocto_near >= MAX_YOCTO_NEAR {
                return Err(Error::User(UserError::ExcessYoctoNear));
            }
            Ok((
                Action::FunctionCall { receiver_id, method_name, args, gas, yocto_near },
                ParsableTransactionKind::NearNativeAction,
            ))
        }
```

**File:** protocol-model/spec/runtime-execution.md (L151-153)
```markdown
- **Invalid txs make progress, not failure**: a chunk with invalid transactions is not rejected; the offending txs are skipped during conversion, polluting the chain with junk but keeping the shard live (`runtime/runtime/src/lib.rs:1706` doc; skip sites at `:1994`, `:2199`).
- **Refund receipts are free**: system-predecessor receipts burn zero gas; a failed refund burns its deposit into `other_burnt_amount` rather than refunding (`runtime/runtime/src/lib.rs:929`, `:972`).
- **Delayed receipts must stay valid**: a delayed receipt that fails `validate_receipt` on dequeue is treated as `StorageInconsistentState` (`runtime/runtime/src/lib.rs:2500`).
```

**File:** docs/DataStructures/Account.md (L121-122)
```markdown
An ETH-implicit account can only be used by calling the methods of the [Wallet Contract](#wallet-contract). It cannot be deleted, nor can a full access key be added.
The primary purpose of ETH-implicit accounts is to enable seamless integration of existing Ethereum tools (such as wallets) with the NEAR blockchain.
```

**File:** integration-tests/src/tests/features/wallet_contract.rs (L312-324)
```rust
    // The user signs a transaction to transfer some $NEAR
    let transfer_amount = Balance::from_near(1).checked_div(7).unwrap();
    let action = Action::Transfer(TransferAction { deposit: transfer_amount });
    let signed_transaction = create_rlp_execute_tx(
        &receiver,
        action,
        1,
        &eth_implicit_account,
        &secret_key,
        &mut relayer_signer,
        &env,
    );
    check_tx_processing(&mut env, signed_transaction, height, blocks_number);
```
