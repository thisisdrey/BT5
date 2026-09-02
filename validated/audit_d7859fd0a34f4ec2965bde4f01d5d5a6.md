Confirmed pattern: `ft_withdraw` uses `ft_resolve_withdraw` which re-credits (`self.deposit(...)`) the sender on transfer failure via a `.then(...)` resolve callback. `native_withdraw`, however, has **no analogous resolve/refund callback**. It calls `self.withdraw(...)` (which unconditionally `sub`s the wNEAR balance) and then chains `near_withdraw` → `do_native_withdraw`, with no third `.then()` step that re-credits the owner on failure. [1](#0-0) [2](#0-1) 

### Title
NativeWithdraw permanently burns wNEAR balance with no refund path if `near_withdraw` or the final NEAR transfer fails - (File: contracts/defuse/src/contract/tokens/nep141/native.rs)

### Summary
`Contract::native_withdraw` subtracts the owner's wNEAR balance synchronously via `self.withdraw(...)` before any cross-contract call happens, then schedules `near_withdraw` on the wNEAR contract followed by `do_native_withdraw`, which merely panics (`require!(promise_result_checked_void(0).is_ok(), "near_withdraw failed")`) if the unwrap failed. Unlike `ft_withdraw`/`ft_resolve_withdraw`, there is no resolver callback that re-credits (`self.deposit`) the owner's balance when the promise chain fails. If `near_withdraw` fails or the subsequent `Promise::new(receiver_id).transfer(amount)` never lands, the user's Verifier-side wNEAR balance is gone forever while no NEAR was ever delivered.

### Finding Description
The binding that should hold: `balance_before(owner, wNEAR) - amount == balance_after(owner, wNEAR)` **only if** `amount` of native NEAR was actually delivered to `withdraw.receiver_id`; if the unwrap/transfer fails, the binding should be `balance_after == balance_before` (full refund), mirroring `ft_resolve_withdraw`'s refund logic at [3](#0-2) .

In `native_withdraw` at [1](#0-0) , the sequence is:
1. `self.withdraw(owner_id, [(wnear_token, amount)], ..., false)` — this synchronously calls `owner.token_balances.sub(...)` and commits the burn, per `contracts/defuse/src/contract/tokens/mod.rs` lines 76-128.
2. Schedule `ext_wnear::near_withdraw(amount)` on the wNEAR contract.
3. `.then(do_native_withdraw(withdraw))`, which in `contracts/defuse/src/contract/tokens/nep141/native.rs` lines 11-19 only checks `promise_result_checked_void(0)`; on failure it panics with `"near_withdraw failed"`, aborting the promise chain, but **does not re-credit the owner**.

Since receipt-level state mutations already applied in the calling receipt (the `execute_intents`/`native_withdraw` receipt) are committed independently of whether a later scheduled promise/callback later panics, a failure in `near_withdraw` (e.g., wNEAR contract paused, insufficient wNEAR contract balance, storage issues on the wNEAR side) or a failure of the final `Promise::new(receiver_id).transfer(amount)` (e.g., `receiver_id` is an account requiring initialization/registration that reverts, though a plain `transfer` to a non-existent account still typically succeeds in NEAR — the more realistic failure vector is the `near_withdraw` call itself) results in the balance already being burned with no compensating mint. Compare this with `ft_withdraw`'s `ft_resolve_withdraw` callback (lines 156-195 of `nep141/withdraw.rs`), which explicitly computes `refund = amount - used` and calls `self.deposit(...)` to restore the owner's balance when the underlying transfer failed. `native.rs`'s `do_native_withdraw` has no equivalent resolver; it is not even a resolve callback in the "final .then()" position of the chain — it fires the actual `Promise::new(...).transfer(...)` itself, so there is no subsequent callback at all to detect the transfer's own success/failure and refund.

### Impact Explanation
User funds (wNEAR balance held in the Verifier, representing wrapped NEAR) can be permanently destroyed with no compensating credit and no NEAR delivered to the intended receiver, matching the Critical category "user funds permanently frozen" (destroyed). This is not a griefing/DoS issue — it is a direct loss of the account owner's custodied balance. Every failed `near_withdraw` step causes this; the loss is proportional to the requested withdrawal amount and repeatable per native-withdraw intent.

### Likelihood Explanation
Feasibility depends on how often `near_withdraw` can fail in practice on the wNEAR contract (e.g., the wNEAR contract being paused, having a bug, or reaching some resource limit) or the wNEAR contract not having enough NEAR backing to fulfill the unwrap — this is a real, non-hypothetical failure mode NEP-141/wNEAR contracts can hit, and it is exactly the class of failure that `ft_resolve_withdraw` was written to guard against for regular FT withdrawals. However, I could not fully confirm within the available context whether `near_withdraw` can realistically fail under attacker control (self-triggered) versus only under external/environmental conditions (e.g. wNEAR contract state), since triggering it requires the wNEAR contract itself to reject the call, which the calling user does not directly control. This weakens the "attacker-triggerable at will" argument, though a normal user attempting a legitimate withdrawal at a moment when the wNEAR contract is paused, upgraded, or otherwise reverting would suffer this loss involuntarily and irreversibly — and that's a bug regardless of attacker intent, since no privileged/malicious action is required to enter this state.

### Recommendation
Add a `native_resolve_withdraw` callback (or fold this into `do_native_withdraw`, restructured to be the final `.then()` after the NEAR `transfer`, since native transfers to a valid account virtually always succeed) that checks whether `near_withdraw` and the transfer succeeded, and calls `self.deposit(...)` to refund the wNEAR balance on failure, symmetric with `ft_resolve_withdraw`.

### Proof of Concept
Given the indexed file contents alone I could not locate an existing `near-workspaces` sandbox harness for forcing `near_withdraw` to fail deterministically (this would require deploying a mock/broken wNEAR contract, which falls into the out-of-scope `mock.rs`/`tests/**` exclusions for authoring, but the underlying contract logic itself, in `contracts/defuse/src/contract/intents/state.rs` and `contracts/defuse/src/contract/tokens/nep141/native.rs`, is in scope). A reproducible test would:
1. Deploy the Defuse contract with a wNEAR mock configured to reject `near_withdraw` calls (or pause it).
2. Deposit wNEAR into a user's Verifier balance.
3. Submit a signed `NativeWithdraw` intent for that amount via `execute_intents`.
4. Assert `balance_of(owner, wnear_token_id)` before == amount, and after the failed promise chain resolves, assert it is `0` (bug) instead of being refunded back to `amount`.

Because I cannot execute `cargo test`/sandbox runs in this environment, I cannot supply an executed, passing/failing test transcript — only the code-path analysis above, which is directly supported by contrasting `native_withdraw`'s missing resolver against `ft_withdraw`'s present resolver in the same file tree.

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
