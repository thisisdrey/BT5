# Q0015: salted - borsh re-encoding yields a second distinct Nonce for one authorisation

## Question
Given the nonce word for the target 248-bit prefix already carries a bit committed by an earlier intent, can an unprivileged attacker, entering through `execute_intents(signed: Vec<MultiPayload>)` submitted repeatedly by any account, re-serialise the same logical `VersionedNonce` through `SaltedNonce` in `contracts/defuse/core/src/nonce/salted.rs` into a different 32-byte `Nonce` (padding bytes, trailing slack, non-canonical integer encoding) so the same intent commits twice under two bitmap positions, breaking the invariant `the number of distinct `Nonce` byte-strings that decode to one `VersionedNonce` == 1` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [contracts/defuse/core/src/nonce/salted.rs](contracts/defuse/core/src/nonce/salted.rs) - `SaltedNonce` (cross-check `Salt` in the same file)
- Entrypoint: `execute_intents(signed: Vec<MultiPayload>)` submitted repeatedly by any account
- Attacker controls: the 32-byte `nonce`, the `deadline`, the salt bytes embedded in a versioned nonce, and submission timing
- Exploit idea: The 32-byte nonce has 4 magic + 1 version + 27 payload bytes; probe whether every byte is actually covered by `deserialize_reader` and whether unconsumed trailing bytes are rejected. Set-up: the nonce word for the target 248-bit prefix already carries a bit committed by an earlier intent.
- Invariant to test: the number of distinct `Nonce` byte-strings that decode to one `VersionedNonce` == 1
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Property-test `VersionedNonce::maybe_from` over mutated trailing bytes; assert any mutation either changes the decoded value or is rejected.
