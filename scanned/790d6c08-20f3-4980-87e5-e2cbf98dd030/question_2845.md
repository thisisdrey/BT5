# Q2845: mod - overflow branch reported as success (2)

## Question
Given the batch mixes NEP-141, NEP-245 and NEP-171 legs under one signer, can an unprivileged attacker, entering through `execute_intents` where the attacker signs both sides of a trade using accounts they control, drive `execute_signed_intent` in `contracts/defuse/core/src/engine/mod.rs` into the arm where an arithmetic overflow yields `unwrap_or_default()` (zero) and is therefore indistinguishable from a balanced result, breaking the invariant `an arithmetic overflow during finalisation always aborts the batch, never reports balance` and leading to protocol insolvency: sum of `token_balances` owed exceeds assets actually custodied by the Verifier?

## Target
- File/function: [contracts/defuse/core/src/engine/mod.rs](contracts/defuse/core/src/engine/mod.rs) - `execute_signed_intent` (cross-check `finalize` in the same file)
- Entrypoint: `execute_intents` where the attacker signs both sides of a trade using accounts they control
- Attacker controls: both sides' deltas, account ids, and the order of payloads in the vector
- Exploit idea: The `finalize_into` error path folds remaining amounts with `checked_add` then `i128::try_from` then `checked_neg`, defaulting to 0 on failure; a returned 0 is treated as 'no unmatched delta'. Set-up: the batch mixes NEP-141, NEP-245 and NEP-171 legs under one signer.
- Invariant to test: an arithmetic overflow during finalisation always aborts the batch, never reports balance
- Expected Immunefi impact: Critical - Protocol insolvency: sum of `token_balances` owed exceeds assets actually custodied by the Verifier
- Fast validation: Construct deltas whose remaining totals exceed `i128::MAX`; assert `finalize()` returns `InvariantViolated::Overflow`.
