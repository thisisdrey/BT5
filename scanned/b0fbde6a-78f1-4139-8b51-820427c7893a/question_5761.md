# Q5761: amounts - i128::MIN / unsigned_abs asymmetry in delta application (10)

## Question
Given the protocol fee is non-zero, so a fee deposit is added to the matcher, can an unprivileged attacker, entering through `execute_intents` where the attacker signs both sides of a trade using accounts they control, pass `i128::MIN` (or a value whose `unsigned_abs()` exceeds what the opposite branch can represent) through `Amounts` in `contracts/defuse/core/src/amounts.rs` so the debit and the credit for the same delta differ in magnitude, breaking the invariant `|amount debited| == |amount credited| for every delta the engine accepts` and leading to unauthorized minting / balance inflation: a balance is credited with no matching asset received?

## Target
- File/function: [contracts/defuse/core/src/amounts.rs](contracts/defuse/core/src/amounts.rs) - `Amounts` (cross-check `with_apply_deltas` in the same file)
- Entrypoint: `execute_intents` where the attacker signs both sides of a trade using accounts they control
- Attacker controls: both sides' deltas, account ids, and the order of payloads in the vector
- Exploit idea: `internal_apply_deltas` branches on `delta.is_negative()` and uses `delta.unsigned_abs()`; probe whether the negative and positive branches are exact inverses at the representable extremes. Set-up: the protocol fee is non-zero, so a fee deposit is added to the matcher.
- Invariant to test: |amount debited| == |amount credited| for every delta the engine accepts
- Expected Immunefi impact: Critical - Unauthorized minting / balance inflation: a balance is credited with no matching asset received
- Fast validation: Table-test `internal_apply_deltas` at `i128::MIN`, `i128::MAX`, `-1`, `1`; assert symmetry or rejection.
