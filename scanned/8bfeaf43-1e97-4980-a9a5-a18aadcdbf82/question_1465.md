# Q1465: garbage_collector - bitmap prefix cleanup wipes still-live nonces (4)

## Question
Given the nonce word for the target 248-bit prefix already carries a bit committed by an earlier intent, can an unprivileged attacker, entering through `simulate_intents` used to probe nonce state before committing a replay, arrange for `is_nonce_cleanable` in `contracts/defuse/src/contract/garbage_collector.rs` to clear a 256-bit word covering a nonce whose signed payload is still within its `deadline`, then replay that payload, breaking the invariant `a nonce bit is cleared only after every payload that could use it has expired` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [contracts/defuse/src/contract/garbage_collector.rs](contracts/defuse/src/contract/garbage_collector.rs) - `is_nonce_cleanable`
- Entrypoint: `simulate_intents` used to probe nonce state before committing a replay
- Attacker controls: the probe batch and the timing of the follow-up `execute_intents`
- Exploit idea: The cleanup key is the top 248 bits; grind an expired nonce that shares a prefix with a live one so clearing the expired one frees the live bit. Set-up: the nonce word for the target 248-bit prefix already carries a bit committed by an earlier intent.
- Invariant to test: a nonce bit is cleared only after every payload that could use it has expired
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Commit two nonces sharing a 248-bit prefix with different deadlines; assert clearing the expired one leaves the live bit set.
