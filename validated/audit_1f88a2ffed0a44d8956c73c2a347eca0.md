### Title
Failed target `storage_deposit` call permanently burns signer's wNEAR with no refund path - (File: `contracts/defuse/src/contract/intents/state.rs`)

### Summary
`State::storage_deposit` debits the signer's internal wNEAR `token_balances` and unwraps real NEAR via `near_withdraw` before attempting an external `storage_deposit` call on an arbitrary `contract_id`. If that final external call fails (e.g. attacker sets `amount` below the target contract's minimum storage cost), the unwrapped NEAR is refunded by the NEAR runtime to the Defuse contract's own account — not to the signer's internal ledger — and no `.then()` resolver exists to re-credit the signer, permanently freezing their debited balance.

### Finding Description
The broken binding is: `signer_wnear_balance_before − storage_deposit.amount == signer_wnear_balance_after + value_delivered_as_real_storage + value_recredited_on_failure`. This should hold for every settled intent; it does not hold when the terminal external call fails.

Code path in `contracts/defuse/src/contract/intents/state.rs` [1](#0-0) :
1. `self.withdraw(owner_id, [(wnear_token_id, amount)], ...)` immediately debits the signer's internal `token_balances` for `amount` — synchronous, unconditional on later success.
2. `near_withdraw` unwraps that wNEAR into real NEAR held by the Defuse contract account.
3. `.then(... .do_storage_deposit(storage_deposit))` schedules the callback in `contracts/defuse/src/contract/tokens/nep141/storage_deposit.rs` [2](#0-1) .
4. `do_storage_deposit` only checks that promise index 0 (`near_withdraw`) succeeded via `promise_result_checked_void(0)`, then issues `ext_storage_management::ext(storage_deposit.contract_id).with_attached_deposit(storage_deposit.amount)....storage_deposit(...)` as the final, un-resolved Promise of the chain.
5. The entire chain is `.detach()`ed at the call site (line 294), so there is no subsequent `.then()` to inspect whether this final `storage_deposit` call succeeded.

If the target contract's `storage_deposit` panics (e.g. attached deposit below its minimum required storage bytes cost — a normal NEP-145 requirement), NEAR's protocol-level refund returns the attached NEAR to the *predecessor of that failed call*, which is the Defuse contract account itself (not credited to any user's internal ledger, and not returned as wNEAR). Compare this to `ft_withdraw`, which has an explicit `ft_resolve_withdraw` callback (visible via `resolve_deposit_internal` pattern in `contracts/defuse/src/contract/tokens/mod.rs` [3](#0-2) ) that re-credits users on failed transfers. No equivalent resolver exists for `do_storage_deposit` (or the structurally identical `do_native_withdraw` [4](#0-3) ).

Attacker payload: sign a `MultiPayload` containing a `StorageDeposit { amount: 1 yoctoNEAR, contract_id: <any contract with a standard NEP-145 storage_deposit min>, deposit_for_account_id: <any> }` intent for their own account. The attacker only needs a nonzero wNEAR balance in the Verifier ≥ `amount`.

None of the listed guards (`MultiPayload::verify`, nonce checks, `Lock`, `TransferMatcher::finalize`, `#[private]`, `access_control_any`) address this — they only govern signature/replay/lock validity, not the post-settlement failure/refund path of this specific async chain.

### Impact Explanation
The signer's internal wNEAR `token_balances` is permanently reduced by `amount` with nothing delivered (no storage credited on the target contract) and nothing re-credited to the signer. This is a genuine, repeatable, self-inflicted or attacker-inducible fund freeze — the signer's own funds vanish from the Verifier's accounting with no corresponding value anywhere in their control, matching the "user funds permanently frozen" Critical category. It is repeatable per signer/per amount and does not require any privileged role.

### Likelihood Explanation
Preconditions are trivial for an unprivileged attacker: hold any wNEAR balance in the Verifier, sign one `StorageDeposit` intent with an `amount` below the target contract's minimum storage byte cost (a value the attacker fully controls and knows in advance, e.g. `amount: 1`). No relayer key, DAO role, or third-party cooperation is needed — the attacker can execute this against their own account solely to demonstrate the bug, or trick another party into believing a storage deposit intent is safe.

### Recommendation
Add a resolver `.then()` after the target `storage_deposit` call in `do_storage_deposit` (mirroring `ft_resolve_withdraw`/`resolve_deposit_internal`) that checks the outcome of the external `storage_deposit` promise and, on failure, re-credits the signer's internal wNEAR `token_balances` for `amount` (and ideally re-wraps the returned NEAR back into wNEAR, or explicitly reconciles the NEAR that was refunded to the contract account).

### Proof of Concept
`cargo test` (near-workspaces sandbox) plan:
1. Deploy Defuse contract with wNEAR configured, and a target FT contract implementing standard NEP-145 `storage_deposit` with a nonzero minimum required deposit.
2. Fund a signer account with wNEAR inside the Verifier (deposit wNEAR then have it credited to `token_balances`).
3. Sign and submit a `MultiPayload` with a single `StorageDeposit { amount: 1, contract_id: <target>, deposit_for_account_id: signer }` intent via `execute_intents`.
4. Await promise resolution in the sandbox.
5. Assert: `signer_wnear_balance_after == signer_wnear_balance_before - 1` (debited), AND target contract's storage balance for the account is unchanged/None (nothing delivered), AND no compensating credit event (`mt_mint`/refund) was emitted for the signer — demonstrating the debited value is neither delivered nor recredited.

### Citations

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

**File:** contracts/defuse/src/contract/tokens/mod.rs (L152-220)
```rust
    pub fn resolve_deposit_internal<'a, I>(&mut self, receiver_id: &AccountIdRef, tokens: I)
    where
        I: IntoIterator<Item = (TokenId, &'a mut u128)>,
        I::IntoIter: ExactSizeIterator,
    {
        let tokens_iter = tokens.into_iter();
        let tokens_count = tokens_iter.len();

        let requested_refunds = promise_result_checked_json_with_len::<Vec<U128>>(0, tokens_count)
            .ok()
            .and_then(Result::ok)
            .filter(|refunds| refunds.len() == tokens_count);

        let mut burn_event = MtBurnEvent {
            owner_id: Cow::Borrowed(receiver_id),
            authorized_id: None,
            token_ids: Vec::with_capacity(tokens_count).into(),
            amounts: Vec::with_capacity(tokens_count).into(),
            memo: Some(REFUND_MEMO.into()),
        };

        let Some(receiver) = self
            .storage
            .accounts
            .get_mut(receiver_id)
            .map(Lock::as_inner_unchecked_mut)
        else {
            tokens_iter.for_each(|(_, amount)| *amount = 0);
            return;
        };

        for ((token_id, deposited), requested_refund) in
            tokens_iter.zip_eq(requested_refunds.map_or_else(
                || Either::Right(std::iter::repeat_n(None, tokens_count)),
                |v| Either::Left(v.into_iter().map(|elem| Some(elem.0))),
            ))
        {
            let requested_refund = requested_refund.unwrap_or(*deposited);
            let balance_left = receiver.token_balances.amount_for(&token_id);
            // NOTE: refunds are capped by deposited amounts and balance left on the receiver
            let refund_amount = requested_refund.min(*deposited).min(balance_left);
            *deposited = refund_amount;
            if refund_amount == 0 {
                continue;
            }

            burn_event.token_ids.to_mut().push(token_id.to_string());
            burn_event.amounts.to_mut().push(refund_amount);

            receiver
                .token_balances
                .sub(token_id.clone(), refund_amount)
                .ok_or(DefuseError::BalanceOverflow)
                .unwrap_or_else(|err| err.panic());

            self.storage
                .state
                .total_supplies
                .sub(token_id, refund_amount)
                .ok_or(DefuseError::BalanceOverflow)
                .unwrap_or_else(|err| err.panic());
        }

        if !burn_event.amounts.is_empty() {
            // NOTE: No need for `check_refund()` here since this IS the refund.
            // The refund memo size was already accounted for in the original mint.
            MtEvent::MtBurn([burn_event].as_slice().into()).emit();
        }
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
