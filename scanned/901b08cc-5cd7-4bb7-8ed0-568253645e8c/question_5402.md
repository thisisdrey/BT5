# Q5402: deltas - self-cancelling deltas inside one TokenDiff (4)

## Question
Given the attacker signs both counterparties using accounts they control, can an unprivileged attacker, entering through `simulate_intents` to find a batch that reports balanced, then `execute_intents` to commit it, submit a `TokenDiff` whose `diff` map reaches `TransferMatcher` in `contracts/defuse/core/src/engine/state/deltas.rs` with entries that cancel at the matcher level but each independently trigger (or skip) the fee branch, so fees are charged on notional that never moved, breaking the invariant `fees credited to `fee_collector` == fees owed on value that actually changed hands` and leading to theft of unclaimed protocol fees / protocol fee bypass?

## Target
- File/function: [contracts/defuse/core/src/engine/state/deltas.rs](contracts/defuse/core/src/engine/state/deltas.rs) - `TransferMatcher` (cross-check `deposit` in the same file)
- Entrypoint: `simulate_intents` to find a batch that reports balanced, then `execute_intents` to commit it
- Attacker controls: the entire batch across both calls
- Exploit idea: Only negative deltas pay fees; craft a diff whose negative legs are matched by the signer's own positive legs so the net movement is zero but fee accounting is not. Set-up: the attacker signs both counterparties using accounts they control.
- Invariant to test: fees credited to `fee_collector` == fees owed on value that actually changed hands
- Expected Immunefi impact: High - Theft of unclaimed protocol fees / protocol fee bypass
- Fast validation: Execute a self-cancelling `TokenDiff`; assert `fees_collected` is zero when net movement is zero.
