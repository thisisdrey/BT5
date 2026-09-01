# Q0612: lib - nonce deadline vs intent deadline boundary (3)

## Question
Given the nonce word for the target 248-bit prefix already carries a bit committed by an earlier intent, can an unprivileged attacker, entering through `ft_on_transfer` `msg` with `execute_intents`, replayed across separate deposits, exploit the `intent_deadline > deadline` and `deadline < Timestamp::now()` comparisons around `Timestamp` in `crates/primitives/time/src/lib.rs` at an exact-equality boundary so an expired authorisation is still accepted, or a valid one is permanently rejected, breaking the invariant `an intent executes only while `Timestamp::now() <= min(intent_deadline, nonce_deadline)`` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [crates/primitives/time/src/lib.rs](crates/primitives/time/src/lib.rs) - `Timestamp` (cross-check `from_nanos` in the same file)
- Entrypoint: `ft_on_transfer` `msg` with `execute_intents`, replayed across separate deposits
- Attacker controls: the nonce and deadline of each nested payload, plus the number of deposits
- Exploit idea: Target off-by-one at `==`, nanosecond truncation in the timestamp conversion, and the ordering of the two comparisons. Set-up: the nonce word for the target 248-bit prefix already carries a bit committed by an earlier intent.
- Invariant to test: an intent executes only while `Timestamp::now() <= min(intent_deadline, nonce_deadline)`
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Table-test `Timestamp` at deadline-1/deadline/deadline+1 nanoseconds; assert the accept/reject boundary.
