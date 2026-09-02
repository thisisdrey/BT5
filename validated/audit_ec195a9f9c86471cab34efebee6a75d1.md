### Title
`do_native_withdraw` has no resolve/refund callback, so a failed final `NEAR` transfer permanently strands funds inside the Defuse contract while the owner's wNEAR balance stays debited - ([File: contracts/defuse/src/contract/tokens/nep141/native.rs])

### Summary
`native_withdraw` debits `owner_id`'s wNEAR balance, unwraps it into raw NEAR via `near_withdraw`, then calls `do_native_withdraw` which issues `Promise::new(withdraw.receiver_id).transfer(withdraw.amount)` with no `.then()` resolver checking the outcome. If that final transfer fails (e.g. `receiver_id` is a non-existent account), the NEAR receipt is refunded back to the Defuse contract's own account instead of reaching `receiver_id`, but nothing re-credits `owner_id`'s ledger balance. This breaks the conservation invariant between `token_balances` and NEAR actually held/delivered.

### Finding Description
The binding that must hold: `internal_sub_balance(owner_id, wNEAR, amount)` == `NEAR delivered to receiver_id`. In `native_withdraw` (contracts/defuse/src/contract/intents/state.rs:212-240), the wNEAR balance is subtracted via `self.withdraw(...)` before any external NEAR movement occurs, then `near_withdraw` unwraps wNEAR into the Defuse contract's own NEAR balance, followed by `do_native_withdraw`: [1](#0-0) 

`do_native_withdraw` only checks that `near_withdraw` (promise index 0) succeeded via `promise_result_checked_void`, then fires `Promise::new(withdraw.receiver_id).transfer(withdraw.amount)` and returns it directly - there is no `.then()` scheduling a resolver, unlike every other withdraw path in the contract. Contrast with `internal_ft_withdraw` / `ft_resolve_withdraw` in contracts/defuse/src/contract/tokens/nep141/withdraw.rs:98-105 and 154-195, which always schedules a resolver that checks the transfer result and re-`deposit`s (`REFUND_MEMO`) any amount that failed to move. `do_storage_deposit` has the identical missing-resolver pattern, but that is out of scope for this question.

Exploit flow (fully reachable by an unprivileged attacker on their own account):
1. Attacker has an account with sufficient wNEAR balance in the Verifier (their own funds, no privilege required).
2. Attacker submits an intent producing `NativeWithdraw { receiver_id: <account that does not exist / cannot receive a plain balance Transfer action>, amount }`.
3. `withdraw()` calls `internal_sub_balance`, decrementing `owner_id`'s wNEAR balance to zero (or reduced by `amount`).
4. `near_withdraw` succeeds, converting the wNEAR into raw NEAR now held in the Defuse contract's own account.
5. `do_native_withdraw`'s `Promise::new(receiver_id).transfer(amount)` fails because `receiver_id` is not a valid/existing account for a plain Transfer action; the NEAR protocol refunds the deposit back to the predecessor of that receipt, which is the Defuse contract itself - not `owner_id`.
6. No callback exists to detect this failure and call `self.deposit(...)` to re-credit `owner_id`. The wNEAR debit is never reversed, and the NEAR is now sitting in the contract's own account balance with zero ledger entry.

Existing guards do not prevent this: `promise_result_checked_void(0)` in `do_native_withdraw` only validates `near_withdraw`'s result (index 0), never the outcome of the `transfer` it itself schedules, and there is no subsequent `#[private]` resolver method for `native_withdraw` analogous to `ft_resolve_withdraw`.

### Impact Explanation
`owner_id`'s wNEAR balance is permanently destroyed while the corresponding NEAR value never reaches `receiver_id` and is not re-credited to any account's ledger balance - it becomes untracked NEAR sitting in the Defuse contract's own account. This is a direct conservation break between `token_balances` and actual custodied NEAR, falling under the Critical category "user funds permanently frozen." The attack is repeatable by any account against itself (self-inflicted loss is not the concern; the concern is any account that becomes the target of such a `NativeWithdraw`, including third parties if a caller can direct withdrawals on their own balance to an arbitrary `receiver_id` - which `NativeWithdraw` allows since `receiver_id` is attacker-controlled input). Each failed transfer permanently strands `amount` yoctoNEAR per attempt, and the contract silently accumulates undocumented NEAR with no ledger accounting.

### Likelihood Explanation
No special privileges, roles, or victim keys are needed - only a valid signed `NativeWithdraw` intent naming a `receiver_id` that will fail the plain NEAR `Transfer` action (e.g., a syntactically valid but non-existent named account, or a not-yet-created account). The attacker only needs their own wNEAR balance and standard `execute_intents` access. This is trivially reproducible and repeatable.

### Recommendation
Add a `#[private]` resolver for `native_withdraw`, analogous to `ft_resolve_withdraw`, scheduled via `.then()` after the `transfer` promise in `do_native_withdraw`, that checks `promise_result_checked_void` on the transfer result and calls `self.deposit(owner_id, [(wnear_token_id, amount)], Some(REFUND_MEMO))` when the transfer failed, mirroring the refund pattern already used for `ft_withdraw`.

### Proof of Concept
```rust
// near-workspaces sandbox test (contracts/defuse or tests crate)
// 1. Deploy defuse contract + wnear contract, register owner_id with wNEAR balance = N.
// 2. Submit signed intent: NativeWithdraw { receiver_id: "nonexistent-account.near", amount: N }
//    via execute_intents.
// 3. Assert:
//    - defuse.balance_of(owner_id, wnear_token_id) == 0   (wNEAR debited)
//    - receiver "nonexistent-account.near" balance query fails / == 0 (never created, never received funds)
//    - defuse contract's own NEAR account balance increased by ~N (stranded funds, refund receipt)
//    - No deposit/refund event or ledger entry credits N back to owner_id or anyone else.
// This demonstrates token_balances(owner_id) went from N to 0 while no account's actual
// NEAR balance increased by N except the Defuse contract's own untracked balance,
// breaking the wNEAR-debited == NEAR-delivered invariant.
```

### Citations

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
