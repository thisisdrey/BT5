## Finding: Attached deposit gets permanently stuck in the eth-implicit `WalletContract` when the address-registrar callback rejects a faulty-relayer target

I found a direct nearcore analog of the reported "tokens can get stuck on deposit" bug class in the `near-wallet-contract` (the eth-implicit account wallet contract), which is explicitly in scope.

### Title
Attached deposit not refunded on `address_check_callback` "Invalid target" error path - ([File: runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs])

### Summary
`WalletContract::rlp_execute` immediately deposits any attached NEAR into the wallet contract's own balance, as is standard NEAR semantics [1](#0-0) . To make sure a caller who attaches a deposit gets it back if the eventual cross-contract call fails, the contract tracks it in a `CallerDeposit` struct and threads it through every promise chain, refunding it in the failure branch of `rlp_execute_callback` [2](#0-1) . However, one specific error branch inside `address_check_callback` returns an error `ExecuteResponse` directly, without ever creating the refund transfer promise, silently dropping the `caller_deposit`.

### Finding Description
`CallerDeposit::new` captures the caller's `attached_deposit` whenever an external (non-self) caller invokes `rlp_execute` [3](#0-2) . This value is passed into `address_check_callback` for the `EOABaseTokenTransfer` case that requires an address-registrar lookup [4](#0-3) .

Inside `address_check_callback`, if the registrar lookup shows that the `target` actually corresponds to an existing named account (i.e. the relayer supplied a wrong/faulty target), and the transaction signer is *not* the wallet account itself (the common case — an external relayer signing with its own key), the function returns immediately with an "Invalid target" error, dropping `caller_deposit` entirely: [5](#0-4) 

Compare this to every other error path in the contract, all of which correctly forward `caller_deposit` to `rlp_execute_callback` for a refund on failure — e.g. `nep_141_storage_balance_callback`'s branches [6](#0-5)  and the generic fallback path in `inner_rlp_execute` [7](#0-6) . Only the branch at lines 168-173 fails to do this.

Because `rlp_execute` returns `PromiseOrValue::Value(...)` here (a normal, successful function-call return, not a panic), the protocol's automatic deposit-refund mechanism does **not** trigger — that mechanism (`refund_unspent_gas_and_deposits`) only fires when the whole receipt execution fails [8](#0-7) . Since the wallet contract's own logic completes "successfully" (just returning an error value), the deposit that was already credited to the wallet account's balance on receipt entry stays there permanently with no contract-level mechanism ever created to move it back to the caller.

### Impact Explanation
Any external, non-owner caller (e.g. someone paying the wallet a fee/deposit while calling `rlp_execute` directly, as demonstrated by the `test_caller_refunds` test pattern [9](#0-8) ) permanently loses their attached deposit if: (1) the transaction is an `EOABaseTokenTransfer` requiring an address-registrar check, (2) the registrar unexpectedly resolves the `target` to an existing named account (faulty/malicious relayer supplied target), and (3) the signer is not the wallet account itself. This is a concrete permanent freezing/loss of user funds inside a production NEAR contract, matching the "Medium" severity/loss pattern of the original report.

### Likelihood Explanation
This path is reachable by any unprivileged external account attaching a deposit and calling `rlp_execute` with an `EOABaseTokenTransfer` whose `to` address matches a registered account — it requires either a malicious/faulty relayer supplying a wrong `target`, or a race where the target account gets registered between transaction construction and execution. It does not require any special privileges, only a normal deposit-attached function call.

### Recommendation
In the `maybe_account_id.is_some()` / signer-mismatch branch of `address_check_callback`, forward `caller_deposit` and issue the refund transfer (mirroring the logic already present in `rlp_execute_callback`'s failure arm) before returning the error `ExecuteResponse`, instead of silently dropping it.

### Proof of Concept
1. An external account `Alice` (not the wallet owner) calls `wallet_contract.rlp_execute(target, tx_bytes_b64)` with `deposit > 0`, where the underlying signed Ethereum tx is an `EOABaseTokenTransfer` whose `to` address requires an address-registrar lookup (`address_check: Some(address)`), as in `inner_rlp_execute` [4](#0-3) .
2. The registrar lookup resolves `address` to `Some(existing_account_id)` (a faulty/malicious relayer supplied an incorrect `target` account, or the account was registered in the interim).
3. `env::signer_account_id() != current_account_id` (Alice signed with her own key/access key, the normal relayer case), so `address_check_callback` hits the branch at lines 168-173 and returns the "Invalid target" `ExecuteResponse` directly.
4. `caller_deposit` (Alice's `deposit`) is never used to create a refund promise; the deposit remains permanently as part of the wallet contract account's NEAR balance with no way for Alice to reclaim it — analogous to the reported `deposit()`/`depositFor()` stuck-token bug.

### Citations

**File:** docs/RuntimeSpec/Components/BindingsSpec/EconomicsAPI.md (L9-10)
```markdown
- `attached_deposit` -- the balance that was attached to the call that will be immediately deposited before
  the contract execution starts;
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L161-173)
```rust
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L230-268)
```rust
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
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L296-305)
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
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L412-432)
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
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L466-470)
```rust
        _ => {
            let ext =
                WalletContract::ext(current_account_id).with_static_gas(RLP_EXECUTE_CALLBACK_GAS);
            action_to_promise(target, action)?.then(ext.rlp_execute_callback(caller_deposit))
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

**File:** runtime/runtime/src/lib.rs (L1249-1249)
```rust
        let deposit_refund = if result.result.is_err() { total_deposit } else { Balance::ZERO };
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
