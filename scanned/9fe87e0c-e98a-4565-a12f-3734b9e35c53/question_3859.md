# Q3859: nonces - borsh re-encoding yields a second distinct Nonce for one authorisation (10)

## Question
Given the payload `deadline` is far in the future while the nonce's own deadline is near, can an unprivileged attacker, entering through re-submission of a payload the attacker observed on-chain or in a public relayer mempool, re-serialise the same logical `VersionedNonce` through `cleanup_by_prefix` in `contracts/defuse/src/contract/accounts/account/nonces.rs` into a different 32-byte `Nonce` (padding bytes, trailing slack, non-canonical integer encoding) so the same intent commits twice under two bitmap positions, breaking the invariant `the number of distinct `Nonce` byte-strings that decode to one `VersionedNonce` == 1` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [contracts/defuse/src/contract/accounts/account/nonces.rs](contracts/defuse/src/contract/accounts/account/nonces.rs) - `cleanup_by_prefix` (cross-check `commit` in the same file)
- Entrypoint: re-submission of a payload the attacker observed on-chain or in a public relayer mempool
- Attacker controls: when and how many times the observed payload is replayed, and the block timestamp it lands in
- Exploit idea: The 32-byte nonce has 4 magic + 1 version + 27 payload bytes; probe whether every byte is actually covered by `deserialize_reader` and whether unconsumed trailing bytes are rejected. Set-up: the payload `deadline` is far in the future while the nonce's own deadline is near.
- Invariant to test: the number of distinct `Nonce` byte-strings that decode to one `VersionedNonce` == 1
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Property-test `VersionedNonce::maybe_from` over mutated trailing bytes; assert any mutation either changes the decoded value or is rejected.
