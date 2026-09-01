# Q5164: mod - bitmap set_bit / get_bit index derivation mismatch (12)

## Question
Given the payload `deadline` is far in the future while the nonce's own deadline is near, can an unprivileged attacker, entering through `simulate_intents` used to probe nonce state before committing a replay, exploit an inconsistency between how `cleanup_by_prefix` in `contracts/defuse/core/src/nonce/mod.rs` derives the (word, bit) index for writes versus reads, so a committed nonce reads back as unused, breaking the invariant ``get_bit(n)` after `set_bit(n)` == true for all 2^256 nonces` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [contracts/defuse/core/src/nonce/mod.rs](contracts/defuse/core/src/nonce/mod.rs) - `cleanup_by_prefix` (cross-check `is_used` in the same file)
- Entrypoint: `simulate_intents` used to probe nonce state before committing a replay
- Attacker controls: the probe batch and the timing of the follow-up `execute_intents`
- Exploit idea: Target the 248/8 split, endianness of the bit index, and any `u8`/`usize` truncation in the index computation. Set-up: the payload `deadline` is far in the future while the nonce's own deadline is near.
- Invariant to test: `get_bit(n)` after `set_bit(n)` == true for all 2^256 nonces
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Property-test `cleanup_by_prefix` over random nonces including all-zero and all-ones; assert set/get agreement.
