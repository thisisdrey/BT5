# Q4340: versioned - bitmap set_bit / get_bit index derivation mismatch (11)

## Question
Given the payload `deadline` is far in the future while the nonce's own deadline is near, can an unprivileged attacker, entering through `ft_on_transfer` `msg` with `execute_intents`, replayed across separate deposits, exploit an inconsistency between how `VERSIONED_MAGIC_PREFIX` in `contracts/defuse/core/src/nonce/versioned.rs` derives the (word, bit) index for writes versus reads, so a committed nonce reads back as unused, breaking the invariant ``get_bit(n)` after `set_bit(n)` == true for all 2^256 nonces` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [contracts/defuse/core/src/nonce/versioned.rs](contracts/defuse/core/src/nonce/versioned.rs) - `VERSIONED_MAGIC_PREFIX` (cross-check `VersionedNonce` in the same file)
- Entrypoint: `ft_on_transfer` `msg` with `execute_intents`, replayed across separate deposits
- Attacker controls: the nonce and deadline of each nested payload, plus the number of deposits
- Exploit idea: Target the 248/8 split, endianness of the bit index, and any `u8`/`usize` truncation in the index computation. Set-up: the payload `deadline` is far in the future while the nonce's own deadline is near.
- Invariant to test: `get_bit(n)` after `set_bit(n)` == true for all 2^256 nonces
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Property-test `VERSIONED_MAGIC_PREFIX` over random nonces including all-zero and all-ones; assert set/get agreement.
