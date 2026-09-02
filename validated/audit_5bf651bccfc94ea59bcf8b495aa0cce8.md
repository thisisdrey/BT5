### No vulnerability found for this question.

The `auth_call` branching in `contracts/defuse/src/contract/intents/state.rs::auth_call` (lines 303-337) is intentional design, not a bug. When `attached_deposit.is_zero()`, calling `Self::do_auth_call` directly (rather than chained after `near_withdraw`) is correct because there is no `near_withdraw` promise whose result needs checking — the `promise_result_checked_void(0)` guard inside `do_auth_call` (`contracts/defuse/src/contract/intents/auth_call.rs:19-24`) is explicitly conditioned on `!auth_call.attached_deposit.is_zero()` for exactly this reason. [1](#0-0) [2](#0-1) 

The claimed "fee bypass"/drain relies on `state_init` deployment consuming storage staking paid from the Verifier's own NEAR reserve. But the code comment on `STATE_INIT_GAS` explicitly documents that this only covers deployment "via Global Contract ref (NEP-591) with <770B storage which doesn't require storage staking" — i.e., the design assumes no storage-staking NEAR is consumed by a zero-deposit `state_init` call in the covered case, and `Promise::state_init(state_init, NearToken::ZERO)` transfers zero NEAR value. [3](#0-2) [4](#0-3) 

Any residual gas/storage cost from executing this promise is paid out of the enclosing NEAR transaction's own gas allowance (paid by whoever submitted the outer transaction), not credited/debited through the intents balance-accounting model (`internal_add_balance`/`internal_sub_balance`, `token_balances`). This is exactly the class of finding the rules exclude: "unbounded gas or storage consumption, denial of service, rate limiting, retry behaviour and resource exhaustion." There is no broken equality in signer balances — when `attached_deposit.is_zero()`, the binding "signer's `token_balances` delta == 0" is the *intended* semantic (nothing is attached, so nothing should be withdrawn), not a divergence from a documented invariant that value moves without authorization from the Verifier's custodied funds. [5](#0-4)

### Citations

**File:** contracts/defuse/src/contract/intents/state.rs (L147-195)
```rust
    fn internal_add_balance(
        &mut self,
        owner_id: AccountId,
        tokens: impl IntoIterator<Item = (TokenId, u128)>,
    ) -> Result<()> {
        let owner = self
            .accounts
            .get_or_create(owner_id)
            // we allow locked accounts to accept deposits and incoming deposits
            .as_inner_unchecked_mut();

        for (token_id, amount) in tokens {
            if amount == 0 {
                return Err(DefuseError::InvalidIntent);
            }
            owner
                .token_balances
                .add(token_id, amount)
                .ok_or(DefuseError::BalanceOverflow)?;
        }

        Ok(())
    }

    fn internal_sub_balance(
        &mut self,
        owner_id: &AccountIdRef,
        tokens: impl IntoIterator<Item = (TokenId, u128)>,
    ) -> Result<()> {
        let owner = self
            .accounts
            .get_mut(owner_id)
            .ok_or_else(|| DefuseError::AccountNotFound(owner_id.to_owned()))?
            .get_mut()
            .ok_or_else(|| DefuseError::AccountLocked(owner_id.to_owned()))?;

        for (token_id, amount) in tokens {
            if amount == 0 {
                return Err(DefuseError::InvalidIntent);
            }

            owner
                .token_balances
                .sub(token_id.clone(), amount)
                .ok_or(DefuseError::BalanceOverflow)?;
        }

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

**File:** contracts/defuse/src/contract/intents/auth_call.rs (L10-15)
```rust
    pub(crate) const DO_AUTH_CALL_MIN_GAS: Gas = Gas::from_tgas(5);

    /// Covers `StateInit` (NEP-616) cost when deterministic account doesn't exist yet.
    /// Only accounts for deploying via Global Contract ref (NEP-591) with <770B storage
    /// which doesn't require storage staking.
    pub const STATE_INIT_GAS: Gas = Gas::from_tgas(15);
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
