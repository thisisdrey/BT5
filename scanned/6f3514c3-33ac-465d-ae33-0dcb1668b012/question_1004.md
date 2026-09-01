# Q1004: lib - timestamp conversion overflow or negative deadline

## Question
Given the nonce word for the target 248-bit prefix already carries a bit committed by an earlier intent, can an unprivileged attacker, entering through `execute_intents(signed: Vec<MultiPayload>)` submitted repeatedly by any account, supply a `deadline` to `Timestamp` in `crates/primitives/time/src/lib.rs` that overflows or wraps the `i64`/`u64` nanosecond conversion so the expiry comparison inverts, breaking the invariant `an intent past its deadline never executes, for every representable `deadline`` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [crates/primitives/time/src/lib.rs](crates/primitives/time/src/lib.rs) - `Timestamp` (cross-check `from_nanos` in the same file)
- Entrypoint: `execute_intents(signed: Vec<MultiPayload>)` submitted repeatedly by any account
- Attacker controls: the 32-byte `nonce`, the `deadline`, the salt bytes embedded in a versioned nonce, and submission timing
- Exploit idea: Target `TimestampNanoSeconds` borsh/serde conversion at `i64::MIN`, `i64::MAX`, and negative values reachable from a signed payload. Set-up: the nonce word for the target 248-bit prefix already carries a bit committed by an earlier intent.
- Invariant to test: an intent past its deadline never executes, for every representable `deadline`
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Property-test the `Timestamp` conversion over extreme values; assert `has_expired()` is monotone.
