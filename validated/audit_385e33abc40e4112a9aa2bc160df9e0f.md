### Title
Attached deposit from an external (non-relayer) caller is permanently lost on early `rlp_execute` validation errors - ([File: runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs])

### Summary
`WalletContract::rlp_execute` is a `#[payable]` method, so any `attached_deposit` sent with the call is credited to the wallet's account balance the instant the receipt executes, regardless of what the method later returns. The contract is explicitly designed to *not* panic on error (so relayer-banning state changes persist), and instead returns an `ExecuteResponse{success:false, ...}` value. To make external callers whole in failure cases, the code introduces `CallerDeposit` bookkeeping that is supposed to trigger an explicit refund `Promise`. However, that refund is only wired up for the *asynchronous* failure path (`rlp_execute_callback` on `PromiseResult::Failed`) — it is never issued for the *synchronous* validation-error path taken before any promise is created, so the attached deposit from an ordinary unprivileged caller is silently kept by the contract forever.

### Finding Description
`inner_rlp_execute` computes `caller_deposit` immediately from the attached deposit: [1](#0-0) 

`CallerDeposit::new` only tracks (and is thus only refundable for) an *external* caller, i.e. when `predecessor_account_id != current_account_id`: [2](#0-1) 

If RLP parsing / transaction validation (`internal::parse_rlp_tx_to_action`) fails with `Error::User(_)` or with any of `Error::AccountId(_)` / `Error::Relayer(_)` (when the caller is not itself using an access key that must be banned), `inner_rlp_execute` returns early with `Err(err)` *before* any `Promise` — and therefore before `caller_deposit` is ever used: [3](#0-2) 

Back in the public entry point, this `Err(e)` is converted straight into a plain returned value with no refund promise attached: [4](#0-3) 

By contrast, the only place `caller_deposit` is actually spent to issue a refund is deep in the async callback, on cross-contract-call failure: [5](#0-4) 

This is structurally the same bug class as the CPortModule report: a payment-accepting entry point (`_executeOrderBuySide` / `rlp_execute`) has a validation/refund path where the value meant to guard against fund loss (`msgValueItemPrice` / the `CallerDeposit` refund promise) gets computed but is discarded on a code path that returns "success" at the API level (no revert / no panic) instead of enforcing the refund, so `msg.value` / `attached_deposit` is retained by the contract instead of being returned to the unprivileged caller.

### Impact Explanation
Any ordinary (non-relayer, non-access-key) NEAR account can call `rlp_execute` directly with an attached deposit (this is an explicitly supported flow, exercised by `test_caller_refunds`). If the RLP-encoded Ethereum transaction fails synchronous validation for any reason (malformed base64, bad signature, unsupported action type, oversized value, unknown selector, bad ABI encoding, wrong chain id, insufficient gas, wrong nonce, etc. — all the `UserError`/`RelayerError`/`AccountIdError` variants), the attached NEAR tokens are absorbed into the wallet contract's balance with no refund issued and no revert, i.e. permanent loss of the caller's funds. This matches the "concrete theft or permanent freezing of funds" bar: the funds are not stolen by an attacker but are irrecoverably lost to the ordinary user due to a code-path where a refund that should occur does not.

### Likelihood Explanation
The eth-implicit wallet contract is explicitly documented and tested to support calls "from an external caller" (not just relayers with access keys) attaching a deposit — see `test_caller_refunds`. Triggering a validation error is trivial: any external caller can construct/attach a transaction that fails RLP parsing, signature checks, or any of the numerous `UserError`/`RelayerError` conditions while still attaching NEAR tokens; no special privileges or state are required, only an ordinary transaction to the wallet contract's `rlp_execute` method.

### Recommendation
In `inner_rlp_execute`, on every early-return `Err` path (not only the `Failed` promise-result path), check whether `caller_deposit` is `Some` and, if so, issue a `Promise` transferring the deposit back to `caller_deposit.account_id` before returning the error — or alternatively make `rlp_execute` refund `attached_deposit` to `predecessor_account_id` for any external caller whenever the function is about to return a `success: false` `ExecuteResponse` without having spawned a promise that already accounts for it.

### Proof of Concept
1. An external account `alice.near` (not a registered relayer, i.e. `predecessor_account_id != current_account_id`) calls `rlp_execute(target, tx_bytes_b64)` on an eth-implicit wallet contract account, attaching `3 NEAR`.
2. `tx_bytes_b64` decodes to an `EthTransactionKind` whose RLP payload fails validation in `parse_rlp_tx_to_action` — e.g. it uses an unsupported action selector, producing `Error::User(UserError::UnknownFunctionSelector)`.
3. `inner_rlp_execute` returns `Err(Error::User(...))` at [6](#0-5)  without touching `caller_deposit`.
4. `rlp_execute` matches `Err(e) => PromiseOrValue::Value(e.into())` at [7](#0-6) , returning `ExecuteResponse{success:false, ...}` with no NEAR ever transferred back to `alice.near`.
5. The 3 NEAR attached in step 1 remains permanently in the wallet contract's balance; `alice.near`'s balance is reduced by 3 NEAR (plus gas) with no compensating refund receipt, unlike the `test_caller_refunds` scenario which only exercises the `PromiseResult::Failed` refund path, not this early-return path.

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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L340-345)
```rust
    let context = ExecutionContext::new(
        current_account_id.clone(),
        predecessor_account_id,
        env::attached_deposit(),
    )?;
    let caller_deposit = CallerDeposit::new(&context);
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L386-409)
```rust

            (action, transaction_kind)
        }
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/types.rs (L180-192)
```rust
impl CallerDeposit {
    pub fn new(context: &ExecutionContext) -> Option<Self> {
        // Only track for external (non-self) callers
        if context.current_account_id == context.predecessor_account_id {
            return None;
        }

        NonZeroU128::new(context.attached_deposit.as_yoctonear()).map(|yocto_near| Self {
            account_id: context.predecessor_account_id.clone(),
            yocto_near,
        })
    }
}
```
