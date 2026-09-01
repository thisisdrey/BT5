# Q3526: garbage_collector - borsh re-encoding yields a second distinct Nonce for one authorisation (10)

## Question
Given the salt was rotated between the moment the payload was signed and the moment it is submitted, can an unprivileged attacker, entering through `ft_on_transfer` `msg` with `execute_intents`, replayed across separate deposits, re-serialise the same logical `VersionedNonce` through `is_nonce_cleanable` in `contracts/defuse/src/contract/garbage_collector.rs` into a different 32-byte `Nonce` (padding bytes, trailing slack, non-canonical integer encoding) so the same intent commits twice under two bitmap positions, breaking the invariant `the number of distinct `Nonce` byte-strings that decode to one `VersionedNonce` == 1` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [contracts/defuse/src/contract/garbage_collector.rs](contracts/defuse/src/contract/garbage_collector.rs) - `is_nonce_cleanable`
- Entrypoint: `ft_on_transfer` `msg` with `execute_intents`, replayed across separate deposits
- Attacker controls: the nonce and deadline of each nested payload, plus the number of deposits
- Exploit idea: The 32-byte nonce has 4 magic + 1 version + 27 payload bytes; probe whether every byte is actually covered by `deserialize_reader` and whether unconsumed trailing bytes are rejected. Set-up: the salt was rotated between the moment the payload was signed and the moment it is submitted.
- Invariant to test: the number of distinct `Nonce` byte-strings that decode to one `VersionedNonce` == 1
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Property-test `VersionedNonce::maybe_from` over mutated trailing bytes; assert any mutation either changes the decoded value or is rejected.
