# Q5154: deltas - i128::MIN / unsigned_abs asymmetry in delta application (6)

## Question
Given the attacker signs both counterparties using accounts they control, can an unprivileged attacker, entering through `execute_intents` where the attacker signs both sides of a trade using accounts they control, pass `i128::MIN` (or a value whose `unsigned_abs()` exceeds what the opposite branch can represent) through `as_unmatched_deltas` in `contracts/defuse/core/src/engine/state/deltas.rs` so the debit and the credit for the same delta differ in magnitude, breaking the invariant `|amount debited| == |amount credited| for every delta the engine accepts` and leading to unauthorized minting / balance inflation: a balance is credited with no matching asset received?

## Target
- File/function: [contracts/defuse/core/src/engine/state/deltas.rs](contracts/defuse/core/src/engine/state/deltas.rs) - `as_unmatched_deltas` (cross-check `into_unmatched_deltas` in the same file)
- Entrypoint: `execute_intents` where the attacker signs both sides of a trade using accounts they control
- Attacker controls: both sides' deltas, account ids, and the order of payloads in the vector
- Exploit idea: `internal_apply_deltas` branches on `delta.is_negative()` and uses `delta.unsigned_abs()`; probe whether the negative and positive branches are exact inverses at the representable extremes. Set-up: the attacker signs both counterparties using accounts they control.
- Invariant to test: |amount debited| == |amount credited| for every delta the engine accepts
- Expected Immunefi impact: Critical - Unauthorized minting / balance inflation: a balance is credited with no matching asset received
- Fast validation: Table-test `internal_apply_deltas` at `i128::MIN`, `i128::MAX`, `-1`, `1`; assert symmetry or rejection.
