# Q4462: token_diff - fee_collector credit re-enters the matcher

## Question
Given the batch mixes NEP-141, NEP-245 and NEP-171 legs under one signer, can an unprivileged attacker, entering through `execute_intents(signed: Vec<MultiPayload>)` with a multi-intent, multi-token batch, exploit that `TokenDiff` in `contracts/defuse/core/src/intents/token_diff.rs` credits `fee_collector` through `internal_add_balance`, which itself records a matcher deposit, so the fee itself must be matched by a withdrawal that no intent supplies, breaking the invariant `fee deposits recorded in the matcher == fee amounts subtracted from the paying legs` and leading to protocol insolvency: sum of `token_balances` owed exceeds assets actually custodied by the Verifier?

## Target
- File/function: [contracts/defuse/core/src/intents/token_diff.rs](contracts/defuse/core/src/intents/token_diff.rs) - `TokenDiff` (cross-check `token_fee` in the same file)
- Entrypoint: `execute_intents(signed: Vec<MultiPayload>)` with a multi-intent, multi-token batch
- Attacker controls: the number of payloads, the intents in each, and every `(token_id, delta)` pair in every `TokenDiff`
- Exploit idea: Trace whether fee deposits are excluded from the conservation check or must be balanced by the fee-paying legs; a mismatch either blocks honest batches or permits unbalanced ones. Set-up: the batch mixes NEP-141, NEP-245 and NEP-171 legs under one signer.
- Invariant to test: fee deposits recorded in the matcher == fee amounts subtracted from the paying legs
- Expected Immunefi impact: Critical - Protocol insolvency: sum of `token_balances` owed exceeds assets actually custodied by the Verifier
- Fast validation: Execute a fee-bearing `TokenDiff`; assert `finalize()` succeeds and total credits equal total debits including fees.
