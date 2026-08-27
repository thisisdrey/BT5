### Title
Permanent freezing of an ETH-implicit account's funds via stuck `has_in_flight_tx` flag in the Wallet Contract - ([File: runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs])

### Summary
The Wallet Contract that backs every ETH-implicit account uses a `has_in_flight_tx` boolean to serialize transaction execution. It is set to `true` before a cross-contract promise is dispatched and is only reset to `false` inside the terminal callback (`rlp_execute_callback`) or the error-path callbacks. If that callback ever fails to complete (e.g. traps on out-of-gas), the state mutation that resets the flag is rolled back by the runtime, leaving `has_in_flight_tx` stuck at `true` forever. Because `rlp_execute` unconditionally refuses to do anything while the flag is `true`, and ETH-implicit accounts can never be deleted nor have a full-access key added, there is no way for the owner to ever recover: the account and any $NEAR/token balance it holds become permanently unusable.

### Finding Description
`WalletContract::rlp_execute` gates all execution on `has_in_flight_tx`: [1](#0-0) 

Every code path that dispatches a promise sets the flag `true`, and only the terminal callback resets it, at the very first line of the function: [2](#0-1) 

That callback is invoked with a fixed, small static gas budget (`RLP_EXECUTE_CALLBACK_GAS = 5 Tgas`), which is chained onto an arbitrary attacker-controlled target when the action is a `FunctionCall`: [3](#0-2) [4](#0-3) 

The `target` of a `FunctionCall` action is fully attacker-controlled (any deployed contract the ETH-implicit-account owner or a relayer chooses to call), and its return value (`env::promise_result(0)`) is copied verbatim into `ExecuteResponse.success_value` and then serialized/returned by the callback: [5](#0-4) 

Because the fee-config protocol guarantees a failed receipt is fully rolled back (no partial state persists), if the callback traps for any reason after line 280 but before returning successfully (e.g., insufficient static gas to process/copy/serialize a large `success_value`, or any other panic), the earlier write of `self.has_in_flight_tx = false` is discarded along with everything else in that receipt: [6](#0-5) 

Since `has_in_flight_tx` was set to `true` in a *previous, already-committed* receipt (the one that dispatched the promise chain), the account is left permanently in the "transaction in progress" state. `rlp_execute` will reject every subsequent call forever: [7](#0-6) 

Crucially, unlike the `Comet`/`Bulker` report where the team relied on upgradeability or a manager-only sweep function, ETH-implicit accounts have no equivalent recovery mechanism: they cannot be deleted and a full-access key can never be added to them, so there is no way to bypass the Wallet Contract or reclaim the funds once it is bricked: [8](#0-7) 

### Impact Explanation
Any $NEAR (or token) balance held by the ETH-implicit account becomes permanently inaccessible once `has_in_flight_tx` gets stuck `true`, because the Wallet Contract is the only way to act on behalf of that account and it will unconditionally reject all further `rlp_execute` calls. This is a direct, irreversible freezing of user funds, matching the "locked assets" bug class from the analog report (ETH stuck in `Comet`/`Bulker` with no withdrawal path) — except here there is no upgrade path or manager-only sweep to fall back on, since the account's contract cannot be replaced and no full-access key can ever be attached.

### Likelihood Explanation
Reaching this state requires only an ordinary signed transaction from the account owner or its authorized relayer performing an `rlp_execute` call whose decoded action is a `FunctionCall` to a contract (owner-chosen or attacker-controlled) that returns an outsized payload, exhausting the fixed 5 Tgas static budget allocated to `rlp_execute_callback` before it finishes copying/serializing the returned bytes. No validator or node misbehavior is needed — it is purely a user-transaction-driven, unprivileged-signer path through the eth-implicit wallet contract.

### Recommendation
- Reset `has_in_flight_tx` in a way that survives a callback failure, e.g. by having a preceding step (or the dispatcher itself, before spawning the promise) schedule a best-effort/guaranteed unlock, or by using a `.then()` failure-safe pattern that does not depend on the terminal callback executing successfully.
- Bound/limit the size of data returned from arbitrary `FunctionCall` targets before copying it into `ExecuteResponse`, or allocate gas proportionally to the observed return-value size instead of a fixed static budget.
- Add an explicit, permissionless "unstick" mechanism (e.g., a timeout-based reset of `has_in_flight_tx` if no callback fires within N blocks) so a single failed cross-contract call can never permanently brick the account.

### Proof of Concept
1. Deploy a contract `Evil` with a method `boom()` that returns a return value sized close to the maximum allowed return-data size (e.g., several hundred KB/near the protocol max).
2. Using a relayer with a `FunctionCall`-restricted access key on the ETH-implicit account (as set up in `test_wallet_contract_interaction`), sign an Ethereum-style transaction whose decoded Near action is `FunctionCall` targeting `Evil::boom()`.
3. Submit via `rlp_execute`. `inner_rlp_execute` builds `Promise::new(Evil).function_call(...).then(ext.rlp_execute_callback(...))` with `RLP_EXECUTE_CALLBACK_GAS` (5 Tgas) statically allocated for the callback (`runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs:462-470`).
4. `Evil::boom()` succeeds and returns its large payload; `rlp_execute_callback` receives it via `env::promise_result(0)` and attempts to package/serialize it as `ExecuteResponse.success_value` (`runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs:313-315`), exceeding the 5 Tgas budget and trapping.
5. The trapped receipt is rolled back per the standard failed-receipt atomicity rule, so `has_in_flight_tx = false` (set at line 280) never persists; the account's stored `has_in_flight_tx` remains `true` from the prior committed receipt.
6. Any further `rlp_execute` call by the legitimate owner now unconditionally returns `"Error: transaction already in progress, please try again later."` (line 97-104) forever, and because ETH-implicit accounts can never receive a full-access key or be deleted (`docs/DataStructures/Account.md:121`), the account's funds are permanently frozen.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L37-41)
```rust
const RLP_EXECUTE_CALLBACK_GAS: Gas = Gas::from_tgas(5);
const ADDRESS_CHECK_CALLBACK_GAS: Gas = Gas::from_tgas(5).saturating_add(RLP_EXECUTE_CALLBACK_GAS);
const NEP_141_STORAGE_BALANCE_CALLBACK_GAS: Gas = Gas::from_tgas(5)
    .saturating_add(NEP_141_STORAGE_DEPOSIT_GAS)
    .saturating_add(RLP_EXECUTE_CALLBACK_GAS);
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L94-128)
```rust
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L466-472)
```rust
        _ => {
            let ext =
                WalletContract::ext(current_account_id).with_static_gas(RLP_EXECUTE_CALLBACK_GAS);
            action_to_promise(target, action)?.then(ext.rlp_execute_callback(caller_deposit))
        }
    };
    Ok(promise)
```

**File:** protocol-model/spec/runtime-execution.md (L149-149)
```markdown
- **Failed receipt atomicity**: a receipt whose result is `Err` triggers `state_update.rollback()`, so no state changes persist except the outcome/gas accounting (`runtime/runtime/src/lib.rs:967`). `set_error` additionally clears queued receipts, proposals, and burnt/subsidized amounts (`runtime/runtime/src/lib.rs:487`).
```

**File:** docs/DataStructures/Account.md (L119-122)
```markdown
Once a NEAR-implicit account is created it acts as a regular account until it's deleted.

An ETH-implicit account can only be used by calling the methods of the [Wallet Contract](#wallet-contract). It cannot be deleted, nor can a full access key be added.
The primary purpose of ETH-implicit accounts is to enable seamless integration of existing Ethereum tools (such as wallets) with the NEAR blockchain.
```
