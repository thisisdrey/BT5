Based on the code, this vulnerability is confirmed. Let's establish the binding precisely.

**Binding:** `internal_ft_withdraw` debits `token_balances[owner][wnear] -= storage_deposit.amount` alongside `token_balances[owner][ft_token] -= amount` in one atomic `self.withdraw(...)` call [1](#0-0) . The only credit-back path after promise failure is `ft_resolve_withdraw`, whose signature carries `token`, `sender_id`, `amount`, `is_call` — **no `storage_deposit` parameter at all** — and its refund logic only ever calls `self.deposit(sender_id, [(Nep141TokenId::new(token).into(), refund)], ...)` for the FT token, never for wnear [2](#0-1) .

Tracing the failure path: when `storage_deposit` is `Some`, the chain is `near_withdraw` → `do_ft_withdraw` → `ft_resolve_withdraw` [3](#0-2) . If the cross-contract `near_withdraw` call to the wnear contract fails on-chain (e.g., insufficient real NEAR reserves backing the internal wnear ledger, which is a distinct failure mode from the internal ledger check that already succeeded), `do_ft_withdraw`'s `require!(promise_result_checked_void(0).is_ok(), "near_withdraw failed")` panics [4](#0-3) . This makes the `do_ft_withdraw` receipt itself fail, and `ft_resolve_withdraw` runs as its callback, seeing a failed promise result at index 0 and refunding only the FT `amount` (or nothing if `is_call` and deserialization succeeds to an `Err`) — the wnear `storage_deposit` amount that was debited in step one is never mentioned or credited anywhere in the resolver [5](#0-4) .

This differs from the existing test `ft_withdraw_intent`, which only covers the case where the internal ledger lacks wnear balance — causing `internal_sub_balance` to fail synchronously before any promise is scheduled, reverting everything and returning `unwrap_err()` [6](#0-5) . It does not cover the scenario where the internal balance is sufficient but the actual asynchronous `near_withdraw` call to the wnear contract fails after the debit is already committed as part of the receipt — that scenario has no test coverage and no refund path.

### Title
Wnear `storage_deposit` debited in `internal_ft_withdraw` is never refunded when `near_withdraw` fails - (File: contracts/defuse/src/contract/tokens/nep141/withdraw.rs)

### Summary
`internal_ft_withdraw` atomically debits both the FT `amount` and the wnear `storage_deposit` amount from the owner's Verifier balance before any promise resolves. If the subsequent `near_withdraw` cross-contract call fails, `do_ft_withdraw` panics via `require!`, and the terminal callback `ft_resolve_withdraw` only refunds the FT `amount` — it has no parameter or logic to credit back the wnear `storage_deposit` debit, causing a silent, permanent loss of the withdrawer's wnear balance.

### Finding Description
The broken binding is: `wnear_debited_in_internal_ft_withdraw == wnear_delivered_to_contract + wnear_recredited_by_resolver`. Before: `wnear_debited = storage_deposit.amount`, `wnear_delivered = 0` (near_withdraw failed), `wnear_recredited = 0` (no path). After: the equality fails since `storage_deposit.amount != 0`.

Code path: `internal_ft_withdraw` subtracts both the FT token amount and, if `withdraw.storage_deposit` is `Some`, the wnear amount, in a single `self.withdraw(...)` call built via `iter::once(...).chain(...)` [1](#0-0) . It then schedules `near_withdraw` → `do_ft_withdraw` → `ft_resolve_withdraw` [7](#0-6) . `do_ft_withdraw` requires the `near_withdraw` promise result to be `Ok`, else it panics with `"near_withdraw failed"` [4](#0-3) . `ft_resolve_withdraw` is declared and called with only `token, sender_id, amount, is_call` — `withdraw.token` (the FT), never the wnear id or `storage_deposit` amount [8](#0-7) [9](#0-8) . Its body only ever deposits back into the FT `token_id`, never a wnear `Nep141TokenId` [10](#0-9) .

Attacker payload: sign a `DefuseIntents` with `FtWithdraw { token: <owned_ft>, receiver_id: attacker, amount: X, storage_deposit: Some(near_amount), .. }`, having enough internal ledger balance of both the FT and wnear. If the real on-chain `near_withdraw` call to the wnear contract fails (e.g., contract's real backing NEAR is momentarily insufficient, or the wnear contract itself rejects the withdrawal), the internal debit of the wnear `storage_deposit` amount that already happened synchronously in `internal_ft_withdraw` is never reversed.

The existing test `ft_withdraw_intent` only demonstrates the case where the internal ledger check itself fails before any promise fires (returns `Err` synchronously, no state change committed) [6](#0-5) , which is a different, safe case. It does not cover asynchronous `near_withdraw` promise failure after the debit is already committed.

### Impact Explanation
The withdrawer's wnear balance equal to `storage_deposit.amount` is destroyed with no possibility of recovery — it is debited from the Verifier ledger but never delivered to any external contract and never recredited by any resolver. This is a "user funds permanently frozen" condition, matching the Critical impact category. It is repeatable for every `FtWithdraw` (and analogously `NftWithdraw`/`MtWithdraw`, which share the same `storage_deposit` chaining pattern) that specifies a non-`None` `storage_deposit` whenever the asynchronous `near_withdraw` fails.

### Likelihood Explanation
Requires: attacker has sufficient internal Verifier balance of the FT token and wnear to pass the synchronous `internal_sub_balance` check, and the real `near_withdraw` cross-contract call subsequently fails for any reason (contract-level, not ledger-level) — e.g., transient NEAR balance shortfall in the wnear contract's real reserves, gas issues at that receipt, or any other legitimate on-chain failure of `near_withdraw`. This does not require any privileged role and can be self-inflicted by any account with a Verifier deposit and controlling `storage_deposit`, or could be triggered by conditions outside the attacker's direct control but that still destroy user funds (making it a genuine bug rather than solely attacker-triggerable, but demonstrable in a sandboxed test by forcing `near_withdraw` to fail).

### Recommendation
Pass the wnear `storage_deposit` amount (and wnear token id) into `ft_resolve_withdraw` (and the analogous `nft_resolve_withdraw`/`mt_resolve_withdraw`), and check the result of the `near_withdraw`/`do_ft_withdraw` promise there to refund the wnear amount back to `sender_id` whenever `storage_deposit` was requested but the withdrawal sequence failed before delivering it.

### Proof of Concept
`cargo test` (near-workspaces sandbox) plan:
1. Set up `Env` with a Verifier, wnear contract, and FT contract, per `tests/src/tests/defuse/intents/ft_withdraw.rs`.
2. Fund `user` with FT and wnear balances inside the Verifier (deposit sufficiently, as in the existing `ft_withdraw_intent` test setup).
3. Force `near_withdraw` on the wnear contract to fail for the do_ft_withdraw receipt (e.g., by manipulating the wnear contract's real NEAR reserve, or using a stub/mock wnear contract that always rejects `near_withdraw` above some threshold, while still allowing the Verifier's internal ledger check on `storage_deposit` to pass).
4. Sign and execute an `FtWithdraw` intent with `storage_deposit: Some(STORAGE_DEPOSIT)`.
5. Assert: (a) the call/receipt for `near_withdraw`/`do_ft_withdraw` fails as expected; (b) `mt_balance_of` for the user's FT token id is refunded back to the pre-withdraw amount (confirming `ft_resolve_withdraw` refunds the FT side); (c) `mt_balance_of` for the wnear token id remains debited by `STORAGE_DEPOSIT` and is NOT restored to its pre-withdraw value — proving the wnear storage_deposit amount is permanently lost with no resolver crediting it back.

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

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L118-124)
```rust
    pub fn do_ft_withdraw(withdraw: FtWithdraw) -> Promise {
        let min_gas = withdraw.min_gas();
        let p = if let Some(storage_deposit) = withdraw.storage_deposit {
            require!(
                promise_result_checked_void(0).is_ok(),
                "near_withdraw failed",
            );
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

**File:** tests/src/tests/defuse/intents/ft_withdraw.rs (L105-124)
```rust
    let missing_storage_payload = user
        .sign_defuse_payload_default(
            &env.defuse,
            [FtWithdraw {
                token: ft.contract_id().clone(),
                receiver_id: other_user_id.clone(),
                amount: 1000,
                memo: None,
                msg: None,
                // user has no wnear yet
                storage_deposit: Some(STORAGE_DEPOSIT),
                min_gas: None,
            }],
        )
        .await
        .unwrap();

    env.defuse_simulate_and_execute_intents(env.defuse.contract_id(), [missing_storage_payload])
        .await
        .unwrap_err();
```

**File:** contracts/defuse/src/tokens/nep141.rs (L23-31)
```rust
#[ext_contract(ext_ft_withdraw_resolver)]
pub trait FungibleTokenWithdrawResolver {
    fn ft_resolve_withdraw(
        &mut self,
        token: AccountId,
        sender_id: AccountId,
        amount: U128,
        is_call: bool,
    ) -> U128;
```
