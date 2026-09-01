# Q5821: amounts - i128::MIN / unsigned_abs asymmetry in delta application (11)

## Question
Given the protocol fee is non-zero, so a fee deposit is added to the matcher, can an unprivileged attacker, entering through `ft_on_transfer` / `mt_on_transfer` `msg` with `execute_intents` funded by the attacker's own deposit, pass `i128::MIN` (or a value whose `unsigned_abs()` exceeds what the opposite branch can represent) through `checked_apply` in `contracts/defuse/core/src/amounts.rs` so the debit and the credit for the same delta differ in magnitude, breaking the invariant `|amount debited| == |amount credited| for every delta the engine accepts` and leading to unauthorized minting / balance inflation: a balance is credited with no matching asset received?

## Target
- File/function: [contracts/defuse/core/src/amounts.rs](contracts/defuse/core/src/amounts.rs) - `checked_apply` (cross-check `Amounts` in the same file)
- Entrypoint: `ft_on_transfer` / `mt_on_transfer` `msg` with `execute_intents` funded by the attacker's own deposit
- Attacker controls: the deposited amount, the nested batch, and `refund_if_fails`
- Exploit idea: `internal_apply_deltas` branches on `delta.is_negative()` and uses `delta.unsigned_abs()`; probe whether the negative and positive branches are exact inverses at the representable extremes. Set-up: the protocol fee is non-zero, so a fee deposit is added to the matcher.
- Invariant to test: |amount debited| == |amount credited| for every delta the engine accepts
- Expected Immunefi impact: Critical - Unauthorized minting / balance inflation: a balance is credited with no matching asset received
- Fast validation: Table-test `internal_apply_deltas` at `i128::MIN`, `i128::MAX`, `-1`, `1`; assert symmetry or rejection.
