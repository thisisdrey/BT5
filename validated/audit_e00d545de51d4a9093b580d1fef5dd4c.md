### Title
Failed final promise in `native_withdraw`/`storage_deposit`/`auth_call` never re-credits the debited wNEAR — permanent loss of signer funds - (File: `contracts/defuse/src/contract/intents/state.rs`)

### Summary
`native_withdraw`, `storage_deposit`, and the deposit-bearing branch of `auth_call` all call `self.withdraw(...)` (which mutates `token_balances` via `internal_sub_balance`) and commit that state change *before* dispatching the `near_withdraw() -> do_*` promise chain. Unlike `ft_withdraw`/`nft_withdraw`/`mt_withdraw`, none of these three paths schedule a `*_resolve_*` callback that inspects the final promise's outcome and refunds on failure, so if the last leg of the chain fails after the wNEAR has already been unwrapped, the debited balance is never restored anywhere.

### Finding Description
The broken binding is: `wNEAR debited from signer == NEAR/value delivered to the named target`, evaluated per call.

- `native_withdraw`: `self.withdraw(owner_id, [wnear_amount], ...)` commits the ledger debit first, then chains `near_withdraw()` → `do_native_withdraw` [1](#0-0) . `do_native_withdraw` only checks that `near_withdraw` itself succeeded and then does a bare `Promise::new(withdraw.receiver_id).transfer(withdraw.amount)` with no further `.then()` resolve/refund step [2](#0-1) . A `Transfer` action to a named account that was never created fails, and that failure is never observed or compensated by the Verifier.
- `storage_deposit`: same pattern — `self.withdraw(...)` debits first, then `near_withdraw().then(do_storage_deposit)` [3](#0-2) . `do_storage_deposit` forwards the deposit to an attacker/target-controlled `storage_deposit.contract_id` with no trailing resolve/refund callback [4](#0-3) . If that contract rejects/panics on `storage_deposit`, the debit stands.
- `auth_call` (attached_deposit branch): same pattern — `self.withdraw(...)` debits, then `near_withdraw().then(do_auth_call)` [5](#0-4) . `do_auth_call` dispatches `on_auth` to an arbitrary `contract_id` with no resolve callback afterward [6](#0-5) . Any panic in the callee's `on_auth` leaves the debit permanent.

Contrast this with `ft_withdraw`/`nft_withdraw`/`mt_withdraw`, which explicitly chain a `ft_resolve_withdraw`/`nft_resolve_withdraw`/`mt_resolve_withdraw` callback that reads `promise_result_checked_*` and calls `self.deposit(...)` to refund whatever wasn't actually transferred [7](#0-6) . That refund mechanism is absent for the native/storage_deposit/auth_call paths — there is no code anywhere in `contracts/defuse/src/contract/tokens/nep141/native.rs`, `storage_deposit.rs`, or `contracts/defuse/src/contract/intents/auth_call.rs` that ever calls `internal_add_balance`/`deposit` to restore the signer's balance.

The `withdraw()` helper itself (`contracts/defuse/src/contract/tokens/mod.rs:76-129`) unconditionally commits `token_balances.sub` and emits an `mt_burn` event before any promise executes, and there is no compensating credit path once that has happened for these three intents.

Existing guards do not prevent this: `require!(promise_result_checked_void(0).is_ok(), ...)` in `do_native_withdraw`/`do_storage_deposit`/`do_auth_call` only reverts the *callback receipt* itself (i.e., stops the final transfer from being attempted when `near_withdraw` failed) — it does not, and cannot, undo the `internal_sub_balance` that was already committed in the prior successful receipt that called `withdraw()` and scheduled the promise chain.

### Impact Explanation
Whenever the final leg of the promise chain fails after the wNEAR unwrap succeeds — a named receiver account that doesn't exist (`native_withdraw`), a storage-provider contract that rejects/panics on `storage_deposit`, or an `on_auth` callee that panics (`auth_call` with `attached_deposit`) — the signer's tracked `token_balances` entry for wNEAR is permanently reduced with no compensating credit anywhere in the Verifier's state, while the unwrapped NEAR (refunded by the NEAR runtime back to the Verifier contract account on receipt failure) sits in the contract untracked by the ledger. This is a "refund or resolver credit that does not match what failed to settle" leading to permanently frozen signer funds — matching the Critical category. It is repeatable across any account/amount and is triggerable by any unprivileged holder of a Verifier wNEAR balance, either accidentally or via a deliberately crafted target (self-controlled contract for `storage_deposit`/`auth_call`, or a nonexistent account name for `native_withdraw`) to demonstrate the loss.

### Likelihood Explanation
No special privileges are required — only a signer with a wNEAR balance in the Verifier and the ability to submit `execute_intents`/`simulate_intents` with a self-signed `MultiPayload` containing a `NativeWithdraw`, `StorageDeposit`, or `AuthCall` intent. For `storage_deposit` and `auth_call`, the attacker fully controls the target contract (deploy one that panics), making the failure trivially reproducible. For `native_withdraw`, specifying a syntactically-valid but never-created named account triggers the same failure deterministically in a sandbox. Cost is just the amount being withdrawn plus gas; the bug is deterministically reproducible every time.

### Recommendation
Add `*_resolve_*` callbacks for `native_withdraw`, `storage_deposit`, and `auth_call` (mirroring `ft_resolve_withdraw`) that inspect the outcome of the final promise (`Promise::transfer`, `storage_deposit`, `on_auth`) and call `internal_add_balance`/`deposit` to restore the signer's wNEAR balance when that final step fails.

### Proof of Concept
Three `near-workspaces` sandbox tests, one per intent, each asserting the same binding fails to hold:

1. `native_withdraw` test: sign and execute a `NativeWithdraw` intent with `receiver_id` set to a syntactically valid but never-created named account. Assert: signer's `token_balances` wNEAR amount (via a view call) decreased by the full withdrawn amount; target account balance query shows no NEAR delivered; and no `internal_add_balance`/compensating credit is observed for the signer afterward.
2. `storage_deposit` test: deploy a contract at `contract_id` that panics/rejects on `storage_deposit`; execute a `StorageDeposit` intent targeting it. Assert the same triple: signer debited in full, target never registered/received deposit, and no refund credited back.
3. `auth_call` test: deploy a callee contract whose `on_auth` panics; execute an `AuthCall` intent with nonzero `attached_deposit` targeting it. Assert the same triple: signer debited in full, callee never received usable NEAR (call reverted), and no refund credited back in the Verifier's `token_balances`. [1](#0-0) [2](#0-1) [4](#0-3) [6](#0-5) [8](#0-7) [7](#0-6)

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

**File:** contracts/defuse/src/contract/intents/auth_call.rs (L17-37)
```rust
    #[private]
    pub fn do_auth_call(signer_id: AccountId, auth_call: AuthCall) -> Promise {
        if !auth_call.attached_deposit.is_zero() {
            require!(
                promise_result_checked_void(0).is_ok(),
                "near_withdraw failed",
            );
        }

        let min_gas = auth_call.min_gas();
        let mut p = Promise::new(auth_call.contract_id);

        if let Some(state_init) = auth_call.state_init {
            p = p.state_init(state_init, NearToken::ZERO);
        }

        ext_auth_callee::ext_on(p)
            .with_attached_deposit(auth_call.attached_deposit)
            .with_static_gas(min_gas)
            .on_auth(signer_id, auth_call.msg)
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

**File:** contracts/defuse/src/contract/tokens/mod.rs (L76-129)
```rust
    pub(crate) fn withdraw(
        &mut self,
        owner_id: &AccountIdRef,
        token_amounts: impl IntoIterator<Item = (TokenId, u128)>,
        memo: Option<impl Into<String>>,
        force: bool,
    ) -> Result<()> {
        let owner = self
            .storage
            .accounts
            .get_mut(owner_id)
            .ok_or_else(|| DefuseError::AccountNotFound(owner_id.to_owned()))?
            .get_mut_maybe_forced(force)
            .ok_or_else(|| DefuseError::AccountLocked(owner_id.to_owned()))?;

        let mut burn_event = MtBurnEvent {
            owner_id: Cow::Owned(owner_id.to_owned()),
            authorized_id: None,
            token_ids: Vec::new().into(),
            amounts: Vec::new().into(),
            memo: memo.map(Into::into).map(Into::into),
        };

        for (token_id, amount) in token_amounts {
            if amount == 0 {
                return Err(DefuseError::InvalidIntent);
            }

            burn_event.token_ids.to_mut().push(token_id.to_string());
            burn_event.amounts.to_mut().push(amount);

            owner
                .token_balances
                .sub(token_id.clone(), amount)
                .ok_or(DefuseError::BalanceOverflow)?;

            self.storage
                .state
                .total_supplies
                .sub(token_id, amount)
                .ok_or(DefuseError::BalanceOverflow)?;
        }

        // Schedule to emit `mt_burn` events only in the end of tx
        // to avoid confusion when `mt_burn` occurs before relevant
        // `mt_transfer` arrives. This can happen due to postponed
        // delta-matching during intents execution.
        if !burn_event.amounts.is_empty() {
            self.runtime.postponed_burns.mt_burn(burn_event);
        }

        Ok(())
    }
}
```
