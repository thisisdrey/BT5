### Title
Failed `storage_deposit` in NEP-141 withdraw is never refunded, permanently freezing the wNEAR debited for it - ([File: contracts/defuse/src/contract/tokens/nep141/withdraw.rs])

### Summary
`internal_ft_withdraw` atomically debits both the withdrawn FT `amount` and the wNEAR `storage_deposit` amount from the owner's internal balance, then unwraps the wNEAR to real NEAR via `near_withdraw` and attaches it to a `storage_deposit` call on the target `token` contract. If that `storage_deposit` call fails, its attached NEAR is refunded by the NEAR runtime to the defuse contract's own account balance, but `ft_resolve_withdraw` never checks or refunds the `storage_deposit` portion at all — it only tracks and refunds the FT `amount`.

### Finding Description
The broken binding is:
`wnear storage_deposit debited by Contract::withdraw == wnear consumed by successful external storage_deposit + wnear refunded to owner`

In `internal_ft_withdraw` ( [1](#0-0) ), the owner's internal balances for both the FT `amount` and the wNEAR `storage_deposit` are debited atomically in one `self.withdraw(...)` call.

The promise chain then runs `near_withdraw` (unwrap wNEAR → NEAR) `.then(do_ft_withdraw)` `.then(ft_resolve_withdraw)`. Inside `do_ft_withdraw` ( [2](#0-1) ), only the `near_withdraw` result (`promise_result_checked_void(0)`) is validated via `require!`. The subsequent `storage_deposit` call to the attacker-controlled `token` contract (line 126-131) is chained via `ext_ft_core::ext_on(p)` directly into `ft_transfer_call`/`ft_transfer` (line 136-150) — there is no callback on the defuse contract that inspects `storage_deposit`'s own promise result before proceeding. NEAR's `.then()` semantics execute the following call unconditionally regardless of whether the predecessor promise succeeded or failed.

`ft_resolve_withdraw` ( [3](#0-2) ) only receives `token`, `sender_id`, `amount`, `is_call` — it has no parameter for the `storage_deposit` amount and only inspects `promise_result(0)`, which is the result of the *last* scheduled call (`ft_transfer`/`ft_transfer_call`), not `storage_deposit`.

Exploit flow: caller invokes `ft_withdraw` with `storage_deposit = Some(NearToken)`, `msg = Some(msg)`, and `token` pointed at a contract they deploy themselves whose `storage_deposit` implementation always panics/rejects while `ft_transfer_call` always succeeds and reports `used = amount`. `near_withdraw` succeeds (unwraps the caller's own wNEAR), the `require!` passes, `storage_deposit` fails and its attached NEAR is refunded by the protocol to the defuse contract's own account (not to the caller's internal ledger), `ft_transfer_call` still executes and succeeds, and `ft_resolve_withdraw` computes `refund = amount - used = 0` for the FT token and never even considers the `storage_deposit` amount. The wNEAR debited for `storage_deposit` is permanently gone from the internal ledger and is now un-attributed real NEAR sitting in the defuse contract's own account balance.

### Impact Explanation
The withdrawing user's internal wNEAR balance is decreased by `storage_deposit` with no code path to credit it back, regardless of whether the external `storage_deposit` call to the target `token` contract succeeds or fails. Because this is a structural gap (`ft_resolve_withdraw` has no parameter or logic for the `storage_deposit` amount at all), it applies to every `ft_withdraw`/`ft_force_withdraw` call that sets `storage_deposit` and whose target token's `storage_deposit` fails — whether triggered deliberately with a malicious `token` contract or by an ordinary failure (insufficient attached deposit, paused registration, etc.). This matches the "user funds permanently frozen" Critical category, since the wNEAR is unrecoverable through any normal intents flow once converted to un-tracked NEAR.

### Likelihood Explanation
The precondition set is minimal and fully within an unprivileged attacker's control: they only need to call `ft_withdraw` (or have any other user do so against any `token` contract whose `storage_deposit` can fail) with `storage_deposit = Some(...)` and a `msg`. Deploying a token contract with a failing `storage_deposit` and successful `ft_transfer_call` requires no special privilege. No signature forgery, role, or victim key is needed — the bug is reachable through the documented public entrypoint `ft_withdraw`.

### Recommendation
Thread the `storage_deposit` amount and the intermediate `storage_deposit` promise result through to `ft_resolve_withdraw` (e.g., insert a private callback between `storage_deposit` and `ft_transfer_call`/`ft_transfer` that checks `promise_result_checked_void`, and pass that boolean plus the `storage_deposit` amount to `ft_resolve_withdraw`), refunding the wNEAR internal balance to `sender_id` whenever the `storage_deposit` call did not succeed.

### Proof of Concept
Sandbox test plan (`near-workspaces`):
1. Deploy the defuse contract and a malicious FT contract `evil_token` whose `storage_deposit` always panics and whose `ft_transfer_call` always succeeds, returning the full `used` amount.
2. Fund an account's internal balances: `evil_token` FT amount and wNEAR.
3. Call `ft_withdraw(token = evil_token, receiver_id = attacker_receiver, amount, memo = None, msg = Some("x"), storage_deposit = Some(NearToken::from_yoctonear(N)))` (via `ft_force_withdraw` with `UnrestrictedWithdrawer`/DAO role, or via the equivalent public flow with `storage_deposit`).
4. Assert: before call, internal wNEAR balance for the account = `B`; the FT `token_balances.amount_for(wnear_token_id)` decreases by `N` (per `Contract::withdraw`).
5. After the promise chain resolves, assert the account's internal wNEAR balance is still `B - N` (no refund credited), while the defuse contract's real NEAR account balance increased by approximately `N` (the refunded, un-attributed deposit from the failed `storage_deposit`) — proving `N` yoctoNEAR of the user's wNEAR is permanently lost from the internal ledger with no corresponding refund path in `ft_resolve_withdraw`.

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

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L117-151)
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
