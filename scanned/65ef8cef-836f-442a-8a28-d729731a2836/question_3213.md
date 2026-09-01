# Q3213: amounts - zero-amount / empty-collection guard bypass

## Question
Given the batch mixes NEP-141, NEP-245 and NEP-171 legs under one signer, can an unprivileged attacker, entering through `execute_intents(signed: Vec<MultiPayload>)` with a multi-intent, multi-token batch, reach `Amounts` in `contracts/defuse/core/src/amounts.rs` with an amount of zero, an empty `Amounts`, or an empty `diff` through a path that does not run the `InvalidIntent` guard, so an event is emitted or state touched with no corresponding value, breaking the invariant `every balance-mutating call carries a strictly positive amount` and leading to unauthorized minting / balance inflation: a balance is credited with no matching asset received?

## Target
- File/function: [contracts/defuse/core/src/amounts.rs](contracts/defuse/core/src/amounts.rs) - `Amounts` (cross-check `with_apply_deltas` in the same file)
- Entrypoint: `execute_intents(signed: Vec<MultiPayload>)` with a multi-intent, multi-token batch
- Attacker controls: the number of payloads, the intents in each, and every `(token_id, delta)` pair in every `TokenDiff`
- Exploit idea: Guards live in some callers but not all; enumerate every path into `internal_add_balance` / `internal_sub_balance` / `deposit` / `withdraw`. Set-up: the batch mixes NEP-141, NEP-245 and NEP-171 legs under one signer.
- Invariant to test: every balance-mutating call carries a strictly positive amount
- Expected Immunefi impact: Critical - Unauthorized minting / balance inflation: a balance is credited with no matching asset received
- Fast validation: Enumerate callers of `Amounts`; assert each rejects zero amounts.
