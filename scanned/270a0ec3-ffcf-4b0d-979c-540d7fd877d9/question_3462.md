# Q3462: deltas - deltas recorded on the matcher but not on storage (or the reverse)

## Question
Given the batch mixes NEP-141, NEP-245 and NEP-171 legs under one signer, can an unprivileged attacker, entering through `execute_intents(signed: Vec<MultiPayload>)` with a multi-intent, multi-token batch, find an ordering through `Deltas` in `contracts/defuse/core/src/engine/state/deltas.rs` where `Deltas` records a movement that the underlying `State` rejected, or where the underlying state mutates without the matcher observing it, breaking the invariant `for every token, the matcher's net delta == the net change written to persistent `token_balances`` and leading to protocol insolvency: sum of `token_balances` owed exceeds assets actually custodied by the Verifier?

## Target
- File/function: [contracts/defuse/core/src/engine/state/deltas.rs](contracts/defuse/core/src/engine/state/deltas.rs) - `Deltas` (cross-check `Transfers` in the same file)
- Entrypoint: `execute_intents(signed: Vec<MultiPayload>)` with a multi-intent, multi-token batch
- Attacker controls: the number of payloads, the intents in each, and every `(token_id, delta)` pair in every `TokenDiff`
- Exploit idea: `internal_add_balance` writes to `self.state` first, then `self.deltas.deposit(...)`; probe whether a failure between them (or a `?` early return) leaves the two out of sync mid-batch. Set-up: the batch mixes NEP-141, NEP-245 and NEP-171 legs under one signer.
- Invariant to test: for every token, the matcher's net delta == the net change written to persistent `token_balances`
- Expected Immunefi impact: Critical - Protocol insolvency: sum of `token_balances` owed exceeds assets actually custodied by the Verifier
- Fast validation: Inject a failure between the state write and the matcher update; assert the batch aborts and no partial write survives.
