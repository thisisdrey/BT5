# Q3793: lib - bitmap set_bit / get_bit index derivation mismatch (13)

## Question
Given the account still carries a legacy (pre-versioned) nonce map alongside the new one, can an unprivileged attacker, entering through `execute_intents(signed: Vec<MultiPayload>)` submitted repeatedly by any account, exploit an inconsistency between how `BitMap` in `crates/bitmap/src/lib.rs` derives the (word, bit) index for writes versus reads, so a committed nonce reads back as unused, breaking the invariant ``get_bit(n)` after `set_bit(n)` == true for all 2^256 nonces` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [crates/bitmap/src/lib.rs](crates/bitmap/src/lib.rs) - `BitMap` (cross-check `get_bit` in the same file)
- Entrypoint: `execute_intents(signed: Vec<MultiPayload>)` submitted repeatedly by any account
- Attacker controls: the 32-byte `nonce`, the `deadline`, the salt bytes embedded in a versioned nonce, and submission timing
- Exploit idea: Target the 248/8 split, endianness of the bit index, and any `u8`/`usize` truncation in the index computation. Set-up: the account still carries a legacy (pre-versioned) nonce map alongside the new one.
- Invariant to test: `get_bit(n)` after `set_bit(n)` == true for all 2^256 nonces
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Property-test `BitMap` over random nonces including all-zero and all-ones; assert set/get agreement.
