# Q5226: mod - expired-nonce grinding to force a victim's cleanup (9)

## Question
Given the payload `deadline` is far in the future while the nonce's own deadline is near, can an unprivileged attacker, entering through `execute_intents(signed: Vec<MultiPayload>)` submitted repeatedly by any account, use `commit` in `contracts/defuse/core/src/nonce/mod.rs` to make a victim's still-valid nonce word eligible for cleanup by planting expired nonces in the same prefix, enabling replay of the victim's signed intent, breaking the invariant `cleanup of account A's nonce word never clears a bit set by a still-live authorisation of account A` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [contracts/defuse/core/src/nonce/mod.rs](contracts/defuse/core/src/nonce/mod.rs) - `commit` (cross-check `Nonces` in the same file)
- Entrypoint: `execute_intents(signed: Vec<MultiPayload>)` submitted repeatedly by any account
- Attacker controls: the 32-byte `nonce`, the `deadline`, the salt bytes embedded in a versioned nonce, and submission timing
- Exploit idea: The attacker can create accounts and commit nonces of their choosing; probe whether cleanup is scoped per account or can be steered onto a victim's word. Set-up: the payload `deadline` is far in the future while the nonce's own deadline is near.
- Invariant to test: cleanup of account A's nonce word never clears a bit set by a still-live authorisation of account A
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Sandbox: plant expired nonces sharing a prefix with a victim's live nonce; assert cleanup leaves the live bit set.
