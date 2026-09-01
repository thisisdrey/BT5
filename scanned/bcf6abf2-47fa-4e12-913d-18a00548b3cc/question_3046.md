# Q3046: signer - nonce cleanup driven by an attacker's timing (7)

## Question
Given the wallet's nonce bitmap has just rotated `current` into `old`, can an unprivileged attacker, entering through replay of a `RequestMessage` observed on-chain, against the same or a sibling wallet, call `sign_hash` in `contracts/wallet/signatures/ed25519/src/signer.rs` to force a nonce rotation at a moment that discards a victim's in-flight nonce, enabling replay of their signed request, breaking the invariant `a nonce is discarded only after every request that could carry it has expired` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [contracts/wallet/signatures/ed25519/src/signer.rs](contracts/wallet/signatures/ed25519/src/signer.rs) - `sign_hash` (cross-check `sign_offchain_msg` in the same file)
- Entrypoint: replay of a `RequestMessage` observed on-chain, against the same or a sibling wallet
- Attacker controls: when it is replayed and which wallet instance it is sent to
- Exploit idea: Rotation is time-driven and triggered by ordinary calls; probe whether an unprivileged caller can advance it early. Set-up: the wallet's nonce bitmap has just rotated `current` into `old`.
- Invariant to test: a nonce is discarded only after every request that could carry it has expired
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Trigger rotation immediately before a victim's request lands; assert the request cannot execute twice.
