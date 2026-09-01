# Q5352: versioned - expired-nonce grinding to force a victim's cleanup (15)

## Question
Given the account still carries a legacy (pre-versioned) nonce map alongside the new one, can an unprivileged attacker, entering through `ft_on_transfer` `msg` with `execute_intents`, replayed across separate deposits, use `maybe_from` in `contracts/defuse/core/src/nonce/versioned.rs` to make a victim's still-valid nonce word eligible for cleanup by planting expired nonces in the same prefix, enabling replay of the victim's signed intent, breaking the invariant `cleanup of account A's nonce word never clears a bit set by a still-live authorisation of account A` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [contracts/defuse/core/src/nonce/versioned.rs](contracts/defuse/core/src/nonce/versioned.rs) - `maybe_from` (cross-check `VERSIONED_MAGIC_PREFIX` in the same file)
- Entrypoint: `ft_on_transfer` `msg` with `execute_intents`, replayed across separate deposits
- Attacker controls: the nonce and deadline of each nested payload, plus the number of deposits
- Exploit idea: The attacker can create accounts and commit nonces of their choosing; probe whether cleanup is scoped per account or can be steered onto a victim's word. Set-up: the account still carries a legacy (pre-versioned) nonce map alongside the new one.
- Invariant to test: cleanup of account A's nonce word never clears a bit set by a still-live authorisation of account A
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Sandbox: plant expired nonces sharing a prefix with a victim's live nonce; assert cleanup leaves the live bit set.
