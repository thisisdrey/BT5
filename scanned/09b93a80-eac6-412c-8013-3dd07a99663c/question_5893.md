# Q5893: expirable - timestamp conversion overflow or negative deadline (12)

## Question
Given the payload `deadline` is far in the future while the nonce's own deadline is near, can an unprivileged attacker, entering through `simulate_intents` used to probe nonce state before committing a replay, supply a `deadline` to `ExpirableNonce` in `contracts/defuse/core/src/nonce/expirable.rs` that overflows or wraps the `i64`/`u64` nanosecond conversion so the expiry comparison inverts, breaking the invariant `an intent past its deadline never executes, for every representable `deadline`` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [contracts/defuse/core/src/nonce/expirable.rs](contracts/defuse/core/src/nonce/expirable.rs) - `ExpirableNonce` (cross-check `has_expired` in the same file)
- Entrypoint: `simulate_intents` used to probe nonce state before committing a replay
- Attacker controls: the probe batch and the timing of the follow-up `execute_intents`
- Exploit idea: Target `TimestampNanoSeconds` borsh/serde conversion at `i64::MIN`, `i64::MAX`, and negative values reachable from a signed payload. Set-up: the payload `deadline` is far in the future while the nonce's own deadline is near.
- Invariant to test: an intent past its deadline never executes, for every representable `deadline`
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Property-test the `Timestamp` conversion over extreme values; assert `has_expired()` is monotone.
