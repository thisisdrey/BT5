# Q3117: mod - Lock serialization skips the flag (10)

## Question
Given the victim account is currently locked, can an unprivileged attacker, entering through `simulate_intents` as a probe of another account's state before acting, exploit that `Lock<T>` in `contracts/defuse/src/contract/accounts/account/entry/mod.rs` serialises `locked` with `skip_serializing_if = Not::not`, so a round-trip through `MaybeVersionedAccountEntry` (state read/write, ABI, or a migration) loses the locked flag, breaking the invariant `an account's `is_locked` state after any serialisation round-trip == its state before` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/src/contract/accounts/account/entry/mod.rs](contracts/defuse/src/contract/accounts/account/entry/mod.rs) - `MaybeVersionedAccountEntry` (cross-check `AccountEntry` in the same file)
- Entrypoint: `simulate_intents` as a probe of another account's state before acting
- Attacker controls: the probe batch composition
- Exploit idea: A dropped `locked: true` silently unlocks a frozen account. Set-up: the victim account is currently locked.
- Invariant to test: an account's `is_locked` state after any serialisation round-trip == its state before
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Round-trip a locked `Lock<Account>` through borsh and serde; assert the flag survives.
