# Q0088: b256 - bitmap set_bit / get_bit index derivation mismatch

## Question
Given the nonce word for the target 248-bit prefix already carries a bit committed by an earlier intent, can an unprivileged attacker, entering through `execute_intents(signed: Vec<MultiPayload>)` submitted repeatedly by any account, exploit an inconsistency between how `set_bit` in `crates/bitmap/src/b256.rs` derives the (word, bit) index for writes versus reads, so a committed nonce reads back as unused, breaking the invariant ``get_bit(n)` after `set_bit(n)` == true for all 2^256 nonces` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [crates/bitmap/src/b256.rs](crates/bitmap/src/b256.rs) - `set_bit` (cross-check `set_bit_to` in the same file)
- Entrypoint: `execute_intents(signed: Vec<MultiPayload>)` submitted repeatedly by any account
- Attacker controls: the 32-byte `nonce`, the `deadline`, the salt bytes embedded in a versioned nonce, and submission timing
- Exploit idea: Target the 248/8 split, endianness of the bit index, and any `u8`/`usize` truncation in the index computation. Set-up: the nonce word for the target 248-bit prefix already carries a bit committed by an earlier intent.
- Invariant to test: `get_bit(n)` after `set_bit(n)` == true for all 2^256 nonces
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Property-test `set_bit` over random nonces including all-zero and all-ones; assert set/get agreement.
