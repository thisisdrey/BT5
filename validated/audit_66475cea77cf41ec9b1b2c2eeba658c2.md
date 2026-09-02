### Title
Missing refund/resolve callback after `do_storage_deposit` permanently burns signer's debited wNEAR on failure - (File: `contracts/defuse/src/contract/tokens/nep141/storage_deposit.rs`)

### Summary
`Contract::do_storage_deposit` schedules `ext_storage_management::storage_deposit` to an attacker-controlled `contract_id` but never attaches a `.then()` resolve callback to check the outcome and re-credit the signer. Unlike the analogous `ft_withdraw` flow, which uses `ft_resolve_withdraw` to refund any amount that failed to settle, the `StorageDeposit` intent path has no such mechanism, so a failed or silently-absorbing `storage_deposit` call at the target contract leaves the signer's internal wNEAR balance permanently decremented with no path to recovery.

### Finding Description
The claimed binding is:
`internal_sub_balance(signer_id, wNEAR, amount)` (debited in `State::storage_deposit`, `contracts/defuse/src/contract/intents/state.rs:270-278`) **==** `internal_add_balance` crediting *someone* with `amount`-equivalent value, either the registered storage at `contract_id` for `deposit_for_account_id`, or a refund back to `signer_id` if that registration fails.

Tracing the code path:
1. `State::storage_deposit` (`contracts/defuse/src/contract/intents/state.rs:265-297`) calls `self.withdraw(...)`, which performs `internal_sub_balance` on the signer's wNEAR balance for `storage_deposit.amount` — this is unconditional and immediate.
2. It then chains `ext_wnear::near_withdraw` → `.then(Self::do_storage_deposit(storage_deposit))`, and `.detach()`s the whole promise chain (no further `.then()` is attached at this call site).
3. `Contract::do_storage_deposit` (`contracts/defuse/src/contract/tokens/nep141/storage_deposit.rs:13-26`) only checks that `near_withdraw` succeeded (`promise_result_checked_void(0)`), then returns a bare `Promise` calling `ext_storage_management::storage_deposit(contract_id, deposit_for_account_id)` with `with_unused_gas_weight(0)`. This returned promise is the *final* promise in the chain — nothing consumes or checks its result.

Compare this to `contract/tokens/nep141/withdraw.rs`: `internal_ft_withdraw` explicitly chains `.then(... .ft_resolve_withdraw(...))` (`withdraw.rs:98-104`), and `ft_resolve_withdraw` (`withdraw.rs:155-195`) checks the promise result and calls `self.deposit(sender_id, ..., refund)` for any amount not successfully transferred. `do_storage_deposit` has no equivalent resolver.

Root cause: the `StorageDeposit`/`NativeWithdraw`/`AuthCall` promise chains debit the internal ledger before dispatching external calls, but only `FtWithdraw` implements the refund-on-failure resolver pattern; `StorageDeposit` was never given one.

Attacker's exact payload: sign a `MultiPayload` containing intent `StorageDeposit{ contract_id: <attacker contract>, amount: <N>, deposit_for_account_id: <any account> }`, where the attacker's own contract's `storage_deposit` method either panics or accepts the attached NEAR without registering storage. Since `require!(promise_result_checked_void(0).is_ok(), "near_withdraw failed")` in `do_storage_deposit` only gates on the *previous* `near_withdraw` result (index 0), the final `storage_deposit` call's success/failure is completely unchecked by the contract.

However, note this is **self-authorized**: the signer must sign this exact intent themselves, debiting only their own wNEAR balance — no other account's balance is touched, and the Verifier's own NEAR holdings decrease exactly by the amount that actually leaves the contract as attached deposit. If `storage_deposit` at `contract_id` panics, NEAR runtime semantics refund the attached deposit back to the calling contract's own account (not to the signer's internal ledger), so the signer's internal ledger entry is never restored even though the corresponding NEAR returns to the Defuse contract itself — this is the "frozen funds" defect. If instead `storage_deposit` succeeds without panicking but simply keeps the funds (no registration), the NEAR genuinely leaves the contract to the destination the signer chose.

### Impact Explanation
This is a real accounting bug: on the panic-refund sub-case, the Defuse contract's actual NEAR balance is unaffected (refunded to contract by protocol), yet the signer's internal wNEAR ledger balance is permanently decremented with no code path to re-credit it — the signer's own tokens become irrecoverable through the protocol despite the backing value staying in the contract, matching "user funds permanently frozen." This is repeatable per signer per intent and does not require any privileged role. It does not, however, constitute Verifier insolvency or theft of another user's funds without their signature — the signer must sign the exact `StorageDeposit{contract_id, amount, deposit_for_account_id}` intent themselves, so the debit is self-authorized and the destination is entirely of the signer's own choosing (comparable to a signer voluntarily withdrawing to a malicious/broken contract of their choice). No victim's balance is decremented, and the batch's net balance change from the Verifier's perspective still nets to zero (debit matches what actually leaves/stays with the contract).

### Likelihood Explanation
Trivial to trigger: any account with a wNEAR balance in the Verifier can sign a `StorageDeposit` intent pointing `contract_id` at their own deployed contract. No special privileges, roles, or victim cooperation needed. The only "cost" is losing their own already-debited wNEAR balance, i.e., it is self-inflicted for the signer (the only account whose funds are at risk is the signer's own).

### Recommendation
Add a `.then()` resolve callback after the `storage_deposit` promise (mirroring `ft_resolve_withdraw`) that inspects the promise result at `do_storage_deposit`'s external call index and, on failure, re-credits `signer_id`'s wNEAR balance via `internal_add_balance`/`self.deposit(...)`, exactly as `ft_resolve_withdraw` does for `ft_transfer`/`ft_transfer_call` failures.

### Proof of Concept
```
// near-workspaces sandbox test (contracts/defuse):
// 1. Deploy Defuse contract + wNEAR contract; register `alice` with wNEAR balance N.
// 2. Deploy `evil_storage.wasm` implementing `storage_deposit(account_id, registration_only)`
//    as a no-op that keeps the attached deposit and returns a StorageBalance-shaped
//    value without inserting into its own storage map (or simply `env::panic_str(...)`).
// 3. alice.execute_intents([ MultiPayload{ signer_id: alice, intents: [
//        StorageDeposit{ contract_id: evil_storage, amount: N, deposit_for_account_id: alice } ] } ])
// 4. Assert:
//    - defuse.balance_of(alice, wnear_token_id) == 0   (debited, binding LHS)
//    - evil_storage.storage_balance_of(alice) == None  (no registration happened, binding RHS unmet)
//    - defuse.balance_of(alice, wnear_token_id) is NEVER restored to N in any subsequent block
//      (no resolve/refund credits alice), proving the two sides of the binding diverge permanently.
```