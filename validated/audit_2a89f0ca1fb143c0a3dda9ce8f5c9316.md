### Title
`ft_resolve_withdraw` never refunds the wNEAR `storage_deposit` fee when `do_ft_withdraw`'s `storage_deposit` call to the token fails - ([File: contracts/defuse/src/contract/tokens/nep141/withdraw.rs])

### Summary
`internal_ft_withdraw` atomically debits both the withdrawn FT `amount` and the wNEAR `storage_deposit` from the signer's ledger balance, converts the wNEAR into real NEAR via `near_withdraw`, and attaches it to a `storage_deposit` call on the target token contract. If that `storage_deposit` call fails (e.g. deposit below the token's minimum), the FT `amount` is correctly refunded by `ft_resolve_withdraw`, but the `storage_deposit` value is never passed to, or refunded by, any resolver, permanently orphaning the user's wNEAR.

### Finding Description
The broken binding: `wNEAR debited from signer via internal_ft_withdraw's self.withdraw(...)` should equal `wNEAR value ultimately consumed as storage balance on token for receiver_id OR refunded back to signer`. In the failure case, neither holds.

- `internal_ft_withdraw` debits the FT `amount` and, if `storage_deposit` is `Some`, the wNEAR amount, in a single `self.withdraw(...)` call: [1](#0-0) 
- It then calls `near_withdraw` (converting the debited wNEAR into real NEAR held by the defuse contract's own account) and chains `do_ft_withdraw`: [2](#0-1) 
- In `do_ft_withdraw`, the real NEAR is attached to `ext_storage_management::ext(token).storage_deposit(...)`, and the result is chained via `ext_ft_core::ext_on(p)` into `ft_transfer`/`ft_transfer_call` regardless of whether the `storage_deposit` promise succeeded: [3](#0-2) 
- Crucially, `ft_resolve_withdraw`'s signature only carries `token, sender_id, amount, is_call` - the `storage_deposit` value is not passed at all, so there is no code path capable of refunding it: [4](#0-3) [5](#0-4) 

When the token's `storage_deposit` call panics/reverts because the attached deposit is below its required minimum, standard NEP-141/storage-management semantics refund the attached deposit back to the *predecessor* of that failed receipt - which is the defuse contract's own account, not the signer. The signer's internal ledger, meanwhile, was already permanently debited for that amount in `self.withdraw(...)`. The subsequent `ft_transfer`/`ft_transfer_call` to the unregistered `receiver_id` also fails (as expected), and `ft_resolve_withdraw` correctly refunds the FT `amount` via `promise_result_checked_void`/`promise_result_checked_json` checks and `self.deposit(...)`, but it has no knowledge of, or mechanism for, refunding the `storage_deposit` wNEAR. (Note: with `msg: None` as in the attacker's payload, `do_ft_withdraw` actually invokes `ft_transfer`, not `ft_transfer_call`, but this detail does not change the outcome - the resolver still refunds only `amount`.)

### Impact Explanation
The signer's wNEAR balance is permanently reduced by `storage_deposit.as_yoctonear()` with nothing credited back, while the real NEAR converted from it becomes stranded on the defuse contract's own account, unaccounted for and unreachable by the user through any documented flow. This is a resolver credit that does not match what failed to settle (the FT withdrawal fully failed and was refunded, but the storage-deposit leg silently failed to be refunded), fitting the Critical category. The attack is repeatable per withdrawal call, across any token and by any signer with wNEAR balance, at no cost to the attacker beyond the loss itself (which they could also inflict on themselves accidentally, or an attacker/griefer could deliberately trigger the loss against a target token whose minimum storage balance is unknown to a signer/relayer at build time). Note that per the impact criteria, the loss is against the signer's own funds, so this is best framed as unrecoverable user-funds loss/permanent freeze rather than an attacker profiting from another's funds - there is no separate direct third-party profit demonstrated here.

### Likelihood Explanation
The precondition is simple: any signer holding a token balance and wNEAR calls `ft_withdraw` (or the intent-based equivalent) with a `storage_deposit` value that is insufficient for the target token's registration requirements. No privileged role or relayer key is required, and the discrepancy between the true minimum storage balance and a supplied `storage_deposit` is easy to trigger (many tokens have varying/updatable minimum balances, e.g. after a schema/state upgrade). This can happen accidentally to unaware users and can be intentionally engineered by an attacker targeting their own or others' misconfigured withdraw parameters.

### Recommendation
Pass `storage_deposit` (and `receiver_id`) into `ft_resolve_withdraw`, check the result of the `storage_deposit` promise explicitly (not just implicitly via the final `ft_transfer`/`ft_transfer_call` result), and if it failed, refund the `storage_deposit` amount back to the signer's wNEAR balance via `self.deposit(...)`, mirroring the FT `amount` refund logic. Alternatively, avoid attaching real NEAR to a chained call whose failure is indistinguishable from the final transfer's failure, and instead perform storage_deposit as an independently resolved step with its own callback and refund path.

### Proof of Concept
`cargo test` (unit test with a mocked/sandboxed NEP-141 token requiring a minimum storage balance greater than the supplied `storage_deposit`):
1. Deploy a real/sandbox NEP-141 token contract with `storage_balance_bounds().min` set above `NearToken::from_millinear(1)`.
2. Fund the defuse contract's ledger for `signer` with `amount` of the token and sufficient wNEAR.
3. Sign and execute `FtWithdraw { token, receiver_id, amount, storage_deposit: Some(NearToken::from_millinear(1)), min_gas: None, msg: None }` via `internal_ft_withdraw`/`ft_withdraw`.
4. Assert (a) signer's ledger FT balance for `token` is restored to its pre-withdraw value (via `ft_resolve_withdraw`'s refund), confirming the FT leg is safely reverted; and (b) signer's ledger wNEAR balance remains permanently reduced by `storage_deposit.as_yoctonear()` with no compensating `deposit` event/credit anywhere in the resolver flow, while the defuse contract account's own native NEAR balance increased by the refunded attached deposit - demonstrating the broken equality between debited wNEAR and credited/refunded wNEAR.

### Citations

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L60-74)
```rust
        self.withdraw(
            &owner_id,
            iter::once((
                Nep141TokenId::new(withdraw.token.clone()).into(),
                withdraw.amount,
            ))
            .chain(withdraw.storage_deposit.map(|amount| {
                (
                    Nep141TokenId::new(self.wnear_id().into_owned()).into(),
                    amount.as_yoctonear(),
                )
            })),
            Some("withdraw"),
            force,
        )?;
```

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L77-97)
```rust
        Ok(if let Some(storage_deposit) = withdraw.storage_deposit {
            ext_wnear::ext(self.wnear_id.clone())
                .with_attached_deposit(NearToken::from_yoctonear(1))
                .with_static_gas(NEAR_WITHDRAW_GAS)
                // do not distribute remaining gas here
                .with_unused_gas_weight(0)
                .near_withdraw(U128(storage_deposit.as_yoctonear()))
                .then(
                    // schedule storage_deposit() only after near_withdraw() returns
                    Self::ext(env::current_account_id())
                        .with_static_gas(
                            Self::DO_FT_WITHDRAW_GAS
                                .checked_add(withdraw.min_gas())
                                .ok_or(DefuseError::GasOverflow)
                                .unwrap_or_else(|err| err.panic()),
                        )
                        .do_ft_withdraw(withdraw.clone()),
                )
        } else {
            Self::do_ft_withdraw(withdraw.clone())
        }
```

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L99-105)
```rust
            Self::ext(env::current_account_id())
                .with_static_gas(Self::FT_RESOLVE_WITHDRAW_GAS)
                // do not distribute remaining gas here
                .with_unused_gas_weight(0)
                .ft_resolve_withdraw(withdraw.token, owner_id, withdraw.amount.into(), is_call),
        )
        .into())
```

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L118-151)
```rust
    pub fn do_ft_withdraw(withdraw: FtWithdraw) -> Promise {
        let min_gas = withdraw.min_gas();
        let p = if let Some(storage_deposit) = withdraw.storage_deposit {
            require!(
                promise_result_checked_void(0).is_ok(),
                "near_withdraw failed",
            );

            ext_storage_management::ext(withdraw.token)
                .with_attached_deposit(storage_deposit)
                .with_static_gas(STORAGE_DEPOSIT_GAS)
                // do not distribute remaining gas here
                .with_unused_gas_weight(0)
                .storage_deposit(Some(withdraw.receiver_id.clone()), None)
        } else {
            Promise::new(withdraw.token)
        };

        let p = ext_ft_core::ext_on(p)
            .with_attached_deposit(NearToken::from_yoctonear(1))
            .with_static_gas(min_gas)
            // distribute remaining gas here
            .with_unused_gas_weight(1);
        if let Some(msg) = withdraw.msg {
            p.ft_transfer_call(
                withdraw.receiver_id,
                withdraw.amount.into(),
                withdraw.memo,
                msg,
            )
        } else {
            p.ft_transfer(withdraw.receiver_id, withdraw.amount.into(), withdraw.memo)
        }
    }
```

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L156-194)
```rust
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
