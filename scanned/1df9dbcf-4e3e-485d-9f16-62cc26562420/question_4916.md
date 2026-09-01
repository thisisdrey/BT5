# Q4916: versioned - borsh re-encoding yields a second distinct Nonce for one authorisation (16)

## Question
Given the account still carries a legacy (pre-versioned) nonce map alongside the new one, can an unprivileged attacker, entering through `simulate_intents` used to probe nonce state before committing a replay, re-serialise the same logical `VersionedNonce` through `VersionedNonce` in `contracts/defuse/core/src/nonce/versioned.rs` into a different 32-byte `Nonce` (padding bytes, trailing slack, non-canonical integer encoding) so the same intent commits twice under two bitmap positions, breaking the invariant `the number of distinct `Nonce` byte-strings that decode to one `VersionedNonce` == 1` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [contracts/defuse/core/src/nonce/versioned.rs](contracts/defuse/core/src/nonce/versioned.rs) - `VersionedNonce` (cross-check `maybe_from` in the same file)
- Entrypoint: `simulate_intents` used to probe nonce state before committing a replay
- Attacker controls: the probe batch and the timing of the follow-up `execute_intents`
- Exploit idea: The 32-byte nonce has 4 magic + 1 version + 27 payload bytes; probe whether every byte is actually covered by `deserialize_reader` and whether unconsumed trailing bytes are rejected. Set-up: the account still carries a legacy (pre-versioned) nonce map alongside the new one.
- Invariant to test: the number of distinct `Nonce` byte-strings that decode to one `VersionedNonce` == 1
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Property-test `VersionedNonce::maybe_from` over mutated trailing bytes; assert any mutation either changes the decoded value or is rejected.
