# Q1523: simulation_output - Lock serialization skips the flag (2)

## Question
Given the victim account has no stored entry yet, can an unprivileged attacker, entering through an `AddPublicKey` / `RemovePublicKey` / `SetAuthByPredecessorId` intent inside `execute_intents`, exploit that `Lock<T>` in `contracts/defuse/src/simulation_output.rs` serialises `locked` with `skip_serializing_if = Not::not`, so a round-trip through `StateOutput` (state read/write, ABI, or a migration) loses the locked flag, breaking the invariant `an account's `is_locked` state after any serialisation round-trip == its state before` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/src/simulation_output.rs](contracts/defuse/src/simulation_output.rs) - `StateOutput`
- Entrypoint: an `AddPublicKey` / `RemovePublicKey` / `SetAuthByPredecessorId` intent inside `execute_intents`
- Attacker controls: the key bytes and the position of the intent within the batch
- Exploit idea: A dropped `locked: true` silently unlocks a frozen account. Set-up: the victim account has no stored entry yet.
- Invariant to test: an account's `is_locked` state after any serialisation round-trip == its state before
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Round-trip a locked `Lock<Account>` through borsh and serde; assert the flag survives.
