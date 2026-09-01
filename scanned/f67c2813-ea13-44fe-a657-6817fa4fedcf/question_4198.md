# Q4198: mod - fee taken on a leg whose value never moved

## Question
Given the batch mixes NEP-141, NEP-245 and NEP-171 legs under one signer, can an unprivileged attacker, entering through `execute_intents(signed: Vec<MultiPayload>)` with a multi-intent, multi-token batch, make `finalize` in `contracts/defuse/core/src/engine/mod.rs` charge or skip the protocol fee on a delta whose counterparty leg is never matched, so `fees_collected` and the actual matched volume disagree, breaking the invariant `fees credited == `Pips::fee_ceil` over negative deltas that were actually matched` and leading to theft of unclaimed protocol fees / protocol fee bypass?

## Target
- File/function: [contracts/defuse/core/src/engine/mod.rs](contracts/defuse/core/src/engine/mod.rs) - `finalize` (cross-check `verify_intent_nonce` in the same file)
- Entrypoint: `execute_intents(signed: Vec<MultiPayload>)` with a multi-intent, multi-token batch
- Attacker controls: the number of payloads, the intents in each, and every `(token_id, delta)` pair in every `TokenDiff`
- Exploit idea: Fees are computed per negative delta at intent time; matching happens only at `finalize`. An unmatched negative delta should abort, but check every path where it does not. Set-up: the batch mixes NEP-141, NEP-245 and NEP-171 legs under one signer.
- Invariant to test: fees credited == `Pips::fee_ceil` over negative deltas that were actually matched
- Expected Immunefi impact: High - Theft of unclaimed protocol fees / protocol fee bypass
- Fast validation: Craft a diff whose negative leg is unmatched; assert either the batch aborts or no fee is credited.
