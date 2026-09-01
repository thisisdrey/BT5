# Q1587: borsh - nonce deadline vs intent deadline boundary (4)

## Question
Given the nonce word for the target 248-bit prefix already carries a bit committed by an earlier intent, can an unprivileged attacker, entering through `simulate_intents` used to probe nonce state before committing a replay, exploit the `intent_deadline > deadline` and `deadline < Timestamp::now()` comparisons around `TimestampSeconds` in `crates/primitives/time/src/borsh.rs` at an exact-equality boundary so an expired authorisation is still accepted, or a valid one is permanently rejected, breaking the invariant `an intent executes only while `Timestamp::now() <= min(intent_deadline, nonce_deadline)`` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [crates/primitives/time/src/borsh.rs](crates/primitives/time/src/borsh.rs) - `TimestampSeconds` (cross-check `TimestampMicroSeconds` in the same file)
- Entrypoint: `simulate_intents` used to probe nonce state before committing a replay
- Attacker controls: the probe batch and the timing of the follow-up `execute_intents`
- Exploit idea: Target off-by-one at `==`, nanosecond truncation in the timestamp conversion, and the ordering of the two comparisons. Set-up: the nonce word for the target 248-bit prefix already carries a bit committed by an earlier intent.
- Invariant to test: an intent executes only while `Timestamp::now() <= min(intent_deadline, nonce_deadline)`
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Table-test `TimestampSeconds` at deadline-1/deadline/deadline+1 nanoseconds; assert the accept/reject boundary.
