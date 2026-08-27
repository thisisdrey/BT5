### Title
`rlp_execute` on the eth-implicit Wallet Contract permanently freezes the caller's attached deposit when the call fails synchronously - ([File: runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs])

### Summary
`WalletContract::rlp_execute` is a `#[payable]` entry point that accepts an attached NEAR deposit alongside an RLP-encoded Ethereum transaction. When the call fails before a cross-contract promise is created (e.g. another transaction is already in flight, or `inner_rlp_execute` returns a parsing/relayer/account-id error), the method returns `PromiseOrValue::Value(...)` synchronously with no promise and, critically, no transfer back to the caller. The attached deposit is silently absorbed into the wallet contract's balance and permanently lost to the caller, mirroring the Derby `pushVaultAmounts`/`sendFundsToVault` pattern where attached native funds are frozen whenever the "used" branch is skipped.

### Finding Description
`rlp_execute` is marked `#[payable]`, meaning any attached deposit is unconditionally credited to the contract's account balance the moment the receipt is applied, before the method body runs [1](#0-0) .

There are two distinct failure paths that consume the deposit without ever creating a refund:

1. **In-flight-transaction short-circuit**: if `self.has_in_flight_tx` is `true`, the function returns immediately with an error response, without even inspecting `env::attached_deposit()`: [2](#0-1) 

2. **Synchronous errors from `inner_rlp_execute`**: parsing failures (`Relayer`/`User`/`AccountId` errors) and the ordinary relayer-error path (when the signer is not the contract itself) return `Err(e)`, which is turned directly into a value response with no promise created and consequently no transfer scheduled: [3](#0-2) 

Inside `inner_rlp_execute`, a `CallerDeposit` is constructed to track the caller's attached deposit for refunding *later*, but this value is only ever consumed by the success path, where it is threaded through as an argument to `address_check_callback`, `nep_141_storage_balance_callback`, or `rlp_execute_callback` so that a refund can be issued if the eventual cross-contract call fails [4](#0-3) . When `inner_rlp_execute` itself returns `Err` before building any promise (i.e., before `caller_deposit` is passed anywhere), the tracked deposit information is simply dropped along with the `Result`, and the attached NEAR is never returned — matching the reported bug class of "native funds attached to a call are frozen when the funds-consuming branch isn't taken."

By contrast, the contract does correctly refund the deposit when a scheduled cross-contract call later fails, via `rlp_execute_callback`, which explicitly creates a transfer promise back to `account_id` using the tracked `CallerDeposit`: [5](#0-4) . This proves the intended design is "always refund unused caller deposits on failure," but the synchronous failure paths in `rlp_execute` bypass that mechanism entirely.

### Impact Explanation
Any external, unprivileged account can call `rlp_execute` with an attached NEAR deposit. If:
- another transaction is currently in flight on that wallet contract (a race any two concurrent callers can trigger deterministically by both calling `rlp_execute` in quick succession), or
- the relayer/caller supplies a malformed, incorrectly targeted, or otherwise rejected RLP transaction (any of the `Relayer`/`User`/`AccountId` error variants),

then the attached deposit is credited to the wallet contract's balance and never returned. Because the wallet contract has no withdrawal or sweep mechanism for this class of stuck funds, the deposit is permanently and irrecoverably lost to the depositor. This is a genuine, concrete freezing/loss of user funds triggered purely by ordinary (even accidental) client behavior — no privileged access or malicious node/validator behavior is required.

### Likelihood Explanation
Likelihood is high: an ordinary user or relayer attaching a deposit to `rlp_execute` while another transaction is in flight, or submitting a transaction that fails any of the numerous `Relayer`/`User`/`AccountId` validations, is an entirely realistic and easily reproducible scenario in normal operation (e.g., two legitimate relayers racing, a stale nonce, wrong chain ID, or any malformed but plausible transaction). No adversarial coordination or special privileges are needed.

### Recommendation
In every early-return/error path of `rlp_execute` (the `has_in_flight_tx` check and each `Err(e)` arm from `inner_rlp_execute`, including the `Relayer` ban-relayer branch), check `env::attached_deposit()` and, if non-zero, schedule a transfer back to `env::predecessor_account_id()` before returning the error `ExecuteResponse`, consistent with how `rlp_execute_callback` already refunds `CallerDeposit` on downstream failures.

### Proof of Concept
1. Deploy an eth-implicit `WalletContract` and fund/derive its address normally.
2. Account `A` calls `rlp_execute(target, tx_bytes_b64_1)` attaching gas sufficient to keep the promise pending (e.g., an emulated ERC-20 transfer that goes through the multi-step callback chain), setting `has_in_flight_tx = true`.
3. Before that call resolves, account `B` calls `rlp_execute(target, tx_bytes_b64_2)` attaching `N` yoctoNEAR.
4. Because `has_in_flight_tx` is still `true`, the contract immediately returns `ExecuteResponse{success:false, error:"transaction already in progress..."}` per [6](#0-5)  — `B`'s `N` yoctoNEAR deposit is now part of the contract's balance and no promise or transfer is created to return it.
5. Verify: `B`'s account balance decreased by `N` (plus gas), and the wallet contract's balance increased by `N`, with no subsequent receipt crediting `B` back. This directly reproduces the "funds attached but not used remain frozen" pattern described in the Derby M-2 report, analogously confirmed by the existing test `test_caller_refunds` in [7](#0-6) , which only exercises the *asynchronous* failure refund path (`rlp_execute_callback`) and does not cover the synchronous early-return paths shown above.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L88-105)
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
```

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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L330-410)
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

    let parsing_result = internal::parse_rlp_tx_to_action(&tx_bytes_b64, &target, &context, *nonce);
    let (action, transaction_kind) = match parsing_result {
        Ok((action, transaction_kind)) => {
            // Increment nonce for all cases where the registrar contract is not needed
            // to prevent replay of those transactions. For transactions that go through
            // the registrar we still do not know if the transaction has a relayer error
            // or not, therefore we must delay incrementing the nonce.
            //
            // Note: relayers with access keys cannot use this delay to needlessly spend
            // the users tokens because only one transaction is allowed to be in-flight
            // at a time.
            if let TransactionKind::EthEmulation(EthEmulationKind::EOABaseTokenTransfer {
                address_check: Some(_),
                ..
            }) = &transaction_kind
            {
            } else {
                *nonce = nonce.saturating_add(1);
            }

            // If the action is an emulated base token or ERC-20 transfer with a non-zero fee then
            // create a promise to send the refund to the relayer. This allows any relayer
            // to safely serve base token transfers from any wallet without additional
            // on-boarding because the relayer will receive some compensation for sending
            // the transaction. Users should always verify the fee before signing a base token
            // transfer. Relayers should also verify the fee before sending to make sure the
            // user's signed transaction will refund enough to cover the relayer's gas costs.
            if let TransactionKind::EthEmulation(EthEmulationKind::EOABaseTokenTransfer {
                fee,
                ..
            })
            | TransactionKind::EthEmulation(EthEmulationKind::ERC20Transfer { fee, .. }) =
                &transaction_kind
            {
                if !fee.is_zero() && context.predecessor_account_id != context.current_account_id {
                    let refund_promise = env::promise_batch_create(&context.predecessor_account_id);
                    env::promise_batch_action_transfer(refund_promise, *fee);
                }
            }

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
    };
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
