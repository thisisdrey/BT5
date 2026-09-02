### Title
`NativeWithdraw` permanently loses user funds on transfer failure with no refund path - ([File: contracts/defuse/src/contract/tokens/nep141/native.rs])

### Summary
`NativeWithdraw` debits the signer's internal wNEAR balance and unwraps it to raw NEAR before attempting a native `Promise::transfer()` to `receiver_id`. Unlike every other withdrawal path in the same contract (`FtWithdraw`, `MtWithdraw`, `NftWithdraw`), this path has no resolver callback that checks whether the final transfer succeeded and refunds the signer if it did not. If the final `transfer()` action fails (e.g. `receiver_id` does not exist, or the account was deleted between signing and execution), the debited value is neither delivered to the receiver nor refunded to the signer — the value is conserved on-chain (it returns to the contract account via a refund receipt) but is permanently lost from the intents ledger, with no way to replay or reclaim it.

### Finding Description
The `NativeWithdraw` intent flow is:

1. `Contract::native_withdraw` in [1](#0-0)  first calls `self.withdraw(...)` to debit the signer's internal wNEAR balance, then chains `ext_wnear::near_withdraw()` (unwrap wNEAR → raw NEAR held by the contract) into `Self::do_native_withdraw(withdraw)`.
2. `do_native_withdraw` in [2](#0-1)  only checks that `near_withdraw` succeeded, then issues `Promise::new(withdraw.receiver_id).transfer(withdraw.amount)` with **no subsequent resolver** to check the outcome of this transfer.

This is explicitly different from `FtWithdraw`/`MtWithdraw`, both of which schedule a `ft_resolve_withdraw` / `mt_resolve_withdraw` callback that inspects the promise result and re-`deposit()`s (refunds) any amount that failed to reach the receiver — see [3](#0-2)  and [4](#0-3) .

The intent's own doc comment acknowledges the risk but the code has no mitigation: [5](#0-4) .

This breaks the "value debited versus value delivered plus refunded" custody binding: after the sequence completes, `debited(signer) != delivered(receiver) + refunded(signer)` whenever the final NEAR transfer fails — exactly the same class of bug as the reported OptimismPortal issue, where an intermediate step succeeds (unlocking/unwrapping value) but the final delivery step can fail without reverting or restoring the balance that was already consumed.

### Impact Explanation
Any account that specifies a `receiver_id` that cannot successfully receive a native NEAR transfer at execution time (non-existent account, deleted account, or any other reason the runtime rejects the `Transfer` action) will have their wNEAR balance permanently zeroed with no compensating credit and no way to replay the withdrawal. This is a "funds permanently frozen" outcome as permitted by scope, arising from a genuine conservation-boundary violation between debit and delivery/refund.

### Likelihood Explanation
This does not require a malicious actor with special privileges — any signer who submits a `NativeWithdraw` intent to a `receiver_id` that later fails to accept a native transfer (e.g. the account was deleted, or never existed and cannot be resolved by the runtime at the time the deferred cross-contract promise executes) triggers the loss deterministically. Because intents are asynchronous (unwrap → transfer happen in separate scheduled promises), the receiver's existence/validity at signing time does not guarantee it still holds at execution time, making this reachable without any privileged role.

### Recommendation
Add a resolver callback to `do_native_withdraw` (mirroring `ft_resolve_withdraw`/`mt_resolve_withdraw`) that inspects the result of the `Promise::transfer()` and re-deposits (refunds) the wNEAR-equivalent amount back to the signer's internal balance if the transfer failed, preserving the debited-equals-delivered-plus-refunded invariant.

### Proof of Concept
1. User A signs a `NativeWithdraw { receiver_id: "some-account-that-will-be-deleted.near", amount }`.
2. Before the transaction lands, `some-account-that-will-be-deleted.near` is deleted (or was never created and its format prevents runtime auto-creation).
3. `native_withdraw` debits A's wNEAR balance and schedules `near_withdraw` → `do_native_withdraw`.
4. `near_withdraw` succeeds (wNEAR unwrapped to raw NEAR held by the intents contract).
5. `Promise::new(receiver_id).transfer(amount)` fails because the account doesn't exist; the NEAR is returned to the intents contract via a refund receipt.
6. No resolver exists to detect this failure and re-credit A; A's internal balance remains zero and the withdrawn value is unrecoverable through the protocol.

### Citations

**File:** contracts/defuse/src/contract/intents/state.rs (L212-240)
```rust
    fn native_withdraw(&mut self, owner_id: &AccountIdRef, withdraw: NativeWithdraw) -> Result<()> {
        self.withdraw(
            owner_id,
            [(
                Nep141TokenId::new(self.wnear_id().into_owned()).into(),
                withdraw.amount.as_yoctonear(),
            )],
            Some("withdraw"),
            false,
        )?;

        ext_wnear::ext(self.wnear_id.clone())
            .with_attached_deposit(NearToken::from_yoctonear(1))
            .with_static_gas(NEAR_WITHDRAW_GAS)
            // do not distribute remaining gas here
            .with_unused_gas_weight(0)
            .near_withdraw(U128(withdraw.amount.as_yoctonear()))
            .then(
                // do_native_withdraw only after unwrapping NEAR
                Self::ext(env::current_account_id())
                    .with_static_gas(Self::DO_NATIVE_WITHDRAW_GAS)
                    // do not distribute remaining gas here
                    .with_unused_gas_weight(0)
                    .do_native_withdraw(withdraw),
            )
            .detach();

        Ok(())
    }
```

**File:** contracts/defuse/src/contract/tokens/nep141/native.rs (L7-19)
```rust
#[near]
impl Contract {
    pub(crate) const DO_NATIVE_WITHDRAW_GAS: Gas = Gas::from_tgas(12);

    #[private]
    pub fn do_native_withdraw(withdraw: NativeWithdraw) -> Promise {
        require!(
            promise_result_checked_void(0).is_ok(),
            "near_withdraw failed",
        );

        Promise::new(withdraw.receiver_id).transfer(withdraw.amount)
    }
```

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L154-194)
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
```

**File:** contracts/defuse/src/contract/tokens/nep245/withdraw.rs (L200-256)
```rust
#[near]
impl MultiTokenWithdrawResolver for Contract {
    #[private]
    fn mt_resolve_withdraw(
        &mut self,
        token: AccountId,
        sender_id: AccountId,
        token_ids: Vec<defuse_nep245::TokenId>,
        amounts: Vec<U128>,
        is_call: bool,
    ) -> Vec<U128> {
        require!(
            token_ids.len() == amounts.len() && !amounts.is_empty(),
            "invalid args"
        );

        let mut used = if is_call {
            // `mt_batch_transfer_call` returns successfully transferred amounts
            match promise_result_checked_json_with_len::<Vec<U128>>(0, amounts.len()) {
                Ok(Ok(used)) if used.len() == amounts.len() => used,
                Ok(_) => vec![U128(0); amounts.len()],
                // do not refund on failed `mt_batch_transfer_call` due to
                // NEP-141 vulnerability: `mt_resolve_transfer` fails to
                // read result of `mt_on_transfer` due to insufficient gas
                Err(_) => amounts.clone(),
            }
        } else {
            // `mt_batch_transfer` returns empty result on success
            if promise_result_checked_void(0).is_ok() {
                amounts.clone()
            } else {
                vec![U128(0); amounts.len()]
            }
        };

        self.deposit(
            sender_id,
            token_ids
                .into_iter()
                .zip(amounts)
                .zip(&mut used)
                .filter_map(|((token_id, amount), used)| {
                    // update min during iteration
                    used.0 = used.0.min(amount.0);
                    let refund = amount.0.saturating_sub(used.0);
                    if refund > 0 {
                        Some((Nep245TokenId::new(token.clone(), token_id).into(), refund))
                    } else {
                        None
                    }
                }),
            Some(REFUND_MEMO),
        )
        .unwrap_or_else(|err| err.panic());

        used
    }
```

**File:** contracts/defuse/core/src/intents/tokens.rs (L426-435)
```rust
#[cfg_attr(feature = "schemars-v0_8", derive(::schemars::JsonSchema))]
#[derive(Debug, Clone, Serialize, Deserialize)]
/// Withdraw native tokens (NEAR) from the intents contract to a given external account id (external being outside of intents).
/// This will subtract from the account's wNEAR balance, and will be sent to the account specified as native NEAR.
/// NOTE: the `wNEAR` will not be refunded in case of fail (e.g. `receiver_id`
/// account does not exist).
pub struct NativeWithdraw {
    pub receiver_id: AccountId,
    pub amount: NearToken,
}
```
