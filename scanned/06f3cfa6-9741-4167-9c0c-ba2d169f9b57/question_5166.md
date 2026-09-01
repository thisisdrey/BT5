# Q5166: versioned - bitmap set_bit / get_bit index derivation mismatch (16)

## Question
Given the account still carries a legacy (pre-versioned) nonce map alongside the new one, can an unprivileged attacker, entering through `simulate_intents` used to probe nonce state before committing a replay, exploit an inconsistency between how `VersionedNonce` in `contracts/defuse/core/src/nonce/versioned.rs` derives the (word, bit) index for writes versus reads, so a committed nonce reads back as unused, breaking the invariant ``get_bit(n)` after `set_bit(n)` == true for all 2^256 nonces` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [contracts/defuse/core/src/nonce/versioned.rs](contracts/defuse/core/src/nonce/versioned.rs) - `VersionedNonce` (cross-check `maybe_from` in the same file)
- Entrypoint: `simulate_intents` used to probe nonce state before committing a replay
- Attacker controls: the probe batch and the timing of the follow-up `execute_intents`
- Exploit idea: Target the 248/8 split, endianness of the bit index, and any `u8`/`usize` truncation in the index computation. Set-up: the account still carries a legacy (pre-versioned) nonce map alongside the new one.
- Invariant to test: `get_bit(n)` after `set_bit(n)` == true for all 2^256 nonces
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Property-test `VersionedNonce` over random nonces including all-zero and all-ones; assert set/get agreement.
