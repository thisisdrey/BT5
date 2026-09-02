### Title
No refund/resolver on `storage_deposit` Promise failure permanently burns signer's wNEAR - ([File: contracts/defuse/src/contract/tokens/nep141/storage_deposit.rs])

### Summary
The standalone `StorageDeposit` intent debits the signer's wNEAR balance in the Verifier before any cross-contract call executes, then fires `near_withdraw()` followed by `do_storage_deposit()`, but never attaches a resolver callback to check the outcome of the final `storage_deposit` Promise. If that final Promise fails, the debited wNEAR is neither refunded to the signer nor does any accounting record the credit anywhere, so funds are silently destroyed.

### Finding Description
Binding claimed: `wnear_debited(signer) == near_credited_as_storage(deposit_for_account_id, contract_id)`.

The `storage_deposit` intent handler in `contracts/defuse/src/contract/intents/state.rs:265-297` first calls `self.withdraw(owner_id, [(wnear_token_id, storage_deposit.amount)], ...)`, which immediately debits the signer's internal wNEAR balance in the Verifier's ledger [1](#0-0) . It then schedules:
```
near_withdraw(amount).then(do_storage_deposit(storage_deposit)).detach()
``` [2](#0-1) 

Critically, the whole chain is `.detach()`ed — there is no `.then()` scheduling a resolver method on the Verifier contract to observe whether the final `storage_deposit` cross-contract call succeeded or failed.

`do_storage_deposit` itself, in `contracts/defuse/src/contract/tokens/nep141/storage_deposit.rs:13-26`, only checks the result of the preceding `near_withdraw` Promise via `promise_result_checked_void(0)`:
```rust
#[private]
pub fn do_storage_deposit(storage_deposit: StorageDeposit) -> Promise {
    require!(
        promise_result_checked_void(0).is_ok(),
        "near_withdraw failed",
    );
    ext_storage_management::ext(storage_deposit.contract_id)
        .with_attached_deposit(storage_deposit.amount)
        .with_static_gas(STORAGE_DEPOSIT_GAS)
        .with_unused_gas_weight(0)
        .storage_deposit(Some(storage_deposit.deposit_for_account_id), None)
}
``` [3](#0-2) 

It returns the `storage_deposit` call's Promise directly to the runtime with no `.then()` resolver appended. Consequently, the Verifier contract never inspects whether `contract_id::storage_deposit` actually succeeded, and there is no `#[private]` resolve function analogous to `ft_resolve_withdraw` / `nft_resolve_withdraw` / `mt_resolve_withdraw` (compare `contracts/defuse/src/contract/tokens/nep141/withdraw.rs:154-195`) that could re-credit the signer [4](#0-3) .

Exploit / failure path: any signer submits a `StorageDeposit { contract_id, deposit_for_account_id, amount }` intent where `contract_id` is any account that does not implement NEP-145 `storage_deposit` (e.g., a bare stub contract, or any account without the method), or where the call panics for any reason (insufficient deposit relative to that contract's requirement, account_id validation failure, etc.). The signer's wNEAR is already debited via `self.withdraw` before the Promise chain runs; when the final Promise fails, NEAR simply reverts the attached deposit to the calling contract's own account balance (not to the signer's Verifier balance), and no code path credits it back to the signer's `TokenId` balance in the Verifier. The wNEAR is permanently lost from the signer's perspective — it is not present in the target contract's storage balance for `deposit_for_account_id`, and it is not present back in the signer's Verifier balance.

Existing guards do not prevent this: `require!(promise_result_checked_void(0).is_ok(), ...)` in `do_storage_deposit` only guards against `near_withdraw` failing, not the eventual `storage_deposit` call; `MultiPayload::verify`, nonce checks, and `Lock` state checks are all satisfied normally since the intent itself is validly signed — the divergence is purely about the missing resolver/refund for the terminal Promise.

### Impact Explanation
Matches the Critical category "user funds permanently frozen" (destroyed, in this case): the signer's wNEAR debited from the Verifier is not credited anywhere — neither back to the signer nor as a registered storage balance on `contract_id`. This is repeatable per signer per `StorageDeposit` intent against any misconfigured/non-NEP-145 `contract_id`, and the attacker (an unprivileged signer) can trigger it against their own account intentionally, or accidentally lose funds — but from a security-audit "value leaving custody without matching credit" standpoint, this represents a real accounting break: the Verifier's total custodied wNEAR no longer matches obligations (the signer's balance is reduced with no corresponding liability created anywhere).

### Likelihood Explanation
Trivial precondition: the signer needs a wNEAR balance in the Verifier and simply signs a `StorageDeposit` intent naming any `contract_id` lacking `storage_deposit` (or one that will panic on the call, e.g., insufficient `amount` for that particular contract's minimum). No special roles, no relayer key, no victim key needed — fully within the described unprivileged attacker capabilities. Cost is just the wNEAR amount itself (which is lost) plus gas.

### Recommendation
Attach a private resolver method (e.g., `resolve_storage_deposit`) via `.then()` after `do_storage_deposit`'s `storage_deposit` Promise that inspects `promise_result_checked_void`/`promise_result_checked_json` on the outcome, and on failure credits the `amount` (or unused portion) back to the signer's wNEAR balance in the Verifier, mirroring the pattern used in `ft_resolve_withdraw` / `nft_resolve_withdraw` / `mt_resolve_withdraw`.

### Proof of Concept
```rust
// tests/src/tests/defuse/storage/no_refund_on_failure.rs
#[rstest]
#[tokio::test]
async fn storage_deposit_intent_no_refund_on_storage_deposit_failure(#[future(awt)] env: Env) {
    let user = env.create_user().await;

    // stub contract with NO storage_deposit method implemented
    let stub_contract_id = env.deploy_stub_without_storage_management("stub").await;

    // fund user's wNEAR balance in the Verifier
    user.near_deposit(env.wnear.contract_id(), NearToken::from_near(10)).await.unwrap();
    env.defuse_ft_deposit_to(
        env.wnear.contract_id(),
        NearToken::from_near(1).as_yoctonear(),
        user.account_id(),
        None,
    ).await.unwrap();

    let wnear_before = env
        .defuse_mt_balance_of(user.account_id(), &wnear_token_id(&env))
        .await
        .unwrap();

    let payload = user
        .sign_defuse_payload_default(&env.defuse, [StorageDeposit {
            contract_id: stub_contract_id.clone(),
            deposit_for_account_id: user.account_id().clone(),
            amount: NearToken::from_millinear(125),
        }])
        .await
        .unwrap();

    // execute; the storage_deposit sub-call fails because stub has no such method
    env.defuse_simulate_and_execute_intents(env.defuse.contract_id(), [payload])
        .await
        .unwrap(); // outer intent "succeeds" (schedules async promise), inner promise fails silently

    let wnear_after = env
        .defuse_mt_balance_of(user.account_id(), &wnear_token_id(&env))
        .await
        .unwrap();

    // BROKEN BINDING: wnear_debited(signer) != near_credited_as_storage(...)
    assert_eq!(wnear_after, wnear_before - NearToken::from_millinear(125).as_yoctonear(),
        "signer's wNEAR was debited");

    let storage_balance = stub_contract_id
        .call("storage_balance_of")
        .args_json(json!({ "account_id": user.account_id() }))
        .view()
        .await;
    assert!(storage_balance.is_err() || storage_balance.unwrap().json::<Option<serde_json::Value>>().unwrap().is_none(),
        "no storage balance was ever registered for deposit_for_account_id on contract_id");
}
```

### Citations

**File:** contracts/defuse/src/contract/intents/state.rs (L265-278)
```rust
    fn storage_deposit(
        &mut self,
        owner_id: &AccountIdRef,
        storage_deposit: StorageDeposit,
    ) -> Result<()> {
        self.withdraw(
            owner_id,
            [(
                Nep141TokenId::new(self.wnear_id().into_owned()).into(),
                storage_deposit.amount.as_yoctonear(),
            )],
            Some("withdraw"),
            false,
        )?;
```

**File:** contracts/defuse/src/contract/intents/state.rs (L280-296)
```rust
        ext_wnear::ext(self.wnear_id.clone())
            .with_attached_deposit(NearToken::from_yoctonear(1))
            .with_static_gas(NEAR_WITHDRAW_GAS)
            // do not distribute remaining gas here
            .with_unused_gas_weight(0)
            .near_withdraw(U128(storage_deposit.amount.as_yoctonear()))
            .then(
                // do_storage_deposit only after unwrapping NEAR
                Self::ext(env::current_account_id())
                    .with_static_gas(Self::DO_STORAGE_DEPOSIT_GAS)
                    // do not distribute remaining gas here
                    .with_unused_gas_weight(0)
                    .do_storage_deposit(storage_deposit),
            )
            .detach();

        Ok(())
```

**File:** contracts/defuse/src/contract/tokens/nep141/storage_deposit.rs (L13-26)
```rust
    #[private]
    pub fn do_storage_deposit(storage_deposit: StorageDeposit) -> Promise {
        require!(
            promise_result_checked_void(0).is_ok(),
            "near_withdraw failed",
        );

        ext_storage_management::ext(storage_deposit.contract_id)
            .with_attached_deposit(storage_deposit.amount)
            .with_static_gas(STORAGE_DEPOSIT_GAS)
            // do not distribute remaining gas here
            .with_unused_gas_weight(0)
            .storage_deposit(Some(storage_deposit.deposit_for_account_id), None)
    }
```

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L154-195)
```rust
#[near]
impl FungibleTokenWithdrawResolver for Contract {
    #[private]
    fn ft_resolve_withdraw(
        &mut self,
        token: AccountId,
        sender_id: AccountId,
        amount: U128,
        is_call: bool,
    ) -> U128 {
        let used = if is_call {
            // `ft_transfer_call` returns successfully transferred amount
            match promise_result_checked_json::<U128>(0) {
                Ok(Ok(used)) => used.0.min(amount.0),
                Ok(Err(_deserialize_err)) => 0,
                // do not refund on failed `ft_transfer_call` due to
                // NEP-141 vulnerability: `ft_resolve_transfer` fails to
                // read result of `ft_on_transfer` due to insufficient gas
                Err(_) => amount.0,
            }
        } else {
            // `ft_transfer` returns empty result on success
            if promise_result_checked_void(0).is_ok() {
                amount.0
            } else {
                0
            }
        };

        let refund = amount.0.saturating_sub(used);
        if refund > 0 {
            self.deposit(
                sender_id,
                [(Nep141TokenId::new(token).into(), refund)],
                Some(REFUND_MEMO),
            )
            .unwrap_or_else(|err| err.panic());
        }

        U128(used)
    }
}
```
