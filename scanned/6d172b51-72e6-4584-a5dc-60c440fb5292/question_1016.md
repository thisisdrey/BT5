# Q1016: mod - intent ordering vs concurrent promise execution (4)

## Question
Given the batch mixes NEP-141, NEP-245 and NEP-171 legs under one signer, can an unprivileged attacker, entering through `simulate_intents` to find a batch that reports balanced, then `execute_intents` to commit it, exploit the documented warning that promises created by different intents in one `DefuseIntents` execute concurrently, so `ExecutableIntent` in `contracts/defuse/core/src/intents/mod.rs` observes a balance that a sibling intent's promise has not yet settled, breaking the invariant `the state each intent in a batch acts on == the state produced by all preceding intents in that batch` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/core/src/intents/mod.rs](contracts/defuse/core/src/intents/mod.rs) - `ExecutableIntent` (cross-check `execute_intent` in the same file)
- Entrypoint: `simulate_intents` to find a batch that reports balanced, then `execute_intents` to commit it
- Attacker controls: the entire batch across both calls
- Exploit idea: Combine a `StorageDeposit` or withdrawal intent with a `TokenDiff` that depends on it; the ordering guarantee holds for state changes but not for promise effects. Set-up: the batch mixes NEP-141, NEP-245 and NEP-171 legs under one signer.
- Invariant to test: the state each intent in a batch acts on == the state produced by all preceding intents in that batch
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Sandbox: batch a withdrawal and a diff that depends on its refund; assert the observed ordering.
