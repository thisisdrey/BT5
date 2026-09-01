# Q5042: versioned - bitmap set_bit / get_bit index derivation mismatch (14)

## Question
Given the account still carries a legacy (pre-versioned) nonce map alongside the new one, can an unprivileged attacker, entering through re-submission of a payload the attacker observed on-chain or in a public relayer mempool, exploit an inconsistency between how `VERSIONED_MAGIC_PREFIX` in `contracts/defuse/core/src/nonce/versioned.rs` derives the (word, bit) index for writes versus reads, so a committed nonce reads back as unused, breaking the invariant ``get_bit(n)` after `set_bit(n)` == true for all 2^256 nonces` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [contracts/defuse/core/src/nonce/versioned.rs](contracts/defuse/core/src/nonce/versioned.rs) - `VERSIONED_MAGIC_PREFIX` (cross-check `maybe_from` in the same file)
- Entrypoint: re-submission of a payload the attacker observed on-chain or in a public relayer mempool
- Attacker controls: when and how many times the observed payload is replayed, and the block timestamp it lands in
- Exploit idea: Target the 248/8 split, endianness of the bit index, and any `u8`/`usize` truncation in the index computation. Set-up: the account still carries a legacy (pre-versioned) nonce map alongside the new one.
- Invariant to test: `get_bit(n)` after `set_bit(n)` == true for all 2^256 nonces
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Property-test `VERSIONED_MAGIC_PREFIX` over random nonces including all-zero and all-ones; assert set/get agreement.
