# Q4976: lock - Lock serialization skips the flag (14)

## Question
Given the victim's entry is still at the v0 layout, can an unprivileged attacker, entering through `execute_intents` mixing payloads from several signers in one vector, exploit that `Lock<T>` in `contracts/defuse/core/src/lock.rs` serialises `locked` with `skip_serializing_if = Not::not`, so a round-trip through `unlocked` (state read/write, ABI, or a migration) loses the locked flag, breaking the invariant `an account's `is_locked` state after any serialisation round-trip == its state before` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/core/src/lock.rs](contracts/defuse/core/src/lock.rs) - `unlocked` (cross-check `get_mut` in the same file)
- Entrypoint: `execute_intents` mixing payloads from several signers in one vector
- Attacker controls: the number and order of payloads and which accounts each targets
- Exploit idea: A dropped `locked: true` silently unlocks a frozen account. Set-up: the victim's entry is still at the v0 layout.
- Invariant to test: an account's `is_locked` state after any serialisation round-trip == its state before
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Round-trip a locked `Lock<Account>` through borsh and serde; assert the flag survives.
