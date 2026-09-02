### No vulnerability found for this question.

This is documented, intentional behavior rather than a hidden flaw. The `NativeWithdraw` intent's doc comment explicitly states: "NOTE: the `wNEAR` will not be refunded in case of fail (e.g. `receiver_id` account does not exist)." [1](#0-0) 

The mechanics match the question's description: `native_withdraw` synchronously debits the signer's wNEAR balance via `internal_sub_balance`/`withdraw` before scheduling the cross-contract `near_withdraw` call chained to `do_native_withdraw` [2](#0-1) , and `do_native_withdraw` indeed has no refund path — it only checks the prior promise result and panics via `require!` if `near_withdraw` failed, with no `self.deposit` call unlike `ft_resolve_withdraw` [3](#0-2) .

However, this does not qualify as a valid finding under the audit rules:

1. **No unauthorized value movement**: The signer is withdrawing their own funds by their own signed request. No other party's funds are moved, credited, or frozen without authorization — the loss (if any) only affects the signer who initiated the exact withdrawal, using their own signature.
2. **Out-of-scope failure modes**: The question itself excludes the only realistic ways to make `near_withdraw` fail (wNEAR being paused/upgraded requires DAO action, and wNEAR is 1:1 backed so insufficient-supply failures aren't attacker-controlled). Since Defuse's own wNEAR balance backing the debited amount is already held 1:1 in the wNEAR contract, `near_withdraw` unwrapping the contract's own tokens has no attacker-reachable failure path within scope.
3. **No attacker profit, no cross-account impact**: The rules explicitly exclude "griefing with no attacker profit" and "theoretical findings with no demonstration." There is no reachable/reproducible scenario within the stated scope (excluding DAO-gated pause/upgrade) that lets an unprivileged attacker trigger this on their own or another signer's funds for gain.

Since the only demonstrable trigger conditions are excluded by the question's own scoping, and the behavior is a documented design tradeoff affecting only the signer's own self-authorized withdrawal (not the Verifier's solvency or another party's funds), this does not meet the Critical/High impact bar required (e.g., "value leaving the Verifier that the signer never authorised").

### Citations

**File:** contracts/defuse/core/src/intents/tokens.rs (L428-435)
```rust
/// Withdraw native tokens (NEAR) from the intents contract to a given external account id (external being outside of intents).
/// This will subtract from the account's wNEAR balance, and will be sent to the account specified as native NEAR.
/// NOTE: the `wNEAR` will not be refunded in case of fail (e.g. `receiver_id`
/// account does not exist).
pub struct NativeWithdraw {
    pub receiver_id: AccountId,
    pub amount: NearToken,
}
```

**File:** contracts/defuse/src/contract/intents/state.rs (L212-239)
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
