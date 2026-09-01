# Q1886: add_sub - overflow branch reported as success (10)

## Question
Given the protocol fee is non-zero, so a fee deposit is added to the matcher, can an unprivileged attacker, entering through `execute_intents` where the attacker signs both sides of a trade using accounts they control, drive `CheckedSub` in `crates/num-utils/src/add_sub.rs` into the arm where an arithmetic overflow yields `unwrap_or_default()` (zero) and is therefore indistinguishable from a balanced result, breaking the invariant `an arithmetic overflow during finalisation always aborts the batch, never reports balance` and leading to protocol insolvency: sum of `token_balances` owed exceeds assets actually custodied by the Verifier?

## Target
- File/function: [crates/num-utils/src/add_sub.rs](crates/num-utils/src/add_sub.rs) - `CheckedSub` (cross-check `checked_add` in the same file)
- Entrypoint: `execute_intents` where the attacker signs both sides of a trade using accounts they control
- Attacker controls: both sides' deltas, account ids, and the order of payloads in the vector
- Exploit idea: The `finalize_into` error path folds remaining amounts with `checked_add` then `i128::try_from` then `checked_neg`, defaulting to 0 on failure; a returned 0 is treated as 'no unmatched delta'. Set-up: the protocol fee is non-zero, so a fee deposit is added to the matcher.
- Invariant to test: an arithmetic overflow during finalisation always aborts the batch, never reports balance
- Expected Immunefi impact: Critical - Protocol insolvency: sum of `token_balances` owed exceeds assets actually custodied by the Verifier
- Fast validation: Construct deltas whose remaining totals exceed `i128::MAX`; assert `finalize()` returns `InvariantViolated::Overflow`.
