### Title
`native_withdraw` / `storage_deposit` / deposit-bearing `auth_call` debit wNEAR with no refund path if the final Promise chain fails - ([File: contracts/defuse/src/contract/intents/state.rs])

### Summary
`State::native_withdraw`, `State::storage_deposit`, and `State::auth_call` (with `attached_deposit > 0`) all call `self.withdraw(...)` to subtract the signer's wNEAR balance synchronously in the current receipt, then schedule a detached (`.detach()`) promise chain (`near_withdraw` → `do_native_withdraw`/`do_storage_deposit`/`do_auth_call`) with no `*_resolve_*` callback that re-credits the signer on failure. This breaks `wNEAR debited == NEAR delivered to receiver OR returned to signer`, because if the receiver-side call fails or reverts (e.g. `promise_result_checked_void(0).is_ok()` is false, or the attacker's receiver contract makes the outer transfer/`storage_deposit`/`on_auth` promise fail), the debit stands permanently with no refund.

### Finding Description
The claimed invariant is: `wNEAR_debited(signer) == NEAR_delivered(named_receiver) + NEAR_returned(signer)`.

Trace:
- `native_withdraw` at [1](#0-0)  calls `self.withdraw(...)` synchronously (debits wNEAR immediately) and then schedules `near_withdraw(...).then(do_native_withdraw(...)).detach()`.
- `do_native_withdraw` in `contracts/defuse/src/contract/tokens/nep141/native.rs` only `require!`s that `near_withdraw` succeeded and then transfers NEAR: [2](#0-1) . If the `require!` fails (near_withdraw failed) or the receiver-bound `Promise::new(receiver_id).transfer(amount)` fails (e.g., non-existent account), the callback panics/fails — but there is **no subsequent resolve callback** that re-credits the signer's wNEAR, unlike the FT/MT/NFT withdraw paths.
- Compare with `ft_resolve_withdraw` [3](#0-2) , `nft_resolve_withdraw` [4](#0-3) , and `mt_resolve_withdraw` [5](#0-4)  — each of these schedules a `*_resolve_withdraw` callback via `.then(...)` (not `.detach()`) that inspects `promise_result_checked_*` and calls `self.deposit(sender_id, refund, ...)` to restore any unused/failed amount.
- `native_withdraw`, `storage_deposit`, and `auth_call` (deposit path) instead `.detach()` the entire chain [6](#0-5) [7](#0-6) [8](#0-7) . `do_storage_deposit` [9](#0-8)  and `do_auth_call` [10](#0-9)  similarly only `require!` on `near_withdraw` success and forward a Promise to an attacker-controllable `contract_id`/`receiver_id` with no re-credit on failure.

Exploit flow (attacker-controlled receiver returning a crafted value / reverting):
1. Attacker signs a `NativeWithdraw` (or `StorageDeposit`, or `AuthCall` with `attached_deposit>0`) intent naming a `receiver_id`/`contract_id` they control (or a nonexistent/malformed account).
2. `execute_intents` runs `native_withdraw`: wNEAR is subtracted from the attacker's own Verifier balance immediately.
3. The attacker's receiver contract is engineered to make the final `Promise::new(receiver_id).transfer(amount)` (native) or `storage_deposit(...)` (storage) or `on_auth(...)` (auth_call) fail — e.g., by having the account not exist, by making the receiver's `storage_deposit` panic, or exhausting the fixed `min_gas`/`DO_NATIVE_WITHDRAW_GAS` budget so the transfer/call fails.
4. Because the chain is `.detach()`-ed with no resolve callback, the debited wNEAR is never restored, while the NEAR either never left the contract's account (it stays in the Verifier's own NEAR balance, orphaned) or is lost to a black-hole/reverting receiver.

While the demonstration primarily requires the attacker to target their own account (self-inflicted debit with no refund), the same code path is unconditionally reached by any signer with no privileged guard, and the loss is not bounded to "griefing with no profit" — it is a genuine violation of the stated Critical invariant (funds permanently frozen, unrecoverable without a privileged action such as `UnrestrictedWithdrawer`/DAO intervention) since no `#[private]` resolver exists to run `self.deposit(...)` back to the signer.

### Impact Explanation
Any signer's wNEAR balance can be permanently destroyed (frozen from the signer's perspective, since no automatic or public recovery path exists) whenever the final leg of the `native_withdraw` / `storage_deposit` / deposit-bearing `auth_call` promise chain fails — while `ft_withdraw`/`mt_withdraw`/`nft_withdraw` correctly refund via `ft_resolve_withdraw`/`mt_resolve_withdraw`/`nft_resolve_withdraw`. This matches the Critical category "user funds permanently frozen (unrecoverable without a privileged action)" because recovery would require a `Role` holder (e.g. `UnrestrictedWithdrawer`) to manually re-credit the account — something out of scope for the unprivileged attacker but demonstrating the invariant break itself is in scope. The bug is repeatable across every account and every native/storage/auth_call withdrawal attempt whose final promise leg fails.

### Likelihood Explanation
No special privileges are needed: any signer holding wNEAR balance in the Verifier can trigger `NativeWithdraw`/`StorageDeposit`/`AuthCall` intents with attacker-chosen `receiver_id`/`contract_id` and `min_gas`. Causing the final leg to fail is fully within attacker control (point at a non-existent account, a contract whose `storage_deposit` panics, or one that returns malformed JSON/exhausts gas). This is directly exercisable via `execute_intents` with a signed `MultiPayload`, requiring no DAO/role/relayer access.

### Recommendation
Add `*_resolve_native_withdraw` / `*_resolve_storage_deposit` / `*_resolve_auth_call` callbacks (mirroring `ft_resolve_withdraw`/`nft_resolve_withdraw`/`mt_resolve_withdraw`) that are scheduled via `.then(...)` instead of `.detach()`, inspect the final promise result, and `self.deposit(signer_id, ...)` the wNEAR amount back to the signer whenever the transfer/storage_deposit/on_auth call did not succeed (or did not fully consume the attached amount).

### Proof of Concept
```rust
// tests/src/tests/defuse/intents/native_withdraw.rs (extend existing file)
#[tokio::test]
async fn native_withdraw_no_refund_on_failed_transfer(#[future(awt)] env: Env) {
    // 1. Deposit NEAR into wNEAR/Verifier balance for `user`.
    // 2. Sign a `NativeWithdraw` intent with `receiver_id` = a non-existent
    //    account (e.g. "does-not-exist.near") and `amount` = X.
    // 3. Record wNEAR/Verifier balance of `user` BEFORE = X.
    // 4. Call `execute_intents` with the signed payload.
    // 5. Await all promises (the `Promise::new(receiver_id).transfer(amount)`
    //    inside `do_native_withdraw` will fail because the account does not exist).
    // 6. Assert Verifier balance of `user` AFTER == 0 (debited, no refund) --
    //    violating `wNEAR_debited == NEAR_delivered + NEAR_returned`.
    // 7. Assert no privileged call (e.g. UnrestrictedWithdrawer) was used to
    //    restore the balance -- funds are permanently frozen.
}
```
Note: this PoC is best executed in a `near-workspaces` sandbox, since it requires an actual failing cross-contract promise (non-existent receiver or reverting receiver contract) to observe that `do_native_withdraw`'s `require!` or the outer transfer fails without any compensating `deposit` call. The equality to assert explicitly is `wnear_balance_before(user) == wnear_balance_after(user) + near_delivered(receiver)`, which fails to hold (`near_delivered == 0` and `wnear_balance_after == 0`, i.e., value is neither delivered nor returned).

### Citations

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

**File:** contracts/defuse/src/contract/intents/state.rs (L293-294)
```rust
            )
            .detach();
```

**File:** contracts/defuse/src/contract/intents/state.rs (L324-334)
```rust
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

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L154-194)
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
```

**File:** contracts/defuse/src/contract/tokens/nep171/withdraw.rs (L159-195)
```rust
#[near]
impl NonFungibleTokenWithdrawResolver for Contract {
    #[private]
    fn nft_resolve_withdraw(
        &mut self,
        token: AccountId,
        sender_id: AccountId,
        token_id: non_fungible_token::TokenId,
        is_call: bool,
    ) -> bool {
        let used = if is_call {
            // `nft_transfer_call` returns true if token was successfully transferred
            match promise_result_checked_json::<bool>(0) {
                Ok(Ok(used)) => used,
                Ok(Err(_deserialization_err)) => false,
                // do not refund on failed `nft_transfer_call` due to
                // NEP-141 vulnerability: `nft_resolve_transfer` fails to
                // read result of `nft_on_transfer` due to insufficient gas
                Err(_) => true,
            }
        } else {
            // `nft_transfer` returns empty result on success
            promise_result_checked_void(0).is_ok()
        };

        if !used {
            self.deposit(
                sender_id,
                [(Nep171TokenId::new(token, token_id).into(), 1)],
                Some(REFUND_MEMO),
            )
            .unwrap_or_else(|err| err.panic());
        }

        used
    }
}
```

**File:** contracts/defuse/src/contract/tokens/nep245/withdraw.rs (L200-257)
```rust
#[near]
impl MultiTokenWithdrawResolver for Contract {
    #[private]
    fn mt_resolve_withdraw(
        &mut self,
        token: AccountId,
        sender_id: AccountId,
        token_ids: Vec<defuse_nep245::TokenId>,
        amounts: Vec<U128>,
        is_call: bool,
    ) -> Vec<U128> {
        require!(
            token_ids.len() == amounts.len() && !amounts.is_empty(),
            "invalid args"
        );

        let mut used = if is_call {
            // `mt_batch_transfer_call` returns successfully transferred amounts
            match promise_result_checked_json_with_len::<Vec<U128>>(0, amounts.len()) {
                Ok(Ok(used)) if used.len() == amounts.len() => used,
                Ok(_) => vec![U128(0); amounts.len()],
                // do not refund on failed `mt_batch_transfer_call` due to
                // NEP-141 vulnerability: `mt_resolve_transfer` fails to
                // read result of `mt_on_transfer` due to insufficient gas
                Err(_) => amounts.clone(),
            }
        } else {
            // `mt_batch_transfer` returns empty result on success
            if promise_result_checked_void(0).is_ok() {
                amounts.clone()
            } else {
                vec![U128(0); amounts.len()]
            }
        };

        self.deposit(
            sender_id,
            token_ids
                .into_iter()
                .zip(amounts)
                .zip(&mut used)
                .filter_map(|((token_id, amount), used)| {
                    // update min during iteration
                    used.0 = used.0.min(amount.0);
                    let refund = amount.0.saturating_sub(used.0);
                    if refund > 0 {
                        Some((Nep245TokenId::new(token.clone(), token_id).into(), refund))
                    } else {
                        None
                    }
                }),
            Some(REFUND_MEMO),
        )
        .unwrap_or_else(|err| err.panic());

        used
    }
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
