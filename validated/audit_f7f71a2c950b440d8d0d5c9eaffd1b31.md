### Title
Native NEAR withdrawals permanently burn user balance when `near_withdraw` or the final NEAR transfer fails - (File: contracts/defuse/src/contract/intents/state.rs, contracts/defuse/src/contract/tokens/nep141/native.rs)

### Summary
`NativeWithdraw` debits a user's internal wNEAR-backed balance synchronously and only afterwards attempts, via cross-contract promises, to unwrap wNEAR into native NEAR and transfer it to the receiver. If either the `near_withdraw` call or the final `Promise::new(receiver_id).transfer(...)` fails, the already-debited balance is never restored, permanently freezing the user's funds. This mirrors the reported TON bug: value is subtracted from the user's ledger before the operation that could fail is known to succeed, with no refund path on failure.

### Finding Description
`native_withdraw` in `Contract::native_withdraw` first calls `self.withdraw(...)`, which internally subtracts the wNEAR balance from the owner's account, before any cross-contract call has been attempted: [1](#0-0) 

Only after this synchronous, irreversible balance subtraction does the code schedule `near_withdraw` on the wNEAR contract, chained to `do_native_withdraw`: [2](#0-1) 

`do_native_withdraw` requires that the `near_withdraw` promise succeeded (`promise_result_checked_void(0).is_ok()`); if it did not, the function panics, and then attempts `Promise::new(withdraw.receiver_id).transfer(withdraw.amount)`, which itself can fail (e.g., if `receiver_id` does not exist or the transfer for any other on-chain reason fails). Because this transfer happens in a distinct scheduled receipt executed *after* the balance was already committed in the original `execute_intents` transaction, a failure here cannot roll back the earlier `internal_sub_balance` state change — NEAR only reverts state changes within the failing receipt itself, not in prior, already-finalized receipts.

The `NativeWithdraw` intent definition itself documents this exact risk: [3](#0-2) 

This is the direct FunC-to-Rust analog of the reported issue: `raw_reserve` in `supply_withdraw_ton` corresponds to the eager `internal_sub_balance`/`self.withdraw` call, and the unreachable refund in `master_core_logic_supply_withdraw` corresponds to the absence of any deposit-back call when `near_withdraw`/the final `transfer` fails. Every other withdrawal path in this codebase (`ft_resolve_withdraw`, `nft_resolve_withdraw`, `mt_resolve_withdraw`) explicitly re-`deposit`s the unused amount back to the sender on failure: [4](#0-3) 

`NativeWithdraw` conspicuously breaks this pattern — the balance-debited-versus-value-delivered-plus-refunded invariant is violated: `internal_sub_balance(amount)` executes unconditionally, but `amount` is only "delivered" if two chained cross-contract calls both succeed, and there is no code path that adds `amount` back to the user's balance otherwise.

### Impact Explanation
This is a Critical finding under "funds permanently frozen." A native NEAR withdrawal whose downstream transfer fails (attacker sends `receiver_id` to a non-existent account, or triggers any other failure of `near_withdraw`/the final transfer) results in the wNEAR-equivalent balance being irrecoverably burned from the user's Defuse account: it is not in the user's Defuse balance, not delivered on-chain to the receiver, and not returned to the sender.

### Likelihood Explanation
Likelihood is meaningful but constrained: `receiver_id` is user-supplied in the `NativeWithdraw` intent, and `Promise::new(receiver_id).transfer(...)` to a nonexistent/invalid account is a realistic failure trigger, requiring no privileged access, oracle manipulation, or race condition — only a signed intent with an attacker- (or accidentally user-) chosen bad `receiver_id`. An attacker griefing their own funds is not itself impactful, but a relayer/integrator constructing withdrawal batches on a user's behalf, or a user typo, both directly cause fund loss with no recovery mechanism, matching the "Data Validation" class from the reference report.

### Recommendation
- **Short term:** Do not call `internal_sub_balance`/`self.withdraw` before the `near_withdraw`/native `transfer` sequence is known to have succeeded. Either defer the balance debit to `do_native_withdraw` after a successful unwrap, or add a resolver step (as is already done for `ft`, `nft`, and `mt` withdrawals) that re-deposits the amount back to the owner if `near_withdraw` or the final transfer fails.
- **Long term:** Enforce the same debit/refund pattern uniformly across all withdraw paths (`ft_withdraw`, `nft_withdraw`, `mt_withdraw`, `native_withdraw`) so that every asset type has a resolver that reconciles debited vs. delivered value, closing the gap that currently only exists for `NativeWithdraw`.

### Proof of Concept
1. User has `N` yoctoNEAR of wNEAR-backed balance in Defuse.
2. User (or a relayer acting on the user's signed intent) submits a `NativeWithdraw { receiver_id: <nonexistent_account>, amount: N }` intent via `execute_intents`.
3. `Contract::native_withdraw` (`contracts/defuse/src/contract/intents/state.rs:212-240`) immediately subtracts `N` from the user's internal balance via `self.withdraw(...)`, and this state change is committed as part of the current transaction/receipt.
4. `near_withdraw` succeeds (unwraps wNEAR into the Defuse contract's native NEAR balance), and `do_native_withdraw` executes `Promise::new(receiver_id).transfer(N)` (`contracts/defuse/src/contract/tokens/nep141/native.rs:12-19`).
5. Because `receiver_id` does not exist, the native transfer fails at the protocol level.
6. There is no resolver/callback that re-deposits `N` back to the user (unlike `ft_resolve_withdraw`/`nft_resolve_withdraw`/`mt_resolve_withdraw`), so `N` is permanently lost — not in the user's Defuse balance, not delivered on-chain.

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

**File:** contracts/defuse/core/src/intents/tokens.rs (L429-435)
```rust
/// This will subtract from the account's wNEAR balance, and will be sent to the account specified as native NEAR.
/// NOTE: the `wNEAR` will not be refunded in case of fail (e.g. `receiver_id`
/// account does not exist).
pub struct NativeWithdraw {
    pub receiver_id: AccountId,
    pub amount: NearToken,
}
```

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L164-194)
```rust
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
