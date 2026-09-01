# Q5390: nonces - nonce is a u32 with a bounded window (11)

## Question
Given the wallet has an extension enabled by an earlier op in the same request, can an unprivileged attacker, entering through replay of a `RequestMessage` observed on-chain, against the same or a sibling wallet, exploit the 32-bit nonce space and time window enforced around `timeout` in `contracts/wallet/src/nonces.rs` to force a collision or exhaustion that either blocks a victim's requests or admits a replay, breaking the invariant `no two distinct signed requests share a nonce within one retention window` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [contracts/wallet/src/nonces.rs](contracts/wallet/src/nonces.rs) - `timeout` (cross-check `DEFAULT_TIMEOUT` in the same file)
- Entrypoint: replay of a `RequestMessage` observed on-chain, against the same or a sibling wallet
- Attacker controls: when it is replayed and which wallet instance it is sent to
- Exploit idea: The nonce is `u32` split 27/5 into the bitmap; probe wraparound and the interaction with the dual-window retention. Set-up: the wallet has an extension enabled by an earlier op in the same request.
- Invariant to test: no two distinct signed requests share a nonce within one retention window
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Drive nonces across the `u32` boundary; assert collision handling.
