### Title
Attached `CallerDeposit` is never refunded on early/parse-time or ambiguous-target failures in `rlp_execute` - (File: `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs`)

### Summary
The eth-implicit wallet contract's `rlp_execute` entry point is `#[payable]` and tracks an external caller's attached deposit via `CallerDeposit` so it can be refunded if the eventual cross-contract call fails. However, several early failure paths return an error `ExecuteResponse` (or a value, not a `Promise`) without ever constructing a refund `Transfer` promise, permanently stranding the caller's attached NEAR in the wallet contract's balance — the same class of bug as the reported Blueberry issue, where funds handed over for an operation are not sent back when that operation aborts.

### Finding Description
`WalletContract::rlp_execute` is a payable method; any deposit attached by the predecessor is immediately credited to the wallet contract's account balance by the NEAR runtime, independent of what the contract logic subsequently does [1](#0-0) . Inside `inner_rlp_execute`, a `CallerDeposit` is captured specifically to refund external (non-self) callers if the operation ultimately fails [2](#0-1) [3](#0-2) .

The refund is only actually issued in one place: `rlp_execute_callback`'s `PromiseResult::Failed` branch, which creates a transfer promise back to `caller_deposit.account_id` [4](#0-3) .

But `caller_deposit` is silently dropped (never refunded) in at least these two reachable failure paths:

1. **RLP parse / transaction-decoding errors.** If `internal::parse_rlp_tx_to_action` fails with `Error::User(_)` or any other `Error`, `inner_rlp_execute` returns `Err(err)` immediately, discarding the already-computed `caller_deposit` without creating any transfer [5](#0-4) . Back in `rlp_execute`, this `Err` becomes a plain `ExecuteResponse` value with no promise at all — the attached deposit is never returned [6](#0-5) .

2. **Ambiguous `target` address (target resolves to an existing named account) sent by a non-owner relayer.** In `address_check_callback`, when the registrar lookup shows the address corresponds to an existing named account and the call was not made using the wallet's own access key (`env::signer_account_id() != current_account_id`), the function returns an error `ExecuteResponse` directly, again without refunding `caller_deposit`, which is a parameter of this very callback [7](#0-6) .

In both cases the deposit that was attached specifically to fund this operation (and is explicitly modeled as refundable via `CallerDeposit`) is retained forever by the wallet contract, exactly mirroring the reported pattern: an asset intentionally set aside for return to the caller is dropped on certain failure branches instead of being sent back.

### Impact Explanation
Any external account (a relayer or any third party) that calls `rlp_execute` with an attached NEAR deposit intended to cover fees/compensation, and whose transaction happens to fail RLP parsing/validation, or targets an address that coincidentally resolves to an existing named account while using its own (non-owner) signer key, permanently loses that deposit into the wallet contract's balance with no built-in recovery path. This is a concrete, permanent loss/freezing of user funds reachable directly from an ordinary (non-privileged) account interacting with a deployed eth-implicit wallet contract — no malicious node, validator, or privileged actor is required.

### Likelihood Explanation
`rlp_execute` is the sole public entry point of the eth-implicit wallet contract and is expected to be called routinely by relayers submitting Ethereum-style transactions on behalf of users, frequently with an attached deposit to compensate the relayer/cover fees. Malformed RLP transactions (bad signatures, unsupported actions, etc.) and address-registrar collisions are realistic occurrences in normal relayer operation, not a contrived edge case, so this is straightforward to trigger unintentionally, and trivial to trigger intentionally by any relayer/caller who wants to test or exploit the refund gap.

### Recommendation
Ensure every failure/return path in `rlp_execute`/`inner_rlp_execute`/`address_check_callback` that has a non-empty `caller_deposit` issues a `Transfer` promise back to `caller_deposit.account_id` before/while returning the error response, mirroring the pattern already used in `rlp_execute_callback`'s failure branch. Concretely:
- In the `Err(err @ Error::User(_))` and `Err(err)` branches of `inner_rlp_execute`'s parsing match, propagate `caller_deposit` alongside the error so `rlp_execute` can issue a refund transfer (or build a small helper promise) before returning `PromiseOrValue::Value`.
- In `address_check_callback`'s branch where `maybe_account_id.is_some()` and `signer_account_id() != current_account_id`, refund `caller_deposit` before returning the error `ExecuteResponse`, exactly as done in `rlp_execute_callback`.

### Proof of Concept
1. Deploy the eth-implicit wallet contract as in `runtime/near-wallet-contract/implementation/wallet-contract/src/tests/sanity.rs`'s `test_caller_refunds` setup [8](#0-7) .
2. Have an external account (not the wallet owner) call `rlp_execute` attaching a deposit (e.g. 3 NEAR) with an RLP-encoded transaction that fails to parse into a supported `near_action::Action` (e.g., malformed signature or unsupported action type), so that `internal::parse_rlp_tx_to_action` returns `Err(Error::User(_))`.
3. Observe `rlp_execute` returns `PromiseOrValue::Value(ExecuteResponse{success:false,...})` directly (no promise), and check the caller's account balance before/after: the full attached deposit is gone from the caller's balance and now permanently resides in the wallet contract balance, unlike the existing `test_caller_refunds` scenario (target `"fake.near"`) where the deposit *is* returned because that failure occurs after a promise is created and routed through `rlp_execute_callback`'s `Failed` branch [9](#0-8) .
4. Similarly, construct a transaction whose `target` resolves (via the address registrar) to an existing named account, and send it from a relayer account using its own key (not the wallet's key) to trigger the `address_check_callback` branch at lines 165-173; verify the deposit attached to the original `rlp_execute` call is likewise never refunded.

### Citations

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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L160-192)
```rust
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L330-345)
```rust
fn inner_rlp_execute(
    current_account_id: AccountId,
    predecessor_account_id: AccountId,
    target: AccountId,
    tx_bytes_b64: String,
    nonce: &mut u64,
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L388-409)
```rust
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/types.rs (L180-191)
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
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/tests/sanity.rs (L170-196)
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

```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/tests/sanity.rs (L197-213)
```rust
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
