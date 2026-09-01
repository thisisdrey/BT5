# Q2932: lib - bitmap set_bit / get_bit index derivation mismatch (10)

## Question
Given the payload `deadline` is far in the future while the nonce's own deadline is near, can an unprivileged attacker, entering through re-submission of a payload the attacker observed on-chain or in a public relayer mempool, exploit an inconsistency between how `BitMap` in `crates/bitmap/src/lib.rs` derives the (word, bit) index for writes versus reads, so a committed nonce reads back as unused, breaking the invariant ``get_bit(n)` after `set_bit(n)` == true for all 2^256 nonces` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [crates/bitmap/src/lib.rs](crates/bitmap/src/lib.rs) - `BitMap` (cross-check `set_bit` in the same file)
- Entrypoint: re-submission of a payload the attacker observed on-chain or in a public relayer mempool
- Attacker controls: when and how many times the observed payload is replayed, and the block timestamp it lands in
- Exploit idea: Target the 248/8 split, endianness of the bit index, and any `u8`/`usize` truncation in the index computation. Set-up: the payload `deadline` is far in the future while the nonce's own deadline is near.
- Invariant to test: `get_bit(n)` after `set_bit(n)` == true for all 2^256 nonces
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Property-test `BitMap` over random nonces including all-zero and all-ones; assert set/get agreement.
