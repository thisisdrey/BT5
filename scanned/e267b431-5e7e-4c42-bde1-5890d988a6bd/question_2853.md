# Q2853: token_diff - overflow branch reported as success (4)

## Question
Given the batch mixes NEP-141, NEP-245 and NEP-171 legs under one signer, can an unprivileged attacker, entering through `simulate_intents` to find a batch that reports balanced, then `execute_intents` to commit it, drive `token_fee` in `contracts/defuse/core/src/intents/token_diff.rs` into the arm where an arithmetic overflow yields `unwrap_or_default()` (zero) and is therefore indistinguishable from a balanced result, breaking the invariant `an arithmetic overflow during finalisation always aborts the batch, never reports balance` and leading to protocol insolvency: sum of `token_balances` owed exceeds assets actually custodied by the Verifier?

## Target
- File/function: [contracts/defuse/core/src/intents/token_diff.rs](contracts/defuse/core/src/intents/token_diff.rs) - `token_fee` (cross-check `closure` in the same file)
- Entrypoint: `simulate_intents` to find a batch that reports balanced, then `execute_intents` to commit it
- Attacker controls: the entire batch across both calls
- Exploit idea: The `finalize_into` error path folds remaining amounts with `checked_add` then `i128::try_from` then `checked_neg`, defaulting to 0 on failure; a returned 0 is treated as 'no unmatched delta'. Set-up: the batch mixes NEP-141, NEP-245 and NEP-171 legs under one signer.
- Invariant to test: an arithmetic overflow during finalisation always aborts the batch, never reports balance
- Expected Immunefi impact: Critical - Protocol insolvency: sum of `token_balances` owed exceeds assets actually custodied by the Verifier
- Fast validation: Construct deltas whose remaining totals exceed `i128::MAX`; assert `finalize()` returns `InvariantViolated::Overflow`.
