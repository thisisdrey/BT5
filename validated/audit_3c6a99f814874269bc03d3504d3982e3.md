### Title
`native_withdraw`/`storage_deposit` permanently debit signer's wNEAR `token_balances` with no refund path when `near_withdraw` fails - ([File: contracts/defuse/src/contract/intents/state.rs])

### Summary
`Contract::native_withdraw` and `Contract::storage_deposit` (contracts/defuse/src/contract/intents/state.rs lines 212-297) call `self.withdraw(...)` — which internally calls `internal_sub_balance` and persists the debit in the same receipt — before scheduling `ext_wnear::near_withdraw().then(do_native_withdraw/do_storage_deposit)`. If `near_withdraw` fails, the callback panics via `require!` and the debited `token_balances` entry is never re-credited, unlike the sibling `ft_withdraw` flow which chains an additional `ft_resolve_withdraw` callback that explicitly refunds on failure.

### Finding Description
The broken binding: `token_balances[owner_id][wNEAR] before withdraw == token_balances[owner_id][wNEAR] after failed near_withdraw + amount withdrawn`. This should always hold (debited value must equal delivered value or be refunded), but for native withdrawals/storage deposits it does not.

Trace:
- `native_withdraw` at [1](#0-0)  calls `self.withdraw(...)` which invokes `internal_sub_balance` (persisted immediately in this receipt) at [2](#0-1) , then schedules `ext_wnear::near_withdraw(...).then(Self::do_native_withdraw(withdraw)).detach()`.
- `do_native_withdraw` at [3](#0-2)  only does `require!(promise_result_checked_void(0).is_ok(), "near_withdraw failed")` and then transfers NEAR — if the `near_withdraw` promise failed, this callback **panics** and no `Promise::transfer` and no balance re-credit ever happen.
- `storage_deposit` at [4](#0-3)  and `do_storage_deposit` at [5](#0-4)  exhibit the identical pattern.
- Contrast with `internal_ft_withdraw` at [6](#0-5) , which chains a *second* `.then(ft_resolve_withdraw)` after `do_ft_withdraw`; `ft_resolve_withdraw` at [7](#0-6)  explicitly checks the outcome and calls `self.deposit(...)` to refund unsettled amounts even if `do_ft_withdraw` itself panicked upstream. `native_withdraw`/`storage_deposit` lack this final resolver entirely — there is only a single `.then(do_native_withdraw).detach()` with no subsequent refund-capable callback.

Because NEAR persists state changes per-receipt, the `internal_sub_balance` committed inside the `native_withdraw`/`storage_deposit` function call is not rolled back by a later panic in the separate `do_native_withdraw`/`do_storage_deposit` receipt. Any failure of the `near_withdraw` cross-contract call (insufficient wNEAR-side FT balance held by the Verifier account, wNEAR contract paused, insufficient gas, or any other on-chain condition causing that call to error) leaves the signer's `token_balances` entry permanently decremented with the corresponding NEAR never released and never refunded.

### Impact Explanation
Value leaves the signer's `token_balances` ledger with no corresponding NEAR delivered to the receiver and no re-credit to the signer — this is a permanent freeze of user funds, matching the Critical category "user funds permanently frozen." It is repeatable for any account/any amount that triggers a `near_withdraw` failure, and applies uniformly to `native_withdraw`, `storage_deposit`, and `auth_call`'s wNEAR-funded branch which follows the same pattern.

### Likelihood Explanation
The exploit requires a way to make `near_withdraw` on the wNEAR contract fail after the Verifier has already debited the ledger. The wNEAR contract itself is external to this repo (only the `ext_wnear` trait is defined here at [8](#0-7) ), so I cannot verify from this repository whether an unprivileged party can reliably force `near_withdraw` to fail (e.g., via storage exhaustion) without accessing the actual wNEAR/wrap.near contract implementation. The missing-refund defect itself, however, is unconditionally present in the code regardless of the exact trigger mechanism.

### Recommendation
Add a resolver callback analogous to `ft_resolve_withdraw` after `do_native_withdraw`/`do_storage_deposit` that inspects the promise result and calls `self.deposit(...)` to re-credit the signer's wNEAR `token_balances` whenever the downstream `near_withdraw`/transfer/storage_deposit promise did not succeed, instead of only panicking.

### Proof of Concept
```
cargo test in contracts/defuse (near-workspaces sandbox):
1. Deploy Verifier + a wNEAR-mock contract whose near_withdraw can be forced to fail (e.g. simulate insufficient balance or return a failing promise).
2. Fund signer's Defuse token_balances[wNEAR] = N via deposit.
3. Assert token_balances[wNEAR] == N.
4. Call native_withdraw with amount == N, causing near_withdraw to fail.
5. Assert do_native_withdraw's promise panics ("near_withdraw failed").
6. Assert token_balances[wNEAR] == 0 (debited) with no compensating NEAR transfer and no re-credit — violating debited == delivered + refunded.
```

### Citations

**File:** contracts/defuse/src/contract/intents/state.rs (L171-195)
```rust
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

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L53-106)
```rust
impl Contract {
    pub(crate) fn internal_ft_withdraw(
        &mut self,
        owner_id: AccountId,
        withdraw: FtWithdraw,
        force: bool,
    ) -> Result<PromiseOrValue<U128>> {
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

        let is_call = withdraw.is_call();
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

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L155-195)
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
}
```

**File:** crates/near/wnear/src/lib.rs (L9-13)
```rust
#[ext_contract(ext_wnear)]
pub trait WNear: FungibleTokenCore + FungibleTokenResolver + StorageManagement {
    fn near_deposit(&mut self);
    fn near_withdraw(&mut self, amount: U128) -> Promise;
}
```
