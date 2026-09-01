# Q0616: amounts - batch balance changes do not net to zero (4)

## Question
Given the batch mixes NEP-141, NEP-245 and NEP-171 legs under one signer, can an unprivileged attacker, entering through `simulate_intents` to find a batch that reports balanced, then `execute_intents` to commit it, construct a `MultiPayload` batch whose passage through `with_add` in `contracts/defuse/core/src/amounts.rs` leaves the sum of all `token_balances` changes for one token non-zero, so `TransferMatcher::finalize` reports success while the ledger owes more than it holds, breaking the invariant `sum of every `token_balances` delta for token T across one `execute_intents` call == 0` and leading to protocol insolvency: sum of `token_balances` owed exceeds assets actually custodied by the Verifier?

## Target
- File/function: [contracts/defuse/core/src/amounts.rs](contracts/defuse/core/src/amounts.rs) - `with_add` (cross-check `with_apply_delta` in the same file)
- Entrypoint: `simulate_intents` to find a batch that reports balanced, then `execute_intents` to commit it
- Attacker controls: the entire batch across both calls
- Exploit idea: Target the `finalize_into` pairing loop: the `unmatched == 0` branch treated as success, `saturating_sub` in `sub_add`, and the `i128::try_from`/`checked_neg` fallbacks that return `unwrap_or_default()` (i.e. zero) on overflow. Set-up: the batch mixes NEP-141, NEP-245 and NEP-171 legs under one signer.
- Invariant to test: sum of every `token_balances` delta for token T across one `execute_intents` call == 0
- Expected Immunefi impact: Critical - Protocol insolvency: sum of `token_balances` owed exceeds assets actually custodied by the Verifier
- Fast validation: Unit-test `TransferMatcher` with deltas engineered to overflow the unmatched accumulator; assert `finalize()` errors rather than returning `Ok`.
