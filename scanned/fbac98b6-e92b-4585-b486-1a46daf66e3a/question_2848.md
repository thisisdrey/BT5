# Q2848: mod - zero-amount / empty-collection guard bypass (4)

## Question
Given the batch mixes NEP-141, NEP-245 and NEP-171 legs under one signer, can an unprivileged attacker, entering through `simulate_intents` to find a batch that reports balanced, then `execute_intents` to commit it, reach `storage_deposit` in `contracts/defuse/core/src/engine/state/mod.rs` with an amount of zero, an empty `Amounts`, or an empty `diff` through a path that does not run the `InvalidIntent` guard, so an event is emitted or state touched with no corresponding value, breaking the invariant `every balance-mutating call carries a strictly positive amount` and leading to unauthorized minting / balance inflation: a balance is credited with no matching asset received?

## Target
- File/function: [contracts/defuse/core/src/engine/state/mod.rs](contracts/defuse/core/src/engine/state/mod.rs) - `storage_deposit` (cross-check `add_public_key` in the same file)
- Entrypoint: `simulate_intents` to find a batch that reports balanced, then `execute_intents` to commit it
- Attacker controls: the entire batch across both calls
- Exploit idea: Guards live in some callers but not all; enumerate every path into `internal_add_balance` / `internal_sub_balance` / `deposit` / `withdraw`. Set-up: the batch mixes NEP-141, NEP-245 and NEP-171 legs under one signer.
- Invariant to test: every balance-mutating call carries a strictly positive amount
- Expected Immunefi impact: Critical - Unauthorized minting / balance inflation: a balance is credited with no matching asset received
- Fast validation: Enumerate callers of `storage_deposit`; assert each rejects zero amounts.
