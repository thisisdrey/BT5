# Q5833: expirable - timestamp conversion overflow or negative deadline (11)

## Question
Given the payload `deadline` is far in the future while the nonce's own deadline is near, can an unprivileged attacker, entering through `ft_on_transfer` `msg` with `execute_intents`, replayed across separate deposits, supply a `deadline` to `has_expired` in `contracts/defuse/core/src/nonce/expirable.rs` that overflows or wraps the `i64`/`u64` nanosecond conversion so the expiry comparison inverts, breaking the invariant `an intent past its deadline never executes, for every representable `deadline`` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [contracts/defuse/core/src/nonce/expirable.rs](contracts/defuse/core/src/nonce/expirable.rs) - `has_expired` (cross-check `ExpirableNonce` in the same file)
- Entrypoint: `ft_on_transfer` `msg` with `execute_intents`, replayed across separate deposits
- Attacker controls: the nonce and deadline of each nested payload, plus the number of deposits
- Exploit idea: Target `TimestampNanoSeconds` borsh/serde conversion at `i64::MIN`, `i64::MAX`, and negative values reachable from a signed payload. Set-up: the payload `deadline` is far in the future while the nonce's own deadline is near.
- Invariant to test: an intent past its deadline never executes, for every representable `deadline`
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Property-test the `Timestamp` conversion over extreme values; assert `has_expired()` is monotone.
