# Q1680: b256 - bitmap set_bit / get_bit index derivation mismatch (5)

## Question
Given the salt was rotated between the moment the payload was signed and the moment it is submitted, can an unprivileged attacker, entering through `execute_intents(signed: Vec<MultiPayload>)` submitted repeatedly by any account, exploit an inconsistency between how `get_bit` in `crates/bitmap/src/b256.rs` derives the (word, bit) index for writes versus reads, so a committed nonce reads back as unused, breaking the invariant ``get_bit(n)` after `set_bit(n)` == true for all 2^256 nonces` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [crates/bitmap/src/b256.rs](crates/bitmap/src/b256.rs) - `get_bit` (cross-check `set_bit_to` in the same file)
- Entrypoint: `execute_intents(signed: Vec<MultiPayload>)` submitted repeatedly by any account
- Attacker controls: the 32-byte `nonce`, the `deadline`, the salt bytes embedded in a versioned nonce, and submission timing
- Exploit idea: Target the 248/8 split, endianness of the bit index, and any `u8`/`usize` truncation in the index computation. Set-up: the salt was rotated between the moment the payload was signed and the moment it is submitted.
- Invariant to test: `get_bit(n)` after `set_bit(n)` == true for all 2^256 nonces
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Property-test `get_bit` over random nonces including all-zero and all-ones; assert set/get agreement.
