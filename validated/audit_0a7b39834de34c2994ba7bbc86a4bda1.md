Confirmed vulnerability. Comparing `native_withdraw`/`do_native_withdraw` (contracts/defuse/src/contract/intents/state.rs and contracts/defuse/src/contract/tokens/nep141/native.rs) against every sibling withdraw path (`ft_withdraw`/`ft_resolve_withdraw`, `nft_withdraw`/`nft_resolve_withdraw`, `mt_withdraw`/`mt_resolve_withdraw`, `storage_deposit`) shows those all schedule a `*_resolve_*` callback that checks the final promise result and re-`deposit`s (refunds) the owner if the transfer failed [1](#0-0) , [2](#0-1) , [3](#0-2) . `native_withdraw`, however, only has a `do_native_withdraw` callback that panics on `near_withdraw` failure with no further resolver step and no re-credit to the owner: [4](#0-3) .

### Title
Debited wNEAR is not refunded when `near_withdraw`/`do_native_withdraw` promise chain fails - (File: contracts/defuse/src/contract/tokens/nep141/native.rs)

### Summary
`Contract::native_withdraw` synchronously debits the signer's wNEAR balance via `self.withdraw(...)` before firing an async `near_withdraw().then(do_native_withdraw)` chain, but unlike `ft_withdraw`, `nft_withdraw`, and `mt_withdraw`, there is no resolver callback that refunds the owner if that chain fails. If `near_withdraw` (called on the wNEAR contract) fails, `do_native_withdraw` panics via `require!` and the final `Promise::new(receiver_id).transfer(amount)` never executes, so the debited wNEAR is neither delivered to `receiver_id` nor returned to `owner_id`.

### Finding Description
The broken binding: `debited_wnear(owner_id) == delivered_near(receiver_id) + recredited_wnear(owner_id)` must hold for every `NativeWithdraw` intent execution.

Trace:
1. `Contract::native_withdraw` (`State` impl, contracts/defuse/src/contract/intents/state.rs:212-240) calls `self.withdraw(owner_id, [(wnear_token_id, amount)], ...)`, which is a synchronous state mutation (`internal_sub_balance`/token burn logic in `contracts/defuse/src/contract/tokens/mod.rs`). This happens and is committed regardless of what happens later in the same transaction's async callbacks.
2. It then fires `ext_wnear::near_withdraw(amount)` and chains `.then(do_native_withdraw(withdraw))`, `.detach()`-ing the result — no further `.then(...)` resolver is attached to this outer promise chain.
3. `do_native_withdraw` (contracts/defuse/src/contract/tokens/nep141/native.rs:11-19) does `require!(promise_result_checked_void(0).is_ok(), "near_withdraw failed")`. If `near_withdraw` fails (e.g., the wNEAR contract rejects due to insufficient wNEAR-contract-side balance, paused state, or any transient/external condition), this `require!` panics, and the callback function itself fails — the `Promise::new(withdraw.receiver_id).transfer(withdraw.amount)` line is never reached.
4. Because the chain is `.detach()`ed with no subsequent resolver reading `do_native_withdraw`'s outcome, there is no code path anywhere that re-credits `owner_id`'s wNEAR balance for the failed transfer.

Compare to `ft_withdraw`: `internal_ft_withdraw` in contracts/defuse/src/contract/tokens/nep141/withdraw.rs:53-106 always appends `.then(ft_resolve_withdraw(...))`, whose logic (lines 155-194) computes `used` from the promise result and calls `self.deposit(sender_id, refund, ...)` for the unused amount. `nft_withdraw` and `mt_withdraw` follow the identical resolver pattern. `native_withdraw` omits this resolver entirely, breaking the settlement invariant that funds debited from a signer must either be delivered or refunded.

Note: the receiver_id-panics-on-transfer scenario described in the question (plain `Promise::new(receiver_id).transfer(amount)` to a contract that panics on receiving a transfer) does not actually cause funds loss on NEAR, since a basic `transfer` action does not invoke any receiver logic and cannot fail due to receiver code — that specific framing is inaccurate. However, the real and demonstrable break is upstream: failure of the `near_withdraw` call itself (the wNEAR contract's cross-contract call) leaves the already-debited wNEAR balance permanently unaccounted for, with no refund path, regardless of what `receiver_id` is.

### Impact Explanation
wNEAR debited from `owner_id`'s Verifier balance is permanently lost if the `near_withdraw` call to the wNEAR contract fails for any reason after the intent has been settled/committed — it is neither delivered as NEAR to `receiver_id` nor refunded back into the signer's Verifier balance. This breaks the "debited == delivered + recredited" invariant and constitutes user funds permanently frozen/lost, matching the Critical impact category (user funds permanently frozen). The bug is repeatable for every `NativeWithdraw` intent whenever the downstream `near_withdraw` call fails; it does not require an adversarial receiver — any circumstance causing `near_withdraw` to return an error (e.g., wNEAR contract-imposed limits, or if the wNEAR contract itself is paused/misconfigured) triggers loss for the honest signer. Note that triggering `near_withdraw` to fail requires conditions on the wNEAR contract side that are not fully within the calling attacker's control in the general case, so the finding is best characterized as an insufficiently-guarded failure path, not a directly attacker-triggerable, repeatable drain against a chosen victim.

### Likelihood Explanation
Preconditions: an account has a nonzero wNEAR balance in the Defuse contract and submits a `NativeWithdraw` intent; the subsequent `near_withdraw` cross-contract call to the wNEAR contract must fail (e.g., due to wNEAR contract being paused, storage issues, or a bug/edge case in the wNEAR implementation). The attacker cannot force `near_withdraw` to fail purely from the Defuse-contract side against a targeted third party — it depends on the wNEAR contract's behavior — so this is not a freely repeatable drain-on-demand against arbitrary victims, but it is a real, reproducible loss-of-funds condition whenever the chain fails, and it affects the *signer's own funds* in the question's scenario (self-inflicted or triggerable via conditions that make `near_withdraw` fail), which matches the "funds of the signer" scope of the question.

### Recommendation
Add a `native_resolve_withdraw`-style `#[private]` callback attached via `.then(...)` after the `do_native_withdraw`/`transfer` promise, that checks the promise result and calls `self.deposit(owner_id, [(wnear_token_id, amount)], REFUND_MEMO)` to refund the signer if the NEAR transfer (or the preceding `near_withdraw`) did not succeed — mirroring the pattern already used in `ft_resolve_withdraw`, `nft_resolve_withdraw`, and `mt_resolve_withdraw`.

### Proof of Concept
```
cargo test -p tests native_withdraw_near_withdraw_failure_refund -- --nocapture
```
Plan (near-workspaces/sandbox, extending `tests/src/tests/defuse/intents/native_withdraw.rs`):
1. Deposit wNEAR into `other_user`'s Verifier balance as in the existing `native_withdraw_intent` test.
2. Force the `near_withdraw` promise to fail — e.g., by pausing/breaking the wNEAR contract call (if the sandbox wNEAR extension exposes a way to simulate failure) or by withdrawing more than the wNEAR contract's own liquid balance can support in a crafted scenario.
3. Sign and execute a `NativeWithdraw{receiver_id, amount}` intent for `other_user`.
4. Assert both sides of the binding:
   - `debited = initial_wnear_balance(other_user) - final_wnear_balance(other_user)` (should be `amount`, confirming debit happened).
   - `delivered = final_near_balance(receiver_id) - initial_near_balance(receiver_id)` (expect `0` since `near_withdraw` failed).
   - `recredited = final_wnear_balance(other_user)` restored value (expect `0` under current code, i.e. `debited != delivered + recredited`, proving the invariant is broken).
5. Test should fail against current code (no refund) and pass once a resolver refund is added.

### Citations

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

**File:** contracts/defuse/src/contract/tokens/nep141/native.rs (L7-19)
```rust
#[near]
impl Contract {
    pub(crate) const DO_NATIVE_WITHDRAW_GAS: Gas = Gas::from_tgas(12);

    #[private]
    pub fn do_native_withdraw(withdraw: NativeWithdraw) -> Promise {
        require!(
            promise_result_checked_void(0).is_ok(),
            "near_withdraw failed",
        );

        Promise::new(withdraw.receiver_id).transfer(withdraw.amount)
    }
```
