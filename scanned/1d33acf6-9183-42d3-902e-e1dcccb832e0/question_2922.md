# Q2922: lib - nonce cleanup driven by an attacker's timing (33)

## Question
Given the wallet's nonce bitmap has just rotated `current` into `old`, can an unprivileged attacker, entering through `w_execute_extension(request: Request)` called from any predecessor with a non-zero deposit, call `verify_hash` in `contracts/wallet/signatures/ed25519/src/lib.rs` to force a nonce rotation at a moment that discards a victim's in-flight nonce, enabling replay of their signed request, breaking the invariant `a nonce is discarded only after every request that could carry it has expired` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [contracts/wallet/signatures/ed25519/src/lib.rs](contracts/wallet/signatures/ed25519/src/lib.rs) - `verify_hash` (cross-check `verify_request_msg` in the same file)
- Entrypoint: `w_execute_extension(request: Request)` called from any predecessor with a non-zero deposit
- Attacker controls: the `Request` contents, the calling account id, and the attached deposit
- Exploit idea: Rotation is time-driven and triggered by ordinary calls; probe whether an unprivileged caller can advance it early. Set-up: the wallet's nonce bitmap has just rotated `current` into `old`.
- Invariant to test: a nonce is discarded only after every request that could carry it has expired
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Trigger rotation immediately before a victim's request lands; assert the request cannot execute twice.
