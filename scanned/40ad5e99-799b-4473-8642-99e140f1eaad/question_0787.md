# Q0787: ckd - nonce cleanup driven by an attacker's timing (4)

## Question
Given the wallet's nonce bitmap has just rotated `current` into `old`, can an unprivileged attacker, entering through an on-chain call that triggers nonce rotation or cleanup before a victim's request lands, call `APP_ID_DERIVATION_PREFIX` in `crates/mpc/kdf/src/ckd.rs` to force a nonce rotation at a moment that discards a victim's in-flight nonce, enabling replay of their signed request, breaking the invariant `a nonce is discarded only after every request that could carry it has expired` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [crates/mpc/kdf/src/ckd.rs](crates/mpc/kdf/src/ckd.rs) - `APP_ID_DERIVATION_PREFIX`
- Entrypoint: an on-chain call that triggers nonce rotation or cleanup before a victim's request lands
- Attacker controls: the timing of the triggering call
- Exploit idea: Rotation is time-driven and triggered by ordinary calls; probe whether an unprivileged caller can advance it early. Set-up: the wallet's nonce bitmap has just rotated `current` into `old`.
- Invariant to test: a nonce is discarded only after every request that could carry it has expired
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Trigger rotation immediately before a victim's request lands; assert the request cannot execute twice.
