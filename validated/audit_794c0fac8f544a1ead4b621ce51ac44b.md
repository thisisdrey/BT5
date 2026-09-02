### Title
Native NEAR withdrawal has no resolve/refund callback for a failed final transfer, permanently burning user balance - ([File: contracts/defuse/src/contract/tokens/nep141/native.rs])

### Summary
`native_withdraw` debits the caller's internal wNEAR ledger balance and unwraps it to native NEAR, then performs the final payout with a bare `Promise::new(receiver_id).transfer(amount)` that has no resolve/refund callback, unlike the FT/NFT/MT withdrawal paths which chain a `*_resolve_withdraw` callback that checks the outcome of the final transfer and credits the user back on failure.

### Finding Description
For fungible-token withdrawals, `internal_ft_withdraw` chains the payout promise (`do_ft_withdraw`) with `.then(Self::ext(...).ft_resolve_withdraw(...))`, and `ft_resolve_withdraw` inspects the promise result and re-`deposit`s the unused amount back to the sender if the transfer failed [1](#0-0) [2](#0-1) .

For native NEAR withdrawals, `native_withdraw` first debits the caller's wNEAR balance via `self.withdraw(...)`, then calls `wnear.near_withdraw(...)` followed by `.then(do_native_withdraw(withdraw))` and `.detach()` — there is no further `.then()` resolver chained after `do_native_withdraw` to observe or react to the outcome of the final transfer [3](#0-2) . `do_native_withdraw` itself only checks that the preceding `near_withdraw` succeeded, and then issues the terminal payout as `Promise::new(withdraw.receiver_id).transfer(withdraw.amount)` with no downstream check of whether that transfer itself succeeds [4](#0-3) .

A NEAR `transfer` action to an `receiver_id` fails at the protocol level if that account does not exist (e.g., a non-existent named account, or a since-deleted account) or otherwise cannot receive the deposit. Because `NativeWithdraw.receiver_id` is caller-supplied and unwrap/burn of wNEAR into native NEAR has already occurred and the internal ledger balance has already been decremented before this final, unguarded transfer executes, a failure of that last-leg transfer results in permanent loss: the wNEAR was burned, the ledger balance was already debited, and there is no refund path to restore the user's balance, unlike the FT/NFT/MT withdrawal flows.

This breaks the conservation binding that "value debited" must equal "value delivered plus refunded" (`internal_sub_balance` on native_withdraw ≠ transfer delivered + refund on failure), which the FT withdrawal path enforces via `ft_resolve_withdraw` but the native withdrawal path does not.

### Impact Explanation
This matches the Critical impact bucket "funds permanently frozen": a legitimate account can lose its NEAR balance with no recovery mechanism if the final native transfer fails, because the ledger debit and wNEAR burn are irreversible and no resolver exists to credit the amount back.

### Likelihood Explanation
The only requirement is supplying a `receiver_id` for `NativeWithdraw` that cannot receive a plain NEAR transfer (e.g., a non-existent account name), which is fully attacker/user controlled and requires no privileged role, victim key, or special conditions — any unprivileged user signing a `NativeWithdraw` intent with a bad `receiver_id` can trigger this loss against their own funds, and it can also be used to grief another party's `receiver_id` field if it is attacker-influenced in a batched intent.

### Recommendation
Chain a resolver after the terminal `Promise::new(withdraw.receiver_id).transfer(...)` in `do_native_withdraw`, mirroring `ft_resolve_withdraw`: inspect the promise result of the transfer, and if it failed, re-mint/re-deposit the wNEAR-equivalent balance back to the original owner (and/or re-wrap the NEAR into wNEAR to restore the internal ledger), instead of unconditionally detaching the promise chain without any success check.

### Proof of Concept
1. User has `token_balances` credit in wNEAR-equivalent inside the contract.
2. User submits a signed `NativeWithdraw { receiver_id: "definitely-nonexistent-account.near", amount }`.
3. `native_withdraw` executes `self.withdraw(...)` decrementing the user's internal balance, then `ext_wnear::near_withdraw(...)` (burns wNEAR for native NEAR held by the contract), then `.then(do_native_withdraw(withdraw))` [3](#0-2) .
4. `do_native_withdraw` confirms `near_withdraw` succeeded, then issues `Promise::new(withdraw.receiver_id).transfer(withdraw.amount)` [4](#0-3) .
5. Because `receiver_id` does not exist, the transfer action fails at the protocol level. No resolver observes this failure; the user's internal balance was already zeroed and the wNEAR already burned — the NEAR is unrecoverable by the user through the contract.

### Citations

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L96-106)
```rust
            Self::do_ft_withdraw(withdraw.clone())
        }
        .then(
            Self::ext(env::current_account_id())
                .with_static_gas(Self::FT_RESOLVE_WITHDRAW_GAS)
                // do not distribute remaining gas here
                .with_unused_gas_weight(0)
                .ft_resolve_withdraw(withdraw.token, owner_id, withdraw.amount.into(), is_call),
        )
        .into())
    }
```

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L155-194)
```rust
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
