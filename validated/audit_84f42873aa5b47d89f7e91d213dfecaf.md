### Title
`do_auth_call` panics on failed `near_withdraw`, permanently burning the signer's already-debited wNEAR - ([File: contracts/defuse/src/contract/intents/auth_call.rs])

### Summary
The `AuthCall` flow in the `defuse` contract debits a signer's internal wNEAR balance *before* the corresponding native NEAR is actually unwrapped and delivered. If the subsequent `near_withdraw` cross-contract call fails for any reason, `do_auth_call` deliberately `require!`s success and panics, so the auth-call side-effect (and any refund) never happens — mirroring the reported Stargate `sgReceive()`/`require` pattern where a downstream revert leaves value permanently stranded relative to the balance already deducted from the user.

### Finding Description
`auth_call()` in `contracts/defuse/src/contract/intents/state.rs` first synchronously debits the signer's internal wNEAR balance via `self.withdraw(...)`: [1](#0-0) 

This state mutation commits in the *current* receipt regardless of what happens afterward. It then schedules an async promise chain: `ext_wnear::near_withdraw(...)` followed by `.then(do_auth_call(...))`: [2](#0-1) 

In `do_auth_call`, if `attached_deposit` is non-zero, the code requires that the `near_withdraw` promise result was `Ok`, and panics otherwise: [3](#0-2) 

Because the internal balance was already subtracted from the signer's account **before** this promise chain runs (in a separate, already-finalized receipt), a failure of `near_withdraw` — e.g. due to the wNEAR contract being redeployed/upgraded, a storage-registration edge case, or any other transient/permanent failure on the wNEAR side — causes `do_auth_call` to panic. The panic aborts only the `do_auth_call` receipt; it does **not** roll back the earlier `withdraw()` state change from the initiating receipt. The result: the signer's internal wNEAR balance is permanently reduced, no native NEAR is ever unwrapped/delivered (the `on_auth` call to `auth_call.contract_id` never fires), and there is no refund path back to the signer — a clean case of "value debited without value delivered or refunded."

This is structurally identical to the reported Stargate issue: a `require()` gating a downstream action on the success of an external call whose failure mode was assumed unlikely, but whose realization causes an asymmetric, unrecoverable loss because compensating state was already committed upstream.

### Impact Explanation
This breaks the conservation invariant "value debited == value delivered + value refunded." If `near_withdraw` fails, the signer's wNEAR balance is burned inside the `defuse` contract with no corresponding NEAR ever created/attached to `on_auth`, and no refund credit is issued back to the signer's balance. This is a Critical-class impact per the given classification (funds effectively withdrawn from a user's balance with no valid delivery — analogous to "funds permanently frozen"/lost).

### Likelihood Explanation
Likelihood depends on how often `near_withdraw` can fail on the configured `wnear_id` contract (e.g., insufficient storage registration for the `defuse` contract account on the wNEAR side, gas exhaustion, or if the wNEAR contract is ever redeployed/upgraded with a changed interface) — this mirrors the original report's caveat that estimating the likelihood of an external dependency's failure mode is inherently uncertain. I could not verify from the available code whether `near_withdraw` failures are otherwise guarded against elsewhere (e.g., pre-flight storage checks), which would reduce likelihood; this should be confirmed by someone with deeper knowledge of the wNEAR/`ext_wnear` integration and NEAR runtime guarantees around storage registration for `near_withdraw` callers.

### Recommendation
Do not let `do_auth_call` panic on a failed `near_withdraw`. Instead, on failure, credit the signer's internal wNEAR balance back (mirroring the refund pattern used in `ft_resolve_withdraw`/`mt_resolve_withdraw`/`nft_resolve_withdraw` elsewhere in this codebase, e.g. [4](#0-3) ), and return an error/no-op instead of invoking `on_auth`, so that the debited amount is restored rather than lost.

### Proof of Concept
1. A signer submits an `AuthCall` intent with non-zero `attached_deposit`, targeting some `contract_id`.
2. `auth_call()` calls `self.withdraw(signer_id, [(wnear_token, attached_deposit)], ...)`, immediately reducing the signer's internal wNEAR balance — this state change is committed in the originating receipt.
3. The scheduled promise `ext_wnear::near_withdraw(...)` fails (for any external reason on the wNEAR contract's side).
4. `do_auth_call` executes as the `.then()` callback, sees `promise_result_checked_void(0)` is `Err`, and panics via `require!(..., "near_withdraw failed")`.
5. The panic aborts the `do_auth_call` receipt only; the earlier balance deduction from step 2 remains committed.
6. Result: the signer permanently lost `attached_deposit` worth of wNEAR balance, `on_auth` was never invoked, and no refund was issued.

(Note: I was not able to fully verify all preconditions under which `near_withdraw` can realistically fail on the specific `wnear_id` contract used in this deployment, nor whether any other layer intercepts/refunds this failure before reaching `do_auth_call`. A deeper live/test-based investigation of the `ext_wnear` integration would be needed to conclusively establish real-world reachability.)

### Citations

**File:** contracts/defuse/src/contract/intents/state.rs (L303-317)
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

```

**File:** contracts/defuse/src/contract/intents/state.rs (L318-334)
```rust
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

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L154-196)
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
