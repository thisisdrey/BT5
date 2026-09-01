# Q5461: amounts - batch balance changes do not net to zero (9)

## Question
Given the protocol fee is non-zero, so a fee deposit is added to the matcher, can an unprivileged attacker, entering through `execute_intents(signed: Vec<MultiPayload>)` with a multi-intent, multi-token batch, construct a `MultiPayload` batch whose passage through `checked_apply` in `contracts/defuse/core/src/amounts.rs` leaves the sum of all `token_balances` changes for one token non-zero, so `TransferMatcher::finalize` reports success while the ledger owes more than it holds, breaking the invariant `sum of every `token_balances` delta for token T across one `execute_intents` call == 0` and leading to protocol insolvency: sum of `token_balances` owed exceeds assets actually custodied by the Verifier?

## Target
- File/function: [contracts/defuse/core/src/amounts.rs](contracts/defuse/core/src/amounts.rs) - `checked_apply` (cross-check `Amounts` in the same file)
- Entrypoint: `execute_intents(signed: Vec<MultiPayload>)` with a multi-intent, multi-token batch
- Attacker controls: the number of payloads, the intents in each, and every `(token_id, delta)` pair in every `TokenDiff`
- Exploit idea: Target the `finalize_into` pairing loop: the `unmatched == 0` branch treated as success, `saturating_sub` in `sub_add`, and the `i128::try_from`/`checked_neg` fallbacks that return `unwrap_or_default()` (i.e. zero) on overflow. Set-up: the protocol fee is non-zero, so a fee deposit is added to the matcher.
- Invariant to test: sum of every `token_balances` delta for token T across one `execute_intents` call == 0
- Expected Immunefi impact: Critical - Protocol insolvency: sum of `token_balances` owed exceeds assets actually custodied by the Verifier
- Fast validation: Unit-test `TransferMatcher` with deltas engineered to overflow the unmatched accumulator; assert `finalize()` errors rather than returning `Ok`.
