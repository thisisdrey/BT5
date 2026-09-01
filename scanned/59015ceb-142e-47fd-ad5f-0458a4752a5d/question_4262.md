# Q4262: mod - fee taken on a leg whose value never moved (2)

## Question
Given the batch mixes NEP-141, NEP-245 and NEP-171 legs under one signer, can an unprivileged attacker, entering through `simulate_intents` to find a batch that reports balanced, then `execute_intents` to commit it, make `execute_signed_intent` in `contracts/defuse/core/src/engine/mod.rs` charge or skip the protocol fee on a delta whose counterparty leg is never matched, so `fees_collected` and the actual matched volume disagree, breaking the invariant `fees credited == `Pips::fee_ceil` over negative deltas that were actually matched` and leading to theft of unclaimed protocol fees / protocol fee bypass?

## Target
- File/function: [contracts/defuse/core/src/engine/mod.rs](contracts/defuse/core/src/engine/mod.rs) - `execute_signed_intent` (cross-check `execute_signed_intents` in the same file)
- Entrypoint: `simulate_intents` to find a batch that reports balanced, then `execute_intents` to commit it
- Attacker controls: the entire batch across both calls
- Exploit idea: Fees are computed per negative delta at intent time; matching happens only at `finalize`. An unmatched negative delta should abort, but check every path where it does not. Set-up: the batch mixes NEP-141, NEP-245 and NEP-171 legs under one signer.
- Invariant to test: fees credited == `Pips::fee_ceil` over negative deltas that were actually matched
- Expected Immunefi impact: High - Theft of unclaimed protocol fees / protocol fee bypass
- Fast validation: Craft a diff whose negative leg is unmatched; assert either the batch aborts or no fee is credited.
