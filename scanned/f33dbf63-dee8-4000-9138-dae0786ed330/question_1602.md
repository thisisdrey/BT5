# Q1602: token_diff - self-cancelling deltas inside one TokenDiff

## Question
Given the batch mixes NEP-141, NEP-245 and NEP-171 legs under one signer, can an unprivileged attacker, entering through `execute_intents(signed: Vec<MultiPayload>)` with a multi-intent, multi-token batch, submit a `TokenDiff` whose `diff` map reaches `TokenDiff` in `contracts/defuse/core/src/intents/token_diff.rs` with entries that cancel at the matcher level but each independently trigger (or skip) the fee branch, so fees are charged on notional that never moved, breaking the invariant `fees credited to `fee_collector` == fees owed on value that actually changed hands` and leading to theft of unclaimed protocol fees / protocol fee bypass?

## Target
- File/function: [contracts/defuse/core/src/intents/token_diff.rs](contracts/defuse/core/src/intents/token_diff.rs) - `TokenDiff` (cross-check `closure_delta` in the same file)
- Entrypoint: `execute_intents(signed: Vec<MultiPayload>)` with a multi-intent, multi-token batch
- Attacker controls: the number of payloads, the intents in each, and every `(token_id, delta)` pair in every `TokenDiff`
- Exploit idea: Only negative deltas pay fees; craft a diff whose negative legs are matched by the signer's own positive legs so the net movement is zero but fee accounting is not. Set-up: the batch mixes NEP-141, NEP-245 and NEP-171 legs under one signer.
- Invariant to test: fees credited to `fee_collector` == fees owed on value that actually changed hands
- Expected Immunefi impact: High - Theft of unclaimed protocol fees / protocol fee bypass
- Fast validation: Execute a self-cancelling `TokenDiff`; assert `fees_collected` is zero when net movement is zero.
