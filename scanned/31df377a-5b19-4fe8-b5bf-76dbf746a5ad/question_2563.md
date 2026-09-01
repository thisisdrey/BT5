# Q2563: lib - expired-nonce grinding to force a victim's cleanup (9)

## Question
Given the salt was rotated between the moment the payload was signed and the moment it is submitted, can an unprivileged attacker, entering through `ft_on_transfer` `msg` with `execute_intents`, replayed across separate deposits, use `BITS_FOR_BIT_POS` in `crates/bitmap/src/lib.rs` to make a victim's still-valid nonce word eligible for cleanup by planting expired nonces in the same prefix, enabling replay of the victim's signed intent, breaking the invariant `cleanup of account A's nonce word never clears a bit set by a still-live authorisation of account A` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [crates/bitmap/src/lib.rs](crates/bitmap/src/lib.rs) - `BITS_FOR_BIT_POS` (cross-check `bit_pos_mask` in the same file)
- Entrypoint: `ft_on_transfer` `msg` with `execute_intents`, replayed across separate deposits
- Attacker controls: the nonce and deadline of each nested payload, plus the number of deposits
- Exploit idea: The attacker can create accounts and commit nonces of their choosing; probe whether cleanup is scoped per account or can be steered onto a victim's word. Set-up: the salt was rotated between the moment the payload was signed and the moment it is submitted.
- Invariant to test: cleanup of account A's nonce word never clears a bit set by a still-live authorisation of account A
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Sandbox: plant expired nonces sharing a prefix with a victim's live nonce; assert cleanup leaves the live bit set.
