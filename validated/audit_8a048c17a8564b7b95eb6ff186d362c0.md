## Analysis

The comment in `types.rs` confirms the design intent explicitly: *"The reason that method should never panic is to ensure the contract's state can be changed even in error cases... `rlp_execute` is not meant to panic, therefore success/failure must be communicated via the return value."* [1](#0-0) 

This confirms the critical fact: because `rlp_execute` is `#[payable]` and returns a non-panicking `ExecuteResponse` value on failure, the NEAR protocol's automatic deposit-refund mechanism (documented in `docs/RuntimeSpec/Refunds.md`, which only refunds a deposit "when an action receipt fails to execute") never triggers. [2](#0-1)  The attached deposit has already been credited to the wallet contract's account balance by the protocol before the WASM code executes; if the code returns success (a non-panicking `Value`) yet reports a logical failure, **the contract itself is solely responsible for issuing a manual refund**. `CallerDeposit` was created specifically to track this and drive a manual refund via `rlp_execute_callback`. [3](#0-2) 

I traced two code paths in `inner_rlp_execute`/`rlp_execute` where `caller_deposit` is computed but then **dropped without ever being wired into a refund promise**:

1. In `inner_rlp_execute`, `caller_deposit` is computed immediately after building `ExecutionContext`, before `parse_rlp_tx_to_action` runs. [4](#0-3)  If parsing fails (`Err(err @ Error::User(_))` or the other `Err(err)` branch), the function returns `Err(...)` directly without ever using `caller_deposit`. [5](#0-4) 
2. Back in `rlp_execute`, that `Err(e)` (for any case other than `Error::Relayer` when signer==current) simply becomes `PromiseOrValue::Value(e.into())` — a plain non-panicking `ExecuteResponse{success:false,...}`, with **no promise spawned at all**, hence no refund transfer. [6](#0-5) 

Additionally, in `address_check_callback`, when the registrar lookup indicates the target address already corresponds to a named account (`maybe_account_id.is_some()`) and the caller is not self-relaying, the function returns `PromiseOrValue::Value(ExecuteResponse{success:false,...})` directly — again dropping `caller_deposit` on the floor without spawning any refund. [7](#0-6) 

By contrast, the one path that *is* tested and correctly refunds is `rlp_execute_callback`'s `PromiseResult::Failed` arm, which explicitly issues `promise_batch_action_transfer` back to `caller_deposit.account_id`. [8](#0-7)  The existing test `test_caller_refunds` only exercises the case where the cross-contract call itself fails (i.e., a `Promise` was made and failed), not the early-return `Err` paths, so the gap is untested. [9](#0-8) 

This is a structural analog of the Beanstalk bug: an external, unprivileged caller's funds are moved into the contract's custody (attached deposit credited by protocol) but the internal bookkeeping structure (`CallerDeposit`) designed to make those funds recoverable is discarded on certain non-panicking failure paths, permanently trapping the funds in the contract's balance with no user-facing way to reclaim them (there is no generic "withdraw stuck deposit" method in this contract).

### Title
Attached deposit is permanently stuck in the eth-implicit wallet contract when `rlp_execute` fails via a non-panicking early return - (File: `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs`)

### Summary
`WalletContract::rlp_execute` is `#[payable]`, so the protocol credits any attached deposit to the wallet contract's account balance before contract code runs. Because the contract intentionally never panics (to preserve state changes like relayer banning), the protocol's built-in deposit-refund-on-failed-receipt mechanism never fires; refunding is entirely the contract's responsibility, implemented via the `CallerDeposit` tracking struct. Several failure branches drop `CallerDeposit` without ever spawning the refund transfer, permanently stranding the external caller's deposit in the wallet contract's balance.

### Finding Description
`CallerDeposit::new` captures the predecessor's attached deposit right after the execution context is built in `inner_rlp_execute`. [4](#0-3)  This value is only actually used for a refund inside `rlp_execute_callback`'s `PromiseResult::Failed` branch, i.e., only when a `Promise` was created and later fails on-chain. [8](#0-7) 

However, multiple synchronous error paths return before any promise (and therefore before any refund) is created:
- `parse_rlp_tx_to_action` errors (malformed RLP, unsupported action, etc.) cause `inner_rlp_execute` to `return Err(err)` directly, discarding `caller_deposit`. [5](#0-4) 
- `rlp_execute` converts any such `Err(e)` (other than the self-relayer `Error::Relayer` case) into `PromiseOrValue::Value(e.into())`, i.e., a plain successful receipt carrying a failure value, with no refund promise spawned. [6](#0-5) 
- In `address_check_callback`, if the registrar shows the target already maps to a named account and the caller isn't the wallet itself, the function again returns `PromiseOrValue::Value(...)` directly, discarding `caller_deposit` that was threaded through as a parameter. [7](#0-6) 

Since these return paths do not panic, the NEAR protocol treats the receipt as successful and will not generate a deposit-refund receipt (per `docs/RuntimeSpec/Refunds.md`, refunds are only generated "when an action receipt fails to execute"). [2](#0-1)  The deposit that was credited to the wallet contract's balance at receipt-application time is never moved back out, and the `WalletContract` struct exposes no other method to reclaim or sweep such stray balance. [10](#0-9) 

### Impact Explanation
Any external (non-self) caller of `rlp_execute` who attaches a NEAR deposit and whose RLP-encoded transaction fails to parse (malformed data, unsupported action encoding, etc.), or whose target-address-check callback determines the address already maps to a registered account, permanently loses the attached deposit — it becomes stuck in the wallet contract's account balance with no recovery path. This is a permanent freezing/loss of user funds for an ordinary, unprivileged interaction with a deployed contract that ships as part of nearcore's eth-implicit-account tooling.

### Likelihood Explanation
This is trivially reachable by any relayer or user calling `rlp_execute` with a nonzero deposit and malformed/garbage `tx_bytes_b64`, or with an intentionally crafted address-check scenario — no privileged access or race condition is required, only an ordinary payable contract call. The existing regression test suite (`test_caller_refunds`) only covers the case where a spawned promise later fails, not these earlier synchronous-error branches, so the gap is currently unguarded by tests.

### Recommendation
Thread `caller_deposit` through every failure return path in `rlp_execute`/`inner_rlp_execute`/`address_check_callback` and issue the same `promise_batch_action_transfer` refund used in `rlp_execute_callback` whenever the method resolves to a failure `ExecuteResponse` without having spawned a cross-contract promise that itself carries the refund obligation. Alternatively, restructure so that any early, purely-local validation failure occurs before the deposit is retained (not generally possible given `#[payable]` semantics), or add a fallback method allowing the original depositor to reclaim a matching stuck balance recorded in contract state.

### Proof of Concept
1. Deploy `WalletContract` to an eth-implicit account as in the existing test harness (`TestContext::new`). [11](#0-10) 
2. As an external account (`predecessor_account_id != current_account_id`), call `rlp_execute` with a nonzero attached deposit and a `tx_bytes_b64` value that is not valid RLP (e.g., `b"random_garbage_data"` as used in `test_relayer_invalid_tx_data`, but from a non-relayer caller with a deposit attached). [12](#0-11) 
3. Observe: `ExecuteResponse.success == false`, but the caller's account balance decreases by the full deposit amount and the wallet contract's balance increases by that amount, with no subsequent transfer back to the caller — unlike `test_caller_refunds`, where a failure that manifests via a spawned promise (`PromiseResult::Failed`) does trigger a refund. [13](#0-12)

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/types.rs (L49-59)
```rust
/// Response given from the `rlp_execute` entry point to the contract.
/// The error information is needed because that method is not meant to panic,
/// therefore success/failure must be communicated via the return value.
/// The reason that method should never panic is to ensure the contract's state
/// can be changed even in error cases. For example, banning a dishonest relayer.
#[derive(Debug, PartialEq, Eq, Clone, serde::Serialize, serde::Deserialize)]
pub struct ExecuteResponse {
    pub success: bool,
    pub success_value: Option<Vec<u8>>,
    pub error: Option<String>,
}
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/types.rs (L172-192)
```rust
/// A data type to keep track of the deposit given by an external caller.
/// This allows us to refund the caller's deposit if the cross-contract call fails.
#[derive(Debug, PartialEq, Eq, Clone, serde::Serialize, serde::Deserialize)]
pub struct CallerDeposit {
    pub account_id: AccountId,
    pub yocto_near: NonZeroU128,
}

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

**File:** docs/RuntimeSpec/Refunds.md (L15-18)
```markdown
## Deposit Refunds

Deposit refunds are generated when an action receipt fails to execute. All attached deposit amounts are summed together and
sent as a refund to a `predecessor_id` (because only the predecessor can attach deposits).
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L43-55)
```rust
#[near_bindgen]
#[derive(Default, BorshDeserialize, BorshSerialize)]
#[borsh(crate = "near_sdk::borsh")]
pub struct WalletContract {
    pub nonce: u64,
    /// Tracks whether a transaction is currently being executed
    /// (i.e. has receipts that have not yet resolved).
    /// Invariant: `has_in_flight_tx` must be `true` when a mutable method
    /// of this contract returns a promise and `false` otherwise (except
    /// for the check if a transaction is already in flight at the beginning
    /// of `rlp_execute`).
    pub has_in_flight_tx: bool,
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L160-173)
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L340-345)
```rust
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/tests/sanity.rs (L170-229)
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

    // External caller does not get a refund when their tokens are spent
    let pre_tx_account_balance = post_tx_account_balance;
    let receiver_id = address_registrar.id();
    let result = wallet_contract
        .rlp_execute_from(&caller, receiver_id.as_str(), &create_tx(receiver_id, 1), deposit_amount)
        .await?;
    assert!(result.success);
    let post_tx_account_balance = caller.view_account().await?.balance;
    assert!(
        pre_tx_account_balance.as_yoctonear() - post_tx_account_balance.as_yoctonear()
            >= deposit_amount.as_yoctonear()
    );

    Ok(())
}
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/tests/relayer.rs (L96-105)
```rust
    let inputs: [&[u8]; 2] = [b"random_garbage_data", &[]];
    let relayer_keys = {
        // Need to generate all the relayer keys first because they are
        // going to get banned as we run the different inputs in the later loop.
        let mut tmp = Vec::new();
        for _ in 0..(inputs.len()) {
            tmp.push(new_relayer(&worker, &mut wallet_contract).await?);
        }
        tmp
    };
```
