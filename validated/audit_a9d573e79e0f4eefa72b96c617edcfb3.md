### Title
Attached NEAR deposit is permanently trapped on synchronous (non-Promise) failure paths of `rlp_execute` - (File: `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs`)

### Summary
The `WalletContract::rlp_execute` entry point is marked `#[payable]` and is the standard way for a relayer/caller to attach a NEAR deposit (representing the ETH transaction's `value`) when submitting an RLP-encoded Ethereum transaction to an eth-implicit account's wallet contract. When the call succeeds asynchronously and later fails, the contract carefully threads a `CallerDeposit` through its promise chain and refunds it in `rlp_execute_callback` on `PromiseResult::Failed`. However, several synchronous, non-Promise failure paths inside `rlp_execute`/`inner_rlp_execute` return a plain `ExecuteResponse` value (not a panic) without ever creating a refund promise, so any attached deposit is silently retained by the wallet contract instead of being returned to the caller — the same bug class as the reported `bridgeRingToAbstract()` issue where a payable function short-circuits without refunding `msg.value`.

### Finding Description
`rlp_execute` is declared `#[payable]`: [1](#0-0) 

If another transaction is already in flight, it returns immediately with a failure `ExecuteResponse` — but with no promise and no refund of any attached deposit: [2](#0-1) 

Further down, the generic error branch from `inner_rlp_execute` also resolves to a plain value, again without refunding: [3](#0-2) 

Inside `inner_rlp_execute`, the `ExecutionContext` (which captures `env::attached_deposit()`) and the corresponding `CallerDeposit` are constructed up front, before the transaction is parsed/validated: [4](#0-3) 

If parsing/validation subsequently fails with a `User` error (nonce already incremented to block replay) or any non-Relayer error, the function returns `Err(err)` directly — the already-computed `caller_deposit` is simply discarded, never used to build a refund promise: [5](#0-4) 

This differs critically from the properly-refunding path, which only exists once a cross-contract-call Promise has actually been dispatched: `rlp_execute_callback` explicitly refunds the tracked `caller_deposit` when `PromiseResult::Failed`: [6](#0-5) 

The reason this results in permanent fund loss (rather than an automatic NEAR-protocol deposit refund) is that NEAR only auto-generates a deposit-refund receipt when the *entire receipt fails* (i.e., the contract call panics/aborts). Per the runtime spec, the attached deposit is credited to the receiving account's balance before execution starts, and deposit refunds are only produced "when an action receipt fails to execute": [7](#0-6) [8](#0-7) 

Since `rlp_execute` returns a normal `PromiseOrValue::Value(...)` (a successful execution outcome from the protocol's point of view) rather than panicking, no automatic refund receipt is ever generated, and the wallet contract itself does not issue a manual refund on these synchronous paths — so the caller's attached deposit is absorbed into the wallet contract's (i.e., the eth-implicit account owner's) balance instead of being returned to the actual depositor (`predecessor_account_id`).

### Impact Explanation
Any relayer or external caller who attaches NEAR to `rlp_execute` (as intended by the design, mirroring an ETH transaction's `value`) permanently loses that deposit whenever:
- another transaction is already in flight on that wallet (`has_in_flight_tx == true`), or
- the transaction fails synchronous validation with a `User`/`AccountId`/non-self-signed `Relayer` error before any promise is created.

The deposit is not returned to the caller; it is effectively transferred to the eth-implicit wallet account's balance without the depositor's consent. This is a direct, silent loss of funds for the caller, matching "concrete theft or permanent freezing of funds" from an ordinary unprivileged caller's transaction.

### Likelihood Explanation
This is reachable by any ordinary account calling `rlp_execute` with an attached deposit — no privileged role is required. The `has_in_flight_tx` race is trivially triggerable (e.g., submit a second `rlp_execute` while a prior one is pending), and validation failures (bad nonce, malformed calldata, wrong target) are common in normal relayer operation, especially under any concurrency or misconfiguration. Because the contract accepts attached deposits by design for value-carrying ETH-emulated transactions, users/relayers routinely attach non-zero deposits, making accidental loss realistic rather than purely theoretical.

### Recommendation
Ensure every code path that can consume/retain an attached deposit either:
1. Refunds the deposit via an explicit transfer promise to `predecessor_account_id` before returning a non-panicking `ExecuteResponse` (mirroring the existing `rlp_execute_callback` refund logic), for both the `has_in_flight_tx` early-return and the generic `Err(e) => PromiseOrValue::Value(e.into())` branch in `rlp_execute`; or
2. Panic/abort on these failure conditions instead of returning a value, so the NEAR protocol's automatic deposit-refund mechanism is triggered.
Additionally, thread the already-computed `caller_deposit`/`context.attached_deposit` through every early-return `Err` branch of `inner_rlp_execute` so it is never silently dropped.

### Proof of Concept
1. Attacker/careless caller `X` calls `wallet_contract.rlp_execute(target, tx_bytes_b64)` attaching `N` yoctoNEAR, while a prior `rlp_execute` call from a relayer is still in flight (`has_in_flight_tx == true`).
2. `rlp_execute` hits the early-return branch at `lib.rs:97-105`, returning `PromiseOrValue::Value(ExecuteResponse{ success: false, ... })` synchronously.
3. Because this is a successful (non-panicking) execution outcome, NEAR does not generate a deposit-refund receipt, and no promise-based refund is ever issued.
4. `X`'s `N` yoctoNEAR permanently becomes part of the wallet contract's account balance instead of being returned to `X`.
5. The same outcome occurs if the RLP transaction fails parsing/validation with a `User` error (e.g., stale nonce) after `caller_deposit` has already been computed in `inner_rlp_execute` (`lib.rs:340-409`): the deposit is dropped without refund.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L88-93)
```rust
    #[payable]
    pub fn rlp_execute(
        &mut self,
        target: AccountId,
        tx_bytes_b64: String,
    ) -> PromiseOrValue<ExecuteResponse> {
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L94-105)
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
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L116-127)
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
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L296-312)
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
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L336-345)
```rust
) -> Result<Promise, Error> {
    if *nonce == u64::MAX {
        return Err(Error::AccountNonceExhausted);
    }
    let context = ExecutionContext::new(
        current_account_id.clone(),
        predecessor_account_id,
        env::attached_deposit(),
    )?;
    let caller_deposit = CallerDeposit::new(&context);
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L389-409)
```rust
        Err(err @ Error::User(_)) => {
            // Increment nonce on all user errors to prevent replay.
            *nonce = nonce.saturating_add(1);
            return Err(err);
        }
        Err(err) => {
            // Do not increment nonce on Relayer or AccountId errors.
            // The latter error is an issue in the deployment (so the nonce is meaningless).
            // The former arises from the relayer itself doing something wrong and thus the
            // user's transaction could still be valid and potentially submitted properly by
            // another relayer. To allow this we do not increment the nonce.
            //
            // Note: if a relayer is using an access key for this wallet then that key will
            // still be revoked (in the main logic of `rlp_execute`). This fact together with
            // the condition that there only be one in-flight transaction at a time implies
            // that a relayer cannot maliciously burn a large portion of the user's tokens.
            // If the relayer is not using an access key then they are spending their own
            // resources on the gas and therefore we do not care if the relayer submits
            // the same faulty transaction multiple times.
            return Err(err);
        }
```

**File:** docs/RuntimeSpec/Refunds.md (L15-18)
```markdown
## Deposit Refunds

Deposit refunds are generated when an action receipt fails to execute. All attached deposit amounts are summed together and
sent as a refund to a `predecessor_id` (because only the predecessor can attach deposits).
```

**File:** docs/RuntimeSpec/Components/BindingsSpec/EconomicsAPI.md (L7-10)
```markdown
- `account_balance` -- the balance attached to the given account. This includes the `attached_deposit` that was attached
  to the transaction;
- `attached_deposit` -- the balance that was attached to the call that will be immediately deposited before
  the contract execution starts;
```
