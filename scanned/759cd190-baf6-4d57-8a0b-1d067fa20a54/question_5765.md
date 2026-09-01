# Q5765: mod - intent ordering vs concurrent promise execution (39)

## Question
Given the attacker signs both counterparties using accounts they control, can an unprivileged attacker, entering through `simulate_intents` to find a batch that reports balanced, then `execute_intents` to commit it, exploit the documented warning that promises created by different intents in one `DefuseIntents` execute concurrently, so `internal_add_balance` in `contracts/defuse/core/src/engine/state/mod.rs` observes a balance that a sibling intent's promise has not yet settled, breaking the invariant `the state each intent in a batch acts on == the state produced by all preceding intents in that batch` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/core/src/engine/state/mod.rs](contracts/defuse/core/src/engine/state/mod.rs) - `internal_add_balance` (cross-check `native_withdraw` in the same file)
- Entrypoint: `simulate_intents` to find a batch that reports balanced, then `execute_intents` to commit it
- Attacker controls: the entire batch across both calls
- Exploit idea: Combine a `StorageDeposit` or withdrawal intent with a `TokenDiff` that depends on it; the ordering guarantee holds for state changes but not for promise effects. Set-up: the attacker signs both counterparties using accounts they control.
- Invariant to test: the state each intent in a batch acts on == the state produced by all preceding intents in that batch
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Sandbox: batch a withdrawal and a diff that depends on its refund; assert the observed ordering.
