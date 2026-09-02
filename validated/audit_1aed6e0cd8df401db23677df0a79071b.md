### Title
Failed `near_withdraw` in `FtWithdraw` (storage_deposit) permanently burns signer's wNEAR balance - ([File: contracts/defuse/src/contract/tokens/nep141/withdraw.rs])

### Summary
`Contract::internal_ft_withdraw` debits both the FT amount and the wNEAR `storage_deposit` from the signer's Verifier balance synchronously, then asynchronously calls `near_withdraw` followed by `do_ft_withdraw`. If `near_withdraw` fails, `do_ft_withdraw`'s `require!(promise_result_checked_void(0).is_ok(), ...)` panics, but the subsequent `ft_resolve_withdraw` callback only refunds the withdrawn FT `token`, never the wNEAR `storage_deposit` amount, permanently freezing/burning the signer's wNEAR balance. Note: the file cited in the question (`contracts/defuse/src/contract/garbage_collector.rs`) is unrelated to this logic — the actual code lives in `contracts/defuse/src/contract/tokens/nep141/withdraw.rs`.

### Finding Description
The broken binding: `signer_wnear_balance_before - storage_deposit_debited == signer_wnear_balance_after + wnear_actually_consumed_by_near_withdraw`. This should always hold (either the wNEAR is consumed by a successful `near_withdraw`/`storage_deposit` flow, or it's refunded back to the signer). In practice it can be violated.

`internal_ft_withdraw` synchronously sub-balances both the FT `amount` and the wNEAR `storage_deposit.as_yoctonear()` from the signer via `self.withdraw(...)`: [1](#0-0) 

It then schedules `ext_wnear::near_withdraw` chained into `Self::do_ft_withdraw`, and that whole chain is further chained into `ft_resolve_withdraw`: [2](#0-1) 

`do_ft_withdraw` requires the `near_withdraw` result to be `Ok`, panicking otherwise, before it would call `storage_deposit`/`ft_transfer`: [3](#0-2) 

Because `do_ft_withdraw` is `#[private]` and scheduled via `.then()`, its panic fails only that receipt; NEAR still executes the chained `ft_resolve_withdraw` callback (this is standard resolve-pattern behavior). However `ft_resolve_withdraw` only inspects and refunds the FT `token`/`amount` — it has no logic to check or refund the wNEAR `storage_deposit`: [4](#0-3) 

So if `near_withdraw` fails (e.g., wNEAR contract paused, or the Verifier's on-chain wNEAR reserve is insufficient due to a race with other pending withdrawals in the same block/batch), the flow is:
1. Signer's Verifier balance loses `amount` (FT) + `storage_deposit` (wNEAR) atomically.
2. `near_withdraw` fails → `do_ft_withdraw` panics → no `storage_deposit`/`ft_transfer` executes.
3. `ft_resolve_withdraw` sees `promise_result_checked_void(0)` as failed (since the whole preceding promise chain failed) → `used = 0` → refunds the full FT `amount` back to signer via `self.deposit(...)`.
4. The wNEAR `storage_deposit` amount is **never** refunded, since `ft_resolve_withdraw` doesn't reference wNEAR/`storage_deposit` at all.

This matches the scoped claim: the FT leg is properly reconciled via existing refund logic, but the wNEAR leg used for `storage_deposit` has no corresponding refund path, resulting in permanent loss of signer funds without a matching failure elsewhere in the system (a Critical "user funds permanently frozen" condition, or equivalently, the Verifier retains custody of wNEAR that no signer can any longer claim).

### Impact Explanation
The signer's own wNEAR balance is permanently and irrecoverably debited by `storage_deposit.as_yoctonear()` any time `near_withdraw` fails during an `FtWithdraw` (or the analogous `NftWithdraw`/`MtWithdraw` paths, which share the identical pattern) with `storage_deposit` set. This is repeatable per attempt/account/token and requires no privileged role — any signer including the attacker against their own account, or (more concerning) triggered incidentally on other signers' payloads whenever wNEAR is paused/insufficient. The loss is the signer's own funds becoming permanently frozen inside the Verifier's wNEAR reserve, matching the Critical category "user funds permanently frozen."

### Likelihood Explanation
Triggering `near_withdraw` failure requires either: the wNEAR contract being paused (an external factor outside attacker control, since wNEAR is a separate contract not owned by the attacker), or a race condition where the Verifier's own wNEAR balance held at the wNEAR contract is insufficient relative to concurrent `storage_deposit` withdrawals in the same or nearby blocks. The attacker cannot directly force `near_withdraw` to fail against a healthy, well-funded wNEAR contract under normal operation, since the Verifier itself validated the signer had a sufficient wNEAR balance and the Verifier's `wnear_id` should hold that reserve unless something external (pause, third-party manipulation) intervenes. This makes exploitation largely dependent on external conditions (pause events) rather than something the attacker can reliably trigger deterministically by choosing `storage_deposit`, weakening — but not eliminating — the practical likelihood; the code path itself is confirmed non-defensive regardless of trigger cause.

### Recommendation
In `ft_resolve_withdraw` (and the analogous `nft_resolve_withdraw`/`mt_resolve_withdraw`), when the underlying withdraw chain failed, also refund the `storage_deposit` wNEAR amount back to the signer. This requires threading `storage_deposit: Option<NearToken>` and `owner_id`/wnear token id through to the resolver, or alternatively refunding in `do_ft_withdraw`'s failure branch before it panics (e.g., using `#[private]` self-callback that deposits back wNEAR on `near_withdraw` failure instead of unconditionally panicking).

### Proof of Concept
```rust
// near-workspaces sandbox test, in tests/src/tests/defuse/intents/ft_withdraw.rs style:
// 1. Pause the wNEAR contract (or drain the Verifier's wNEAR reserve at the wNEAR
//    contract via a concurrent withdrawal) so that `near_withdraw` will fail.
// 2. Fund `user`'s Verifier wNEAR balance with STORAGE_DEPOSIT and record
//    `wnear_balance_before = env.defuse.mt_balance_of(user, wnear_token_id)`.
// 3. Sign an FtWithdraw intent with `storage_deposit: Some(STORAGE_DEPOSIT)`.
// 4. Call `env.defuse_simulate_and_execute_intents(..., [payload])`.
// 5. Assert the FT amount balance IS refunded (existing correct behavior).
// 6. Assert `env.defuse.mt_balance_of(user, wnear_token_id) == wnear_balance_before`
//    — this assertion FAILS, proving wNEAR is permanently lost (balance ends up
//    STORAGE_DEPOSIT short of wnear_balance_before, with no refund path).
```

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

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L76-106)
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
    }
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
