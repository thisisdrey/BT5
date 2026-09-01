# Q4273: expirable - nonce deadline vs intent deadline boundary (6)

## Question
Given the salt was rotated between the moment the payload was signed and the moment it is submitted, can an unprivileged attacker, entering through re-submission of a payload the attacker observed on-chain or in a public relayer mempool, exploit the `intent_deadline > deadline` and `deadline < Timestamp::now()` comparisons around `ExpirableNonce` in `contracts/defuse/core/src/nonce/expirable.rs` at an exact-equality boundary so an expired authorisation is still accepted, or a valid one is permanently rejected, breaking the invariant `an intent executes only while `Timestamp::now() <= min(intent_deadline, nonce_deadline)`` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [contracts/defuse/core/src/nonce/expirable.rs](contracts/defuse/core/src/nonce/expirable.rs) - `ExpirableNonce` (cross-check `has_expired` in the same file)
- Entrypoint: re-submission of a payload the attacker observed on-chain or in a public relayer mempool
- Attacker controls: when and how many times the observed payload is replayed, and the block timestamp it lands in
- Exploit idea: Target off-by-one at `==`, nanosecond truncation in the timestamp conversion, and the ordering of the two comparisons. Set-up: the salt was rotated between the moment the payload was signed and the moment it is submitted.
- Invariant to test: an intent executes only while `Timestamp::now() <= min(intent_deadline, nonce_deadline)`
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Table-test `ExpirableNonce` at deadline-1/deadline/deadline+1 nanoseconds; assert the accept/reject boundary.
