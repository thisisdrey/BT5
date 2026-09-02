### Title
Failed `near_withdraw`/`storage_deposit` in `ft_withdraw`'s `storage_deposit` path permanently burns wNEAR since `ft_resolve_withdraw` only refunds the primary token - ([File: contracts/defuse/src/contract/tokens/nep141/withdraw.rs])

### Summary
`internal_ft_withdraw` synchronously debits both the primary `token`/`amount` and (if set) the wNEAR `storage_deposit` amount from the user's ledger balance before scheduling the `near_withdraw` → `do_ft_withdraw` → `ft_resolve_withdraw` promise chain. `ft_resolve_withdraw` only re-credits the primary `token`/`amount` on failure; the wNEAR storage-deposit debit has no corresponding refund path anywhere in the chain, so a failed `near_withdraw`/`storage_deposit` promise permanently destroys the user's wNEAR balance.

### Finding Description
The broken binding: `wnear_debited_at_withdraw_call == wnear_actually_spent_on_storage_deposit + wnear_refunded_by_resolver`. In practice, when the `near_withdraw`/`storage_deposit` sub-promise fails, `wnear_actually_spent = 0` and `wnear_refunded_by_resolver = 0`, so the equality is violated by the full `storage_deposit` amount.

Code path: `internal_ft_withdraw` in `contracts/defuse/src/contract/tokens/nep141/withdraw.rs:54-106` calls `self.withdraw(...)` synchronously, debiting both `Nep141TokenId(withdraw.token)`/`amount` and, if `withdraw.storage_deposit` is `Some`, `Nep141TokenId(wnear_id)`/`storage_deposit.as_yoctonear()` [1](#0-0) . It then schedules `near_withdraw().then(do_ft_withdraw(withdraw))` when `storage_deposit` is set, and always chains `.then(ft_resolve_withdraw(token, owner_id, amount, is_call))` on the outer promise [2](#0-1) .

`do_ft_withdraw` uses `require!(promise_result_checked_void(0).is_ok(), "near_withdraw failed")` to abort if the wNEAR unwrap fails [3](#0-2) . If that `require!` panics (or if the subsequent `storage_deposit` call to the token contract fails), the whole `do_ft_withdraw` receipt fails, but the outer `.then(ft_resolve_withdraw)` still fires as a NEAR promise callback.

`ft_resolve_withdraw` only inspects `promise_result_checked_void(0)`/`promise_result_checked_json(0)` to decide the refund for the *primary* `token`/`amount`; it never re-credits the wNEAR `storage_deposit` amount that was debited separately upfront [4](#0-3) . There is no other resolver or refund logic anywhere in this call chain that touches the wNEAR side of the debit.

This is reachable by an unprivileged user because `storage_deposit` is a normal field of the `FtWithdraw` intent struct [5](#0-4) , which can be signed and submitted via `execute_intents`/`simulate_intents` in a `MultiPayload`, executed through `Contract::ft_withdraw` intent handling and `CachedState::ft_withdraw`/`internal_sub_balance` [6](#0-5) . The unprivileged, direct `ft_withdraw` NEAR entrypoint hardcodes `storage_deposit: None` [7](#0-6) , but the intents path (`FtWithdraw` as a signed intent) does not restrict this field, so the attacker (acting as their own signer) can set it freely.

Why guards don't help: `require!`/`#[private]`/`#[pause]` don't prevent the asynchronous failure from bypassing the wNEAR-specific refund - they only gate execution of `do_ft_withdraw` itself. The synchronous `self.withdraw()` check (seen in the test at `tests/src/tests/defuse/intents/ft_withdraw.rs:105-124`, which fails atomically if the user's wNEAR balance is *insufficient*) only guards against insufficient-balance situations, not against post-debit *promise* failures once the balance check passes.

### Impact Explanation
An attacker (using their own wNEAR balance and their own token/receiver) can trigger a `near_withdraw` or downstream `storage_deposit` promise failure (e.g., by choosing a `receiver_id`/`token` combination on a controlled token contract that rejects `storage_deposit`, or by exploiting min_gas/gas exhaustion on the `do_ft_withdraw` call which also causes the `require!` to fail via panicking) and permanently lose (burn) the `storage_deposit` amount of wNEAR from their own Defuse ledger balance with no possibility of recovery, since `Contract::token_balances` is never re-credited for that token id. This matches the "user funds permanently frozen" Critical category, though notably the funds destroyed here are the attacker's own wNEAR (self-inflicted), not another victim's — unless the attacker can force this outcome against another user's withdrawal (not evident from the code, since `owner_id` for a signed intent is always the signer). The value is not moved anywhere; it is destroyed against the ledger's own tracked balance vs. what was actually spent, breaking the internal conservation invariant, but it doesn't constitute theft from another party's authorized funds.

### Likelihood Explanation
Preconditions: attacker must hold wNEAR balance (deposited via `ft_transfer_call`), sign an `FtWithdraw` intent with `storage_deposit: Some(X)`, and reliably cause the `near_withdraw` or `storage_deposit` promise to fail (e.g., through gas manipulation, a receiver/token combination that rejects `storage_deposit`, or setting `min_gas` such that `do_ft_withdraw`'s attached gas plus the storage-deposit call causes the `require!` panic pathway or downstream call failure). This is self-inflicted loss and repeatable per attempt but each repetition destroys more of the attacker's own funds, and there is no way shown in the reachable code to direct this loss onto another user's balance since intents are signer-scoped.

### Recommendation
In `ft_resolve_withdraw` (and the analogous NFT/MT withdraw resolvers), also track and refund the wNEAR `storage_deposit` portion on failure of the wNEAR-side promises: pass the `storage_deposit` amount (if any) through to the resolver and add a re-credit to the `wnear_id` token balance whenever `do_ft_withdraw`/`near_withdraw`/`storage_deposit` promise fails, mirroring the existing refund logic for the primary token, rather than only refunding on the last promise's result.

### Proof of Concept
```rust
// tests/src/tests/defuse/intents/ft_withdraw.rs (new test)
// 1. Deposit wNEAR (storage_deposit amount) and primary ft tokens for `user`.
// 2. Deploy or select a `token` FT contract whose `storage_deposit` call will
//    reject the deposit for the given `receiver_id` (e.g. receiver not registered
//    and the token panics rather than allowing implicit registration, or attach
//    an intentionally too-low `min_gas` so the do_ft_withdraw `require!` fails).
// 3. Record `wnear_balance_before = mt_balance_of(user, wnear_token_id)`.
// 4. Sign and execute an `FtWithdraw` intent with `storage_deposit: Some(STORAGE_DEPOSIT)`
//    targeting the failing scenario.
// 5. Await full promise chain resolution.
// 6. Assert:
//    let wnear_balance_after = mt_balance_of(user, wnear_token_id);
//    assert_eq!(wnear_balance_after, wnear_balance_before - STORAGE_DEPOSIT.as_yoctonear());
//    // No corresponding wNEAR credit anywhere, proving permanent loss.
//    assert_eq!(ft.balance_of(&other_user_id).await.unwrap().raw(), 0); // token not delivered either if do_ft_withdraw failed
```

### Citations

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L27-50)
```rust
    fn ft_withdraw(
        &mut self,
        token: AccountId,
        receiver_id: AccountId,
        amount: U128,
        memo: Option<String>,
        msg: Option<String>,
    ) -> PromiseOrValue<U128> {
        assert_one_yocto();
        self.internal_ft_withdraw(
            self.ensure_auth_predecessor_id(),
            FtWithdraw {
                token,
                receiver_id,
                amount: amount.into(),
                memo,
                msg,
                storage_deposit: None,
                min_gas: None,
            },
            false,
        )
        .unwrap_or_else(|err| err.panic())
    }
```

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

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L76-105)
```rust
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
```

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L117-134)
```rust
    #[private]
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

**File:** contracts/defuse/core/src/intents/tokens.rs (L135-163)
```rust
pub struct FtWithdraw {
    pub token: AccountId,
    pub receiver_id: AccountId,
    #[serde_as(as = "DisplayFromStr")]
    pub amount: u128,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub memo: Option<String>,

    /// Message to pass to `ft_transfer_call`. Otherwise, `ft_transfer` will be used.
    /// NOTE: No refund will be made in case of insufficient `storage_deposit`
    /// on `token` for `receiver_id`
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub msg: Option<String>,

    /// Optionally make `storage_deposit` for `receiver_id` on `token`.
    /// The amount will be subtracted from user's NEP-141 `wNEAR` balance.
    /// NOTE: the `wNEAR` will not be refunded in case of fail
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub storage_deposit: Option<NearToken>,

    /// Optional minimum required Near gas for created Promise to succeed:
    /// * `ft_transfer`:      minimum: 15TGas, default: 15TGas
    /// * `ft_transfer_call`: minimum: 30TGas, default: 50TGas
    ///
    /// Remaining gas will be distributed evenly across all Function Call
    /// Promises created during execution of current receipt.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub min_gas: Option<Gas>,
}
```

**File:** contracts/defuse/core/src/engine/state/cached.rs (L257-271)
```rust
    fn ft_withdraw(&mut self, owner_id: &AccountIdRef, withdraw: FtWithdraw) -> Result<()> {
        self.internal_sub_balance(
            owner_id,
            std::iter::once((
                Nep141TokenId::new(withdraw.token.clone()).into(),
                withdraw.amount,
            ))
            .chain(withdraw.storage_deposit.map(|amount| {
                (
                    Nep141TokenId::new(self.wnear_id().into_owned()).into(),
                    amount.as_yoctonear(),
                )
            })),
        )
    }
```
