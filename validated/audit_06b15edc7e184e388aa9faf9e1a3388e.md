### Title
`native_withdraw`/`storage_deposit`/`auth_call` debit user balances before an unchecked `near_withdraw` cross-contract call, with no refund path on failure - (File: `contracts/defuse/src/contract/intents/state.rs`)

### Summary
`Contract::native_withdraw`, `Contract::storage_deposit`, and `Contract::auth_call` (all in `state.rs`) subtract the user's internal wNEAR ledger balance synchronously, then asynchronously call the external `wnear` contract's `near_withdraw` and chain a callback (`do_native_withdraw` / `do_storage_deposit` / `do_auth_call`) that panics if that call failed. Unlike the sibling `ft_withdraw`/`mt_withdraw` flows, which have an explicit `*_resolve_withdraw` step that reads the promise result and refunds the user on failure, these three flows have no such resolver - the debit is final regardless of whether the unwrap/transfer actually completes.

### Finding Description
`internal_sub_balance` is called synchronously and commits in the same receipt as `native_withdraw`/`storage_deposit`/`auth_call`, before any cross-contract call executes: [1](#0-0) 

The subsequent chain is `near_withdraw(...).then(do_native_withdraw(...)).detach()`: [2](#0-1) 

`do_native_withdraw` only proceeds if the prior promise succeeded; otherwise it panics via `require!`, aborting only its own receipt (the transfer never happens), while the balance debit made in the earlier, already-committed receipt is never reversed: [3](#0-2) 

The identical pattern repeats for `storage_deposit`: [4](#0-3) 

and for `auth_call` with a nonzero `attached_deposit`: [5](#0-4) 

Contrast this with `ft_withdraw`/`mt_withdraw`, where a dedicated resolver (`ft_resolve_withdraw`/`mt_resolve_withdraw`) inspects the promise outcome and explicitly re-deposits (`self.deposit(...)`) any amount that was not actually transferred: [6](#0-5) 

This is the same root-cause pattern as the referenced report: a value-affecting external call's failure is not correctly propagated into a compensating action, so an operation that debits a balance is executed and committed independently of whether the corresponding external effect (the NEAR unwrap/transfer, the storage deposit, or the authorized call) succeeded. The binding broken is: `value debited from ledger == value delivered to receiver + value refunded to owner`. Here, on any `near_withdraw` failure, value is debited but neither delivered nor refunded.

### Impact Explanation
If `wnear.near_withdraw` (or the subsequent `storage_deposit`/`do_auth_call` promise) fails for any reason - e.g., the `wnear` contract is paused, upgraded to add new checks, hits a NEP-141 zero/rounding edge case, or the callback exceeds gas - the caller's internal wNEAR balance has already been irreversibly subtracted with no compensating deposit. This is a direct, permanent loss of the user's funds inside the Defuse ledger, matching the "funds permanently frozen"/critical-impact bucket (value is debited from the signer's balance without a corresponding delivery or refund).

### Likelihood Explanation
Likelihood is low-to-medium: it requires the external `wnear` contract call to fail after the ledger debit has already committed (e.g., `wnear` paused/upgraded, or `near_withdraw` reverting on an edge case such as zero amount or an invariant the wrap contract enforces). This mirrors the referenced report's own assessed likelihood (external dependency behaving unexpectedly), which was still rated at least medium severity because the resulting loss, when it occurs, is total and unrecoverable for that call.

### Recommendation
Add resolver callbacks for `native_withdraw`, `storage_deposit`, and `auth_call` analogous to `ft_resolve_withdraw`/`mt_resolve_withdraw`: inspect the result of the `near_withdraw` (and subsequent) promise, and if it failed, re-`deposit` the withdrawn amount back to the owner's ledger balance instead of unconditionally panicking with no refund path.

### Proof of Concept
1. User has wNEAR balance `X` in the Defuse ledger and submits a `NativeWithdraw` intent for amount `X`.
2. `native_withdraw` executes `internal_sub_balance(owner_id, X)` synchronously; this commits in the current receipt.
3. `ext_wnear::near_withdraw(X)` is dispatched; assume it fails (e.g. `wnear` contract paused, or the call runs out of allocated `NEAR_WITHDRAW_GAS`).
4. `do_native_withdraw` callback's `promise_result_checked_void(0)` returns `Err`, and `require!(...)` panics -> the `Promise::new(receiver_id).transfer(...)` is never scheduled.
5. Net effect: the owner's internal ledger balance is permanently reduced by `X`, no NEAR was ever transferred to `receiver_id`, and no refund deposit occurred, since no resolver exists to perform one - a straightforward violation of `debited == delivered + refunded`.

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

**File:** contracts/defuse/src/contract/intents/state.rs (L265-297)
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
    }
```

**File:** contracts/defuse/src/contract/intents/state.rs (L303-337)
```rust
    fn auth_call(&mut self, signer_id: &AccountIdRef, auth_call: AuthCall) -> Result<()> {
        if auth_call.attached_deposit.is_zero() {
            Self::do_auth_call(signer_id.to_owned(), auth_call)
        } else {
            // withdraw from signer's wNEAR balance
            self.withdraw(
                signer_id,
                [(
                    Nep141TokenId::new(self.wnear_id().into_owned()).into(),
                    auth_call.attached_deposit.as_yoctonear(),
                )],
                Some("withdraw"),
                false,
            )?;

            ext_wnear::ext(self.wnear_id.clone())
                .with_attached_deposit(NearToken::from_yoctonear(1))
                .with_static_gas(NEAR_WITHDRAW_GAS)
                // do not distribute remaining gas here
                .with_unused_gas_weight(0)
                .near_withdraw(U128(auth_call.attached_deposit.as_yoctonear()))
                .then(
                    // do_auth_call only after unwrapping NEAR
                    Self::ext(env::current_account_id())
                        .with_static_gas(
                            Self::auth_call_callback_gas(&auth_call)
                                .ok_or(DefuseError::GasOverflow)?,
                        )
                        .do_auth_call(signer_id.to_owned(), auth_call),
                )
        }
        .detach();

        Ok(())
    }
```

**File:** contracts/defuse/src/contract/tokens/nep141/native.rs (L11-19)
```rust
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
