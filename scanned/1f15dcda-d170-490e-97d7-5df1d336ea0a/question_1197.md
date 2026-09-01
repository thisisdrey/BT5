# Q1197: borsh - nonce deadline vs intent deadline boundary (2)

## Question
Given the nonce word for the target 248-bit prefix already carries a bit committed by an earlier intent, can an unprivileged attacker, entering through re-submission of a payload the attacker observed on-chain or in a public relayer mempool, exploit the `intent_deadline > deadline` and `deadline < Timestamp::now()` comparisons around `TimestampMilliSeconds` in `crates/primitives/time/src/borsh.rs` at an exact-equality boundary so an expired authorisation is still accepted, or a valid one is permanently rejected, breaking the invariant `an intent executes only while `Timestamp::now() <= min(intent_deadline, nonce_deadline)`` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [crates/primitives/time/src/borsh.rs](crates/primitives/time/src/borsh.rs) - `TimestampMilliSeconds` (cross-check `TimestampNanoSeconds` in the same file)
- Entrypoint: re-submission of a payload the attacker observed on-chain or in a public relayer mempool
- Attacker controls: when and how many times the observed payload is replayed, and the block timestamp it lands in
- Exploit idea: Target off-by-one at `==`, nanosecond truncation in the timestamp conversion, and the ordering of the two comparisons. Set-up: the nonce word for the target 248-bit prefix already carries a bit committed by an earlier intent.
- Invariant to test: an intent executes only while `Timestamp::now() <= min(intent_deadline, nonce_deadline)`
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Table-test `TimestampMilliSeconds` at deadline-1/deadline/deadline+1 nanoseconds; assert the accept/reject boundary.
