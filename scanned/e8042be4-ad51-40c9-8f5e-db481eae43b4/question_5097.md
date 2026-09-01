# Q5097: mod - intent ordering vs concurrent promise execution (29)

## Question
Given several accounts hold exactly equal amounts, so the descending sort is tie-broken by iteration order, can an unprivileged attacker, entering through `execute_intents(signed: Vec<MultiPayload>)` with a multi-intent, multi-token batch, exploit the documented warning that promises created by different intents in one `DefuseIntents` execute concurrently, so `new_intent` in `contracts/defuse/core/src/intents/mod.rs` observes a balance that a sibling intent's promise has not yet settled, breaking the invariant `the state each intent in a batch acts on == the state produced by all preceding intents in that batch` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/core/src/intents/mod.rs](contracts/defuse/core/src/intents/mod.rs) - `new_intent` (cross-check `MaybeIntentEvent` in the same file)
- Entrypoint: `execute_intents(signed: Vec<MultiPayload>)` with a multi-intent, multi-token batch
- Attacker controls: the number of payloads, the intents in each, and every `(token_id, delta)` pair in every `TokenDiff`
- Exploit idea: Combine a `StorageDeposit` or withdrawal intent with a `TokenDiff` that depends on it; the ordering guarantee holds for state changes but not for promise effects. Set-up: several accounts hold exactly equal amounts, so the descending sort is tie-broken by iteration order.
- Invariant to test: the state each intent in a batch acts on == the state produced by all preceding intents in that batch
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Sandbox: batch a withdrawal and a diff that depends on its refund; assert the observed ordering.
