# Q4208: lock - locked account still mutated through an unchecked path (9)

## Question
Given the victim's entry is still at the v0 layout, can an unprivileged attacker, entering through an `AddPublicKey` / `RemovePublicKey` / `SetAuthByPredecessorId` intent inside `execute_intents`, reach `as_inner_unchecked` in `contracts/defuse/core/src/lock.rs` on a locked account via a path that uses `as_inner_unchecked_mut()` (or `get_or_create(...).as_inner_unchecked_mut()`) instead of `get_mut()`, so the lock does not actually prevent the mutation, breaking the invariant `the set of mutations possible on a locked account == deposits and refunds only` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/core/src/lock.rs](contracts/defuse/core/src/lock.rs) - `as_inner_unchecked` (cross-check `get_mut` in the same file)
- Entrypoint: an `AddPublicKey` / `RemovePublicKey` / `SetAuthByPredecessorId` intent inside `execute_intents`
- Attacker controls: the key bytes and the position of the intent within the batch
- Exploit idea: Deposits, refunds and some account-creation paths deliberately bypass the lock; enumerate which mutations are reachable by an unprivileged caller on a locked victim. Set-up: the victim's entry is still at the v0 layout.
- Invariant to test: the set of mutations possible on a locked account == deposits and refunds only
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Lock an account, then attempt every unprivileged entrypoint against it; assert only deposits/refunds succeed.
