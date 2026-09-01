# Q5202: contract - nonce is a u32 with a bounded window (24)

## Question
Given the wallet's nonce bitmap has just rotated `current` into `old`, can an unprivileged attacker, entering through an on-chain call that triggers nonce rotation or cleanup before a victim's request lands, exploit the 32-bit nonce space and time window enforced around `execute_signed` in `contracts/wallet/src/contract.rs` to force a collision or exhaustion that either blocks a victim's requests or admits a replay, breaking the invariant `no two distinct signed requests share a nonce within one retention window` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [contracts/wallet/src/contract.rs](contracts/wallet/src/contract.rs) - `execute_signed` (cross-check `check_extension_enabled` in the same file)
- Entrypoint: an on-chain call that triggers nonce rotation or cleanup before a victim's request lands
- Attacker controls: the timing of the triggering call
- Exploit idea: The nonce is `u32` split 27/5 into the bitmap; probe wraparound and the interaction with the dual-window retention. Set-up: the wallet's nonce bitmap has just rotated `current` into `old`.
- Invariant to test: no two distinct signed requests share a nonce within one retention window
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Drive nonces across the `u32` boundary; assert collision handling.
