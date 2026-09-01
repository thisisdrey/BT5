# Q0206: amounts - batch balance changes do not net to zero (2)

## Question
Given the batch mixes NEP-141, NEP-245 and NEP-171 legs under one signer, can an unprivileged attacker, entering through `execute_intents` where the attacker signs both sides of a trade using accounts they control, construct a `MultiPayload` batch whose passage through `apply_delta` in `contracts/defuse/core/src/amounts.rs` leaves the sum of all `token_balances` changes for one token non-zero, so `TransferMatcher::finalize` reports success while the ledger owes more than it holds, breaking the invariant `sum of every `token_balances` delta for token T across one `execute_intents` call == 0` and leading to protocol insolvency: sum of `token_balances` owed exceeds assets actually custodied by the Verifier?

## Target
- File/function: [contracts/defuse/core/src/amounts.rs](contracts/defuse/core/src/amounts.rs) - `apply_delta` (cross-check `checked_apply` in the same file)
- Entrypoint: `execute_intents` where the attacker signs both sides of a trade using accounts they control
- Attacker controls: both sides' deltas, account ids, and the order of payloads in the vector
- Exploit idea: Target the `finalize_into` pairing loop: the `unmatched == 0` branch treated as success, `saturating_sub` in `sub_add`, and the `i128::try_from`/`checked_neg` fallbacks that return `unwrap_or_default()` (i.e. zero) on overflow. Set-up: the batch mixes NEP-141, NEP-245 and NEP-171 legs under one signer.
- Invariant to test: sum of every `token_balances` delta for token T across one `execute_intents` call == 0
- Expected Immunefi impact: Critical - Protocol insolvency: sum of `token_balances` owed exceeds assets actually custodied by the Verifier
- Fast validation: Unit-test `TransferMatcher` with deltas engineered to overflow the unmatched accumulator; assert `finalize()` errors rather than returning `Ok`.
