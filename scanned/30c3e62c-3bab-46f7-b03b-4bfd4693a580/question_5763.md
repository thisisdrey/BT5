# Q5763: cached - zero-amount / empty-collection guard bypass (8)

## Question
Given the attacker signs both counterparties using accounts they control, can an unprivileged attacker, entering through `simulate_intents` to find a batch that reports balanced, then `execute_intents` to commit it, reach `mt_withdraw` in `contracts/defuse/core/src/engine/state/cached.rs` with an amount of zero, an empty `Amounts`, or an empty `diff` through a path that does not run the `InvalidIntent` guard, so an event is emitted or state touched with no corresponding value, breaking the invariant `every balance-mutating call carries a strictly positive amount` and leading to unauthorized minting / balance inflation: a balance is credited with no matching asset received?

## Target
- File/function: [contracts/defuse/core/src/engine/state/cached.rs](contracts/defuse/core/src/engine/state/cached.rs) - `mt_withdraw` (cross-check `fee_collector` in the same file)
- Entrypoint: `simulate_intents` to find a batch that reports balanced, then `execute_intents` to commit it
- Attacker controls: the entire batch across both calls
- Exploit idea: Guards live in some callers but not all; enumerate every path into `internal_add_balance` / `internal_sub_balance` / `deposit` / `withdraw`. Set-up: the attacker signs both counterparties using accounts they control.
- Invariant to test: every balance-mutating call carries a strictly positive amount
- Expected Immunefi impact: Critical - Unauthorized minting / balance inflation: a balance is credited with no matching asset received
- Fast validation: Enumerate callers of `mt_withdraw`; assert each rejects zero amounts.
