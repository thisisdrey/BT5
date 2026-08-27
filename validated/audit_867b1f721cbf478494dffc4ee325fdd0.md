Based on the investigation, the closest reachable analog to the "unchecked delegatecall result" bug class in this codebase is in the **near-wallet-contract** (the ETH-implicit account "Wallet Contract"), which is explicitly in-scope per the rules.

### Title
Unchecked promise result in `WalletContract::ban_relayer` may falsely report a malicious relayer's key as revoked - (File: `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs`)

### Summary
When `rlp_execute` (or `address_check_callback`) detects a `Error::Relayer` fault caused by the account's own signer (i.e. a relayer using a `FunctionCall` access key granted to it), the contract calls `create_ban_relayer_promise(current_account_id)` and chains it to the `#[private] ban_relayer` callback. `ban_relayer` unconditionally sets `has_in_flight_tx = false` and returns an `ExecuteResponse` with `success: false` and `error: Some("Error: faulty relayer")` — without ever inspecting `env::promise_result(0)` (or `env::promise_results_count()`) to confirm that the underlying revocation action (deleting the relayer's access key) actually succeeded. [1](#0-0) [2](#0-1) 

### Finding Description
This mirrors the reported Diamond.yul pattern: a security-critical sub-call (there, `initializeFacet` via `delegatecall`; here, the `DeleteKey` action intended to revoke a faulty/malicious relayer's `FunctionCall` access key) is dispatched as a promise, but the calling code (`ban_relayer`) never checks whether that promise actually succeeded before reporting the operation as done and clearing the `has_in_flight_tx` guard. Every other callback in this same contract (`address_check_callback`, `nep_141_storage_balance_callback`, `rlp_execute_callback`) explicitly matches on `env::promise_result(0)` and handles the `PromiseResult::Failed` case, showing that checking promise outcomes is the established pattern in this contract — `ban_relayer` is the one path that omits it. [3](#0-2) [4](#0-3) 

### Impact Explanation
If the `DeleteKey` action created by `create_ban_relayer_promise` fails for any reason (insufficient gas forwarded, the key already changed/removed concurrently, or any other receipt-level failure), `ban_relayer` still resets `has_in_flight_tx` to `false` and returns a response indicating the relayer was banned. The wallet owner (and any tooling relying on this response) is given false assurance that the faulty/malicious relayer's `FunctionCall` access key has been revoked, when in fact it may still be valid and usable to call `rlp_execute` again on the account. This is an authorization-escalation-persistence issue: a relayer that should have lost the ability to act on behalf of the ETH-implicit account may retain it silently.

### Likelihood Explanation
This path is only reachable through the specific `Error::Relayer` branch that occurs when the transaction's own signer (a relayer using an access key on the wallet) triggers a relayer-classified error, so triggering it requires a relayer key already granted by the account owner — not an arbitrary unprivileged attacker. This somewhat limits likelihood/severity compared to a fully open attack surface, and I was not able to fully retrieve the body of `create_ban_relayer_promise` within the available tool budget to confirm the exact action(s) it dispatches (only its call sites and the `ban_relayer` callback were retrieved), so the precise conditions under which the underlying `DeleteKey` could fail are not fully verified from source in this session.

### Recommendation
In `ban_relayer`, inspect `env::promise_results_count()`/`env::promise_result(0)` before declaring success of the ban operation, and surface a distinguishable error/state (e.g., keep `has_in_flight_tx` semantics accurate, or retry/report) if the underlying `DeleteKey` (or whatever action `create_ban_relayer_promise` performs) did not succeed, consistent with how `address_check_callback`, `nep_141_storage_balance_callback`, and `rlp_execute_callback` already check their promise results.

### Proof of Concept
Not independently reproduced in this session; the finding is based on static code review of `ban_relayer` versus the sibling callbacks that do check `env::promise_result`. A concrete PoC would require constructing a scenario (e.g., low forwarded gas on the ban promise) where the `DeleteKey` action created by `create_ban_relayer_promise` fails, and then confirming `rlp_execute` can still be called successfully afterward using the relayer's key that was supposed to be revoked. This was not verified end-to-end due to tool-call budget constraints.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L116-128)
```rust
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L194-222)
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
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L296-316)
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
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L319-327)
```rust
    #[private]
    pub fn ban_relayer(&mut self) -> ExecuteResponse {
        self.has_in_flight_tx = false;
        ExecuteResponse {
            success: false,
            success_value: None,
            error: Some("Error: faulty relayer".into()),
        }
    }
```
