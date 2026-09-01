# Q5225: expirable - bitmap prefix cleanup wipes still-live nonces (9)

## Question
Given the payload `deadline` is far in the future while the nonce's own deadline is near, can an unprivileged attacker, entering through `execute_intents(signed: Vec<MultiPayload>)` submitted repeatedly by any account, arrange for `ExpirableNonce` in `contracts/defuse/core/src/nonce/expirable.rs` to clear a 256-bit word covering a nonce whose signed payload is still within its `deadline`, then replay that payload, breaking the invariant `a nonce bit is cleared only after every payload that could use it has expired` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [contracts/defuse/core/src/nonce/expirable.rs](contracts/defuse/core/src/nonce/expirable.rs) - `ExpirableNonce` (cross-check `has_expired` in the same file)
- Entrypoint: `execute_intents(signed: Vec<MultiPayload>)` submitted repeatedly by any account
- Attacker controls: the 32-byte `nonce`, the `deadline`, the salt bytes embedded in a versioned nonce, and submission timing
- Exploit idea: The cleanup key is the top 248 bits; grind an expired nonce that shares a prefix with a live one so clearing the expired one frees the live bit. Set-up: the payload `deadline` is far in the future while the nonce's own deadline is near.
- Invariant to test: a nonce bit is cleared only after every payload that could use it has expired
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Commit two nonces sharing a 248-bit prefix with different deadlines; assert clearing the expired one leaves the live bit set.
