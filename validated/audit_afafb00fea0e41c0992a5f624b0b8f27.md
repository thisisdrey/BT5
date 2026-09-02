### Title
`auth_call()` debits signer's wNEAR balance without any refund path when `near_withdraw` fails - (File: `contracts/defuse/src/contract/intents/state.rs:303`)

### Finding Description
The broken binding: `debited_amount (internal_sub_balance in self.withdraw)` should equal `refunded_amount (credited back to signer_id on near_withdraw failure)`, but no code path enforces this for `auth_call`.

In `auth_call()` [1](#0-0) , when `attached_deposit` is nonzero, `self.withdraw(...)` is called first, which internally calls `internal_sub_balance` [2](#0-1)  and commits the debit to persistent contract state in the *current* receipt, before any promise executes. Only after that does the code schedule `near_withdraw().then(do_auth_call(...))` as a detached async promise chain [3](#0-2) .

`do_auth_call` is the sole callback in that chain [4](#0-3) . If `near_withdraw` fails (e.g. wnear contract paused, or insufficient backing), `promise_result_checked_void(0).is_ok()` is `false`, and `require!(..., "near_withdraw failed")` panics. On NEAR, a panic in a receipt only reverts state changes made *within that same receipt* — it cannot undo the `internal_sub_balance` debit that was already committed and persisted in the earlier, already-finalized receipt where `auth_call` executed. There is no `*_resolve_*` callback analogous to `ft_resolve_withdraw` (which does `self.deposit(sender_id, ..., REFUND_MEMO)` on failure [5](#0-4) ) anywhere in the `auth_call` chain. `do_native_withdraw` and `do_storage_deposit` have the identical pattern (debit-then-panic-with-no-refund) [6](#0-5) [7](#0-6) , confirming this is a systemic gap rather than an isolated typo, but the question specifically concerns `auth_call`.

Attacker/trigger: any unprivileged user can trigger this simply by submitting an `AuthCall` intent with nonzero `attached_deposit` at a time when the `wnear` contract's `near_withdraw` fails (paused, insufficient NEAR backing due to any prior imbalance, or gas/attached-deposit issues on the cross-contract call). The signer does not need to be malicious — this can happen to any legitimate user, and can also be deliberately induced/observed by an attacker who controls timing or who can cause wnear to reject the withdrawal (e.g., front-running a pause, or by first draining the wnear contract's real NEAR balance through other legitimate operations that are within their own control).

### Impact Explanation
The signer's internal `token_balances` for the wNEAR `TokenId` is permanently reduced by `attached_deposit.as_yoctonear()` with no compensating credit, while the real wNEAR/NEAR was never withdrawn (since `near_withdraw` itself failed) — meaning the Verifier's book-keeping no longer matches reality and the user's funds are permanently frozen/lost from their perspective. This matches the Critical category: "user funds permanently frozen" — a batch's balance changes do not net to zero (value disappears with no corresponding settlement). This is repeatable per-account, per `AuthCall` attempt, any time `near_withdraw` can be made to fail.

### Likelihood Explanation
Preconditions: the wnear contract must reject `near_withdraw` for the given amount (pause, insufficient backing, gas issues) at the time the async chain runs. This is not fully under the attacker's control in isolation, but is a plausible operational failure mode (state described in the question, e.g. wnear paused or backing imbalance) and requires no special privilege — any user issuing an `AuthCall` with nonzero `attached_deposit` is exposed. No DAO/role/relayer access needed.

### Recommendation
Add a resolver step analogous to `ft_resolve_withdraw`: on `do_auth_call`'s `near_withdraw` failure, instead of (or before) panicking, credit back `attached_deposit` to `signer_id`'s wNEAR balance via `self.deposit(...)` and return/log gracefully rather than allowing the debit to remain uncompensated. Apply the same fix to `do_native_withdraw` and `do_storage_deposit`.

### Proof of Concept
`cargo test` plan (near-workspaces sandbox):
1. Deploy `defuse` contract with a mock/controllable `wnear` contract that can be toggled to reject `near_withdraw` calls (e.g., via pause or by making the mock panic).
2. Fund `signer_id`'s internal wNEAR balance via a normal deposit; assert `balance_of(signer_id, wnear_token_id) == N`.
3. Toggle the wnear mock to fail `near_withdraw`.
4. Submit a signed `MultiPayload` containing an `AuthCall` intent with `attached_deposit = M` (`M <= N`) targeting an arbitrary receiver contract.
5. Execute via `execute_intents`; await promise resolution.
6. Assert: `do_auth_call` receipt panics with `"near_withdraw failed"`.
7. Assert the broken binding: `balance_of(signer_id, wnear_token_id) == N - M` (debited) while no `deposit`/refund event was ever emitted for `signer_id`, proving `debited_amount (M) != refunded_amount (0)`, i.e., `M` yoctoNEAR-equivalent wNEAR is permanently lost from the signer's Verifier balance despite no successful withdrawal.

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

**File:** contracts/defuse/src/contract/intents/auth_call.rs (L17-24)
```rust
    #[private]
    pub fn do_auth_call(signer_id: AccountId, auth_call: AuthCall) -> Promise {
        if !auth_call.attached_deposit.is_zero() {
            require!(
                promise_result_checked_void(0).is_ok(),
                "near_withdraw failed",
            );
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

**File:** contracts/defuse/src/contract/tokens/nep141/storage_deposit.rs (L13-18)
```rust
    #[private]
    pub fn do_storage_deposit(storage_deposit: StorageDeposit) -> Promise {
        require!(
            promise_result_checked_void(0).is_ok(),
            "near_withdraw failed",
        );
```
