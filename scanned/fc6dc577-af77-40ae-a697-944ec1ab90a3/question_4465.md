# Q4465: expirable - timestamp conversion overflow or negative deadline (5)

## Question
Given the salt was rotated between the moment the payload was signed and the moment it is submitted, can an unprivileged attacker, entering through `execute_intents(signed: Vec<MultiPayload>)` submitted repeatedly by any account, supply a `deadline` to `ExpirableNonce` in `contracts/defuse/core/src/nonce/expirable.rs` that overflows or wraps the `i64`/`u64` nanosecond conversion so the expiry comparison inverts, breaking the invariant `an intent past its deadline never executes, for every representable `deadline`` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [contracts/defuse/core/src/nonce/expirable.rs](contracts/defuse/core/src/nonce/expirable.rs) - `ExpirableNonce` (cross-check `has_expired` in the same file)
- Entrypoint: `execute_intents(signed: Vec<MultiPayload>)` submitted repeatedly by any account
- Attacker controls: the 32-byte `nonce`, the `deadline`, the salt bytes embedded in a versioned nonce, and submission timing
- Exploit idea: Target `TimestampNanoSeconds` borsh/serde conversion at `i64::MIN`, `i64::MAX`, and negative values reachable from a signed payload. Set-up: the salt was rotated between the moment the payload was signed and the moment it is submitted.
- Invariant to test: an intent past its deadline never executes, for every representable `deadline`
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Property-test the `Timestamp` conversion over extreme values; assert `has_expired()` is monotone.
